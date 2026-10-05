# agent-core

[![CI](https://github.com/AOX-LLC/agent-core/actions/workflows/ci.yml/badge.svg)](https://github.com/AOX-LLC/agent-core/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

Model-driven work needs the same plumbing every time: routed calls with a cost budget, validated outputs, traces with tokens and cost, a way to run and test with no API key, a human approval step for agent actions, and an audit log with a verifiable hash chain. agent-core is that plumbing as one small Python library, installed by git tag. On Postgres, once the owner role has installed the schema, the database refuses an approval from the agent's role. The example below runs on a throwaway SQLite file with no key and no Docker, where the library's own checks are all there is.

```sh
pip install "aox-agent-core @ git+https://github.com/AOX-LLC/agent-core@v0.1.0"
```

![A terminal running examples/approval_flow.py in replay mode: a traced call with its cost, an approval refusing an agent and a second use, and the audit log verifying](docs/media/approval-flow.gif)

The output of `uv run --extra otel python examples/approval_flow.py` in replay mode (a recorded call, so no key; ids differ per run). It shows the traced call and its cost, the approval refusing an agent and a second use, and the audit log verifying. [MP4 of the same run](docs/media/approval-flow.mp4).

```text
invoice: INV-1042 from Northwind Paper Supply, total 244.08
trace id: 7733bb5ed8d91babb8241f85e8de4ad0
span: agent_core.model_call
  agent_core.prompt.id = invoices.extract
  agent_core.tier = small
  agent_core.mode = replay
  agent_core.run_id = example-run-1
cost: $0.002364

Approval:
submitted by agent-extract: pending
refused, as it should be: Request 18840830-231b-4b48-bf7d-39b1adcf4b57 cannot be resolved: not_human.
resolved by user-21: approved
consumed by agent-extract: consumed
second use refused: Request 18840830-231b-4b48-bf7d-39b1adcf4b57 is consumed.

Audit log:
   1  model.call               agent-extract
   2  approval.requested       agent-extract
   3  approval.resolve_denied  agent-extract (not_human)
   4  approval.resolved        user-21
   5  approval.consumed        agent-extract
   6  approval.consume_denied  agent-extract (not_open)
audit log verified: 6 records, head seq 6
```

agent-core is a Python library for routed Claude model calls, structured outputs, tracing with cost, human approvals, an append-only audit log, and evals. Models are chosen by cost tier. A record/replay mode lets a project run and test with no API key. Projects install it by git tag.

- **Model calls:** cost-based tiers, structured outputs with retries, versioned prompts (`PromptRef`), PNG, JPEG and PDF attachments, a per-call budget, and OpenTelemetry spans with token and cost attributes.
- **Record and replay:** calls are recorded to files keyed by content and replayed with no key.
- **Control layer:** a hash-chained audit log and a human-approval queue on SQLite or Postgres, async, with a `RunContext` that ties records to the host's own run.
- **Evals:** an eval runner with JSON and Markdown scorecards.

The Bedrock provider is an interface only. See [Known limits](#known-limits).

## Install

The distribution is named `aox-agent-core`, and this is `v0.1.0`. `anthropic` is a core dependency in this release, so every install pulls it in, replay included.

```sh
pip install "aox-agent-core @ git+https://github.com/AOX-LLC/agent-core@v0.1.0"
```

With extras:

```sh
pip install "aox-agent-core[postgres,otel] @ git+https://github.com/AOX-LLC/agent-core@v0.1.0"
```

With uv:

```sh
uv add "aox-agent-core @ git+https://github.com/AOX-LLC/agent-core" --tag v0.1.0
```

| Extra      | Adds                                         |
| ---------- | -------------------------------------------- |
| `bedrock`  | Amazon Bedrock support for the Anthropic SDK |
| `postgres` | Postgres backend (psycopg 3 and its pool)    |
| `otel`     | OpenTelemetry SDK and OTLP HTTP exporter     |
| `testing`  | pytest, for the test helpers                 |

Python 3.11 or later. Postgres 16 or later and SQLite 3.35 or later for the storage backends; the library refuses an older one with a `ConfigError`.

## Quick start

```python
from aox_agent_core import AgentClient, Tier

with AgentClient() as client:
    result = client.call_sync("Summarize this ticket: ...", tier=Tier.SMALL)
    print(result.output, result.cost_usd, result.trace_id)
```

The default mode is `replay`, so a fresh clone never spends money and needs no key. In async code use `await client.call(...)`; `call_sync` raises if an event loop is already running. Pass `output=SomePydanticModel` for validated structured output, and `task="extraction"` instead of a tier once `routing.tasks` maps it.

A versioned prompt with an attachment and a run context:

```python
from aox_agent_core import Attachment, PromptRef, RunContext

EXTRACT = PromptRef(
    id="invoices.extract",
    version=1,
    system="You extract fields from supplier invoices.",
    template="Extract the invoice fields. Amounts are in ${currency}.",
)

result = await client.call(
    EXTRACT,
    inputs={"currency": "USD"},
    attachments=[Attachment.from_path("invoice.pdf")],
    output=InvoiceFields,
    tier=Tier.SMALL,
    context=RunContext(run_id=str(run.id), external_ids={"execution_id": "4812"}),
)
```

An attachment's type is read from its first bytes, never its name, and is capped at 5 MB per image and 24 MB per PDF. Type a dependency as `ModelClient`, the protocol `AgentClient` implements, so a test double can stand in.

## Configuration

- `AGENT_CORE_MODE` is `replay` (the default), `record` or `live`.
- `AGENT_CORE_CONFIG` is the path of a TOML file merged over the packaged defaults; restate only what you change. See [examples/agent-core.toml](examples/agent-core.toml).
- The API key comes only from an explicit `api_key` argument or `AGENT_CORE_ANTHROPIC_API_KEY`. The library never reads `ANTHROPIC_API_KEY`, and live and record modes refuse to start while `ANTHROPIC_CUSTOM_HEADERS` is set.
- `AGENT_CORE_AUDIT_DATABASE_URL` sets the audit log's database. A URL with a password is refused in a config file.

```toml
[routing.tiers.large]
model = "claude-sonnet-5-5"

[routing.tasks]
extraction = "small"
```

## Record and replay

In `replay` mode every call is served from a recording under `replay.cassette_dir`. To record, set `AGENT_CORE_MODE=record` and `AGENT_CORE_ANTHROPIC_API_KEY`, run the code once, and commit the files it writes. Recording refuses to write anything that looks like a key.

- A `PromptRef` call is keyed by its content (prompt ID and version, routed tier, output schema name, inputs, attachment SHA-256s, attempt number), never by model ID, call order or timestamps. A miss raises `ReplayMissError` and never falls back to a live call. Editing a prompt without bumping its version raises `StaleRecordingError`.
- Recordings are format 2. A call without a `PromptRef`, and a structured prompted recording, depends on the JSON schema the installed Anthropic SDK generates for the output model, so upgrading `anthropic` or `pydantic` can make replay miss or go stale; record those again.
- Check recordings in CI with `aox-agent-core cassettes check replays/`.
- In tests, enable the fixtures with `pytest_plugins = ["aox_agent_core.testing.pytest_plugin"]` (the `testing` extra) and take `use_cassette("name")`.

## Audit log and approvals

```python
from aox_agent_core.approvals import RoleApproverPolicy, SQLApprovalQueue
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.storage import open_database


async def main() -> None:
    async with open_database("sqlite:///control.sqlite3") as database:
        audit_log = SQLAuditLog(database)
        policy = RoleApproverPolicy(roles_by_action={"crm.update_contact": "ops.approver"})
        approvals = SQLApprovalQueue(database, audit_log=audit_log, policy=policy)
        await audit_log.append(AuditEvent(action="crm.contact_viewed", actor_id="agent-intake"))
```

- The audit log is append-only and hash-chained. Keep the head from `await audit_log.head()` somewhere the application cannot write, and check against it with `audit_log.verify(expected_head=...)`, `audit_log.verify_report(...)` or `aox-agent-core audit verify`: a chain on its own cannot show it was cut short.
- Approvals are single use: call `consume(..., principal=...)` right before acting. At most one request is open per requester, action and payload. The approver may be neither the requester nor a delegate. `wait_for_decision` waits for a decision without polling every second.
- On Postgres, `append`, `append_many` and a queue's methods accept `connection=` to commit with the host's own transaction.
- A script with no event loop wraps a log or queue in `aox_agent_core.sync`.

## Trust model

What enforces the approval rules depends on the backend.

- **Postgres: database-enforced.** An operator creates a requester role (the agent side: submit, consume, cancel) and an approver role (the decision side: approve, reject), then runs `storage.install_postgres_schema(owner_url, requester_role=..., approver_role=...)` once, as the owner role. A guard trigger on the approvals table checks every insert and update against a fixed transition table, so the requester role cannot move a request to approved by any route, plain SQL included. A queue checks the setup before its first statement and raises `ConfigError` if it is wrong. Each audit row records `db_role` (the role that inserted it) and `db_login` (the login that authenticated), both set by the database.
- **SQLite is not a trust boundary.** Any process that can write the file can change anything in it, the audit log and approvals included, and one queue acts for both sides. Use SQLite for local development and tests.
- **Login binding is opt-in and Postgres only.** With `install_postgres_schema(..., bind_resolved_by=True)`, the database requires `resolved_by` to be the principal the owner mapped to the login that decides the request (`bind_approver_login`). It is off by default, and a host whose approvers share one login cannot use it. Without it, the approver role can record any `resolved_by`; read `db_login` next to it.
- Some rules stay with the library, because the database does not know principals: that the approver is a human holding the required role, that the consumer is the requester or a named delegate, and that the action and payload presented at `consume` match the approved hash. The database also refuses an approver who is the requester or a delegate. It cannot force an audit event for a change made with plain SQL.

The role layout, the transition table and the setup are in [docs/upgrade-0.1.0a3.md](docs/upgrade-0.1.0a3.md).

**Use pseudonymous database login names.** Audit rows (`db_login`) and the login-binding table are append-only, so a login name written to them cannot be erased. A login named `approver_17` leaves less behind than one named for a person.

## Known limits

- The Bedrock provider is an interface only. The small tier has no Bedrock default model.
- Recordings are format 2 and tied to the installed SDK's output-schema hash (see Record and replay).
- No connection pooler has been tested. Statements use `prepare=False` and per-transaction settings, which is the design for a transaction-mode pooler.
- Postgres 16 or later is required; CI runs 16 and 17 only.
- In a host's transaction (`connection=`), a queue call takes the request's row before the audit append lock, so a host that appends first and then calls the queue can be aborted by Postgres with `40P01`. Retry the transaction.
- The database cannot force an audit event: with plain SQL a role can submit, decide, cancel, consume or purge without one.
- Writes to the login mapping table go through a guard that checks `session_user` and `current_user`, so a role with no grant, such as a member of `pg_write_all_data`, cannot write it. Members of `pg_read_all_data` can still read it. The owner role, its members and superusers are trusted: they can disable the guard.
- Login binding is only as strong as each login's authentication.

The full list, by release, is in [CHANGELOG.md](CHANGELOG.md).

## Documentation

- [docs/upgrading.md](docs/upgrading.md): upgrading between pre-releases and to `v0.1.0`, with the operator steps.
- [docs/compat-03.md](docs/compat-03.md): how a consuming project's interface names map to agent-core's.
- [docs/api.md](docs/api.md): the API reference.
- [examples/routed_call.py](examples/routed_call.py): a routed, prompted call with a PDF in replay mode: `uv run --extra otel python examples/routed_call.py`.
- [examples/control_layer_demo.py](examples/control_layer_demo.py): the audit log and an approval on a local SQLite file.
- [examples/approval_flow.py](examples/approval_flow.py): an approval flow from request to use.
- `evals/run_triage_eval.py` runs the synthetic eval suite and prints its scorecard.

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
docker compose up -d --wait postgres   # optional: the Postgres tests
AGENT_CORE_TEST_POSTGRES_ADMIN_URL=postgresql://postgres@127.0.0.1:4202/postgres uv run pytest
uvx pre-commit install
```

`benchmarks/audit_throughput.py` times concurrent audit appends against Postgres; see [benchmarks/README.md](benchmarks/README.md).

## Releases

Releases are git tags: `vX.Y.Z` for releases and `vX.Y.ZaN` for pre-releases. See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
