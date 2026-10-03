# Upgrading from 0.1.0a6 to 0.1.0a7

Release 0.1.0a7 follows 0.1.0a6. (0.1.0a5 was tagged but never released; the upgrade from 0.1.0a4 is described further down.) It adds three things and one operator step. The audit table records the login that wrote a row (`db_login`). The approvals guard can bind `resolved_by` to the database login that decides a request; this is off unless the operator turns it on. `wait_for_decision` waits for a request to leave `pending`. `SQLAuditLog.verify_report` lists every problem in a chain instead of stopping at the first.

Unchanged: the recording format stays at 2, there is no new optional extra, and `anthropic` is still a base dependency. `db_login` is outside the audit hash.

The required step is a reinstall: run `install_postgres_schema` from 0.1.0a7 as the owner role before the first 0.1.0a7 process connects. This holds for every Postgres deployment, whether or not you use login binding.

This part of the page has an operator section, one for project 03 (its own backends), one for project 04 (the SQL backends), and a list of what is not verified. Where this page says "not tested", nothing was run.

## Operators (0.1.0a7)

1. **Stop the a6 processes, install, then start a7.** An a6 process that is still running writes audit records at schema version 3, which the new insert trigger refuses, and an a6 process that connects after the install refuses the new guard, so do not leave one running. Run the installer as the owner role, with the same roles as before:

   ```python
   from aox_agent_core.storage import install_postgres_schema

   report = install_postgres_schema(
       "postgresql://agent_core_owner@db.example/agent_core",
       requester_role="agent_core_requester",
       approver_role="agent_core_approver",
       schema="public",
       bind_resolved_by=None,
   )
   print(report)
   ```

   `bind_resolved_by=None` is the default and is shown for clarity. The installer changes, in one run: it adds the column `db_login` to the audit table; replaces the audit insert trigger (revision 6); creates the table `agent_core_approver_logins` and the function `agent_core_bound_principal()`; and replaces the approvals guard function (revision 7, with a `-- agent-core login binding on|off` comment line).

2. **A refused a6 schema.** An a7 queue or audit log refuses an a6 schema with a `ConfigError` before it writes anything. The audit insert trigger carries `-- agent-core audit trigger revision 6` and the guard `-- agent-core guard revision 7`; a connection that finds revision 5 of the trigger or revision 6 of the guard tells the operator to run `install_postgres_schema` from 0.1.0a7. So run the installer before the first a7 process connects, for everyone, bound or not. On SQLite nothing is required of you: the `db_login` column is added in place on first use.

3. **`bind_resolved_by` keeps the installed setting by default.** `None` keeps whatever the schema has now, which is off in a fresh install, so a reinstall never switches binding off by accident. `True` turns binding on and `False` turns it off. Binding is off by default because a host whose approvers share one database login, or reach the database through a pooler login shared by many users, cannot use it: the guard judges the login.

4. **Turn binding on only after every approver has a login and a mapping.** With binding on, a login that has no active mapping cannot decide any request, and a queue connected as such a login refuses to start. The order that avoids an outage:

   1. Create or identify one database login for each approver, as a member of the approver role and not a member of the requester role. It must not be a superuser.
   2. Map each login to the principal id the application will pass as `resolved_by`, as the owner role and not from the application (the example below).
   3. Run `install_postgres_schema(...)` again and read `InstallReport.unmapped_logins`: the login roles that are members of the approver role and have no active mapping. With binding on, each of them cannot decide requests. `InstallReport.login_binding` says whether binding is on.
   4. Run `install_postgres_schema(..., bind_resolved_by=True)`.

   ```python
   from aox_agent_core.storage import (
       bind_approver_login,
       install_postgres_schema,
       unbind_approver_login,
   )

   OWNER_URL = "postgresql://agent_core_owner@db.example/agent_core"

   bind_approver_login(OWNER_URL, login="approver_alice", principal="user-17", schema="public")

   report = install_postgres_schema(
       OWNER_URL,
       requester_role="agent_core_requester",
       approver_role="agent_core_approver",
       schema="public",
       bind_resolved_by=True,
   )
   assert report.login_binding
   print(report.unmapped_logins)  # logins with no active mapping, if any

   # Later, when approver_alice leaves:
   unbind_approver_login(OWNER_URL, login="approver_alice", schema="public")
   ```

   `bind_approver_login` raises `ConfigError` unless the login is a login role, is not a superuser, is a member of the approver role and not of the requester role, and neither the login nor the principal has ever been mapped, active or removed.

5. **What the guard then enforces.** On a decision to approved or rejected, `resolved_by` must equal the principal mapped to `session_user`, the login that authenticated. An unmapped login is refused too. The refusal text is "resolved_by must be the principal mapped to the deciding login". With binding off, nothing changes from 0.1.0a6. The identity judged is `session_user`, not `current_user`: `SET ROLE` changes `current_user` only, so a member who logs in as themselves and switches role, to another approver's login or to the approver group role, is still judged as themselves and can record only their own mapped principal. Only a superuser can change `session_user`, and the guard already refuses superusers.

6. **The mapping table.** `agent_core_approver_logins(login, login_oid, principal, mapped_at, removed_at)` is always installed, bound or not.

   - `login` is the primary key, `principal` is unique and `login_oid` is unique. The OID is the role's OID when it was mapped, so a role dropped and recreated under the same name inherits nothing, and a rename cannot move a mapping.
   - Rows are never deleted: a trigger refuses DELETE and TRUNCATE. A mapping ends only when `removed_at` is set, once.
   - Both uniques cover removed rows. In practice a login or a principal is never reused after removal: to bring a person back, create a new role under a new name and map it to a new principal id.
   - Only the owner can touch the table. The installer revokes everything on it (table and column privileges) from the requester and approver roles, and a queue refuses to start if any other role holds a privilege on it, or if the table or the function was made by a role other than the approvals table's owner. The guard reads it through a SECURITY DEFINER function, `agent_core_bound_principal()`, with its `search_path` pinned and EXECUTE granted only to the approver role. The function returns only the connecting login's own principal.

7. **What a queue checks when binding is on.** `SQLApprovalQueue` raises `ConfigError` (from `side()` or the first call) if the mapping table, the lookup function or the table's triggers are missing; if the requester role, the approver role or any role the connection can switch to can write the mapping table; or, on the approver side only, if the connecting login has no active mapping (the message names the login). `resolve` compares `principal.id` with the mapped principal before it writes and raises `NotAuthorizedToResolveError`, audited as `approval.resolve_denied` with the new `DenialReason.LOGIN_BINDING` (`"login_binding"`). That check is a courtesy for a clear error: the guard is the enforcement, and it also refuses plain SQL.

8. **Limits of binding.**

   - It binds `resolved_by` only. It does not bind the audit event's `actor_id`.
   - A shared login cannot use it.
   - The owner and superusers are trusted.
   - Role OIDs can in theory be reused after OID wraparound.
   - The requester role still has no route to approved. Binding adds a check and removes none.
   - It was not tested against a real pooler.
   - It is only as strong as each login's authentication. Under `trust` in `pg_hba.conf`, or with a password shared between approvers, any login can connect as another.
   - If the approver role is itself a login (the example deployment connects the approver service as it), it needs its own mapping and is listed in `unmapped_logins` until it has one.
   - Login names are written to every audit row (`db_login`) and to the mapping table, and neither can be erased: both are append-only. Use pseudonymous login names (`approver_17`), not personal names, if your data-retention rules require that.

9. **Reading `db_login`.** Each audit row now has `db_login`, set by the insert trigger to `session_user` whatever the writer supplied, outside the hash, next to `db_role` (still `current_user`). `SET ROLE` changes `db_role` but never `db_login`, so a row written after a role switch names the real login. Records written before 0.1.0a7 have `db_login` NULL.

## Project 03: its own backends (0.1.0a7)

Almost nothing changes. The `ApprovalQueue` and `AuditLog` protocols are the same.

1. **`AuditRecord.db_login` is a new optional field,** `str | None`, default `None`. A host with its own `AuditLog` that builds `AuditRecord` or inserts rows itself needs no change; set it only if your store can tell you the login. `AUDIT_SCHEMA_VERSION` is now 4. Records at version 2 and 3 still verify.
2. **`wait_for_decision` is a free function,** not a protocol method. A host-supplied queue needs no new method (see the project 04 section).
3. **`DenialReason.LOGIN_BINDING` exists** (`"login_binding"`). A host that maps denial reasons to its own errors should map it like the other reasons, to `NotAuthorizedToResolveError`. Only agent-core's own queue produces it.
4. If it uses agent-core's installer, re-run it as the owner from 0.1.0a7 (see Operators).

## Project 04: the SQL backends (0.1.0a7)

1. **Audit schema 4 and `db_login`.** On Postgres `AuditRecord.db_login` is the login that authenticated the connection (`session_user`) and `db_role` is the role the statement ran as (`current_user`). They differ after a `SET ROLE`. On SQLite `db_login` is `None`, as it is for records written before 0.1.0a7. In a review query, select `db_login` beside `db_role` on the rows you care about, such as `approval.resolved`: `db_login` names who wrote a row, and `db_role` shows which role they acted as. Check the audit table's name and schema in your own installation.

2. **Binding.** If you turn it on (see Operators), `resolve` for an approver whose login is not mapped to `principal.id` raises `NotAuthorizedToResolveError` with `DenialReason.LOGIN_BINDING`, and the denial is audited as `approval.resolve_denied`. Each approver must connect as their own login; an application that connects every approver through one login cannot use binding. The limits are listed in Operators.

3. **`verify_report`.** `SQLAuditLog.verify_report(*, expected_head=None, max_problems=1000) -> VerifyReport` walks the whole chain and does not raise at the first bad record. `VerifyReport` and `VerifyProblem` are importable from `aox_agent_core.audit`.

   ```python
   report = await audit_log.verify_report(expected_head=saved_head)
   if not report.ok:
       for problem in report.problems:
           print(problem.seq, problem.kind, problem.detail)
   print(report.records_checked, report.truncated)
   ```

   - `VerifyReport` has `head`, `records_checked`, `problems` (a tuple) and `truncated`, and a property `ok`. `VerifyProblem` has `seq` (an int or `None`), `kind` (`"malformed"`, `"seq"`, `"link"`, `"hash"` or `"anchor"`) and `detail`.
   - It keeps walking from a bad record's stored hash, so one altered record is reported once, at its own `seq`. A missing record is one `seq` problem and a cut link is a `link` problem. With `expected_head`, a rewritten chain or a cut tail shows as `anchor`.
   - It stops at `max_problems` and sets `truncated`. It is read-only.
   - `verify()` is unchanged and still raises at the first problem.
   - It is a method of `SQLAuditLog`, not of the `AuditLog` protocol or of `SyncAuditLog`, so a host-supplied log needs nothing.
   - An empty problem list is no proof against someone who can rebuild every hash. Keep the head somewhere the application cannot write, as before. `aox-agent-core audit verify` stays as the cross-check.

4. **`wait_for_decision`.** `from aox_agent_core.approvals import wait_for_decision`.

   ```python
   from datetime import timedelta

   from aox_agent_core.approvals import ApprovalWaitTimeoutError, wait_for_decision

   try:
       request = await wait_for_decision(queue, request_id, timeout=timedelta(hours=72))
   except ApprovalWaitTimeoutError as timed_out:
       last = timed_out.last  # the last request read
   ```

   - Signature: `wait_for_decision(queue, request_id, *, timeout, poll_interval=timedelta(seconds=1), max_poll_interval=timedelta(seconds=30)) -> ApprovalRequest`. It works over any `ApprovalQueue`, because it only calls `get`.
   - It reads once at once, then polls. The pause starts at `poll_interval` and roughly doubles after each read, with jitter between x0.75 and x1.25, capped at `max_poll_interval` and never longer than the time left. A 72 hour wait does not poll every second.
   - It returns as soon as the status is not `PENDING`: approved, rejected, cancelled, expired (a lapsed request reads as expired) or consumed. The caller decides what each means, and an approved request still has to be consumed.
   - It raises `ApprovalWaitTimeoutError` (an `ApprovalError`, with `.request_id` and `.last`) on timeout; `ValueError` for a non-positive `timeout` or `poll_interval`, or a `max_poll_interval` below `poll_interval`; and whatever `get` raises (`ApprovalNotFoundError`, `ApprovalIntegrityError`).
   - It takes no `connection=` and holds no connection or transaction between reads: each read is its own short transaction. That suits a transaction-mode pooler; this was not tested against one. Cancelling the task cancels only the wait.
   - `SyncApprovalQueue.wait_for_decision(request_id, *, timeout, poll_interval, max_poll_interval)` blocks the caller with the same arguments.

## What is not verified (0.1.0a7)

- **Postgres versions.** As in earlier releases, CI runs Postgres 16 and 17 only. Postgres 18 was not run.
- **A real pooler.** Neither login binding nor `wait_for_decision` was run against pgbouncer or any other pooler.
- **Binding with shared logins.** Binding was not exercised with a host whose approvers share one database login.
- **A reinstall under load.** The a6 to a7 installer run was not tried on a large or busy database, or with writers active. Plan a window with no writers.
- **a6 code against an a7 schema.** Not tested.

---

# Upgrading from 0.1.0a4 to 0.1.0a6

0.1.0a5 was tagged but never released: there is no GitHub release and no wheel for it. 0.1.0a6 is the fix release and contains everything 0.1.0a5 had. Go from 0.1.0a4 straight to 0.1.0a6, in one step. This page describes that step. Nothing here asks you to install 0.1.0a5.

Release 0.1.0a6 completes the approvals model. A repeated submit returns the open request, an approval that lapses unused expires, stored payloads can be purged, an approver may be neither the requester nor a delegate, free text loses its control characters, and the audit append lock is bounded and keyed per schema. It changes the `ApprovalQueue` protocol, the `ApprovalRequest` model and the Postgres schema. There is one operator reinstall: run `install_postgres_schema` from 0.1.0a6 as the owner role again.

This part of the page has an operator section, one for project 03 (its own backends), one for project 04 (the SQL backends), and a sub-section on the delegate rule, which is a behavior change. The upgrade from 0.1.0a3 to 0.1.0a4 follows further down and still applies to a database that is on a3: a database on a3 gets the a4 changes and the changes below from the same single installer run of 0.1.0a6, and the a3 notes on the report still apply. "What is not verified" at the end covers both.

Everything below was taken from the 0.1.0a5 and 0.1.0a6 change sets (see CHANGELOG.md). Where this page says "not tested", nothing was run.

## Operators (0.1.0a6)

1. **Install first, then deploy a6.** Run the installer as the owner role, with the same roles as before:

   ```python
   from datetime import timedelta

   from aox_agent_core.storage import install_postgres_schema

   report = install_postgres_schema(
       "postgresql://agent_core_owner@db.example/agent_core",
       requester_role="agent_core_requester",
       approver_role="agent_core_approver",
       schema="public",
       close_duplicates=False,
       payload_retention_floor=timedelta(hours=24),
   )
   print(report)
   ```

   Both new keywords are shown at their defaults. The installer changes, in one run: it adds the column `approvals.payload_purged_at`; builds the unique index `agent_core_approvals_one_open` and the purge index `agent_core_approvals_purge_due`; replaces the approvals guard function (`approved -> expired`, the database stamping `closed_at`, `consumed_at`, a rejection's `resolved_at` and `payload_purged_at` itself, the purge rule, the delegate rule, the `resolved_at` bound, the free-text rule, the revision comment and the floor comment); replaces the audit insert trigger (schema-keyed lock, revision comment); and grants the approver role column UPDATE on `payload_json` and `payload_purged_at`.

   About the purge index: its predicate also requires the finish time to be a canonical timestamp, which matches the library's purge query on Postgres, so the planner can use it for the ordered scan. If the index `agent_core_approvals_purgeable` is present, the installer drops it. SQLite does the same on first use. A database coming from 0.1.0a4 has only the new index to build.

   About the stamps: the guard writes `closed_at`, `consumed_at`, a rejection's `resolved_at` (never before the request's `created_at`) and `payload_purged_at` from the database clock, whatever the statement carried. An approval's `resolved_at` is not stamped, because it is not a finish time. It keeps its bound: not before `created_at`, within 5 minutes of the database clock.

2. **A refused a4 schema.** An a6 queue or audit log refuses an a4 schema with a `ConfigError` before it writes anything. The insert trigger carries `-- agent-core audit trigger revision 5` and the guard `-- agent-core guard revision 6`; a connection that finds an older guard or trigger tells the operator to run `install_postgres_schema` from 0.1.0a6 again. So run the installer before the first a6 process connects.

3. **a4 code against an a6 schema: not tested.** What is known: the a4 library checked its schema by columns and had no knowledge of the new rules, so it would not detect the new guard and trigger. Whether it works against them was not tried. Plan a short window with no writers: stop the a4 processes, run the installer, start a6.

4. **Duplicates and `close_duplicates`.** 0.1.0a4 allowed several open requests for one requester, action and payload hash. A database that holds any is refused by the installer with a `ConfigError` that lists them, and nothing is changed. With `close_duplicates=True` the installer keeps each group's approved request (or, if none is approved, its oldest) and cancels the other pending ones. It writes no audit event for them. The ids it cancelled are in `InstallReport.closed_duplicates`; copy them into your own record. A group with two approved requests is still refused: a person decides which one stands, then the installer is run again.

5. **`payload_retention_floor`.** The shortest `older_than` the database accepts for a purge. The default is 24 hours. It is written into the guard as `-- agent-core payload retention floor <n> seconds`, so changing it means running the installer again with the new value. The library raises `ValueError` for an `older_than` shorter than the floor. SQLite has no floor.

6. **Purging needs the approver role.** The approver role gets column UPDATE on `(payload_json, payload_purged_at)`; the installer adds the grant once, when it upgrades a schema that predates the column, and a later run does not give back what you revoked. The requester role must not have that grant, and a connect check refuses a requester role that does. Whatever runs `purge_payloads` therefore connects as the approver role. See the project 04 section for the schedule.

   **Check `backdated_finishes` before the first purge.** The installer report has a new field, `backdated_finishes`: finished requests that still hold a payload and whose finish time is before their own creation or decision. 0.1.0a4 let the closing role write `closed_at` and `consumed_at`, so such a row is purgeable at once under any retention floor. At most 1000 ids are listed, by id. The installer changes nothing about them. A backdated time that still falls after the request's creation and decision cannot be told from a real one and is not listed. Look at each listed request before you run `purge_payloads`.

7. **SQLite needs a new enough library, and nothing else from you.** A file is upgraded in place on first use: the `payload_purged_at` column is added, the unique index is built, the purge index `agent_core_approvals_purge_due` is built, and `agent_core_approvals_purgeable` is dropped if it exists. While the file holds duplicate open requests the library refuses, with a `ConfigError` that lists the ids, at the first submit. `cancel()` and reads still work, so the extras can be cancelled and the submit retried.

   **SQLite 3.35 or later is required.** The queue reads back what it wrote with `UPDATE ... RETURNING`, which SQLite added in 3.35 (March 2021). `open_database` for a `sqlite:` URL refuses an older SQLite library with a `ConfigError` that names the version found and the minimum. The library is the one your Python is linked against: check `sqlite3.sqlite_version`. A Python build that links an older SQLite has to be replaced with one that links a newer SQLite, or you use Postgres. Postgres is not affected.

8. **Check the lock settings.** A role that can connect can still hold the audit append lock and delay writers, for up to `lock_timeout` (5 seconds by default) for the library's own transactions. Set `idle_in_transaction_session_timeout` on the runtime roles.

9. **A clock more than 5 minutes off.** The guard requires an approver's `resolved_at` to be within 5 minutes of the database clock. The library writes the application's clock (`max(now, created_at)`), so an application host whose clock is more than 5 minutes off the database's has its `resolve` calls refused. Keep both on NTP. The purge is not affected: the guard writes `payload_purged_at` itself, so `purge_payloads` no longer fails when a run lasts more than 5 minutes.

10. **Sweeps skip a request another transaction holds.** On Postgres, `expire_due` and `purge_payloads` skip a request that another transaction holds (`FOR UPDATE SKIP LOCKED`) instead of failing the sweep with `LockNotAvailable` after `lock_timeout`. The next run takes it. SQLite is unchanged.

A request already approved by one of its own delegates stays readable and consumable.

## Project 03: its own backends (0.1.0a6)

If project 03's backends implement `ApprovalQueue`, they must match these rules. Nothing changes for the `AuditLog` protocol.

1. **Add `purge_payloads`.** It is part of the `ApprovalQueue` protocol now.

   ```python
   async def purge_payloads(
       self,
       *,
       principal: Principal,
       older_than: timedelta,
       limit: int = 500,
       connection=None,
   ) -> int: ...
   ```

   It sets the stored payload to nothing, records when, keeps `payload_sha256`, and returns the count. The rules are in the project 04 section.

2. **Make `submit` idempotent.** At most one request is open (pending, or approved and not yet consumed) per requester, action and payload hash, and the rule must hold when calls race, which means the store enforces it, not only the code. An exact repeat (same `required_role`, same lifetime, same `delegates`) returns the existing open request, with its stored payload if one was stored, and writes no audit event. A repeat that differs in `summary`, `required_role`, lifetime or `delegates` raises `ApprovalConflictError`, audited as `approval.submit_conflict` (its payload carries `approval_action`, as the other `approval.*` events do); so does one that asks for the payload to be stored (`include_payload`) when the open request has none. `context` is not compared. An open request past its lifetime is closed as expired in the same transaction and the new one is queued. The protocol docstring states this rule.

3. **Allow `EXPIRED` with `decision` `approve`.** An approval that lapses unused is now `EXPIRED`, and keeps `decision`, `resolved_by` and `resolved_at`. `expire_due` sweeps approved, unconsumed requests too, and `get` reports a pending or approved request past its lifetime as `EXPIRED`. A backend that forbids `approved -> expired` would hold the unique key forever.

4. **Add the delegate rule.** See "The delegate rule" below.

5. **Add `payload_purged_at`.** `ApprovalRequest.payload_purged_at` is `AwareDatetime | None`. A purged request reads `payload` as `None` with `payload_purged_at` set. A request that never stored a payload reads both as `None`.

6. **Handle `ApprovalConflictError`.** It is an `ApprovalError` with `existing: UUID` and `differs`, a sorted tuple that is a subset of `("delegates", "lifetime", "payload", "required_role", "summary")`, or `("row",)` or `("payload",)` for an open request that cannot be read or whose stored payload fails its hash. It keeps its notes (`add_note`) as well as `existing` and `differs` when pickled or copied.

7. **Free text.** `summary`, `reason` and a cancel reason may not contain control (Cc), format (Cf) or line and paragraph separator (Zl, Zp) characters. `SQLApprovalQueue` raises `ValidationError` for `summary` and `ValueError` for `reason`. Text already stored with them is shown with each such character replaced by U+FFFD. Audit and stored approval payloads may not contain NUL in any key or string.

8. **Audit denial.** `DenialReason.DELEGATE_APPROVAL` maps to `NotAuthorizedToResolveError` and is audited as `approval.resolve_denied` with reason `delegate_approval`.

If 03 uses agent-core's `SQLAuditLog` and `Database`, the changes of the next section apply.

## Project 04: the SQL backends (0.1.0a6)

1. **Expect the first request back from a repeated submit.** The same requester, action and payload hash, while a request is open, returns the existing request and writes no audit event. Tests that submit the same payload twice from one requester now get the first request back; change the payload, the action or the requester in a test that needs two requests.

2. **Handle `ApprovalConflictError`.** A repeat with another `required_role`, lifetime or `delegates` raises it. It carries the existing request's id and what differs.

   ```python
   from aox_agent_core.approvals import ApprovalConflictError

   try:
       request = await queue.submit(...)
   except ApprovalConflictError as conflict:
       # conflict.existing is the open request's id
       # conflict.differs is a sorted tuple, for example ("lifetime",)
       ...
   ```

   Two refusals are not an `ApprovalConflictError`. An open request stored with an id that is not a UUID raises `ApprovalIntegrityError` (audited as `approval.submit_conflict` with no `subject_id`). An open request the library cannot parse (reason `malformed_row`) cannot be cancelled through the library: the table owner must close it, see "A stored request the library cannot read" at the end of this page. A request whose stored payload fails its hash can still be cancelled.

   Decide in your code whether to wait for the existing request, cancel it and submit again, or surface the conflict. The conflict is audited as `approval.submit_conflict`, and its audit payload carries `approval_action`. `context` is not compared, so a repeat that differs only in it returns the first request with the first context. A different `summary` is a conflict, which is what stops a row planted for your principal by another holder of the requester role from being adopted with its own summary. Because `requested_by` is written by the requester role, a compromised requester role can occupy another principal's key with a request of other terms. That principal then gets `ApprovalConflictError` naming the request, may cancel it (it is theirs by `requested_by`), and resubmit. This is a documented limit.

3. **Do not read `APPROVED` as "usable".** An approved request past its lifetime now reads as `EXPIRED`, and `expire_due()` closes it. Check `is_expired`, or call `consume()`, which is the real test.

4. **Schedule `purge_payloads`.** Project 04 keeps arguments at most 7 days. Nothing else purges a payload.

   ```python
   from datetime import timedelta

   purged = await approver_queue.purge_payloads(
       principal=service_principal,
       older_than=timedelta(days=7),
       limit=500,
   )
   ```

   - It must run on a queue connected as the approver role: a requester-side queue raises `ConfigError`. Give the scheduled job approver credentials, and keep them apart from the requester's.
   - `older_than` must be at or above the installed floor (24 hours by default), or `ValueError`. 7 days is above it.
   - It purges requests that are consumed, rejected, cancelled or expired and have a stored payload, whose finish time is older than `older_than` by the database's clock (the application's on SQLite). The finish time is `consumed_at`, `resolved_at` (rejected) or `closed_at` (cancelled, expired). Pending and approved requests are never purged.
   - On Postgres it skips a request another transaction holds, and the next run takes it, so a short batch does not always mean the backlog is empty. A run may last more than 5 minutes: the guard stamps `payload_purged_at` from the database clock.
   - It works in batches of `limit`, so run it until it returns less than `limit`. Each purged request gets one `approval.payload_purged` audit event in the same transaction. `payload_sha256` is kept.
   - A purged request reads `payload` as `None` and `payload_purged_at` as set. A UI must handle a request without a payload, and must not read `None` as "never stored" without checking `payload_purged_at`.

5. **Handle `AuditLockTimeoutError`.** `SQLAuditLog(..., lock_timeout=timedelta(seconds=5))`. The library's own append transactions wait for the append lock at most that long, then raise `AuditLockTimeoutError` (sqlstate `55P03`) and write nothing, so the call can be retried. The queue's calls that write audit events can raise it too. In a transaction of yours (`connection=`) the bound is applied for the append, and your own `lock_timeout` is put back afterwards.

6. **Append before you touch approval rows.** In a host transaction, append to the audit log first (or let the library do both), and only then update approval rows. The reverse order deadlocked in a test: a host that appended first and then touched a request's row, against the queue's own transaction that had taken the row first and then the lock, and Postgres aborted one with `40P01`. The queue's own write transactions now take the append lock before they touch the row, so the host's order has to match. With `connection=` the queue does not take the lock early, because the transaction is yours. If you will append later in the same transaction but must update first, call the new `await audit_log.lock_in(session)` to take the lock now. Even then a library-owned call on the same request can meet your `connection=` call in the other order, rarely, and Postgres aborts one side with `40P01` (`psycopg.errors.DeadlockDetected`). **Retry your transaction on `40P01`:** the aborted transaction committed nothing, the queue and audit writes in it are gone with it, and a retry succeeds.

7. **Per-schema lock key.** The advisory lock key is `hashtextextended('agent_core_audit:' || schema, 0)`, computed in SQL by both the library and the trigger. The constants `audit.sql.APPEND_LOCK_KEY` and `_postgres_schema.AUDIT_APPEND_LOCK_KEY` are removed. If your code imported one, compute the key in SQL instead. The a4 note that all schemas share one lock no longer holds.

8. **Free text.** `summary`, `reason` and a cancel reason are refused if they contain control, format (including bidirectional overrides and zero-width characters) or line and paragraph separator characters. Strip them before you call. Payloads may not contain NUL either (`ValueError` "payload text must not contain NUL"; `AuditEvent` construction fails with `ValidationError`).

9. **A clock more than 5 minutes off the database's is refused at `resolve`.** The approver's `resolved_at` must be at or after the request's `created_at` and within 5 minutes of the database clock.

10. **The delegate rule.** See the next section: a `resolve` by a delegate is now refused.

## The delegate rule (0.1.0a6)

This is a behavior change, in its own commit.

- The approver may be neither the requester nor one of the request's delegates. `RoleApproverPolicy` denies a delegate with `DenialReason.DELEGATE_APPROVAL` (`"delegate_approval"`), after the self-approval check. It maps to `NotAuthorizedToResolveError` and is audited as `approval.resolve_denied` with that reason.
- The Postgres guard refuses a `resolved_by` that is in the request's `delegates`, so plain SQL cannot get around the policy.
- `list_pending` no longer offers a request to its own delegate.
- A delegate may still `consume`.
- A request approved under a4 by one of its own delegates stays readable and consumable: the model does not forbid it. Only new decisions are refused.
- A host that has its own approver policy or queue must add the same check. A host whose workflow had a delegate approve the request must name a different approver.

---

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
   - **Other limits.** A separate refusal record carries the audit pool's `db_role`, not your connection's. If the separate commit fails in an ambiguous way, the fallback can leave a second copy of the refusal. Any role that can connect can hold the append lock with an idle transaction and stall every writer: set `idle_in_transaction_session_timeout` on the runtime roles. The lock is one key for the whole database, so all schemas share it. Both changed in 0.1.0a5, which ships in 0.1.0a6: a `lock_timeout` on the library's own append transactions, and a lock key that includes the schema (see the 0.1.0a6 sections above).

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

    `summary` is written by the requester and is not covered by `payload_sha256`. A UI must show `payload` when it is present, and must not decide from `summary` alone. Handle `ApprovalIntegrityError` from `get`, and expect `list_pending` to omit a request whose stored payload does not match its hash. The payload is never copied into the audit log. Nothing purges a stored payload in 0.1.0a4: count it in your retention plan. (0.1.0a6 adds `purge_payloads`.)

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

- **Postgres 16 or later.** An older server is refused with a `ConfigError` at connect and at install. The message reads "This Postgres server is version 15; agent-core <release> needs 16 or later." with the server's version and a release number (the current code names 0.1.0a5 there).
- **Until you re-run the installer, an a3 schema is refused.** The audit log raises a `ConfigError` before it writes anything, and the approvals queue does the same when its table lacks `payload_json`. The audit log's message:

  > Table agent_core_audit was created by an earlier agent-core and has no recorded_at column. As the owner role, run install_postgres_schema from 0.1.0a4 with the requester and approver roles: it upgrades the schema in place and keeps every row.

  Run the installer before starting a4 code against the database.
- **SQLite needs nothing.** An a3 file gets the `recorded_at` and `payload_json` columns, and the append trigger that also checks `prev_hash`, in place on first use.
- **`recorded_at` is the trigger's word, not the chain's.** Like `db_role`, it is outside the hash and guaranteed by the database trigger. The table owner could edit it undetected. On SQLite the library writes it from the writer's clock, so it is not independent there.
- **Old records still verify.** Run `aox-agent-core audit verify` against a saved anchor with the a3 release before the upgrade (a4 cannot read an a3 schema until the installer has run), and with a4 after. A row inserted with a wrong `record_hash` makes `verify` fail from that record on; the message shows the row's `db_role` column, which the hash does not cover. Rows written before a4 have no `recorded_at`.

## What is not verified

Still not verified for 0.1.0a6:

- **A sweep with a small `limit`.** `expire_due` and `purge_payloads` skip requests another transaction holds. If the oldest `limit` due requests are all held, a batch changes nothing and the run stops; the requests behind them wait for the next run.
- **A real pooler.** The design fits a transaction-mode pooler. It has not been run against pgbouncer or any other pooler, and a pgbouncer CI job is planned for after 0.1.0 (it was moved out of 0.1.0a7).
- **Postgres 18 and later.** CI runs 16 on Python 3.11 to 3.14 and 17 on Python 3.12 in a separate job.
- **0.1.0a6 on a large or busy database.** The upgrade was tested on small databases only, not on production-sized ones or with writers active.
- **An a4 database upgraded in place under load.** Plan a window with no writers.
- **a4 code against an a6 schema.** Not tested.
- **A SQLite library older than 3.35.** The refusal in `open_database` was not run against one.
- **SQLite with two event loops on one `Database`.**
- **The full house-standard checklists.** The audit checklists were not run in full.
- **The full a4 to a6 installer path on a database that holds both duplicate open requests and approved requests,** beyond what the tests cover.

Carried over from the a3 to a4 upgrade:

- **Postgres 17 in CI.** The Postgres test files passed against `postgres:17-alpine` on a developer machine. Postgres 18 and later were not tested.
- **This upgrade on a large or busy database.** `tests/test_upgrade_from_a3.py` replays a schema dump made by 0.1.0a3 with four audit rows and one approval, upgrades it, and checks the rows, the chain and a second installer run. It has not been run on a production-sized database, or with writers active.
- **Mixed versions.** An a3 library writing to an a4 schema has not been tested. Plan a short window with no writers: stop the a3 processes, run the installer, start a4.

## A stored request the library cannot read

An open request (pending, or approved and not yet used) holds its requester, action and payload hash: no second one can be queued while it stands. A row the library cannot parse, or one stored with an id that is not a UUID, cannot be used, cancelled or expired through the library, so a `submit` that meets it is refused and audited as `approval.submit_conflict`, and it keeps the key until the table owner closes it. Such a row exists only if it was written before the guard (0.1.0a2) or past it; the guard refuses to insert one.

`submit` names what it found. For a row with a UUID it raises `ApprovalConflictError` with `existing` set and `reason` `malformed_row` in the audit event, and says the owner must close it. For an id that is not a UUID it raises `ApprovalIntegrityError` and the audit event has no `subject_id`. A request whose payload fails its hash is different: it can still be cancelled, which its error says.

To free the key, as the table owner (the guard has to be switched off for the one statement, so this is a person's decision, and it writes no audit event: record it yourself):

```sql
BEGIN;
ALTER TABLE public.agent_core_approvals DISABLE TRIGGER agent_core_approvals_guard;
UPDATE public.agent_core_approvals
   SET status = 'cancelled',
       closed_at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
 WHERE id = '<the id>' AND status IN ('pending', 'approved');
ALTER TABLE public.agent_core_approvals ENABLE TRIGGER agent_core_approvals_guard;
COMMIT;
```

Use your schema in place of `public`. The row stays in the table, unreadable, but no longer open.
