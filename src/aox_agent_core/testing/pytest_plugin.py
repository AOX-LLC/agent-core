"""pytest fixtures for consuming projects. Requires the `testing` extra."""

import re

import pytest

from aox_agent_core.models.client import AgentClient
from aox_agent_core.testing import cassette_client


@pytest.fixture
def agent_core_client(request: pytest.FixtureRequest) -> AgentClient:
    """A client bound to a cassette named after the test module and test."""
    module_name = request.module.__name__.rpartition(".")[2]
    return cassette_client(_cassette_name(f"{module_name}-{request.node.name}"))


def _cassette_name(test_id: str) -> str:
    """Turn a test id such as "test_triage-test_route[urgent]" into a valid cassette name."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", test_id).strip("_-")[:128]
