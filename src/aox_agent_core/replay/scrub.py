"""Finding secrets before anything is written to a recording or the audit log."""

from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import JsonValue, SecretStr

from aox_agent_core._model import FrozenModel

DEFAULT_SECRET_PATTERNS: Mapping[str, str] = {
    "anthropic_api_key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "aws_access_key_id": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "bearer_token": r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}


class SecretFinding(FrozenModel):
    """Where a secret was found and which rule matched. Never holds the secret itself."""

    rule: str
    path: str


class Scrubber(Protocol):
    """Scans JSON values for secrets and can redact them."""

    def find_secrets(self, value: JsonValue) -> tuple[SecretFinding, ...]: ...

    def redact(self, value: JsonValue) -> JsonValue: ...


class PatternScrubber:
    """Matches every string in a value against regex rules and known secret values.

    known_secrets holds values to match exactly, such as the live API key, so a
    key is caught even if it does not look like one.
    """

    def __init__(
        self,
        *,
        extra_patterns: Mapping[str, str] | None = None,
        known_secrets: Sequence[SecretStr] = (),
    ) -> None:
        self._patterns = {**DEFAULT_SECRET_PATTERNS, **(extra_patterns or {})}
        self._known_secrets = tuple(known_secrets)

    def find_secrets(self, value: JsonValue) -> tuple[SecretFinding, ...]:
        raise NotImplementedError("PatternScrubber.find_secrets is not implemented yet.")

    def redact(self, value: JsonValue) -> JsonValue:
        raise NotImplementedError("PatternScrubber.redact is not implemented yet.")
