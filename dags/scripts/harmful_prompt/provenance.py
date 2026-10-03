"""SHA-256 helpers shared by every stage that has to be reproducible.

Generation, judging and deduplication all need to answer the same question
later: "which exact policy text and which exact rendered prompt produced this
record?". They answer it by storing digests rather than the text itself, so an
artifact stays small and a reviewer can still prove two runs saw identical
inputs.

`canonical_sha256` sorts keys and strips insignificant whitespace, so a mapping
that is semantically unchanged does not appear to change when a serializer
reorders it.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def text_sha256(text: str) -> str:
    """Digest of a string, always over its UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(value: Any) -> str:
    """Digest of a JSON-serializable value, independent of mapping order."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
