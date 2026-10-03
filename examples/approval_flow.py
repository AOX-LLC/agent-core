"""A routed call, its trace and cost, then a human-approved action and a verified audit log.

Run it from the repository root; it needs no API key and no network:

    uv run --extra otel python examples/approval_flow.py

The steps, in order:

1. One routed, prompted call in replay mode: the invoice extraction from
   examples/routed_call.py, answered from the recording in examples/replays.
2. Its trace id, span attributes and cost.
3. An approval to pay the extracted invoice. An AGENT principal submits it; a HUMAN
   principal holding the required role resolves it (an agent cannot, and nobody can
   approve their own request); the agent then consumes it right before acting. One
   approval authorizes one run, so a second use is refused.
4. The audit log, walked and verified from the first record to the last.

The database is a temporary SQLite file. SQLite is not a trust boundary: it has no
roles, so anyone who can write the file could approve a request or drop the audit
log's triggers, and the library's own checks are all there is. On Postgres the
approvals are enforced by the database itself, once the owner role has run
install_postgres_schema; see the trust notes in docs/api.md. Everything here is
synthetic.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from routed_call import CONFIG_PATH, InvoiceFields, extract_invoice

from aox_agent_core import AgentClient, CallResult, RunContext, load_config
from aox_agent_core.approvals import (
    Decision,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
    SQLApprovalQueue,
)
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    NotAuthorizedToResolveError,
    ReplayMissError,
)
from aox_agent_core.storage import open_database

PAY_ACTION = "invoice.pay"
REQUIRED_ROLE = "ap.approver"
EXTRACTION_AGENT = Principal(id="agent-extract", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-21", kind=PrincipalKind.HUMAN, roles=frozenset({REQUIRED_ROLE}))
# The run id extract_invoice puts on its span, so the call and the approval share a run.
RUN = RunContext(run_id="example-run-1", external_ids={"source": "examples"})


def print_trace_and_cost(result: CallResult[InvoiceFields], spans: InMemorySpanExporter) -> None:
    (span,) = spans.get_finished_spans()
    attributes = span.attributes or {}
    print(f"trace id: {result.trace_id}")
    print(f"span: {span.name}")
    for name in ("agent_core.prompt.id", "agent_core.tier", "agent_core.mode", "agent_core.run_id"):
        print(f"  {name} = {attributes[name]}")
    print(f"cost: ${result.cost_usd:.6f}")


async def approve_payment(database_path: Path, result: CallResult[InvoiceFields]) -> bool:
    """Request, decide and use one approval, then verify the audit log. True if it verified."""
    database = open_database(f"sqlite:///{database_path}")
    log = SQLAuditLog(database)
    # The approver side decides which role each action needs; the requester cannot.
    policy = RoleApproverPolicy(roles_by_action={PAY_ACTION: REQUIRED_ROLE})
    queue = SQLApprovalQueue(database, audit_log=log, policy=policy)
    fields = result.output

    # Metadata only: ids, counts, a cost as a decimal string. No prompt or completion text.
    await log.append(
        AuditEvent(
            action="model.call",
            actor_id=EXTRACTION_AGENT.id,
            subject_id=fields.invoice_number,
            payload={
                "tier": result.tier.value,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cost_usd": str(result.cost_usd),
            },
            context=RUN,
        )
    )

    # The approval binds this exact action and payload; consume() must present both again.
    payload = {
        "invoice_number": fields.invoice_number,
        "vendor": fields.vendor,
        "total": fields.total,
    }
    request = await queue.submit(
        action=PAY_ACTION,
        summary=f"Pay invoice {fields.invoice_number} from {fields.vendor}",
        payload=payload,
        requested_by=EXTRACTION_AGENT,
        required_role=REQUIRED_ROLE,
        ttl_seconds=3_600,
        include_payload=True,
        context=RUN,
    )
    print(f"submitted by {request.requested_by}: {request.status.value}")

    try:
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=EXTRACTION_AGENT)
    except NotAuthorizedToResolveError as error:
        print(f"refused, as it should be: {error}")

    approved = await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    print(f"resolved by {approved.resolved_by}: {approved.status.value}")

    consumed = await queue.consume(
        request.id, action=PAY_ACTION, payload=payload, principal=EXTRACTION_AGENT
    )
    print(f"consumed by {EXTRACTION_AGENT.id}: {consumed.status.value}")
    try:
        await queue.consume(
            request.id, action=PAY_ACTION, payload=payload, principal=EXTRACTION_AGENT
        )
    except ApprovalAlreadyResolvedError as error:
        print(f"second use refused: {error}")

    print("\nAudit log:")
    async for record in log.iter_records():
        reason = record.payload.get("reason")
        suffix = f" ({reason})" if reason else ""
        print(f"  {record.seq:>2}  {record.action:<24} {record.actor_id}{suffix}")

    # Walks every record and reports every problem. It proves the chain is intact, not that
    # the log was never rewritten: for that, keep log.head() somewhere the log's writers
    # cannot reach and pass it back as expected_head.
    report = await log.verify_report()
    await database.aclose()
    if report.ok:
        print(f"audit log verified: {report.records_checked} records, head seq {report.head.seq}")
    else:
        print(f"audit log FAILED verification: {len(report.problems)} problem(s)")
        for problem in report.problems:
            print(f"  seq {problem.seq}: {problem.kind}: {problem.detail}")
    return report.ok


def main() -> None:
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    trace.set_tracer_provider(tracer_provider)

    # call_sync owns an event loop, so the routed call runs before the async approval steps.
    with AgentClient(load_config(CONFIG_PATH)) as client:
        try:
            result = extract_invoice(client)
        except ReplayMissError as error:
            print(f"replay miss: {error}", file=sys.stderr)
            sys.exit(1)
        finally:
            tracer_provider.shutdown()

    fields = result.output
    print(f"invoice: {fields.invoice_number} from {fields.vendor}, total {fields.total}")
    print_trace_and_cost(result, spans)

    print("\nApproval:")
    with tempfile.TemporaryDirectory() as directory:
        verified = asyncio.run(approve_payment(Path(directory) / "approval-flow.sqlite3", result))
    if not verified:
        sys.exit(1)


if __name__ == "__main__":
    main()
