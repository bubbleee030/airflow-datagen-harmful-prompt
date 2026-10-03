"""End-to-end run with the API stubbed out.

The whole pipeline -- config, policy, plan, chunking, prompt building, parsing,
schema, manifest -- is exercised without a network call or an API key, so this
suite runs in CI and on a laptop.
"""
from __future__ import annotations

import json

import pytest
import yaml

from harmful_prompt import generate as gen
from harmful_prompt.client import Completion


@pytest.fixture
def workspace(tmp_path):
    """A minimal but complete project: policies, an aux doc, and a config."""
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "p.jsonl").write_text(
        json.dumps({
            "policy_id": "A1",
            "policy": "Reject eligibility fraud.",
            "policy_zh_TW": "拒絕資格詐欺。",
            "severity": {"minor": "probe", "moderate": "evade", "severe": "fraud"},
            "example_prompts": ["先開通再補件"],
        }, ensure_ascii=False) + "\n"
        + json.dumps({"policy_id": "A2", "policy": "Reject harmful content."},
                     ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "kb.md").write_text("# 名詞\n點數 是計算資源單位", encoding="utf-8")
    config = tmp_path / "run.yaml"
    config.write_text(yaml.safe_dump({
        "policy_file": "policies/p.jsonl",
        "domain": "TAIWAN AI RAP 客服",
        "total": 12,
        "aux_documents": ["kb.md"],
        "output_dir": "out",
        "run_id": "testrun",
        "max_prompts_per_call": 2,
    }, allow_unicode=True), encoding="utf-8")
    return tmp_path, config


def stub_client(monkeypatch, responder):
    """Replace ChatClient with one that answers from `responder(system, user)`."""
    class Stub:
        def __init__(self, *args, **kwargs):
            self.calls = []

        async def complete(self, system, user, model=None, extra_body=None):
            self.calls.append((system, user))
            return responder(system, user, model)

        async def close(self):
            pass

    monkeypatch.setattr(gen, "ChatClient", Stub)


def json_responder(n=2):
    def respond(system, user, model=None):
        payload = {"prompts": [
            {"prompt": f"prompt {i} :: {hash(user) % 10000}",
             "generation_rationale": f"rationale {i}"}
            for i in range(n)
        ]}
        return Completion(content=json.dumps(payload, ensure_ascii=False), ok=True, attempts=1)
    return respond


def test_dry_run_makes_no_calls_and_needs_no_key(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.delenv("NCHC_API_KEY", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("dry run must not construct a client")

    monkeypatch.setattr(gen, "ChatClient", explode)
    result = gen.run(config=str(config), dry_run=True)
    assert result["dry_run"] is True
    assert result["planned_prompts"] == 12
    assert (tmp_path / "out" / "testrun" / "dry_run_prompts.jsonl").exists()


def test_dry_run_renders_policy_and_aux_content(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.setattr(gen, "ChatClient", lambda *a, **k: None)
    gen.run(config=str(config), dry_run=True)
    text = (tmp_path / "out" / "testrun" / "dry_run_prompts.jsonl").read_text(encoding="utf-8")
    assert "拒絕資格詐欺" in text          # zh-TW policy preferred
    assert "點數 是計算資源單位" in text   # auxiliary document reached the prompt
    assert "先開通再補件" in text          # example prompt included


def test_dry_run_shows_which_model_each_call_would_use(workspace, monkeypatch):
    """A dry run that hides the routing cannot show what would actually be called."""
    tmp_path, config = workspace
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["generators"] = [
        {"model": "model-a", "weight": 0.5},
        {"model": "model-b", "weight": 0.5},
    ]
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(gen, "ChatClient", lambda *a, **k: None)

    gen.run(config=str(config), dry_run=True)
    rows = [json.loads(line) for line
            in (tmp_path / "out" / "testrun" / "dry_run_prompts.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()]
    planned = [row for row in rows if "policy_id" in row]
    counts: dict[str, int] = {}
    for row in planned:
        counts[row["model"]] = counts.get(row["model"], 0) + row["count"]
    assert counts == {"model-a": 6, "model-b": 6}


def test_full_run_writes_dataset_and_manifest(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "test-key")
    stub_client(monkeypatch, json_responder(2))

    manifest = gen.run(config=str(config))
    out = tmp_path / "out" / "testrun"

    assert (out / "harmful_prompts.jsonl").exists()
    assert (out / "run_manifest.json").exists()
    assert manifest["produced"] > 0
    assert manifest["failures"] == 0
    assert manifest["requested"] == 12


def test_every_output_record_is_schema_valid(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "test-key")
    stub_client(monkeypatch, json_responder(2))
    gen.run(config=str(config))

    from harmful_prompt.schema import validate_record
    lines = (tmp_path / "out" / "testrun" / "harmful_prompts.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        assert validate_record(json.loads(line)) == []


def test_manifest_never_contains_the_api_key(workspace, monkeypatch):
    """Provenance must not become a credential leak."""
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "super-secret-value")
    stub_client(monkeypatch, json_responder(1))
    gen.run(config=str(config))
    text = (tmp_path / "out" / "testrun" / "run_manifest.json").read_text(encoding="utf-8")
    assert "super-secret-value" not in text


def test_manifest_records_input_hashes(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_client(monkeypatch, json_responder(1))
    manifest = gen.run(config=str(config))
    assert manifest["policy_file_sha256"]
    assert len(manifest["aux_document_sha256"]) == 1


def test_api_failures_are_recorded_not_silently_dropped(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_client(monkeypatch, lambda s, u, model=None: Completion("", False, 3, "HTTP 500"))

    manifest = gen.run(config=str(config))
    assert manifest["produced"] == 0
    assert manifest["failures"] > 0
    failures = (tmp_path / "out" / "testrun" / "failures.jsonl").read_text(encoding="utf-8")
    assert "HTTP 500" in failures


def test_unparseable_responses_are_recorded_as_parse_failures(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_client(monkeypatch, lambda s, u, model=None: Completion("I cannot help with that.", True, 1))

    manifest = gen.run(config=str(config))
    assert manifest["produced"] == 0
    assert manifest["failures"] > 0
    text = (tmp_path / "out" / "testrun" / "failures.jsonl").read_text(encoding="utf-8")
    assert '"stage": "parse"' in text


def test_identical_prompts_are_deduplicated_within_a_run(workspace, monkeypatch):
    """A model repeating itself must not inflate the dataset."""
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    constant = json.dumps({"prompts": [{"prompt": "same", "generation_rationale": "r"}]})
    stub_client(monkeypatch, lambda s, u, model=None: Completion(constant, True, 1))

    manifest = gen.run(config=str(config))
    # every call returned the identical prompt; A1 and A2 differ only by policy
    assert manifest["produced"] == 2, "one record per (policy, prompt) pair"


def test_severity_and_policy_are_carried_onto_records(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_client(monkeypatch, json_responder(2))
    gen.run(config=str(config))

    records = [json.loads(l) for l in
               (tmp_path / "out" / "testrun" / "harmful_prompts.jsonl").read_text(
                   encoding="utf-8").splitlines()]
    assert {r["violated_policy"] for r in records} <= {"A1", "A2"}
    assert {r["severity"] for r in records} <= {"low", "medium", "high"}

    assert all(r["domain"] == "TAIWAN AI RAP 客服" for r in records)

def test_full_run_uses_and_reports_exact_generator_mix(workspace, monkeypatch):
    """Ignoring configured generator allocation would produce no per-model mix."""
    tmp_path, config = workspace
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["generators"] = [
        {"model": "model-a", "weight": 0.5},
        {"model": "model-b", "weight": 0.5},
    ]
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("NCHC_API_KEY", "test-key")

    seen_models = []

    def responder(system, user, model):
        seen_models.append(model)
        payload = {"prompts": [{
            "prompt": f"abstract request from {model} {len(seen_models)}",
            "generation_rationale": "abstract policy mismatch",
        }]}
        return Completion(json.dumps(payload), True, 1)

    stub_client(monkeypatch, responder)
    manifest = gen.run(config=str(config))
    assert set(seen_models) == {"model-a", "model-b"}
    assert manifest["by_generator"] == {"model-a": 6, "model-b": 6}


def test_generation_records_policy_and_template_provenance(workspace, monkeypatch):
    """Without per-record digests a dataset cannot be traced back to its exact inputs."""
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_client(monkeypatch, json_responder(1))
    manifest = gen.run(config=str(config))
    record = json.loads((tmp_path / "out" / "testrun" / "harmful_prompts.jsonl")
                        .read_text(encoding="utf-8").splitlines()[0])
    meta = record["meta"]
    assert meta["generator_model"]
    assert len(meta["policy_sha256"]) == 64
    assert meta["policy_version"]
    assert len(meta["generation_system_sha256"]) == 64
    assert len(meta["generation_user_sha256"]) == 64
    assert set(manifest["policies"]) == {"A1", "A2"}


def test_manifest_never_names_a_credential_variable(workspace, monkeypatch):
    """Nested stage settings inherit api_key_env, so a top-level-only filter leaks it."""
    tmp_path, config = workspace
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["api_key_env"] = "SECRET_KEY_VAR"
    data["judge"] = {"enabled": True, "model": "judge-a"}
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("SECRET_KEY_VAR", "k")
    stub_client(monkeypatch, json_responder(1))

    gen.run(config=str(config))
    manifest = (tmp_path / "out" / "testrun" / "run_manifest.json").read_text(encoding="utf-8")
    assert "api_key_env" not in manifest
    assert "SECRET_KEY_VAR" not in manifest


def test_surplus_prompts_are_trimmed_to_the_requested_count(workspace, monkeypatch):
    """Models return more than asked; untrimmed, the configured mix stops holding."""
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    # every call asks for 2 (total 12 / 6 buckets); answer with 4
    stub_client(monkeypatch, json_responder(4))

    manifest = gen.run(config=str(config))
    assert manifest["requested"] == 12
    assert manifest["produced"] == 12
    assert sum(manifest["by_severity"].values()) == 12
    assert manifest["by_policy"] == {"A1": 6, "A2": 6}


def test_surplus_backfills_records_lost_to_validation(workspace, monkeypatch):
    """Trimming must not discard spares that are still needed to reach the count."""
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")

    def responder(system, user, model=None):
        # first entry is unusable, the next two are fine
        payload = {"prompts": [
            {"prompt": "   ", "generation_rationale": "blank"},
            {"prompt": f"good one {hash(user) % 10000}", "generation_rationale": "r"},
            {"prompt": f"good two {hash(user) % 10000}", "generation_rationale": "r"},
        ]}
        return Completion(json.dumps(payload), True, 1)

    stub_client(monkeypatch, responder)
    manifest = gen.run(config=str(config))
    assert manifest["produced"] == 12


def test_failures_record_why_the_response_ended(workspace, monkeypatch):
    """Without finish_reason a truncated reply looks like a garbage reply."""
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_client(monkeypatch, lambda s, u, model=None: Completion(
        "```json {\"prompts\": [{\"prompt\": \"cut off mid", True, 1,
        finish_reason="length", completion_tokens=4096))

    manifest = gen.run(config=str(config))
    assert manifest["produced"] == 0
    failures = [json.loads(line) for line
                in (tmp_path / "out" / "testrun" / "failures.jsonl")
                .read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(f["finish_reason"] == "length" for f in failures)
    assert all(f["completion_tokens"] == 4096 for f in failures)
    assert all(f["response_chars"] > 0 for f in failures)


def test_total_override_is_respected(workspace, monkeypatch):
    tmp_path, config = workspace
    monkeypatch.setenv("NCHC_API_KEY", "k")
    stub_client(monkeypatch, json_responder(1))
    assert gen.run(config=str(config), total=4)["requested"] == 4
