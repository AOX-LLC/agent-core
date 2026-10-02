# agent-core

agent-core is a small Python library for routed Claude model calls, structured outputs, tracing with cost, human approvals, an append-only audit log, and evals. Models are chosen by cost tier. A record/replay mode lets projects run with no API key. Other projects install it by git tag.

## Status

Pre-release. Only the package layout and public interfaces exist so far. Behavior arrives in later releases. The first usable pre-release will be `v0.1.0a1`.

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

Set the audit log's database with `AGENT_CORE_AUDIT_DATABASE_URL`. A URL with a password is refused in a config file, so it never gets committed.

This example moves the large tier to Sonnet and maps a task to a tier:

```toml
[routing.tiers.large]
model = "claude-sonnet-5-5"

[routing.tasks]
extraction = "small"
```

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
