"""Parse a judge reply into a normalized result, then audit it against the record.

Parsing is tolerant about packaging and strict about meaning. Reasoner models
wrap answers in think blocks and code fences, so those are unwrapped; but a
verdict naming a different policy, a non-violation carrying a severity, or a
boolean sent as the string "false" is rejected rather than coerced, because each
of those signals the judge did not answer the question that was asked.

Stripped reasoning is discarded, never persisted: the audit trail records what
was decided, not the model's private deliberation.

`audit_judgment` is separate from parsing on purpose. The verdict is formed
without any knowledge of what the generator claimed; only afterwards is it
compared against the record's claims. Anything short of a clean agreement goes to
review rather than being accepted or thrown away.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from .config import JudgeSettings
from .parser import FENCE, _balanced_span
from .policy import SEVERITY_LEVELS

VERDICTS = ("violation", "non_violation", "ambiguous")

# Both spellings appear in the wild; only complete blocks are removed, so a
# truncated reply stays unparseable instead of silently losing its tail.
REASONING_BLOCK = re.compile(
    r"<think>.*?</think>|\[THINK\].*?\[/THINK\]", re.DOTALL | re.IGNORECASE
)


class JudgeParseError(ValueError):
    """The judge reply could not be read as a usable verdict."""


@dataclass(frozen=True)
class JudgeResult:
    verdict: str
    matched_policy_id: str | None
    severity: str | None
    requires_external_context: bool
    decision_summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _candidate_payloads(raw: str) -> list[str]:
    text = REASONING_BLOCK.sub("", raw).strip()
    candidates = [text]
    fenced = FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    balanced = _balanced_span(text, "{", "}")
    if balanced:
        candidates.append(balanced)
    return [candidate for candidate in candidates if candidate]


def _require_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise JudgeParseError(f"{key} must be a JSON boolean, got {value!r}")
    return value


def parse_judge_response(raw: str, expected_policy_id: str) -> JudgeResult:
    """Read one judge reply, or raise `JudgeParseError` explaining why not."""
    payload: Any = None
    for candidate in _candidate_payloads(raw):
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            break
        payload = None
    if payload is None:
        raise JudgeParseError("no JSON object found in judge response")

    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise JudgeParseError(f"verdict must be one of {VERDICTS}, got {verdict!r}")

    summary = payload.get("decision_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise JudgeParseError("decision_summary must be a non-empty string")

    requires_external_context = _require_bool(payload, "requires_external_context")
    matched = payload.get("matched_policy_id")
    severity = payload.get("severity")

    if verdict == "violation":
        if matched != expected_policy_id:
            raise JudgeParseError(
                f"violation must match the judged policy {expected_policy_id!r}, "
                f"got {matched!r}"
            )
        if severity not in SEVERITY_LEVELS:
            raise JudgeParseError(f"violation needs a severity, got {severity!r}")
    else:
        if matched is not None:
            raise JudgeParseError(f"{verdict} must not name a policy, got {matched!r}")
        if severity is not None:
            raise JudgeParseError(f"{verdict} must not carry a severity, got {severity!r}")

    return JudgeResult(
        verdict=verdict,
        matched_policy_id=matched,
        severity=severity,
        requires_external_context=requires_external_context,
        decision_summary=summary.strip(),
    )


def audit_judgment(
    result: JudgeResult, record: dict[str, Any], settings: JudgeSettings
) -> dict[str, bool]:
    """Compare an independent verdict with what the record claims about itself."""
    policy_match = (
        result.verdict == "violation"
        and result.matched_policy_id == record.get("violated_policy")
    )
    severity_match = result.severity == record.get("severity")
    accepted = (
        policy_match
        and not result.requires_external_context
        and (severity_match or not settings.require_severity_match)
    )
    return {
        "policy_match": policy_match,
        "severity_match": severity_match,
        "accepted": accepted,
    }
