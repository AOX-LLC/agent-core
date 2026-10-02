"""Canonical JSON: the one byte form the library hashes."""

import hashlib
import json

from pydantic import JsonValue


def canonical_json(value: JsonValue) -> bytes:
    """Serialize with sorted keys, no insignificant whitespace, UTF-8, no NaN."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_of(value: JsonValue) -> str:
    """Return the hex SHA-256 of the value's canonical JSON."""
    return hashlib.sha256(canonical_json(value)).hexdigest()
