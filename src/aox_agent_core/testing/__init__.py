"""Test helpers for consuming projects. Importing this module never imports pytest.

The pytest fixtures live in aox_agent_core.testing.pytest_plugin, which needs
the `testing` extra. Enable it from a conftest.py with:

    pytest_plugins = ["aox_agent_core.testing.pytest_plugin"]
"""

from aox_agent_core.config import AgentCoreConfig
from aox_agent_core.models.client import AgentClient


def cassette_client(name: str, *, config: AgentCoreConfig | None = None) -> AgentClient:
    """Return a client bound to the cassette `name`.

    It replays by default. With AGENT_CORE_MODE=record it records to that
    cassette instead, which needs AGENT_CORE_ANTHROPIC_API_KEY.
    """
    raise NotImplementedError("cassette_client is not implemented yet.")


__all__ = ["cassette_client"]
