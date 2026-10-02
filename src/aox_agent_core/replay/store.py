"""Loading and saving cassettes."""

import json
import os
import tempfile
import time
import weakref
from pathlib import Path
from typing import Any, Protocol

from pydantic import JsonValue, ValidationError

from aox_agent_core.config import SecretAction
from aox_agent_core.errors import (
    CassetteConflictError,
    CassetteFormatError,
    SecretInRecordingError,
)
from aox_agent_core.replay.cassette import Cassette
from aox_agent_core.replay.scrub import Scrubber

# The store recording each cassette path in this process. Weak, so a store that
# is no longer used releases its cassettes.
_RECORDING_OWNERS: weakref.WeakValueDictionary[Path, "DirectoryCassetteStore"] = (
    weakref.WeakValueDictionary()
)

# Cassettes are meant to be committed and read by everyone, unlike mkstemp's 0600.
CASSETTE_FILE_MODE = 0o644


class CassetteStore(Protocol):
    """Reads and writes cassettes by name. Loading a name never saved returns an empty cassette."""

    def load(self, name: str) -> Cassette: ...

    def save(self, cassette: Cassette) -> None: ...


class DirectoryCassetteStore:
    """Stores each cassette as <directory>/<name>.json.

    save() scans the recorded content (messages, system prompt, output schema,
    response text and model) with the scrubber first; structural fields such as
    request hashes are not scanned. With SecretAction.REFUSE, a finding raises
    SecretInRecordingError and nothing is written; with REDACT, matches are
    replaced before writing. Files are written atomically with sorted keys so
    diffs stay readable.

    save() raises CassetteConflictError instead of overwriting a cassette that
    another store in this process is recording. It also refuses a file that
    another process wrote after this store was created, on a best-effort basis,
    since only file timestamps are shared between processes.
    """

    def __init__(
        self,
        directory: Path,
        *,
        scrubber: Scrubber,
        on_secret: SecretAction = SecretAction.REFUSE,
    ) -> None:
        self._directory = directory
        self._scrubber = scrubber
        self._on_secret = on_secret
        self._created_ns = time.time_ns()
        self._last_written: dict[str, tuple[int, int]] = {}

    def path_for(self, name: str) -> Path:
        # Cassette validates the name, so it cannot contain a path separator.
        return self._directory / f"{Cassette(name=name).name}.json"

    def load(self, name: str) -> Cassette:
        """Return the named cassette, empty if its file does not exist.

        Raises CassetteFormatError if the file cannot be read or is not valid.
        """
        path = self.path_for(name)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Cassette(name=name)
        except (OSError, UnicodeDecodeError) as error:
            raise CassetteFormatError(f"Cannot read cassette {path}: {error}") from error
        return parse_cassette(text, source=path)

    def save(self, cassette: Cassette) -> None:
        """Write the cassette, replacing an earlier file of that name from a previous run."""
        path = self.path_for(cassette.name)
        self._refuse_concurrent_writer(cassette.name, path)

        document = cassette.model_dump(mode="json")
        findings = self._scrubber.find_secrets(recorded_content(document))
        if findings and self._on_secret is SecretAction.REFUSE:
            located = ", ".join(f"{finding.rule} at {finding.path}" for finding in findings)
            raise SecretInRecordingError(
                f"Cassette {cassette.name!r} was not written: it contains {located}."
            )
        if findings:
            document = self._redacted(cassette.name, document)

        self._directory.mkdir(parents=True, exist_ok=True)
        _write_atomically(path, cassette_text(document))
        self._last_written[cassette.name] = _fingerprint(path) or (0, 0)

    def _refuse_concurrent_writer(self, name: str, path: Path) -> None:
        owner = _RECORDING_OWNERS.setdefault(path.resolve(), self)
        if owner is not self:
            raise CassetteConflictError(
                f"Cassette {path} is already being recorded by another client in this "
                "process; record each cassette from one client."
            )
        # Across processes only file timestamps are shared. The kernel stamps them
        # with a coarse clock, so this catches a parallel writer on a best-effort basis.
        current = _fingerprint(path)
        written_by_someone_else = (
            current is not None
            and current != self._last_written.get(name)
            and current[0] >= self._created_ns
        )
        if written_by_someone_else:
            raise CassetteConflictError(
                f"Cassette {path} was written by another recorder during this run; "
                "record each cassette from one client and one process."
            )

    def _redacted(self, name: str, document: dict[str, Any]) -> dict[str, Any]:
        for entry in document["entries"]:
            request, response = entry["request"], entry["response"]
            for message in request["messages"]:
                message["content"] = self._scrubber.redact(message["content"])
            for field in ("system", "output_schema"):
                if request.get(field) is not None:
                    request[field] = self._scrubber.redact(request[field])
            for field in ("text", "model"):
                response[field] = self._scrubber.redact(response[field])
        try:
            return Cassette.model_validate(document).model_dump(mode="json")
        except ValidationError as error:
            raise SecretInRecordingError(
                f"Cassette {name!r} was not written: redacting it left it invalid."
            ) from error


def recorded_content(document: dict[str, Any]) -> JsonValue:
    """The parts of a cassette document that came from prompts and responses.

    Only these are scanned for secrets. Paths in findings still read like the
    full document, e.g. $.entries[0].request.messages[1].content.
    """
    content_entries: list[JsonValue] = []
    for entry in document["entries"]:
        request, response = entry["request"], entry["response"]
        content_request: dict[str, JsonValue] = {"messages": request["messages"]}
        for field in ("system", "output_schema"):
            if request.get(field) is not None:
                content_request[field] = request[field]
        content_entries.append(
            {
                "request": content_request,
                "response": {"text": response["text"], "model": response["model"]},
            }
        )
    return {"entries": content_entries}


def parse_cassette(text: str, *, source: Path) -> Cassette:
    """Parse cassette JSON, raising CassetteFormatError that names `source` if invalid."""
    try:
        return Cassette.model_validate_json(text)
    except ValidationError as error:
        raise CassetteFormatError(f"{source} is not a valid cassette:\n{error}") from error


def cassette_text(document: dict[str, object]) -> str:
    """Render a cassette document in the on-disk form: indented, sorted keys, final newline."""
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
        os.chmod(temporary_name, CASSETTE_FILE_MODE)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _fingerprint(path: Path) -> tuple[int, int] | None:
    """(modification time in ns, size), or None if the file does not exist."""
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_mtime_ns, status.st_size
