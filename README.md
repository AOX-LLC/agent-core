# agent-core

agent-core is a small Python library for routed Claude model calls, structured outputs, tracing with cost, human approvals, an append-only audit log, and evals. Models are chosen by cost tier. A record/replay mode lets projects run with no API key. Other projects install it by git tag.

## Status

`v0.1.0a2`, the second pre-release. What works:

- routed Claude calls with cost-based tiers, structured outputs, cost and OpenTelemetry tracing;
- versioned prompts (`PromptRef`) and PNG, JPEG and PDF attachments;
- record and replay keyed by content, so projects run and test with no API key; recordings in this repository were made against the live API;
- a `RunContext` that ties calls, audit records and approvals to the host's run;
- an append-only, hash-chained audit log on SQLite or Postgres;
- a human-approval queue with a role policy and single-use approvals;
- an eval runner with JSON and Markdown scorecards.

Not yet: the Bedrock provider is an interface only. The API may still change before `v0.1.0`.

## Quick start

```python
from aox_agent_core import AgentClient, Tier

with AgentClient() as client:
    result = client.call_sync("Summarize this ticket: ...", tier=Tier.SMALL)
    print(result.output, result.cost_usd, result.trace_id)
```

In async code use `await client.call(...)`; `call_sync` raises if an event loop is already running. Pass `output=SomePydanticModel` for validated structured output, and `task="extraction"` instead of a tier once `routing.tasks` maps it.

A versioned prompt with an attachment and a run context:

```python
from aox_agent_core import Attachment, PromptRef, RunContext

EXTRACT = PromptRef(
    id="invoices.extract",
    version=1,
    system="You extract fields from supplier invoices.",
    template="Extract the invoice fields. Amounts are in ${currency}.",
)

result = await client.call(
    EXTRACT,
    inputs={"currency": "USD"},
    attachments=[Attachment.from_path("invoice.pdf")],
    output=InvoiceFields,
    tier=Tier.SMALL,
    context=RunContext(run_id=str(run.id), external_ids={"execution_id": "4812"}),
)
```

An attachment's type is read from its first bytes, never from its name, and is capped at 5 MB per image and 24 MB per PDF. The per-call budget counts a PDF's pages from its bytes, which is best effort against a file built to hide them; when PDFs come from untrusted users, set `routing.count_pdf_pages = false` to budget every PDF at the API's 100-page limit, and build attachments from untrusted bytes with `asyncio.to_thread`, since counting can take up to about a second. Images are sent as they are; the library does no resizing or rotation. Type a dependency as `ModelClient`, the protocol `AgentClient` implements, so a test double can stand in.

`examples/routed_call.py` reads a synthetic invoice PDF in replay mode and prints its trace, fields and cost: `uv run --extra otel python examples/routed_call.py`. Add `--attachment other.pdf` to see a replay miss.

## Install

The distribution is named `aox-agent-core`. Replace `vX.Y.Z` with a release tag. The examples use `v0.1.0a2`.

```sh
pip install "aox-agent-core @ git+https://github.com/AOX-LLC/agent-core@v0.1.0a2"
```

With extras:

```sh
pip install "aox-agent-core[postgres,otel] @ git+https://github.com/AOX-LLC/agent-core@v0.1.0a2"
```

With uv:

```sh
uv add "aox-agent-core @ git+https://github.com/AOX-LLC/agent-core" --tag v0.1.0a2
```

| Extra      | Adds                                         |
| ---------- | -------------------------------------------- |
| `bedrock`  | Amazon Bedrock support for the Anthropic SDK |
| `postgres` | Postgres backend for the audit log           |
| `otel`     | OpenTelemetry SDK and OTLP HTTP exporter     |
| `testing`  | pytest, for the test helpers                 |

## Configuration

Set the mode with `AGENT_CORE_MODE`. It is one of `replay`, `record` or `live`. The default is `replay`, so a fresh clone never spends money.

Set `AGENT_CORE_CONFIG` to the path of a TOML file. It is merged over the packaged defaults. You only restate what you change.

The API key comes only from an explicit `api_key` argument or `AGENT_CORE_ANTHROPIC_API_KEY`. The library never reads `ANTHROPIC_API_KEY`.

The library passes the key and base URL to the Anthropic SDK explicitly, so the SDK's own `ANTHROPIC_*` variables are never used; live and record modes refuse to start while `ANTHROPIC_CUSTOM_HEADERS` is set. The SDK's `ANTHROPIC_LOG=debug` logs whole requests, prompts included, so leave it unset outside local debugging.

Set the audit log's database with `AGENT_CORE_AUDIT_DATABASE_URL`. A URL with a password is refused in a config file, so it never gets committed.

This example moves the large tier to Sonnet and maps a task to a tier:

```toml
[routing.tiers.large]
model = "claude-sonnet-5-5"

[routing.tasks]
extraction = "small"
```

## Record and replay

In `replay` mode every call is served from a recording under `replay.cassette_dir`, so a project runs and tests with no API key. To record, set `AGENT_CORE_MODE=record` and `AGENT_CORE_ANTHROPIC_API_KEY`, then run the code once; commit the files it writes. Recording refuses to write anything that looks like a key, and the live key itself is always caught. Each exchange is its own file, written atomically, so re-recording touches only the calls that ran.

A `PromptRef` call is keyed by its content: `replay_key()` in `aox_agent_core.replay` hashes the prompt ID and version, the tier the call was routed to, the output schema's name, the inputs (Unicode NFC), each attachment's SHA-256, and the attempt number. The model ID, call order, run IDs and timestamps never enter the key. The recording lives at `prompts/<prompt id>/v<version>/<key>.json`; attachments are stored as hash, type and size, never as bytes.

- A miss raises `ReplayMissError`, whose `key` and `path` name the file it looked for. It never falls back to a live call.
- Editing a template, system prompt or output schema without bumping the prompt's version raises `StaleRecordingError`.
- The key uses the tier after routing. A price-table change can make a budget drop pick a lower tier, which changes the key; the miss then says the routed tier differs from the recorded one.
- A structured-output retry is attempt 2 and has its own recording.
- Prompted recordings do not belong to a cassette: `replay.cassette` and `use_cassette(name)` separate only unprompted ones, so identical prompted calls in two tests share one file.
- A recording made on another model replays as that model and is priced at that model's rate on the provider it was recorded with. If the API answered with a name the price table lacks, such as an alias, the rate of the model the recorded request named applies. If neither has a price any more, replay raises `ConfigError`.

A call without a `PromptRef` is keyed by the hash of the whole request plus how many times that request was made before, under `requests/<cassette>/`. That hash covers the JSON schema the Anthropic SDK generates for an output model, so upgrading `anthropic` or `pydantic` can change it, and replay then misses. A prompted key holds only the schema's name, but its recording stores a hash of the generated schema, so the same upgrade raises `StaleRecordingError` on structured prompted recordings; record them again.

Recordings are format 2. Format 1 cassettes from `v0.1.0a1` are refused with a message saying to record them again.

Check cassettes in CI with:

```sh
aox-agent-core cassettes check replays/
```

In tests, install the `testing` extra and enable the fixtures from a `conftest.py`:

```python
pytest_plugins = ["aox_agent_core.testing.pytest_plugin"]


def test_triage(use_cassette):
    client = use_cassette("triage-urgent")
    ...
```

## Audit log, approvals and evals

```python
from aox_agent_core.storage import open_database
from aox_agent_core.audit import SQLAuditLog
from aox_agent_core.approvals import SQLApprovalQueue

database = open_database("sqlite:///control.sqlite3")
audit_log = SQLAuditLog(database)
approvals = SQLApprovalQueue(database, audit_log=audit_log)
```

Keep the head from `await audit_log.head()` somewhere the application cannot write, and check against it with `await audit_log.verify(expected_head=...)` or `aox-agent-core audit verify`: the chain on its own cannot show that it was not rewritten or cut short.

Pass `context=RunContext(...)` to `AuditEvent`, `submit`, `resolve` and `consume`: the run is stored with the record and covered by its hash (audit schema 2). The library never alters an existing table, so tables created by `v0.1.0a1` are refused with a `ConfigError`; keep that database to check its records with `v0.1.0a1`, and point this version at a new one. `ApprovalQueue` and `AuditLog` are protocols, so a host can supply its own backends, and `SQLApprovalQueue` accepts any `AuditLog`.

Approvals are enforced by the library and recorded in the audit log; the database alone does not protect them, since the application role may update the approvals table. Each approval authorizes one run: call `consume(..., principal=...)` right before acting.

On Postgres, an operator creates the tables once as the owner role with `storage.install_postgres_schema(owner_url, app_role="...")`; the application then connects as the app role, which may only insert into and read the audit log. `examples/control_layer_demo.py` walks through both, and `evals/run_triage_eval.py` runs the synthetic eval suite and prints its scorecard.

## Bedrock

The Bedrock provider is an interface only in this release. Setting a tier's `provider = "bedrock"` without a `model` picks that tier's model from `bedrock.tier_models`, priced under `pricing.bedrock`: the mid and large tiers have defaults. The small tier has none: Claude Haiku 4.5 reaches end of life on Bedrock no sooner than 2026-10-16, so moving the small tier to Bedrock without naming and pricing a model is a configuration error.

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
docker compose up -d --wait postgres   # optional: the Postgres tests
AGENT_CORE_TEST_POSTGRES_ADMIN_URL=postgresql://postgres@127.0.0.1:4202/postgres uv run pytest
uvx pre-commit install
```

## Releases

Releases are git tags: `vX.Y.Z` for releases and `vX.Y.ZaN` for pre-releases. See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
