"""The offline judge stage, end to end, with the API stubbed out.

Two properties matter more than the counts. Every source record must come out
somewhere -- accepted, rejected, or review -- because a record that silently
vanishes is a dataset that quietly shrinks. And the judge must never be shown
what the generator claimed, or its verdict is not independent.
"""
from __future__ import annotations

import json

import pytest
import yaml

from harmful_prompt import judge
from harmful_prompt.client import Completion

VIOLATION = json.dumps({
    "verdict": "violation",
    "matched_policy_id": "A1",
    "severity": "medium",
    "requires_external_context": False,
    "decision_summary": "The request crosses the documented boundary.",
})
NON_VIOLATION = json.dumps({
    "verdict": "non_violation",
    "matched_policy_id": None,
    "severity": None,
    "requires_external_context": False,
    "decision_summary": "Within the documented boundary.",
})
AMBIGUOUS = json.dumps({
    "verdict": "ambiguous",
    "matched_policy_id": None,
    "severity": None,
    "requires_external_context": False,
    "decision_summary": "Underdetermined by the supplied policy.",
})


def read_jsonl(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def source_record(record_id, prompt, severity, rationale):
    return {
        "id": record_id,
        "prompt": prompt,
        "violated_policy": "A1",
        "severity": severity,
        "domain": "abstract service desk",
        "generation_rationale": rationale,
        "meta": {
            "generator_model": "generator-alpha",
            "model": "generator-alpha",
            "policy_sha256": "0" * 64,
            "policy_version": "v1",
            "run_id": "srcrun",
        },
    }


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "p.jsonl").write_text(
        json.dumps({
            "policy_id": "A1",
            "policy": "Reject requests outside the documented service scope.",
            "policy_version": "v1",
            "severity": {"low": "probe", "medium": "clear bypass", "high": "scalable bypass"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "run.yaml"
    config.write_text(yaml.safe_dump({
        "policy_file": "policies/p.jsonl",
        "domain": "abstract service desk",
        "total": 3,
        "output_dir": "out",
        "run_id": "judgerun",
        "judge": {"enabled": True, "model": "judge-a"},
    }, allow_unicode=True), encoding="utf-8")

    source = tmp_path / "source.jsonl"
    source.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
        source_record("r1", "an abstract out-of-scope request", "medium",
                      "GENERATOR SAID: clearly a medium bypass"),
        source_record("r2", "an abstract in-scope question", "low",
                      "GENERATOR SAID: mild probe"),
        source_record("r3", "an abstract borderline request", "high",
                      "GENERATOR SAID: severe"),
    ]) + "\n", encoding="utf-8")
    return tmp_path, config, source


def stub_judge_client(monkeypatch, responder):
    """Replace the judge's ChatClient; records every user message it was sent."""
    seen: list[str] = []

    class Stub:
        def __init__(self, *args, **kwargs):
            self.model = kwargs.get("model")

        async def complete(self, system, user, model=None, extra_body=None):
            seen.append(user)
            return responder(system, user, model)

        async def close(self):
            pass

    monkeypatch.setattr(judge, "ChatClient", Stub)
    return seen


def by_prompt(system, user, model=None):
    if "out-of-scope" in user:
        return Completion(VIOLATION, True, 1)
    if "in-scope" in user:
        return Completion(NON_VIOLATION, True, 1)
    return Completion(AMBIGUOUS, True, 1)


def test_judge_partitions_every_source_record(workspace, monkeypatch):
    tmp_path, config, source = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_judge_client(monkeypatch, by_prompt)

    manifest = judge.run(config=str(config), input=str(source))
    out = tmp_path / "out" / "judgerun"

    assert manifest["source_records"] == 3
    assert manifest["accepted"] == 1
    assert manifest["rejected"] == 1
    assert manifest["review"] == 1
    assert len(read_jsonl(out / "judged_prompts.jsonl")) == 3
    assert len(read_jsonl(out / "accepted_prompts.jsonl")) == 1
    assert len(read_jsonl(out / "judge_review.jsonl")) == 1
    assert len(read_jsonl(out / "judge_rejected.jsonl")) == 1


def test_every_source_id_lands_in_exactly_one_partition(workspace, monkeypatch):
    """A record that is neither accepted, rejected nor reviewed has been lost."""
    tmp_path, config, source = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_judge_client(monkeypatch, by_prompt)

    judge.run(config=str(config), input=str(source))
    out = tmp_path / "out" / "judgerun"
    landed = [
        record["id"]
        for name in ("accepted_prompts.jsonl", "judge_rejected.jsonl", "judge_review.jsonl")
        for record in read_jsonl(out / name)
    ]
    assert sorted(landed) == ["r1", "r2", "r3"]


def test_judge_never_sees_what_the_generator_claimed(workspace, monkeypatch):
    tmp_path, config, source = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    seen = stub_judge_client(monkeypatch, by_prompt)

    judge.run(config=str(config), input=str(source))
    combined = "\n".join(seen)
    assert "GENERATOR SAID" not in combined
    assert "generator-alpha" not in combined
    assert "generation_rationale" not in combined
    assert "severity\": \"medium\"" not in combined


def test_api_failures_go_to_review_and_are_counted(workspace, monkeypatch):
    tmp_path, config, source = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_judge_client(monkeypatch, lambda s, u, m=None: Completion("", False, 3, "HTTP 500"))

    manifest = judge.run(config=str(config), input=str(source))
    out = tmp_path / "out" / "judgerun"

    assert manifest["failures"] == 3
    assert manifest["accepted"] == 0
    assert manifest["review"] == 3
    review = read_jsonl(out / "judge_review.jsonl")
    assert sorted(record["id"] for record in review) == ["r1", "r2", "r3"]
    assert all(record["judge"]["judge_error"] for record in review)
    assert len(read_jsonl(out / "judged_prompts.jsonl")) == 3


def test_unparseable_verdicts_go_to_review_not_acceptance(workspace, monkeypatch):
    tmp_path, config, source = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_judge_client(monkeypatch, lambda s, u, m=None: Completion("I cannot judge.", True, 1))

    manifest = judge.run(config=str(config), input=str(source))
    out = tmp_path / "out" / "judgerun"
    assert manifest["review"] == 3
    assert manifest["accepted"] == 0
    assert all(record["judge"]["judge_error"] for record in read_jsonl(out / "judge_review.jsonl"))


def test_manifest_records_provenance_and_no_raw_response(workspace, monkeypatch):
    tmp_path, config, source = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_judge_client(monkeypatch, by_prompt)

    manifest = judge.run(config=str(config), input=str(source))
    out = tmp_path / "out" / "judgerun"

    assert len(manifest["source_sha256"]) == 64
    assert manifest["policies"]["A1"]["sha256"]
    assert len(manifest["judge_system_sha256"]) == 64
    assert manifest["judge_model"] == "judge-a"
    raw = (out / "judge_manifest.json").read_text(encoding="utf-8")
    assert "api_key_env" not in raw
    assert "decision_summary" not in raw
    judged = read_jsonl(out / "judged_prompts.jsonl")
    assert all("raw_response" not in record["judge"] for record in judged)
    accepted = read_jsonl(out / "accepted_prompts.jsonl")[0]
    assert accepted["judge"]["verdict"] == "violation"
    assert accepted["judge"]["accepted"] is True
    assert len(accepted["judge"]["judge_policy_sha256"]) == 64


def test_source_file_is_never_overwritten(workspace, monkeypatch):
    tmp_path, config, source = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_judge_client(monkeypatch, by_prompt)
    before = source.read_text(encoding="utf-8")

    judge.run(config=str(config), input=str(source))
    assert source.read_text(encoding="utf-8") == before


def test_record_claiming_an_unknown_policy_is_rejected_loudly(workspace, monkeypatch):
    tmp_path, config, source = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_judge_client(monkeypatch, by_prompt)
    bad = source_record("r4", "an abstract request", "low", "x")
    bad["violated_policy"] = "ZZ"
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(bad, ensure_ascii=False) + "\n")

    with pytest.raises(judge.JudgeInputError, match="ZZ"):
        judge.run(config=str(config), input=str(source))
