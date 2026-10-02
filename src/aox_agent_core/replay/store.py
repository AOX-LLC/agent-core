"""Loading and saving cassettes."""

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from aox_agent_core.config import SecretAction
from aox_agent_core.errors import CassetteFormatError, SecretInRecordingError
from aox_agent_core.replay.cassette import Cassette
from aox_agent_core.replay.scrub import Scrubber


class CassetteStore(Protocol):
    """Reads and writes cassettes by name. Loading a name never saved returns an empty cassette."""

    def load(self, name: str) -> Cassette: ...

    def save(self, cassette: Cassette) -> None: ...


class DirectoryCassetteStore:
    """Stores each cassette as <directory>/<name>.json.

    save() scans every entry with the scrubber first. With SecretAction.REFUSE, a
    finding raises SecretInRecordingError and nothing is written; with REDACT,
    matches are replaced before writing. Files are written atomically with sorted
    keys so diffs stay readable.
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
        except OSError as error:
            raise CassetteFormatError(f"Cannot read cassette {path}: {error.strerror}") from error
        return parse_cassette(text, source=path)

    def save(self, cassette: Cassette) -> None:
        """Write the cassette, replacing any earlier file of that name."""
        document = cassette.model_dump(mode="json")
        findings = self._scrubber.find_secrets(document)
        if findings and self._on_secret is SecretAction.REFUSE:
            located = ", ".join(f"{finding.rule} at {finding.path}" for finding in findings)
            raise SecretInRecordingError(
                f"Cassette {cassette.name!r} was not written: it contains {located}."
            )
        if findings:
            document = Cassette.model_validate(self._scrubber.redact(document)).model_dump(
                mode="json"
            )

        self._directory.mkdir(parents=True, exist_ok=True)
        _write_atomically(self.path_for(cassette.name), cassette_text(document))


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
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
