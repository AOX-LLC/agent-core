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
- Error hierarchy.
- Extras `bedrock`, `postgres`, `otel` and `testing`.
- CI: lint, types and tests on Python 3.11 to 3.14, package check and gitleaks.
- Tag-driven release workflow.

[Unreleased]: https://github.com/AOX-LLC/agent-core/commits/main
