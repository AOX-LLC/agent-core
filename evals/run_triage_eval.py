"""A small support-ticket triage eval suite, scored with FieldMatch.

It sends the 10 synthetic tickets in evals/triage/cases.jsonl through the
"extraction" task and checks the queue and urgency against the expected labels.
By default it replays evals/replays/triage-eval.json, so it needs no API key
and costs nothing.

Run it from the repository root:

    uv run python evals/run_triage_eval.py [--json PATH]

To re-record against the live API, run it with AGENT_CORE_MODE=record and
AGENT_CORE_ANTHROPIC_API_KEY set.
"""

import argparse
import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from aox_agent_core import AgentClient, load_config
from aox_agent_core.evals import (
    EvalRunner,
    EvalSuite,
    FieldMatch,
    Scorecard,
    model_call_target,
    render_scorecard_markdown,
    write_scorecard_json,
)

EVALS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = EVALS_DIR / "agent-core.toml"
CASES_PATH = EVALS_DIR / "triage" / "cases.jsonl"

SYSTEM_PROMPT = "You triage support tickets for a fictional software company."


class TicketTriage(BaseModel):
    queue: Literal["billing", "technical", "account"]
    urgent: bool
    summary: str


async def run(client: AgentClient) -> Scorecard:
    """Run the triage suite through the client and return its scorecard."""
    suite = EvalSuite.from_jsonl(CASES_PATH, name="triage")
    target = model_call_target(client, output=TicketTriage, task="extraction", system=SYSTEM_PROMPT)
    runner = EvalRunner([FieldMatch(["queue", "urgent"])], concurrency=4)
    return await runner.run(suite, target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the triage eval suite.")
    parser.add_argument("--json", type=Path, metavar="PATH", help="also write the JSON scorecard")
    args = parser.parse_args()

    async def run_with_client() -> Scorecard:
        async with AgentClient(load_config(CONFIG_PATH)) as client:
            return await run(client)

    scorecard = asyncio.run(run_with_client())
    if args.json is not None:
        write_scorecard_json(scorecard, args.json)
    print(render_scorecard_markdown(scorecard))


if __name__ == "__main__":
    main()
