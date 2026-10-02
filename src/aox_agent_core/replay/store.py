"""Recordings on disk: one file per recorded exchange.

    <root>/prompts/<prompt_id>/v<version>/<key>.json             prompted calls
    <root>/requests/<cassette>/<request_hash>.<sequence>.json    unprompted calls

One file per exchange means a re-record rewrites only the keys it touches,
recordings merge without conflicts, and a miss can name the exact file it
expected. Recording a key again replaces its file.
"""

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

from pydantic import JsonValue, ValidationError

from aox_agent_core._model import CassetteName
from aox_agent_core.config import SecretAction
from aox_agent_core.errors import CassetteFormatError, SecretInRecordingError
from aox_agent_core.replay.keys import PromptKey
from aox_agent_core.replay.recording import RECORDING_FORMAT_VERSION, Recording
from aox_agent_core.replay.scrub import Scrubber

PROMPTS_DIRECTORY: Final = "prompts"
REQUESTS_DIRECTORY: Final = "requests"

# Recordings are meant to be committed and read by everyone, unlike mkstemp's 0600.
RECORDING_FILE_MODE = 0o644


class DirectoryRecordingStore:
    """Reads and writes recordings under a root directory (replay.cassette_dir).

    save() scans the recorded content (messages, system prompt, output schema,
    prompt inputs, response text and model) with the scrubber first. With
    SecretAction.REFUSE a finding raises SecretInRecordingError and nothing is
    written; with REDACT, matches are replaced before writing. Files are written
    atomically, with sorted keys so diffs stay readable.
    """

    def __init__(
        self,
        directory: Path,
        *,
        scrubber: Scrubber,
        on_secret: SecretAction = SecretAction.REFUSE,
    ) -> None:
        self.directory = directory
        self._scrubber = scrubber
        self._on_secret = on_secret

    def prompt_path(self, prompt: PromptKey) -> Path:
        """Where the recording of a prompted call lives."""
        # PromptId allows no '/' and cannot start with '.', so this stays under the root.
        return (
            self.directory
            / PROMPTS_DIRECTORY
            / prompt.prompt_id
            / f"v{prompt.version}"
            / f"{prompt.key}.json"
        )

    def request_path(self, cassette: CassetteName, request_key: str, sequence: int) -> Path:
        """Where the recording of an unprompted call lives."""
        return self.directory / REQUESTS_DIRECTORY / cassette / f"{request_key}.{sequence}.json"

    def path_of(self, recording: Recording, *, cassette: CassetteName) -> Path:
        if recording.prompt is not None:
            return self.prompt_path(recording.prompt)
        return self.request_path(cassette, recording.replay_hash, recording.sequence)

    def load(self, path: Path) -> Recording | None:
        """The recording at `path`, or None if there is none.

        Raises CassetteFormatError if the file cannot be read or is not a valid
        format 2 recording.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as error:
            raise CassetteFormatError(f"Cannot read recording {path}: {error}") from error
        return parse_recording(text, source=path)

    def prompt_recordings(self, prompt_id: str, version: int) -> Iterator[Recording]:
        """Every recording of one prompt version, for explaining a miss."""
        version_directory = self.directory / PROMPTS_DIRECTORY / prompt_id / f"v{version}"
        for path in sorted(version_directory.glob("*.json")):
            recording = self.load(path)
            if recording is not None:
                yield recording

    def save(self, recording: Recording, *, cassette: CassetteName) -> Path:
        """Write a recording, replacing any earlier one at its path, and return the path."""
        document = recording.model_dump(mode="json")
        findings = self._scrubber.find_secrets(recorded_content(document))
        if findings and self._on_secret is SecretAction.REFUSE:
            located = ", ".join(f"{finding.rule} at {finding.path}" for finding in findings)
            raise SecretInRecordingError(f"The recording was not written: it contains {located}.")
        if findings:
            document = self._redacted(document)

        path = self.path_of(recording, cassette=cassette)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(path, recording_text(document))
        return path

    def _redacted(self, document: dict[str, Any]) -> dict[str, Any]:
        request, response = document["request"], document["response"]
        for message in request["messages"]:
            message["content"] = self._scrubber.redact(message["content"])
        for field in ("system", "output_schema"):
            if request.get(field) is not None:
                request[field] = self._scrubber.redact(request[field])
        for field in ("text", "model"):
            response[field] = self._scrubber.redact(response[field])
        if document.get("prompt") is not None:
            document["prompt"]["inputs"] = self._scrubber.redact(document["prompt"]["inputs"])
        try:
            return Recording.model_validate(document).model_dump(mode="json")
        except ValidationError as error:
            raise SecretInRecordingError(
                "The recording was not written: redacting it left it invalid."
            ) from error


def recorded_content(document: dict[str, Any]) -> JsonValue:
    """The parts of a recording that came from prompts, inputs and responses.

    Only these are scanned for secrets; structural fields such as keys and
    hashes are not. Paths in findings read like the full document, e.g.
    $.request.messages[1].content.
    """
    request, response = document["request"], document["response"]
    content_request: dict[str, JsonValue] = {
        "messages": [{"content": message["content"]} for message in request["messages"]]
    }
    for field in ("system", "output_schema"):
        if request.get(field) is not None:
            content_request[field] = request[field]
    content: dict[str, JsonValue] = {
        "request": content_request,
        "response": {"text": response["text"], "model": response["model"]},
    }
    if document.get("prompt") is not None:
        content["prompt"] = {"inputs": document["prompt"]["inputs"]}
    return content


def parse_recording(text: str, *, source: Path) -> Recording:
    """Parse a recording, raising CassetteFormatError that names `source` if invalid.

    A format 1 file (agent-core 0.1.0a1) gets a message saying to record again.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise CassetteFormatError(f"{source} is not JSON: {error.msg}") from error
    if isinstance(document, dict) and document.get("format_version") == 1:
        raise CassetteFormatError(
            f"{source} is a format 1 cassette from agent-core 0.1.0a1, which this version no "
            f"longer reads; record it again (format {RECORDING_FORMAT_VERSION}) with "
            "AGENT_CORE_MODE=record."
        )
    try:
        return Recording.model_validate(document)
    except ValidationError as error:
        raise CassetteFormatError(f"{source} is not a valid recording:\n{error}") from error


def recording_text(document: dict[str, object]) -> str:
    """The on-disk form: indented, sorted keys, final newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_atomically(path: Path, text: str) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_name, RECORDING_FILE_MODE)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
