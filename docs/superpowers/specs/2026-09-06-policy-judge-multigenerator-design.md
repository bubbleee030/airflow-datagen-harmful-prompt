# Policy-Conditioned Judge and Multi-Generator Design

**Date:** 2026-09-06

**Status:** Approved direction; implementation pending

## Objective

Extend the existing local harmful-prompt generator so one YAML configuration can:

1. allocate exact generation quotas across more than one generator model;
2. validate generated records against user-provided policies with an independent,
   policy-conditioned judge;
3. quarantine ambiguous, context-dependent, and metadata-inconsistent records;
4. remove semantic near-duplicates across generator sources; and
5. record enough provenance to reproduce every generation, judgment, and deduplication
   decision.

The Airflow DAG remains out of scope. Each capability must work as a local Python module
and CLI so a later DAG only wraps the same entry points.

## Research Decision

Do not train one judge per user policy. The primary implementation uses a fixed judge
model that receives the active policy at inference time. The first high-quality candidate
is `gpt-oss-safeguard-120b` on Inner Medusa. Lower-cost candidates such as
`gpt-oss-safeguard-20b`, `mistralai/Shieldstral-1.0-3B`, and
`nvidia/Nemotron-3.5-Content-Safety` are evaluated through the same record contract.

A custom small judge is a later optimization. If built, it must remain
policy-conditioned and be trained across many policies, policy paraphrases, and
same-content/different-policy contrastive pairs. It must be evaluated on entirely unseen
policies. It is not part of this implementation.

## Existing Behavior That Must Remain Compatible

- A two-field policy row containing only `policy_id` and `policy` remains valid.
- The root `model` configuration key remains the single-generator fallback.
- `python3 -m harmful_prompt.generate run --config ...` continues to work.
- Existing top-level dataset fields remain unchanged:
  `id`, `prompt`, `violated_policy`, `severity`, `domain`, and
  `generation_rationale`.
- A generation dry run never reads an API key or calls a model.
- Secrets are read only from named environment variables and never written to records,
  manifests, logs, dry-run output, or errors.
- Exact prompt identifiers remain derived from `(violated_policy, prompt)`.

## Configuration Contract

The existing flat generator settings remain valid. Three optional structured sections
are added.

```yaml
generators:
  - model: "Mistral-Large-3-675B-Instruct-2512"
    weight: 0.7
  - model: "Gemma-3-TAIDE-12b-Chat"
    weight: 0.3

judge:
  enabled: true
  model: "gpt-oss-safeguard-120b"
  base_url: null
  api_key_env: null
  temperature: 0.0
  max_tokens: 4096
  concurrency: 2
  max_retries: 3
  timeout: 300
  reasoning_effort: "high"
  require_severity_match: true
  include_aux_context: true

semantic_dedup:
  enabled: true
  provider: "sentence_transformers"
  model: "BAAI/bge-m3"
  threshold: 0.90
  batch_size: 16
```

Rules:

- If `generators` is absent or empty, root `model` receives 100% of every quota.
- Generator weights must be finite and strictly positive. Duplicate model names and
  unknown nested keys are errors. Weights are normalized internally.
- `--model X` explicitly forces a single-generator run with model `X`, even when the YAML
  contains a generator mix.
- Null `judge.base_url` and `judge.api_key_env` inherit the root settings.
- Judge temperature defaults to zero. `reasoning_effort` may be null when an endpoint
  does not support it.
- Judge and dedup stages are opt-in. Existing generation-only configurations do not
  change behavior.
- A semantic threshold is an experiment parameter, not a universal truth. The example
  value is a starting point and must be calibrated on labeled duplicate pairs before a
  production release.

## Extended Policy Contract

The following fields are optional additions to each JSONL policy row:

```json
{
  "policy_id": "A1",
  "policy": "Natural-language rule",
  "policy_version": "2026-09-06",
  "definitions": {"term": "operational definition"},
  "example_prompts": ["known violating boundary example"],
  "allowed_examples": ["known allowed boundary example"]
}
```

`example_prompts` retains its current meaning and is treated as violating examples.
Definitions must be a string-to-string object. `allowed_examples` accepts either a string
or list of strings. Empty values are removed. Unknown top-level policy fields remain
ignored for backward compatibility.

Every parsed policy exposes:

- `sha256`: a digest of a canonical JSON representation of the policy fields used by the
  pipeline;
- `effective_version`: the explicit `policy_version`, or `sha256:<first 12 hex chars>`
  when no version is supplied.

## Multi-Generator Planning

Generation quotas are assigned in this order:

1. exact total across policies;
2. exact per-policy total across severity levels; and
3. exact per-policy/per-severity total across generator weights.

All three allocations use the existing largest-remainder rule. Therefore every bucket
sums exactly to its parent, including small totals. Each call-level `WorkItem` carries its
assigned `model`. Chunking preserves the model assignment.

The shared `ChatClient` accepts a per-call model override so one concurrency semaphore
limits aggregate traffic across all generators. Every generated record records its actual
generator model, and the run manifest reports counts by generator.

## Judge Contract

Judgment is deliberately independent from the generator's claims.

The first-pass judge input contains only:

- one compiled policy specification;
- the generated `prompt`;
- the policy severity rubric; and
- auxiliary context when enabled.

It must not contain `generation_rationale`, the claimed `severity`, the generator model,
or previous judge output. The content is delimited as untrusted data and any instruction
inside it is not executable by the judge.

The compiled policy format contains, in order:

1. policy ID and effective version;
2. normative statement;
3. definitions;
4. violation criteria and violating examples;
5. allowed boundary examples;
6. low/medium/high severity definitions; and
7. literal JSON output rules.

The judge returns one object with this normalized shape:

```json
{
  "verdict": "violation",
  "matched_policy_id": "A1",
  "severity": "medium",
  "requires_external_context": false,
  "decision_summary": "Short audit explanation grounded in the supplied policy."
}
```

Allowed verdicts are `violation`, `non_violation`, and `ambiguous`. A non-violation or
ambiguous result has a null severity. `requires_external_context` is true when the result
depends on facts unavailable in the policy or supplied auxiliary context. The persisted
result contains no raw chain-of-thought.

The metadata audit is computed after the independent verdict:

- `policy_match` is true only when the verdict is `violation` and the matched policy is
  the record's claimed policy;
- `severity_match` compares the independent judge severity with the claimed severity;
- `accepted` requires `policy_match`, no external-context requirement, and, when
  configured, `severity_match`;
- malformed/API-failed, ambiguous, external-context, and severity-mismatched records go
  to review rather than being silently accepted or discarded;
- clear `non_violation` results go to the rejected output.

The judge CLI reads a generated JSONL file and writes five artifacts without overwriting
the generation source:

- `judged_prompts.jsonl`: every successfully assessed record;
- `accepted_prompts.jsonl`: accepted records only;
- `judge_rejected.jsonl`: clear non-violations only;
- `judge_review.jsonl`: ambiguous, context-dependent, metadata-mismatched, or failed
  records;
- `judge_manifest.json`: counts, model settings, policy/template hashes, and file hashes.

## Semantic Deduplication Contract

Semantic deduplication is a separate offline stage so embeddings can be recalculated or
thresholds changed without regenerating or re-judging data.

- The production adapter lazily imports `sentence_transformers` and loads the configured
  model. The base installation does not require this optional dependency.
- Tests inject deterministic vectors and never download a model.
- Texts are normalized only for surrounding whitespace before embedding; the original
  prompt remains unchanged in output.
- Vectors are L2-normalized and compared with cosine similarity.
- An undirected union-find graph forms transitive duplicate clusters for all pairs whose
  similarity is greater than or equal to the configured threshold.
- The representative is selected deterministically by highest available judge score,
  then shortest prompt, then lexical record ID. Because reasoner judges may not expose a
  calibrated score, the latter two rules are the normal fallback.
- The stage runs across generator models and policies. It does not merge records that
  claim different policies; cross-policy similarities are reported for audit but retained.
- Outputs are `deduped_prompts.jsonl`, `semantic_duplicates.jsonl`, and
  `dedup_manifest.json`.

## Provenance Contract

Generation metadata adds:

- `generator_model`;
- `policy_version`;
- `policy_sha256`;
- `generation_system_sha256`;
- `generation_user_sha256`; and
- `run_id`.

Judge metadata adds:

- `judge_model`;
- `judge_policy_sha256`;
- `judge_template_sha256`;
- normalized verdict fields;
- `policy_match`, `severity_match`, and `accepted`; and
- the source dataset SHA-256.

Dedup metadata and manifest record the embedding provider/model, threshold, source hash,
cluster count, removed count, and representative-to-duplicate ID mapping.

## Evaluation Protocol

The code provides an offline evaluator for human-labeled JSONL. A gold row contains:

```json
{
  "id": "case-001",
  "prompt": "Boundary-case user request",
  "policy_id": "A1",
  "gold_verdict": "violation",
  "gold_severity": "medium"
}
```

Evaluation joins predictions by `id` and reports:

- coverage and missing prediction count;
- violation precision, recall, and F1;
- policy-match accuracy on gold violations;
- severity accuracy and linearly weighted Cohen's kappa on gold violations with both
  severity labels present; and
- counts by verdict and severity.

Gold construction itself is a human task and is not fabricated by the implementation.
Existing generated samples may be used for smoke testing but must never be called gold.

The target study is 60 human-labeled cases per current policy: 30 violations balanced by
severity, 15 clear allowed cases, and 15 near-boundary or cross-policy hard negatives.
At least two additional policies remain completely unseen until prompts, templates, and
thresholds are frozen.

## Second-Generator Pilot

The engineering implementation supports any configured mix. The first planned pilot is:

- `Mistral-Large-3-675B-Instruct-2512`: 90 stratified records;
- `Gemma-3-TAIDE-12b-Chat`: 90 new records;
- 10 records in every policy-by-severity cell for each model.

The judge input is blind to generator identity. Compare judge acceptance, policy match,
severity agreement, parse failure, semantic unique rate, and framing diversity. Begin a
production mix at 70% Mistral and 30% TAIDE only if the TAIDE acceptance rate is no more
than five percentage points below Mistral and semantic uniqueness improves. These are
provisional gates and must be frozen before the pilot output is inspected.

## Validation and Safety Boundaries

- All automated tests use abstract or benign policy fixtures and stub external API calls.
- No test or documentation embeds operational harmful instructions.
- A live run is never part of `pytest`.
- A live Medusa smoke test runs only when `NCHC_API_KEY` is already present in the
  environment; absence of the variable is a reported external blocker, not a reason to
  weaken tests.
- Model availability and exact Inner Medusa identifiers are checked at the live pilot
  checkpoint, not assumed from public model names.
- No push, merge, publish, model download, or DAG change is authorized by this spec.

## Acceptance Criteria

1. Existing single-model configs and tests remain compatible.
2. Multi-generator allocation is exact in every policy/severity bucket and observable in
   dry-run and manifest output.
3. Judge prompts exclude generator rationale, claimed severity, and generator identity.
4. Judge parsing handles plain JSON, fenced JSON, and reasoning-wrapped JSON; invalid
   values fail closed into review.
5. Accepted/review/rejected outputs are exhaustive and mutually exclusive.
6. Semantic deduplication is deterministic and testable without network access.
7. Every stage records source, policy, model, and template hashes without secrets.
8. Offline metrics are verified by hand-derived fixtures.
9. Base and optional dependency setup is documented for the new VM.
10. The full automated suite passes before any live pilot is attempted.
