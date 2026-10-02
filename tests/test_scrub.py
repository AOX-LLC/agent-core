from pydantic import JsonValue, SecretStr

from aox_agent_core.replay import PatternScrubber

FAKE_ANTHROPIC_KEY = "sk-ant-" + "x" * 24
FAKE_AWS_KEY_ID = "AKIA" + "Z" * 16
LIVE_KEY = "plain-looking-live-key-12345"


def test_finds_each_default_pattern() -> None:
    value: dict[str, JsonValue] = {
        "a": f"key {FAKE_ANTHROPIC_KEY}",
        "b": FAKE_AWS_KEY_ID,
        "c": "Authorization: Bearer " + "t" * 30,
        "d": "-----BEGIN RSA PRIVATE KEY-----",
        "fine": "nothing to see",
    }

    rules = {finding.rule for finding in PatternScrubber().find_secrets(value)}

    assert rules == {"anthropic_api_key", "aws_access_key_id", "bearer_token", "private_key"}


def test_known_secret_is_found_even_without_a_pattern() -> None:
    scrubber = PatternScrubber(known_secrets=[SecretStr(LIVE_KEY)])

    (finding,) = scrubber.find_secrets({"messages": [{"content": f"use {LIVE_KEY}"}]})

    assert finding.rule == "known_secret"
    assert finding.path == "$.messages[0].content"


def test_extra_patterns_are_applied() -> None:
    scrubber = PatternScrubber(extra_patterns={"ticket_token": r"TKT-[0-9]{6}"})

    assert [finding.rule for finding in scrubber.find_secrets(["TKT-123456"])] == ["ticket_token"]


def test_dictionary_keys_are_scanned_and_paths_never_show_the_secret() -> None:
    findings = PatternScrubber().find_secrets({FAKE_ANTHROPIC_KEY: {"inner": "ok"}})

    assert [finding.rule for finding in findings] == ["anthropic_api_key"]
    assert all(FAKE_ANTHROPIC_KEY not in finding.path for finding in findings)


def test_redact_replaces_secrets_and_keeps_structure() -> None:
    scrubber = PatternScrubber(known_secrets=[SecretStr(LIVE_KEY)])

    redacted = scrubber.redact({"text": f"{LIVE_KEY} and {FAKE_ANTHROPIC_KEY}", "count": 3})

    assert redacted == {
        "text": "[REDACTED:known_secret] and [REDACTED:anthropic_api_key]",
        "count": 3,
    }
