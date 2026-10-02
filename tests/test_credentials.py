import pytest
from pydantic import SecretStr

from aox_agent_core.credentials import API_KEY_ENV, resolve_api_key
from aox_agent_core.errors import MissingCredentialsError

FAKE_KEY = "test-key-not-real"


def test_explicit_key_wins_over_the_environment() -> None:
    key = resolve_api_key("explicit-test-key", environ={API_KEY_ENV: FAKE_KEY})

    assert key.get_secret_value() == "explicit-test-key"


def test_explicit_secret_str_is_accepted() -> None:
    key = resolve_api_key(SecretStr(FAKE_KEY), environ={})

    assert key.get_secret_value() == FAKE_KEY


def test_key_is_read_from_the_library_variable() -> None:
    key = resolve_api_key(environ={API_KEY_ENV: f"  {FAKE_KEY}\n"})

    assert key.get_secret_value() == FAKE_KEY


@pytest.mark.parametrize(
    "sdk_variable",
    ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"],
)
def test_the_sdk_credential_variables_are_never_read(sdk_variable: str) -> None:
    with pytest.raises(MissingCredentialsError):
        resolve_api_key(environ={sdk_variable: FAKE_KEY})


def test_sdk_variable_is_ignored_in_the_real_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

    with pytest.raises(MissingCredentialsError, match=API_KEY_ENV):
        resolve_api_key()


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_keys_are_rejected(blank: str) -> None:
    with pytest.raises(MissingCredentialsError):
        resolve_api_key(blank, environ={})
    with pytest.raises(MissingCredentialsError):
        resolve_api_key(environ={API_KEY_ENV: blank})


def test_key_does_not_appear_in_its_repr() -> None:
    key = resolve_api_key(FAKE_KEY, environ={})

    assert FAKE_KEY not in repr(key)
    assert FAKE_KEY not in str(key)
