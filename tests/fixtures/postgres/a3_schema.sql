-- The Postgres schema install_postgres_schema created in agent-core 0.1.0a3 (e5368dc),
-- with the rows 0.1.0a3 wrote: four audit events (three model calls and one approval
-- request) and one pending approval, all synthetic. Replayed as the owner role into an
-- empty database that already has the requester and approver roles, for the a3 -> a4
-- upgrade tests. Captured with pg_dump, minus session settings, ownership and comments.

CREATE FUNCTION public.agent_core_approvals_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
        
DECLARE
    requester_role text := 'agent_core_requester';
    approver_role text := 'agent_core_approver';
    -- The shapes the library writes; rows of any other shape are refused, so
    -- casts never depend on session settings and every row parses on read.
    timestamp_shape text :=
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9][.][0-9]{6}Z$';
    principal_shape text := '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$';
    opaque_shape text := '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$';
    run_context jsonb;
    is_overlong boolean;
    as_requester boolean;
    as_approver boolean;
    db_now timestamptz := statement_timestamp();
    is_expired boolean;
BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION 'approval requests are never deleted';
    END IF;
    as_requester := pg_has_role(current_user, requester_role, 'MEMBER')
        AND NOT pg_has_role(current_user, approver_role, 'MEMBER')
        AND NOT pg_has_role(session_user, approver_role, 'MEMBER');
    as_approver := pg_has_role(current_user, approver_role, 'MEMBER')
        AND NOT pg_has_role(current_user, requester_role, 'MEMBER')
        AND NOT pg_has_role(session_user, requester_role, 'MEMBER');

    IF TG_OP = 'INSERT' THEN
        IF NOT as_requester THEN
            RAISE EXCEPTION 'only the requester role may submit approval requests';
        END IF;
        IF NEW.id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR NEW.action !~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*)*$'
           OR length(NEW.action) > 100
           OR length(NEW.summary) NOT BETWEEN 1 AND 500
           OR NEW.payload_sha256 !~ '^[0-9a-f]{64}$'
           OR NEW.requested_by !~ principal_shape
           OR NEW.required_role !~ '^[a-z][a-z0-9_.-]{0,63}$'
           OR (NEW.created_at !~ timestamp_shape OR to_char((NEW.created_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> NEW.created_at)
           OR (NEW.expires_at !~ timestamp_shape OR to_char((NEW.expires_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> NEW.expires_at) THEN
            RAISE EXCEPTION 'a new approval request has a field of the wrong shape';
        END IF;
        IF jsonb_typeof(NEW.delegates::jsonb) <> 'array'
           OR jsonb_array_length(NEW.delegates::jsonb) > 16
           OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(NEW.delegates::jsonb) AS delegate
               WHERE jsonb_typeof(delegate) <> 'string' OR delegate #>> '{}' !~ principal_shape
           ) THEN
            RAISE EXCEPTION 'delegates must be at most 16 principal ids';
        END IF;
        IF NEW.run_context IS NOT NULL THEN
            run_context := NEW.run_context::jsonb;
            IF jsonb_typeof(run_context) <> 'object'
               OR EXISTS (
                   SELECT 1 FROM jsonb_object_keys(run_context) AS key
                   WHERE key NOT IN ('run_id', 'external_ids')
               )
               OR jsonb_typeof(run_context -> 'run_id') IS DISTINCT FROM 'string'
               OR run_context ->> 'run_id' !~ opaque_shape
               OR jsonb_typeof(COALESCE(run_context -> 'external_ids', '{}'::jsonb)) <> 'object'
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_each(COALESCE(run_context -> 'external_ids', '{}'::jsonb)) AS id
                   WHERE id.key !~ '^[a-z][a-z0-9_]{0,63}$'
                      OR jsonb_typeof(id.value) <> 'string'
                      OR id.value #>> '{}' !~ opaque_shape
               ) THEN
                RAISE EXCEPTION 'run_context must be a run id and opaque external ids';
            END IF;
        END IF;
        IF NEW.status <> 'pending' OR NEW.decision IS NOT NULL OR NEW.resolved_by IS NOT NULL
           OR NEW.resolved_at IS NOT NULL OR NEW.consumed_at IS NOT NULL
           OR NEW.closed_at IS NOT NULL OR NEW.reason IS NOT NULL THEN
            RAISE EXCEPTION 'a new approval request must be pending and undecided';
        END IF;
        IF NEW.created_at::timestamptz > db_now + interval '5 minutes'
           OR NEW.expires_at::timestamptz <= NEW.created_at::timestamptz
           OR NEW.expires_at::timestamptz > NEW.created_at::timestamptz + interval '7 days' THEN
            RAISE EXCEPTION 'an approval request must start by now and live at most 7 days';
        END IF;
        RETURN NEW;
    END IF;

    IF ROW(NEW.id, NEW.action, NEW.summary, NEW.payload_sha256, NEW.requested_by,
           NEW.required_role, NEW.created_at, NEW.expires_at, NEW.run_context, NEW.delegates)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.action, OLD.summary, OLD.payload_sha256, OLD.requested_by,
           OLD.required_role, OLD.created_at, OLD.expires_at, OLD.run_context, OLD.delegates) THEN
        RAISE EXCEPTION 'an approval request''s identity and payload never change';
    END IF;
    is_expired := OLD.expires_at::timestamptz <= db_now;

    IF OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected') THEN
        IF NOT as_approver THEN
            RAISE EXCEPTION 'only the approver role may decide an approval request';
        END IF;
        -- Rows written before the guard existed (0.1.0a2) may carry any lifetime;
        -- such a request can be neither decided nor used, only cancelled or expired.
        is_overlong := (OLD.created_at !~ timestamp_shape OR to_char((OLD.created_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> OLD.created_at) OR (OLD.expires_at !~ timestamp_shape OR to_char((OLD.expires_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> OLD.expires_at)
            OR OLD.expires_at::timestamptz > OLD.created_at::timestamptz + interval '7 days';
        IF is_expired OR is_overlong THEN
            RAISE EXCEPTION 'approval request % has expired or has no valid lifetime', OLD.id;
        END IF;
        IF NEW.decision IS DISTINCT FROM
               (CASE NEW.status WHEN 'approved' THEN 'approve' ELSE 'reject' END)
           OR NEW.resolved_by IS NULL OR NEW.resolved_by = OLD.requested_by
           OR NEW.resolved_by !~ principal_shape
           OR NEW.resolved_at IS NULL OR (NEW.resolved_at !~ timestamp_shape OR to_char((NEW.resolved_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> NEW.resolved_at)
           OR (NEW.reason IS NOT NULL AND length(NEW.reason) NOT BETWEEN 1 AND 500)
           OR NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
           OR NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
            RAISE EXCEPTION 'a decision sets decision, resolved_by and resolved_at only';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'pending' AND NEW.status IN ('cancelled', 'expired') THEN
        IF NEW.status = 'cancelled' AND NOT as_requester THEN
            RAISE EXCEPTION 'only the requester role may cancel an approval request';
        END IF;
        IF NEW.status = 'expired' AND NOT (as_requester OR as_approver) THEN
            RAISE EXCEPTION 'only the requester or approver role may expire a request';
        END IF;
        IF NEW.status = 'expired' AND NOT is_expired THEN
            RAISE EXCEPTION 'approval request % has not expired yet', OLD.id;
        END IF;
        IF NEW.closed_at IS NULL OR (NEW.closed_at !~ timestamp_shape OR to_char((NEW.closed_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> NEW.closed_at)
           OR ROW(NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.consumed_at)
              IS DISTINCT FROM
              ROW(OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.consumed_at) THEN
            RAISE EXCEPTION 'closing a request sets closed_at only';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'approved' AND NEW.status = 'consumed' THEN
        IF NOT as_requester THEN
            RAISE EXCEPTION 'only the requester role may consume an approval';
        END IF;
        -- Rows written before the guard existed (0.1.0a2) may carry any lifetime;
        -- such a request can be neither decided nor used, only cancelled or expired.
        is_overlong := (OLD.created_at !~ timestamp_shape OR to_char((OLD.created_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> OLD.created_at) OR (OLD.expires_at !~ timestamp_shape OR to_char((OLD.expires_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> OLD.expires_at)
            OR OLD.expires_at::timestamptz > OLD.created_at::timestamptz + interval '7 days';
        IF is_expired OR is_overlong THEN
            RAISE EXCEPTION 'approval request % has expired or has no valid lifetime', OLD.id;
        END IF;
        IF NEW.consumed_at IS NULL OR (NEW.consumed_at !~ timestamp_shape OR to_char((NEW.consumed_at)::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') <> NEW.consumed_at)
           OR ROW(NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.closed_at)
              IS DISTINCT FROM
              ROW(OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.closed_at) THEN
            RAISE EXCEPTION 'consuming an approval sets consumed_at only';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'approval request % cannot move from % to %', OLD.id, OLD.status, NEW.status;
END

        $_$;

CREATE FUNCTION public.agent_core_audit_append_at_end() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        DECLARE last_seq bigint;
        BEGIN
            EXECUTE format('SELECT COALESCE(MAX(seq), 0) FROM %I.%I',
                           TG_TABLE_SCHEMA, TG_TABLE_NAME) INTO last_seq;
            IF NEW.seq <> last_seq + 1 THEN
                RAISE EXCEPTION 'agent_core_audit is append-only';
            END IF;
            NEW.db_role := current_user;
            RETURN NEW;
        END $$;

CREATE FUNCTION public.agent_core_audit_refuse_change() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN RAISE EXCEPTION 'agent_core_audit is append-only'; END $$;

CREATE TABLE public.agent_core_approval_roles (
    singleton boolean DEFAULT true NOT NULL,
    requester_role text NOT NULL,
    approver_role text NOT NULL,
    CONSTRAINT agent_core_approval_roles_singleton_check CHECK (singleton)
);

CREATE TABLE public.agent_core_approvals (
    id text NOT NULL,
    action text NOT NULL,
    summary text NOT NULL,
    payload_sha256 text NOT NULL,
    requested_by text NOT NULL,
    required_role text NOT NULL,
    created_at text NOT NULL,
    expires_at text NOT NULL,
    status text NOT NULL,
    decision text,
    resolved_by text,
    resolved_at text,
    consumed_at text,
    reason text,
    run_context text,
    closed_at text,
    delegates text DEFAULT '[]'::text NOT NULL
);

CREATE TABLE public.agent_core_audit (
    seq bigint NOT NULL,
    schema_version integer NOT NULL,
    event_id text NOT NULL,
    occurred_at text NOT NULL,
    action text NOT NULL,
    actor_id text NOT NULL,
    subject_id text,
    payload text NOT NULL,
    run_context text,
    prev_hash text NOT NULL,
    record_hash text NOT NULL,
    db_role text
);

ALTER TABLE ONLY public.agent_core_approval_roles
    ADD CONSTRAINT agent_core_approval_roles_pkey PRIMARY KEY (singleton);

ALTER TABLE ONLY public.agent_core_approvals
    ADD CONSTRAINT agent_core_approvals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_core_audit
    ADD CONSTRAINT agent_core_audit_event_id_key UNIQUE (event_id);

ALTER TABLE ONLY public.agent_core_audit
    ADD CONSTRAINT agent_core_audit_pkey PRIMARY KEY (seq);

CREATE INDEX agent_core_approvals_pending ON public.agent_core_approvals USING btree (status, created_at, id);

CREATE TRIGGER agent_core_approvals_guard BEFORE INSERT OR DELETE OR UPDATE ON public.agent_core_approvals FOR EACH ROW EXECUTE FUNCTION public.agent_core_approvals_guard();

CREATE TRIGGER agent_core_approvals_no_truncate BEFORE TRUNCATE ON public.agent_core_approvals FOR EACH STATEMENT EXECUTE FUNCTION public.agent_core_approvals_guard();

CREATE TRIGGER agent_core_audit_append_at_end BEFORE INSERT ON public.agent_core_audit FOR EACH ROW EXECUTE FUNCTION public.agent_core_audit_append_at_end();

CREATE TRIGGER agent_core_audit_no_truncate BEFORE TRUNCATE ON public.agent_core_audit FOR EACH STATEMENT EXECUTE FUNCTION public.agent_core_audit_refuse_change();

CREATE TRIGGER agent_core_audit_no_update_delete BEFORE DELETE OR UPDATE ON public.agent_core_audit FOR EACH ROW EXECUTE FUNCTION public.agent_core_audit_refuse_change();

GRANT SELECT ON TABLE public.agent_core_approval_roles TO PUBLIC;
GRANT SELECT,INSERT ON TABLE public.agent_core_approvals TO agent_core_requester;
GRANT SELECT ON TABLE public.agent_core_approvals TO agent_core_approver;
GRANT UPDATE(status) ON TABLE public.agent_core_approvals TO agent_core_requester;
GRANT UPDATE(status) ON TABLE public.agent_core_approvals TO agent_core_approver;
GRANT UPDATE(decision) ON TABLE public.agent_core_approvals TO agent_core_approver;
GRANT UPDATE(resolved_by) ON TABLE public.agent_core_approvals TO agent_core_approver;
GRANT UPDATE(resolved_at) ON TABLE public.agent_core_approvals TO agent_core_approver;
GRANT UPDATE(consumed_at) ON TABLE public.agent_core_approvals TO agent_core_requester;
GRANT UPDATE(reason) ON TABLE public.agent_core_approvals TO agent_core_approver;
GRANT UPDATE(closed_at) ON TABLE public.agent_core_approvals TO agent_core_requester;
GRANT UPDATE(closed_at) ON TABLE public.agent_core_approvals TO agent_core_approver;
GRANT SELECT,INSERT ON TABLE public.agent_core_audit TO agent_core_requester;
GRANT SELECT,INSERT ON TABLE public.agent_core_audit TO agent_core_approver;

-- The rows below are replayed as the owner with the insert guards off, so they keep
-- the db_role and timestamps 0.1.0a3 recorded; the guards are back on afterwards.
ALTER TABLE public.agent_core_approvals DISABLE TRIGGER agent_core_approvals_guard;

ALTER TABLE public.agent_core_audit DISABLE TRIGGER agent_core_audit_append_at_end;

INSERT INTO public.agent_core_approval_roles VALUES (true, 'agent_core_requester', 'agent_core_approver');

INSERT INTO public.agent_core_approvals VALUES ('c2bfea0a-996d-4b4e-b97e-6eb4cae0241e', 'crm.update_contact', 'Update contact 17', '3c1d962265b3d6d8e2ff9eaa05efe30a8343d084aa7b2649c89ccb558b141922', 'agent-intake', 'ops.approver', '2026-10-03T05:22:30.414776Z', '2026-10-10T05:22:30.414776Z', 'pending', NULL, NULL, NULL, NULL, NULL, NULL, NULL, '[]');

INSERT INTO public.agent_core_audit VALUES (1, 3, 'acd77eb4-ac54-470c-8499-4f98fde678c4', '2026-10-03T05:22:30.390684Z', 'model.call', 'svc-triage', 'ticket-1', '{"n":1}', NULL, '0000000000000000000000000000000000000000000000000000000000000000', 'c22d98a36373dbe95cc97b0283e375ace056fb0b85d70a224e1fb0a4b48083fe', 'agent_core_requester');
INSERT INTO public.agent_core_audit VALUES (2, 3, '19a97461-fd21-4ba8-a8d3-bd7cde8db970', '2026-10-03T05:22:30.402687Z', 'model.call', 'svc-triage', 'ticket-2', '{"n":2}', NULL, 'c22d98a36373dbe95cc97b0283e375ace056fb0b85d70a224e1fb0a4b48083fe', '7162c05ed008a21703a8053621bbbbe443a9d6be31a46326a2268bb4aa5bb8b6', 'agent_core_requester');
INSERT INTO public.agent_core_audit VALUES (3, 3, '119e1a9f-b15c-4852-b03f-0ca184b65c0f', '2026-10-03T05:22:30.412415Z', 'model.call', 'svc-triage', 'ticket-3', '{"n":3}', NULL, '7162c05ed008a21703a8053621bbbbe443a9d6be31a46326a2268bb4aa5bb8b6', 'f5082e4a41af7de41238f0ef52bf8b1ef4f1ea249b52210527336f0f15405200', 'agent_core_requester');
INSERT INTO public.agent_core_audit VALUES (4, 3, '636c6711-929f-4559-80d1-cc81fb4ae58b', '2026-10-03T05:22:30.436609Z', 'approval.requested', 'agent-intake', 'c2bfea0a-996d-4b4e-b97e-6eb4cae0241e', '{"approval_action":"crm.update_contact","payload_sha256":"3c1d962265b3d6d8e2ff9eaa05efe30a8343d084aa7b2649c89ccb558b141922","required_role":"ops.approver"}', NULL, 'f5082e4a41af7de41238f0ef52bf8b1ef4f1ea249b52210527336f0f15405200', '06fa7d5bfe276023a844654fc0a9edb589b35460153c8a6ac40e0cfec72bc3f4', 'agent_core_requester');

ALTER TABLE public.agent_core_approvals ENABLE TRIGGER agent_core_approvals_guard;

ALTER TABLE public.agent_core_audit ENABLE TRIGGER agent_core_audit_append_at_end;
