"""Metrics for judge quality, measured against human labels.

The judge's own confidence is not evidence about the judge. The only way to know
whether a policy-conditioned judge works on a new policy is to score it against
labels a human wrote, so this stage is pure arithmetic over two files and never
calls a model.

A record with no prediction is uncovered, never an implicit non-violation --
counting judge failures as correct rejections would flatter exactly the case
this stage exists to detect.
"""
from __future__ import annotations

import json

import pytest

from harmful_prompt.evaluate import compute_metrics, linear_weighted_kappa


def test_binary_metrics_and_coverage_are_hand_derived():
    gold = [
        {"id": "1", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "low"},
        {"id": "2", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "high"},
        {"id": "3", "policy_id": "A", "gold_verdict": "non_violation", "gold_severity": None},
        {"id": "4", "policy_id": "A", "gold_verdict": "non_violation", "gold_severity": None},
    ]
    predictions = [
        {"id": "1", "judge": {"verdict": "violation", "matched_policy_id": "A", "severity": "low"}},
        {"id": "2", "judge": {"verdict": "non_violation", "matched_policy_id": None, "severity": None}},
        {"id": "3", "judge": {"verdict": "violation", "matched_policy_id": "A", "severity": "medium"}},
    ]
    metrics = compute_metrics(gold, predictions)
    assert metrics["coverage"] == 0.75
    assert metrics["missing_predictions"] == 1
    assert metrics["violation_precision"] == 0.5
    assert metrics["violation_recall"] == 0.5
    assert metrics["violation_f1"] == 0.5


def test_linear_weighted_kappa_perfect_and_opposite():
    assert linear_weighted_kappa(["low", "medium", "high"],
                                 ["low", "medium", "high"]) == 1.0
    assert linear_weighted_kappa(["low", "low", "high", "high"],
                                 ["high", "high", "low", "low"]) == pytest.approx(-1.0)


def test_duplicate_prediction_ids_are_rejected():
    gold = [{"id": "1", "policy_id": "A", "gold_verdict": "non_violation",
             "gold_severity": None}]
    predictions = [{"id": "1", "judge": {}}, {"id": "1", "judge": {}}]
    with pytest.raises(ValueError, match="duplicate prediction id"):
        compute_metrics(gold, predictions)


def test_duplicate_gold_ids_are_rejected():
    gold = [
        {"id": "1", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "low"},
        {"id": "1", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "low"},
    ]
    with pytest.raises(ValueError, match="duplicate gold id"):
        compute_metrics(gold, [])


def test_unknown_gold_labels_are_rejected():
    gold = [{"id": "1", "policy_id": "A", "gold_verdict": "probably",
             "gold_severity": None}]
    with pytest.raises(ValueError, match="gold_verdict"):
        compute_metrics(gold, [])


def test_missing_predictions_are_uncovered_not_implicit_non_violations():
    """Counting an unjudged violation as a correct rejection would flatter recall."""
    gold = [
        {"id": "1", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "low"},
        {"id": "2", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "low"},
    ]
    predictions = [
        {"id": "1", "judge": {"verdict": "violation", "matched_policy_id": "A",
                              "severity": "low"}},
    ]
    metrics = compute_metrics(gold, predictions)
    assert metrics["coverage"] == 0.5
    assert metrics["violation_recall"] == 1.0
    assert metrics["counts"]["true_positive"] == 1
    assert metrics["counts"]["false_negative"] == 0


def test_failed_judgments_are_uncovered():
    gold = [{"id": "1", "policy_id": "A", "gold_verdict": "violation",
             "gold_severity": "low"}]
    predictions = [{"id": "1", "judge": {"verdict": None, "judge_error": "api: HTTP 500"}}]
    metrics = compute_metrics(gold, predictions)
    assert metrics["coverage"] == 0.0
    assert metrics["missing_predictions"] == 1


def test_severity_metrics_only_use_covered_gold_violations():
    gold = [
        {"id": "1", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "low"},
        {"id": "2", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "high"},
        {"id": "3", "policy_id": "A", "gold_verdict": "non_violation", "gold_severity": None},
    ]
    predictions = [
        {"id": "1", "judge": {"verdict": "violation", "matched_policy_id": "A", "severity": "low"}},
        {"id": "2", "judge": {"verdict": "violation", "matched_policy_id": "A", "severity": "high"}},
        {"id": "3", "judge": {"verdict": "non_violation", "matched_policy_id": None, "severity": None}},
    ]
    metrics = compute_metrics(gold, predictions)
    assert metrics["severity_exact_accuracy"] == 1.0
    assert metrics["severity_pairs"] == 2
    assert metrics["policy_match_accuracy"] == 1.0


def test_kappa_is_null_when_it_cannot_be_computed():
    assert linear_weighted_kappa(["low"], ["low"]) is None
    assert linear_weighted_kappa([], []) is None


def test_run_writes_stable_json_and_returns_it(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text(json.dumps({
        "id": "1", "policy_id": "A", "gold_verdict": "violation", "gold_severity": "low",
    }) + "\n", encoding="utf-8")
    predictions_path = tmp_path / "judged.jsonl"
    predictions_path.write_text(json.dumps({
        "id": "1", "judge": {"verdict": "violation", "matched_policy_id": "A",
                             "severity": "low"},
    }) + "\n", encoding="utf-8")
    out = tmp_path / "metrics.json"

    from harmful_prompt import evaluate

    metrics = evaluate.run(gold=str(gold_path), predictions=str(predictions_path),
                           output=str(out))
    assert metrics["coverage"] == 1.0
    assert json.loads(out.read_text(encoding="utf-8")) == metrics
