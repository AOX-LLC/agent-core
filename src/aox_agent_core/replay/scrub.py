"""Finding secrets before anything is written to a recording or the audit log."""

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol

from pydantic import JsonValue, SecretStr

from aox_agent_core._model import FrozenModel

DEFAULT_SECRET_PATTERNS: Mapping[str, str] = {
    "anthropic_api_key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "aws_access_key_id": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "bearer_token": r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}

KNOWN_SECRET_RULE = "known_secret"  # noqa: S105 - a rule name, not a secret


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
    key is caught even if it does not look like one. Dictionary keys are scanned
    as well as values.
    """

    def __init__(
        self,
        *,
        extra_patterns: Mapping[str, str] | None = None,
        known_secrets: Sequence[SecretStr] = (),
    ) -> None:
        patterns = {**DEFAULT_SECRET_PATTERNS, **(extra_patterns or {})}
        self._patterns = {rule: re.compile(pattern) for rule, pattern in patterns.items()}
        self._known_secrets = tuple(
            secret.get_secret_value() for secret in known_secrets if secret.get_secret_value()
        )

    def find_secrets(self, value: JsonValue) -> tuple[SecretFinding, ...]:
        """Return one finding per rule that matches each string in `value`."""
        # A path is built from dictionary keys, and a key can itself be a secret,
        # so paths are redacted too.
        return tuple(
            SecretFinding(rule=rule, path=self._redact_text(path))
            for path, text in _strings_in(value, "$")
            for rule in self._rules_matching(text)
        )

    def redact(self, value: JsonValue) -> JsonValue:
        """Return a copy of `value` with every match replaced by [REDACTED:<rule>]."""
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact_text(key): self.redact(item) for key, item in value.items()}
        return value

    def _rules_matching(self, text: str) -> list[str]:
        rules = [rule for rule, pattern in self._patterns.items() if pattern.search(text)]
        if any(secret in text for secret in self._known_secrets):
            rules.append(KNOWN_SECRET_RULE)
        return rules

    def _redact_text(self, text: str) -> str:
        for secret in self._known_secrets:
            text = text.replace(secret, f"[REDACTED:{KNOWN_SECRET_RULE}]")
        for rule, pattern in self._patterns.items():
            text = pattern.sub(f"[REDACTED:{rule}]", text)
        return text


def _strings_in(value: JsonValue, path: str) -> Iterator[tuple[str, str]]:
    """Yield (JSON path, string) for every string in value, dictionary keys included."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings_in(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.<key>", key
            yield from _strings_in(item, f"{path}.{key}")
