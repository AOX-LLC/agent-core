"""Prompted calls through the client: rendering, attachments, replay keys, pricing, context."""

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, SecretStr

from aox_agent_core import (
    AgentClient,
    Attachment,
    Message,
    ModelClient,
    PromptRef,
    Provider,
    Role,
    RunContext,
    Tier,
)
from aox_agent_core.errors import (
    AttachmentError,
    BudgetExceededError,
    ConfigError,
    ReplayMissError,
)
from aox_agent_core.models import AnthropicProvider, ProviderRequest
from aox_agent_core.models.pricing import (
    IMAGE_TOKENS_ESTIMATE,
    PDF_PAGE_TOKENS_ESTIMATE,
    PDF_PAGES_CEILING,
    cost_of,
    estimate_attachment_tokens,
    worst_case_cost,
)
from aox_agent_core.replay import (
    DirectoryRecordingStore,
    PatternScrubber,
    RecordingProvider,
    replay_key,
    request_hash,
)
from aox_agent_core.tracing import attributes
from support import FIXTURES, SMALL_MODEL, ScriptedProvider, make_config, response

RECEIPT_JSON = '{"vendor": "Northwind", "total": "12.40"}'
PROMPT = PromptRef(
    id="receipts.extract",
    version=1,
    template="Extract the receipt from ${source}.",
    system="Reply with JSON only.",
)
PNG = Attachment.from_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
PDF = Attachment.from_bytes(
    b"%PDF-1.7\n1 0 obj << /Type /Pages /Kids [2 0 R 3 0 R] >>\n"
    b"2 0 obj << /Type /Page >>\n3 0 obj << /Type/Page >>\n%%EOF\n"
)


class Receipt(BaseModel):
    vendor: str
    total: str


def client(provider: Any, tmp_path: Path | None = None, **overrides: Any) -> AgentClient:
    return AgentClient(make_config(tmp_path, **overrides), provider=provider)


async def call_receipt(model_client: ModelClient, **arguments: Any) -> Any:
    """Depends on the protocol only, as a host would."""
    return await model_client.call(
        PROMPT, inputs={"source": "the scan"}, output=Receipt, tier=Tier.SMALL, **arguments
    )


# Rendering and argument rules


async def test_a_prompt_ref_is_rendered_with_its_own_system_prompt() -> None:
    provider = ScriptedProvider(response(RECEIPT_JSON))

    result = await call_receipt(client(provider))

    (sent,) = provider.requests
    assert sent.messages == (Message(role=Role.USER, content="Extract the receipt from the scan."),)
    assert sent.system == "Reply with JSON only."
    assert result.output == Receipt(vendor="Northwind", total="12.40")
    assert result.prompt_id == "receipts.extract"


@pytest.mark.parametrize(
    ("prompt", "arguments", "message"),
    [
        (PROMPT, {}, "needs inputs"),
        (PROMPT, {"inputs": {"source": "x"}, "system": "other"}, "own system prompt"),
        ("plain text", {"inputs": {"source": "x"}}, "only apply to a PromptRef"),
        (
            [Message(role=Role.USER, content="a"), Message(role=Role.ASSISTANT, content="b")],
            {"attachments": [PNG]},
            "must be a user turn",
        ),
    ],
    ids=["no-inputs", "system-override", "inputs-without-ref", "attachment-on-assistant"],
)
async def test_bad_argument_combinations_are_refused(
    prompt: Any, arguments: dict[str, Any], message: str
) -> None:
    provider = ScriptedProvider()

    with pytest.raises(ValueError, match=message):
        await client(provider).call(prompt, tier=Tier.SMALL, **arguments)

    assert provider.requests == []


async def test_an_attachment_without_bytes_is_refused_before_sending() -> None:
    provider = ScriptedProvider()

    with pytest.raises(AttachmentError, match="has no bytes"):
        await call_receipt(client(provider), attachments=[PNG.reference()])

    assert provider.requests == []


async def test_attachments_go_on_the_last_user_message() -> None:
    provider = ScriptedProvider(response("ok"))
    turns = [
        Message(role=Role.USER, content="first"),
        Message(role=Role.ASSISTANT, content="noted"),
        Message(role=Role.USER, content="now this"),
    ]

    await client(provider).call(turns, attachments=[PNG, PDF], tier=Tier.SMALL)

    (sent,) = provider.requests
    assert [len(message.attachments) for message in sent.messages] == [0, 0, 2]
    assert sent.messages[-1].attachments == (PNG, PDF)


# Replay keys


async def test_each_attempt_gets_its_own_prompt_key() -> None:
    provider = ScriptedProvider(response('{"vendor": 1}'), response(RECEIPT_JSON))

    result = await call_receipt(client(provider), attachments=[PDF])

    first, second = provider.prompt_keys
    assert first is not None
    assert second is not None
    assert (first.attempt, second.attempt) == (1, 2)
    assert first.tier is Tier.SMALL
    assert first.output_schema == "Receipt"
    assert first.attachments == (PDF.reference(),)
    assert first.key == replay_key(
        PROMPT,
        tier=Tier.SMALL,
        output_schema="Receipt",
        inputs={"source": "the scan"},
        attachments=[PDF],
    )
    assert result.replay_key == second.key


async def test_an_unprompted_call_reports_its_request_hash_as_its_key() -> None:
    provider = ScriptedProvider(response("hi"))

    result = await client(provider).call("hello", tier=Tier.SMALL)

    assert provider.prompt_keys == [None]
    assert result.replay_key == request_hash(provider.requests[0])
    assert result.prompt_id is None


async def test_an_attachment_call_replays_from_its_recording(tmp_path: Path) -> None:
    store = DirectoryRecordingStore(tmp_path, scrubber=PatternScrubber())
    recorder = RecordingProvider(ScriptedProvider(response(RECEIPT_JSON)), store, "default")
    recorded = await call_receipt(client(recorder, tmp_path), attachments=[PDF])

    replayed = await call_receipt(AgentClient(make_config(tmp_path)), attachments=[PDF])

    assert replayed.output == recorded.output
    assert replayed.replay_key == recorded.replay_key
    (path,) = (tmp_path / "prompts" / "receipts.extract" / "v1").glob("*.json")
    assert path.stem == recorded.replay_key
    assert "%PDF" not in path.read_text()


async def test_a_different_attachment_misses_and_never_goes_live(tmp_path: Path) -> None:
    store = DirectoryRecordingStore(tmp_path, scrubber=PatternScrubber())
    recorder = RecordingProvider(ScriptedProvider(response(RECEIPT_JSON)), store, "default")
    await call_receipt(client(recorder, tmp_path), attachments=[PDF])

    with pytest.raises(ReplayMissError) as caught:
        await call_receipt(AgentClient(make_config(tmp_path)), attachments=[PNG])

    assert caught.value.path.startswith(str(tmp_path / "prompts" / "receipts.extract" / "v1"))
    assert caught.value.key in caught.value.path


# Pricing


async def test_a_replayed_response_is_priced_at_its_recorded_model() -> None:
    config = make_config()
    provider = ScriptedProvider(response(RECEIPT_JSON, model="claude-sonnet-5-5"))

    result = await call_receipt(AgentClient(config, provider=provider))

    assert result.model == "claude-sonnet-5-5"
    sonnet = config.price_for(Provider.ANTHROPIC, "claude-sonnet-5-5")
    assert result.cost_usd == cost_of(result.usage, sonnet)
    assert result.cost_usd != cost_of(
        result.usage, config.price_for(Provider.ANTHROPIC, SMALL_MODEL)
    )


async def record_receipt(tmp_path: Path, reply: Any, **config: Any) -> None:
    store = DirectoryRecordingStore(tmp_path, scrubber=PatternScrubber())
    recorder = RecordingProvider(ScriptedProvider(reply), store, "default")
    await call_receipt(AgentClient(make_config(tmp_path, **config), provider=recorder))


async def test_a_replayed_alias_is_priced_at_the_recorded_request_model(tmp_path: Path) -> None:
    await record_receipt(tmp_path, response(RECEIPT_JSON, model="claude-haiku-latest"))
    config = make_config(tmp_path)

    result = await call_receipt(AgentClient(config))

    assert result.model == "claude-haiku-latest"
    assert result.cost_usd == cost_of(
        result.usage, config.price_for(Provider.ANTHROPIC, SMALL_MODEL)
    )


async def test_a_replay_is_priced_on_the_provider_it_was_recorded_with(tmp_path: Path) -> None:
    mid = "claude-sonnet-5-5"
    store = DirectoryRecordingStore(tmp_path, scrubber=PatternScrubber())
    recorder = RecordingProvider(ScriptedProvider(response(RECEIPT_JSON, model=mid)), store, "x")
    recording_client = AgentClient(make_config(tmp_path), provider=recorder)
    await recording_client.call(PROMPT, inputs={"source": "s"}, output=Receipt, tier=Tier.MID)
    # The mid tier has since moved to Bedrock, whose price table names it differently.
    moved = make_config(
        tmp_path,
        routing={"tiers": {"mid": {"provider": "bedrock", "model": "anthropic.claude-sonnet-5-5"}}},
    )

    result = await AgentClient(moved).call(
        PROMPT, inputs={"source": "s"}, output=Receipt, tier=Tier.MID
    )

    assert result.cost_usd == cost_of(result.usage, moved.price_for(Provider.ANTHROPIC, mid))


async def test_replaying_a_model_that_lost_its_price_is_a_config_error(tmp_path: Path) -> None:
    retired = "claude-retired-1"
    haiku_price = make_config().pricing[Provider.ANTHROPIC].models[SMALL_MODEL].model_dump()
    await record_receipt(
        tmp_path,
        response(RECEIPT_JSON, model=retired),
        routing={"tiers": {"small": {"model": retired}}},
        pricing={"anthropic": {"models": {retired: haiku_price}}},
    )

    with pytest.raises(ConfigError, match="claude-retired-1"):
        await call_receipt(AgentClient(make_config(tmp_path)))


async def test_a_live_alias_without_a_price_falls_back_to_the_route() -> None:
    config = make_config(mode="live")
    provider = ScriptedProvider(response(RECEIPT_JSON, model="claude-haiku-latest"))

    result = await call_receipt(AgentClient(config, api_key="test-key-not-real", provider=provider))

    assert result.cost_usd == cost_of(
        result.usage, config.price_for(Provider.ANTHROPIC, SMALL_MODEL)
    )


def test_attachment_estimates_count_images_and_pdf_pages() -> None:
    assert estimate_attachment_tokens([PNG]) == IMAGE_TOKENS_ESTIMATE
    assert estimate_attachment_tokens([PDF]) == 2 * PDF_PAGE_TOKENS_ESTIMATE
    assert estimate_attachment_tokens([PDF.reference()]) == 2 * PDF_PAGE_TOKENS_ESTIMATE
    recorded = Attachment.model_validate(PDF.reference().model_dump())
    assert estimate_attachment_tokens([recorded]) == PDF_PAGES_CEILING * PDF_PAGE_TOKENS_ESTIMATE


async def test_the_budget_counts_attachments() -> None:
    config = make_config()
    small = config.routing.tiers[Tier.SMALL]
    # Room for the text and a full reply, but not for a two-page PDF.
    price = config.price_for(Provider.ANTHROPIC, SMALL_MODEL)
    budget = worst_case_cost(1_000, small.max_tokens, price)
    provider = ScriptedProvider(response(RECEIPT_JSON), response(RECEIPT_JSON))
    budgeted = client(provider, routing={"budget_usd_per_call": str(budget)})

    await call_receipt(budgeted)
    with pytest.raises(BudgetExceededError):
        await call_receipt(budgeted, attachments=[PDF])

    assert len(provider.requests) == 1


async def test_strict_mode_budgets_every_pdf_at_the_page_ceiling() -> None:
    config = make_config()
    small = config.routing.tiers[Tier.SMALL]
    price = config.price_for(Provider.ANTHROPIC, SMALL_MODEL)
    # Room for the two counted pages, but not for 100.
    budget = worst_case_cost(20_000, small.max_tokens, price)
    counted = ScriptedProvider(response(RECEIPT_JSON))
    strict = ScriptedProvider()

    await call_receipt(
        client(counted, routing={"budget_usd_per_call": str(budget)}), attachments=[PDF]
    )
    with pytest.raises(BudgetExceededError):
        await call_receipt(
            client(strict, routing={"budget_usd_per_call": str(budget), "count_pdf_pages": False}),
            attachments=[PDF],
        )

    assert len(counted.requests) == 1
    assert strict.requests == []
    assert estimate_attachment_tokens([PDF], count_pdf_pages=False) == (
        PDF_PAGES_CEILING * PDF_PAGE_TOKENS_ESTIMATE
    )


# Run context


async def test_run_context_reaches_span_attributes_but_not_the_key(
    spans: InMemorySpanExporter,
) -> None:
    context = RunContext(
        run_id="run-0001", external_ids={"workflow_id": "wf-7", "execution_id": "ex-42"}
    )
    provider = ScriptedProvider(response(RECEIPT_JSON), response(RECEIPT_JSON))
    model_client = client(provider)

    with_context = await call_receipt(model_client, attachments=[PNG], context=context)
    without = await call_receipt(model_client, attachments=[PNG])

    first, _ = spans.get_finished_spans()
    span_attributes = dict(first.attributes or {})
    assert span_attributes[attributes.AGENT_CORE_RUN_ID] == "run-0001"
    assert span_attributes["agent_core.external_id.workflow_id"] == "wf-7"
    assert span_attributes["agent_core.external_id.execution_id"] == "ex-42"
    assert span_attributes[attributes.AGENT_CORE_PROMPT_ID] == "receipts.extract"
    assert span_attributes[attributes.AGENT_CORE_PROMPT_VERSION] == 1
    assert span_attributes[attributes.AGENT_CORE_REPLAY_KEY] == with_context.replay_key
    assert span_attributes[attributes.AGENT_CORE_ATTACHMENT_COUNT] == 1
    assert with_context.replay_key == without.replay_key
    assert "the scan" not in str(first.to_json())


async def test_a_context_matching_the_projects_secret_patterns_is_refused() -> None:
    provider = ScriptedProvider()
    patterned = client(provider, replay={"extra_secret_patterns": {"acme": r"acme_[a-f0-9]{16}"}})

    with pytest.raises(ValueError, match="secret patterns: acme"):
        await call_receipt(patterned, context=RunContext(run_id="acme_abcdefabcdef1234"))

    assert provider.requests == []


# The Anthropic provider


def provider_seeing(seen: list[httpx2.Request]) -> AnthropicProvider:
    body = json.loads((FIXTURES / "sdk" / "triage_valid.json").read_text())

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=body)

    return AnthropicProvider(
        api_key=SecretStr("test-key-not-real"),
        settings=make_config(anthropic={"max_retries": 0}).anthropic,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def attachment_request(*attached: Attachment) -> ProviderRequest:
    return ProviderRequest(
        provider=Provider.ANTHROPIC,
        model=SMALL_MODEL,
        messages=(Message(role=Role.USER, content="read these", attachments=attached),),
        max_tokens=100,
    )


async def test_attachments_are_sent_as_base64_blocks_before_the_text() -> None:
    seen: list[httpx2.Request] = []

    await provider_seeing(seen).complete(attachment_request(PNG, PDF))

    (message,) = json.loads(seen[0].content)["messages"]
    kinds = [
        (block["type"], block.get("source", {}).get("media_type")) for block in message["content"]
    ]
    assert kinds == [("image", "image/png"), ("document", "application/pdf"), ("text", None)]
    assert message["content"][0]["source"]["type"] == "base64"
    assert message["content"][-1]["text"] == "read these"


async def test_a_plain_message_is_still_sent_as_a_string() -> None:
    seen: list[httpx2.Request] = []

    await provider_seeing(seen).complete(attachment_request())

    (message,) = json.loads(seen[0].content)["messages"]
    assert message["content"] == "read these"


async def test_the_provider_refuses_an_attachment_without_bytes() -> None:
    seen: list[httpx2.Request] = []

    with pytest.raises(AttachmentError, match="can only be replayed"):
        await provider_seeing(seen).complete(attachment_request(PNG.reference()))

    assert seen == []
