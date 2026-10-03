"""Auxiliary document loading and context-block budgeting."""
from __future__ import annotations

import json

import pytest

from harmful_prompt.aux_docs import (
    AuxDocError,
    AuxDocument,
    build_context_block,
    load_document,
    load_documents,
)


def test_txt_is_read_verbatim(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("RAP 提供 RAG 知識問答介面。", encoding="utf-8")
    assert load_document(p).text == "RAP 提供 RAG 知識問答介面。"


def test_md_is_read_verbatim(tmp_path):
    p = tmp_path / "guide.md"
    p.write_text("# Heading\n\nbody text", encoding="utf-8")
    assert "body text" in load_document(p).text


def test_jsonl_text_field_is_extracted(tmp_path):
    p = tmp_path / "kb.jsonl"
    p.write_text(
        json.dumps({"text": "第一筆"}, ensure_ascii=False) + "\n"
        + json.dumps({"content": "第二筆"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert load_document(p).text.splitlines() == ["第一筆", "第二筆"]


def test_jsonl_without_known_field_keeps_whole_record(tmp_path):
    """Unusual schemas still contribute their domain vocabulary."""
    p = tmp_path / "kb.jsonl"
    p.write_text(json.dumps({"weird_key": "術語"}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert "術語" in load_document(p).text


def test_jsonl_malformed_line_is_kept_not_fatal(tmp_path):
    p = tmp_path / "kb.jsonl"
    p.write_text('{"text":"good"}\n{broken\n', encoding="utf-8")
    text = load_document(p).text
    assert "good" in text and "broken" in text


def test_unsupported_format_is_rejected(tmp_path):
    p = tmp_path / "manual.pdf"
    p.write_bytes(b"%PDF-1.4")
    with pytest.raises(AuxDocError, match="unsupported"):
        load_document(p)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(AuxDocError, match="not found"):
        load_document(tmp_path / "nope.txt")


def test_empty_documents_are_dropped(tmp_path):
    empty = tmp_path / "empty.txt"; empty.write_text("   ", encoding="utf-8")
    full = tmp_path / "full.txt"; full.write_text("content", encoding="utf-8")
    assert [d.name for d in load_documents([str(empty), str(full)])] == ["full.txt"]


def test_no_documents_returns_empty_list():
    assert load_documents(None) == []
    assert load_documents([]) == []


# --- context block ---------------------------------------------------------

def test_context_block_is_empty_without_documents():
    assert build_context_block([]) == ""


def test_context_block_names_each_document():
    docs = [AuxDocument("a.txt", "alpha"), AuxDocument("b.md", "beta")]
    block = build_context_block(docs)
    assert "--- a.txt ---" in block and "--- b.md ---" in block
    assert "alpha" in block and "beta" in block


def test_budget_is_shared_so_one_long_doc_cannot_crowd_others_out():
    docs = [AuxDocument("long.txt", "x" * 10000), AuxDocument("short.txt", "important")]
    block = build_context_block(docs, char_budget=2000)
    assert "important" in block, "the short document must survive"
    assert "truncated" in block


def test_short_documents_are_not_truncated():
    block = build_context_block([AuxDocument("a.txt", "brief")], char_budget=5000)
    assert "truncated" not in block


def test_non_positive_budget_is_rejected():
    with pytest.raises(AuxDocError, match="positive"):
        build_context_block([AuxDocument("a.txt", "x")], char_budget=0)


def test_budget_is_a_soft_cap_because_of_the_per_document_floor():
    """Documented behaviour: the 200-char floor can push the block past the budget.

    Injecting 120 characters of a document is useless, so the floor wins over the
    budget. The effective cap is max(budget, 200 * n_documents), and the docs say so.
    """
    docs = [AuxDocument(f"d{i}.md", "y" * 5000) for i in range(50)]
    block = build_context_block(docs, char_budget=6000)
    assert len(block) > 6000, "the floor should push this past the nominal budget"
    assert all(f"d{i}.md" in block for i in range(50)), "every document still appears"
