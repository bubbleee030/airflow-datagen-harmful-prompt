from __future__ import annotations

from harmful_prompt.provenance import canonical_sha256, text_sha256


def test_text_digest_is_utf8_and_stable():
    assert text_sha256("中文") == text_sha256("中文")
    assert len(text_sha256("中文")) == 64


def test_canonical_digest_ignores_mapping_order():
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})


def test_canonical_digest_changes_with_value():
    assert canonical_sha256({"a": 1}) != canonical_sha256({"a": 2})
