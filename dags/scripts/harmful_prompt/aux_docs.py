"""Optional auxiliary documents that ground generation in a specific domain.

The issue restricts the accepted formats to ``.txt``, ``.md`` and ``.jsonl``.
Anything else is rejected loudly rather than silently ignored, because a
user who points at a PDF and gets no error would reasonably assume it was used.

Documents are concatenated into one context block with a character budget, so a
large reference corpus cannot silently overflow the model's context window.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_SUFFIXES: tuple[str, ...] = (".txt", ".md", ".jsonl")

# Fields we look at, in order, when pulling text out of a .jsonl record.
JSONL_TEXT_FIELDS: tuple[str, ...] = ("text", "content", "body", "prompt", "description")

DEFAULT_CHAR_BUDGET = 6000


class AuxDocError(ValueError):
    """An auxiliary document is missing, unreadable, or an unsupported format."""


@dataclass(frozen=True)
class AuxDocument:
    name: str
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


def _read_jsonl(path: Path) -> str:
    """Pull human-readable text out of a JSONL file, one record per line."""
    chunks: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            # A malformed line in a reference document is not worth failing the
            # whole run over; keep the raw text, which is still useful context.
            chunks.append(stripped)
            continue
        if isinstance(row, str):
            chunks.append(row)
        elif isinstance(row, dict):
            for field in JSONL_TEXT_FIELDS:
                value = row.get(field)
                if isinstance(value, str) and value.strip():
                    chunks.append(value.strip())
                    break
            else:
                # No recognised text field: fall back to the whole record, so
                # domain vocabulary in unusual schemas still reaches the model.
                chunks.append(json.dumps(row, ensure_ascii=False))
        else:
            chunks.append(str(row))
    return "\n".join(chunks)


def load_document(path: str | Path) -> AuxDocument:
    """Read one auxiliary document."""
    source = Path(path)
    if not source.exists():
        raise AuxDocError(f"auxiliary document not found: {source}")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise AuxDocError(
            f"unsupported auxiliary document format {suffix or '(none)'!r} for {source.name}; "
            f"supported: {', '.join(SUPPORTED_SUFFIXES)}"
        )
    try:
        text = _read_jsonl(source) if suffix == ".jsonl" else source.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise AuxDocError(f"{source.name} is not valid UTF-8 text: {error}") from None
    return AuxDocument(name=source.name, text=text.strip())


def load_documents(paths: list[str] | tuple[str, ...] | None) -> list[AuxDocument]:
    """Read every auxiliary document, skipping ones that turn out to be empty."""
    if not paths:
        return []
    documents = [load_document(p) for p in paths]
    return [d for d in documents if d.text]


def build_context_block(
    documents: list[AuxDocument],
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> str:
    """Render documents into one block, truncating to stay inside the budget.

    The budget is shared evenly across documents so that one very long file
    cannot crowd the others out entirely.
    """
    if not documents:
        return ""
    if char_budget <= 0:
        raise AuxDocError("char_budget must be positive")

    per_document = max(200, char_budget // len(documents))
    sections: list[str] = []
    for document in documents:
        text = document.text
        if len(text) > per_document:
            text = text[:per_document].rstrip() + "\n…(truncated)"
        sections.append(f"--- {document.name} ---\n{text}")
    return "\n\n".join(sections)
