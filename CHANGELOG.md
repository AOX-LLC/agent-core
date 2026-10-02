# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Pre-releases are spelled the PEP 440 way, so tags look like `v0.1.0a1`.

## [Unreleased]

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
- Budget checks count attachments with a pessimistic per-image and per-PDF-page estimate. PDF pages are counted once, including those in compressed object streams (`Attachment.pdf_pages`); a PDF whose pages cannot be counted is budgeted at 100 pages. `AgentCoreConfig.has_price()`.
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

[Unreleased]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a2...HEAD
[0.1.0a2]: https://github.com/AOX-LLC/agent-core/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/AOX-LLC/agent-core/releases/tag/v0.1.0a1
