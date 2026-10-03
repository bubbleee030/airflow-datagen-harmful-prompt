# Policy-Conditioned Judge and Multi-Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exact multi-generator allocation, an independent policy-conditioned judge, deterministic semantic deduplication, complete provenance, and offline judge metrics to the existing local harmful-prompt pipeline.

**Architecture:** Preserve the current generator entry point and six required dataset fields. Add nested opt-in configuration, assign a model to every generation work item, implement judging and semantic deduplication as separate rerunnable offline stages, and join their artifacts through stable record IDs and SHA-256 provenance. External model calls stay behind `ChatClient`; tests inject stubs or deterministic embeddings.

**Tech Stack:** Python 3.12, dataclasses, asyncio, OpenAI Python SDK 2.24.0, PyYAML, Fire, pytest, optional sentence-transformers/BGE-M3.

**Spec:** `docs/superpowers/specs/2026-09-06-policy-judge-multigenerator-design.md`

## Global Constraints

- The Airflow DAG remains out of scope.
- A two-field policy row containing only `policy_id` and `policy` remains valid.
- The root `model` configuration key remains the single-generator fallback.
- `python3 -m harmful_prompt.generate run --config configs/example.yaml` continues to work.
- Existing top-level dataset fields remain unchanged: `id`, `prompt`, `violated_policy`, `severity`, `domain`, and `generation_rationale`.
- A generation dry run never reads an API key or calls a model.
- Secrets are read only from named environment variables and never written to records, manifests, logs, dry-run output, or errors.
- Exact prompt identifiers remain derived from `(violated_policy, prompt)`.
- All automated tests use abstract or benign policy fixtures and stub external API calls.
- A live run is never part of `pytest`.
- Do not modify `dags/__init__.py`, create an Airflow DAG, push, merge, publish, or download a model.
- Preserve the user's pre-existing uncommitted change in `docs/demo-walkthrough.zh-TW.md`.
- Follow strict red-green-refactor TDD: each production behavior is preceded by a test that is run and observed failing for the intended reason.

## File Map

- `dags/scripts/harmful_prompt/config.py`: nested generator, judge, and dedup settings plus validation and inheritance.
- `dags/scripts/harmful_prompt/policy.py`: optional judge-oriented policy fields and stable policy identity.
- `dags/scripts/harmful_prompt/planner.py`: exact generator allocation within every policy/severity bucket.
- `dags/scripts/harmful_prompt/client.py`: shared client with safe per-call model and request overrides.
- `dags/scripts/harmful_prompt/provenance.py`: canonical JSON/text SHA-256 helpers used by every stage.
- `dags/scripts/harmful_prompt/generate.py`: multi-generator execution and generation provenance.
- `dags/scripts/harmful_prompt/judge_prompt_builder.py`: policy compiler and blind judge messages.
- `dags/scripts/harmful_prompt/judge_schema.py`: normalized judge result parser and acceptance audit.
- `dags/scripts/harmful_prompt/judge.py`: offline judging CLI and artifact partitioning.
- `dags/scripts/harmful_prompt/embedding.py`: optional sentence-transformers adapter.
- `dags/scripts/harmful_prompt/semantic_dedup.py`: vector clustering, representative selection, and CLI artifacts.
- `dags/scripts/harmful_prompt/evaluate.py`: pure offline metrics for human gold labels.
- `tests/test_config.py`, `tests/test_policy.py`, `tests/test_planner.py`, `tests/test_generate_integration.py`: extend existing behavior tests.
- `tests/test_provenance.py`, `tests/test_judge_prompt_builder.py`, `tests/test_judge_schema.py`, `tests/test_judge_integration.py`, `tests/test_semantic_dedup.py`, `tests/test_evaluate.py`: focused tests for new modules.
- `requirements-dev.txt`, `requirements-semantic.txt`: new-VM development and optional embedding dependencies.
- `configs/example.yaml`, `README.md`, `docs/datagen-doc.md`: user-facing configuration and commands.

---

### Task 1: New-VM Bootstrap and Configuration/Policy Contracts

**Files:**
- Create: `requirements-dev.txt`
- Create: `requirements-semantic.txt`
- Modify: `dags/scripts/harmful_prompt/config.py`
- Modify: `dags/scripts/harmful_prompt/policy.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_policy.py`

**Interfaces:**
- Consumes: existing `RunConfig`, `load_config()`, `Policy`, and `parse_policy()`.
- Produces: `GeneratorSpec`, `JudgeSettings`, `SemanticDedupSettings`, `RunConfig.effective_generators(force_model=None)`, `Policy.sha256`, and `Policy.effective_version`.

- [ ] **Step 1: Add development dependency files**

Create `requirements-dev.txt` with exactly:

```text
-r requirements.txt
pytest>=8.0,<10
```

Create `requirements-semantic.txt` with exactly:

```text
-r requirements.txt
sentence-transformers>=3.0,<6
```

- [ ] **Step 2: Write failing nested-config tests**

Append tests that exercise observable normalization and rejection:

```python
def test_generator_mix_and_stage_settings_load(tmp_path):
    path = write_config(tmp_path, {
        **MINIMAL,
        "generators": [
            {"model": "model-a", "weight": 7},
            {"model": "model-b", "weight": 3},
        ],
        "judge": {"enabled": True, "model": "judge-a", "reasoning_effort": "high"},
        "semantic_dedup": {"enabled": True, "threshold": 0.87},
    })
    config = load_config(path)
    assert [(g.model, g.weight) for g in config.effective_generators()] == [
        ("model-a", 0.7), ("model-b", 0.3)
    ]
    assert config.judge.model == "judge-a"
    assert config.judge.base_url == config.base_url
    assert config.judge.api_key_env == config.api_key_env
    assert config.semantic_dedup.threshold == 0.87


def test_model_override_forces_one_generator(tmp_path):
    config = load_config(write_config(tmp_path, {
        **MINIMAL,
        "generators": [{"model": "a", "weight": 1}, {"model": "b", "weight": 1}],
    }))
    assert [(g.model, g.weight) for g in config.effective_generators("forced")] == [
        ("forced", 1.0)
    ]


@pytest.mark.parametrize("generators", [
    [{"model": "a", "weight": 0}],
    [{"model": "a", "weight": 1}, {"model": "a", "weight": 2}],
    [{"model": "a", "weight": float("inf")}],
])
def test_invalid_generator_mix_is_rejected(tmp_path, generators):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, {**MINIMAL, "generators": generators}))


def test_unknown_nested_config_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown judge key"):
        load_config(write_config(tmp_path, {**MINIMAL, "judge": {"modle": "typo"}}))
```

- [ ] **Step 3: Run the config tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py -q
```

Expected: the new tests fail because nested settings and `effective_generators()` do not exist. If `.venv` does not exist, create it and install only `requirements-dev.txt` before rerunning; record the installation result in the task report.

- [ ] **Step 4: Implement nested settings and normalization**

Add frozen dataclasses with these fields and defaults:

```python
@dataclass(frozen=True)
class GeneratorSpec:
    model: str
    weight: float = 1.0


@dataclass(frozen=True)
class JudgeSettings:
    enabled: bool = False
    model: str = "gpt-oss-safeguard-120b"
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    concurrency: int = 2
    max_retries: int = 3
    timeout: float = 300.0
    reasoning_effort: str | None = "high"
    require_severity_match: bool = True
    include_aux_context: bool = True


@dataclass(frozen=True)
class SemanticDedupSettings:
    enabled: bool = False
    provider: str = "sentence_transformers"
    model: str = "BAAI/bge-m3"
    threshold: float = 0.90
    batch_size: int = 16
```

Add `generators`, `judge`, and `semantic_dedup` fields to `RunConfig`. In `load_config()`, reject unknown nested keys before constructing the dataclasses. Normalize weights in:

```python
def effective_generators(self, force_model: str | None = None) -> list[GeneratorSpec]:
    if force_model:
        return [GeneratorSpec(force_model.strip(), 1.0)]
    if not self.generators:
        return [GeneratorSpec(self.model, 1.0)]
    total = sum(spec.weight for spec in self.generators)
    return [GeneratorSpec(spec.model, spec.weight / total) for spec in self.generators]
```

Validate non-empty names, finite positive weights, unique model names, judge numeric bounds, reasoning effort in `{None, "low", "medium", "high"}`, semantic provider equal to `sentence_transformers`, threshold in `(0, 1]`, and positive batch size. Replace null judge endpoint/key values with inherited root values after `RunConfig` construction using `dataclasses.replace()`.

- [ ] **Step 5: Run config tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py -q
```

Expected: all config tests pass with no warnings.

- [ ] **Step 6: Write failing policy identity tests**

Append:

```python
def test_policy_accepts_judge_fields_and_has_stable_identity():
    row = {
        "policy_id": "A",
        "policy": "Reject disallowed requests.",
        "policy_version": "v2",
        "definitions": {"restricted": "content outside the allowed scope"},
        "example_prompts": ["request outside the allowed scope"],
        "allowed_examples": ["request inside the allowed scope"],
    }
    first = parse_policy(row)
    second = parse_policy(dict(reversed(list(row.items()))))
    assert first.allowed_examples == ("request inside the allowed scope",)
    assert first.definitions == {"restricted": "content outside the allowed scope"}
    assert first.sha256 == second.sha256
    assert first.effective_version == "v2"


def test_policy_without_version_uses_digest_version():
    policy = parse_policy({"policy_id": "A", "policy": "Reject disallowed requests."})
    assert policy.effective_version == f"sha256:{policy.sha256[:12]}"


def test_invalid_definitions_are_rejected():
    with pytest.raises(PolicyError, match="definitions"):
        parse_policy({"policy_id": "A", "policy": "x", "definitions": ["not-a-map"]})
```

- [ ] **Step 7: Run policy tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_policy.py -q
```

Expected: failures name the missing fields/properties.

- [ ] **Step 8: Implement extended policy fields and canonical digest**

Add `policy_version`, `definitions`, and `allowed_examples` to `Policy`. Reuse one list/string coercer for both example families. Build the digest from `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` encoded as UTF-8. Include only parsed fields used by the pipeline; do not include source path, line number, or mutable runtime state.

- [ ] **Step 9: Run focused and full tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_policy.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 10: Commit Task 1**

```bash
git add requirements-dev.txt requirements-semantic.txt dags/scripts/harmful_prompt/config.py dags/scripts/harmful_prompt/policy.py tests/test_config.py tests/test_policy.py
git commit -m "feat: add policy-aware quality configuration"
```

---

### Task 2: Exact Multi-Generator Allocation and Execution

**Files:**
- Modify: `dags/scripts/harmful_prompt/planner.py`
- Modify: `dags/scripts/harmful_prompt/client.py`
- Modify: `dags/scripts/harmful_prompt/generate.py`
- Modify: `tests/test_planner.py`
- Modify: `tests/test_generate_integration.py`

**Interfaces:**
- Consumes: `RunConfig.effective_generators(force_model)` and `GeneratorSpec` from Task 1.
- Produces: `WorkItem.model: str | None`, `assign_generators(items, generators)`, and `ChatClient.complete(system, user, model=None, extra_body=None)`.

- [ ] **Step 1: Write failing exact-allocation tests**

Append to `tests/test_planner.py`:

```python
from harmful_prompt.config import GeneratorSpec
from harmful_prompt.planner import assign_generators


def test_generator_allocation_is_exact_inside_every_bucket():
    policy = Policy("A", "x", dict(DEFAULT_SEVERITY))
    items = [WorkItem(policy, "low", 7), WorkItem(policy, "high", 3)]
    assigned = assign_generators(items, [
        GeneratorSpec("large", 0.7), GeneratorSpec("small", 0.3)
    ])
    got = {(item.severity, item.model): item.count for item in assigned}
    assert got == {
        ("low", "large"): 5, ("low", "small"): 2,
        ("high", "large"): 2, ("high", "small"): 1,
    }
    assert sum(item.count for item in assigned) == 10


def test_chunking_preserves_generator_assignment():
    policy = Policy("A", "x", dict(DEFAULT_SEVERITY))
    chunks = chunk_plan([WorkItem(policy, "medium", 5, model="small")], 2)
    assert [(item.count, item.model) for item in chunks] == [(2, "small"), (2, "small"), (1, "small")]
```

- [ ] **Step 2: Run planner tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_planner.py -q
```

Expected: failures identify the missing model field and allocator.

- [ ] **Step 3: Implement model-aware planning**

Extend `WorkItem` with `model: str | None = None`. Make `key` include `model` only when assigned. Implement `assign_generators()` by applying `_largest_remainder()` independently to each incoming item and emitting one item per positive model allocation. Preserve `model` inside `chunk_plan()`.

- [ ] **Step 4: Run planner tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_planner.py -q
```

Expected: all planner tests pass.

- [ ] **Step 5: Write failing multi-generator integration test**

Update the test stub to accept `model=None, extra_body=None`, then add:

```python
def test_full_run_uses_and_reports_exact_generator_mix(workspace, monkeypatch):
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
```

Make `stub_client()` call the responder with the model argument. Also change the helper returned by `json_responder()` to accept `model=None`, preserving every pre-existing test that uses that helper. The literal expected count is twelve because each of the six policy/severity buckets splits two records evenly across the two models, and each response contains one record.

- [ ] **Step 6: Run integration test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_generate_integration.py::test_full_run_uses_and_reports_exact_generator_mix -q
```

Expected: failure shows generation ignores the configured mix or does not report it.

- [ ] **Step 7: Add per-call model selection**

Change the client signature to:

```python
async def complete(
    self,
    system: str,
    user: str,
    model: str | None = None,
    extra_body: dict | None = None,
) -> Completion:
```

Pass `model or self.model` to the SDK. Add `extra_body` to the SDK request only when it is a non-empty dictionary. Do not create one client per generator; retain the existing shared semaphore.

In `generate.run()`, call `assign_generators()` before `chunk_plan()`, use `settings.effective_generators(model)`, pass `item.model` to `complete()`, record the actual model in `meta.generator_model`, and compute `by_generator` from produced records. When `--model` is present, it must force one model.

- [ ] **Step 8: Run focused and full tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_planner.py tests/test_generate_integration.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit Task 2**

```bash
git add dags/scripts/harmful_prompt/planner.py dags/scripts/harmful_prompt/client.py dags/scripts/harmful_prompt/generate.py tests/test_planner.py tests/test_generate_integration.py
git commit -m "feat: allocate generation across model mix"
```

---

### Task 3: Reproducible Generation Provenance

**Files:**
- Create: `dags/scripts/harmful_prompt/provenance.py`
- Create: `tests/test_provenance.py`
- Modify: `dags/scripts/harmful_prompt/generate.py`
- Modify: `tests/test_generate_integration.py`

**Interfaces:**
- Consumes: `Policy.sha256`, `Policy.effective_version`, rendered system/user messages, and actual work-item model.
- Produces: `text_sha256(text)`, `canonical_sha256(value)`, record-level template hashes, and manifest-level per-policy identity.

- [ ] **Step 1: Write failing provenance helper tests**

Create `tests/test_provenance.py`:

```python
from harmful_prompt.provenance import canonical_sha256, text_sha256


def test_text_digest_is_utf8_and_stable():
    assert text_sha256("中文") == text_sha256("中文")
    assert len(text_sha256("中文")) == 64


def test_canonical_digest_ignores_mapping_order():
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})


def test_canonical_digest_changes_with_value():
    assert canonical_sha256({"a": 1}) != canonical_sha256({"a": 2})
```

- [ ] **Step 2: Run helper tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_provenance.py -q
```

Expected: import fails because `provenance.py` does not exist.

- [ ] **Step 3: Implement canonical SHA-256 helpers**

Use:

```python
def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

- [ ] **Step 4: Run helper tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_provenance.py -q
```

Expected: three tests pass.

- [ ] **Step 5: Write failing generation-provenance integration test**

Append:

```python
def test_generation_records_policy_and_template_provenance(workspace, monkeypatch):
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
```

- [ ] **Step 6: Run the integration test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_generate_integration.py::test_generation_records_policy_and_template_provenance -q
```

Expected: missing metadata keys cause failure.

- [ ] **Step 7: Add record and manifest provenance**

Hash the exact rendered system and per-call user messages, not the template source file. Add the specified record metadata and a manifest `policies` mapping:

```python
"policies": {
    policy.policy_id: {
        "version": policy.effective_version,
        "sha256": policy.sha256,
    }
    for policy in policies
}
```

Retain `policy_file_sha256` and auxiliary hashes. Never add `api_key_env` or an environment value to the manifest.

- [ ] **Step 8: Run focused, credential, and full tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_provenance.py tests/test_generate_integration.py tests/test_no_hardcoded_secrets.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass and credential guards remain green.

- [ ] **Step 9: Commit Task 3**

```bash
git add dags/scripts/harmful_prompt/provenance.py tests/test_provenance.py dags/scripts/harmful_prompt/generate.py tests/test_generate_integration.py
git commit -m "feat: record reproducible generation provenance"
```

---

### Task 4: Blind Policy Judge Core and Offline Judge CLI

**Files:**
- Create: `dags/scripts/harmful_prompt/judge_prompt_builder.py`
- Create: `dags/scripts/harmful_prompt/judge_schema.py`
- Create: `dags/scripts/harmful_prompt/judge.py`
- Create: `tests/test_judge_prompt_builder.py`
- Create: `tests/test_judge_schema.py`
- Create: `tests/test_judge_integration.py`

**Interfaces:**
- Consumes: `Policy`, `JudgeSettings`, `ChatClient`, `load_documents()`, `build_context_block()`, `text_sha256()`, and generated JSONL records.
- Produces: `compile_judge_policy(policy) -> str`, `build_judge_messages(policy, prompt, aux_context="") -> tuple[str, str]`, `JudgeResult`, `parse_judge_response(raw, expected_policy_id)`, `audit_judgment(result, record, settings)`, and `judge.run(config, input, model=None, output_dir=None)`.

- [ ] **Step 1: Write failing blind-prompt tests**

Create `tests/test_judge_prompt_builder.py`:

```python
from harmful_prompt.judge_prompt_builder import build_judge_messages, compile_judge_policy
from harmful_prompt.policy import parse_policy


def make_policy():
    return parse_policy({
        "policy_id": "A1",
        "policy": "Reject requests outside the documented service scope.",
        "policy_version": "v1",
        "definitions": {"service scope": "documented supported operations"},
        "severity": {"low": "probe", "medium": "clear bypass", "high": "scalable bypass"},
        "example_prompts": ["request an unsupported operation"],
        "allowed_examples": ["ask about a supported operation"],
    })


def test_compiled_policy_contains_boundaries_and_version():
    text = compile_judge_policy(make_policy())
    assert "A1" in text and "v1" in text
    assert "documented supported operations" in text
    assert "request an unsupported operation" in text
    assert "ask about a supported operation" in text
    assert "low" in text and "medium" in text and "high" in text


def test_judge_prompt_is_blind_to_generator_claims():
    system, user = build_judge_messages(
        make_policy(),
        prompt="Please perform an operation outside the documented scope.",
        aux_context="Supported operations are listed in the service guide.",
    )
    combined = system + user
    assert "generation_rationale" not in combined
    assert "generator_model" not in combined
    assert "claimed severity" not in combined.lower()
    assert "untrusted" in combined.lower()
    assert "Supported operations" in combined
```

- [ ] **Step 2: Run prompt tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_judge_prompt_builder.py -q
```

Expected: import fails because the builder does not exist.

- [ ] **Step 3: Implement the deterministic policy compiler and blind messages**

The compiler must emit headings in this exact order: `POLICY ID`, `POLICY VERSION`, `RULE`, `DEFINITIONS`, `VIOLATIONS`, `ALLOWED`, `SEVERITY`, `OUTPUT`. Sort definition keys. Render absent example sections as the literal text `No examples supplied.` The system message declares content untrusted and requires one JSON object using the spec's normalized fields. The user message contains the compiled policy, optional auxiliary context, and prompt in separate XML-style delimiters.

- [ ] **Step 4: Run prompt tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_judge_prompt_builder.py -q
```

Expected: both tests pass.

- [ ] **Step 5: Write failing result parser and audit tests**

Create `tests/test_judge_schema.py` with plain, fenced, and reasoning-wrapped JSON fixtures:

```python
import pytest

from harmful_prompt.config import JudgeSettings
from harmful_prompt.judge_schema import JudgeParseError, audit_judgment, parse_judge_response


VALID = '{"verdict":"violation","matched_policy_id":"A1","severity":"medium",' \
        '"requires_external_context":false,"decision_summary":"The request crosses the rule."}'


@pytest.mark.parametrize("raw", [
    VALID,
    "```json\n" + VALID + "\n```",
    "<think>private reasoning</think>\n" + VALID,
    "[THINK]private reasoning[/THINK]\n" + VALID,
])
def test_parse_normalizes_supported_wrappers(raw):
    result = parse_judge_response(raw, "A1")
    assert result.verdict == "violation"
    assert result.severity == "medium"
    assert "private reasoning" not in result.decision_summary


@pytest.mark.parametrize("raw", [
    '{"verdict":"maybe","matched_policy_id":null,"severity":null,'
    '"requires_external_context":false,"decision_summary":"x"}',
    '{"verdict":"non_violation","matched_policy_id":null,"severity":"low",'
    '"requires_external_context":false,"decision_summary":"x"}',
    '{"verdict":"violation","matched_policy_id":"OTHER","severity":"low",'
    '"requires_external_context":false,"decision_summary":"x"}',
])
def test_parse_rejects_invalid_or_cross_policy_results(raw):
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, "A1")


def test_audit_accepts_only_policy_and_severity_match():
    result = parse_judge_response(VALID, "A1")
    record = {"violated_policy": "A1", "severity": "medium"}
    audit = audit_judgment(result, record, JudgeSettings(enabled=True))
    assert audit == {"policy_match": True, "severity_match": True, "accepted": True}


def test_external_context_never_auto_accepts():
    raw = VALID.replace("false", "true")
    result = parse_judge_response(raw, "A1")
    audit = audit_judgment(result, {"violated_policy": "A1", "severity": "medium"},
                           JudgeSettings(enabled=True))
    assert audit["accepted"] is False
```

- [ ] **Step 6: Run schema tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_judge_schema.py -q
```

Expected: import fails because the schema module does not exist.

- [ ] **Step 7: Implement strict parsing and auditing**

Create a frozen `JudgeResult` dataclass with the five normalized fields. Strip complete `<think>` and `[THINK]` blocks before trying whole JSON, fenced JSON, then the first balanced JSON object. Reject missing/extra-invalid field values, booleans represented as strings, a violation without the expected policy and severity, and a non-violation/ambiguous result with a severity or matched policy. Never retain stripped reasoning. Implement `audit_judgment()` exactly from the spec.

- [ ] **Step 8: Run schema tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_judge_schema.py -q
```

Expected: all parser and audit tests pass.

- [ ] **Step 9: Write failing offline judge integration tests**

Create a temporary config, one policy, and three benign abstract source records. Stub `judge.ChatClient` so it returns one violation matching severity, one non-violation, and one ambiguous result. Assert:

```python
assert manifest["source_records"] == 3
assert manifest["accepted"] == 1
assert manifest["rejected"] == 1
assert manifest["review"] == 1
assert len(read_jsonl(out / "judged_prompts.jsonl")) == 3
assert len(read_jsonl(out / "accepted_prompts.jsonl")) == 1
assert len(read_jsonl(out / "judge_review.jsonl")) == 1
assert len(read_jsonl(out / "judge_rejected.jsonl")) == 1
```

Add a separate API-failure test that proves a failed completion appears in `judge_review.jsonl`, does not disappear, and increments `failures`. Inspect the stub's user messages and assert generator rationale, generator model, and claimed severity text are absent.

- [ ] **Step 10: Run judge integration tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_judge_integration.py -q
```

Expected: import or command failures show the offline runner is absent.

- [ ] **Step 11: Implement the offline judge runner**

Implement `run(config, input, model=None, output_dir=None)` with Fire-compatible primitive arguments. Load the same config and selected policies, validate every source record with `validate_record()`, and map `violated_policy` to exactly one policy. Use one judge `ChatClient` configured from inherited `JudgeSettings`; pass `extra_body={"reasoning_effort": value}` only when the value is not null. Preserve source record fields, attach a top-level `judge` object containing normalized result, audit fields, hashes, and actual judge model, and write the four stage artifacts atomically through temporary sibling files followed by `Path.replace()`.

The union of accepted, rejected, and review IDs must equal the source IDs exactly once. `judged_prompts.jsonl` contains every source record with either a result or a normalized `judge_error`. The manifest includes source/output hashes, settings with only the environment variable name removed, policy/compiler hashes, count breakdowns, and no raw model response.

- [ ] **Step 12: Run judge, security, and full tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_judge_prompt_builder.py tests/test_judge_schema.py tests/test_judge_integration.py tests/test_no_hardcoded_secrets.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 13: Commit Task 4**

```bash
git add dags/scripts/harmful_prompt/judge_prompt_builder.py dags/scripts/harmful_prompt/judge_schema.py dags/scripts/harmful_prompt/judge.py tests/test_judge_prompt_builder.py tests/test_judge_schema.py tests/test_judge_integration.py
git commit -m "feat: add blind policy-conditioned judge"
```

---

### Task 5: Deterministic Multilingual Semantic Deduplication

**Files:**
- Create: `dags/scripts/harmful_prompt/embedding.py`
- Create: `dags/scripts/harmful_prompt/semantic_dedup.py`
- Create: `tests/test_semantic_dedup.py`

**Interfaces:**
- Consumes: `SemanticDedupSettings`, judged/accepted JSONL, stable IDs, and optional `judge.score`.
- Produces: `SentenceTransformerEmbedder.encode(texts)`, `duplicate_clusters(records, vectors, threshold)`, `choose_representative(records)`, and `semantic_dedup.run(config, input, output_dir=None, embedder=None)`.

- [ ] **Step 1: Write failing vector-clustering tests**

Create `tests/test_semantic_dedup.py`:

```python
from harmful_prompt.semantic_dedup import choose_representative, duplicate_clusters


def records():
    return [
        {"id": "b", "prompt": "short text", "violated_policy": "A1"},
        {"id": "a", "prompt": "a much longer equivalent text", "violated_policy": "A1"},
        {"id": "c", "prompt": "different policy text", "violated_policy": "A2"},
    ]


def test_clusters_are_transitive_within_policy():
    vectors = [[1.0, 0.0], [0.95, 0.05], [1.0, 0.0]]
    clusters, cross_policy = duplicate_clusters(records(), vectors, threshold=0.90)
    assert [[r["id"] for r in cluster] for cluster in clusters] == [["a", "b"]]
    assert cross_policy == [("b", "c", 1.0), ("a", "c", pytest.approx(0.9986, abs=1e-3))]


def test_representative_uses_score_then_length_then_id():
    scored = [
        {"id": "a", "prompt": "short", "judge": {"score": 0.7}},
        {"id": "b", "prompt": "longer", "judge": {"score": 0.9}},
    ]
    assert choose_representative(scored)["id"] == "b"
    assert choose_representative(records()[:2])["id"] == "b"
```

Import `pytest` in the file. Derive the approximate cosine value by hand once and keep the literal in the assertion.

- [ ] **Step 2: Run clustering tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_semantic_dedup.py -q
```

Expected: import fails because the module does not exist.

- [ ] **Step 3: Implement pure cosine clustering and deterministic selection**

Validate equal record/vector lengths, non-empty equal-dimensional vectors, finite values, and non-zero norms. Normalize vectors, enumerate each pair once, collect cross-policy similarities above threshold for audit without unioning them, and union same-policy pairs. Emit only clusters with at least two members, sorted by member ID and then first ID. Select representative using descending numeric `judge.score` when present, then ascending stripped prompt length, then lexical ID.

- [ ] **Step 4: Run clustering tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_semantic_dedup.py -q
```

Expected: clustering and selection tests pass.

- [ ] **Step 5: Write failing CLI artifact test with an injected embedder**

Add a fake embedder whose `encode()` returns fixed vectors and assert the runner writes:

```python
assert [row["id"] for row in read_jsonl(out / "deduped_prompts.jsonl")] == ["b", "c"]
duplicates = read_jsonl(out / "semantic_duplicates.jsonl")
assert duplicates == [{"representative_id": "b", "duplicate_ids": ["a"], "policy_id": "A1"}]
assert manifest["source_records"] == 3
assert manifest["retained"] == 2
assert manifest["removed"] == 1
assert manifest["clusters"] == 1
```

Also assert source SHA-256, provider/model, threshold, and cross-policy audit count exist in the manifest.

- [ ] **Step 6: Run the CLI test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_semantic_dedup.py -q
```

Expected: runner/adapter behavior is missing.

- [ ] **Step 7: Implement lazy embedding adapter and offline runner**

`SentenceTransformerEmbedder.__init__()` imports `SentenceTransformer` inside the method and raises a user-facing `RuntimeError` naming `requirements-semantic.txt` when unavailable. `encode()` requests normalized embeddings and converts the result to plain lists. The runner accepts an injected embedder for tests, never imports the optional dependency when dedup is disabled, preserves source order for retained records, attaches cluster metadata only to representatives, and writes artifacts atomically.

- [ ] **Step 8: Run semantic and full tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_semantic_dedup.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass without installing or downloading sentence-transformers.

- [ ] **Step 9: Commit Task 5**

```bash
git add dags/scripts/harmful_prompt/embedding.py dags/scripts/harmful_prompt/semantic_dedup.py tests/test_semantic_dedup.py
git commit -m "feat: add semantic duplicate audit stage"
```

---

### Task 6: Offline Human-Gold Evaluation and User Documentation

**Files:**
- Create: `dags/scripts/harmful_prompt/evaluate.py`
- Create: `tests/test_evaluate.py`
- Modify: `configs/example.yaml`
- Modify: `README.md`
- Modify: `docs/datagen-doc.md`

**Interfaces:**
- Consumes: human gold JSONL and `judged_prompts.jsonl` joined by `id`.
- Produces: `compute_metrics(gold_rows, prediction_rows) -> dict` and `evaluate.run(gold, predictions, output=None)`.

- [ ] **Step 1: Write failing hand-derived metric tests**

Create `tests/test_evaluate.py`:

```python
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
```

- [ ] **Step 2: Run evaluation tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_evaluate.py -q
```

Expected: import fails because evaluator code is absent.

- [ ] **Step 3: Implement strict joins and metrics**

Validate unique IDs and allowed labels. Treat missing predictions as uncovered, not as implicit non-violations. Compute binary metrics over covered predictions only and report numerator/denominator counts. Compute policy-match accuracy and severity metrics only on covered gold violations with usable labels. Implement linearly weighted kappa over ordered labels `low=0`, `medium=1`, `high=2`; return null when the denominator is zero rather than dividing by zero. The CLI writes stable, indented JSON and also returns the dictionary.

- [ ] **Step 4: Run evaluation tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_evaluate.py -q
```

Expected: all metric tests pass.

- [ ] **Step 5: Document the exact local workflows**

Update `configs/example.yaml` with disabled-by-default nested sections matching the spec and a 70/30 Mistral/TAIDE example. Update README and `docs/datagen-doc.md` with these commands:

```bash
python3 -m harmful_prompt.generate run --config configs/example.yaml --dry_run
python3 -m harmful_prompt.generate run --config configs/example.yaml
python3 -m harmful_prompt.judge run --config configs/example.yaml --input output/example-run/harmful_prompts.jsonl
python3 -m harmful_prompt.semantic_dedup run --config configs/example.yaml --input output/example-run/accepted_prompts.jsonl
python3 -m harmful_prompt.evaluate run --gold datasets/judge_gold.jsonl --predictions output/example-run/judged_prompts.jsonl
```

Explain artifact meanings, judge blindness, human-review routing, optional embedding installation, threshold calibration, the held-out-policy evaluation rule, and that generated samples are not human gold. Do not alter `docs/demo-walkthrough.zh-TW.md` because it contains a pre-existing user change.

- [ ] **Step 6: Run example-config, evaluator, and full tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_example_config.py tests/test_evaluate.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 6**

```bash
git add dags/scripts/harmful_prompt/evaluate.py tests/test_evaluate.py configs/example.yaml README.md docs/datagen-doc.md
git commit -m "docs: add quality-stage evaluation workflow"
```

---

### Task 7: End-to-End Verification and Controlled Medusa Pilot

**Files:**
- Modify only if a failing verification exposes a defect: the smallest file covered by a new failing regression test.
- Create runtime artifacts only under ignored `output/`; do not commit model outputs.

**Interfaces:**
- Consumes: all commands and artifacts from Tasks 1–6.
- Produces: verified test evidence, dry-run evidence, and, only when credentials/models are available, an 18-record balanced live smoke report followed by a separately authorized 90-record TAIDE pilot configuration.

- [ ] **Step 1: Verify worktree and dependency state**

Run:

```bash
git status --short --branch
.venv/bin/python --version
.venv/bin/python -m pip check
```

Expected: the implementation branch contains no unrelated changes, Python reports 3.12.x, and `pip check` reports no broken requirements.

- [ ] **Step 2: Run the complete automated suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass with no warnings or errors. If a regression appears, first add the smallest failing test that reproduces it, observe RED, fix minimally, and rerun the focused and complete suites before committing.

- [ ] **Step 3: Run generation-only backward-compatibility dry runs**

Run:

```bash
PYTHONPATH=dags/scripts .venv/bin/python -m harmful_prompt.generate run --config configs/demo_bank.yaml --dry_run --output_dir /tmp/harmful-prompt-demo-bank
PYTHONPATH=dags/scripts .venv/bin/python -m harmful_prompt.generate run --config configs/example.yaml --dry_run --total 18 --output_dir /tmp/harmful-prompt-mix
```

Expected: the legacy config plans one generator; the new example plans 18 records with exact policy/severity/generator totals, and neither command requests an API key.

- [ ] **Step 4: Validate dry-run contents without exposing prompts or secrets**

Use a short Python command to parse each JSONL and report only counts by `policy_id`, `severity`, and assigned model. Confirm the total is 18 and both configured generator names appear. Search dry-run and manifest paths for the literal names of known environment variables only; never print environment values.

- [ ] **Step 5: Check credential and model availability safely**

Run `test -n "$NCHC_API_KEY"` without echoing the variable. If absent, record `LIVE_PILOT_BLOCKED: NCHC_API_KEY is not set` and stop live calls while still completing all code verification. If present, call the Inner Medusa model-list endpoint and retain only exact model names matching `Mistral-Large-3-675B-Instruct-2512`, `Gemma-3-TAIDE-12b-Chat`, and case-insensitive substrings `oss` plus `safeguard`; do not save headers or credentials.

- [ ] **Step 6: Run an 18-record balanced live smoke only when all three model IDs resolve**

Create a temporary YAML under `/tmp` derived from `configs/example.yaml` with total 18, equal 0.5/0.5 generator weights, one record per policy/severity/model cell, judge enabled, semantic dedup disabled, and a unique run ID. Run generation, then the judge over the generated JSONL. Report only aggregate counts, parse strategies, per-generator counts, judge partitions, latency, and failures. Do not reproduce prompt text in the report.

Expected: requested total 18; both generators and all policy/severity cells represented; accepted/review/rejected counts sum to 18. A missing model or endpoint incompatibility is recorded exactly and is not worked around by substituting an unreviewed model.

- [ ] **Step 7: Gate the 90-record TAIDE pilot**

Do not launch the 90-record pilot unless the 18-record smoke has zero API/parse failures, all partition totals reconcile, and the resolved judge is the intended safeguard model. When the gate passes, prepare but do not commit a `/tmp` pilot config for 90 TAIDE records with 10 records per policy/severity cell. Launching the cost-bearing 90-record call is a separate user-visible authorization checkpoint under the spec's side-effect boundary.

- [ ] **Step 8: Final verification commit, if verification required code changes**

```bash
git add dags/scripts/harmful_prompt tests
git commit -m "fix: close quality pipeline verification gaps"
```

If no code changed, do not create an empty commit.

- [ ] **Step 9: Record final handoff evidence**

Record commit range, complete test command/output summary, dry-run totals, live-smoke status, unresolved model identifiers, and exact paths of uncommitted runtime reports. Explicitly state that DAG work and custom SLM training remain deferred.
