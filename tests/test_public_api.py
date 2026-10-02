"""The public API, frozen.

A failure here means the public API changed. If that was intended, update this
list in the same pull request and record the change in CHANGELOG.md.
"""

import importlib

import pytest

EXPECTED_EXPORTS = {
    "aox_agent_core": [
        "AgentClient",
        "AgentCoreConfig",
        "AgentCoreError",
        "BudgetAction",
        "CallResult",
        "Effort",
        "Message",
        "Mode",
        "Provider",
        "Role",
        "Tier",
        "Usage",
        "__version__",
        "load_config",
        "resolve_api_key",
    ],
    "aox_agent_core.models": [
        "AgentClient",
        "AnthropicProvider",
        "BedrockProvider",
        "CallResult",
        "ConfigRouter",
        "LiveProviders",
        "Message",
        "ModelProvider",
        "Prompt",
        "ProviderRequest",
        "ProviderResponse",
        "Role",
        "RouteDecision",
        "RouteRequest",
        "Router",
        "Usage",
    ],
    "aox_agent_core.replay": [
        "CASSETTE_FORMAT_VERSION",
        "DEFAULT_SECRET_PATTERNS",
        "Cassette",
        "CassetteEntry",
        "CassetteStore",
        "DirectoryCassetteStore",
        "PatternScrubber",
        "RecordingProvider",
        "ReplayProvider",
        "Scrubber",
        "SecretFinding",
        "request_hash",
    ],
    "aox_agent_core.tracing": ["INSTRUMENTATION_NAME", "attributes", "get_tracer"],
    "aox_agent_core.approvals": [
        "TTL_SECONDS_MAX",
        "ApprovalQueue",
        "ApprovalRequest",
        "ApprovalStatus",
        "ApproverPolicy",
        "Decision",
        "DenialReason",
        "Principal",
        "PrincipalKind",
        "ResolveVerdict",
        "RoleApproverPolicy",
    ],
    "aox_agent_core.audit": [
        "AUDIT_SCHEMA_VERSION",
        "FORBIDDEN_KEY_SUFFIXES",
        "GENESIS_HASH",
        "MAX_PAYLOAD_BYTES",
        "AuditEvent",
        "AuditHead",
        "AuditLog",
        "AuditRecord",
        "SQLAuditLog",
        "UnsealedAuditRecord",
        "compute_record_hash",
    ],
    "aox_agent_core.evals": [
        "SCORECARD_FORMAT_VERSION",
        "CaseResult",
        "EvalCase",
        "EvalRunner",
        "EvalSuite",
        "EvalTarget",
        "Score",
        "Scorecard",
        "Scorer",
        "TargetOutput",
        "render_scorecard_markdown",
        "write_scorecard_json",
    ],
    "aox_agent_core.testing": ["cassette_client"],
    "aox_agent_core.storage": [
        "Database",
        "Dialect",
        "PostgresDatabase",
        "SQLiteDatabase",
        "Session",
        "install_postgres_schema",
        "open_database",
    ],
}


@pytest.mark.parametrize(("module_name", "expected"), sorted(EXPECTED_EXPORTS.items()))
def test_public_exports_are_unchanged(module_name: str, expected: list[str]) -> None:
    module = importlib.import_module(module_name)

    assert sorted(module.__all__) == sorted(expected)
    for name in expected:
        assert hasattr(module, name), f"{module_name}.{name} is listed but missing"


def test_version_is_a_development_or_release_version() -> None:
    import aox_agent_core

    assert aox_agent_core.__version__.startswith("0.")
