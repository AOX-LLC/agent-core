"""Loading and saving cassettes."""

from pathlib import Path
from typing import Protocol

from aox_agent_core.config import SecretAction
from aox_agent_core.replay.cassette import Cassette
from aox_agent_core.replay.scrub import Scrubber


class CassetteStore(Protocol):
    """Reads and writes cassettes by name."""

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

    def load(self, name: str) -> Cassette:
        raise NotImplementedError("DirectoryCassetteStore.load is not implemented yet.")

    def save(self, cassette: Cassette) -> None:
        raise NotImplementedError("DirectoryCassetteStore.save is not implemented yet.")
