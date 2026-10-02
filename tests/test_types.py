"""Validation built into the public models."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError

from aox_agent_core.approvals import (
    ApprovalRequest,
    DenialReason,
    Principal,
    PrincipalKind,
    ResolveVerdict,
)
from aox_agent_core.audit import (
    GENESIS_HASH,
    MAX_PAYLOAD_BYTES,
    AuditEvent,
    AuditRecord,
)
from aox_agent_core.config import Provider
from aox_agent_core.evals import CaseResult, EvalCase, EvalSuite, Score, Scorecard
from aox_agent_core.models import ProviderRequest
from aox_agent_core.replay import Cassette

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SAMPLE_HASH = "a" * 64


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "", "with space", "x" * 129])
def test_cassette_names_cannot_leave_the_cassette_directory(name: str) -> None:
    with pytest.raises(ValidationError):
        Cassette(name=name)


def test_cassette_defaults_to_the_current_format() -> None:
    assert Cassette(name="triage-flow").format_version == 1


def test_provider_request_needs_a_message() -> None:
    with pytest.raises(ValidationError):
        ProviderRequest(provider=Provider.ANTHROPIC, model="m", messages=(), max_tokens=10)


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "x"},
        {"api_key": "x"},
        {"apiKey": "x"},
        {"X-Api-Key": "x"},
        {"client_secret": "x"},
        {"access_token": "x"},
        {"Authorization": "x"},
        {"session_cookie": "x"},
        {"meta": {"refresh_token": "x"}},
        {"items": [{"private_key": "x"}]},
    ],
)
def test_audit_payload_rejects_secret_shaped_keys(payload: dict[str, JsonValue]) -> None:
    with pytest.raises(ValidationError, match="forbidden keys"):
        AuditEvent(action="model.call", actor_id="svc-triage", payload=payload)


def test_audit_payload_allows_token_counts_and_hashes() -> None:
    event = AuditEvent(
        action="model.call",
        actor_id="svc-triage",
        payload={"input_tokens": 120, "output_tokens": 40, "prompt_sha256": SAMPLE_HASH},
    )

    assert event.payload["input_tokens"] == 120


@pytest.mark.parametrize(
    "payload", [{"cost_usd": 0.0123}, {"latency": float("nan")}, {"items": [1, 2.5]}]
)
def test_audit_payload_rejects_floats(payload: dict[str, JsonValue]) -> None:
    with pytest.raises(ValidationError, match="integers"):
        AuditEvent(action="model.call", actor_id="svc-triage", payload=payload)


@pytest.mark.parametrize("subject_id", ["jane@example.com", "has space", ""])
def test_audit_subject_must_be_opaque(subject_id: str) -> None:
    with pytest.raises(ValidationError):
        AuditEvent(action="model.call", actor_id="svc-triage", subject_id=subject_id)


def test_audit_payload_size_is_capped() -> None:
    with pytest.raises(ValidationError, match="limit"):
        AuditEvent(
            action="model.call", actor_id="svc-triage", payload={"note": "x" * MAX_PAYLOAD_BYTES}
        )


@pytest.mark.parametrize("actor_id", ["jane@example.com", "has space", ""])
def test_identifiers_must_be_opaque(actor_id: str) -> None:
    with pytest.raises(ValidationError):
        AuditEvent(action="model.call", actor_id=actor_id)
    with pytest.raises(ValidationError):
        Principal(id=actor_id, kind=PrincipalKind.HUMAN)


def audit_record_fields(**overrides: Any) -> dict[str, Any]:
    return {
        "seq": 1,
        "event_id": uuid4(),
        "occurred_at": NOW,
        "action": "approval.resolved",
        "actor_id": "user-17",
        "subject_id": None,
        "payload": {},
        "prev_hash": GENESIS_HASH,
        "record_hash": SAMPLE_HASH,
        **overrides,
    }


def test_audit_record_accepts_a_well_formed_record() -> None:
    assert AuditRecord(**audit_record_fields()).seq == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"occurred_at": datetime(2026, 10, 2, 12, 0)},
        {"record_hash": "not-a-hash"},
        {"seq": 0},
    ],
)
def test_audit_record_rejects_bad_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AuditRecord(**audit_record_fields(**overrides))


def approval_request_fields(**overrides: Any) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "action": "crm.update_contact",
        "summary": "Update the sample contact's phone number",
        "payload_sha256": SAMPLE_HASH,
        "requested_by": "agent-intake",
        "required_role": "ops.approver",
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        **overrides,
    }


def test_approval_request_must_expire_after_it_is_created() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        ApprovalRequest(**approval_request_fields(expires_at=NOW))


def test_approval_request_needs_aware_timestamps() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(**approval_request_fields(created_at=datetime(2026, 10, 2, 12, 0)))


def test_approval_request_expiry_check() -> None:
    request = ApprovalRequest(**approval_request_fields())

    assert not request.is_expired(NOW)
    assert request.is_expired(NOW + timedelta(hours=1))


def test_verdict_denial_needs_a_reason() -> None:
    assert ResolveVerdict(allowed=False, reason=DenialReason.SELF_APPROVAL).reason
    with pytest.raises(ValidationError):
        ResolveVerdict(allowed=False)
    with pytest.raises(ValidationError):
        ResolveVerdict(allowed=True, reason=DenialReason.EXPIRED)


def test_eval_suite_rejects_duplicate_case_ids() -> None:
    cases = (EvalCase(id="case-1", input="a"), EvalCase(id="case-1", input="b"))

    with pytest.raises(ValidationError, match="duplicate case ids: case-1"):
        EvalSuite(name="sample", cases=cases)


def test_scorecard_failures_include_errors_and_failed_scores() -> None:
    passed = Score(scorer="exact", value=1.0, passed=True)
    failed = Score(scorer="exact", value=0.0, passed=False)
    results = (
        CaseResult(case_id="ok", scores=(passed,), latency_ms=5, cost_usd=Decimal(0)),
        CaseResult(case_id="wrong", scores=(failed,), latency_ms=5, cost_usd=Decimal(0)),
        CaseResult(case_id="crashed", latency_ms=5, cost_usd=Decimal(0), error="timeout"),
    )
    scorecard = Scorecard(
        suite="sample",
        started_at=NOW,
        finished_at=NOW,
        results=results,
        accuracy=1 / 3,
        latency_p50_ms=5,
        latency_p95_ms=5,
        cost_total_usd=Decimal(0),
        cost_per_case_usd=Decimal(0),
    )

    assert [result.case_id for result in scorecard.failures()] == ["wrong", "crashed"]
