# agent-core

agent-core is a small Python library for routed Claude model calls, structured outputs, tracing with cost, human approvals, an append-only audit log, and evals. Models are chosen by cost tier. A record/replay mode lets projects run with no API key. Other projects install it by git tag.

## Status

`v0.1.0a1`, the first pre-release. What works:

- routed Claude calls with cost-based tiers, structured outputs, cost and OpenTelemetry tracing;
- record and replay, so projects run and test with no API key; cassettes in this repository were recorded from the live API;
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

`examples/routed_call.py` makes one routed call in replay mode and prints its trace and cost: `uv run --extra otel python examples/routed_call.py`.

## Install

The distribution is named `aox-agent-core`. Replace `vX.Y.Z` with a release tag. The examples use `v0.1.0a1`.

```sh
pip install "aox-agent-core @ git+https://github.com/AOX-LLC/agent-core@v0.1.0a1"
```

With extras:

```sh
pip install "aox-agent-core[postgres,otel] @ git+https://github.com/AOX-LLC/agent-core@v0.1.0a1"
```

With uv:

```sh
uv add "aox-agent-core @ git+https://github.com/AOX-LLC/agent-core" --tag v0.1.0a1
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

In `replay` mode every call is served from a cassette, a JSON file under `replay.cassette_dir`, so a project runs and tests with no API key. To record, set `AGENT_CORE_MODE=record` and `AGENT_CORE_ANTHROPIC_API_KEY`, then run the code once; commit the cassettes it writes. Recording refuses to write a cassette that contains anything that looks like a key, and the live key itself is always caught.

A request's hash covers the JSON schema the Anthropic SDK generates for an output model, so upgrading `anthropic` or `pydantic` can change it. Replay then reports a miss, and the cassette has to be recorded again.

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
