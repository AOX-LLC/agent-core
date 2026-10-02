"""Regenerate evals/replays/triage-eval.json offline.

No live API key was available when the eval suite was written, so the cassette
is built from the documented Messages API response shape (tests/fixtures/sdk/
triage_valid.json), served by a mock HTTP transport, through the real
AnthropicProvider and RecordingProvider code. The canned answers match the
expected labels except for two deliberate misses, so the scorecard shows
failures.

    uv run python scripts/build_eval_cassette.py

Re-recording the eval live replaces this cassette: run
evals/run_triage_eval.py with AGENT_CORE_MODE=record and
AGENT_CORE_ANTHROPIC_API_KEY set.
"""

import asyncio
import json
import sys
from pathlib import Path

import anthropic
import httpx2
from pydantic import SecretStr

from aox_agent_core import AgentClient, load_config
from aox_agent_core.config import Mode, SecretAction
from aox_agent_core.models.anthropic_provider import AnthropicProvider
from aox_agent_core.replay.providers import RecordingProvider
from aox_agent_core.replay.scrub import PatternScrubber
from aox_agent_core.replay.store import DirectoryCassetteStore

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_KEY = "offline-placeholder-key"
MODEL = "claude-haiku-4-5-20251001"

sys.path.insert(0, str(ROOT / "evals"))
from run_triage_eval import CASES_PATH, CONFIG_PATH, run  # noqa: E402

# case id -> (queue, urgent, summary). Two answers deliberately differ from the
# expected labels: triage-04 gets the wrong queue, triage-08 the wrong urgency.
ANSWERS = {
    "triage-01": ("billing", True, "Duplicate charge on INV-2001 needs a refund today."),
    "triage-02": ("billing", False, "Customer wants a PDF copy of invoice INV-2002."),
    "triage-03": ("billing", False, "Customer asks to change the billing address."),
    "triage-04": ("account", True, "Workspace suspended after a declined card; team locked out."),
    "triage-05": ("technical", True, "Report pages return a 500 error and block the team."),
    "triage-06": ("technical", False, "CSV export puts the date column after the amount."),
    "triage-07": ("technical", False, "Customer asks about the reports endpoint rate limit."),
    "triage-08": ("account", False, "Password reset link expires immediately; meeting in an hour."),
    "triage-09": ("account", False, "Customer asks to add a teammate to the workspace."),
    "triage-10": ("account", False, "Customer wants to close the account and export data."),
}


def load_tickets() -> dict[str, str]:
    """Map each case's ticket text to its case id."""
    tickets = {}
    for line in CASES_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            tickets[case["input"]] = case["id"]
    return tickets


TICKETS = load_tickets()


def last_user_text(body: dict) -> str:
    content = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content)


def handler(request: httpx2.Request) -> httpx2.Response:
    problems = []
    if str(request.url) != "https://api.anthropic.com/v1/messages":
        problems.append(f"unexpected URL {request.url}")
    if request.headers.get("x-api-key") != PLACEHOLDER_KEY:
        problems.append("x-api-key header missing or wrong")
    if "authorization" in request.headers:
        problems.append("unexpected Authorization header")
    ticket = last_user_text(json.loads(request.content))
    case_id = TICKETS.get(ticket)
    if case_id is None:
        problems.append("ticket text matches no eval case")
    if problems:
        raise RuntimeError("; ".join(problems))

    number = int(case_id.rsplit("-", 1)[1])
    queue, urgent, summary = ANSWERS[case_id]
    body = {
        "id": f"msg_eval_{number:04d}",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [
            {
                "type": "text",
                "text": json.dumps({"queue": queue, "urgent": urgent, "summary": summary}),
            }
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 170 + (number * 5) % 51,
            "output_tokens": 35 + (number * 7) % 26,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "service_tier": "standard",
        },
    }
    return httpx2.Response(200, json=body)


async def build() -> None:
    config = load_config(CONFIG_PATH).model_copy(update={"mode": Mode.RECORD})
    live = AnthropicProvider(
        api_key=SecretStr(PLACEHOLDER_KEY),
        settings=config.anthropic,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    store = DirectoryCassetteStore(
        ROOT / "evals/replays", scrubber=PatternScrubber(), on_secret=SecretAction.REFUSE
    )
    provider = RecordingProvider(live=live, store=store, cassette="triage-eval")
    async with AgentClient(config, provider=provider) as client:
        scorecard = await run(client)
    print(f"recorded {len(scorecard.results)} cases, accuracy {scorecard.accuracy:.0%}")


if __name__ == "__main__":
    asyncio.run(build())
