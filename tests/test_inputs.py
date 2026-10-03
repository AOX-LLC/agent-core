"""Attachments, prompt references and run contexts: the new call inputs."""

import hashlib
import os
import time
import zlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from aox_agent_core import Tier
from aox_agent_core.context import MAX_EXTERNAL_IDS, RunContext
from aox_agent_core.errors import AttachmentError, PromptError
from aox_agent_core.models import Message, Role, attachments
from aox_agent_core.models.attachments import MAX_IMAGE_BYTES, Attachment
from aox_agent_core.models.prompts import PromptRef
from aox_agent_core.replay import replay_key

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.4\n" + b"%" * 32


@pytest.mark.parametrize(
    ("data", "media_type"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (PDF, "application/pdf")],
)
def test_type_comes_from_the_bytes(data: bytes, media_type: str) -> None:
    attachment = Attachment.from_bytes(data)

    assert attachment.media_type == media_type
    assert attachment.size_bytes == len(data)
    assert len(attachment.sha256) == 64


def test_unknown_bytes_and_a_wrong_declared_type_are_refused() -> None:
    with pytest.raises(AttachmentError, match="not a PNG, JPEG or PDF"):
        Attachment.from_bytes(b"GIF89a....")
    with pytest.raises(
        AttachmentError, match="Declared application/pdf but the bytes are image/png"
    ):
        Attachment.from_bytes(PNG, media_type="application/pdf")
    with pytest.raises(AttachmentError, match="empty"):
        Attachment.from_bytes(b"")


def test_size_caps_apply_and_can_only_be_lowered() -> None:
    with pytest.raises(AttachmentError, match="cap is"):
        Attachment.from_bytes(PNG + b"\x00" * MAX_IMAGE_BYTES)
    with pytest.raises(AttachmentError, match="cap is 10"):
        Attachment.from_bytes(PNG, max_bytes=10)
    assert Attachment.from_bytes(PNG, max_bytes=10**12).size_bytes == len(PNG)


def test_a_misnamed_file_is_typed_by_its_contents(tmp_path: Path) -> None:
    path = tmp_path / "receipt.jpg"
    path.write_bytes(PDF)

    assert Attachment.from_path(path).media_type == "application/pdf"
    with pytest.raises(AttachmentError, match="Cannot read"):
        Attachment.from_path(tmp_path / "absent.png")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"media_type": "image/png", "data": JPEG}, "bytes are image/jpeg"),
        ({"size_bytes": 1}, "size_bytes is 1"),
        ({"sha256": "0" * 64}, "sha256 does not match"),
    ],
    ids=["type", "size", "hash"],
)
def test_a_directly_built_attachment_must_match_its_bytes(
    fields: dict[str, object], message: str
) -> None:
    valid = Attachment.from_bytes(PNG)
    built = {**valid.model_dump(), "data": PNG, **fields}
    if "data" in fields:
        built["size_bytes"] = len(JPEG)

    with pytest.raises(AttachmentError, match=message):
        Attachment.model_validate(built)


def test_a_directly_built_oversized_attachment_is_refused() -> None:
    data = PNG + b"\x00" * MAX_IMAGE_BYTES
    fields = {
        "media_type": "image/png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "data": data,
    }

    with pytest.raises(AttachmentError, match="the cap is"):
        Attachment.model_validate(fields)


def test_from_path_reads_only_regular_files_and_stops_past_the_cap(tmp_path: Path) -> None:
    big = tmp_path / "big.png"
    big.write_bytes(PNG + b"\x00" * 2_000)

    with pytest.raises(AttachmentError, match="not a regular file"):
        Attachment.from_path("/dev/zero")
    with pytest.raises(AttachmentError, match="over the 1000-byte cap"):
        Attachment.from_path(big, max_bytes=1_000)


def object_stream_pdf(pages: int, *, filter_ok: bool = True) -> bytes:
    """A PDF 1.5-style file whose page objects live in a compressed object stream."""
    objects = b" ".join(b"<< /Type /Page /Parent 2 0 R >>" for _ in range(pages))
    body = zlib.compress(objects) if filter_ok else b"not flate data"
    header = b"5 0 obj << /Type /ObjStm /N %d /First 0 /Filter /FlateDecode /Length %d >>"
    return (
        b"%PDF-1.7\n"
        + header % (pages, len(body))
        + b"\nstream\n"
        + body
        + b"\nendstream\nendobj\n%EOF\n"
    )


def test_pdf_pages_are_counted_inside_object_streams() -> None:
    assert Attachment.from_bytes(object_stream_pdf(7)).pdf_pages == 7
    assert Attachment.from_bytes(PDF).pdf_pages is None
    assert Attachment.from_bytes(object_stream_pdf(3, filter_ok=False)).pdf_pages is None
    assert Attachment.from_bytes(PNG).pdf_pages is None


def test_inflating_stops_at_the_budget_instead_of_running_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments, "MAX_INFLATED_BYTES", 1_000)
    exactly_at_budget = zlib.compress(b"\x00" * 1_000)
    bomb = zlib.compress(b"\x00" * 20_000_000)
    data = b"%PDF-1.7\n" + b"".join(
        b"1 0 obj << /Type /ObjStm >>\nstream\n" + body + b"\nendstream\nendobj\n"
        for body in (exactly_at_budget, bomb)
    )

    assert attachments._pdf_page_count(data) is None


def test_many_object_stream_markers_are_scanned_once() -> None:
    markers = b"<< /Type /ObjStm >> " * 20_000
    data = (
        b"%PDF-1.7\n"
        + markers
        + b"\nstream\n"
        + zlib.compress(b"<< /Type /Page >>")
        + b"\nendstream\n"
    )
    started = time.perf_counter()

    assert attachments._pdf_page_count(data) == 1
    assert time.perf_counter() - started < 1


def test_escaped_type_names_and_declared_counts_are_not_under_counted() -> None:
    escaped = PDF + b"<< /Type /Page >> " + b"<< /Type /P#61ge >> " * 99
    declared = PDF + b"<< /Type /Pages /Count 100 >> << /Type /Page >>"

    assert Attachment.from_bytes(escaped).pdf_pages is None
    assert Attachment.from_bytes(declared).pdf_pages == 100


def test_from_path_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(AttachmentError, match="not a regular file"):
        Attachment.from_path(fifo)


def test_a_string_validation_flag_does_not_skip_the_hash_check() -> None:
    fields = {**Attachment.from_bytes(PNG).model_dump(), "data": PNG, "sha256": "0" * 64}

    with pytest.raises(AttachmentError, match="sha256 does not match"):
        Attachment.model_validate(fields, context={"aox_agent_core.attachment_hashed": True})


def test_bytes_never_appear_in_dumps_or_repr() -> None:
    attachment = Attachment.from_bytes(PNG)

    assert "data" not in attachment.model_dump()
    assert "data" not in attachment.model_dump_json()
    assert "\\x89PNG" not in repr(attachment)
    assert attachment.reference().data is None
    assert attachment.reference().sha256 == attachment.sha256


def test_only_user_messages_carry_attachments() -> None:
    attachment = Attachment.from_bytes(PNG)

    assert Message(role=Role.USER, content="see image", attachments=(attachment,)).attachments
    with pytest.raises(ValidationError, match="only user messages"):
        Message(role=Role.ASSISTANT, content="here", attachments=(attachment,))


def test_prompt_renders_strings_as_given_and_other_values_as_json() -> None:
    prompt = PromptRef(id="receipts.extract", version=2, template="Ticket ${ticket} with ${tags}")

    assert prompt.render({"ticket": "INV {1042}", "tags": ["a", 1]}) == (
        'Ticket INV {1042} with ["a",1]'
    )


def test_inputs_differing_only_in_key_order_render_the_same_text() -> None:
    prompt = PromptRef(id="receipts.extract", version=2, template="Data: ${data}")

    first = prompt.render({"data": {"b": 1, "a": [1, 2]}})
    second = prompt.render({"data": {"a": [1, 2], "b": 1}})

    assert first == second == 'Data: {"a":[1,2],"b":1}'


@pytest.mark.parametrize("value", [Decimal("1.5"), datetime(2026, 10, 2), float("nan")])
def test_inputs_that_are_not_plain_json_are_a_prompt_error(value: object) -> None:
    prompt = PromptRef(id="receipts.extract", version=2, template="Data: ${data}")

    with pytest.raises(PromptError, match="plain JSON"):
        prompt.render({"data": value})  # type: ignore[dict-item]
    with pytest.raises(PromptError, match="plain JSON"):
        replay_key(prompt, tier=Tier.SMALL, output_schema=None, inputs={"data": value})  # type: ignore[dict-item]


def test_prompt_errors_name_the_problem() -> None:
    prompt = PromptRef(id="receipts.extract", version=1, template="Hello ${name}")

    with pytest.raises(PromptError, match="needs input 'name'"):
        prompt.render({})
    with pytest.raises(PromptError, match="bad placeholder"):
        PromptRef(id="p", version=1, template="price $").render({})


@pytest.mark.parametrize("prompt_id", ["Receipts", "1st", "has space", ""])
def test_prompt_ids_are_lowercase_dotted_names(prompt_id: str) -> None:
    with pytest.raises(ValidationError):
        PromptRef(id=prompt_id, version=1, template="t")


def test_run_context_accepts_a_uuid_string_and_opaque_external_ids() -> None:
    run_id = UUID("8f0c1a9e-0000-4000-8000-000000000001")

    context = RunContext(
        run_id=str(run_id), external_ids={"n8n_workflow_id": "wf-12", "n8n_execution_id": "4812"}
    )

    assert context.run_id == str(run_id)
    assert context.as_json()["external_ids"] == {
        "n8n_execution_id": "4812",
        "n8n_workflow_id": "wf-12",
    }


@pytest.mark.parametrize(
    "external_ids",
    [
        {"api_key": "x1"},
        {"session_token": "x1"},
        {"note": "sk-ant-" + "q" * 24},
        {"owner": "jane@example.com"},
        {"Workflow": "x1"},
        {f"id_{number}": "x" for number in range(MAX_EXTERNAL_IDS + 1)},
        {"big": "x" * 199, **{f"k{number}": "y" * 199 for number in range(10)}},
    ],
    ids=["secret-name", "token-name", "secret-value", "email", "bad-name", "too-many", "too-big"],
)
def test_run_context_rejects_secret_shaped_or_oversized_ids(external_ids: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        RunContext(run_id="run-1", external_ids=external_ids)
