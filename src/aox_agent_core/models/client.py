"""AgentClient: the entry point for routed model calls."""

import asyncio
import time
import weakref
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, TypeVar, overload

import anthropic
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import BaseModel, SecretStr, ValidationError

from aox_agent_core.config import AgentCoreConfig, Mode, Provider, Tier, load_config
from aox_agent_core.credentials import resolve_api_key
from aox_agent_core.errors import (
    BudgetExceededError,
    EventLoopRunningError,
    ModelRefusalError,
    StructuredOutputError,
)
from aox_agent_core.models.live import LiveProviders
from aox_agent_core.models.pricing import cost_of, estimate_input_tokens
from aox_agent_core.models.provider import ModelProvider, close_provider
from aox_agent_core.models.router import ConfigRouter, RouteDecision, Router, RouteRequest
from aox_agent_core.models.types import (
    CallResult,
    Message,
    ProviderRequest,
    ProviderResponse,
    Role,
    Usage,
)
from aox_agent_core.replay.providers import RecordingProvider, ReplayProvider
from aox_agent_core.replay.scrub import PatternScrubber
from aox_agent_core.replay.store import DirectoryCassetteStore
from aox_agent_core.tracing import attributes, get_tracer

OutputModelT = TypeVar("OutputModelT", bound=BaseModel)

Prompt = str | Sequence[Message]

REFUSAL_STOP_REASON = "refusal"
GEN_AI_PROVIDER_NAMES = {Provider.ANTHROPIC: "anthropic", Provider.BEDROCK: "aws.bedrock"}


class AgentClient:
    """Routes model calls by tier or task and returns output, usage and cost.

    Async first: use `await client.call(...)` in async code and
    `client.call_sync(...)` only in scripts. In replay mode, the default, the
    client never needs an API key and never contacts a provider. In live and
    record mode it resolves the key when it is built, so a missing key fails
    before any SDK is touched.

    Pass `output=SomeModel` to get a validated instance of that Pydantic model;
    a response that fails validation is sent again, up to `max_attempts` tries in
    all. If routing.escalate_on_structured_failure is set, the call then moves up
    one tier and gets another `max_attempts` tries before StructuredOutputError
    is raised. A refusal raises ModelRefusalError at once. Usage and cost in the
    result cover every attempt.

    call_sync runs on one event loop the client keeps, so SDK connections are
    reused between calls. Call close() when done, or use the client as a context
    manager.
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
        self._api_key = resolve_api_key(api_key) if api_key is not None else None
        self._router = router if router is not None else ConfigRouter(self._config)
        self._provider = provider if provider is not None else self._provider_for_mode()
        self._runner: asyncio.Runner | None = None
        self._close_loop_on_collect: weakref.finalize[[], AgentClient] | None = None

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
        """Make one routed model call. Name a tier or a task, not both.

        Raises ValueError for `max_attempts` below 1, an empty message list, or
        both a tier and a task. Also raises BudgetExceededError, ModelRefusalError
        and StructuredOutputError as described on the class, and a ProviderError
        subclass when the provider fails.
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        messages = _as_messages(prompt)
        route_request = RouteRequest(
            tier=tier,
            task=task,
            estimated_input_tokens=estimate_input_tokens(
                system, *(message.content for message in messages)
            ),
            max_output_tokens=max_tokens,
        )
        decision = self._router.select(route_request)
        call = _CallInProgress(
            config=self._config,
            provider=self._provider,
            router=self._router,
            messages=messages,
            system=system,
            output=output,
            max_attempts=max_attempts,
        )

        # The span records failures itself, by class name only: an error message
        # can quote the request, and spans never carry content by default.
        with get_tracer().start_as_current_span(
            attributes.SPAN_MODEL_CALL, record_exception=False, set_status_on_exception=False
        ) as span:
            started = time.perf_counter()
            try:
                value, final_decision, response = await call.run(decision, route_request)
            except Exception as error:
                call.annotate(span, decision)
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(Status(StatusCode.ERROR, type(error).__name__))
                raise
            call.annotate(span, final_decision, response)
            self._capture_content(span, messages, response)

            return CallResult(
                output=value,
                tier=final_decision.tier,
                task=task,
                provider=final_decision.provider,
                model=response.model,
                mode=self._config.mode,
                usage=call.usage,
                cost_usd=call.cost_usd,
                latency_ms=(time.perf_counter() - started) * 1000,
                stop_reason=response.stop_reason,
                trace_id=_trace_id(span),
                attempts=call.attempts,
            )

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
        `await client.call(...)` there. Not safe to call from several threads.
        """
        _raise_if_event_loop_running()
        if self._runner is None:
            self._runner = asyncio.Runner()
            # A client dropped without close() still closes its loop when collected.
            # Closing the loop itself is safe even while another loop is running,
            # unlike Runner.close(), which would try to run this one.
            self._close_loop_on_collect = weakref.finalize(self, self._runner.get_loop().close)
        return self._runner.run(
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

    def close(self) -> None:
        """Release connections and close the event loop call_sync uses.

        The client stays usable afterwards; the next call opens new connections.
        From async code, use `await client.aclose()` instead.
        """
        if self._runner is None:
            return
        _raise_if_event_loop_running()
        if self._close_loop_on_collect is not None:
            self._close_loop_on_collect.detach()
        self._runner.run(close_provider(self._provider))
        self._runner.close()
        self._runner = None
        self._close_loop_on_collect = None

    async def aclose(self) -> None:
        """Release the connections opened on the running event loop."""
        await close_provider(self._provider)

    def __enter__(self) -> "AgentClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    async def __aenter__(self) -> "AgentClient":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    def _provider_for_mode(self) -> ModelProvider:
        if self._config.mode is Mode.REPLAY:
            return ReplayProvider(self._cassette_store(), self._config.replay.cassette)

        api_key = self._api_key if self._api_key is not None else resolve_api_key()
        live = LiveProviders(self._config, api_key=api_key)
        if self._config.mode is Mode.LIVE:
            return live
        return RecordingProvider(
            live, self._cassette_store(known_secret=api_key), self._config.replay.cassette
        )

    def _cassette_store(self, known_secret: SecretStr | None = None) -> DirectoryCassetteStore:
        replay = self._config.replay
        scrubber = PatternScrubber(
            extra_patterns=replay.extra_secret_patterns,
            known_secrets=(known_secret,) if known_secret is not None else (),
        )
        return DirectoryCassetteStore(
            replay.cassette_dir, scrubber=scrubber, on_secret=replay.on_secret
        )

    def _capture_content(
        self, span: Span, messages: tuple[Message, ...], response: ProviderResponse
    ) -> None:
        if not self._config.tracing.capture_content:
            return
        scrubber = PatternScrubber(extra_patterns=self._config.replay.extra_secret_patterns)
        prompt = "\n\n".join(message.content for message in messages)
        span.add_event("gen_ai.content.prompt", {"text": str(scrubber.redact(prompt))})
        span.add_event("gen_ai.content.completion", {"text": str(scrubber.redact(response.text))})


class _AttemptsExhaustedError(Exception):
    """Every structured attempt on one tier failed validation."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__(f"{len(failures)} attempts failed validation")
        self.failures = failures


class _CallInProgress:
    """One call's attempts, with usage and cost summed across all of them."""

    def __init__(
        self,
        *,
        config: AgentCoreConfig,
        provider: ModelProvider,
        router: Router,
        messages: tuple[Message, ...],
        system: str | None,
        output: type[BaseModel] | None,
        max_attempts: int,
    ) -> None:
        self._config = config
        self._provider = provider
        self._router = router
        self._messages = messages
        self._system = system
        self._output = output
        self._max_attempts = max_attempts
        self._output_schema = anthropic.transform_schema(output) if output is not None else None
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.cost_usd = Decimal(0)
        self.attempts = 0

    async def run(
        self, decision: RouteDecision, route_request: RouteRequest
    ) -> tuple[Any, RouteDecision, ProviderResponse]:
        try:
            value, response = await self._attempt_tier(decision)
            return value, decision, response
        except _AttemptsExhaustedError as first_tier:
            escalated = self._escalation(decision, route_request)
            if escalated is None:
                raise self._structured_error(decision, first_tier.failures) from None
            try:
                value, response = await self._attempt_tier(escalated)
            except _AttemptsExhaustedError as second_tier:
                failures = first_tier.failures + second_tier.failures
                raise self._structured_error(escalated, failures) from None
            return value, escalated, response

    async def _attempt_tier(self, decision: RouteDecision) -> tuple[Any, ProviderResponse]:
        conversation = list(self._messages)
        failures: list[str] = []
        for _ in range(self._max_attempts):
            response = await self._send(decision, conversation)
            if self._output is None:
                return response.text, response
            try:
                return self._output.model_validate_json(response.text), response
            except ValidationError as error:
                failure = _validation_summary(error)
                failures.append(failure)
                conversation += [
                    Message(role=Role.ASSISTANT, content=response.text or "(empty reply)"),
                    Message(
                        role=Role.USER,
                        content=(
                            f"That reply did not match the required JSON schema: {failure} "
                            "Reply again with only JSON that matches the schema."
                        ),
                    ),
                ]
        raise _AttemptsExhaustedError(failures)

    async def _send(self, decision: RouteDecision, conversation: list[Message]) -> ProviderResponse:
        config = self._config
        request = ProviderRequest(
            provider=decision.provider,
            model=decision.model,
            messages=tuple(conversation),
            max_tokens=decision.max_tokens,
            system=self._system,
            effort=config.routing.tiers[decision.tier].effort,
            output_schema=self._output_schema,
        )
        response = await self._provider.complete(request)

        self.attempts += 1
        self.usage = _add_usage(self.usage, response.usage)
        price = config.price_for(decision.provider, decision.model)
        self.cost_usd += cost_of(response.usage, price)
        if response.stop_reason == REFUSAL_STOP_REASON:
            raise ModelRefusalError(
                f"The model declined the request on tier {decision.tier.value} ({decision.model})."
            )
        return response

    def _escalation(
        self, decision: RouteDecision, route_request: RouteRequest
    ) -> RouteDecision | None:
        """Return a route one tier up, or None when escalation is off, impossible or over budget."""
        higher_tier = decision.tier.one_higher()
        if not self._config.routing.escalate_on_structured_failure or higher_tier is None:
            return None
        try:
            escalated = self._router.select(
                route_request.model_copy(update={"tier": higher_tier, "task": None})
            )
        except BudgetExceededError:
            return None
        if escalated.tier is decision.tier:
            return None
        return escalated.model_copy(
            update={
                "reason": f"{escalated.reason}; escalated after {self._max_attempts} "
                f"failed structured attempts on {decision.tier.value}"
            }
        )

    def _structured_error(
        self, decision: RouteDecision, failures: list[str]
    ) -> StructuredOutputError:
        return StructuredOutputError(
            f"No valid {self._output.__name__ if self._output else 'output'} after "
            f"{len(failures)} attempts (last tier {decision.tier.value}).",
            attempts=tuple(failures),
        )

    def annotate(
        self, span: Span, decision: RouteDecision, response: ProviderResponse | None = None
    ) -> None:
        """Set the span's attributes. Never prompt or completion text."""
        span.set_attributes(
            {
                attributes.GEN_AI_OPERATION_NAME: "chat",
                attributes.GEN_AI_PROVIDER_NAME: GEN_AI_PROVIDER_NAMES[decision.provider],
                attributes.GEN_AI_REQUEST_MODEL: decision.model,
                attributes.GEN_AI_REQUEST_MAX_TOKENS: decision.max_tokens,
                attributes.GEN_AI_USAGE_INPUT_TOKENS: self.usage.input_tokens,
                attributes.GEN_AI_USAGE_OUTPUT_TOKENS: self.usage.output_tokens,
                attributes.AGENT_CORE_CACHE_CREATION_INPUT_TOKENS: (
                    self.usage.cache_creation_input_tokens
                ),
                attributes.AGENT_CORE_CACHE_READ_INPUT_TOKENS: self.usage.cache_read_input_tokens,
                attributes.AGENT_CORE_TIER: decision.tier.value,
                attributes.AGENT_CORE_REQUESTED_TIER: decision.requested_tier.value,
                attributes.AGENT_CORE_MODE: self._config.mode.value,
                attributes.AGENT_CORE_ROUTE_REASON: decision.reason,
                attributes.AGENT_CORE_COST_USD: float(self.cost_usd),
                attributes.AGENT_CORE_STRUCTURED_ATTEMPTS: self.attempts,
            }
        )
        if response is not None:
            span.set_attribute(attributes.GEN_AI_RESPONSE_MODEL, response.model)
            span.set_attribute(attributes.GEN_AI_RESPONSE_FINISH_REASONS, [response.stop_reason])


def _as_messages(prompt: Prompt) -> tuple[Message, ...]:
    if isinstance(prompt, str):
        return (Message(role=Role.USER, content=prompt),)
    messages = tuple(prompt)
    if not messages:
        raise ValueError("prompt has no messages")
    return messages


def _add_usage(total: Usage, more: Usage) -> Usage:
    return Usage(
        input_tokens=total.input_tokens + more.input_tokens,
        output_tokens=total.output_tokens + more.output_tokens,
        cache_creation_input_tokens=(
            total.cache_creation_input_tokens + more.cache_creation_input_tokens
        ),
        cache_read_input_tokens=total.cache_read_input_tokens + more.cache_read_input_tokens,
    )


def _validation_summary(error: ValidationError) -> str:
    """Describe what failed without echoing the model's output."""
    problems = [
        f"{'.'.join(str(part) for part in detail['loc']) or '<root>'}: {detail['msg']}"
        for detail in error.errors(include_input=False, include_url=False)[:5]
    ]
    return "; ".join(problems) + "."


def _trace_id(span: Span) -> str | None:
    context = span.get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def _raise_if_event_loop_running() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise EventLoopRunningError(
        "call_sync() cannot run inside a running event loop; use 'await client.call(...)'."
    )
