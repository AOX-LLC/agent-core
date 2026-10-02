"""Regenerate examples/replays/routed-call.json offline.

No live API key was available when the example was written, so the cassette is
built from the documented Messages API response shape (tests/fixtures/sdk/
triage_valid.json), served by a mock HTTP transport, through the real
AnthropicProvider and RecordingProvider code. The recorded request is exactly
what examples/routed_call.py sends.

    uv run python scripts/build_example_cassette.py

With a real key, record the example itself instead: run it with
AGENT_CORE_MODE=record and AGENT_CORE_ANTHROPIC_API_KEY set.
"""

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
RESPONSE_BODY = (ROOT / "tests/fixtures/sdk/triage_valid.json").read_bytes()

sys.path.insert(0, str(ROOT / "examples"))
from routed_call import CONFIG_PATH, triage  # noqa: E402


def handler(request: httpx2.Request) -> httpx2.Response:
    problems = []
    if str(request.url) != "https://api.anthropic.com/v1/messages":
        problems.append(f"unexpected URL {request.url}")
    if request.headers.get("x-api-key") != PLACEHOLDER_KEY:
        problems.append("x-api-key header missing or wrong")
    if "authorization" in request.headers:
        problems.append("unexpected Authorization header")
    if problems:
        raise RuntimeError("; ".join(problems))
    return httpx2.Response(200, content=RESPONSE_BODY, headers={"content-type": "application/json"})


def main() -> None:
    config = load_config(CONFIG_PATH).model_copy(update={"mode": Mode.RECORD})
    live = AnthropicProvider(
        api_key=SecretStr(PLACEHOLDER_KEY),
        settings=config.anthropic,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    store = DirectoryCassetteStore(
        ROOT / "examples/replays", scrubber=PatternScrubber(), on_secret=SecretAction.REFUSE
    )
    provider = RecordingProvider(live=live, store=store, cassette="routed-call")
    with AgentClient(config, provider=provider) as client:
        result = triage(client)
    print(f"recorded: {result.output}")


if __name__ == "__main__":
    main()
