"""The output record and its stable identifier.

Field set is fixed by the issue's acceptance criteria: id, prompt,
violated_policy, severity, domain, generation_rationale. Everything beyond that
is provenance, kept in a nested ``meta`` object so the required fields stay
easy to read at the top level of each line.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

REQUIRED_FIELDS: tuple[str, ...] = (
    "id", "prompt", "violated_policy", "severity", "domain", "generation_rationale",
)


def prompt_id(policy_id: str, prompt: str) -> str:
    """A stable id derived from the content, so reruns do not renumber records.

    Content-derived rather than sequential: two runs that generate the same
    prompt produce the same id, which makes dedup across runs trivial.
    """
    digest = hashlib.sha256(f"{policy_id}\x00{prompt.strip()}".encode("utf-8")).hexdigest()
    return f"hp_{digest[:16]}"


@dataclass
class HarmfulPrompt:
    prompt: str
    violated_policy: str
    severity: str
    domain: str
    generation_rationale: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return prompt_id(self.violated_policy, self.prompt)

    def to_record(self) -> dict[str, Any]:
        record = {
            "id": self.id,
            "prompt": self.prompt,
            "violated_policy": self.violated_policy,
            "severity": self.severity,
            "domain": self.domain,
            "generation_rationale": self.generation_rationale,
        }
        if self.meta:
            record["meta"] = self.meta
        return record


def validate_record(record: dict[str, Any]) -> list[str]:
    """Return a list of problems with a record; empty means valid."""
    problems: list[str] = []
    for name in REQUIRED_FIELDS:
        value = record.get(name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"missing or empty field: {name}")
    severity = record.get("severity")
    if isinstance(severity, str) and severity not in ("low", "medium", "high"):
        problems.append(f"severity must be low/medium/high, got {severity!r}")
    return problems
