import gc
import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from aox_agent_core import Message, Provider, Role
from aox_agent_core.config import SecretAction
from aox_agent_core.errors import (
    CassetteConflictError,
    CassetteFormatError,
    ReplayMissError,
    SecretInRecordingError,
)
from aox_agent_core.models import ProviderRequest
from aox_agent_core.replay import (
    Cassette,
    DirectoryCassetteStore,
    PatternScrubber,
    RecordingProvider,
    ReplayProvider,
    request_hash,
)
from support import ScriptedProvider, response

FAKE_KEY = "sk-ant-" + "q" * 24


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


def store(tmp_path: Path, on_secret: SecretAction = SecretAction.REFUSE) -> DirectoryCassetteStore:
    return DirectoryCassetteStore(tmp_path, scrubber=PatternScrubber(), on_secret=on_secret)


def test_request_key_is_stable_and_ignores_unset_fields() -> None:
    assert request_hash(request()) == request_hash(request())
    assert request_hash(request()) == request_hash(request(system=None))
    assert request_hash(request()) != request_hash(request("hello!"))
    assert request_hash(request()) != request_hash(request(system="be brief"))


def test_request_key_is_sha256_of_canonical_json() -> None:
    import hashlib

    canonical = json.dumps(
        request().model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    assert request_hash(request()) == hashlib.sha256(canonical.encode()).hexdigest()


async def test_record_then_replay_round_trip(tmp_path: Path) -> None:
    live = ScriptedProvider(response("first"), response("second"), response("other"))
    recorder = RecordingProvider(live, store(tmp_path), "round-trip")
    await recorder.complete(request())
    await recorder.complete(request())
    await recorder.complete(request("different"))

    replay = ReplayProvider(store(tmp_path), "round-trip")

    assert (await replay.complete(request())).text == "first"
    assert (await replay.complete(request("different"))).text == "other"
    assert (await replay.complete(request())).text == "second"
    with pytest.raises(ReplayMissError, match="call 3"):
        await replay.complete(request())


async def test_cassette_file_is_sorted_json_with_sequence_numbers(tmp_path: Path) -> None:
    recorder = RecordingProvider(
        ScriptedProvider(response("a"), response("b")), store(tmp_path), "seq"
    )
    await recorder.complete(request())
    await recorder.complete(request())

    text = (tmp_path / "seq.json").read_text()
    document = json.loads(text)

    assert text == json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert document["format_version"] == 1
    assert [entry["sequence"] for entry in document["entries"]] == [0, 1]
    assert not list(tmp_path.glob(".*.tmp"))


async def test_recording_starts_the_cassette_afresh(tmp_path: Path) -> None:
    first = RecordingProvider(ScriptedProvider(response("old")), store(tmp_path), "fresh")
    await first.complete(request("stale"))
    del first  # a later run: the first recorder is gone
    gc.collect()
    second = RecordingProvider(ScriptedProvider(response("new")), store(tmp_path), "fresh")
    await second.complete(request())

    entries = store(tmp_path).load("fresh").entries

    assert [entry.response.text for entry in entries] == ["new"]


async def test_secret_in_a_response_refuses_the_write(tmp_path: Path) -> None:
    recorder = RecordingProvider(
        ScriptedProvider(response(f"the key is {FAKE_KEY}")), store(tmp_path), "leaky"
    )

    with pytest.raises(SecretInRecordingError, match=r"anthropic_api_key at \$\.entries") as caught:
        await recorder.complete(request())

    assert FAKE_KEY not in str(caught.value)
    assert not (tmp_path / "leaky.json").exists()


async def test_redact_mode_writes_the_cassette_redacted(tmp_path: Path) -> None:
    recorder = RecordingProvider(
        ScriptedProvider(response(f"the key is {FAKE_KEY}")),
        store(tmp_path, SecretAction.REDACT),
        "redacted",
    )
    await recorder.complete(request())

    text = (tmp_path / "redacted.json").read_text()

    assert FAKE_KEY not in text
    assert "[REDACTED:anthropic_api_key]" in text


def test_missing_cassette_loads_empty_and_replays_as_a_miss(tmp_path: Path) -> None:
    assert store(tmp_path).load("absent") == Cassette(name="absent")


def test_malformed_cassette_raises_format_error(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text('{"format_version": 9, "name": "broken"}')

    with pytest.raises(CassetteFormatError, match="not a valid cassette"):
        store(tmp_path).load("broken")


def test_store_rejects_names_that_escape_the_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="name"):
        store(tmp_path).load("../outside")


async def test_hash_like_extra_patterns_do_not_match_structural_fields(tmp_path: Path) -> None:
    hex_pattern_store = DirectoryCassetteStore(
        tmp_path, scrubber=PatternScrubber(extra_patterns={"hex_token": r"\b[0-9a-f]{32,}\b"})
    )
    recorder = RecordingProvider(ScriptedProvider(response("fine")), hex_pattern_store, "hex")

    await recorder.complete(request())

    assert (tmp_path / "hex.json").exists()


class _BrokenRedactor(PatternScrubber):
    def redact(self, value: JsonValue) -> JsonValue:
        return 42


async def test_redaction_that_breaks_the_cassette_is_refused(tmp_path: Path) -> None:
    broken = DirectoryCassetteStore(
        tmp_path, scrubber=_BrokenRedactor(), on_secret=SecretAction.REDACT
    )
    recorder = RecordingProvider(ScriptedProvider(response(f"key {FAKE_KEY}")), broken, "broken")

    with pytest.raises(SecretInRecordingError, match="left it invalid"):
        await recorder.complete(request())

    assert not (tmp_path / "broken.json").exists()


async def test_second_recorder_for_the_same_cassette_is_refused(tmp_path: Path) -> None:
    first = RecordingProvider(
        ScriptedProvider(response("a"), response("c")), store(tmp_path), "shared"
    )
    second = RecordingProvider(ScriptedProvider(response("b")), store(tmp_path), "shared")
    await first.complete(request())

    with pytest.raises(CassetteConflictError, match="already being recorded"):
        await second.complete(request("other"))

    await first.complete(request("more from the first"))
    assert len(store(tmp_path).load("shared").entries) == 2


async def test_cassette_files_are_world_readable(tmp_path: Path) -> None:
    recorder = RecordingProvider(ScriptedProvider(response("a")), store(tmp_path), "mode")
    await recorder.complete(request())

    assert (tmp_path / "mode.json").stat().st_mode & 0o777 == 0o644
