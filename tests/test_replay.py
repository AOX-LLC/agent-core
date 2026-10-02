"""Replay keys, format 2 recordings and the replay and recording providers."""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from aox_agent_core import Message, Provider, Role, Tier
from aox_agent_core.config import SecretAction
from aox_agent_core.errors import (
    CassetteFormatError,
    ReplayMissError,
    SecretInRecordingError,
    StaleRecordingError,
)
from aox_agent_core.models import ProviderRequest
from aox_agent_core.models.attachments import Attachment
from aox_agent_core.models.prompts import PromptRef
from aox_agent_core.replay import (
    DirectoryRecordingStore,
    PatternScrubber,
    PromptKey,
    RecordingProvider,
    ReplayProvider,
    replay_key,
    request_hash,
)
from support import ScriptedProvider, response

FAKE_KEY = "sk-ant-" + "q" * 24
PROMPT = PromptRef(id="receipts.extract", version=3, template="Extract: ${text}")
PNG = Attachment.from_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 16)


def request(content: str = "hello", **fields: object) -> ProviderRequest:
    return ProviderRequest.model_validate(
        {
            "provider": Provider.ANTHROPIC,
            "model": "claude-haiku-4-5-20251001",
            "messages": (Message(role=Role.USER, content=content),),
            "max_tokens": 100,
            **fields,
        }
    )


def prompt_key(prompt: PromptRef = PROMPT, **overrides: Any) -> PromptKey:
    arguments: dict[str, Any] = {
        "tier": Tier.SMALL,
        "output_schema": "Receipt",
        "output_json_schema": {"type": "object"},
        "inputs": {"text": "Total 12.40"},
        "attachments": (),
        "attempt": 1,
        **overrides,
    }
    return PromptKey.for_call(prompt, **arguments)


def store(tmp_path: Path, on_secret: SecretAction = SecretAction.REFUSE) -> DirectoryRecordingStore:
    return DirectoryRecordingStore(tmp_path, scrubber=PatternScrubber(), on_secret=on_secret)


# Keys


def test_request_hash_is_canonical_json_without_default_fields() -> None:
    canonical = json.dumps(
        request().model_dump(mode="json", exclude_defaults=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    assert request_hash(request()) == hashlib.sha256(canonical.encode()).hexdigest()
    assert request_hash(request()) == request_hash(request(system=None))
    assert request_hash(request()) != request_hash(request("hello!"))


def test_attachments_enter_a_request_hash_by_reference() -> None:
    with_bytes = Message(role=Role.USER, content="see", attachments=(PNG,))
    without_bytes = Message(role=Role.USER, content="see", attachments=(PNG.reference(),))

    assert request_hash(request(messages=(with_bytes,))) == request_hash(
        request(messages=(without_bytes,))
    )
    assert request_hash(request(messages=(with_bytes,))) != request_hash(request("see"))


def test_replay_key_matches_the_prompt_key_of_the_same_call() -> None:
    key = replay_key(
        PROMPT, tier=Tier.SMALL, output_schema="Receipt", inputs={"text": "Total 12.40"}
    )

    assert key == prompt_key().key
    assert len(key) == 64


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": PROMPT.model_copy(update={"id": "receipts.other"})},
        {"prompt": PROMPT.model_copy(update={"version": 4})},
        {"tier": Tier.MID},
        {"output_schema": "Invoice"},
        {"inputs": {"text": "Total 99.00"}},
        {"attachments": (PNG,)},
        {"attempt": 2},
    ],
    ids=["prompt-id", "version", "tier", "schema-name", "inputs", "attachments", "attempt"],
)
def test_each_part_of_a_prompted_call_changes_its_key(change: dict[str, Any]) -> None:
    assert prompt_key(**change).key != prompt_key().key


def test_template_system_and_schema_text_do_not_change_the_key() -> None:
    edited = PROMPT.model_copy(update={"template": "Now read: ${text}", "system": "be brief"})

    assert prompt_key(edited, output_json_schema={"type": "string"}).key == prompt_key().key


def test_inputs_are_compared_in_unicode_nfc_form() -> None:
    composed = prompt_key(inputs={"text": "café"})
    decomposed = prompt_key(inputs={"text": "café"})

    assert composed.key == decomposed.key


# Prompted recordings


async def test_prompted_call_records_one_content_addressed_file(tmp_path: Path) -> None:
    key = prompt_key(attachments=(PNG,))
    recorder = RecordingProvider(ScriptedProvider(response("first")), store(tmp_path), "default")

    await recorder.complete(request(), prompt_key=key)

    path = tmp_path / "prompts" / "receipts.extract" / "v3" / f"{key.key}.json"
    document = json.loads(path.read_text())
    assert document["format_version"] == 2
    assert document["prompt"]["attachments"] == [
        {"media_type": "image/png", "sha256": PNG.sha256, "size_bytes": PNG.size_bytes}
    ]
    assert "data" not in path.read_text()


async def test_repeated_prompted_calls_replay_the_same_recording(tmp_path: Path) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("answer")), store(tmp_path), "default")
    await recorder.complete(request(), prompt_key=prompt_key())
    replay = ReplayProvider(store(tmp_path), "default")

    first = await replay.complete(request("rendered differently"), prompt_key=prompt_key())
    second = await replay.complete(request(), prompt_key=prompt_key())

    assert first.text == second.text == "answer"


async def test_a_miss_names_the_key_and_path_and_never_goes_live(tmp_path: Path) -> None:
    replay = ReplayProvider(store(tmp_path), "default")
    key = prompt_key()

    with pytest.raises(ReplayMissError, match=key.key) as caught:
        await replay.complete(request(), prompt_key=key)

    expected = tmp_path / "prompts" / "receipts.extract" / "v3" / f"{key.key}.json"
    assert caught.value.key == key.key
    assert caught.value.path == str(expected)
    assert str(expected) in str(caught.value)


async def test_a_miss_says_when_the_routed_tier_differs_from_the_recorded_one(
    tmp_path: Path,
) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("small")), store(tmp_path), "default")
    await recorder.complete(request(), prompt_key=prompt_key(tier=Tier.MID))
    replay = ReplayProvider(store(tmp_path), "default")

    with pytest.raises(
        ReplayMissError, match="routed to the small tier, but it was recorded on mid"
    ):
        await replay.complete(request(), prompt_key=prompt_key(tier=Tier.SMALL))


@pytest.mark.parametrize(
    ("changed_prompt", "changed_schema", "named"),
    [
        (PROMPT.model_copy(update={"template": "Read: ${text}"}), {"type": "object"}, "template"),
        (PROMPT.model_copy(update={"system": "be brief"}), {"type": "object"}, "system prompt"),
        (PROMPT, {"type": "object", "title": "Receipt"}, "output schema"),
    ],
)
async def test_an_edit_without_a_version_bump_is_stale_not_silent(
    tmp_path: Path, changed_prompt: PromptRef, changed_schema: dict[str, JsonValue], named: str
) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("old")), store(tmp_path), "default")
    await recorder.complete(request(), prompt_key=prompt_key())
    replay = ReplayProvider(store(tmp_path), "default")

    with pytest.raises(StaleRecordingError, match=f"different {named}"):
        await replay.complete(
            request(), prompt_key=prompt_key(changed_prompt, output_json_schema=changed_schema)
        )


async def test_a_schema_only_change_points_at_a_dependency_upgrade(tmp_path: Path) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("old")), store(tmp_path), "default")
    await recorder.complete(request(), prompt_key=prompt_key())
    replay = ReplayProvider(store(tmp_path), "default")

    with pytest.raises(StaleRecordingError, match="upgrading anthropic or pydantic"):
        await replay.complete(
            request(), prompt_key=prompt_key(output_json_schema={"type": "object", "x": 1})
        )


async def test_a_broken_sibling_file_does_not_hide_the_miss(tmp_path: Path) -> None:
    version_directory = tmp_path / "prompts" / "receipts.extract" / "v3"
    version_directory.mkdir(parents=True)
    (version_directory / "garbage.json").write_text("{not json")
    replay = ReplayProvider(store(tmp_path), "default")

    with pytest.raises(ReplayMissError) as caught:
        await replay.complete(request(), prompt_key=prompt_key())

    assert caught.value.key == prompt_key().key


async def test_a_copied_prompted_recording_is_refused(tmp_path: Path) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("a")), store(tmp_path), "default")
    await recorder.complete(request(), prompt_key=prompt_key())
    other = prompt_key(inputs={"text": "Total 99.00"})
    shutil.copy(store(tmp_path).prompt_path(prompt_key()), store(tmp_path).prompt_path(other))

    with pytest.raises(CassetteFormatError, match="copied or renamed"):
        await ReplayProvider(store(tmp_path), "default").complete(request(), prompt_key=other)


async def test_a_renamed_unprompted_recording_is_refused(tmp_path: Path) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("a")), store(tmp_path), "seq")
    await recorder.complete(request())
    (path,) = (tmp_path / "requests" / "seq").glob("*.json")
    path.rename(path.with_name(f"{request_hash(request('other'))}.0.json"))

    with pytest.raises(CassetteFormatError, match="copied or renamed"):
        await ReplayProvider(store(tmp_path), "seq").complete(request("other"))


# Unprompted recordings


async def test_unprompted_round_trip_keeps_sequences(tmp_path: Path) -> None:
    live = ScriptedProvider(response("first"), response("second"), response("other"))
    recorder = RecordingProvider(live, store(tmp_path), "round-trip")
    await recorder.complete(request())
    await recorder.complete(request())
    await recorder.complete(request("different"))

    replay = ReplayProvider(store(tmp_path), "round-trip")

    assert (await replay.complete(request())).text == "first"
    assert (await replay.complete(request("different"))).text == "other"
    assert (await replay.complete(request())).text == "second"
    with pytest.raises(ReplayMissError, match="call 3") as caught:
        await replay.complete(request())
    assert caught.value.path.endswith(f"requests/round-trip/{request_hash(request())}.2.json")


async def test_recording_files_are_sorted_json_written_atomically(tmp_path: Path) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("a")), store(tmp_path), "seq")
    await recorder.complete(request())

    (path,) = (tmp_path / "requests" / "seq").glob("*.json")
    text = path.read_text()
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert path.stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.rglob(".*.tmp"))


# Format and secrets


def test_a_format_1_cassette_is_rejected_with_a_way_forward(tmp_path: Path) -> None:
    old = tmp_path / "requests" / "default" / "x.0.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"format_version": 1, "name": "x", "entries": []}))

    with pytest.raises(CassetteFormatError, match=r"format 1 .* record it again"):
        store(tmp_path).load(old)


def test_a_missing_recording_loads_as_none(tmp_path: Path) -> None:
    assert store(tmp_path).load(tmp_path / "absent.json") is None


async def test_a_secret_in_a_response_refuses_the_write(tmp_path: Path) -> None:
    recorder = RecordingProvider(
        ScriptedProvider(response(f"the key is {FAKE_KEY}")), store(tmp_path), "leaky"
    )

    with pytest.raises(
        SecretInRecordingError, match=r"anthropic_api_key at \$\.response"
    ) as caught:
        await recorder.complete(request())

    assert FAKE_KEY not in str(caught.value)
    assert not list(tmp_path.rglob("*.json"))


async def test_a_secret_in_prompt_inputs_refuses_the_write(tmp_path: Path) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("ok")), store(tmp_path), "default")

    with pytest.raises(SecretInRecordingError, match=r"\$\.prompt\.inputs"):
        await recorder.complete(request(), prompt_key=prompt_key(inputs={"text": FAKE_KEY}))


async def test_redact_mode_writes_the_recording_redacted(tmp_path: Path) -> None:
    recorder = RecordingProvider(
        ScriptedProvider(response(f"the key is {FAKE_KEY}")),
        store(tmp_path, SecretAction.REDACT),
        "redacted",
    )
    await recorder.complete(request())

    (path,) = tmp_path.rglob("*.json")
    assert FAKE_KEY not in path.read_text()
    assert "[REDACTED:anthropic_api_key]" in path.read_text()


async def test_hash_like_extra_patterns_do_not_match_structural_fields(tmp_path: Path) -> None:
    hex_pattern_store = DirectoryRecordingStore(
        tmp_path, scrubber=PatternScrubber(extra_patterns={"hex_token": r"\b[0-9a-f]{32,}\b"})
    )
    recorder = RecordingProvider(ScriptedProvider(response("fine")), hex_pattern_store, "hex")

    await recorder.complete(request(), prompt_key=prompt_key(attachments=(PNG,)))

    assert list(tmp_path.rglob("*.json"))


class _BrokenRedactor(PatternScrubber):
    def redact(self, value: JsonValue) -> JsonValue:
        return 42


async def test_redaction_that_breaks_the_recording_is_refused(tmp_path: Path) -> None:
    broken = DirectoryRecordingStore(
        tmp_path, scrubber=_BrokenRedactor(), on_secret=SecretAction.REDACT
    )
    recorder = RecordingProvider(ScriptedProvider(response(f"key {FAKE_KEY}")), broken, "broken")

    with pytest.raises(SecretInRecordingError, match="left it invalid"):
        await recorder.complete(request())

    assert not list(tmp_path.rglob("*.json"))
