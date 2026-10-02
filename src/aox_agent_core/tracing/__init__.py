"""OpenTelemetry tracing for model calls.

The library only uses the OpenTelemetry API. It never configures an exporter;
the application does, or the `otel` extra's helpers do for scripts.
"""

from importlib.metadata import version

from opentelemetry import trace

from aox_agent_core.tracing import attributes

INSTRUMENTATION_NAME = "aox_agent_core"


def get_tracer() -> trace.Tracer:
    """Return the library's tracer. It records nothing until the application sets up the SDK."""
    return trace.get_tracer(INSTRUMENTATION_NAME, version("aox-agent-core"))


__all__ = ["INSTRUMENTATION_NAME", "attributes", "get_tracer"]
