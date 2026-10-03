#!/usr/bin/env bash
# ===========================================================================
# Demo: the YAML config actually controls the pipeline.
#
# Every step below changes ONE key in the config and shows the measurable
# effect. Nothing is hardcoded in Python -- if the YAML says 12 prompts across
# one policy at low severity, that is exactly what the pipeline plans.
#
# Runs entirely in --dry_run: no API calls, no API key, no cost.
#
#   bash demo/demo_yaml_control.sh
# ===========================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
export PYTHONPATH="$REPO/dags/scripts"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -r configs/policies configs/aux "$WORK/"

BASE="configs/example.yaml"
GEN="python3 -m harmful_prompt.generate run"

bold()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
rule()  { printf '%s\n' "------------------------------------------------------------------"; }
note()  { printf '  %s\n' "$*"; }

# Write a variant of the baseline config with the given YAML patch applied.
variant() {  # $1 = output name, $2 = python dict literal of overrides
    python3 - "$1" "$2" <<'PY'
import sys, yaml, pathlib, ast, os
name, patch = sys.argv[1], ast.literal_eval(sys.argv[2])
cfg = yaml.safe_load(open("configs/example.yaml"))
work = os.environ["WORK"]
cfg.update(patch)
cfg["output_dir"] = f"{work}/out_{name}"
pathlib.Path(f"{work}/{name}.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
PY
}
export WORK

plan() {  # print just the plan table + call count
    $GEN --config "$1" --dry_run 2>&1 \
      | grep -E "^policy +low|^[A-Z][^ ]* +[0-9]|^TOTAL|calls:" \
      | sed 's/^/    /'
}

fail() {  # print only the error line
    $GEN --config "$1" --dry_run 2>&1 | grep -E "Error:" | tail -1 | sed 's/^/    /'
}

# ===========================================================================
bold "0. The config we start from"
rule
grep -E "^(total|policy_ids|severity_ratio|language|max_prompts_per_call|aux_documents)|^  (low|medium|high|- aux)" "$BASE" | sed 's/^/    /'

# ===========================================================================
bold "1. total  --  how many prompts to generate"
rule
for n in 90 30 12; do
    variant "total_$n" "{'total': $n}"
    note "total: $n"
    plan "$WORK/total_$n.yaml"
done
note "The plan always sums to exactly the requested total (largest-remainder)."

# ===========================================================================
bold "2. severity_ratio  --  the low/medium/high mix"
rule
variant "ratio_even"  "{'total': 90, 'severity_ratio': {'low': 1, 'medium': 1, 'high': 1}}"
note "severity_ratio: 1 / 1 / 1  (equal)"
plan "$WORK/ratio_even.yaml"
variant "ratio_high"  "{'total': 90, 'severity_ratio': {'low': 0, 'medium': 0, 'high': 1}}"
note "severity_ratio: 0 / 0 / 1  (high only)"
plan "$WORK/ratio_high.yaml"
note "Weights are normalised, so 20/50/30 and 0.2/0.5/0.3 mean the same thing."

# ===========================================================================
bold "3. policy_ids  --  which policies to target"
rule
variant "pol_all" "{'total': 12, 'policy_ids': []}"
note "policy_ids: []            (empty = every policy in the file)"
plan "$WORK/pol_all.yaml"
variant "pol_a1"  "{'total': 12, 'policy_ids': ['A1']}"
note "policy_ids: ['A1']        (only A1)"
plan "$WORK/pol_a1.yaml"

# ===========================================================================
bold "4. max_prompts_per_call  --  how many API calls the same work becomes"
rule
for n in 3 5 12; do
    variant "chunk_$n" "{'total': 90, 'max_prompts_per_call': $n}"
    calls=$($GEN --config "$WORK/chunk_$n.yaml" --dry_run 2>&1 | grep -oE "^calls: +[0-9]+" | grep -oE "[0-9]+")
    note "max_prompts_per_call: $(printf '%-3s' "$n") ->  $calls API calls for the same 90 prompts"
done
note "Smaller calls resist max_tokens truncation; larger calls are cheaper."

# ===========================================================================
bold "5. aux_documents  --  does domain vocabulary actually reach the prompt?"
rule
for pair in "with:['aux/service_overview.md']" "without:[]"; do
    name="${pair%%:*}"; docs="${pair#*:}"
    variant "aux_$name" "{'total': 3, 'policy_ids': ['A1'], 'aux_documents': $docs}"
    $GEN --config "$WORK/aux_$name.yaml" --dry_run >/dev/null 2>&1
    f=$(ls -t "$WORK/out_aux_$name"/*/dry_run_prompts.jsonl 2>/dev/null | head -1)
    hits=$(grep -o "Safety Proxy\|PRJ-2026-001\|Workflow Builder" "$f" 2>/dev/null | wc -l)
    note "aux_documents: $(printf '%-34s' "$docs") -> $hits domain-vocabulary hits in the rendered prompt"
done
note "Proof the auxiliary document is injected, not decorative."

# ===========================================================================
bold "6. language and domain  --  they reach the system message"
rule
variant "lang_en" "{'total': 3, 'policy_ids': ['A1'], 'language': 'English', 'domain': 'Acme Bank support'}"
$GEN --config "$WORK/lang_en.yaml" --dry_run >/dev/null 2>&1
f=$(ls -t "$WORK/out_lang_en"/*/dry_run_prompts.jsonl 2>/dev/null | head -1)
python3 - "$f" <<'PY' | sed 's/^/    /'
import json, sys
sysmsg = json.loads(open(sys.argv[1], encoding="utf-8").readline())["system"]
for line in sysmsg.splitlines():
    if "Write in" in line or "Acme Bank" in line:
        print(line.strip())
PY

# ===========================================================================
bold "7. policy_file  --  the only thing carrying domain knowledge"
rule
variant "dom_rap"  "{'total': 6, 'policy_file': 'policies/example_platform.jsonl', 'policy_ids': []}"
note "policy_file: example_platform.jsonl"
plan "$WORK/dom_rap.yaml"
variant "dom_bank" "{'total': 6, 'policy_file': 'policies/demo_bank.jsonl', 'policy_ids': [], 'aux_documents': [], 'language': 'English', 'domain': 'Taiwan bank online customer service'}"
note "policy_file: demo_bank.jsonl   (different domain, same code)"
plan "$WORK/dom_bank.yaml"

# ===========================================================================
bold "8. Bad config is rejected loudly, never silently ignored"
rule
variant "e_typo"  "{'severity_ratios': {'low': 1}}";                            note "typo: 'severity_ratios' (should be severity_ratio)"; fail "$WORK/e_typo.yaml"
variant "e_zero"  "{'severity_ratio': {'low': 0, 'medium': 0, 'high': 0}}";     note "severity_ratio all zero";                            fail "$WORK/e_zero.yaml"
variant "e_neg"   "{'severity_ratio': {'low': -1, 'high': 2}}";                 note "negative weight";                                    fail "$WORK/e_neg.yaml"
variant "e_total" "{'total': 0}";                                               note "total: 0";                                           fail "$WORK/e_total.yaml"
variant "e_temp"  "{'temperature': 5}";                                         note "temperature: 5  (valid range 0-2)";                  fail "$WORK/e_temp.yaml"
variant "e_pol"   "{'policy_ids': ['Z9']}";                                     note "policy_ids: ['Z9']  (not in the file)";              fail "$WORK/e_pol.yaml"
variant "e_file"  "{'policy_file': 'policies/nope.jsonl'}";                     note "policy_file that does not exist";                    fail "$WORK/e_file.yaml"
printf '%%PDF-1.4 fake' > "$WORK/aux/manual.pdf"
variant "e_pdf"   "{'aux_documents': ['aux/manual.pdf']}";                      note "aux document in an unsupported format";              fail "$WORK/e_pdf.yaml"

bold "Done."
rule
note "Everything above ran with --dry_run: no API calls, no key, no cost."
note "Drop --dry_run and set NCHC_API_KEY to generate for real."
