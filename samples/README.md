# Samples

Two representative runs, kept as model-generated examples (not human gold).

- `taiwan_ai_rap_*`: generated from the production TAIWAN AI RAP policy file, which is not part of
  this public release. Each record's `meta.policy_statement` (the policy text) and `generation_rationale` (which
  quotes the policy) have been removed,
  and email addresses that appeared inside generated prompts are replaced with `[email]`. The run
  manifest still names the original policy and auxiliary files and their SHA-256 hashes, as the
  record of what produced the run.
- `demo_bank_*`: generated from `configs/policies/demo_bank.jsonl`, which is included.

`configs/policies/example_platform.jsonl` is a generic stand-in with the same schema as the
production policy; use it, or your own policy file, to reproduce a run of this shape.
