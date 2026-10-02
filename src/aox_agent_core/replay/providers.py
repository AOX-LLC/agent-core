"""Providers that replay recorded responses or record live ones."""

from collections import Counter
from pathlib import Path

from aox_agent_core._model import CassetteName
from aox_agent_core.errors import ReplayMissError, StaleRecordingError
from aox_agent_core.models.provider import ModelProvider, close_provider
from aox_agent_core.models.types import ProviderRequest, ProviderResponse
from aox_agent_core.replay.keys import PromptKey, request_hash
from aox_agent_core.replay.recording import Recording
from aox_agent_core.replay.store import DirectoryRecordingStore


class ReplayProvider:
    """Serves recorded responses and never contacts a model provider.

    A prompted call is looked up by its content-addressed key; repeated calls
    with the same key get the same recording. An unprompted call is looked up
    by its request hash and how many times that request was made before. A miss
    raises ReplayMissError naming the key and the file it expected, and a
    recording whose template, system prompt or schema has changed raises
    StaleRecordingError.
    """

    def __init__(self, store: DirectoryRecordingStore, cassette: CassetteName) -> None:
        self._store = store
        self._cassette = cassette
        self._served: Counter[str] = Counter()

    async def complete(
        self, request: ProviderRequest, *, prompt_key: PromptKey | None = None
    ) -> ProviderResponse:
        if prompt_key is not None:
            return self._replay_prompted(prompt_key)

        key = request_hash(request)
        sequence = self._served[key]
        path = self._store.request_path(self._cassette, key, sequence)
        recording = self._store.load(path)
        if recording is None:
            raise ReplayMissError(
                f"No recording for request {key} (call {sequence + 1} of that request) at "
                f"{path}. Record it with AGENT_CORE_MODE=record.",
                key=key,
                path=str(path),
            )
        self._served[key] += 1
        return _as_replayed(recording)

    def _replay_prompted(self, prompt_key: PromptKey) -> ProviderResponse:
        path = self._store.prompt_path(prompt_key)
        recording = self._store.load(path)
        if recording is None:
            raise ReplayMissError(
                self._prompted_miss_message(prompt_key, path),
                key=prompt_key.key,
                path=str(path),
            )
        assert recording.prompt is not None  # noqa: S101 - prompt_path only holds prompted ones
        stale = prompt_key.stale_parts(recording.prompt)
        if stale:
            raise StaleRecordingError(
                f"The recording at {path} was made with a different {' and '.join(stale)} "
                f"for prompt {prompt_key.prompt_id} v{prompt_key.version}. Bump the prompt "
                "version, or record it again with AGENT_CORE_MODE=record."
            )
        return _as_replayed(recording)

    def _prompted_miss_message(self, prompt_key: PromptKey, path: Path) -> str:
        message = (
            f"No recording for prompt {prompt_key.prompt_id} v{prompt_key.version} "
            f"(attempt {prompt_key.attempt}) with key {prompt_key.key} at {path}."
        )
        other_tiers = sorted(
            recording.prompt.tier.value
            for recording in self._store.prompt_recordings(prompt_key.prompt_id, prompt_key.version)
            if recording.prompt is not None and prompt_key.differs_only_in_tier(recording.prompt)
        )
        if other_tiers:
            message += (
                f" This call routed to the {prompt_key.tier.value} tier, but it was recorded on "
                f"{', '.join(other_tiers)}: routing or a budget drop chose another tier, for "
                "example after a price-table change."
            )
        return message + " Record it with AGENT_CORE_MODE=record."


def _as_replayed(recording: Recording) -> ProviderResponse:
    """The recorded response, noting the route it was recorded on, for pricing."""
    return recording.response.model_copy(
        update={
            "recorded_provider": recording.request.provider,
            "recorded_model": recording.request.model,
        }
    )


class RecordingProvider:
    """Calls a live provider and records every exchange, one file each.

    Each recording passes through the store's secret scan before it is written:
    if a secret is found, SecretInRecordingError is raised and the exchange is
    not kept. Recording a key again replaces its file.
    """

    def __init__(
        self, live: ModelProvider, store: DirectoryRecordingStore, cassette: CassetteName
    ) -> None:
        self._live = live
        self._store = store
        self._cassette = cassette
        self._recorded: Counter[str] = Counter()

    async def complete(
        self, request: ProviderRequest, *, prompt_key: PromptKey | None = None
    ) -> ProviderResponse:
        response = await self._live.complete(request)
        if prompt_key is not None:
            recording = Recording(
                replay_hash=prompt_key.key, prompt=prompt_key, request=request, response=response
            )
        else:
            key = request_hash(request)
            recording = Recording(
                replay_hash=key, sequence=self._recorded[key], request=request, response=response
            )
            self._recorded[key] += 1
        self._store.save(recording, cassette=self._cassette)
        return response

    async def aclose(self) -> None:
        await close_provider(self._live)
