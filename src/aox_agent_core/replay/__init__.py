"""Record and replay model calls so projects run and test with no API key."""

from aox_agent_core.replay.keys import (
    PROMPT_KEY_VERSION,
    PromptKey,
    normalize_inputs,
    replay_key,
    request_hash,
)
from aox_agent_core.replay.providers import RecordingProvider, ReplayProvider
from aox_agent_core.replay.recording import RECORDING_FORMAT_VERSION, Recording
from aox_agent_core.replay.scrub import (
    DEFAULT_SECRET_PATTERNS,
    PatternScrubber,
    Scrubber,
    SecretFinding,
)
from aox_agent_core.replay.store import DirectoryRecordingStore

__all__ = [
    "DEFAULT_SECRET_PATTERNS",
    "PROMPT_KEY_VERSION",
    "RECORDING_FORMAT_VERSION",
    "DirectoryRecordingStore",
    "PatternScrubber",
    "PromptKey",
    "Recording",
    "RecordingProvider",
    "ReplayProvider",
    "Scrubber",
    "SecretFinding",
    "normalize_inputs",
    "replay_key",
    "request_hash",
]
