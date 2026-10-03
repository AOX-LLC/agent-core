-- The Postgres schema install_postgres_schema created in agent-core 0.1.0a2 (37944be),
-- for the a2 -> a3 upgrade tests. APP_ROLE is replaced with the test's legacy role.

CREATE TABLE agent_core_audit (
    seq BIGINT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    subject_id TEXT,
    payload TEXT NOT NULL,
    run_context TEXT,
    prev_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL
);

CREATE FUNCTION agent_core_audit_refuse_change() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp AS $$
    BEGIN RAISE EXCEPTION 'agent_core_audit is append-only'; END $$;

CREATE TRIGGER agent_core_audit_no_update_delete BEFORE UPDATE OR DELETE ON agent_core_audit
    FOR EACH ROW EXECUTE FUNCTION agent_core_audit_refuse_change();

CREATE TRIGGER agent_core_audit_no_truncate BEFORE TRUNCATE ON agent_core_audit
    FOR EACH STATEMENT EXECUTE FUNCTION agent_core_audit_refuse_change();

CREATE FUNCTION agent_core_audit_append_at_end() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp AS $$
    DECLARE last_seq bigint;
    BEGIN
        EXECUTE format('SELECT COALESCE(MAX(seq), 0) FROM %I.%I', TG_TABLE_SCHEMA, TG_TABLE_NAME)
            INTO last_seq;
        IF NEW.seq <> last_seq + 1 THEN
            RAISE EXCEPTION 'agent_core_audit is append-only';
        END IF;
        RETURN NEW;
    END $$;

CREATE TRIGGER agent_core_audit_append_at_end BEFORE INSERT ON agent_core_audit
    FOR EACH ROW EXECUTE FUNCTION agent_core_audit_append_at_end();

REVOKE ALL ON agent_core_audit FROM PUBLIC;

GRANT SELECT, INSERT ON agent_core_audit TO "APP_ROLE";

CREATE TABLE agent_core_approvals (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    required_role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    consumed_at TEXT,
    reason TEXT,
    run_context TEXT
);

CREATE INDEX agent_core_approvals_pending ON agent_core_approvals (status, created_at, id);

REVOKE ALL ON agent_core_approvals FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON agent_core_approvals TO "APP_ROLE";

