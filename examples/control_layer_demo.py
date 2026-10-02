"""The audit log and approval queue on a local SQLite file, step by step.

    uv run python examples/control_layer_demo.py seed control-demo.sqlite3
    uv run python examples/control_layer_demo.py self-approve control-demo.sqlite3

`seed` writes a few audit events and runs one approval from request to use;
`self-approve` shows a requester trying to approve their own request. Both print
the audit log afterwards. Everything here is synthetic.
"""

import argparse
import asyncio
from pathlib import Path

from aox_agent_core.approvals import (
    Decision,
    Principal,
    PrincipalKind,
    SQLApprovalQueue,
)
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.errors import NotAuthorizedToResolveError
from aox_agent_core.storage import open_database

INTAKE_AGENT = Principal(id="agent-intake", kind=PrincipalKind.AGENT)
APPROVER = Principal(id="user-17", kind=PrincipalKind.HUMAN, roles=frozenset({"ops.approver"}))
PAYLOAD = {"contact_id": "c-1001", "phone": "+1-555-0100"}


async def seed(log: SQLAuditLog, queue: SQLApprovalQueue) -> None:
    for ticket in (1, 2, 3):
        await log.append(
            AuditEvent(
                action="model.call",
                actor_id="svc-triage",
                subject_id=f"ticket-{ticket}",
                payload={"tier": "small", "input_tokens": 180, "output_tokens": 45},
            )
        )
    request = await queue.submit(
        action="crm.update_contact",
        summary="Update the sample contact's phone number",
        payload=PAYLOAD,
        requested_by=INTAKE_AGENT,
        required_role="ops.approver",
        ttl_seconds=3_600,
    )
    await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    await queue.consume(request.id, action="crm.update_contact", payload=PAYLOAD)


async def self_approve(queue: SQLApprovalQueue) -> None:
    request = await queue.submit(
        action="crm.update_contact",
        summary="Change a contact, then approve it myself",
        payload=PAYLOAD,
        requested_by=APPROVER,
        required_role="ops.approver",
        ttl_seconds=3_600,
    )
    try:
        await queue.resolve(request.id, decision=Decision.APPROVE, principal=APPROVER)
    except NotAuthorizedToResolveError as error:
        print(f"Refused: {error}")
    print(f"Request status: {(await queue.get(request.id)).status.value}")


async def print_log(log: SQLAuditLog) -> None:
    print("\nAudit log:")
    async for record in log.iter_records():
        reason = record.payload.get("reason")
        suffix = f" ({reason})" if reason else ""
        print(f"  {record.seq:>2}  {record.action:<24} {record.actor_id}{suffix}")
    head = await log.verify()
    print(f"Head: {head.seq} {head.record_hash}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Audit log and approvals on SQLite.")
    parser.add_argument("step", choices=["seed", "self-approve"])
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args()

    database = open_database(f"sqlite:///{arguments.path}")
    log = SQLAuditLog(database)
    queue = SQLApprovalQueue(database, audit_log=log)
    if arguments.step == "seed":
        await seed(log, queue)
    else:
        await self_approve(queue)
    await print_log(log)


if __name__ == "__main__":
    asyncio.run(main())
