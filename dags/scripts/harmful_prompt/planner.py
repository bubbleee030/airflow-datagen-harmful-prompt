"""Turn a requested total and severity ratio into concrete units of work.

The user asks for e.g. "90 prompts across A1-A3, 20% low / 50% medium / 30% high".
This module turns that into an explicit list of work items, each naming one
policy, one severity, and how many prompts to ask for. Making the plan an
inspectable object -- rather than arithmetic buried in the generation loop --
means the DAG can log it, and a dry run can show exactly what would be called.

Rounding is deliberate: fractional allocations are distributed by largest
remainder so the plan always sums to exactly the requested total.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .config import GeneratorSpec
from .policy import SEVERITY_LEVELS, Policy

DEFAULT_RATIO: dict[str, float] = {"low": 0.3, "medium": 0.4, "high": 0.3}


class PlanError(ValueError):
    """The requested plan cannot be satisfied."""


@dataclass(frozen=True)
class WorkItem:
    """One generation call: n prompts for this policy at this severity."""

    policy: Policy
    severity: str
    count: int
    model: str | None = None

    @property
    def key(self) -> str:
        key = f"{self.policy.policy_id}:{self.severity}"
        return f"{key}:{self.model}" if self.model else key


def normalise_ratio(ratio: dict[str, float] | None) -> dict[str, float]:
    """Validate and normalise a severity ratio so it sums to 1."""
    if not ratio:
        return dict(DEFAULT_RATIO)

    from .policy import normalise_severity_key

    cleaned: dict[str, float] = {}
    for key, value in ratio.items():
        level = normalise_severity_key(key)
        try:
            weight = float(value)
        except (TypeError, ValueError):
            raise PlanError(f"severity ratio for {key!r} must be a number, got {value!r}") from None
        if weight < 0:
            raise PlanError(f"severity ratio for {key!r} must not be negative")
        if level in cleaned:
            raise PlanError(f"severity ratio defines {level!r} twice")
        cleaned[level] = weight

    total = sum(cleaned.values())
    if total <= 0:
        raise PlanError("severity ratio must contain at least one positive weight")

    return {level: cleaned.get(level, 0.0) / total for level in SEVERITY_LEVELS}


def _largest_remainder(total: int, weights: dict[str, float]) -> dict[str, int]:
    """Apportion `total` across weights so the parts sum to exactly `total`."""
    raw = {key: total * weight for key, weight in weights.items()}
    counts = {key: int(value) for key, value in raw.items()}
    shortfall = total - sum(counts.values())
    if shortfall:
        # Hand the leftovers to whichever buckets were rounded down hardest.
        order = sorted(raw, key=lambda k: (raw[k] - counts[k], weights[k]), reverse=True)
        for key in order[:shortfall]:
            counts[key] += 1
    return counts


def build_plan(
    policies: Sequence[Policy],
    total: int,
    ratio: dict[str, float] | None = None,
) -> list[WorkItem]:
    """Split `total` prompts evenly across policies, then by severity ratio."""
    if not policies:
        raise PlanError("no policies selected")
    if total <= 0:
        raise PlanError(f"total must be positive, got {total}")

    weights = normalise_ratio(ratio)
    per_policy = _largest_remainder(total, {p.policy_id: 1.0 / len(policies) for p in policies})

    items: list[WorkItem] = []
    for policy in policies:
        allocation = _largest_remainder(per_policy[policy.policy_id], weights)
        for severity in SEVERITY_LEVELS:
            count = allocation.get(severity, 0)
            if count > 0:
                items.append(WorkItem(policy=policy, severity=severity, count=count))
    return items


def assign_generators(
    items: Sequence[WorkItem], generators: Sequence[GeneratorSpec]
) -> list[WorkItem]:
    """Allocate every policy/severity bucket exactly across generator models."""
    weights = {generator.model: generator.weight for generator in generators}
    assigned: list[WorkItem] = []
    for item in items:
        allocation = _largest_remainder(item.count, weights)
        for generator in generators:
            count = allocation[generator.model]
            if count > 0:
                assigned.append(WorkItem(
                    policy=item.policy,
                    severity=item.severity,
                    count=count,
                    model=generator.model,
                ))
    return assigned


def chunk_plan(items: Sequence[WorkItem], max_per_call: int) -> list[WorkItem]:
    """Split work items so no single call asks for more than `max_per_call`.

    Asking one call for a dozen prompts invites truncation: the response runs
    past ``max_tokens`` and the JSON never closes. Several smaller calls are
    more robust, and they parallelise better under the concurrency semaphore.
    """
    if max_per_call <= 0:
        raise PlanError(f"max_per_call must be positive, got {max_per_call}")

    chunked: list[WorkItem] = []
    for item in items:
        remaining = item.count
        while remaining > 0:
            size = min(max_per_call, remaining)
            chunked.append(WorkItem(
                policy=item.policy, severity=item.severity, count=size, model=item.model
            ))
            remaining -= size
    return chunked


def plan_total(items: Sequence[WorkItem]) -> int:
    return sum(item.count for item in items)


def summarise_plan(items: Sequence[WorkItem]) -> str:
    """A human-readable plan summary, for logs and dry runs."""
    if not items:
        return "(empty plan)"
    lines = [f"{'policy':<12}{'low':>6}{'medium':>8}{'high':>6}{'total':>8}"]
    by_policy: dict[str, dict[str, int]] = {}
    for item in items:
        by_policy.setdefault(item.policy.policy_id, {})[item.severity] = item.count
    for policy_id, counts in by_policy.items():
        row = [counts.get(level, 0) for level in SEVERITY_LEVELS]
        lines.append(
            f"{policy_id:<12}{row[0]:>6}{row[1]:>8}{row[2]:>6}{sum(row):>8}"
        )
    lines.append(f"{'TOTAL':<12}{'':>6}{'':>8}{'':>6}{plan_total(items):>8}")
    return "\n".join(lines)
