from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from aox_agent_core import AgentClient, Mode, Role, Tier
from aox_agent_core.errors import (
    BudgetExceededError,
    ModelRefusalError,
    ReplayMissError,
    StructuredOutputError,
)
from aox_agent_core.replay import DirectoryRecordingStore, PatternScrubber, RecordingProvider
from aox_agent_core.tracing import attributes
from support import ScriptedProvider, make_config, response

VALID = '{"queue": "billing", "urgent": true}'
INVALID = '{"queue": "sales"}'
FAKE_KEY = "sk-ant-" + "k" * 24


class Triage(BaseModel):
    queue: Literal["billing", "technical"]
    urgent: bool


TASKS = {"routing": {"tasks": {"extraction": "small"}}}


def client(provider: ScriptedProvider, **config_overrides: Any) -> AgentClient:
    return AgentClient(make_config(**TASKS, **config_overrides), provider=provider)


async def test_text_call_returns_output_usage_and_cost() -> None:
    provider = ScriptedProvider(response("hi", input_tokens=1_000, output_tokens=200))

    result = await client(provider).call("hello", tier=Tier.SMALL)

    assert result.output == "hi"
    assert (result.tier, result.model, result.attempts) == (Tier.SMALL, provider_model(), 1)
    assert result.cost_usd == Decimal("0.002")  # 1000 x $1/M + 200 x $5/M
    assert result.mode is Mode.REPLAY


def provider_model() -> str:
    return "claude-haiku-4-5-20251001"


async def test_request_carries_routing_choices_and_the_schema() -> None:
    provider = ScriptedProvider(response(VALID))

    await client(provider).call("t", output=Triage, task="extraction", system="sys", max_tokens=50)

    (sent,) = provider.requests
    assert (sent.model, sent.max_tokens, sent.system) == (provider_model(), 50, "sys")
    assert sent.output_schema is not None
    assert sent.output_schema["additionalProperties"] is False


async def test_structured_output_is_validated() -> None:
    result = await client(ScriptedProvider(response(VALID))).call("t", output=Triage)

    assert result.output == Triage(queue="billing", urgent=True)


async def test_invalid_output_is_retried_with_feedback_and_costs_add_up() -> None:
    provider = ScriptedProvider(response(INVALID), response(VALID))

    result = await client(provider).call("t", output=Triage, tier=Tier.SMALL)

    assert result.attempts == 2
    assert result.usage.input_tokens == 200
    retry = provider.requests[1].messages
    assert [message.role for message in retry] == [Role.USER, Role.ASSISTANT, Role.USER]
    assert retry[1].content == INVALID
    assert "did not match the required JSON schema" in retry[2].content
    assert "sales" not in retry[2].content


async def test_exhausted_retries_raise_with_every_attempt() -> None:
    provider = ScriptedProvider(response(INVALID), response("not json"))

    with pytest.raises(StructuredOutputError, match="after 2 attempts") as caught:
        await client(provider).call("t", output=Triage, tier=Tier.SMALL)

    assert len(caught.value.attempts) == 2
    assert all("sales" not in attempt for attempt in caught.value.attempts)


async def test_escalates_one_tier_after_retries_run_out() -> None:
    provider = ScriptedProvider(
        response(INVALID), response(INVALID), response(VALID, model="claude-sonnet-5-5")
    )
    escalating = AgentClient(
        make_config(routing={"escalate_on_structured_failure": True}), provider=provider
    )

    result = await escalating.call("t", output=Triage, tier=Tier.SMALL)

    assert result.tier is Tier.MID
    assert result.attempts == 3
    assert [request.model for request in provider.requests] == [
        provider_model(),
        provider_model(),
        "claude-sonnet-5-5",
    ]
    assert len(provider.requests[2].messages) == 1


async def test_escalation_failure_reports_all_attempts() -> None:
    provider = ScriptedProvider(*(response(INVALID) for _ in range(4)))
    escalating = AgentClient(
        make_config(routing={"escalate_on_structured_failure": True}), provider=provider
    )

    with pytest.raises(StructuredOutputError, match="last tier mid") as caught:
        await escalating.call("t", output=Triage, tier=Tier.SMALL)

    assert len(caught.value.attempts) == 4


async def test_large_tier_has_nowhere_to_escalate() -> None:
    provider = ScriptedProvider(response(INVALID), response(INVALID))
    escalating = AgentClient(
        make_config(routing={"escalate_on_structured_failure": True}), provider=provider
    )

    with pytest.raises(StructuredOutputError, match="last tier large"):
        await escalating.call("t", output=Triage, tier=Tier.LARGE)


async def test_refusal_raises_at_once() -> None:
    provider = ScriptedProvider(response("", stop_reason="refusal"), response(VALID))

    with pytest.raises(ModelRefusalError):
        await client(provider).call("t", output=Triage)

    assert len(provider.requests) == 1


async def test_span_has_the_fixed_attributes_and_no_content(spans: InMemorySpanExporter) -> None:
    provider = ScriptedProvider(response(VALID, input_tokens=1_000, output_tokens=200))

    result = await client(provider).call("secret prompt", output=Triage, task="extraction")

    (span,) = spans.get_finished_spans()
    span_attributes = dict(span.attributes or {})
    assert span.name == attributes.SPAN_MODEL_CALL
    assert span_attributes[attributes.AGENT_CORE_TIER] == "small"
    assert span_attributes[attributes.AGENT_CORE_MODE] == "replay"
    assert span_attributes[attributes.GEN_AI_PROVIDER_NAME] == "anthropic"
    assert span_attributes[attributes.GEN_AI_USAGE_INPUT_TOKENS] == 1_000
    assert span_attributes[attributes.AGENT_CORE_COST_USD] == pytest.approx(0.002)
    assert span_attributes[attributes.AGENT_CORE_ROUTE_REASON] == "task extraction maps to small"
    assert result.trace_id == format(span.context.trace_id, "032x")
    assert "secret prompt" not in str(span.to_json())
    assert span.events == ()


async def test_content_capture_is_opt_in_and_scrubbed(spans: InMemorySpanExporter) -> None:
    provider = ScriptedProvider(response("done"))
    capturing = client(provider, tracing={"capture_content": True})

    await capturing.call(f"use {FAKE_KEY}")

    (span,) = spans.get_finished_spans()
    captured = {event.name: dict(event.attributes or {}) for event in span.events}
    assert captured["gen_ai.content.prompt"]["text"] == "use [REDACTED:anthropic_api_key]"
    assert captured["gen_ai.content.completion"]["text"] == "done"


async def test_failed_call_marks_the_span_as_an_error(spans: InMemorySpanExporter) -> None:
    with pytest.raises(ModelRefusalError):
        await client(ScriptedProvider(response("", stop_reason="refusal"))).call("t")

    (span,) = spans.get_finished_spans()
    assert span.status.description == "ModelRefusalError"
    assert dict(span.attributes or {})["error.type"] == "ModelRefusalError"
    assert span.events == ()


def test_call_sync_reuses_one_event_loop() -> None:
    provider = ScriptedProvider(response("a"), response("b"))

    with client(provider) as sync_client:
        sync_client.call_sync("one")
        sync_client.call_sync("two")

    assert provider.loops[0] is provider.loops[1]
    assert provider.loops[0].is_closed()


def test_replay_mode_serves_a_recorded_cassette(tmp_path: Path) -> None:
    config = make_config(tmp_path, replay={"cassette": "recorded"}, **TASKS)
    store = DirectoryRecordingStore(tmp_path, scrubber=PatternScrubber())
    recording = RecordingProvider(ScriptedProvider(response(VALID)), store, "recorded")
    with AgentClient(config, provider=recording) as recorder:
        recorder.call_sync("triage this", output=Triage, task="extraction")

    with AgentClient(config) as replayer:
        replayed = replayer.call_sync("triage this", output=Triage, task="extraction")
        assert replayed.output == Triage(queue="billing", urgent=True)
        with pytest.raises(ReplayMissError):
            replayer.call_sync("something never recorded")


def budgeted(provider: ScriptedProvider, budget: str, **routing: Any) -> AgentClient:
    config = make_config(routing={"budget_usd_per_call": budget, **routing})
    return AgentClient(config, provider=provider)


async def test_budget_covers_retries() -> None:
    # Small tier: worst case per attempt is 10,000 output tokens x $5/M = $0.05.
    expensive_failure = response(INVALID, input_tokens=100, output_tokens=5_000)
    provider = ScriptedProvider(expensive_failure, response(VALID))

    with pytest.raises(BudgetExceededError, match="Attempt 2 could cost"):
        await budgeted(provider, "0.06").call(
            "t", output=Triage, tier=Tier.SMALL, max_tokens=10_000
        )

    assert len(provider.requests) == 1


async def test_budget_covers_escalation() -> None:
    # Two small attempts spend $0.0004. The mid tier's worst case, $0.100002, fits
    # the budget alone but not on top of what was already spent.
    provider = ScriptedProvider(response(INVALID), response(INVALID), response(VALID))
    escalating = budgeted(provider, "0.1003", escalate_on_structured_failure=True)

    with pytest.raises(BudgetExceededError):
        await escalating.call("t", output=Triage, tier=Tier.SMALL, max_tokens=10_000)

    assert len(provider.requests) == 2


async def test_truncated_structured_output_is_not_retried() -> None:
    provider = ScriptedProvider(response('{"queue": "bil', stop_reason="max_tokens"))

    with pytest.raises(StructuredOutputError, match="after 1 attempts") as caught:
        await client(provider).call("t", output=Triage, max_tokens=5)

    assert caught.value.attempts == ("output cut off at max_tokens=5",)
    assert len(provider.requests) == 1


async def test_failed_escalated_call_reports_the_escalated_tier(
    spans: InMemorySpanExporter,
) -> None:
    provider = ScriptedProvider(
        response(INVALID), response(INVALID), response("", stop_reason="refusal")
    )
    escalating = AgentClient(
        make_config(routing={"escalate_on_structured_failure": True}), provider=provider
    )

    with pytest.raises(ModelRefusalError):
        await escalating.call("t", output=Triage, tier=Tier.SMALL)

    (span,) = spans.get_finished_spans()
    assert dict(span.attributes or {})[attributes.AGENT_CORE_TIER] == "mid"


async def test_task_is_recorded_on_the_span(spans: InMemorySpanExporter) -> None:
    await client(ScriptedProvider(response("ok"))).call("t", task="extraction")

    (span,) = spans.get_finished_spans()
    assert dict(span.attributes or {})[attributes.AGENT_CORE_TASK] == "extraction"


async def test_captured_content_never_shows_the_live_key(spans: InMemorySpanExporter) -> None:
    live_key = "plain-live-key-for-this-test"
    capturing = AgentClient(
        make_config(tracing={"capture_content": True}),
        api_key=live_key,
        provider=ScriptedProvider(response(f"echo {live_key}")),
    )

    await capturing.call(f"my key is {live_key}")

    (span,) = spans.get_finished_spans()
    assert live_key not in str(span.to_json())
