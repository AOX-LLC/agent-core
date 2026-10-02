"""Validation errors must not echo the values they reject."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from aox_agent_core.audit import AuditEvent
from aox_agent_core.config import load_config
from aox_agent_core.errors import ConfigError

REJECTED_VALUE = "rejected-value-must-not-be-echoed"


def test_rejected_audit_payload_is_not_echoed() -> None:
    with pytest.raises(ValidationError) as caught:
        AuditEvent(action="model.call", actor_id="svc-triage", payload={"api_key": REJECTED_VALUE})

    assert REJECTED_VALUE not in str(caught.value)


def test_rejected_config_value_is_not_echoed(tmp_path: Path) -> None:
    override = tmp_path / "agent-core.toml"
    override.write_text(f'[routing]\napi_key = "{REJECTED_VALUE}"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_config(override, environ={})

    assert REJECTED_VALUE not in str(caught.value)
