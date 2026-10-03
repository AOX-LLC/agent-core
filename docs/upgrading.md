# Upgrading from 0.1.0a3 to 0.1.0a4

Release 0.1.0a4 makes storage async, adds `append_many`, lets a host run the library's writes inside its own Postgres transaction, and lets a request store its exact payload. It changes the `AuditLog` and `ApprovalQueue` protocols, the `Session` and `Database` types, and the Postgres schema.

This page has three parts: what a project that implements its own backends must change (project 03), what a project that uses the SQL backends with its own wrappers must change (project 04), and what the operator does to the database. Read the operator section first: the library refuses an a3 schema.

Every signature below was checked against the source of this release. Nothing here was run against a database while this page was written; see "What is not verified" at the end.

## Project 03: its own backends

Project 03 implements `ApprovalQueue` and `AuditLog` itself and uses agent-core's hash function. The checklist:

1. Add `append_many` to the audit log. It is part of the `AuditLog` protocol now.

   ```python
   async def append_many(self, events: Sequence[AuditEvent]) -> list[AuditRecord]: ...
   ```

   All or nothing: validate and scan every event first, take the chain lock once, write consecutive records, commit once, and return the records in order. An empty sequence writes nothing. `SQLAuditLog` names the index of an event whose payload it refuses (`"Event 3: ..."`); do the same. It also caps a batch at `MAX_APPEND_BATCH = 1000` and raises `ValueError` above it; the protocol sets no cap.

2. Honor `AuditEvent.occurred_at`. It is new, `AwareDatetime | None`, default `None`.

   ```python
   # before: the record's occurred_at was always your clock
   occurred_at = datetime.now(UTC)

   # after
   occurred_at = event.occurred_at if event.occurred_at is not None else database_now
   ```

   A supplied value must lie within `OCCURRED_AT_MAX_PAST` (24 hours) before and `OCCURRED_AT_MAX_FUTURE` (5 minutes) after the store's clock, or `append` raises `AuditTimeRejectedError`. Both constants are in `aox_agent_core.audit`. `occurred_at` is in the hash, as before.

3. Set `AuditRecord.recorded_at` when you build the record. It is `AwareDatetime | None`: when your store wrote the row, outside the hash. Leave it `None` if you cannot give it. Records from before a4 have `None`.

4. Leave the hash alone. `compute_record_hash` is unchanged, `AUDIT_SCHEMA_VERSION` is still 3, and records written under a3 verify as they did. `recorded_at` is not an input to the hash.

5. Accept `include_payload` on `submit`.

   ```python
   # before
   async def submit(
       self,
       *,
       action,
       summary,
       payload,
       requested_by,
       required_role,
       ttl_seconds,
       delegates=(),
       context=None,
   ) -> ApprovalRequest: ...


   # after: one more keyword
   async def submit(
       self,
       *,
       action,
       summary,
       payload,
       requested_by,
       required_role,
       ttl_seconds,
       delegates=(),
       context=None,
       include_payload=False,
   ) -> ApprovalRequest: ...
   ```

   With `include_payload=True` the protocol's contract is: store the exact payload, at most 8192 bytes of canonical JSON, under the audit log's rules (no secret-shaped keys, integers only, a secret scan); set `ApprovalRequest.payload`; and check a stored payload against `payload_sha256` on every read (`approval_payload_hash(action, payload)` in `aox_agent_core.approvals` computes it). `SQLApprovalQueue` raises `ApprovalPayloadRejectedError` for a payload it will not store and writes nothing. `ApprovalRequest.payload` is new and defaults to `None`.

6. Handle the new errors if your backend raises or catches them: `AuditTimeRejectedError` (an `AuditError`), and `ApprovalIntegrityError` and `ApprovalPayloadRejectedError` (both `ApprovalError`).

7. If your backend stores a payload, mirror what `SQLApprovalQueue` does on a mismatch. `get` raises `ApprovalIntegrityError`. `list_pending` leaves the request out. `resolve` refuses, audits `approval.resolve_denied` with reason `payload_integrity`, and the request stays pending. `cancel`, `consume` and `expire_due` do not check and return `payload=None`, so a requester can always withdraw. Stored text must also pass the rules a submit applies when it is read, and a malformed one hides only its own request.

8. If you call agent-core's installer, the signature has not changed. It needs Postgres 16 now. See the operator section.

If project 03's backends do not use agent-core's `Database` or `Session`, none of the storage changes in the next section apply to them.

## Project 04: the SQL backends and its own wrappers

Project 04 uses `SQLAuditLog` and `SQLApprovalQueue` on Postgres 17, with wrappers of its own. The checklist:

1. Install the new extra. `postgres` is now `psycopg[binary,pool]`. If you pin psycopg yourself, add `psycopg_pool`.

2. Postgres must be 16 or later. 17 passes the check and is tested (see the end of this page).

3. Close what you open. A Postgres `Database` owns a `psycopg_pool.AsyncConnectionPool` that opens on first use, on the running event loop.

   ```python
   # before
   database = open_database(url)

   # after
   async with open_database(url, max_connections=10) as database:
       ...
   # or: await database.aclose()
   ```

   Use one `Database` per event loop. A second loop gets a `ConfigError`. `max_connections` defaults to 10.

4. To share a pool, hand it over. `PostgresDatabase.from_pool(pool)` borrows a host pool, and `aclose()` never closes it.

   ```python
   from aox_agent_core.storage import PostgresDatabase

   database = PostgresDatabase.from_pool(pool)
   ```

5. Make every `Database.run` callback async. `Session.execute` and `Session.execute_count` are coroutines, and `run` awaits `work`. `Database.run_sync` is gone.

   ```python
   # before
   def work(session):
       rows = session.execute("SELECT 1")
       return rows


   result = await database.run(work)


   # after
   async def work(session):
       rows = await session.execute("SELECT 1")
       return rows


   result = await database.run(work)
   ```

   A plain function now fails when `run` awaits it. `SQLAuditLog.append_in` is a coroutine too: `await audit_log.append_in(session, event)`. `audit_table_exists` is a coroutine, so `await` it.

6. Move blocking callers to the facades or to your own loop. `aox_agent_core.sync` has `SyncAuditLog` and `SyncApprovalQueue`.

   ```python
   from aox_agent_core.sync import SyncAuditLog

   with SyncAuditLog(SQLAuditLog(open_database(url))) as log:
       record = log.append(event)
   ```

   Each facade runs its calls on one background thread with its own loop, and `close()` (or leaving the `with`) closes the database. A facade raises `EventLoopRunningError` when called from inside a running event loop; await the async object there. A facade takes no `connection=`. `install_postgres_schema` and the CLI stay blocking.

7. To write your own rows and the library's records in one transaction, pass your connection. `connection=` is accepted by `SQLAuditLog.append` and `append_many`, and by `SQLApprovalQueue.submit`, `get`, `list_pending`, `resolve`, `consume`, `cancel`, `expire_due` and `side`. It takes a psycopg `AsyncConnection` only, and Postgres only.

   ```python
   async with pool.connection() as connection, connection.transaction():
       await connection.execute("UPDATE orders SET state = 'held' WHERE id = %s", (order_id,))
       request = await queue.submit(
           action="orders.release",
           summary="Release a held order",
           payload={"order_id": order_id},
           requested_by=agent,
           required_role="ops.approver",
           ttl_seconds=3600,
           connection=connection,
       )
   ```

   The rules:

   - The connection must already be in a transaction, or a `ConfigError` is raised. Open one with `connection.transaction()`.
   - The transaction must be READ COMMITTED, or a `ConfigError` is raised.
   - agent-core works in a savepoint. It never commits, rolls back or closes your transaction. It pins `search_path` for the savepoint and restores it afterwards.
   - Your commit or rollback decides everything the library wrote.

8. Know the hazards of `connection=` before you use it.

   - **The audit lock lasts as long as your transaction.** An audit write takes the chain's advisory lock, and Postgres releases it only when your transaction ends. Write audit events late in the transaction, and keep it short.
   - **Records are provisional until you commit.** Do not anchor a `head()` taken inside the transaction.
   - **A refusal is written apart.** A denied `consume`, `resolve` or `cancel` changed nothing, and you will most likely roll back when the error reaches you, which would erase its audit event. So the event is written on a separate pooled connection of the audit log's own pool and committed at once, waiting at most 2 seconds for a pooled connection (`DENIAL_ACQUIRE_TIMEOUT`) and 2 seconds for the append lock (`DENIAL_LOCK_TIMEOUT`). If the pool, the lock or the database fails, a warning is logged and the event is written in your transaction instead, and it is lost if you roll back.
   - **A host pool of size 1 delays that write.** If your pool has one connection and you hold it, the separate write waits 2 seconds, logs a warning and falls back to your transaction.
   - **The log must share the queue's database.** With `connection=`, a queue whose audit log is not a `SQLAuditLog` on the same `Database` object, or one opened from the same connection settings, raises `ConfigError`: events appended through another log would commit before your transaction does. A `from_pool` Database and a Database opened from a URL count as different, even on one server.
   - **Worst case: a refusal can be lost, and the only trace is the log.** Any role that can connect can hold the append lock (see below), so every refusal waits out its 2 seconds, falls back into your transaction, and is lost if you roll back. The log line names the action, subject, actor and reason. If the fallback fails too, the refusal is still raised, with a note on the exception saying it was not audited.
   - **Other limits.** A separate refusal record carries the audit pool's `db_role`, not your connection's. If the separate commit fails in an ambiguous way, the fallback can leave a second copy of the refusal. Any role that can connect can hold the append lock with an idle transaction and stall every writer: set `idle_in_transaction_session_timeout` on the runtime roles. The lock is one key for the whole database, so all schemas share it. Both are planned to change in 0.1.0a5: a `lock_timeout` on the library's own append transactions, and a lock key that includes the schema.

9. Use `append_many` for batches. One lock and one commit for up to 1000 events; all or nothing; an error names the event's index.

   ```python
   records = await audit_log.append_many(events)
   ```

10. Read the timestamps differently. `AuditEvent.occurred_at=None` now means the database's clock on Postgres. Before a4 it was always the application's clock. On SQLite it is still the writer's clock. A supplied `occurred_at` must be within 24 hours before and 5 minutes after the database clock, or `AuditTimeRejectedError` is raised, and the Postgres trigger checks it again. `AuditRecord.recorded_at` is set by the trigger, whatever the writer sends.

11. Show the payload to approvers. Pass `include_payload=True` to `submit` when the approver must see what the hash binds.

    ```python
    request = await queue.submit(..., include_payload=True)
    request.payload  # the exact payload, or None if it was not stored
    ```

    `summary` is written by the requester and is not covered by `payload_sha256`. A UI must show `payload` when it is present, and must not decide from `summary` alone. Handle `ApprovalIntegrityError` from `get`, and expect `list_pending` to omit a request whose stored payload does not match its hash. The payload is never copied into the audit log. Nothing purges a stored payload: count it in your retention plan.

12. Check that nothing in your wrappers depends on a connection per call. Every statement now runs with `prepare=False`, and isolation and `search_path` are set per transaction, never per session (`SET TRANSACTION`, `SET LOCAL`). There is no `LISTEN`, and advisory locks are transaction-level. That is what a transaction-mode pooler needs. It was not tested against one (see the end of this page).

## Operators: the Postgres schema

Re-run the installer as the owner role, with the same roles as before:

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

It is idempotent. Run on an a3 schema it:

- adds the `recorded_at` column to the audit table;
- replaces the audit insert trigger function. The new one takes the same advisory lock the library takes, requires `seq` to be the head's plus one and `prev_hash` to equal the head's `record_hash` (the genesis zeros when the log is empty), checks the schema version (3) and the shape of `event_id`, `record_hash` and `occurred_at`, checks the `occurred_at` bounds, bounds `action`, `actor_id`, `subject_id`, `payload` (8192 bytes, a JSON object) and `run_context` (2048 bytes, a JSON object), and sets `db_role` and `recorded_at`. It cannot check `record_hash` itself; `verify()` recomputes it;
- adds the `payload_json` column to the approvals table;
- rewrites the approvals guard, which now measures a request's lifetime in 168 hours regardless of the writer's time zone, bounds `run_context` (2048 bytes, 16 external ids) and `delegates` (4096 bytes), and covers `payload_json`: it is immutable after insert, and a value that is not a JSON object, or is over 8192 bytes, is refused. The requester and approver roles have no UPDATE on it.

It keeps every row and does not change a grant that is already there. The a3 notes on `outside_layout` and `unaudited_approvals` still apply to the report.

Other changes for the operator:

- **Postgres 16 or later.** An older server is refused with a `ConfigError` at connect and at install. The message reads "This Postgres server is version 15; agent-core 0.1.0a4 needs 16 or later." (with the server's version).
- **Until you re-run the installer, an a3 schema is refused.** The audit log raises a `ConfigError` before it writes anything, and the approvals queue does the same when its table lacks `payload_json`. The audit log's message:

  > Table agent_core_audit was created by an earlier agent-core and has no recorded_at column. As the owner role, run install_postgres_schema from 0.1.0a4 with the requester and approver roles: it upgrades the schema in place and keeps every row.

  Run the installer before starting a4 code against the database.
- **SQLite needs nothing.** An a3 file gets the `recorded_at` and `payload_json` columns, and the append trigger that also checks `prev_hash`, in place on first use.
- **`recorded_at` is the trigger's word, not the chain's.** Like `db_role`, it is outside the hash and guaranteed by the database trigger. The table owner could edit it undetected. On SQLite the library writes it from the writer's clock, so it is not independent there.
- **Old records still verify.** Run `aox-agent-core audit verify` against a saved anchor with the a3 release before the upgrade (a4 cannot read an a3 schema until the installer has run), and with a4 after. A row inserted with a wrong `record_hash` makes `verify` fail from that record on; the message shows the row's `db_role` column, which the hash does not cover. Rows written before a4 have no `recorded_at`.

## What is not verified

- **Postgres 17 in CI.** The Postgres test files passed against `postgres:17-alpine` on a developer machine. CI runs 16 on Python 3.11 to 3.14 and 17 on Python 3.12 in a separate job. Postgres 18 and later were not tested.
- **A real pooler.** The design fits a transaction-mode pooler (see item 12 for project 04). It has not been run against pgbouncer or any other pooler.
- **This upgrade on a large or busy database.** `tests/test_upgrade_from_a3.py` replays a schema dump made by 0.1.0a3 with four audit rows and one approval, upgrades it, and checks the rows, the chain and a second installer run. It has not been run on a production-sized database, or with writers active.
- **Mixed versions.** An a3 library writing to an a4 schema has not been tested. Plan a short window with no writers: stop the a3 processes, run the installer, start a4.
