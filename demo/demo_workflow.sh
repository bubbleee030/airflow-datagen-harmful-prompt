#!/usr/bin/env bash
# ===========================================================================
# Demo: the whole harmful-prompt workflow, stage by stage.
#
#   policy ─┐
#  severity ┼─→ plan ─→ compiled prompt ─→ LLM ─→ parse ─→ dataset
#  aux docs ┤
#  examples ┘
#
# Stages 1-3 and 6-8 need no API key. Stage 4 makes ONE small real call and
# is skipped automatically if NCHC_API_KEY is unset.
#
#   bash demo/demo_workflow.sh                 # dry stages only
#   NCHC_API_KEY=... bash demo/demo_workflow.sh   # includes the live call
# ===========================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PYTHONPATH="$REPO/dags/scripts"

WORK="$(mktemp -d)"; export WORK
trap 'rm -rf "$WORK"' EXIT
cp -r configs/policies configs/aux "$WORK/"

step() { printf '\n\033[1m═══ %s\033[0m\n' "$*"; }
sub()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

# ===========================================================================
step "STAGE 1 — INPUT: the policy file is the only domain knowledge"
note "file: configs/policies/example_platform.jsonl"
python3 - <<'PY' | sed 's/^/  /'
import json
rows = [json.loads(l) for l in open("configs/policies/example_platform.jsonl", encoding="utf-8") if l.strip()]
print(f"{len(rows)} policies\n")
r = rows[0]
print(f"policy_id       : {r['policy_id']}")
print(f"policy (en)     : {r['policy'][:74]}…")
print(f"policy_zh_TW    : {r['policy_zh_TW'][:40]}…")
print(f"severity keys   : {list(r['severity'])}   <- legacy naming, auto-normalised")
print(f"example_prompts : {len(r['example_prompts'])}")
for e in r["example_prompts"][:1]:
    print(f"    e.g. {e[:60]}…")
PY
note ""
note "Required fields are only policy_id and policy. Everything else is optional."

# ===========================================================================
step "STAGE 2 — PLAN: config turns into concrete units of work"
python3 - <<'PY' > "$WORK/plan.yaml"
import yaml, pathlib, os
c = yaml.safe_load(open("configs/example.yaml"))
c.update({"total": 12, "output_dir": os.environ["WORK"] + "/out"})
print(yaml.safe_dump(c, allow_unicode=True, sort_keys=False))
PY
note "total: 12, severity_ratio 0.3/0.4/0.3, 3 policies"
python3 -m harmful_prompt.generate run --config "$WORK/plan.yaml" --dry_run 2>&1 \
  | grep -E "^policy +low|^A[0-9]|^TOTAL|calls:" | sed 's/^/    /'
note ""
note "Allocation is exact: the plan always sums to the requested total."

# ===========================================================================
step "STAGE 3 — COMPILED PROMPT: exactly what would be sent"
f=$(ls -t "$WORK/out"/*/dry_run_prompts.jsonl 2>/dev/null | head -1)
sub "  System message (fixed per run):"
python3 - "$f" <<'PY' | sed 's/^/    /'
import json, sys
print(json.loads(open(sys.argv[1], encoding="utf-8").readline())["system"])
PY
sub "  User message (one policy, one severity, per call):"
python3 - "$f" <<'PY' | sed 's/^/    /'
import json, sys
row = json.loads(open(sys.argv[1], encoding="utf-8").read().splitlines()[1])
text = row["user"]
head, _, tail = text.partition("[Reference material")
print(head.rstrip())
print("\n    …[auxiliary document injected here]…\n")
print(tail.split("\n\n")[-1].strip())
PY
note ""
note "One policy + one severity per call: mixing them caused category drift."

# ===========================================================================
step "STAGE 4 — GENERATE: one real call"
if [[ -z "${NCHC_API_KEY:-}" ]]; then
    note "SKIPPED — NCHC_API_KEY is not set."
    note "Run:  NCHC_API_KEY=... bash demo/demo_workflow.sh"
else
    python3 - <<'PY' > "$WORK/live.yaml"
import yaml, os
c = yaml.safe_load(open("configs/example.yaml"))
c.update({"total": 2, "policy_ids": ["A1"], "max_prompts_per_call": 2,
          "output_dir": os.environ["WORK"] + "/live"})
print(yaml.safe_dump(c, allow_unicode=True, sort_keys=False))
PY
    note "generating 2 prompts for A1 …"
    python3 -m harmful_prompt.generate run --config "$WORK/live.yaml" 2>&1 \
      | grep -E "A1:|produced" | sed 's/^/    /'
    LIVE=$(ls -td "$WORK/live"/*/ 2>/dev/null | head -1)
fi

# ===========================================================================
step "STAGE 5 — OUTPUT: the dataset record"
SRC="${LIVE:-}"
if [[ -n "$SRC" && -f "$SRC/harmful_prompts.jsonl" ]]; then
    note "from the live run just made:"
else
    SRC="samples"; note "from the committed sample run (samples/):"
fi
python3 - "$SRC" <<'PY' | sed 's/^/    /'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
f = d / "harmful_prompts.jsonl"
if not f.exists():
    f = d / "taiwan_ai_rap_harmful_prompts.jsonl"
r = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
for k in ("id", "violated_policy", "severity", "domain"):
    print(f"{k:22s}: {r[k]}")
print(f"{'prompt':22s}: {r['prompt'][:70]}…")
print(f"{'generation_rationale':22s}: {r['generation_rationale'][:70]}…")
print(f"{'meta':22s}: {json.dumps(r.get('meta', {}), ensure_ascii=False)[:70]}…")
PY
note ""
note "Six required fields, exactly as the issue's 驗收標準 specifies."
note "id is a hash of policy+prompt, so reruns produce identical ids -> easy dedup."

# ===========================================================================
step "STAGE 6 — PROVENANCE: every run records how it was made"
python3 - <<'PY' | sed 's/^/    /'
import json
m = json.load(open("samples/taiwan_ai_rap_run_manifest.json", encoding="utf-8"))
for k in ("run_id", "requested", "produced", "api_calls", "failures"):
    print(f"{k:20s}: {m[k]}")
print(f"{'by_severity':20s}: {m['by_severity']}")
print(f"{'by_policy':20s}: {m['by_policy']}")
print(f"{'parse_strategies':20s}: {m['parse_strategies']}")
print(f"{'policy_file_sha256':20s}: {m['policy_file_sha256'][:32]}…")
PY
note ""
note "Input hashes + resolved parameters = the run is reproducible."

# ===========================================================================
step "STAGE 7 — ROBUSTNESS: what happens when the model misbehaves"
python3 - <<'PY' | sed 's/^/    /'
import sys; sys.path.insert(0, "dags/scripts")
from harmful_prompt.parser import parse_response
import json
cases = [
    ("clean JSON",            json.dumps({"prompts":[{"prompt":"p","generation_rationale":"r"}]})),
    ("wrapped in ```json",    '```json\n{"prompts":[{"prompt":"p","generation_rationale":"r"}]}\n```'),
    ("reasoning block first", '[THINK]thinking[/THINK]{"prompts":[{"prompt":"p","generation_rationale":"r"}]}'),
    ("TRUNCATED mid-array",   '{"prompts":[{"prompt":"first","generation_rationale":"r"},{"prompt":"cut o'),
    ("old free-text format",  '問題：先開通再補件\nSeverity：Level 2 / Moderate'),
    ("a refusal",             "I'm sorry, but I can't help with that."),
]
for label, raw in cases:
    r = parse_response(raw)
    status = f"{len(r.prompts)} recovered via '{r.strategy}'" if r.ok else f"FAILED -> {r.error[:34]}…"
    print(f"{label:24s} -> {status}")
PY
note ""
note "The truncated case is real: it cost a whole call before the salvage parser."
note "A refusal fails loudly and lands in failures.jsonl -- never silently dropped."

# ===========================================================================
step "STAGE 8 — GENERALITY: same code, different domain"
python3 - <<'PY' > "$WORK/bank.yaml"
import yaml, os
c = yaml.safe_load(open("configs/example.yaml"))
c.update({"total": 6, "policy_file": "policies/demo_bank.jsonl", "policy_ids": [],
          "aux_documents": [], "language": "English",
          "domain": "Taiwan bank online customer service",
          "output_dir": os.environ["WORK"] + "/bank"})
print(yaml.safe_dump(c, allow_unicode=True, sort_keys=False))
PY
note "policy_file swapped to a 2-line bank policy with NO severity, NO examples, NO aux docs:"
python3 -m harmful_prompt.generate run --config "$WORK/bank.yaml" --dry_run 2>&1 \
  | grep -E "^policy +low|^B-|^TOTAL" | sed 's/^/    /'
note ""
note "Nothing in the code is RAP-specific. See samples/demo_bank_*.jsonl for real output."

step "Workflow complete"
note "Tests: python3 -m pytest -q tests/     (191, no API key needed)"
