"""Entry point: policy in, harmful-prompt dataset out.

Local usage:

    export NCHC_API_KEY=...
    python3 -m harmful_prompt.generate run --config configs/example.yaml
    python3 -m harmful_prompt.generate run --config configs/example.yaml --dry_run

``--dry_run`` builds the plan and renders the exact prompts that would be sent,
without calling the API. Use it to check a new policy file cheaply.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .aux_docs import load_documents
from .client import ChatClient, Completion
from .config import ConfigError, RunConfig, load_config
from .parser import parse_response
from .planner import WorkItem, assign_generators, build_plan, chunk_plan, plan_total, summarise_plan
from .policy import load_policies, select_policies
from .provenance import text_sha256
from .prompt_builder import build_system_message, build_user_message
from .schema import HarmfulPrompt, validate_record

logger = logging.getLogger("harmful_prompt")


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def file_digest(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


async def _run_items(
    client: ChatClient, system: str, items: list[WorkItem], config: RunConfig, aux_documents: list
) -> list[tuple[WorkItem, str, Completion]]:
    """Issue one call per work item, concurrently."""
    async def one(item: WorkItem) -> tuple[WorkItem, str, Completion]:
        user = build_user_message(
            policy=item.policy,
            severity=item.severity,
            count=item.count,
            domain=config.domain,
            aux_documents=aux_documents,
            aux_char_budget=config.aux_char_budget,
        )
        completion = await client.complete(system, user, model=item.model)
        status = "ok" if completion.ok else f"FAILED ({completion.error})"
        logger.info("%s x%d -> %s", item.key, item.count, status)
        return item, user, completion

    return await asyncio.gather(*(one(item) for item in items))


def run(
    config: str,
    dry_run: bool = False,
    total: int | None = None,
    model: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Generate a harmful-prompt dataset from a policy file.

    Args:
        config: path to the YAML run config.
        dry_run: build the plan and render prompts without calling the API.
        total: override the configured number of prompts.
        model: override the configured model.
        output_dir: override the configured output directory.
    """
    _configure_logging()
    overrides = {"total": total, "model": model, "output_dir": output_dir}
    settings = load_config(config, overrides)

    policies = select_policies(load_policies(settings.policy_file), settings.policy_ids)
    aux_documents = load_documents(settings.aux_documents)
    plan = build_plan(policies, settings.total, settings.severity_ratio)
    allocated = assign_generators(plan, settings.effective_generators(model))
    calls = chunk_plan(allocated, settings.max_prompts_per_call)

    logger.info("policies: %s", ", ".join(p.policy_id for p in policies))
    if aux_documents:
        logger.info("auxiliary documents: %s", ", ".join(d.name for d in aux_documents))
    logger.info("plan:\n%s", summarise_plan(plan))

    run_id = settings.run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(settings.output_dir) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    system_message = build_system_message(settings.domain, settings.language)

    if dry_run:
        preview_path = out_dir / "dry_run_prompts.jsonl"
        with preview_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"system": system_message}, ensure_ascii=False) + "\n")
            for item in calls:
                handle.write(json.dumps({
                    "policy_id": item.policy.policy_id,
                    "severity": item.severity,
                    "count": item.count,
                    "model": item.model or settings.model,
                    "user": build_user_message(
                        item.policy, item.severity, item.count, settings.domain,
                        aux_documents, settings.aux_char_budget,
                    ),
                }, ensure_ascii=False) + "\n")
        logger.info("dry run: %d call(s) would be made; prompts written to %s",
                    len(calls), preview_path)
        return {"dry_run": True, "calls": len(calls), "planned_prompts": plan_total(plan),
                "output_dir": str(out_dir)}

    client = ChatClient(
        model=settings.model,
        api_key=settings.api_key(),
        base_url=settings.base_url,
        concurrency=settings.concurrency,
        max_retries=settings.max_retries,
        timeout=settings.timeout,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
    )

    async def drive():
        try:
            return await _run_items(client, system_message, calls, settings, aux_documents)
        finally:
            await client.close()

    results = asyncio.run(drive())

    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    strategies: dict[str, int] = {}
    seen_ids: set[str] = set()

    system_sha256 = text_sha256(system_message)

    for item, user_message, completion in results:
        if not completion.ok:
            failures.append({"policy_id": item.policy.policy_id, "severity": item.severity,
                             "requested": item.count, "stage": "api", "error": completion.error})
            continue

        parsed = parse_response(completion.content)
        strategies[parsed.strategy] = strategies.get(parsed.strategy, 0) + 1
        if not parsed.ok:
            failures.append({"policy_id": item.policy.policy_id, "severity": item.severity,
                             "requested": item.count, "stage": "parse", "error": parsed.error,
                             "finish_reason": completion.finish_reason,
                             "completion_tokens": completion.completion_tokens,
                             "response_chars": len(completion.content)})
            continue

        kept = 0
        for entry in parsed.prompts:
            if kept >= item.count:
                break  # models often return more than asked; the surplus would
                       # quietly break the configured policy/severity/model mix
            record = HarmfulPrompt(
                prompt=entry["prompt"],
                violated_policy=item.policy.policy_id,
                severity=item.severity,
                domain=settings.domain,
                generation_rationale=entry.get("generation_rationale") or "(not provided by model)",
                meta={
                    "generator_model": item.model or settings.model,
                    "model": item.model or settings.model,
                    "parse_strategy": parsed.strategy,
                    "policy_statement": item.policy.statement,
                    "policy_sha256": item.policy.sha256,
                    "policy_version": item.policy.effective_version,
                    "generation_system_sha256": system_sha256,
                    "generation_user_sha256": text_sha256(user_message),
                    "run_id": run_id,
                },
            ).to_record()
            problems = validate_record(record)
            if problems:
                failures.append({"policy_id": item.policy.policy_id, "severity": item.severity,
                                 "stage": "validate", "error": "; ".join(problems)})
                continue
            if record["id"] in seen_ids:
                continue  # exact duplicate within this run
            seen_ids.add(record["id"])
            records.append(record)
            kept += 1

    prompts_path = out_dir / "harmful_prompts.jsonl"
    with prompts_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    if failures:
        with (out_dir / "failures.jsonl").open("w", encoding="utf-8") as handle:
            for failure in failures:
                handle.write(json.dumps(failure, ensure_ascii=False) + "\n")

    by_severity: dict[str, int] = {}
    by_policy: dict[str, int] = {}
    by_generator: dict[str, int] = {}
    for record in records:
        by_severity[record["severity"]] = by_severity.get(record["severity"], 0) + 1
        by_policy[record["violated_policy"]] = by_policy.get(record["violated_policy"], 0) + 1
        generator = record["meta"]["generator_model"]
        by_generator[generator] = by_generator.get(generator, 0) + 1

    manifest = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": settings.manifest_safe(),
        "policy_file_sha256": file_digest(settings.policy_file),
        "policies": {
            policy.policy_id: {
                "version": policy.effective_version,
                "sha256": policy.sha256,
            }
            for policy in policies
        },
        "aux_document_sha256": {d: file_digest(d) for d in settings.aux_documents},
        "requested": plan_total(plan),
        "produced": len(records),
        "api_calls": len(calls),
        "failures": len(failures),
        "parse_strategies": strategies,
        "by_severity": by_severity,
        "by_policy": by_policy,
        "by_generator": by_generator,
    }
    (out_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("produced %d/%d prompts (%d failure(s)) -> %s",
                len(records), plan_total(plan), len(failures), prompts_path)
    return manifest


def main() -> None:
    import fire

    fire.Fire({"run": run})


if __name__ == "__main__":
    main()
