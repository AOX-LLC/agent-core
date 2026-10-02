"""Every test runs offline and ignores the developer's environment."""

import socket

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

AMBIENT_VARIABLES = (
    "AGENT_CORE_ANTHROPIC_API_KEY",
    "AGENT_CORE_AUDIT_DATABASE_URL",
    "AGENT_CORE_CONFIG",
    "AGENT_CORE_MODE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AMBIENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


class NetworkBlockedError(OSError):
    """An OSError, so socket helpers close the socket as they would on a real failure."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_connection(*_args: object, **_kwargs: object) -> None:
        raise NetworkBlockedError("Tests must not open network connections.")

    def refuse_lookup(*_args: object, **_kwargs: object) -> None:
        raise NetworkBlockedError("Tests must not resolve host names.")

    monkeypatch.setattr(socket.socket, "connect", refuse_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connection)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_lookup)


@pytest.fixture(scope="session")
def _session_span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(tracer_provider)
    return exporter


@pytest.fixture
def spans(_session_span_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    """Finished spans from this test only."""
    _session_span_exporter.clear()
    return _session_span_exporter
