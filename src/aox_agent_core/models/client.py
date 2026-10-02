"""AgentClient: the entry point for routed model calls."""

import asyncio
from collections.abc import Sequence
from typing import Any, TypeVar, overload

from pydantic import BaseModel, SecretStr

from aox_agent_core.config import AgentCoreConfig, Tier, load_config
from aox_agent_core.errors import EventLoopRunningError
from aox_agent_core.models.provider import ModelProvider
from aox_agent_core.models.router import Router
from aox_agent_core.models.types import CallResult, Message

OutputModelT = TypeVar("OutputModelT", bound=BaseModel)

Prompt = str | Sequence[Message]


class AgentClient:
    """Routes model calls by tier or task and returns output, usage and cost.

    Async first: use `await client.call(...)` in async code and
    `client.call_sync(...)` only in scripts. Building a client never contacts a
    provider; in replay mode, the default, it never needs an API key.

    Pass `output=SomeModel` to get a validated instance of that Pydantic model;
    a response that fails validation is retried up to `max_attempts` times before
    StructuredOutputError is raised.
    """

    def __init__(
        self,
        config: AgentCoreConfig | None = None,
        *,
        api_key: str | SecretStr | None = None,
        provider: ModelProvider | None = None,
        router: Router | None = None,
    ) -> None:
        self._config = config if config is not None else load_config()
        self._api_key = SecretStr(api_key) if isinstance(api_key, str) else api_key
        self._provider = provider
        self._router = router

    @property
    def config(self) -> AgentCoreConfig:
        return self._config

    @overload
    async def call(
        self,
        prompt: Prompt,
        *,
        output: None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
    ) -> CallResult[str]: ...

    @overload
    async def call(
        self,
        prompt: Prompt,
        *,
        output: type[OutputModelT],
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
    ) -> CallResult[OutputModelT]: ...

    async def call(
        self,
        prompt: Prompt,
        *,
        output: type[BaseModel] | None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
    ) -> CallResult[Any]:
        """Make one routed model call. Name a tier or a task, not both."""
        raise NotImplementedError("AgentClient.call is not implemented yet.")

    @overload
    def call_sync(
        self,
        prompt: Prompt,
        *,
        output: None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
    ) -> CallResult[str]: ...

    @overload
    def call_sync(
        self,
        prompt: Prompt,
        *,
        output: type[OutputModelT],
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
    ) -> CallResult[OutputModelT]: ...

    def call_sync(
        self,
        prompt: Prompt,
        *,
        output: type[BaseModel] | None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
    ) -> CallResult[Any]:
        """Blocking version of call() for scripts.

        Raises EventLoopRunningError when called from inside a running event loop
        (FastAPI, Jupyter and the like), where it would block that loop; use
        `await client.call(...)` there.
        """
        _raise_if_event_loop_running()
        return asyncio.run(
            self.call(
                prompt,
                output=output,
                tier=tier,
                task=task,
                system=system,
                max_tokens=max_tokens,
                max_attempts=max_attempts,
            )
        )


def _raise_if_event_loop_running() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise EventLoopRunningError(
        "call_sync() cannot run inside a running event loop; use 'await client.call(...)'."
    )
