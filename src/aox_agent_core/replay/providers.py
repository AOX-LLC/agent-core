"""Providers that replay recorded responses or record live ones."""

from collections import Counter, defaultdict

from aox_agent_core.errors import ReplayMissError
from aox_agent_core.models.provider import ModelProvider
from aox_agent_core.models.types import ProviderRequest, ProviderResponse
from aox_agent_core.replay.cassette import Cassette, CassetteEntry, request_key
from aox_agent_core.replay.store import CassetteStore


class ReplayProvider:
    """Serves responses from one cassette and never contacts a model provider.

    Repeated identical requests get their recordings in sequence order; asking
    for more than were recorded raises ReplayMissError.
    """

    def __init__(self, store: CassetteStore, cassette: str) -> None:
        self._store = store
        self._cassette_name = cassette
        self._recordings: dict[str, list[CassetteEntry]] | None = None
        self._served: Counter[str] = Counter()

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        key = request_key(request)
        recordings = self._load().get(key, [])
        call_number = self._served[key]
        if call_number >= len(recordings):
            raise ReplayMissError(
                f"Cassette {self._cassette_name!r} has no recording for request {key[:12]} "
                f"(call {call_number + 1} of that request). Record it with AGENT_CORE_MODE=record."
            )
        self._served[key] += 1
        return recordings[call_number].response

    def _load(self) -> dict[str, list[CassetteEntry]]:
        if self._recordings is None:
            grouped: dict[str, list[CassetteEntry]] = defaultdict(list)
            for entry in self._store.load(self._cassette_name).entries:
                grouped[entry.request_key].append(entry)
            self._recordings = {
                key: sorted(entries, key=lambda entry: entry.sequence)
                for key, entries in grouped.items()
            }
        return self._recordings


class RecordingProvider:
    """Calls a live provider and records every exchange to one cassette.

    Recording starts the cassette afresh, so stale entries never survive a
    re-record. The cassette is saved after every call, through the store's
    secret scan: if a secret is found, SecretInRecordingError is raised and the
    exchange is not kept.
    """

    def __init__(self, live: ModelProvider, store: CassetteStore, cassette: str) -> None:
        self._live = live
        self._store = store
        self._cassette_name = cassette
        self._entries: list[CassetteEntry] = []
        self._recorded: Counter[str] = Counter()

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        response = await self._live.complete(request)
        key = request_key(request)
        entry = CassetteEntry(
            request_key=key, sequence=self._recorded[key], request=request, response=response
        )
        self._store.save(Cassette(name=self._cassette_name, entries=(*self._entries, entry)))
        self._entries.append(entry)
        self._recorded[key] += 1
        return response
