"""One routed, prompted model call with a PDF attached, in replay mode.

Run it from the repository root; it needs no API key:

    uv run --extra otel python examples/routed_call.py

It reads the synthetic invoice examples/fixtures/invoice-1042.pdf with the
prompt "invoices.extract" v1 on the "extraction" task, which
examples/agent-core.toml maps to the small tier, and prints the trace, the
fields and the cost. The response is replayed from
examples/replays/prompts/invoices.extract/v1/<key>.json, recorded from the live
API; the key comes from the prompt, tier, schema, inputs and the PDF's SHA-256.

Pass --attachment with any other PNG, JPEG or PDF to see a replay miss name the
key and the file it looked for. To record again, run with AGENT_CORE_MODE=record
and AGENT_CORE_ANTHROPIC_API_KEY set.
"""

import argparse
import sys
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from pydantic import BaseModel

from aox_agent_core import AgentClient, Attachment, CallResult, PromptRef, RunContext, load_config
from aox_agent_core.errors import ReplayMissError

EXAMPLES_DIR = Path(__file__).resolve().parent
CONFIG_PATH = EXAMPLES_DIR / "agent-core.toml"
INVOICE_PATH = EXAMPLES_DIR / "fixtures" / "invoice-1042.pdf"

EXTRACT_INVOICE = PromptRef(
    id="invoices.extract",
    version=1,
    system="You extract fields from supplier invoices for a fictional accounts team.",
    template=(
        "Read the attached invoice and extract its fields. Write dates as YYYY-MM-DD and "
        "amounts as plain decimals in ${currency}."
    ),
)


class InvoiceFields(BaseModel):
    vendor: str
    invoice_number: str
    issue_date: str
    due_date: str
    total: str


def extract_invoice(client: AgentClient, invoice: Path = INVOICE_PATH) -> CallResult[InvoiceFields]:
    """The example's one call. It replays only if the prompt, inputs and PDF match."""
    return client.call_sync(
        EXTRACT_INVOICE,
        inputs={"currency": "USD"},
        attachments=[Attachment.from_path(invoice)],
        output=InvoiceFields,
        task="extraction",
        context=RunContext(run_id="example-run-1", external_ids={"source": "examples"}),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract an invoice's fields in replay mode.")
    parser.add_argument("--attachment", type=Path, default=INVOICE_PATH, metavar="PATH")
    arguments = parser.parse_args()

    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    with AgentClient(load_config(CONFIG_PATH)) as client:
        try:
            result = extract_invoice(client, arguments.attachment)
        except ReplayMissError as error:
            print(f"replay miss: {error}", file=sys.stderr)
            print(f"key: {error.key}\npath: {error.path}", file=sys.stderr)
            sys.exit(1)
        finally:
            tracer_provider.shutdown()

    fields = result.output
    print(f"invoice: {fields.invoice_number} from {fields.vendor}, total {fields.total}")
    print(f"issued {fields.issue_date}, due {fields.due_date}")
    print(
        f"tier: {result.tier.value} ({result.model}), mode: {result.mode.value}, "
        f"tokens: {result.usage.input_tokens} in / {result.usage.output_tokens} out"
    )
    print(f"replay key: {result.replay_key}")
    print(f"cost: ${result.cost_usd:.6f}")


if __name__ == "__main__":
    main()
