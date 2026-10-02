"""Attachments, prompt references and run contexts: the new call inputs."""

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from aox_agent_core.context import MAX_EXTERNAL_IDS, RunContext
from aox_agent_core.errors import AttachmentError, PromptError
from aox_agent_core.models import Message, Role
from aox_agent_core.models.attachments import MAX_IMAGE_BYTES, Attachment
from aox_agent_core.models.prompts import PromptRef

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
        'Ticket INV {1042} with ["a", 1]'
    )


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
