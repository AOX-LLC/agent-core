"""One routed model call in replay mode, printing its trace and its cost.

Run it from the repository root; it needs no API key:

    uv run --extra otel python examples/routed_call.py

The call asks for structured output on the "extraction" task, which
examples/agent-core.toml maps to the small tier. Its response is replayed from
examples/replays/routed-call.json, which was recorded from the live API. To
record it again, run with AGENT_CORE_MODE=record and AGENT_CORE_ANTHROPIC_API_KEY
set. The ticket is synthetic.
"""

from pathlib import Path
from typing import Literal

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from pydantic import BaseModel

from aox_agent_core import AgentClient, CallResult, load_config

CONFIG_PATH = Path(__file__).with_name("agent-core.toml")

SYSTEM_PROMPT = "You triage support tickets for a fictional software company."

TICKET = (
    "Subject: Invoice charged twice\n\n"
    "Hi, our card was charged twice for the September invoice (INV-1042). "
    "Please refund the duplicate before our month-end close on Friday."
)


class TicketTriage(BaseModel):
    queue: Literal["billing", "technical", "account"]
    urgent: bool
    summary: str


def triage(client: AgentClient) -> CallResult[TicketTriage]:
    """The example's one call; its request must match the recorded one to replay."""
    return client.call_sync(TICKET, output=TicketTriage, task="extraction", system=SYSTEM_PROMPT)


def main() -> None:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    with AgentClient(load_config(CONFIG_PATH)) as client:
        result = triage(client)
    tracer_provider.shutdown()

    print(f"queue: {result.output.queue}, urgent: {result.output.urgent}")
    print(f"summary: {result.output.summary}")
    print(
        f"tier: {result.tier.value} ({result.model}), mode: {result.mode.value}, "
        f"tokens: {result.usage.input_tokens} in / {result.usage.output_tokens} out"
    )
    print(f"cost: ${result.cost_usd:.6f}")


if __name__ == "__main__":
    main()
