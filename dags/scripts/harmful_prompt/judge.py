"""Judge a generated dataset against its policies, as a separate offline stage.

Generation and judging are deliberately different runs over different models.
Keeping the judge offline means a dataset can be re-judged with a better judge,
a stricter policy, or a different acceptance rule without paying to regenerate
it -- and it means the judge's verdict is formed without any of the generator's
claims in front of it.

Nothing is ever dropped. Every source record leaves this stage in exactly one of
three files: accepted, rejected, or review. Failures and unreadable verdicts go
to review, because a record the judge could not assess is a record a human still
has to look at -- silently discarding it would shrink the dataset without
anybody noticing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .aux_docs import build_context_block, load_documents
from .client import ChatClient, Completion
from .config import ConfigError, RunConfig, load_config
from .judge_prompt_builder import SYSTEM_MESSAGE, build_judge_messages, compile_judge_policy
from .judge_schema import JudgeParseError, JudgeResult, audit_judgment, parse_judge_response
from .policy import Policy, load_policies, select_policies
from .provenance import text_sha256
from .schema import validate_record

logger = logging.getLogger("harmful_prompt.judge")


class JudgeInputError(ValueError):
    """The source dataset cannot be judged against the configured policies."""


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _load_source(path: str | Path) -> list[dict[str, Any]]:
    source_path = Path(path)
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise JudgeInputError(f"cannot read source dataset {source_path}: {error}") from error

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise JudgeInputError(f"{source_path}:{line_no} is not valid JSON: {error}") from error
        problems = validate_record(record)
        if problems:
            raise JudgeInputError(f"{source_path}:{line_no} {'; '.join(problems)}")
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise JudgeInputError(f"{source_path}:{line_no} record has no id")
        if record_id in seen:
            raise JudgeInputError(f"{source_path}:{line_no} duplicate record id {record_id!r}")
        seen.add(record_id)
        records.append(record)

    if not records:
        raise JudgeInputError(f"{source_path} contains no records")
    return records


def _judge_api_key(settings: RunConfig) -> str:
    """The judge's key, from its own variable when set and the run's otherwise."""
    variable = settings.judge.api_key_env or settings.api_key_env
    key = os.environ.get(variable, "").strip()
    if not key:
        raise ConfigError(
            f"environment variable {variable} is not set. Export it, or point "
            "judge.api_key_env at the variable that holds your judge key."
        )
    return key


def _policy_index(records: list[dict[str, Any]], policies: list[Policy]) -> dict[str, Policy]:
    by_id = {policy.policy_id: policy for policy in policies}
    claimed = {record["violated_policy"] for record in records}
    missing = sorted(claimed - by_id.keys())
    if missing:
        raise JudgeInputError(
            "source records claim policies that are not in the configured policy file: "
            + ", ".join(missing)
        )
    return by_id


async def _judge_all(
    client: ChatClient,
    records: list[dict[str, Any]],
    by_id: dict[str, Policy],
    aux_context: str,
    settings: RunConfig,
) -> list[tuple[dict[str, Any], str, Completion]]:
    extra_body = (
        {"reasoning_effort": settings.judge.reasoning_effort}
        if settings.judge.reasoning_effort
        else None
    )

    async def one(record: dict[str, Any]):
        policy = by_id[record["violated_policy"]]
        system, user = build_judge_messages(policy, record["prompt"], aux_context)
        completion = await client.complete(
            system, user, model=settings.judge.model, extra_body=extra_body
        )
        return record, user, completion

    return await asyncio.gather(*(one(record) for record in records))


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    """Write atomically, so a crash never leaves a half-written artifact in place."""
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    return text_sha256(payload)


def run(
    config: str,
    input: str,
    model: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Judge a generated dataset and partition it into accepted/rejected/review.

    Args:
        config: path to the YAML run config.
        input: path to the generated JSONL dataset to judge.
        model: override the configured judge model.
        output_dir: override the configured output directory.
    """
    _configure_logging()
    settings = load_config(config, {"output_dir": output_dir})
    judge_settings = settings.judge
    judge_model = (model or judge_settings.model).strip()

    records = _load_source(input)
    policies = select_policies(load_policies(settings.policy_file), settings.policy_ids)
    by_id = _policy_index(records, policies)

    aux_context = ""
    if judge_settings.include_aux_context:
        aux_context = build_context_block(
            load_documents(settings.aux_documents), char_budget=settings.aux_char_budget
        )

    run_id = settings.run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(settings.output_dir) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    client = ChatClient(
        model=judge_model,
        api_key=_judge_api_key(settings),
        base_url=judge_settings.base_url or settings.base_url,
        concurrency=judge_settings.concurrency,
        max_retries=judge_settings.max_retries,
        timeout=judge_settings.timeout,
        temperature=judge_settings.temperature,
        max_tokens=judge_settings.max_tokens,
    )

    async def drive():
        try:
            return await _judge_all(client, records, by_id, aux_context, settings)
        finally:
            await client.close()

    results = asyncio.run(drive())

    judged: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    failures = 0
    by_verdict: dict[str, int] = {}
    by_severity: dict[str, int] = {}

    for record, user_message, completion in results:
        policy = by_id[record["violated_policy"]]
        judge_meta: dict[str, Any] = {
            "judge_model": judge_model,
            "judge_policy_sha256": policy.sha256,
            "judge_policy_version": policy.effective_version,
            "judge_compiled_policy_sha256": text_sha256(compile_judge_policy(policy)),
            "judge_user_sha256": text_sha256(user_message),
        }

        result: JudgeResult | None = None
        if not completion.ok:
            failures += 1
            judge_meta["judge_error"] = f"api: {completion.error}"
        else:
            try:
                result = parse_judge_response(completion.content, policy.policy_id)
            except JudgeParseError as error:
                judge_meta["judge_error"] = f"parse: {error}"

        if result is None:
            judge_meta.update({
                "verdict": None,
                "policy_match": False,
                "severity_match": False,
                "accepted": False,
            })
            destination = review
        else:
            audit = audit_judgment(result, record, judge_settings)
            judge_meta.update(result.to_dict())
            judge_meta.update(audit)
            by_verdict[result.verdict] = by_verdict.get(result.verdict, 0) + 1
            if result.severity:
                by_severity[result.severity] = by_severity.get(result.severity, 0) + 1
            if audit["accepted"]:
                destination = accepted
            elif result.verdict == "non_violation":
                destination = rejected
            else:
                destination = review

        judged_record = {**record, "judge": judge_meta}
        judged.append(judged_record)
        destination.append(judged_record)

    source_path = Path(input)
    outputs = {
        "judged_prompts.jsonl": judged,
        "accepted_prompts.jsonl": accepted,
        "judge_rejected.jsonl": rejected,
        "judge_review.jsonl": review,
    }
    output_sha256 = {
        name: _write_jsonl(out_dir / name, rows) for name, rows in outputs.items()
    }

    manifest = {
        "run_id": run_id,
        "judged_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source_path),
        "source_sha256": text_sha256(source_path.read_text(encoding="utf-8")),
        "source_records": len(records),
        "judge_model": judge_model,
        "judge_settings": settings.manifest_safe()["judge"],
        "judge_system_sha256": text_sha256(SYSTEM_MESSAGE),
        "policies": {
            policy.policy_id: {
                "version": policy.effective_version,
                "sha256": policy.sha256,
                "compiled_sha256": text_sha256(compile_judge_policy(policy)),
            }
            for policy in policies
        },
        "accepted": len(accepted),
        "rejected": len(rejected),
        "review": len(review),
        "failures": failures,
        "by_verdict": by_verdict,
        "by_severity": by_severity,
        "output_sha256": output_sha256,
    }
    (out_dir / "judge_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("judged %d record(s): %d accepted, %d rejected, %d review (%d failure(s)) -> %s",
                len(records), len(accepted), len(rejected), len(review), failures, out_dir)
    return manifest


def main() -> None:
    import fire

    fire.Fire({"run": run})


if __name__ == "__main__":
    main()
