"""Recover generated prompts from whatever the model actually returned.

We ask for JSON, but a request that runs across dozens of different models will
not get JSON every time. The strategy is layered, most-reliable first:

1. Parse the whole response as JSON.
2. Pull JSON out of a `````json`` fence.
3. Scan for the first balanced JSON object or array in the text.
4. Salvage complete objects from a *truncated* JSON array. Long generations
   routinely hit ``max_tokens`` mid-array; the closing brackets never arrive,
   but the objects already emitted are perfectly good.
5. Fall back to the labelled free-text format the original prototype used
   (``問題：`` / ``Severity：``), which several of the benchmarked models emit
   naturally.

Anything still unparsed is reported, not silently dropped -- a run that quietly
returns 12 prompts instead of 90 is worse than one that says why.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# Reasoning models wrap their thinking; strip it before parsing.
THINK_BLOCK = re.compile(r"\[THINK\].*?\[/THINK\]", re.DOTALL | re.IGNORECASE)
FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# Free-text fallback: "問題：..." / "Prompt: ..." lines, optionally labelled.
FREEFORM_PROMPT = re.compile(
    r"^\s*(?:\d+[.、)]\s*)?(?:問題|提問|prompt|question)\s*[:：]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
FREEFORM_RATIONALE = re.compile(
    r"^\s*(?:理由|原因|說明|rationale|reason)\s*[:：]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

PROMPT_KEYS = ("prompt", "問題", "text", "content", "question")
RATIONALE_KEYS = ("generation_rationale", "rationale", "reason", "理由", "說明")


@dataclass
class ParseResult:
    prompts: list[dict[str, str]]
    strategy: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.prompts)


def _strip_reasoning(text: str) -> str:
    return THINK_BLOCK.sub("", text).strip()


def _balanced_span(text: str, opener: str, closer: str) -> str | None:
    """Find the first balanced opener..closer span, ignoring braces in strings."""
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _coerce_entry(entry: Any) -> dict[str, str] | None:
    """Turn one decoded item into a {prompt, generation_rationale} pair."""
    if isinstance(entry, str):
        text = entry.strip()
        return {"prompt": text, "generation_rationale": ""} if text else None
    if not isinstance(entry, dict):
        return None
    prompt = ""
    for key in PROMPT_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            prompt = value.strip()
            break
    if not prompt:
        return None
    rationale = ""
    for key in RATIONALE_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            rationale = value.strip()
            break
    return {"prompt": prompt, "generation_rationale": rationale}


def _entries_from_payload(payload: Any) -> list[dict[str, str]]:
    """Accept {"prompts": [...]}, a bare list, or a single object."""
    if isinstance(payload, dict):
        for key in ("prompts", "data", "results", "items", "output"):
            value = payload.get(key)
            if isinstance(value, list):
                payload = value
                break
        else:
            single = _coerce_entry(payload)
            return [single] if single else []
    if not isinstance(payload, list):
        return []
    entries = [_coerce_entry(item) for item in payload]
    return [e for e in entries if e]


def _try_json(text: str) -> list[dict[str, str]]:
    try:
        return _entries_from_payload(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        return []


def parse_freeform(text: str) -> list[dict[str, str]]:
    """The prototype's labelled text format, kept as a last resort."""
    prompts = [m.group(1).strip() for m in FREEFORM_PROMPT.finditer(text)]
    rationales = [m.group(1).strip() for m in FREEFORM_RATIONALE.finditer(text)]
    entries: list[dict[str, str]] = []
    for index, prompt in enumerate(prompts):
        if not prompt:
            continue
        entries.append({
            "prompt": prompt,
            "generation_rationale": rationales[index] if index < len(rationales) else "",
        })
    return entries


def salvage_truncated_json(text: str) -> list[dict[str, str]]:
    """Recover whole objects from a JSON array that was cut off mid-flight.

    A response truncated by ``max_tokens`` is not garbage: every object emitted
    before the cut is complete and usable.

    Objects are collected at *any* nesting depth, not just the top level. The
    prompts we want live inside a ``{"prompts": [...]}`` wrapper, and when the
    response is truncated that wrapper never closes -- so waiting for depth to
    return to zero would recover nothing. A stack of opening positions lets each
    inner object be parsed as soon as it closes; spans that do not look like a
    prompt entry are simply discarded.
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    stack: list[int] = []
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            chunk = text[stack.pop():index + 1]
            try:
                entry = _coerce_entry(json.loads(chunk))
            except (json.JSONDecodeError, ValueError):
                continue
            if entry and entry["prompt"] not in seen:
                seen.add(entry["prompt"])
                entries.append(entry)

    # The cut usually lands inside the last object. If its prompt string closed
    # before the cut, the prompt itself is whole and only the rationale was lost
    # -- worth keeping, because otherwise one truncated call discards every
    # prompt it asked for. An unterminated prompt string is never recovered.
    if stack:
        entry = _entry_from_partial_object(text[stack[-1]:])
        if entry and entry["prompt"] not in seen:
            entries.append(entry)
    return entries


def _entry_from_partial_object(chunk: str) -> dict[str, str] | None:
    """Recover a prompt from an object that was cut off before it closed."""
    for key in PROMPT_KEYS:
        match = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
        if match:
            try:
                prompt = json.loads(f'"{match.group(1)}"').strip()
            except json.JSONDecodeError:
                return None
            return {"prompt": prompt, "generation_rationale": ""} if prompt else None
    return None


def parse_response(raw: str) -> ParseResult:
    """Recover prompts from a model response, trying each strategy in turn."""
    if not raw or not raw.strip():
        return ParseResult([], "none", "empty response")

    text = _strip_reasoning(raw)

    entries = _try_json(text)
    if entries:
        return ParseResult(entries, "json")

    for fenced in FENCE.findall(text):
        entries = _try_json(fenced.strip())
        if entries:
            return ParseResult(entries, "json_fence")

    for opener, closer in (("{", "}"), ("[", "]")):
        span = _balanced_span(text, opener, closer)
        if span:
            entries = _try_json(span)
            if entries:
                return ParseResult(entries, "json_embedded")

    entries = salvage_truncated_json(text)
    if entries:
        return ParseResult(entries, "json_salvaged")

    entries = parse_freeform(text)
    if entries:
        return ParseResult(entries, "freeform")

    preview = text[:160].replace("\n", " ")
    return ParseResult([], "none", f"no prompts recovered; response began: {preview!r}")
