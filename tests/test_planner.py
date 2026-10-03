"""Plan arithmetic: allocation must be exact, never approximate."""
from __future__ import annotations

import pytest

from harmful_prompt.config import GeneratorSpec
from harmful_prompt.planner import (
    DEFAULT_RATIO,
    PlanError,
    WorkItem,
    assign_generators,
    build_plan,
    chunk_plan,
    normalise_ratio,
    plan_total,
    summarise_plan,
)
from harmful_prompt.policy import DEFAULT_SEVERITY, Policy


def make_policies(*ids):
    return [Policy(pid, f"policy {pid}", dict(DEFAULT_SEVERITY)) for pid in ids]


# --- ratio -----------------------------------------------------------------

def test_default_ratio_when_unspecified():
    assert normalise_ratio(None) == DEFAULT_RATIO


def test_ratio_is_normalised_to_sum_one():
    ratio = normalise_ratio({"low": 1, "medium": 1, "high": 2})
    assert ratio == {"low": 0.25, "medium": 0.25, "high": 0.5}
    assert sum(ratio.values()) == pytest.approx(1.0)


def test_percentages_work_without_manual_conversion():
    assert normalise_ratio({"low": 20, "medium": 50, "high": 30}) == pytest.approx(
        {"low": 0.2, "medium": 0.5, "high": 0.3}
    )


def test_legacy_severity_names_in_ratio():
    assert normalise_ratio({"minor": 1, "moderate": 1, "severe": 2})["high"] == 0.5


def test_omitted_level_gets_zero_weight():
    assert normalise_ratio({"high": 1.0}) == {"low": 0.0, "medium": 0.0, "high": 1.0}


def test_negative_weight_is_rejected():
    with pytest.raises(PlanError, match="not be negative"):
        normalise_ratio({"low": -1, "high": 2})


def test_all_zero_ratio_is_rejected():
    with pytest.raises(PlanError, match="at least one positive"):
        normalise_ratio({"low": 0, "medium": 0, "high": 0})


def test_non_numeric_weight_is_rejected():
    with pytest.raises(PlanError, match="must be a number"):
        normalise_ratio({"low": "many"})


# --- plan totals -----------------------------------------------------------

@pytest.mark.parametrize("total", [1, 2, 3, 7, 10, 90, 91, 100, 999])
def test_plan_always_sums_to_the_requested_total(total):
    """Rounding must never lose or invent a prompt."""
    assert plan_total(build_plan(make_policies("A", "B", "C"), total)) == total


@pytest.mark.parametrize("n_policies", [1, 2, 3, 5, 7])
def test_plan_total_holds_for_any_policy_count(n_policies):
    policies = make_policies(*[f"P{i}" for i in range(n_policies)])
    assert plan_total(build_plan(policies, 100)) == 100


def test_awkward_ratio_still_sums_exactly():
    total = plan_total(build_plan(make_policies("A"), 7, {"low": 1, "medium": 1, "high": 1}))
    assert total == 7


def test_zero_weight_level_produces_no_work_items():
    items = build_plan(make_policies("A"), 10, {"low": 0, "medium": 0, "high": 1})
    assert {i.severity for i in items} == {"high"}
    assert plan_total(items) == 10


def test_ratio_is_respected_at_scale():
    items = build_plan(make_policies("A"), 1000, {"low": 0.2, "medium": 0.5, "high": 0.3})
    by_severity = {i.severity: i.count for i in items}
    assert by_severity == {"low": 200, "medium": 500, "high": 300}


def test_each_policy_gets_a_fair_share():
    items = build_plan(make_policies("A", "B"), 100)
    per_policy = {}
    for item in items:
        per_policy[item.policy.policy_id] = per_policy.get(item.policy.policy_id, 0) + item.count
    assert per_policy == {"A": 50, "B": 50}


# --- rejections ------------------------------------------------------------

def test_no_policies_is_rejected():
    with pytest.raises(PlanError, match="no policies"):
        build_plan([], 10)


@pytest.mark.parametrize("total", [0, -5])
def test_non_positive_total_is_rejected(total):
    with pytest.raises(PlanError, match="must be positive"):
        build_plan(make_policies("A"), total)


# --- summary ---------------------------------------------------------------

def test_summary_reports_the_true_total():
    items = build_plan(make_policies("A", "B"), 60)
    assert "60" in summarise_plan(items)


def test_empty_plan_summary_is_explicit():
    assert summarise_plan([]) == "(empty plan)"


# --- chunking --------------------------------------------------------------

def test_chunking_preserves_the_total():
    """Splitting calls must never change how many prompts are requested."""
    from harmful_prompt.planner import chunk_plan
    plan = build_plan(make_policies("A", "B", "C"), 90)
    assert plan_total(chunk_plan(plan, 5)) == plan_total(plan) == 90


def test_no_chunk_exceeds_the_limit():
    from harmful_prompt.planner import chunk_plan
    plan = build_plan(make_policies("A", "B", "C"), 90)
    assert all(item.count <= 5 for item in chunk_plan(plan, 5))


def test_chunking_preserves_policy_and_severity():
    from harmful_prompt.planner import chunk_plan
    plan = build_plan(make_policies("A", "B"), 40)
    before = {}
    for item in plan:
        before[item.key] = before.get(item.key, 0) + item.count
    after = {}
    for item in chunk_plan(plan, 3):
        after[item.key] = after.get(item.key, 0) + item.count
    assert before == after


def test_chunk_larger_than_any_item_is_a_no_op():
    from harmful_prompt.planner import chunk_plan
    plan = build_plan(make_policies("A"), 10)
    assert len(chunk_plan(plan, 1000)) == len(plan)


def test_chunk_size_one_yields_one_call_per_prompt():
    from harmful_prompt.planner import chunk_plan
    plan = build_plan(make_policies("A"), 7)
    assert len(chunk_plan(plan, 1)) == 7


def test_non_positive_chunk_size_is_rejected():
    from harmful_prompt.planner import chunk_plan
    with pytest.raises(PlanError, match="must be positive"):
        chunk_plan(build_plan(make_policies("A"), 5), 0)


def test_generator_allocation_is_exact_inside_every_bucket():
    """Changing one bucket's remainder must not affect another bucket."""
    policy = Policy("A", "x", dict(DEFAULT_SEVERITY))
    items = [WorkItem(policy, "low", 7), WorkItem(policy, "high", 3)]
    assigned = assign_generators(items, [
        GeneratorSpec("large", 0.7), GeneratorSpec("small", 0.3),
    ])
    got = {(item.severity, item.model): item.count for item in assigned}
    assert got == {
        ("low", "large"): 5, ("low", "small"): 2,
        ("high", "large"): 2, ("high", "small"): 1,
    }
    assert sum(item.count for item in assigned) == 10


def test_chunking_preserves_generator_assignment():
    """Dropping a model on a chunk would route it to the default generator."""
    policy = Policy("A", "x", dict(DEFAULT_SEVERITY))
    chunks = chunk_plan([WorkItem(policy, "medium", 5, model="small")], 2)
    assert [(item.count, item.model) for item in chunks] == [
        (2, "small"), (2, "small"), (1, "small"),
    ]
