"""AgentClient: the entry point for routed model calls."""

import asyncio
import time
import weakref
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Protocol, TypeVar, overload

import anthropic
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import BaseModel, JsonValue, SecretStr, ValidationError

from aox_agent_core.config import AgentCoreConfig, Mode, ModelPrice, Provider, Tier, load_config
from aox_agent_core.context import RunContext
from aox_agent_core.credentials import resolve_api_key
from aox_agent_core.errors import (
    AttachmentError,
    BudgetExceededError,
    ConfigError,
    EventLoopRunningError,
    ModelRefusalError,
    StructuredOutputError,
)
from aox_agent_core.models.attachments import Attachment
from aox_agent_core.models.live import LiveProviders
from aox_agent_core.models.pricing import (
    cost_of,
    estimate_attachment_tokens,
    estimate_input_tokens,
    worst_case_cost,
)
from aox_agent_core.models.prompts import PromptRef
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
from aox_agent_core.replay.keys import PromptKey, request_hash
from aox_agent_core.replay.providers import RecordingProvider, ReplayProvider
from aox_agent_core.replay.scrub import PatternScrubber
from aox_agent_core.replay.store import DirectoryRecordingStore
from aox_agent_core.tracing import attributes, get_tracer

OutputModelT = TypeVar("OutputModelT", bound=BaseModel)

Prompt = str | Sequence[Message]

REFUSAL_STOP_REASON = "refusal"
MAX_TOKENS_STOP_REASON = "max_tokens"
GEN_AI_PROVIDER_NAMES = {Provider.ANTHROPIC: "anthropic", Provider.BEDROCK: "aws.bedrock"}


class ModelClient(Protocol):
    """What a host depends on to make model calls: AgentClient, or a test double.

    Type a dependency as ModelClient rather than AgentClient, and a fake with
    the same `call` can stand in for it.
    """

    @overload
    async def call(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[str]: ...

    @overload
    async def call(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: type[OutputModelT],
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[OutputModelT]: ...


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
        self._live_key: SecretStr | None = None
        self._provider = provider if provider is not None else self._provider_for_mode()
        self._runner: asyncio.Runner | None = None
        self._close_loop_on_collect: weakref.finalize[[], AgentClient] | None = None

    @property
    def config(self) -> AgentCoreConfig:
        return self._config

    @overload
    async def call(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[str]: ...

    @overload
    async def call(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: type[OutputModelT],
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[OutputModelT]: ...

    async def call(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: type[BaseModel] | None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[Any]:
        """Make one routed model call. Name a tier or a task, not both.

        `prompt` is text, a list of messages, or a PromptRef. A PromptRef is
        rendered from `inputs` (required with it, refused without), carries its
        own system prompt (so `system` must be None), and keys replay by content:
        prompt id and version, routed tier, schema name, inputs, attachment
        hashes and attempt. `attachments` go on the last user message. `context`
        reaches span attributes, never the replay key.

        Raises ValueError for `max_attempts` below 1, an empty message list, or
        both a tier and a task. Also raises BudgetExceededError, ModelRefusalError
        and StructuredOutputError as described on the class, and a ProviderError
        subclass when the provider fails.
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        prompt_ref = prompt if isinstance(prompt, PromptRef) else None
        messages, system = _prepare_prompt(prompt, inputs, attachments, system)
        route_request = RouteRequest(
            tier=tier,
            task=task,
            estimated_input_tokens=estimate_input_tokens(
                system, *(message.content for message in messages)
            )
            + estimate_attachment_tokens(_attachments_in(messages)),
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
            task=task,
            prompt_ref=prompt_ref,
            inputs=inputs or {},
            attachments=tuple(attachments),
            context=context,
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
                call.annotate(span, call.current_decision or decision)
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
                replay_key=call.replay_key,
                prompt_id=prompt_ref.id if prompt_ref is not None else None,
            )

    @overload
    def call_sync(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[str]: ...

    @overload
    def call_sync(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: type[OutputModelT],
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[OutputModelT]: ...

    def call_sync(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: type[BaseModel] | None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
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
                inputs=inputs,
                attachments=attachments,
                output=output,
                tier=tier,
                task=task,
                system=system,
                max_tokens=max_tokens,
                max_attempts=max_attempts,
                context=context,
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
        self._live_key = api_key
        live = LiveProviders(self._config, api_key=api_key)
        if self._config.mode is Mode.LIVE:
            return live
        return RecordingProvider(
            live, self._cassette_store(known_secret=api_key), self._config.replay.cassette
        )

    def _cassette_store(self, known_secret: SecretStr | None = None) -> DirectoryRecordingStore:
        replay = self._config.replay
        scrubber = PatternScrubber(
            extra_patterns=replay.extra_secret_patterns,
            known_secrets=(known_secret,) if known_secret is not None else (),
        )
        return DirectoryRecordingStore(
            replay.cassette_dir, scrubber=scrubber, on_secret=replay.on_secret
        )

    def _capture_content(
        self, span: Span, messages: tuple[Message, ...], response: ProviderResponse
    ) -> None:
        if not self._config.tracing.capture_content:
            return
        scrubber = PatternScrubber(
            extra_patterns=self._config.replay.extra_secret_patterns,
            known_secrets=tuple(key for key in (self._live_key, self._api_key) if key is not None),
        )
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
        task: str | None,
        prompt_ref: PromptRef | None = None,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: tuple[Attachment, ...] = (),
        context: RunContext | None = None,
    ) -> None:
        self._config = config
        self._provider = provider
        self._router = router
        self._task = task
        self._messages = messages
        self._system = system
        self._output = output
        self._max_attempts = max_attempts
        self._output_schema = anthropic.transform_schema(output) if output is not None else None
        self._prompt_ref = prompt_ref
        self._inputs = dict(inputs or {})
        self._attachments = attachments
        self._context = context
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.cost_usd = Decimal(0)
        self.attempts = 0
        self.current_decision: RouteDecision | None = None
        self.replay_key = ""

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
        self.current_decision = decision
        conversation = list(self._messages)
        failures: list[str] = []
        for attempt in range(1, self._max_attempts + 1):
            response = await self._send(decision, conversation, attempt)
            if self._output is None:
                return response.text, response
            if response.stop_reason == MAX_TOKENS_STOP_REASON:
                # Retrying with the same limit would be cut off again.
                raise self._structured_error(
                    decision,
                    [*failures, f"output cut off at max_tokens={decision.max_tokens}"],
                )
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

    async def _send(
        self, decision: RouteDecision, conversation: list[Message], attempt: int
    ) -> ProviderResponse:
        config = self._config
        # Priced before sending, so a misconfigured route fails before it costs anything.
        route_price = config.price_for(decision.provider, decision.model)
        request = ProviderRequest(
            provider=decision.provider,
            model=decision.model,
            messages=tuple(conversation),
            max_tokens=decision.max_tokens,
            system=self._system,
            effort=config.routing.tiers[decision.tier].effort,
            output_schema=self._output_schema,
        )
        self._refuse_over_budget(request, route_price)
        prompt_key = self._prompt_key(decision, attempt)
        self.replay_key = prompt_key.key if prompt_key is not None else request_hash(request)
        response = await self._provider.complete(request, prompt_key=prompt_key)

        self.attempts += 1
        self.usage = _add_usage(self.usage, response.usage)
        self.cost_usd += cost_of(response.usage, self._response_price(decision, response))
        if response.stop_reason == REFUSAL_STOP_REASON:
            raise ModelRefusalError(
                f"The model declined the request on tier {decision.tier.value} ({decision.model})."
            )
        return response

    def _prompt_key(self, decision: RouteDecision, attempt: int) -> PromptKey | None:
        if self._prompt_ref is None:
            return None
        return PromptKey.for_call(
            self._prompt_ref,
            tier=decision.tier,
            output_schema=self._output.__name__ if self._output is not None else None,
            output_json_schema=self._output_schema,
            inputs=self._inputs,
            attachments=self._attachments,
            attempt=attempt,
        )

    def _response_price(self, decision: RouteDecision, response: ProviderResponse) -> ModelPrice:
        """The price of the model that answered, falling back to the model that was asked.

        A replayed response may come from a model other than today's route, for
        example after a tier's model changes, so it is priced as recorded: at
        its own model's rate on the provider it was recorded with, or, when the
        API answered with a name the price table lacks (an alias, a dated ID),
        at the rate of the model the recorded request named. ConfigError if
        neither has a price any more. A live response is priced the same way
        against today's route.
        """
        config = self._config
        provider = response.recorded_provider or decision.provider
        requested = response.recorded_model or decision.model
        for model in (response.model, requested):
            if config.has_price(provider, model):
                return config.price_for(provider, model)
        raise ConfigError(
            f"Cannot price the response: neither {response.model!r} nor {requested!r} has a "
            f"{provider.value} price configured."
        )

    def _refuse_over_budget(self, request: ProviderRequest, price: ModelPrice) -> None:
        """Raise BudgetExceededError if this attempt could take the call over its budget.

        The budget covers the whole call: every retry and an escalation included.
        """
        budget = self._config.routing.budget_usd_per_call
        if budget is None:
            return
        input_tokens = estimate_input_tokens(
            request.system, *(message.content for message in request.messages)
        ) + estimate_attachment_tokens(_attachments_in(request.messages))
        worst_case = worst_case_cost(input_tokens, request.max_tokens, price)
        if self.cost_usd + worst_case > budget:
            raise BudgetExceededError(
                f"Attempt {self.attempts + 1} could cost up to ${worst_case}; with "
                f"${self.cost_usd} already spent that is over the per-call budget ${budget}."
            )

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
        if self._task is not None:
            span.set_attribute(attributes.AGENT_CORE_TASK, self._task)
        if self.replay_key:
            span.set_attribute(attributes.AGENT_CORE_REPLAY_KEY, self.replay_key)
        if self._prompt_ref is not None:
            span.set_attribute(attributes.AGENT_CORE_PROMPT_ID, self._prompt_ref.id)
            span.set_attribute(attributes.AGENT_CORE_PROMPT_VERSION, self._prompt_ref.version)
        if self._attachments:
            span.set_attribute(attributes.AGENT_CORE_ATTACHMENT_COUNT, len(self._attachments))
        if self._context is not None:
            span.set_attributes(context_attributes(self._context))
        if response is not None:
            span.set_attribute(attributes.GEN_AI_RESPONSE_MODEL, response.model)
            span.set_attribute(attributes.GEN_AI_RESPONSE_FINISH_REASONS, [response.stop_reason])


def context_attributes(context: RunContext) -> dict[str, str]:
    """Span attributes for a run context: its run ID and each external ID."""
    values = {attributes.AGENT_CORE_RUN_ID: context.run_id}
    for name, value in context.external_ids.items():
        values[attributes.AGENT_CORE_EXTERNAL_ID_PREFIX + name] = value
    return values


def _prepare_prompt(
    prompt: Prompt | PromptRef,
    inputs: Mapping[str, JsonValue] | None,
    attachments: Sequence[Attachment],
    system: str | None,
) -> tuple[tuple[Message, ...], str | None]:
    """The call's messages and system prompt, with attachments on the last user turn."""
    if isinstance(prompt, PromptRef):
        if inputs is None:
            raise ValueError("A PromptRef call needs inputs; pass inputs={} if it has none.")
        if system is not None:
            raise ValueError("A PromptRef carries its own system prompt; do not pass system.")
        messages: tuple[Message, ...] = (Message(role=Role.USER, content=prompt.render(inputs)),)
        system = prompt.system
    else:
        if inputs is not None:
            raise ValueError("inputs only apply to a PromptRef prompt.")
        messages = _as_messages(prompt)
    if not attachments:
        return messages, system
    for attachment in attachments:
        if attachment.data is None:
            raise AttachmentError(
                f"Attachment {attachment.sha256} has no bytes; build it with "
                "Attachment.from_bytes() or Attachment.from_path()."
            )
    last = messages[-1]
    if last.role is not Role.USER:
        raise ValueError("Attachments go on the last message, which must be a user turn.")
    with_attachments = last.model_copy(update={"attachments": (*last.attachments, *attachments)})
    return (*messages[:-1], with_attachments), system


def _attachments_in(messages: Sequence[Message]) -> list[Attachment]:
    return [attachment for message in messages for attachment in message.attachments]


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
