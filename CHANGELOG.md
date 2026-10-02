# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Pre-releases are spelled the PEP 440 way, so tags look like `v0.1.0a1`.

## [Unreleased]

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

[Unreleased]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/AOX-LLC/agent-core/releases/tag/v0.1.0a1
