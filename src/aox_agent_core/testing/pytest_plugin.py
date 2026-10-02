"""pytest fixtures for consuming projects. Requires the `testing` extra."""

import re
from collections.abc import Callable, Iterator

import pytest

from aox_agent_core.models.client import AgentClient
from aox_agent_core.testing import cassette_client

CassetteClientFactory = Callable[[str], AgentClient]


@pytest.fixture
def use_cassette() -> Iterator[CassetteClientFactory]:
    """A factory for clients bound to a named cassette, closed after the test.

    def test_triage(use_cassette):
        client = use_cassette("triage-urgent")
        result = client.call_sync("...", task="extraction")
    """
    clients: list[AgentClient] = []

    def bind(name: str) -> AgentClient:
        client = cassette_client(name)
        clients.append(client)
        return client

    yield bind
    for client in clients:
        client.close()


@pytest.fixture
def agent_core_client(
    request: pytest.FixtureRequest, use_cassette: CassetteClientFactory
) -> AgentClient:
    """A client bound to a cassette named after the test module and test."""
    module_name = request.module.__name__.rpartition(".")[2]
    return use_cassette(_cassette_name(f"{module_name}-{request.node.name}"))


def _cassette_name(test_id: str) -> str:
    """Turn a test id such as "test_triage-test_route[urgent]" into a valid cassette name."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", test_id).strip("_-")[:128]
