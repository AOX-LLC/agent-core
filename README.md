# agent-core

agent-core is a small Python library for routed Claude model calls, structured outputs, tracing with cost, human approvals, an append-only audit log, and evals. Models are chosen by cost tier. A record/replay mode lets projects run with no API key. Other projects install it by git tag.

## Status

Pre-release. The model layer works: routed calls, structured outputs, cost, tracing and record/replay. Approvals, the audit log and evals are still interfaces only. The first usable pre-release will be `v0.1.0a1`.

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

## Bedrock

The Bedrock provider is an interface only in this release. Setting a tier's `provider = "bedrock"` without a `model` picks that tier's model from `bedrock.tier_models`, priced under `pricing.bedrock`: the mid and large tiers have defaults. The small tier has none: Claude Haiku 4.5 reaches end of life on Bedrock no sooner than 2026-10-16, so moving the small tier to Bedrock without naming and pricing a model is a configuration error.

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
uvx pre-commit install
```

## Releases

Releases are git tags: `vX.Y.Z` for releases and `vX.Y.ZaN` for pre-releases. See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
