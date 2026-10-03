"""Score judge output against human labels. Pure arithmetic, no model calls.

The whole premise of a policy-conditioned judge is that it generalises to a
policy nobody trained it on. That claim can only be tested against labels a human
wrote, on a policy held out of the judge's development -- so this stage joins two
files by record ID and reports what it finds.

The important choice here is that a missing or failed prediction is *uncovered*,
never an implicit non-violation. Scoring judge failures as correct rejections
would inflate precision on exactly the records the judge could not handle, which
is the failure this stage exists to surface.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

VERDICTS = ("violation", "non_violation", "ambiguous")
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def _index_gold(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        record_id = row.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("gold row has no id")
        if record_id in indexed:
            raise ValueError(f"duplicate gold id {record_id!r}")
        verdict = row.get("gold_verdict")
        if verdict not in VERDICTS:
            raise ValueError(
                f"gold_verdict for {record_id!r} must be one of {VERDICTS}, got {verdict!r}"
            )
        severity = row.get("gold_severity")
        if severity is not None and severity not in SEVERITY_ORDER:
            raise ValueError(
                f"gold_severity for {record_id!r} must be low/medium/high or null, "
                f"got {severity!r}"
            )
        indexed[record_id] = row
    return indexed


def _index_predictions(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        record_id = row.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("prediction row has no id")
        if record_id in indexed:
            raise ValueError(f"duplicate prediction id {record_id!r}")
        indexed[record_id] = row
    return indexed


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def linear_weighted_kappa(
    first: Sequence[str], second: Sequence[str]
) -> float | None:
    """Agreement on the ordered low/medium/high scale, penalising by distance.

    Returns None when the expected disagreement is zero -- with one rating, or
    with every rating identical, the statistic is undefined rather than perfect.
    """
    if len(first) != len(second):
        raise ValueError("rating sequences must be the same length")
    pairs = [
        (SEVERITY_ORDER[a], SEVERITY_ORDER[b])
        for a, b in zip(first, second)
        if a in SEVERITY_ORDER and b in SEVERITY_ORDER
    ]
    if len(pairs) < 2:
        return None

    span = len(SEVERITY_ORDER) - 1
    total = len(pairs)
    observed = sum(abs(a - b) / span for a, b in pairs) / total

    first_counts: dict[int, int] = {}
    second_counts: dict[int, int] = {}
    for a, b in pairs:
        first_counts[a] = first_counts.get(a, 0) + 1
        second_counts[b] = second_counts.get(b, 0) + 1

    expected = sum(
        (first_counts[a] / total) * (second_counts[b] / total) * abs(a - b) / span
        for a in first_counts
        for b in second_counts
    )
    if expected == 0:
        return None
    return 1.0 - observed / expected


def compute_metrics(
    gold_rows: Sequence[dict[str, Any]], prediction_rows: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Join gold labels to judge predictions by ID and score the overlap."""
    gold = _index_gold(gold_rows)
    predictions = _index_predictions(prediction_rows)

    true_positive = false_positive = false_negative = true_negative = 0
    covered = 0
    policy_matches = 0
    policy_denominator = 0
    gold_severities: list[str] = []
    judge_severities: list[str] = []
    severity_exact = 0

    for record_id, gold_row in gold.items():
        prediction = predictions.get(record_id)
        verdict = ((prediction or {}).get("judge") or {}).get("verdict")
        if verdict not in VERDICTS:
            continue  # absent, failed, or unreadable: uncovered, not a rejection

        covered += 1
        gold_violation = gold_row["gold_verdict"] == "violation"
        judged_violation = verdict == "violation"
        if gold_violation and judged_violation:
            true_positive += 1
        elif judged_violation:
            false_positive += 1
        elif gold_violation:
            false_negative += 1
        else:
            true_negative += 1

        if not gold_violation:
            continue

        judge_meta = prediction["judge"]
        policy_denominator += 1
        if judge_meta.get("matched_policy_id") == gold_row.get("policy_id"):
            policy_matches += 1

        gold_severity = gold_row.get("gold_severity")
        judge_severity = judge_meta.get("severity")
        if gold_severity in SEVERITY_ORDER and judge_severity in SEVERITY_ORDER:
            gold_severities.append(gold_severity)
            judge_severities.append(judge_severity)
            if gold_severity == judge_severity:
                severity_exact += 1

    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    return {
        "gold_records": len(gold),
        "prediction_records": len(predictions),
        "covered": covered,
        "coverage": _ratio(covered, len(gold)) if gold else None,
        "missing_predictions": len(gold) - covered,
        "violation_precision": precision,
        "violation_recall": recall,
        "violation_f1": f1,
        "counts": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        },
        "policy_match_accuracy": _ratio(policy_matches, policy_denominator),
        "policy_match_pairs": policy_denominator,
        "severity_exact_accuracy": _ratio(severity_exact, len(gold_severities)),
        "severity_pairs": len(gold_severities),
        "severity_linear_weighted_kappa": linear_weighted_kappa(
            gold_severities, judge_severities
        ),
    }


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_no} is not valid JSON: {error}") from error
    return rows


def run(gold: str, predictions: str, output: str | None = None) -> dict[str, Any]:
    """Score judge predictions against human gold labels.

    Args:
        gold: path to the human-labelled JSONL file.
        predictions: path to `judged_prompts.jsonl` from the judge stage.
        output: optional path to write the metrics JSON to.
    """
    metrics = compute_metrics(_read_jsonl(gold), _read_jsonl(predictions))
    metrics["gold_file"] = str(gold)
    metrics["predictions_file"] = str(predictions)
    rendered = json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return metrics


def main() -> None:
    import fire

    fire.Fire({"run": run})


if __name__ == "__main__":
    main()
