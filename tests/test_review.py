"""Blind human review: who was wrong, the generator or the judge?

The worksheet must not show the judge's verdict. A human who can see that the
judge said "non_violation" will agree with it more often than they should, and
the whole point of this stage is to find out whether the judge can be trusted.
"""
from __future__ import annotations

import json

import pytest

from harmful_prompt import review


def judged(record_id, policy, claimed, verdict, judge_severity=None):
    return {
        "id": record_id,
        "prompt": f"prompt {record_id}",
        "violated_policy": policy,
        "severity": claimed,
        "domain": "d",
        "generation_rationale": "GENERATOR SAID: definitely a violation",
        "judge": {
            "verdict": verdict,
            "matched_policy_id": policy if verdict == "violation" else None,
            "severity": judge_severity,
            "decision_summary": "JUDGE SAID: reasoning here",
            "accepted": verdict == "violation" and judge_severity == claimed,
        },
    }


def write(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def read(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture
def dataset(tmp_path):
    rows = [judged(f"r{i}", "A1" if i % 2 else "A2",
                   "low", "violation" if i % 3 else "non_violation",
                   "low" if i % 3 else None)
            for i in range(12)]
    return write(tmp_path / "judged.jsonl", rows), tmp_path


def test_worksheet_hides_every_judge_and_generator_claim(dataset):
    source, tmp_path = dataset
    out = tmp_path / "sheet.jsonl"
    review.sample(input=str(source), output=str(out), n=6, seed=1)
    text = out.read_text(encoding="utf-8")
    assert "JUDGE SAID" not in text
    assert "GENERATOR SAID" not in text
    assert "decision_summary" not in text
    for row in read(out):
        assert row["gold_verdict"] is None
        assert row["gold_severity"] is None
        assert row["prompt"]
        assert row["policy_id"]
        assert row["policy"]


def test_worksheet_is_stratified_across_judge_verdicts(dataset):
    """Sampling only what the judge rejected would answer only half the question."""
    source, tmp_path = dataset
    out = tmp_path / "sheet.jsonl"
    review.sample(input=str(source), output=str(out), n=6, seed=1)
    ids = {r["id"] for r in read(out)}
    original = {r["id"]: r["judge"]["verdict"] for r in read(source)}
    seen = {original[i] for i in ids}
    assert seen == {"violation", "non_violation"}


def test_sampling_is_reproducible_from_the_seed(dataset):
    source, tmp_path = dataset
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    review.sample(input=str(source), output=str(a), n=6, seed=7)
    review.sample(input=str(source), output=str(b), n=6, seed=7)
    assert [r["id"] for r in read(a)] == [r["id"] for r in read(b)]


def test_score_attributes_blame_to_generator_or_judge(tmp_path):
    predictions = write(tmp_path / "judged.jsonl", [
        # human agrees it is a violation, judge agreed too -> nobody wrong
        judged("ok", "A1", "low", "violation", "low"),
        # human says NOT a violation, and the judge caught it -> generator only
        judged("genbad", "A1", "low", "non_violation"),
        # human says NOT a violation but the judge confirmed it -> both wrong,
        # and the bad record reached the accepted set
        judged("bothbad", "A1", "low", "violation", "low"),
        # human says it IS a violation, judge said it was not -> judge missed it
        judged("judgebad", "A1", "low", "non_violation"),
        # human agrees on violation, but severity differs from the judge's
        judged("sev", "A1", "high", "violation", "medium"),
    ])
    labels = write(tmp_path / "sheet.jsonl", [
        {"id": "ok", "policy_id": "A1", "gold_verdict": "violation", "gold_severity": "low"},
        {"id": "genbad", "policy_id": "A1", "gold_verdict": "non_violation", "gold_severity": None},
        {"id": "bothbad", "policy_id": "A1", "gold_verdict": "non_violation", "gold_severity": None},
        {"id": "judgebad", "policy_id": "A1", "gold_verdict": "violation", "gold_severity": "low"},
        {"id": "sev", "policy_id": "A1", "gold_verdict": "violation", "gold_severity": "high"},
    ])
    report = review.score(labels=str(labels), predictions=str(predictions))

    blame = {row["id"]: row["blame"] for row in report["records"]}
    assert blame["ok"] == "both_right"
    assert blame["genbad"] == "generator_wrong"
    assert blame["bothbad"] == "both_wrong"
    assert blame["judgebad"] == "judge_wrong"
    assert blame["sev"] == "severity_disagreement"
    assert report["counts"]["generator_wrong"] == 1
    assert report["counts"]["both_wrong"] == 1
    assert report["counts"]["judge_wrong"] == 1
    assert report["counts"]["both_right"] == 1
    assert report["counts"]["severity_disagreement"] == 1


def test_score_reports_the_judge_metrics_too(tmp_path):
    predictions = write(tmp_path / "j.jsonl", [judged("a", "A1", "low", "violation", "low")])
    labels = write(tmp_path / "s.jsonl", [
        {"id": "a", "policy_id": "A1", "gold_verdict": "violation", "gold_severity": "low"}])
    report = review.score(labels=str(labels), predictions=str(predictions))
    assert report["judge_metrics"]["coverage"] == 1.0
    assert report["judge_metrics"]["violation_precision"] == 1.0


def test_unlabelled_rows_are_skipped_not_counted_as_agreement(tmp_path):
    """A half-finished worksheet must not silently read as everybody agreeing."""
    predictions = write(tmp_path / "j.jsonl", [
        judged("done", "A1", "low", "violation", "low"),
        judged("todo", "A1", "low", "violation", "low"),
    ])
    labels = write(tmp_path / "s.jsonl", [
        {"id": "done", "policy_id": "A1", "gold_verdict": "violation", "gold_severity": "low"},
        {"id": "todo", "policy_id": "A1", "gold_verdict": None, "gold_severity": None},
    ])
    report = review.score(labels=str(labels), predictions=str(predictions))
    assert report["labelled"] == 1
    assert report["unlabelled"] == 1
    assert len(report["records"]) == 1


def test_per_policy_breakdown_locates_the_bad_policy(tmp_path):
    predictions = write(tmp_path / "j.jsonl", [
        judged("a1", "A1", "low", "violation", "low"),
        judged("a2a", "A2", "low", "non_violation"),          # judge caught it
        judged("a2b", "A2", "low", "violation", "low"),       # judge did not
    ])
    labels = write(tmp_path / "s.jsonl", [
        {"id": "a1", "policy_id": "A1", "gold_verdict": "violation", "gold_severity": "low"},
        {"id": "a2a", "policy_id": "A2", "gold_verdict": "non_violation", "gold_severity": None},
        {"id": "a2b", "policy_id": "A2", "gold_verdict": "non_violation", "gold_severity": None},
    ])
    report = review.score(labels=str(labels), predictions=str(predictions))
    assert report["by_policy"]["A2"] == {"generator_wrong": 1, "both_wrong": 1}
    assert report["by_policy"]["A1"] == {"both_right": 1}
