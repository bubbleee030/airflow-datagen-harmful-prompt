"""Blind human review, to settle who is actually wrong.

Every quality number this pipeline reports assumes the judge is right. Nobody
has checked that. This stage is how you check it: a person reads the prompt and
the policy, decides for themselves, and only afterwards is their answer compared
with what the generator claimed and what the judge said.

The worksheet deliberately hides both. A reviewer who can see that the judge
said "non_violation" will agree with it more often than they should, and an
anchored reviewer measures nothing.

Two commands:

    review sample  --input judged_prompts.jsonl --output sheet.jsonl --n 50
    review score   --labels sheet.jsonl --predictions judged_prompts.jsonl

Between them, a human fills in `gold_verdict` and `gold_severity` in the sheet.
"""
from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .evaluate import compute_metrics
from .policy import load_policies

VERDICTS = ("violation", "non_violation")


class ReviewError(ValueError):
    """The review inputs cannot be used."""


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ReviewError(f"{path}:{line_no} is not valid JSON: {error}") from error
    return rows


def _write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    Path(path).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def sample(
    input: str,
    output: str,
    n: int = 50,
    seed: int = 42,
    policy_file: str | None = None,
) -> dict[str, Any]:
    """Build a blind labelling worksheet from judged records.

    Args:
        input: path to `judged_prompts.jsonl` from the judge stage.
        output: path to write the worksheet to.
        n: how many records to sample.
        seed: makes the sample reproducible.
        policy_file: optional policy JSONL, to inline each policy's full text.
    """
    records = _read_jsonl(input)
    if not records:
        raise ReviewError(f"{input} contains no records")

    policies = {}
    if policy_file:
        policies = {p.policy_id: p for p in load_policies(policy_file)}

    # Stratify by policy AND by what the judge concluded, so the sheet contains
    # both the records it accepted and the ones it threw out. Sampling only the
    # rejections would answer only half the question.
    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        verdict = (record.get("judge") or {}).get("verdict") or "unjudged"
        strata[(record["violated_policy"], verdict)].append(record)

    rng = random.Random(seed)
    for group in strata.values():
        rng.shuffle(group)

    chosen: list[dict[str, Any]] = []
    keys = sorted(strata)
    while len(chosen) < min(n, len(records)):
        progressed = False
        for key in keys:
            if strata[key] and len(chosen) < n:
                chosen.append(strata[key].pop())
                progressed = True
        if not progressed:
            break

    chosen.sort(key=lambda r: r["id"])
    sheet = []
    for record in chosen:
        policy = policies.get(record["violated_policy"])
        sheet.append({
            "id": record["id"],
            "policy_id": record["violated_policy"],
            "policy": policy.policy if policy else "(policy text not inlined)",
            "policy_zh_TW": (policy.policy_zh_tw or "") if policy else "",
            "severity_scale": dict(policy.severity) if policy else {},
            "prompt": record["prompt"],
            # Fill these in. Leave them null and the row is skipped, not counted.
            "gold_verdict": None,      # "violation" or "non_violation"
            "gold_severity": None,     # "low" / "medium" / "high", or null
            "note": "",
        })
    _write_jsonl(output, sheet)

    by_policy = Counter(row["policy_id"] for row in sheet)
    return {
        "source": input,
        "output": output,
        "source_records": len(records),
        "sampled": len(sheet),
        "by_policy": dict(sorted(by_policy.items())),
        "seed": seed,
    }


def _blame(gold_verdict: str, gold_severity: Any, judge: dict[str, Any]) -> str:
    """Who was wrong about this record, given the human's answer."""
    judge_verdict = judge.get("verdict")
    if gold_verdict == "non_violation":
        # Every record was generated *as* a violation, so a human saying
        # otherwise always means the generator produced the wrong thing. The
        # question is whether the judge caught it. If it did not, bad data
        # reached the accepted set -- the worst case, and worth its own label.
        return "generator_wrong" if judge_verdict != "violation" else "both_wrong"
    if judge_verdict != "violation":
        return "judge_wrong"
    if gold_severity and judge.get("severity") and gold_severity != judge["severity"]:
        return "severity_disagreement"
    return "both_right"


def score(labels: str, predictions: str, output: str | None = None) -> dict[str, Any]:
    """Compare human labels against the generator's claim and the judge's verdict.

    Args:
        labels: the filled-in worksheet from `sample`.
        predictions: the same `judged_prompts.jsonl` the worksheet came from.
        output: optional path to write the report JSON to.
    """
    label_rows = _read_jsonl(labels)
    by_id = {r["id"]: r for r in _read_jsonl(predictions)}

    records: list[dict[str, Any]] = []
    unlabelled = 0
    gold_for_metrics: list[dict[str, Any]] = []

    for row in label_rows:
        verdict = row.get("gold_verdict")
        if verdict is None:
            unlabelled += 1
            continue
        if verdict not in VERDICTS:
            raise ReviewError(
                f"gold_verdict for {row['id']!r} must be one of {VERDICTS}, got {verdict!r}"
            )
        record = by_id.get(row["id"])
        if record is None:
            raise ReviewError(f"{row['id']!r} is not in {predictions}")

        judge = record.get("judge") or {}
        blame = _blame(verdict, row.get("gold_severity"), judge)
        records.append({
            "id": row["id"],
            "policy_id": row.get("policy_id") or record["violated_policy"],
            "blame": blame,
            "gold_verdict": verdict,
            "gold_severity": row.get("gold_severity"),
            "claimed_severity": record.get("severity"),
            "judge_verdict": judge.get("verdict"),
            "judge_severity": judge.get("severity"),
            "note": row.get("note", ""),
        })
        gold_for_metrics.append({
            "id": row["id"],
            "policy_id": row.get("policy_id") or record["violated_policy"],
            "gold_verdict": verdict,
            "gold_severity": row.get("gold_severity"),
        })

    counts = Counter(r["blame"] for r in records)
    by_policy: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        by_policy[record["policy_id"]][record["blame"]] += 1

    labelled_ids = {r["id"] for r in gold_for_metrics}
    report = {
        "labels": labels,
        "predictions": predictions,
        "labelled": len(records),
        "unlabelled": unlabelled,
        "counts": {
            "both_right": counts.get("both_right", 0),
            "generator_wrong": counts.get("generator_wrong", 0),
            "judge_wrong": counts.get("judge_wrong", 0),
            "both_wrong": counts.get("both_wrong", 0),
            "severity_disagreement": counts.get("severity_disagreement", 0),
        },
        "by_policy": {k: dict(v) for k, v in sorted(by_policy.items())},
        "judge_metrics": compute_metrics(
            gold_for_metrics,
            [r for r in _read_jsonl(predictions) if r["id"] in labelled_ids],
        ),
        "records": records,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(_summarise(report))
    return report


def _summarise(report: dict[str, Any]) -> str:
    counts = report["counts"]
    total = report["labelled"] or 1
    lines = [
        f"labelled {report['labelled']}  (unlabelled {report['unlabelled']})",
        "",
        f"  both right             {counts['both_right']:4d}  {counts['both_right']/total:5.0%}",
        f"  generator wrong        {counts['generator_wrong']:4d}  {counts['generator_wrong']/total:5.0%}   (judge caught it)",
        f"  judge wrong            {counts['judge_wrong']:4d}  {counts['judge_wrong']/total:5.0%}   (missed a real violation)",
        f"  BOTH wrong             {counts['both_wrong']:4d}  {counts['both_wrong']/total:5.0%}   (bad data was accepted)",
        f"  severity disagreement  {counts['severity_disagreement']:4d}  {counts['severity_disagreement']/total:5.0%}",
        "",
        "by policy:",
    ]
    for policy_id, breakdown in report["by_policy"].items():
        parts = ", ".join(f"{k} {v}" for k, v in sorted(breakdown.items()))
        lines.append(f"  {policy_id}: {parts}")
    metrics = report["judge_metrics"]
    lines += [
        "",
        "judge vs human:",
        f"  precision {metrics['violation_precision']}  recall {metrics['violation_recall']}",
        f"  severity exact {metrics['severity_exact_accuracy']} on {metrics['severity_pairs']} pair(s)",
    ]
    return "\n".join(lines)


def main() -> None:
    import fire

    fire.Fire({"sample": sample, "score": score})


if __name__ == "__main__":
    main()
