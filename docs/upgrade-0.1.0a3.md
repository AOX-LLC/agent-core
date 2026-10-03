# Upgrading to 0.1.0a3

This guide is for the operator who sets up or upgrades a Postgres database for agent-core. Release 0.1.0a3 changes how the approvals table is protected.

## What changed and why

In 0.1.0a2, `install_postgres_schema(owner_url, app_role=...)` gave one app role SELECT, INSERT and UPDATE on the approvals table. "Only humans resolve" was a library check only. That role could set a request to approved with plain SQL, and no approval event would exist. 0.1.0a3 enforces approval transitions in the database, with two roles and a guard trigger. `app_role` is gone.

## The role layout

| Role      | Audit table      | Approvals table                                                         |
| --------- | ---------------- | ----------------------------------------------------------------------- |
| Requester | SELECT, INSERT   | SELECT, INSERT, UPDATE of `status`, `consumed_at`, `closed_at`          |
| Approver  | SELECT, INSERT   | SELECT, UPDATE of `status`, `decision`, `resolved_by`, `resolved_at`, `reason`, `closed_at` |
| PUBLIC    | none             | none (it may read `agent_core_approval_roles`; role names are no secret) |

- The requester role is the agent side. The approver role is the decision side.
- Nobody gets DELETE or TRUNCATE.
- On a non-public schema, both roles also get USAGE on the schema.
- The guard trigger (`agent_core_approvals_guard`, plus a truncate trigger) checks every insert and update. It uses role membership of `current_user` and `session_user`, so `SET ROLE` cannot cross sides. It uses the database's clock. The role names are written into the guard function at install.

## The transitions

| From     | To        | Role                | Also required                                                                                         |
| -------- | --------- | ------------------- | ----------------------------------------------------------------------------------------------------- |
| (insert) | pending   | requester           | undecided; `created_at` no later than 5 minutes past database time; lifetime at most 7 days            |
| pending  | approved  | approver            | `decision`, `resolved_by` (not the requester) and `resolved_at` set; not expired                       |
| pending  | rejected  | approver            | as approved                                                                                           |
| pending  | cancelled | requester           | `closed_at` set                                                                                       |
| pending  | expired   | requester or approver | `closed_at` set; `expires_at` already past                                                          |
| approved | consumed  | requester           | `consumed_at` set; not expired                                                                        |

- Everything else is refused, including DELETE and TRUNCATE.
- No update may change `id`, `action`, `summary`, `payload_sha256`, `requested_by`, `required_role`, `created_at`, `expires_at`, `run_context` or `delegates`.

## Setting up a new database

Needs Postgres 14 or later. On Postgres 14, and on a cluster upgraded from it, PUBLIC may create objects in the `public` schema; revoke that first (`REVOKE CREATE ON SCHEMA public FROM PUBLIC;`), or the installer and every queue refuse to run. A requester able to create objects where approver-side code looks could plant a function that runs with the approver's rights. The library itself pins `search_path` to `pg_catalog, pg_temp` on its connections and qualifies every table, so it never resolves a name through a schema another role can write.

Create the roles first, as a role that may create roles. They must be NOSUPERUSER and not members of each other. The owner role owns the tables and is never used at run time. It must be able to create tables in the target schema.

```sql
CREATE ROLE agent_core_owner LOGIN PASSWORD '...' NOSUPERUSER;
CREATE ROLE agent_core_requester LOGIN PASSWORD '...' NOSUPERUSER;
CREATE ROLE agent_core_approver LOGIN PASSWORD '...' NOSUPERUSER;
```

Then run the installer as the owner:

```python
from aox_agent_core.storage import install_postgres_schema

report = install_postgres_schema(
    "postgresql://agent_core_owner@db.example/agent_core",
    requester_role="agent_core_requester",
    approver_role="agent_core_approver",
    schema="public",
)
print(report)
```

The installer is idempotent: a second run with the same roles changes nothing. It runs in one transaction under an advisory lock. It grants a role its layout only on a table where that role holds no privilege yet, and it never revokes. A tightening you made by hand is kept. A role with every privilege revoked counts as fresh and is granted its layout again.

It refuses:

- role names other than those a first run recorded (in `agent_core_approval_roles`);
- overlapping roles, a superuser role, or a run as the requester or approver;
- tables created by 0.1.0a1.

### Reading the report

`install_postgres_schema` returns an `InstallReport` with `schema`, `requester_role`, `approver_role` and `outside_layout`. `outside_layout` is a tuple of `Grant(table, role, privilege, column)`: every grant on the library's tables that is outside the layout. `str(report)` prints them. On a new database it should be empty. Revoke what it lists once nothing uses it.

### Two connections

A deployment runs two queues. The agent side connects as the requester role. The decision side connects as the approver role.

```python
from aox_agent_core.approvals import SQLApprovalQueue
from aox_agent_core.audit import SQLAuditLog
from aox_agent_core.storage import open_database

agent_db = open_database("postgresql://agent_core_requester@db.example/agent_core")
agent_audit = SQLAuditLog(agent_db)
agent_queue = SQLApprovalQueue(agent_db, audit_log=agent_audit)

decision_db = open_database("postgresql://agent_core_approver@db.example/agent_core")
decision_audit = SQLAuditLog(decision_db)
decision_queue = SQLApprovalQueue(decision_db, audit_log=decision_audit)
```

- Requester side: `submit`, `consume`, `cancel`, `expire_due`, `get`.
- Approver side: `list_pending`, `resolve`, `expire_due`, `get`.
- `await queue.side()` returns `ApprovalSide.REQUESTER` or `ApprovalSide.APPROVER`. It returns `BOTH` on SQLite.
- A call from the wrong side raises `ConfigError` in the library, before the database refuses it.

Before its first statement, a Postgres queue checks the setup and raises `ConfigError`, writing nothing, if:

- the schema predates 0.1.0a3 or lost a guard trigger;
- the roles overlap;
- the requester role can update `decision`, `resolved_by`, `resolved_at` or `reason`;
- the approver role can insert;
- the connecting role, or any role it can switch to, is a superuser, the owner, or able to delete or truncate;
- the connecting role is a member of both roles or of neither;
- the requester role can create objects in the install schema or in `public`.

## Upgrading from 0.1.0a2

Until you upgrade, an a3 queue refuses with a `ConfigError` that tells you to run the a3 installer.

1. Create the requester and approver roles, as above. Use new roles. If the a2 app role became the requester, it would keep its table-wide UPDATE, and the connect check refuses that.
2. Run `install_postgres_schema(owner_url, requester_role=..., approver_role=..., schema=...)` as the owner. It adds the `closed_at`, `delegates` and `db_role` columns, the role table, the guard triggers and the grants. It keeps every row. It changes no existing grant.
3. Read the report. The a2 app role's grants appear in `outside_layout`.
4. Point the agent side at the requester role and the decision side at the approver role.
5. Once nothing uses the old app role, revoke it. The guard already refuses its approval writes, since it is in neither role.

```sql
REVOKE INSERT, UPDATE ON agent_core_approvals FROM <a2 app role>;
REVOKE INSERT ON agent_core_audit FROM <a2 app role>;
```

A SQLite file from a2 gains the new columns in place on first use.

### Check what a2 let through

Under a2, a request approved by plain SQL has no matching `approval.resolved` audit event. This query lists such requests:

```sql
SELECT a.id, a.status, a.resolved_by
FROM agent_core_approvals a
WHERE a.status IN ('approved', 'consumed')
  AND NOT EXISTS (
    SELECT 1 FROM agent_core_audit e
    WHERE e.action = 'approval.resolved'
      AND e.subject_id = a.id
      AND e.payload::jsonb ->> 'decision' = 'approve'
  );
```

An empty result rules out only a plain flip. Under a2 the same app role could also append a matching, correctly chained audit event, and a2 records carry no `db_role`. Run `aox-agent-core audit verify` against a saved anchor first. For a non-public schema, add `--schema NAME`.

## SQLite is not a trust boundary

SQLite has no roles. Anyone who can write the file is fully trusted, and the library's checks are all SQLite has. A SQLite queue is `ApprovalSide.BOTH`. Use SQLite for development and single-user setups, not to keep an agent from approving its own requests.

## What the database enforces and what the library enforces

The database enforces:

- who may insert, decide, cancel, expire and consume, by role;
- the transition table above, on the database's clock;
- that no request is deleted, and that identity, payload hash, requester, required role, lifetime, run context and delegates never change;
- that `resolved_by` is not the requester;
- that the audit log is append-only, and `AuditRecord.db_role` (audit schema 3) is the role that inserted the row, set by an insert trigger whatever was sent.

The library enforces:

- that the approver is a human who holds `required_role` (the database cannot know principals, because every agent shares the requester role);
- with `RoleApproverPolicy(roles_by_action={...})` on the approver side, which role each action needs. The requester chooses `required_role` when it submits, so without that map a requester can ask for a weaker role than an action deserves;
- that `consume` is called by the requester or one of the request's delegates, else `NotTheRequesterError`;
- that `cancel` is called by the requester, never a delegate;
- the action and payload match on `consume`.

Both roles may append audit rows, and `actor_id` is library-supplied. To tell who really wrote a record, such as an `approval.resolved` event, read `db_role`.

Delegation is explicit per request: `submit(..., delegates={"svc-crm-writer"})`, at most 16 principal IDs. The guard keeps them unchanged. They are recorded in the `approval.requested` audit event and shown on the request the approver sees.
