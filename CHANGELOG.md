# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Pre-releases are spelled the PEP 440 way, so tags look like `v0.1.0a1`.

## [Unreleased]

## [0.1.0a6] - 2026-10-03

Fix release for 0.1.0a5. **0.1.0a5 was tagged but never released** (see below): upgrade from 0.1.0a4 straight to 0.1.0a6. Details are added below as each fix lands.

### Changed (breaking)

- The guard writes `payload_purged_at` itself, from the database's clock, whatever the purge statement carried (it still requires a canonical timestamp there), as it does for `closed_at` and `consumed_at`. In 0.1.0a5 the guard required the supplied value to be within 5 minutes of its clock, and `purge_payloads` took one reading of the application clock at the start of the run, so a run that went on for more than 5 minutes (a large backlog, `limit` batches) was refused partway. `purge_payloads` now reads the application clock per batch (which only SQLite stores).
- A rejection's `resolved_at` is its finish time, which the retention floor counts from, so the guard writes it too: the database's clock, never before the request's `created_at`. Before, the approver role chose it, within 5 minutes of the database clock, which was enough to slip under a short installed floor. An approval's `resolved_at` is not a finish time and is unchanged. `resolve` returns the stored value.
- The approvals guard is revision 6 (`-- agent-core guard revision 6`). A connection refuses revision 5, so run `install_postgres_schema` from 0.1.0a6 as the owner role before the first 0.1.0a6 process connects (see docs/upgrading.md).

### Fixed

- The purge index now matches the library's purge query on Postgres. `purge_payloads` also requires the finish time to be a canonical timestamp, which the 0.1.0a5 index predicate did not carry, so the planner could not use the index for the ordered scan: it read every due row and sorted them, for each batch, under a backlog (300,000 due rows in a probe: 3.8 s per batch, 0.26 ms with the index). The index is `agent_core_approvals_purge_due`, with the condition in its predicate; the installer drops the 0.1.0a5 index `agent_core_approvals_purgeable` and builds the new one (SQLite does the same on first use). A request whose finish time is not canonical is still never purged.

### Changed

- SQLite 3.35 (March 2021) is the declared minimum, up from the 3.15 the code checked. The approval queue reads back what the store wrote with `UPDATE ... RETURNING` (closed, and since this release a decision's `resolved_at`), which SQLite added in 3.35. `open_database` for a `sqlite:` URL raises a `ConfigError` that names the version your Python is linked against and the minimum, and says to use a Python build with a newer SQLite or Postgres, instead of failing with a syntax error on the first close. Postgres is unaffected. `sqlite3.sqlite_version` shows what you have.
- `ApprovalConflictError` keeps its notes (`add_note`) as well as `existing` and `differs` when it is pickled or copied.
- The `approval.submit_conflict` audit event records `approval_action` in its payload, as the other `approval.*` events do.
- A `submit` that meets an open request stored with an id that is not a UUID no longer crashes with a `ValueError`. It raises `ApprovalIntegrityError` naming the id (cut to 40 characters), and audits `approval.submit_conflict` with no `subject_id`.
- The refusal for an open request the library cannot parse (`malformed_row`) no longer says "Cancel it, then submit again": `cancel` cannot read such a row, so that advice failed. It says the table owner must close it, and docs/upgrading.md gives the procedure ("A stored request the library cannot read"). A request whose stored payload fails its hash can be cancelled, and still says so. The row keeps its key until the owner acts: the library cannot close what it cannot read.
- `expire_due` and `purge_payloads` skip a request another transaction holds (`FOR UPDATE SKIP LOCKED`, taken by the UPDATE after the audit append lock, so the lock order is unchanged) instead of failing the whole sweep with `LockNotAvailable` after `lock_timeout`. The request is picked up by the next sweep. SQLite is unchanged.
- `install_postgres_schema` reports `InstallReport.backdated_finishes`: finished requests that still hold a payload and whose finish time is before their own creation or decision (at most 1000, by id). 0.1.0a4 let the closing role write `closed_at` and `consumed_at`, so such a row is purgeable at once under any retention floor; the guard has written those times itself since 0.1.0a5. The installer changes nothing about them; check them before the first purge. A backdated time that still falls after the request's creation and decision cannot be told from a real one and is not listed.

## [0.1.0a5] - 2026-10-03

**Tagged but not released; use 0.1.0a6.** The `v0.1.0a5` tag exists, but the release workflow refused it because this section was filed under Unreleased, so there is no GitHub release and no published wheel. The tag stays where it is. Everything in this section ships in 0.1.0a6.


Approvals completion: a repeated submit returns the open request instead of a second one, approvals that lapse unused expire, stored payloads can be purged, an approver may be neither the requester nor a delegate, free text with control characters is refused, and the audit append lock is bounded and keyed per schema. One operator reinstall: run `install_postgres_schema` as the owner role again (see docs/upgrading.md).

### Changed (breaking)

- Submit is idempotent. A partial unique index, `agent_core_approvals_one_open`, on approvals `(requested_by, action, payload_sha256)` where `status IN ('pending', 'approved')`, the same index on Postgres and SQLite, allows at most one open request (pending, or approved and not yet consumed) per requester, action and payload hash, so it holds when calls race. `submit` keeps its signature. An exact repeat (same `required_role`, same lifetime, meaning `expires_at - created_at == ttl_seconds`, and same `delegates`) returns the existing open request, with its stored payload if one was stored, and writes no audit event. Tests that submit the same payload twice from one requester now get the first request back.
- A repeat that asks for the payload to be stored (`include_payload=True`) must find that same payload stored on the open request: otherwise it differs in `payload`. Asking for less than is stored is fine. A repeat that differs in `summary`, `required_role`, lifetime or `delegates`, or in `payload`, raises the new `ApprovalConflictError` (an `ApprovalError`) with `existing: UUID` and `differs: tuple[str, ...]`, a sorted subset of `("delegates", "lifetime", "payload", "required_role", "summary")`, or `("row",)` or `("payload",)` for an open request that cannot be read or whose stored payload fails its hash. It is audited as `approval.submit_conflict`: actor is the requester, subject is the existing request's id, and the payload has `differs` as a comma-joined string. `context` is not compared; the first submit's stays.
- An open request already past its lifetime (by the database clock on Postgres, the application clock on SQLite) is closed as expired in the same transaction, audited as `approval.expired` (with `previous_status="approved"` if it was an approval), and the new request is queued.
- `approved -> expired` is a legal transition. The guard allows it for either role once `expires_at` is past by the database clock, and sets `closed_at` only; an approval that lapses unused would otherwise hold the unique key forever. `ApprovalRequest` allows status `EXPIRED` with decision `approve`, which keeps `decision`, `resolved_by` and `resolved_at`. `get()` reports a pending or approved request past its lifetime as `EXPIRED`, and `expire_due()` also sweeps approved, unconsumed requests. In 0.1.0a4 an approved request stayed approved; a host that tests `status is APPROVED` to mean "usable" must also check `is_expired`, or call `consume()`.
- The guard writes `closed_at` and `consumed_at` itself, from the database's clock, whatever the statement carried, when a request is closed, expired or consumed. A client could otherwise backdate its own finish time and get its payload purged under the retention floor, or future-date it and keep it for ever.
- The approver may be neither the requester nor one of the request's delegates (its own commit, see Added). `RoleApproverPolicy` denies with `DenialReason.DELEGATE_APPROVAL`.
- The guard requires an approver's `resolved_at` to be at or after the request's `created_at` and within 5 minutes of the database clock. The library's own value is the application's clock, `max(now, created_at)`, so an application clock more than 5 minutes off the database's is now refused at `resolve`. Before, any canonical value was accepted.
- `summary`, `reason` and a cancel reason may not contain control (Cc), format (Cf, which includes bidirectional overrides and zero-width characters) or line and paragraph separator (Zl, Zp) characters: `ValidationError` for `summary` at submit, `ValueError` for `reason` at `resolve` and `cancel`. The Postgres guard refuses the listed subset on insert (`summary`) and on a decision (`reason`).
- `check_payload`, which covers audit payloads and stored approval payloads, refuses NUL in any key or string (`ValueError`, "payload text must not contain NUL"), which Postgres `jsonb` cannot store. `AuditEvent` construction fails with `ValidationError` for it.
- `SQLAuditLog(..., lock_timeout=timedelta(seconds=5))`: the library's own append transactions wait for the append lock at most that long, then raise the new `AuditLockTimeoutError` (an `AuditError`, sqlstate `55P03`) and write nothing. Inside a host's transaction (`connection=`) the bound is applied for the append and the host's own `lock_timeout` is put back.
- The advisory lock key is per schema: `hashtextextended('agent_core_audit:' || schema, 0)`, computed in SQL by the library and by the insert trigger alike. The constants `audit.sql.APPEND_LOCK_KEY` and `_postgres_schema.AUDIT_APPEND_LOCK_KEY` are removed.
- The `ApprovalQueue` protocol gains `purge_payloads` (see Added), and documents the idempotent-submit rule: a host-supplied queue must implement it. `SyncApprovalQueue` gains `purge_payloads` too.
- The Postgres schema changes: the new column `approvals.payload_purged_at`; the unique index; a replaced approvals guard (`approved -> expired`, the purge rule, the delegate rule, the `resolved_at` bound, the text rule, `-- agent-core guard revision 5`, and the floor comment); a replaced audit insert trigger (schema-keyed lock, `-- agent-core audit trigger revision 5`); and a column grant for the approver role on `payload_json` and `payload_purged_at`. A connection refuses an older trigger or guard with a `ConfigError` that tells the operator to run `install_postgres_schema` again (from 0.1.0a6, which replaces the guard and trigger written by an 0.1.0a4 or a 0.1.0a5 install), before anything is written. SQLite files are upgraded in place on first use.

### Added

- `purge_payloads(*, principal, older_than, limit=500, connection=None) -> int` on `SQLApprovalQueue`, the `ApprovalQueue` protocol and `SyncApprovalQueue`. It sets `payload_json` to NULL and stamps the new column `payload_purged_at` on requests that are consumed, rejected, cancelled or expired, have a stored payload, and whose finish time is older than `older_than` by the database's clock (the application clock on SQLite). The finish time is `consumed_at` for consumed, `resolved_at` for rejected, and `closed_at` for cancelled and expired. It works in batches of `limit`, like `expire_due`, and writes one `approval.payload_purged` audit event per purged request (payload: `approval_action`, `payload_sha256`, `request_status`) in the same transaction. `payload_sha256` is never changed. Nothing else purges a payload.
- Only the approver role may purge: `ConfigError` on a requester-side queue. The requester role has no UPDATE on `payload_json` or `payload_purged_at`, and the connect check refuses a requester role that has it. The guard allows exactly one change to those columns: `payload_json` from non-NULL to NULL together with `payload_purged_at` from NULL to a canonical timestamp within 5 minutes of the database clock, by the approver role, on a finished request whose finish time plus the installed floor is already past. Every other change is refused: restoring a payload, clearing the mark, marking without purging, marking a request that never stored a payload, and a purge that changes anything else.
- `install_postgres_schema(..., payload_retention_floor=timedelta(hours=24))`: the shortest `older_than` the database accepts, written into the guard as `-- agent-core payload retention floor <n> seconds`. The library raises `ValueError` for an `older_than` shorter than the floor. SQLite has no floor.
- `install_postgres_schema(..., close_duplicates=False)` and `InstallReport.closed_duplicates`. A database that already holds duplicate open rows (0.1.0a4 allowed them) is refused with a `ConfigError` that lists them, and nothing is changed. With `close_duplicates=True` the installer keeps each group's approved request (else its oldest) and cancels the other pending ones, without an audit event, and lists them in `closed_duplicates`. A group with two approved requests is still refused: a human decides. On SQLite the library builds the index itself on first submit and refuses with a `ConfigError` listing the ids while duplicates exist; `cancel()` and reads still work so the extras can be cancelled.
- `ApprovalRequest.payload_purged_at: AwareDatetime | None`. A purged request reads `payload` as `None` with `payload_purged_at` set; a request that never stored a payload reads both as `None`. The approver role gets column UPDATE on `(payload_json, payload_purged_at)`, and the installer adds that grant on upgrade.
- `ApprovalConflictError`, `AuditLockTimeoutError`, `DenialReason.DELEGATE_APPROVAL` (`"delegate_approval"`, mapped to `NotAuthorizedToResolveError`, audited as `approval.resolve_denied` with that reason), and the audit events `approval.submit_conflict` and `approval.payload_purged`.
- `SQLAuditLog.lock_in(session)`: takes the append lock now, for a caller that will append later in the same transaction. The `lock_timeout` it sets stays for the rest of that transaction, so a wait for a row another transaction holds is bounded too.
- `agent_core_approvals_purgeable`, a partial index on finished requests that still hold a payload, by finish time: `purge_payloads` and `expire_due` read their candidates first and take the schema-wide append lock only for their writes.
- Delegate rule (its own commit): the approver may be neither the requester nor one of the request's delegates. `RoleApproverPolicy` denies after the self-approval check, and the guard refuses a `resolved_by` that is in the request's `delegates`. `list_pending` no longer offers a request to its own delegate. A delegate may still consume. A 0.1.0a4 request already approved by one of its own delegates stays readable and consumable, because the model does not forbid it.

### Changed

- Free text already stored with control, format or separator characters is shown with each such character replaced by U+FFFD, by `get` and `list_pending`.
- Lock order. A deadlock reproduced in a test: a host that appends first and then touches a request's row through `connection=`, against the queue's own transaction that took the row first and then the lock, and Postgres aborted one with `40P01`. The queue's own write transactions (no `connection=`) now take the append lock before they touch the request row. With `connection=` the queue does not take the lock early, because the transaction is the host's and a refusal is audited apart from it. A host should append, or let the library do both, before it updates approval rows in its own transaction, and never the reverse.

### Security

- At most one request is open per requester, action and payload hash, and the unique index holds when calls race. A repeat with other terms is refused and audited rather than merged.
- Control, bidirectional and zero-width characters in `summary`, `reason` and a cancel reason are refused on the way in by the library and the Postgres guard, and neutralised on the way out for text already stored. This fixes the a4 limit on control, ANSI and bidi characters in those fields.
- A bounded `resolved_at` (not before `created_at`, within 5 minutes of the database clock) fixes the a4 limit where the approver role could set a `resolved_at` before the request existed.
- The append lock wait is bounded (`lock_timeout`, default 5 seconds) and the lock key is per schema, which fixes the a4 limits of an unbounded wait and one lock for the whole database.
- Stored payloads can be purged, and only by the approver role, only after the retention floor, and only with an audit event. 0.1.0a4 had no way to purge one. Run `purge_payloads` on a schedule with `older_than` at or above the floor; project 04 keeps arguments at most 7 days.
- An approver can no longer be one of the request's own delegates, checked by the policy and by the guard.

### Known limits

- `requested_by` is written by the requester role. A compromised requester role can occupy another principal's key with a request of other terms. That principal then gets `ApprovalConflictError` naming the request, may cancel it (it is theirs by `requested_by`), and may resubmit.
- In a host's transaction (`connection=`) a queue call still takes the request's row first and the append lock second, as the host's earlier writes decide. A host that appends first and then calls the queue, against a library-owned call on the same request, can still be aborted by Postgres with `40P01`; append before you touch approval rows, or let the library do both. Reproduced only with the race window widened. A host that uses `connection=` should retry its transaction when it gets `40P01` (`psycopg.errors.DeadlockDetected`): Postgres aborts one side, nothing of it is committed, and a retry succeeds.
- An exact repeat submit returns the open request with the first caller's `context` and writes no event. `summary` is part of the match, so a row a requester role planted for another principal with a different summary is refused, not adopted (audited as `approval.submit_conflict`). `summary` is still not bound by the hash: show the stored `payload` to approvers.
- A guard refusal inside `resolve` (a custom policy that allows a delegate, an approver host clock more than 5 minutes off the database's) escapes as the driver's error, not audited. The guard's free-text pattern covers a subset of the characters the library refuses; the library neutralizes the rest when it reads. The delegate rule compares ids exactly (case-sensitive). The installer's `close_duplicates` and `close_unaudited_approvals` write no audit event, and an a4 duplicate kept is the oldest by `created_at`, which the requester writes.
- A role that can connect can still hold the append lock and delay writers, for up to `lock_timeout` for the library's own transactions. Set `idle_in_transaction_session_timeout` on the runtime roles.
- The database cannot force an audit event: with plain SQL a role can submit, decide, cancel, consume or purge without one. Only the installer's `unaudited_approvals` scan looks for approvals with no event. A reconciliation check is not built.
- `verify()` still stops at the first record that is malformed or has a wrong hash. A verify mode that reports every problem and keeps walking is planned for 0.1.0a6.
- The per-listing cost of requests the library hides or skips is unchanged: each costs a check on every listing, logged at most once a minute. An in-process cache of rejected rows is planned for 0.1.0a6.
- Not in 0.1.0a5 or 0.1.0a6, planned for 0.1.0a7: concurrent-append coalescing, `wait_for_decision`, the `anthropic` optional extra with recording format 3 (and its live re-record), and a pgbouncer CI job. The base install still depends on `anthropic`, replay still needs it, and a transaction-mode pooler is still not tested.
- Fixed in 0.1.0a5, from the 0.1.0a4 list: control and bidi characters in free text, the unbounded `resolved_at`, the unbounded append wait, and the database-wide lock key.

## [0.1.0a4] - 2026-10-03

Async storage on a connection pool, batch appends, writes inside a host's Postgres transaction, and approvals that can carry their exact payload.

### Changed (breaking)

- Storage is async. `Database.run(work)` takes `async def work(session)`; `Session.execute` and `Session.execute_count` are coroutines; `Database.run_sync` is gone; `SQLAuditLog.append_in` is a coroutine, and so is `audit_table_exists`. `install_postgres_schema` and the CLI stay blocking.
- A Postgres `Database` owns a `psycopg_pool.AsyncConnectionPool`, opened on first use on the running event loop. Close it with `await database.aclose()` or `async with`. Use one `Database` per event loop; a second loop gets a `ConfigError`. `open_database(url, *, max_connections=10)`.
- The `AuditLog` protocol gains `append_many(events)`. A host's own log must add it.
- The `ApprovalQueue` protocol's `submit()` gains `include_payload=False`. A host's own queue must accept it.
- `ApprovalRequest` gains `payload`, and `AuditEvent` gains `occurred_at`. `AuditRecord` gains `recorded_at`, outside the hash.
- `AuditEvent.occurred_at=None` now means the database's clock on Postgres. Before, it was always the application's clock. On SQLite it is still the writer's clock.
- Postgres 16 or later. An older server is refused with a `ConfigError` at connect and at install.
- The Postgres schema changes: `audit.recorded_at` and `approvals.payload_json` are new columns, the audit insert trigger is replaced, and the approvals guard covers `payload_json`. An a3 schema is refused until `install_postgres_schema` is run again as the owner (see docs/upgrading.md). The audit log and the approvals queue each raise a `ConfigError` that says so before they write anything.
- The `postgres` extra is `psycopg[binary,pool]`.
- Payloads may nest at most 32 levels (`MAX_PAYLOAD_DEPTH`). The rule is the audit payload rule, so an `AuditEvent` payload deeper than that is refused now too.

### Added

- `PostgresDatabase.from_pool(pool)`: borrows a host's pool, never closes it.
- `connection=` on `SQLAuditLog.append` and `append_many` and on `SQLApprovalQueue.submit`, `get`, `list_pending`, `resolve`, `consume`, `cancel`, `expire_due` and `side`: run inside a transaction the host already has open on a psycopg `AsyncConnection`, in a savepoint, never committing, rolling back or closing it. Postgres only. The connection must be in a transaction at READ COMMITTED, or `ConfigError`. See docs/upgrading.md for the hazards.
- `SQLAuditLog.append_many(events)`: all or nothing, one lock, one commit, consecutive records, at most `MAX_APPEND_BATCH` (1000) events; an error names the index of the event it refused.
- `AuditEvent.occurred_at`, bounded by `OCCURRED_AT_MAX_PAST` (24 hours) and `OCCURRED_AT_MAX_FUTURE` (5 minutes) around the database's clock, else `AuditTimeRejectedError`. `AuditRecord.recorded_at`, set by the Postgres insert trigger whatever the writer sends.
- `submit(..., include_payload=True)` stores the exact payload with the request (`payload_json`), at most 8192 bytes of canonical JSON, under the audit log's rules; `ApprovalRequest.payload` holds it. Every `get`, `list_pending` and `resolve` checks it against `payload_sha256`.
- `aox_agent_core.sync`: `SyncAuditLog` and `SyncApprovalQueue`, blocking wrappers that run on a background loop thread, with `close()` and context-manager support. They raise `EventLoopRunningError` inside a running loop and take no `connection=`.
- `ApprovalIntegrityError` and `ApprovalPayloadRejectedError` (`ApprovalError`), and `AuditTimeRejectedError` (`AuditError`).
- `benchmarks/audit_throughput.py` and `benchmarks/README.md`.
- docs/upgrading.md: the upgrade from 0.1.0a3 for a project with its own backends, a project on the SQL backends, and the operator.

### Changed

- Every Postgres statement runs with `prepare=False`. Isolation and `search_path` are set per transaction (`SET TRANSACTION`, `SET LOCAL`), never per session. There is no `LISTEN`, and advisory locks are transaction-level. This fits a transaction-mode pooler; it has not been tested against one.
- The Postgres audit insert trigger takes the same advisory lock the library takes, and requires `seq` to be the head's plus one and `prev_hash` to equal the head's `record_hash` (genesis zeros when empty), schema version 3, the shape of `event_id`, `record_hash` and `occurred_at`, and the `occurred_at` bounds. It cannot check `record_hash`, which `verify()` recomputes.
- The SQLite append trigger also checks `prev_hash`. An a3 SQLite file gets the new trigger and the `recorded_at` and `payload_json` columns in place on first use.
- A denied `consume`, `resolve` or `cancel` inside a host transaction is audited on a separate connection of the audit log's own pool and committed at once, waiting at most 2 seconds for a pooled connection (`DENIAL_ACQUIRE_TIMEOUT`) and 2 seconds for the append lock (`DENIAL_LOCK_TIMEOUT`). If that fails, a warning naming the action, subject, actor and reason is logged and the event is written in the host's transaction (with the same lock bound), and lost if the host rolls back. If that fails too, the refusal is still raised, with a note on it saying it was not audited, and the details are logged. A role that can connect can hold the append lock and force every refusal onto that fallback. `connection=` needs an audit log that shares the queue's database (the same `Database` object), or `ConfigError`.
- The audit hash is unchanged. It covers `occurred_at` as before and not `recorded_at`; the schema version stays 3, and records from a3 verify as they did.
- docs/compat-03.md maps project 03's interfaces to this release.

### Security

- Stored approval payloads are bound to the request. `resolve` refuses a request whose stored payload does not match `payload_sha256` and audits `approval.resolve_denied` with reason `payload_integrity`; the request stays pending. `get` raises `ApprovalIntegrityError` and `list_pending` omits it. `cancel`, `consume` and `expire_due` do not check a stored payload and return `payload=None`, so a requester can always withdraw. A stored payload must also pass the rules a submit applies (keys, integers only, size, secrets) when it is read, so one the requester wrote with plain SQL cannot show an approver a float, an unsafe integer or a secret, and a malformed one (`1e400`, a huge integer, deep nesting) hides only its own request. A stored run context is still read as it was written, so a rule added later never makes a request unreadable; an audit event about a stored request whose context today's rules refuse is written without it and carries `run_context_dropped`, so a decision, a refusal or the expiry sweep is never blocked by it. A request left out of a listing is logged, at most once a minute, with a count and the first id; hash mismatches are rejected first, so hiding a forged row is cheap, and a page is checked off the event loop. The Postgres guard bounds a run context at 2048 bytes and 16 external ids, and delegates at 4096 bytes. The payload is never copied into the audit log. `summary` is written by the requester and is not covered by the hash, so a UI must show `payload` when present.
- The Postgres guard makes `payload_json` immutable after insert and refuses a value that is not a JSON object or is over 8192 bytes. The requester and approver roles have no UPDATE on it.
- `recorded_at`, like `db_role`, is guaranteed by the database trigger, not by the chain: a table owner could edit it undetected. On SQLite the library writes it from the writer's clock, so it is not independent there.
- Nothing purges a stored payload. Count it in retention and deletion plans. (Addressed in 0.1.0a5.)
- The audit insert trigger also bounds the text columns a writing role can fill: `action`, `actor_id` and `subject_id` by the library's own patterns and lengths, `payload` at 8192 bytes and a JSON object, `run_context` at 2048 bytes and a JSON object. The approvals guard measures a request's lifetime in 168 hours, not "7 days", which the writer's session time zone could stretch to 169 hours and make unreadable. `tests/test_untrusted_columns.py` holds the matrix: for every column the requester role can write with plain SQL, a hostile value is refused by the database or survived by every reader.
- A stored request this library will not read (a lifetime or decision order its model refuses) no longer crashes readers: `list_pending` leaves it out and logs, `get` raises `ApprovalIntegrityError`, `resolve`, `consume` and `cancel` refuse it with an audited `malformed_row` denial, and `expire_due` skips it and logs. A request dated up to five minutes ahead is decided at its own date, not before it. `verify()` reports a record it cannot parse or hash as `AuditIntegrityError`, not a raw exception.

- CI runs Postgres 16 across the Python matrix and Postgres 17 on Python 3.12, in a separate job that is not a required check. Not tested against a real pooler.

### Known limits

Found in review and left for 0.1.0a5:

- Any role that can connect can hold the single, database-wide append lock and stall every writer; the library's own append transactions have no `lock_timeout` yet. Set `idle_in_transaction_session_timeout` on the runtime roles. (Addressed in 0.1.0a5.)
- `summary` (requester) and `reason` (approver) accept control, ANSI and bidi characters, up to 500 characters. A UI or terminal that shows them must neutralize them. (Addressed in 0.1.0a5.)
- The approver role may set any canonical `resolved_at`, even one before the request's `created_at`; the library then refuses to read that request. (Addressed in 0.1.0a5.)
- `verify()` stops at the first record that is malformed or has a wrong hash; its message names the record but, for a malformed one, not the `db_role` column. Tampering after that record is not checked until it is dealt with.
- The database cannot force an audit event: with plain SQL a role can submit, decide, cancel or consume without one. Only the installer's `unaudited_approvals` scan looks for approvals with no event.
- A requester can fill the approvals table with rows this library hides or skips; each costs a small check on every listing, and is logged at most once a minute.

## [0.1.0a3] - 2026-10-03

Approvals enforced by Postgres itself, not only by the library.

### Security

- In 0.1.0a2, Postgres approvals relied on library checks only. `install_postgres_schema` gave one application role SELECT, INSERT and UPDATE on the approvals table, so that role could set a request approved with plain SQL, bypassing the rule that only a human holding the required role may resolve it. 0.1.0a3 adds database enforcement: separate requester and approver roles, column grants, and a guard trigger that checks every insert and update against the allowed transitions using the database's own role membership and clock. A host on 0.1.0a2 should upgrade (see docs/upgrade-0.1.0a3.md) and check its approved requests against the audit log with the query given there.
- `consume()` accepted any principal. It now requires the requester, or a delegate the request names.
- Both roles may append audit records and `actor_id` is supplied by the library, so a record could claim the wrong actor. Audit records now carry `db_role`, set by the database to the inserting role.
- The library pins `search_path` to `pg_catalog, pg_temp` on every Postgres transaction, and the installer and every queue refuse to run while the requester role can create objects in the install schema or in `public` (PUBLIC's default before Postgres 15), where approver-side code could run them with the approver's rights.
- The guard requires the library's canonical UTC timestamps and checks every column's shape, so session settings cannot change how a lifetime is read and a hand-written row cannot block the approver's listing.
- The requester writes `required_role`, so it no longer decides who may approve. `RoleApproverPolicy` takes the role each action needs from `roles_by_action`, configured on the approver side, and refuses a request for an unlisted action (`unknown_action`) or whose stored role differs (`role_mismatch`), both audited.
- Requests approved by plain SQL under 0.1.0a2 survive an upgrade. The guard refuses to let a request whose lifetime exceeds 7 days be decided or used, the install report lists approved, unused requests no `approval.resolved` event approves (`unaudited_approvals`), and `close_unaudited_approvals=True` cancels them during the upgrade.

### Changed (breaking)

- `install_postgres_schema(owner_url, *, requester_role, approver_role, schema="public")` replaces `app_role=`, returns an `InstallReport`, and needs Postgres 14 or later (with `CREATE ON SCHEMA public` revoked from PUBLIC). It is idempotent, schema-qualified and upgrades a 0.1.0a2 schema in place.
- On Postgres a queue connects as the requester role (the agent side: submit, consume, cancel) or the approver role (the decision side: approve, reject); a call from the wrong side raises `ConfigError`. A queue refuses to start if the schema predates 0.1.0a3, lost its guard, or the roles are set up wrongly.
- `consume()` raises `NotTheRequesterError` for anyone but the requester or a named delegate.
- `RoleApproverPolicy()` with no arguments refuses every request. Pass `roles_by_action={...}` on the approver side, or `trust_requester_role=True` to take the requester's `required_role` as given (local development only). A subclass whose `__init__` skips `super().__init__()` refuses too.
- The `ApprovalQueue` protocol gains `cancel()` and `expire_due()`, and `submit()` a `delegates` keyword. A host's own queue must add them.
- `ApprovalRequest` gains `closed_at`, set exactly when the status is `EXPIRED` or `CANCELLED`, and `delegates`. Both statuses are now stored.
- Audit schema 3: `AuditRecord.db_role`, outside the hash. Version 2 records in an upgraded chain keep their version and still verify. `UnsealedAuditRecord.schema_version` accepts 2 or 3.
- Postgres tables from 0.1.0a2 are refused until the 0.1.0a3 installer upgrades them; a SQLite file from 0.1.0a2 gains the new columns in place.

### Added

- `cancel(request_id, *, principal, reason=None, context=None)`: the requester withdraws a pending request; audited, refusals included.
- `expire_due(*, principal, now=None, limit=500)`: stores EXPIRED on pending requests past their lifetime, audited per request; either side may run it. Reads still report such requests as expired without it.
- `submit(..., delegates=...)`: up to 16 principals allowed to consume in the requester's place, fixed and shown to the approver.
- `schema=` on `SQLAuditLog` and `SQLApprovalQueue`, and `--schema` on `aox-agent-core audit verify`.
- `ApprovalSide`, `SQLApprovalQueue.side()`, `NotTheRequesterError`, `storage.InstallReport` and `storage.Grant`.
- `RoleApproverPolicy(roles_by_action=..., trust_requester_role=...)`, and the denial reasons `NOT_REQUESTER`, `UNKNOWN_ACTION` and `ROLE_MISMATCH`.
- docs/upgrade-0.1.0a3.md: the role layout, the transition table, setup, and the upgrade from 0.1.0a2.

## [0.1.0a2] - 2026-10-02

What a consuming project needs to key replay by prompt, send images and PDFs, and tie calls, approvals and audit records to its own runs.

### Changed (breaking)

- Recordings are format 2: one JSON file per exchange, at `prompts/<prompt id>/v<version>/<key>.json` for prompted calls and `requests/<cassette>/<request hash>.<n>.json` for the rest. Format 1 cassettes are refused with a message saying to record them again.
- Removed `Cassette`, `CassetteEntry`, `CASSETTE_FORMAT_VERSION`, `CassetteStore`, `DirectoryCassetteStore` and `CassetteConflictError`. `DirectoryRecordingStore`, `Recording` and `RECORDING_FORMAT_VERSION` replace them.
- The audit log is schema 2: every record has a `run_context`, included in its hash. `UnsealedAuditRecord` requires it (pass `None` for none). Audit and approval tables created by 0.1.0a1 are refused with a `ConfigError`; the library never alters an existing table.
- `ModelProvider.complete` takes a keyword-only `prompt_key` (a `PromptKey` or `None`). Custom providers must accept it.
- `ApprovalQueue.submit`, `resolve` and `consume` take a keyword-only `context` (a `RunContext` or `None`). A host's own queue must accept it to match the protocol.
- The module `aox_agent_core.replay.cassette` is gone; `request_hash` lives in `replay.keys` and is still exported from `aox_agent_core.replay`. `store.parse_cassette` is now `parse_recording`.
- Prompted recordings are shared by every cassette name: they live under `prompts/`, so `replay.cassette` and the `use_cassette(name)` fixture no longer separate them per test. Identical prompted calls in two tests replay the same file.
- Two processes recording the same unprompted cassette at once can now overwrite each other's numbered files; 0.1.0a1 detected that conflict. Record one process at a time.
- `Message` has an `attachments` field, allowed on user messages only. Request hashes leave out fields at their defaults, so hashes of 0.1.0a1 requests are unchanged.
- A replayed response is priced as recorded, not at the model the route now names: at its own model's rate on the provider it was recorded with, else at the rate of the model the recorded request named. If neither has a price, it is a `ConfigError`. `ProviderResponse` gains `recorded_provider` and `recorded_model`, set only by replay and never stored.
- The repository's triage eval cases take an object of prompt inputs (`{"ticket": ...}`) instead of a string.

### Added

- `PromptRef(id, version, template, system=None)`: a versioned prompt, rendered by agent-core from `${name}` placeholders, with non-string inputs as compact JSON with sorted keys. Inputs must be plain JSON, or `PromptError` is raised. `AgentClient.call` and `call_sync` take a `PromptRef` with `inputs=`.
- `Attachment.from_bytes()` and `Attachment.from_path()`: PNG, JPEG and PDF, typed by their first bytes, capped at 5 MB per image and 24 MB per PDF. An attachment built directly must match its bytes in type, size and SHA-256. `from_path` reads only regular files, and no more than the cap. Passed with `attachments=` on the last user message and sent as base64 image or document blocks. No image preprocessing.
- Content-addressed replay keys: `replay_key()` and `PromptKey` hash the prompt ID and version, routed tier, schema name, NFC-normalized inputs, attachment SHA-256s and attempt number, never the model ID, call order, run IDs or timestamps. Recordings store attachments as hash, type and size only.
- `ReplayMissError` carries `key` and `path` and never falls back to a live call. A miss where only the tier differs says the routed tier differs from the recorded one, for example after a price-table change flips a budget drop. A file whose stored key is not the one looked up (copied or renamed) raises `CassetteFormatError`.
- `StaleRecordingError` when a prompt's template, system prompt or output schema changed without a version bump. The generated output schema is part of that check, so upgrading `anthropic` or `pydantic` can make structured prompted recordings stale; record them again.
- `RunContext(run_id, external_ids)`: opaque, size-capped, read-only ids, refused if they look like secrets by the default patterns, by the audit log's scrubber, or by the client's `replay.extra_secret_patterns`. Contexts read back from storage are not scanned again. Accepted by `call`, `AuditEvent`, and the approval queue's `submit`, `resolve` and `consume`; set as span attributes (`agent_core.run_id`, `agent_core.external_id.<name>`), stored on audit records and approval requests, never part of a replay key.
- `ModelClient`, the protocol `AgentClient` implements. `CallResult` reports `replay_key` and `prompt_id`. New span attributes for the prompt ID and version, the replay key and the attachment count.
- `model_call_target(..., prompt=PromptRef)` for prompted eval suites; it accepts any `ModelClient`, and refuses `system` alongside a `PromptRef`.
- Budget checks count attachments with a pessimistic per-image and per-PDF-page estimate. PDF pages are counted once, including those in compressed object streams (`Attachment.pdf_pages`); a PDF whose pages cannot be counted is budgeted at 100 pages. Counting is best effort against a PDF built to hide its pages; `routing.count_pdf_pages = false` budgets every PDF at 100 pages. `AgentCoreConfig.has_price()`.
- A test proving a host can supply its own `AuditLog` and `ApprovalQueue`, and `docs/compat-03.md`, which maps a consuming project's draft interface names to agent-core's.
- The example now reads a synthetic invoice PDF, and both it and the triage eval were recorded again from the live API as prompted calls.

## [0.1.0a1] - 2026-10-02

The first pre-release. Projects can pin it; the API may still change before 0.1.0.

### Added

- Routed model calls: the Anthropic provider with the key and base URL always passed explicitly, tier and task routing, a per-call budget (retries and escalation included) that raises or drops one tier, structured outputs with retries and one-tier escalation, and cost from a dated price table.
- The API key comes only from an explicit argument or `AGENT_CORE_ANTHROPIC_API_KEY`; `ANTHROPIC_*` variables are never used, and live calls refuse to start while `ANTHROPIC_CUSTOM_HEADERS` is set.
- `AgentClient` with `call` and `call_sync` (one event loop per client), usable with `with` and `async with`.
- OpenTelemetry spans with token, cost and routing attributes; prompt and completion text only with `tracing.capture_content`, scrubbed.
- Record and replay: cassette format version 1, checked against live API responses; canonical request hashes with sequence numbers; a secret scrubber that refuses to write by default; `aox-agent-core cassettes check`; the `use_cassette` pytest fixture.
- An append-only, hash-chained audit log on SQLite or Postgres: update and delete triggers (and truncate on Postgres), a Postgres app role limited to insert and select, serialized appends, and `verify()` with an external anchor; `aox-agent-core audit verify`.
- A human-approval queue on the same backends: a role policy (human, holds the role, not the requester, pending and unexpired), compare-and-set resolution, single-use `consume(..., principal=...)` bound to the action and payload, `list_pending` with an `after=` cursor, and every outcome, denials included, audited in the same transaction when the audit log shares the database.
- An eval runner: JSONL suites, exact-match and field-match scorers, accuracy, failures, p50/p95 latency and cost, and JSON and Markdown scorecards that state the run's mode; a replayed run reports no latency, since replay timing is not model latency. A synthetic triage suite runs in replay mode.
- Configuration with packaged defaults: tier models and dated Anthropic and Bedrock prices. The Bedrock provider is an interface only, and the small tier has no Bedrock default.
- Extras `bedrock`, `postgres`, `otel` and `testing`; examples for a routed call and for the control layer.
- CI on every pull request (lint, types, tests on Python 3.11 to 3.14 with Postgres, package check, gitleaks) and a tag-driven release workflow.

[Unreleased]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a6...HEAD
[0.1.0a6]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a5...v0.1.0a6
[0.1.0a5]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a3...v0.1.0a4
[0.1.0a3]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/AOX-LLC/agent-core/releases/tag/v0.1.0a1
