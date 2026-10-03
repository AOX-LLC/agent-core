# Compatibility with project 03

Project 03 (the ops kit) drafted the interfaces it wants from agent-core. This page maps each of those names to what release 0.1.0a6 ships, and says which parts the ops kit keeps in its own adapter.

Every signature below was checked against the source of 0.1.0a4 and against the 0.1.0a5 and 0.1.0a6 change sets. 0.1.0a5 was tagged but never released, so there is no wheel for it: the behaviour described as new in 0.1.0a5 below ships in 0.1.0a6. Where a name differs, the ops kit's factory holds a thin shim. Section "What project 03 must change for 0.1.0a4 to 0.1.0a6" lists what its own backends need to match this release. [upgrading.md](upgrading.md) has the step-by-step upgrades from 0.1.0a4 to 0.1.0a6 and from 0.1.0a3 to 0.1.0a4.

## Name mapping

### Mode

- Project 03: `Mode.MOCK`, `Mode.LIVE`, `Mode.RECORD`.
- agent-core: `aox_agent_core.Mode`.

```python
from aox_agent_core import Mode

Mode.REPLAY  # "replay", project 03's MOCK
Mode.LIVE  # "live"
Mode.RECORD  # "record"
```

- MOCK maps to `Mode.REPLAY`. LIVE and RECORD keep their names and values.
- The mode is client configuration, not part of `RunContext`. Set `mode` in the TOML file named by `AGENT_CORE_CONFIG`, or set `AGENT_CORE_MODE`, which wins over the file.
- `CallResult.mode` reports the mode a call ran in.
- REPLAY is the default, so a fresh checkout never spends money.

### Tier

- Project 03: `Tier`. agent-core: `aox_agent_core.Tier`.

```python
from aox_agent_core import Tier

Tier.SMALL  # "small"
Tier.MID  # "mid"
Tier.LARGE  # "large"
```

- Same three values. The model behind each tier is set in config.

### RunContext

- Project 03: `RunContext(run_id, workflow, mode, external_ids)`.
- agent-core: `aox_agent_core.RunContext`.

```python
from collections.abc import Mapping


class RunContext:
    run_id: str
    external_ids: Mapping[str, str] = {}
```

```python
ctx = RunContext(
    run_id=str(run_uuid),
    external_ids={
        "workflow": "receipts_intake",
        "workflow_id": "wf-123",
        "execution_id": "4812",
    },
)
```

- `run_id` is an opaque string, not a `UUID`. Pass `str(uuid)`.
- There is no `workflow` field. Put it in `external_ids`. The key name is the ops kit's choice.
- There is no `mode` field. See Mode above.
- Ids must match `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$`. The `@` character is not allowed, so an email address cannot pass as an id.
- External id names must match `^[a-z][a-z0-9_]{0,63}$`.
- At most 16 external ids, and at most 2048 bytes of canonical JSON in all.
- Names that look like secrets, and values that look like secrets, are refused.
- The context reaches span attributes (`agent_core.run_id`, `agent_core.external_id.<name>`), audit records (`run_context`, hashed) and approvals. It never enters a replay key.

### PromptRef

- Project 03: `PromptRef(id, version, template)`.
- agent-core: `aox_agent_core.PromptRef`.

```python
class PromptRef:
    id: str  # ^[a-z][a-z0-9_.-]{0,99}$
    version: int  # >= 1
    template: str  # non-empty
    system: str | None = None

    def render(self, inputs: Mapping[str, JsonValue]) -> str: ...
```

- A superset of the draft. `system` is new and optional.
- The template uses `string.Template` placeholders: `${name}`.
- agent-core renders the template. Strings go in as they are. Other values go in as compact JSON with sorted keys. Inputs that are not plain JSON (a `Decimal`, a `datetime`, `NaN`) raise `PromptError`.
- A placeholder with no input raises `PromptError`.

### Attachment

- Project 03: `Attachment(media_type, data)`.
- agent-core: `aox_agent_core.Attachment`, built with a classmethod.

```python
class Attachment:
    media_type: Literal["image/png", "image/jpeg", "application/pdf"]
    sha256: str
    size_bytes: int
    data: bytes | None  # excluded from every dump

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        media_type: AttachmentMediaType | None = None,
        max_bytes: int | None = None,
    ) -> Self: ...

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        *,
        media_type: AttachmentMediaType | None = None,
        max_bytes: int | None = None,
    ) -> Self: ...
```

- The type is sniffed from the first bytes. A declared `media_type` must agree with it.
- Caps: 5 MB per image, 24 MB per PDF. `max_bytes` can only lower the cap.
- Bad input raises `AttachmentError`.
- The bytes are never recorded or hashed. Only media type, SHA-256 and size are.
- agent-core does no image preprocessing. It sends the bytes it is given.

### ModelResult

- Project 03: `ModelResult[T]`. agent-core: `CallResult[T]`.

```python
class CallResult(Generic[OutputT]):
    output: OutputT
    tier: Tier
    task: str | None
    provider: Provider
    model: str
    mode: Mode
    usage: Usage
    cost_usd: Decimal
    latency_ms: float
    stop_reason: str
    trace_id: str | None = None
    attempts: int = 1
    replay_key: str = ""
    prompt_id: str | None = None


class Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
```

| Project 03 | agent-core |
| --- | --- |
| `output` | `output` |
| `input_tokens` | `usage.input_tokens` |
| `output_tokens` | `usage.output_tokens` |
| `cost_usd` | `cost_usd` (`Decimal`, summed over every attempt) |
| `latency_ms` (int) | `latency_ms` (float; round it) |
| `fixture_key` | `replay_key` |
| `mode` | `mode` |

- Extra fields: `tier`, `task`, `provider`, `model`, cache token counts, `stop_reason`, `trace_id`, `attempts`, `prompt_id`.

### ModelClient

- Project 03: `ModelClient.structured(...)`.
- agent-core: `aox_agent_core.ModelClient`, a public `Protocol` with `call`. `AgentClient` implements it.

```python
async def call(
    self,
    prompt: Prompt | PromptRef,
    *,
    inputs: Mapping[str, JsonValue] | None = None,
    attachments: Sequence[Attachment] = (),
    output: type[OutputModelT] | None = None,
    tier: Tier | None = None,
    task: str | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
    max_attempts: int = 2,
    context: RunContext | None = None,
) -> CallResult[OutputModelT]: ...
```

(The source types this as two overloads: `output=None` returns `CallResult[str]`, and `output=SomeModel` returns `CallResult[SomeModel]`.)

The ops kit's `structured` becomes:

```python
result = await client.call(
    prompt,
    inputs=inputs,
    attachments=attachments,
    output=schema,
    tier=tier,
    context=ctx,
)
```

- `schema` must be a Pydantic model class.
- A `PromptRef` call requires `inputs` (it may be `{}`) and forbids `system`. The `PromptRef` carries the system prompt.
- Attachments go on the last user message.
- A response that fails validation is sent again, up to `max_attempts` tries. If `routing.escalate_on_structured_failure` is set, the call then gets `max_attempts` more tries one tier up. Failure then raises `StructuredOutputError`. A refusal raises `ModelRefusalError` at once.
- Name a tier or a task, not both.

### ApprovalQueue

- Project 03: `ApprovalQueue` with `request`, `decide`, `get`, `list_pending`, `expire_due`.
- agent-core: `aox_agent_core.approvals.ApprovalQueue`, a `Protocol`. `SQLApprovalQueue` implements it.

```python
class ApprovalQueue(Protocol):
    async def submit(
        self,
        *,
        action: str,
        summary: str,
        payload: Mapping[str, JsonValue],
        requested_by: Principal,
        required_role: str,
        ttl_seconds: int,
        delegates: Collection[str] = (),
        context: RunContext | None = None,
        include_payload: bool = False,
    ) -> ApprovalRequest: ...

    async def get(self, request_id: UUID) -> ApprovalRequest: ...

    async def list_pending(
        self, principal: Principal, *, limit: int = 100, after: UUID | None = None
    ) -> Sequence[ApprovalRequest]: ...

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest: ...

    async def consume(
        self,
        request_id: UUID,
        *,
        action: str,
        payload: Mapping[str, JsonValue],
        principal: Principal,
        context: RunContext | None = None,
    ) -> ApprovalRequest: ...

    async def cancel(
        self,
        request_id: UUID,
        *,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest: ...

    async def expire_due(
        self, *, principal: Principal, now: datetime | None = None, limit: int = 500
    ) -> int: ...

    async def purge_payloads(
        self,
        *,
        principal: Principal,
        older_than: timedelta,
        limit: int = 500,
        connection=None,
    ) -> int: ...
```

`submit` has been idempotent since the 0.1.0a5 change set (shipped in 0.1.0a6), and the protocol docstring documents the rule (see the `submit` notes below). A host-supplied queue must implement it. `purge_payloads` is also on `SyncApprovalQueue`, which takes no `connection=`.

`request` becomes `submit`:

- `kind` becomes `action`. It must match the action-name pattern: dotted lowercase names such as `receipts.post_bill`.
- `subject` becomes `payload`. Its SHA-256 is always stored. The payload itself is stored only with `include_payload=True` (see below).
- `expires_in` becomes `ttl_seconds=int(expires_in.total_seconds())`. The maximum is 7 days.
- `summary`, `requested_by` and `required_role` are required by agent-core. The ops kit's adapter supplies them.
- `summary` may not contain control (Cc), format (Cf, including bidirectional overrides and zero-width characters) or line and paragraph separator (Zl, Zp) characters: `ValidationError` at submit. The Postgres guard refuses the listed subset on insert. Text already stored with them is shown with each such character replaced by U+FFFD.
- Submit is idempotent (0.1.0a5 change set, shipped in 0.1.0a6). A partial unique index, `agent_core_approvals_one_open`, on `(requested_by, action, payload_sha256)` where `status IN ('pending', 'approved')` allows at most one open request (pending, or approved and not yet consumed) per requester, action and payload hash, on Postgres and SQLite alike, so it holds when calls race. An exact repeat (same `required_role`, same lifetime, meaning `expires_at - created_at == ttl_seconds`, and same `delegates`) returns the existing open request, with its stored payload if one was stored, and writes no audit event. A repeat that differs in `summary`, `required_role`, lifetime or `delegates`, or asks for the payload to be stored when the open request has none, raises `ApprovalConflictError` (an `ApprovalError`) with `existing: UUID` and `differs`, a sorted tuple that is a subset of `("delegates", "lifetime", "payload", "required_role", "summary")`, and is audited as `approval.submit_conflict` (actor is the requester, subject is the existing request's id, `approval_action` and `differs` as a comma-joined string in the payload). An open request stored with an id that is not a UUID raises `ApprovalIntegrityError` instead, audited with no subject. An open request the library cannot parse (`malformed_row`) cannot be cancelled through the library: the table owner must close it (see [upgrading.md](upgrading.md)). `context` is not compared: the first submit's stays. An open request already past its lifetime (the database's clock on Postgres, the application's on SQLite) is closed as expired in the same transaction, audited as `approval.expired` (with `previous_status="approved"` for an approval), and the new request is queued.
- `requested_by` is written by the requester role, so a compromised requester role can occupy another principal's key with a request of other terms. That principal then gets `ApprovalConflictError` naming the request, may cancel it, and resubmit. This is a documented limit.
- `include_payload` is new, default `False`. When `True` the exact payload is stored with the request: at most 8192 bytes of canonical JSON, under the audit log's rules (no secret-shaped keys, integers only, a secret scan with the queue's scrubber). Otherwise `ApprovalPayloadRejectedError`, and nothing is written. `ApprovalRequest.payload` holds it. The payload is never copied into the audit log, and `purge_payloads` (below) is the only thing that removes a stored one.
- `delegates` is new. It names principals, besides the requester, that may consume the approval: at most 16 ids, fixed for the request, recorded in the `approval.requested` audit event and shown on the request the approver sees.
- `resume_url` has no equivalent. See item 7 below.

`decide` becomes `resolve`:

- `decision` takes `Decision.APPROVE` or `Decision.REJECT`.
- `actor` becomes a `Principal`. `note` becomes `reason`.
- `edited_subject` has no equivalent. See item 8 below.
- `RoleApproverPolicy(roles_by_action={...})` on the approver side decides which role each action needs. A request for an unlisted action, or whose stored `required_role` differs from the listed role, is refused (`unknown_action`, `role_mismatch`) and audited. The approver must also be a human who holds that role and is neither the requester nor one of the request's delegates. A delegate is refused with `DenialReason.DELEGATE_APPROVAL` (`"delegate_approval"`, mapped to `NotAuthorizedToResolveError`, audited as `approval.resolve_denied` with that reason), after the self-approval check; the Postgres guard refuses a `resolved_by` in the request's delegates too.
- The default `RoleApproverPolicy()`, with no map, refuses every request. `RoleApproverPolicy(trust_requester_role=True)` takes the requester's `required_role` as given; it is for local development only, never where the requester may be compromised.
- `reason` may not contain control, format or line and paragraph separator characters: `ValueError`, and the guard refuses the listed subset on a decision. The approver's `resolved_at` must be at or after the request's `created_at` and within 5 minutes of the database clock, so an application clock more than 5 minutes off the database's is refused at `resolve`.
- Errors: `ApprovalAlreadyResolvedError` (close to 03's not-pending), `ApprovalExpiredError`, `NotAuthorizedToResolveError`, `ApprovalNotFoundError`.
- Resolution is compare-and-set, so a request is resolved once.
- A stored payload is checked against `payload_sha256` before the policy runs. If it does not match, `resolve` refuses, audits `approval.resolve_denied` with reason `payload_integrity`, and the request stays pending.

`get` keeps its name and takes `request_id`. It raises `ApprovalNotFoundError` when absent, and `ApprovalIntegrityError` when a stored payload does not match its hash.

`list_pending`:

- It takes a `Principal` and returns only what that principal may resolve.
- Order is `created_at`, then id.
- The cursor is the last request's id, passed as `after`. It is a `UUID`, not a string.
- It returns a `Sequence`, not a `Page`.
- It leaves out a request whose stored payload does not match its hash.
- It no longer offers a request to its own delegate.

`expire_due(*, principal, now=None, limit=500) -> int` stores EXPIRED on pending requests, and since the 0.1.0a5 change set on approved, unconsumed requests, past their lifetime and returns how many.

- It works in batches of `limit`, writes one `approval.expired` audit event per request naming `principal`, and sets `closed_at`.
- Either side may run it. On Postgres it picks only what the database's clock agrees is due, and it skips a request another transaction holds (`FOR UPDATE SKIP LOCKED`) instead of failing the sweep after `lock_timeout`: the next run takes it.
- An approved request that lapses unused moves `approved -> expired` (either role, `expires_at` already past by the database clock, `closed_at` set only); otherwise it would hold the unique key forever.
- Reads still judge expiry themselves: `get()` reports a pending or approved request past its lifetime as EXPIRED (with `closed_at` equal to `expires_at`) without writing, so nothing depends on the sweep.
- It takes no `Page` and returns a count, not requests. See item 8 below.

`purge_payloads(*, principal, older_than, limit=500, connection=None) -> int` is new in the 0.1.0a5 change set. It sets `payload_json` to NULL and stamps `payload_purged_at` on requests that are consumed, rejected, cancelled or expired, have a stored payload, and whose finish time (`consumed_at`, `resolved_at` for rejected, `closed_at` for cancelled and expired) is older than `older_than` by the database's clock (the application's on SQLite).

- It works in batches of `limit`, like `expire_due`, and writes one `approval.payload_purged` audit event per purged request (payload: `approval_action`, `payload_sha256`, `request_status`) in the same transaction. `payload_sha256` is never changed.
- On Postgres it skips a request another transaction holds, like `expire_due`, and a run that lasts more than 5 minutes is not refused: the guard stamps `payload_purged_at` from the database clock.
- Only the approver role may purge: `ConfigError` on a requester-side queue. The requester role has no UPDATE on `payload_json` or `payload_purged_at`, and the connect check refuses a requester role that has it.
- The guard allows exactly one change to those columns: `payload_json` non-NULL to NULL together with `payload_purged_at` NULL to a canonical timestamp (the guard replaces it with the database clock), by the approver role, on a finished request whose finish time plus the installed floor is already past. Every other change is refused.
- The installed floor is `install_postgres_schema(..., payload_retention_floor=timedelta(hours=24))`, written into the guard. The library raises `ValueError` for an `older_than` shorter than the floor. SQLite has no floor.
- Nothing else purges a payload.

`cancel(request_id, *, principal, reason=None, context=None)` is new. The requester withdraws a pending request. A delegate may not.

- It stores CANCELLED and sets `closed_at`. It audits `approval.cancelled`, with the reason as `cancel_reason`, or `approval.cancel_denied`.
- It raises `NotTheRequesterError`, `ApprovalAlreadyResolvedError` (no longer pending) or `ApprovalExpiredError`.
- It does not check a stored payload against its hash, so the requester can always withdraw.
- `reason` may not contain control, format or line and paragraph separator characters (`ValueError`).

`consume` is new. Call it right before acting. It is single use, and it checks the action and the payload hash.

- `principal` must be the requester or one of the request's delegates. Otherwise it raises `NotTheRequesterError`, audited as `approval.consume_denied` with reason `not_requester`.
- It also raises `ApprovalPayloadMismatchError`, `ApprovalNotGrantedError`, `ApprovalAlreadyResolvedError` or `ApprovalExpiredError`.

On Postgres, a queue also has a side: `await queue.side()` returns `ApprovalSide.REQUESTER` or `ApprovalSide.APPROVER` (`BOTH` on SQLite). A call from the wrong side raises `ConfigError`. See [upgrade-0.1.0a3.md](upgrade-0.1.0a3.md).

### Approval and Decision

- Project 03: `Approval`. agent-core: `aox_agent_core.approvals.ApprovalRequest`.
- Project 03: `Decision`. agent-core: `aox_agent_core.approvals.Decision`, with values `"approve"` and `"reject"`.

```python
class ApprovalRequest:
    id: UUID
    action: str
    summary: str
    payload_sha256: str
    requested_by: str
    required_role: str
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus  # pending, approved, rejected, consumed, expired, cancelled
    decision: Decision | None
    resolved_by: str | None
    resolved_at: datetime | None
    consumed_at: datetime | None
    closed_at: datetime | None  # set exactly when EXPIRED or CANCELLED
    reason: str | None
    run_context: RunContext | None
    delegates: frozenset[str] = frozenset()
    payload: dict[str, JsonValue] | None = None  # set only with include_payload=True
    payload_purged_at: AwareDatetime | None = None  # set when purge_payloads removed the payload


class Principal:
    id: str
    kind: PrincipalKind  # HUMAN, SERVICE, AGENT
    roles: frozenset[str] = frozenset()
```

- `payload` is the exact payload `payload_sha256` binds, present only when the requester stored it. `summary` is written by the requester and is not bound by the hash, so a UI must show `payload` when it is present and must not decide from `summary` alone.
- `payload_purged_at` is new in the 0.1.0a5 change set. A purged request reads `payload` as `None` with `payload_purged_at` set; a request that never stored a payload reads both as `None`.
- `EXPIRED` may carry `decision` `approve` (0.1.0a5 change set): an approval that lapsed unused keeps `decision`, `resolved_by` and `resolved_at`. A host that tests `status is APPROVED` to mean "usable" should also check `is_expired`, or call `consume()`.
- `ApprovalStatus` also has `EXPIRED` and `CANCELLED`. They are stored states in this release: `expire_due` and `cancel` set them, and both set `closed_at`. `ApprovalRequest` refuses `closed_at` on any other status, and refuses a missing one on these two.

### AuditLog

- Project 03: `AuditLog.append(ctx, actor, action, subject_type, subject_id, details)`.
- agent-core: `aox_agent_core.audit.AuditLog`, a `Protocol`.

```python
async def append(self, event: AuditEvent) -> AuditRecord: ...


async def append_many(self, events: Sequence[AuditEvent]) -> list[AuditRecord]: ...


class AuditEvent:
    action: str  # dotted lowercase, max 100 chars
    actor_id: str
    subject_id: str | None = None
    payload: dict[str, JsonValue] = {}
    context: RunContext | None = None
    occurred_at: AwareDatetime | None = None


class AuditRecord:  # set by the store, outside the hash
    db_role: str | None = None
    recorded_at: AwareDatetime | None = None
```

```python
record = await audit.append(
    AuditEvent(
        action="receipts.bill_posted",
        actor_id=actor,
        subject_id=subject_id,
        payload={"subject_type": "bill", **details},
        context=ctx,
    )
)
```

- `append_many` is new. It writes every event as consecutive records under one lock and one commit, all or nothing, and returns the records in order. `SQLAuditLog` takes at most `MAX_APPEND_BATCH` (1000) events.
- `occurred_at` is new. `None` means the database's clock on Postgres (the writer's clock on SQLite). A value must lie within `OCCURRED_AT_MAX_PAST` (24 hours) before and `OCCURRED_AT_MAX_FUTURE` (5 minutes) after the database's clock, or `AuditTimeRejectedError` is raised. The Postgres insert trigger checks it again. It is in the hash.
- There is no `subject_type` field. Put it in the payload. The name `subject_type` is not secret-shaped, so it passes.
- `action` must match `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`.
- Payload numbers must be integers. Write decimals as strings, for example `"0.0123"`.
- At most 8192 bytes of payload.
- Keys that end in a secret word (token, secret, password, and so on) are refused. Strings are scanned for secrets.
- Records carry audit schema version 3. Version 2 records in an upgraded chain keep version 2 and still verify.
- `AuditRecord.db_role` is new: the database role that inserted the row. On Postgres the database's insert trigger sets it, whatever was sent, and it is fixed afterwards. It is outside the hash. It is `None` on SQLite. Both roles may append audit rows and `actor_id` is supplied by the library, so `db_role` shows who really wrote a record.
- `AuditRecord.recorded_at` is new: when the database wrote the row. On Postgres the insert trigger sets it, whatever was sent. It is outside the hash, so the trigger, not the chain, guarantees it: an owner could edit it undetected. On SQLite the library writes it from the writer's clock, so it is not independent there. It is `None` on records written before 0.1.0a4.
- The hash is unchanged: `occurred_at` is in it, `recorded_at` is not, the schema version stays 3, and old records verify as before.
- `SQLAuditLog(database, *, scrubber=None, schema=None, lock_timeout=timedelta(seconds=5))` and `SQLApprovalQueue(..., schema=None)` take a Postgres schema.
- `lock_timeout` (0.1.0a5 change set): the library's own append transactions wait for the append lock at most that long, then raise `AuditLockTimeoutError` (an `AuditError`, sqlstate `55P03`) and write nothing. In a host's transaction (`connection=`) the bound is applied for the append and the host's own `lock_timeout` is put back. The lock key is per schema (`hashtextextended('agent_core_audit:' || schema, 0)`), computed in SQL by the library and the insert trigger. `SQLAuditLog.lock_in(session)` takes the lock now, for a caller that will append later in the same transaction.
- Lock order: a host that appends first and then touches a request's row through `connection=` can deadlock (`40P01`) against a queue transaction that took the row first. The queue's own write transactions now take the append lock before the row. A host should append, or let the library do both, before it updates approval rows in its own transaction, and should retry its transaction on `40P01` (`psycopg.errors.DeadlockDetected`), since the order cannot be fixed from inside the library when `connection=` is used.
- Payload text (audit payloads and stored approval payloads) may not contain NUL in any key or string (`ValueError`; `AuditEvent` construction fails with `ValidationError`).
- The insert trigger carries `-- agent-core audit trigger revision 5` and the approvals guard `-- agent-core guard revision 6`. A connection refuses an older guard or trigger with a `ConfigError` that says to run `install_postgres_schema` from 0.1.0a6. The guard stamps `closed_at`, `consumed_at`, a rejection's `resolved_at` and `payload_purged_at` from the database clock, whatever the statement carried.
- `aox-agent-core audit verify URL --schema NAME` checks a Postgres schema.
- The protocol also has `iter_records`, `head` and `verify`. The log is hash-chained.

### Core

- `Core` stays the ops kit's own dataclass. agent-core has no equivalent.

## Replay keys

`aox_agent_core.replay.replay_key` gives the key a prompted call is recorded and replayed under.

```python
def replay_key(
    prompt: PromptRef,
    *,
    tier: Tier,
    output_schema: str | None,
    inputs: Mapping[str, JsonValue],
    attachments: Sequence[Attachment] = (),
    attempt: int = 1,
) -> str: ...
```

- The key is a SHA-256 over key version 2, prompt id and version, routed tier, output schema class name, NFC-normalized inputs, attachment SHA-256s and the attempt number.
- The model ID, call order, run ids and timestamps never enter the key.
- `CallResult.replay_key` is this key for the last attempt.
- Fixture root: `[replay] cassette_dir` in the project's agent-core TOML. `AGENT_CORE_CONFIG` names the file. A relative path resolves against the config file's folder.
- Recordings live at `<cassette_dir>/prompts/<prompt_id>/v<version>/<key>.json`.
- A miss raises `ReplayMissError` with `.key` and `.path`. Replay never goes live. If a recording exists that differs only in tier, the message says the routed tier differs.
- Editing a template, system prompt or output schema without bumping the version raises `StaleRecordingError`.
- The tier in the key is the tier after routing. A price-table change that flips a budget drop to a lower tier changes the key.
- A structured retry is attempt 2, with its own key.
- Replaying a recording made on another model returns that recording's model and prices it at that model's rate, on the provider it was recorded with. If the API answered with an unpriced name such as an alias, the recorded request's model's rate applies. `ConfigError` if neither is priced.

## Host-supplied backends

- `ApprovalQueue` and `AuditLog` are `Protocol`s. The ops kit's Postgres implementations can stand in wherever code is typed against them.
- A host can pass its own log to the SQL queue: `SQLApprovalQueue(database, *, audit_log, policy=None, clock=None, schema=None)`.
- `tests/test_host_backends.py` proves both directions, with in-memory classes checked by mypy against both protocols.
- A host `AuditLog` must implement all five methods: `append`, `append_many`, `iter_records`, `head`, `verify`. `append_many` is new in 0.1.0a4; a host log without it no longer matches the protocol.
- A host `ApprovalQueue.submit` must accept `include_payload`.
- A host `ApprovalQueue` must add `purge_payloads`, implement the idempotent-submit rule and `ApprovalConflictError`, allow `EXPIRED` with decision `approve`, and refuse a delegate as approver (0.1.0a6).
- With an `AuditLog` that is not `SQLAuditLog`, the queue writes its audit events right after its own commit, as best effort. They are not in the same transaction as the approval change. With `connection=` the queue refuses such a log (`ConfigError`): it must share the queue's `Database`.

## What stays in project 03's adapter

| Item | Stays in 03? | What agent-core offers |
| --- | --- | --- |
| 5. Postgres `ApprovalQueue` and `AuditLog` on a caller-provided async session, configurable schema | Partly | Both backends are async. `PostgresDatabase(url, max_connections=10)` owns a `psycopg_pool.AsyncConnectionPool`, opened on first use on the running loop and closed with `await database.aclose()`. `PostgresDatabase.from_pool(pool)` borrows a host's pool and never closes it. To run inside a transaction the caller controls, pass `connection=` to `SQLAuditLog.append` and `append_many` and to the queue's `submit`, `get`, `list_pending`, `resolve`, `consume`, `cancel`, `expire_due` and `side`. It must be a psycopg `AsyncConnection`, already in a READ COMMITTED transaction. agent-core works in a savepoint and never commits, rolls back or closes it. 03 asked for a "caller-provided session"; a psycopg `AsyncConnection` is all that is offered, with no SQLAlchemy session. Table names are fixed, the schema is configurable (`schema=`). The hazards of `connection=` are in [upgrading.md](upgrading.md). If 03's session is not a psycopg connection, it implements the protocols itself. |
| 6. Host-run migrations without a superuser, grant-friendly roles, database-enforced append-only audit | Yes, for 03's own backends | `storage.install_postgres_schema(owner_url, *, requester_role, approver_role, schema="public", close_duplicates=False, payload_retention_floor=timedelta(hours=24))` runs as the table owner, not a superuser. It is idempotent and schema-qualified, and needs Postgres 16 or later. `close_duplicates` and the report's `closed_duplicates` handle a database that holds duplicate open requests (see [upgrading.md](upgrading.md)). Its audit table has update, delete and truncate triggers, and an insert trigger that takes the append lock (keyed per schema since the 0.1.0a5 change set), requires the next `seq` and the head's hash as `prev_hash`, checks the `occurred_at` bounds, and sets `db_role` and `recorded_at`. Its approvals table has a guard trigger with a role-separated design: a requester role and an approver role, and a fixed transition table. Use it as a reference design. It installs only agent-core's own tables, not 03's. |
| 7. External resume targets, outbox, at-least-once dispatcher | Yes | Nothing. |
| 8. Decision semantics | Partly | In agent-core: compare-and-set `resolve` with the errors above, keyset paging through `after`, the actor recorded as the principal, `expire_due(*, principal, now=None, limit=500) -> int` and `purge_payloads`. In 03: `edited_subject`, a string cursor and the `Page` type. |
| 9. Image preprocessing | Yes | Nothing, by design. agent-core sends the bytes it is given. |
| 10. "Honest nulls" | Yes | Nothing. |
| 11. Atomic record mode, lister of missing or stale fixtures | Partly | Each recording is written atomically: a temp file, then a rename, one file per exchange. `aox-agent-core cassettes check DIR` reports unreadable files (including format 1), misplaced files, key mismatches (tampering) and secret-bearing files. `StaleRecordingError` fires at replay. In 03: all-or-nothing recording of a whole run, and listing missing or stale fixtures before a run. Build the lister from `replay_key`, `PromptKey.for_call`, `DirectoryRecordingStore.prompt_path` and `PromptKey.stale_parts`. |
| 12. Tag timing | Yes | Pin `v0.1.0a6`. Alpha 5 was tagged but never released. Alpha 4 lacks idempotent submit, `ApprovalConflictError`, `purge_payloads`, `approved -> expired`, the delegate rule and `lock_timeout`. Alpha 3 lacks `append_many`, async storage, `connection=`, `include_payload` and `recorded_at`. Alpha 2 also lacks the role-separated approvals, `cancel` and `expire_due`. Install with `aox-agent-core @ git+https://github.com/AOX-LLC/agent-core@v0.1.0a6`. Name shims stay in 03's factory. |

## What project 03 must change for 0.1.0a4 to 0.1.0a6

0.1.0a5 was tagged but never released; its changes ship in 0.1.0a6.

If project 03 implements `ApprovalQueue` and `AuditLog` itself, as item 5 says, its backends must match the protocols of this release. [upgrading.md](upgrading.md) has the checklists with before and after code.

New in 0.1.0a6 (written in the 0.1.0a5 change set):

- Add `purge_payloads(*, principal, older_than, limit=500, connection=None) -> int` to the queue: finished requests only, finish time older than `older_than` by the store's clock, one `approval.payload_purged` audit event per request, `payload_sha256` kept, approver side only.
- Make `submit` idempotent per requester, action and payload hash while a request is open, enforced by the store so it holds when calls race: an exact repeat returns the open request and writes no audit event; a repeat with another `required_role`, lifetime or `delegates` raises `ApprovalConflictError(existing, differs)` and is audited as `approval.submit_conflict`.
- Allow `EXPIRED` with `decision` `approve`, move an unused approval to `EXPIRED` once its lifetime has passed (`expire_due` and `get` both), and set `closed_at` only.
- Add `ApprovalRequest.payload_purged_at`: set on a purged request, `None` otherwise.
- Refuse an approver who is the requester or one of the request's delegates (`DenialReason.DELEGATE_APPROVAL`, `NotAuthorizedToResolveError`, audited as `approval.resolve_denied`), and leave a delegate out of that request's `list_pending`. A delegate may still `consume`.
- Bound the approver's `resolved_at`: at or after `created_at` and within 5 minutes of the store's clock.
- Stamp `payload_purged_at` and a rejection's `resolved_at` from the store's own clock, as `closed_at` and `consumed_at` already are, and skip, not fail on, a request another transaction holds in `expire_due` and `purge_payloads`.
- Record `approval_action` in the `approval.submit_conflict` audit payload.
- Refuse control, format and line and paragraph separator characters in `summary`, `reason` and a cancel reason, and NUL in payload text.
- If it uses `SQLAuditLog`: `lock_timeout` and `AuditLockTimeoutError`, the per-schema lock key, and the append-before-row order in a host transaction. If it uses agent-core's installer, re-run it as the owner from 0.1.0a6: an a6 queue refuses an a4 schema (guard revision 6, trigger revision 5). Whether a4 code runs against an a6 schema was not tested.

New in 0.1.0a4:

- Add `append_many(events: Sequence[AuditEvent]) -> list[AuditRecord]` to the audit log: all or nothing, one lock, one commit, records in order.
- Accept `submit(..., include_payload=False)`. If it stores a payload, keep to 8192 bytes of canonical JSON under the audit log's rules, set `ApprovalRequest.payload`, and check it against `payload_sha256` on every read: `get` raises `ApprovalIntegrityError`, `list_pending` omits the request, and `resolve` refuses and audits `approval.resolve_denied` with reason `payload_integrity`. `cancel` and `consume` do not check.
- Honor `AuditEvent.occurred_at`, and refuse a value more than 24 hours before or 5 minutes after the store's clock with `AuditTimeRejectedError`.
- Set `AuditRecord.recorded_at` from the store's own clock, outside the hash. `None` is allowed.
- Leave the hash alone. `compute_record_hash` and schema version 3 are unchanged.
- If it uses agent-core's installer, the signature is the same. The server must be Postgres 16 or later, and the installer must be re-run as the owner, because a4 code does not work against an a3 schema.
- If it uses `Database` or `Session` directly: `Database.run` awaits an `async def work(session)`, `Session.execute` and `execute_count` are coroutines, and `Database.run_sync` is gone.

Still required from 0.1.0a3, and unchanged:

- `cancel(request_id, *, principal, reason=None, context=None)` and `expire_due(*, principal, now=None, limit=500) -> int`, with these exact signatures.
- `submit(..., delegates=...)`, at most 16 ids, kept unchanged for the life of the request.
- Refuse `consume` by a principal who is neither the requester nor a delegate, with `NotTheRequesterError`.
- Decide the required role on the approver side, as `RoleApproverPolicy(roles_by_action=...)` does: the requester writes `required_role`, so trusting it lets a compromised requester ask for the weakest role. If the ops kit keeps its own approver policy, it should refuse unlisted actions and mismatched roles the same way.
- Set `closed_at` when storing EXPIRED or CANCELLED. `ApprovalRequest` validates it.
- If it builds records with agent-core's types, expect `AuditRecord.db_role` and audit schema 3.
- If it uses agent-core's installer, call it with `requester_role` and `approver_role`. `app_role` is gone.

## Other requirements

- The API key comes only from `AGENT_CORE_ANTHROPIC_API_KEY` or an explicit argument. `ANTHROPIC_API_KEY` is not read.
- Replay mode, the default, needs no key and never contacts a provider.
- Tiers, prices and routing live in config, not in code.
- The package ships `py.typed`.
- Python 3.11 to 3.14 is supported, so 3.12 is covered.
- SQLite 3.35 or later, if you use the SQLite backend: the queue uses `UPDATE ... RETURNING`, and `open_database` refuses an older SQLite library with a `ConfigError`. Check `sqlite3.sqlite_version`. Postgres 16 or later for Postgres.
- Importing the package starts nothing and opens no connections or databases. It reads only its own installed version (`importlib.metadata`). Configuration is loaded when a client is built, or when `load_config()` is called.
