"""Every test runs offline and ignores the developer's environment."""

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from databases import ControlDatabase, postgres_database, sqlite_database

AMBIENT_VARIABLES = (
    "AGENT_CORE_ANTHROPIC_API_KEY",
    "AGENT_CORE_AUDIT_DATABASE_URL",
    "AGENT_CORE_CONFIG",
    "AGENT_CORE_MODE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_LOG",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AMBIENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


class NetworkBlockedError(OSError):
    """An OSError, so socket helpers close the socket as they would on a real failure."""


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every connection and lookup except loopback, for local test servers."""
    connect = socket.socket.connect
    getaddrinfo = socket.getaddrinfo

    def connect_loopback_only(sock: socket.socket, address: object) -> None:
        if isinstance(address, tuple) and address[0] in LOOPBACK_HOSTS:
            return connect(sock, address)
        raise NetworkBlockedError("Tests must not open network connections.")

    def refuse_connection(*_args: object, **_kwargs: object) -> None:
        raise NetworkBlockedError("Tests must not open network connections.")

    def lookup_loopback_only(host: object, *args: Any, **kwargs: Any) -> Any:
        if host in LOOPBACK_HOSTS:
            return getaddrinfo(host, *args, **kwargs)
        raise NetworkBlockedError("Tests must not resolve host names.")

    monkeypatch.setattr(socket.socket, "connect", connect_loopback_only)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connection)
    monkeypatch.setattr(socket, "getaddrinfo", lookup_loopback_only)


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


@pytest.fixture(params=["sqlite", "postgres"])
def control_database(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ControlDatabase]:
    """The audit and approval tests run against both backends."""
    if request.param == "sqlite":
        yield sqlite_database(tmp_path)
    else:
        with postgres_database() as database:
            yield database
