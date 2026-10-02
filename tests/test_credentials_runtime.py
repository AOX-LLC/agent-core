"""At runtime the library never lets the Anthropic SDK find credentials on its own."""

import json
from pathlib import Path

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from aox_agent_core import AgentClient, Message, Mode, Provider, Role
from aox_agent_core.errors import ConfigError, MissingCredentialsError
from aox_agent_core.models import AnthropicProvider, ProviderRequest
from support import FIXTURES, make_config

DECOY_VALUES = {
    "ANTHROPIC_API_KEY": "decoy-api-key-from-the-environment",
    "ANTHROPIC_AUTH_TOKEN": "decoy-auth-token-from-the-environment",
    "ANTHROPIC_BASE_URL": "https://decoy.invalid",
    "ANTHROPIC_PROFILE": "decoy-profile",
}
LIBRARY_KEY = "library-test-key-not-real"


@pytest.fixture
def sdk_variables_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in DECOY_VALUES.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def sdk_construction_forbidden(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail the test if anything builds an SDK client; record who tried."""
    attempts: list[str] = []

    def refuse(self: object, *_args: object, **_kwargs: object) -> None:
        attempts.append(type(self).__name__)
        raise AssertionError("the SDK client must not be constructed")

    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", refuse)
    monkeypatch.setattr(anthropic.Anthropic, "__init__", refuse)
    return attempts


@pytest.mark.usefixtures("sdk_variables_set")
@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.RECORD])
def test_refuses_to_run_with_only_sdk_variables_set(
    mode: Mode, tmp_path: Path, sdk_construction_forbidden: list[str]
) -> None:
    with pytest.raises(MissingCredentialsError, match="AGENT_CORE_ANTHROPIC_API_KEY"):
        AgentClient(make_config(tmp_path, mode=mode))

    assert sdk_construction_forbidden == []


@pytest.mark.usefixtures("sdk_variables_set")
def test_replay_mode_needs_no_key_and_builds_no_sdk_client(
    tmp_path: Path, sdk_construction_forbidden: list[str]
) -> None:
    AgentClient(make_config(tmp_path, mode=Mode.REPLAY))

    assert sdk_construction_forbidden == []


@pytest.mark.usefixtures("sdk_variables_set")
async def test_explicit_key_and_base_url_win_over_every_sdk_variable() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = (FIXTURES / "sdk" / "text_reply.json").read_text()
        return httpx2.Response(200, json=json.loads(body))

    provider = AnthropicProvider(
        api_key=SecretStr(LIBRARY_KEY),
        settings=make_config().anthropic,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    await provider.complete(
        ProviderRequest(
            provider=Provider.ANTHROPIC,
            model="claude-sonnet-5-5",
            messages=(Message(role=Role.USER, content="hello"),),
            max_tokens=100,
        )
    )
    await provider.aclose()

    (request,) = seen
    assert request.url.host == "api.anthropic.com"
    assert request.headers["x-api-key"] == LIBRARY_KEY
    assert "authorization" not in request.headers
    assert not any(value in str(request.headers) for value in DECOY_VALUES.values())


def test_sdk_custom_headers_variable_blocks_live_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sdk_construction_forbidden: list[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-api-key: injected")

    with pytest.raises(ConfigError, match="ANTHROPIC_CUSTOM_HEADERS"):
        AgentClient(make_config(tmp_path, mode=Mode.LIVE), api_key=LIBRARY_KEY)

    assert sdk_construction_forbidden == []
