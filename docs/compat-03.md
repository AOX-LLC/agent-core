# Compatibility with project 03

Project 03 (the ops kit) drafted the interfaces it wants from agent-core. This page maps each of those names to what release 0.1.0a2 ships, and says which parts the ops kit keeps in its own adapter.

Every signature below was checked against the source of 0.1.0a2. Where a name differs, the ops kit's factory holds a thin shim.

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
- agent-core renders the template. Strings go in as they are. Other values go in as compact JSON.
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
        context: RunContext | None = None,
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
```

`request` becomes `submit`:

- `kind` becomes `action`. It must match the action-name pattern: dotted lowercase names such as `receipts.post_bill`.
- `subject` becomes `payload`. Only its SHA-256 is stored, not the payload.
- `expires_in` becomes `ttl_seconds=int(expires_in.total_seconds())`. The maximum is 7 days.
- `summary`, `requested_by` and `required_role` are required by agent-core. The ops kit's adapter supplies them.
- `resume_url` has no equivalent. See item 7 below.

`decide` becomes `resolve`:

- `decision` takes `Decision.APPROVE` or `Decision.REJECT`.
- `actor` becomes a `Principal`. `note` becomes `reason`.
- `edited_subject` has no equivalent. See item 8 below.
- The default `RoleApproverPolicy` needs a human who holds `required_role` and is not the requester.
- Errors: `ApprovalAlreadyResolvedError` (close to 03's not-pending), `ApprovalExpiredError`, `NotAuthorizedToResolveError`, `ApprovalNotFoundError`.
- Resolution is compare-and-set, so a request is resolved once.

`get` keeps its name and takes `request_id`. It raises `ApprovalNotFoundError` when absent.

`list_pending`:

- It takes a `Principal` and returns only what that principal may resolve.
- Order is `created_at`, then id.
- The cursor is the last request's id, passed as `after`. It is a `UUID`, not a string.
- It returns a `Sequence`, not a `Page`.

`expire_due` has no equivalent. Expiry is judged from `expires_at` whenever a request is resolved or consumed.

`consume` is new. Call it right before acting. It is single use, and it checks the action and the payload hash. It raises `ApprovalPayloadMismatchError`, `ApprovalNotGrantedError`, `ApprovalAlreadyResolvedError` or `ApprovalExpiredError`.

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
    status: ApprovalStatus  # pending, approved, rejected, consumed
    decision: Decision | None
    resolved_by: str | None
    resolved_at: datetime | None
    consumed_at: datetime | None
    reason: str | None
    run_context: RunContext | None


class Principal:
    id: str
    kind: PrincipalKind  # HUMAN, SERVICE, AGENT
    roles: frozenset[str] = frozenset()
```

- `ApprovalStatus` also has `EXPIRED` and `CANCELLED`. This release never sets them.

### AuditLog

- Project 03: `AuditLog.append(ctx, actor, action, subject_type, subject_id, details)`.
- agent-core: `aox_agent_core.audit.AuditLog`, a `Protocol`.

```python
async def append(self, event: AuditEvent) -> AuditRecord: ...


class AuditEvent:
    action: str  # dotted lowercase, max 100 chars
    actor_id: str
    subject_id: str | None = None
    payload: dict[str, JsonValue] = {}
    context: RunContext | None = None
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

- There is no `subject_type` field. Put it in the payload. The name `subject_type` is not secret-shaped, so it passes.
- `action` must match `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`.
- Payload numbers must be integers. Write decimals as strings, for example `"0.0123"`.
- At most 8192 bytes of payload.
- Keys that end in a secret word (token, secret, password, and so on) are refused. Strings are scanned for secrets.
- Records carry audit schema version 2.
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
- A host can pass its own log to the SQL queue: `SQLApprovalQueue(database, *, audit_log, policy=None, clock=None)`.
- `tests/test_host_backends.py` proves both directions, with in-memory classes checked by mypy against both protocols.
- A host `AuditLog` must implement all four methods: `append`, `iter_records`, `head`, `verify`.
- With an `AuditLog` that is not `SQLAuditLog`, the queue writes its audit events right after its own commit, as best effort. They are not in the same transaction as the approval change.

## What stays in project 03's adapter

| Item | Stays in 03? | What agent-core offers |
| --- | --- | --- |
| 5. Postgres `ApprovalQueue` and `AuditLog` on a caller-provided async session, configurable schema | Yes | The SQL backends open their own connection per operation, on sync drivers in a worker thread, with fixed table names. Project 03 implements the protocols itself. |
| 6. Host-run migrations without a superuser, grant-friendly roles, database-enforced append-only audit | Yes, for 03's own backends | `storage.install_postgres_schema(owner_url, app_role=...)` runs as the table owner, not a superuser. Its audit table has update, delete and truncate triggers, and the app role gets insert and select only. Use it as a reference design. It does not install into 03's schema. |
| 7. External resume targets, outbox, at-least-once dispatcher | Yes | Nothing. |
| 8. Decision semantics | Partly | In agent-core: compare-and-set `resolve` with the errors above, keyset paging through `after`, and the actor recorded as the principal. In 03: the `expire_due` sweep, `edited_subject`, a string cursor and the `Page` type. |
| 9. Image preprocessing | Yes | Nothing, by design. agent-core sends the bytes it is given. |
| 10. "Honest nulls" | Yes | Nothing. |
| 11. Atomic record mode, lister of missing or stale fixtures | Partly | Each recording is written atomically: a temp file, then a rename, one file per exchange. `aox-agent-core cassettes check DIR` reports unreadable files (including format 1), misplaced files, key mismatches (tampering) and secret-bearing files. `StaleRecordingError` fires at replay. In 03: all-or-nothing recording of a whole run, and listing missing or stale fixtures before a run. Build the lister from `replay_key`, `PromptKey.for_call`, `DirectoryRecordingStore.prompt_path` and `PromptKey.stale_parts`. |
| 12. Tag timing | Yes | Pin `v0.1.0a2`. Alpha 1 lacks these features. Install with `aox-agent-core @ git+https://github.com/AOX-LLC/agent-core@v0.1.0a2`. Name shims stay in 03's factory. |

## Other requirements

- The API key comes only from `AGENT_CORE_ANTHROPIC_API_KEY` or an explicit argument. `ANTHROPIC_API_KEY` is not read.
- Replay mode, the default, needs no key and never contacts a provider.
- Tiers, prices and routing live in config, not in code.
- The package ships `py.typed`.
- Python 3.11 to 3.14 is supported, so 3.12 is covered.
- Importing the package starts nothing and opens no connections or databases. It reads only its own installed version (`importlib.metadata`). Configuration is loaded when a client is built, or when `load_config()` is called.
