"""Helpers shared by the tests: scripted providers and configs pointed at tmp dirs."""

import asyncio
from pathlib import Path
from typing import Any

from aox_agent_core.config import AgentCoreConfig, load_config
from aox_agent_core.models.types import ProviderRequest, ProviderResponse, Usage
from aox_agent_core.replay.keys import PromptKey

SMALL_MODEL = "claude-haiku-4-5-20251001"
FIXTURES = Path(__file__).parent / "fixtures"


def response(
    text: str,
    *,
    stop_reason: str = "end_turn",
    model: str = SMALL_MODEL,
    input_tokens: int = 100,
    output_tokens: int = 20,
) -> ProviderResponse:
    return ProviderResponse(
        model=model,
        text=text,
        stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class ScriptedProvider:
    """Returns prepared responses in order and keeps every request it received."""

    def __init__(self, *responses: ProviderResponse) -> None:
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.prompt_keys: list[PromptKey | None] = []

    async def complete(
        self, request: ProviderRequest, *, prompt_key: PromptKey | None = None
    ) -> ProviderResponse:
        self.requests.append(request)
        self.prompt_keys.append(prompt_key)
        self.loops.append(asyncio.get_running_loop())
        if not self._responses:
            raise AssertionError("the test scripted no more responses")
        return self._responses.pop(0)


def make_config(tmp_path: Path | None = None, /, **overrides: Any) -> AgentCoreConfig:
    """The packaged defaults with overrides merged in, recording under tmp_path."""
    document = load_config(environ={}).model_dump()
    if tmp_path is not None:
        document["replay"]["cassette_dir"] = tmp_path
    return AgentCoreConfig.model_validate(_merge(document, overrides))


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged
