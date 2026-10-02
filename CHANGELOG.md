# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Pre-releases are spelled the PEP 440 way, so tags look like `v0.1.0a1`.

## [Unreleased]

### Added

- Package layout and public interfaces (protocols, config models) as stubs.
- Configuration with packaged defaults: tier models and dated Anthropic and Bedrock prices.
- API key read only from an explicit argument or `AGENT_CORE_ANTHROPIC_API_KEY`.
- Audit database URL from `AGENT_CORE_AUDIT_DATABASE_URL`; a password in a config file is refused.
- Approvals are bound to their action and payload and are single-use.
- Error hierarchy.
- Extras `bedrock`, `postgres`, `otel` and `testing`.
- CI: lint, types and tests on Python 3.11 to 3.14, package check and gitleaks.
- Tag-driven release workflow.
- Model layer: the Anthropic provider (explicit key and base URL), tier and task routing with a per-call budget that raises or drops one tier, structured outputs with retries and one-tier escalation, and cost from the dated price table.
- `call_sync` runs on one event loop per client; `close()`, `aclose()`, `with` and `async with` release connections, and the client stays usable afterwards.
- OpenTelemetry spans with token, cost and routing attributes; prompt and completion text only with `tracing.capture_content`, scrubbed.
- Record and replay: cassette format version 1, the directory store, canonical request hashes with sequence numbers, a secret scrubber over recorded content that refuses to write by default, and `CassetteConflictError` when two recorders write one cassette.
- `routing.budget_usd_per_call` covers a whole call, retries and escalation included.
- `aox-agent-core cassettes check` command and the `use_cassette` pytest fixture.
- Bedrock provider interface and Bedrock prices; a tier moved to Bedrock without a model takes it from `bedrock.tier_models`, and the small tier has no Bedrock default.
- `examples/routed_call.py`, replaying a call and printing its trace and cost.

### Changed

- `RouteRequest.max_output_tokens` is optional and defaults to the tier's `max_tokens`; `RouteDecision` gains `max_tokens`.
- A relative `replay.cassette_dir` in a config file resolves against that file's folder.
- Live and record modes refuse to start while `ANTHROPIC_CUSTOM_HEADERS` is set, since the SDK would add those headers to every request.

[Unreleased]: https://github.com/AOX-LLC/agent-core/commits/main
