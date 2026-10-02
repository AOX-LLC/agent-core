import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from aox_agent_core import AgentClient, Mode, Tier
from aox_agent_core.errors import EventLoopRunningError, MissingCredentialsError
from aox_agent_core.models import RouteRequest


def test_client_builds_in_replay_mode_without_a_key() -> None:
    client = AgentClient()

    assert client.config.mode is Mode.REPLAY


@pytest.mark.parametrize("blank_key", ["", "   "])
def test_client_rejects_a_blank_api_key(blank_key: str) -> None:
    with pytest.raises(MissingCredentialsError):
        AgentClient(api_key=blank_key)


def test_call_sync_refuses_to_run_inside_an_event_loop() -> None:
    client = AgentClient()

    async def call_from_inside_a_loop() -> None:
        client.call_sync("hello")

    with pytest.raises(EventLoopRunningError, match=r"await client\.call"):
        asyncio.run(call_from_inside_a_loop())


def test_call_sync_runs_call_outside_an_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AgentClient()
    received: dict[str, Any] = {}

    async def fake_call(prompt: str, **options: Any) -> str:
        received.update(options, prompt=prompt)
        return "called"

    monkeypatch.setattr(client, "call", fake_call)

    result: object = client.call_sync("hello", tier=Tier.SMALL)

    assert result == "called"
    assert received["prompt"] == "hello"
    assert received["tier"] is Tier.SMALL


def test_route_request_takes_a_tier_or_a_task_not_both() -> None:
    with pytest.raises(ValidationError, match="not both"):
        RouteRequest(
            tier=Tier.SMALL, task="extraction", estimated_input_tokens=10, max_output_tokens=10
        )
