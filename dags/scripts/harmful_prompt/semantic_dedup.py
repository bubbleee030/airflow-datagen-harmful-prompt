"""Collapse near-duplicate prompts, as a separate rerunnable offline stage.

Exact-duplicate removal already happens during generation. What survives it is
the harder case: two prompts that say the same thing in different words, or the
same thing in two languages. Catching those needs embeddings, which is why this
is its own stage -- a threshold can be re-tuned, or a better embedding model
swapped in, without regenerating or re-judging anything.

Two rules keep the output trustworthy. Selection is fully deterministic, so two
runs over the same vectors keep the same records. And records claiming different
policies are never merged: a prompt that violates two rules is two data points,
so cross-policy similarity is reported for a human to audit rather than acted on.
"""
from __future__ import annotations

import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .config import load_config
from .provenance import text_sha256

logger = logging.getLogger("harmful_prompt.semantic_dedup")


class DedupError(ValueError):
    """The records and vectors cannot be compared."""


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _normalised(vectors: Sequence[Sequence[float]], count: int) -> list[list[float]]:
    if len(vectors) != count:
        raise DedupError(f"expected {count} vectors, got {len(vectors)}")
    if not vectors:
        return []

    width = len(vectors[0])
    if width == 0:
        raise DedupError("vectors must not be empty")

    unit: list[list[float]] = []
    for index, vector in enumerate(vectors):
        if len(vector) != width:
            raise DedupError(
                f"vector {index} has dimension {len(vector)}, expected {width}"
            )
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector):
            raise DedupError(f"vector {index} contains a non-finite value")
        norm = math.sqrt(sum(float(v) * float(v) for v in vector))
        if norm == 0.0:
            raise DedupError(f"vector {index} has zero norm")
        unit.append([float(v) / norm for v in vector])
    return unit


class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


def duplicate_clusters(
    records: Sequence[dict[str, Any]],
    vectors: Sequence[Sequence[float]],
    threshold: float,
) -> tuple[list[list[dict[str, Any]]], list[tuple[str, str, float]]]:
    """Group same-policy near-duplicates, and report cross-policy near-matches.

    Returns the clusters of two or more records, and the cross-policy pairs that
    were above threshold but deliberately left unmerged.
    """
    unit = _normalised(vectors, len(records))
    union = _UnionFind(len(records))
    cross_policy: list[tuple[str, str, float]] = []

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            similarity = sum(a * b for a, b in zip(unit[i], unit[j]))
            if similarity < threshold:
                continue
            if records[i]["violated_policy"] == records[j]["violated_policy"]:
                union.union(i, j)
            else:
                cross_policy.append((records[i]["id"], records[j]["id"], similarity))

    grouped: dict[int, list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        grouped.setdefault(union.find(index), []).append(record)

    clusters = [
        sorted(members, key=lambda record: record["id"])
        for members in grouped.values()
        if len(members) > 1
    ]
    clusters.sort(key=lambda cluster: cluster[0]["id"])
    return clusters, cross_policy


def choose_representative(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pick one record from a duplicate cluster, deterministically.

    Prefer a higher judge score when one exists, then the shorter prompt, then
    the lexically smaller ID. Reasoner judges often expose no calibrated score,
    so the last two rules are the normal path rather than a tie-break.
    """
    def rank(record: dict[str, Any]) -> tuple[float, int, str]:
        score = (record.get("judge") or {}).get("score")
        numeric = float(score) if isinstance(score, (int, float)) else float("-inf")
        return (-numeric, len(record["prompt"].strip()), record["id"])

    return min(records, key=rank)


def _load_source(path: str | Path) -> list[dict[str, Any]]:
    source_path = Path(path)
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise DedupError(f"cannot read source dataset {source_path}: {error}") from error

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise DedupError(f"{source_path}:{line_no} is not valid JSON: {error}") from error
        for field in ("id", "prompt", "violated_policy"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                raise DedupError(f"{source_path}:{line_no} missing or empty field: {field}")
        if record["id"] in seen:
            raise DedupError(f"{source_path}:{line_no} duplicate record id {record['id']!r}")
        seen.add(record["id"])
        records.append(record)

    if not records:
        raise DedupError(f"{source_path} contains no records")
    return records


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    return text_sha256(payload)


def run(
    config: str,
    input: str,
    output_dir: str | None = None,
    embedder: Any = None,
) -> dict[str, Any]:
    """Remove near-duplicate prompts and report what was collapsed.

    Args:
        config: path to the YAML run config.
        input: path to the JSONL dataset to deduplicate.
        output_dir: override the configured output directory.
        embedder: an object with `encode(texts)`; the configured
            sentence-transformers model is loaded only when this is omitted.
    """
    _configure_logging()
    settings = load_config(config, {"output_dir": output_dir})
    dedup_settings = settings.semantic_dedup

    records = _load_source(input)

    if embedder is None:
        from .embedding import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder(
            model=dedup_settings.model, batch_size=dedup_settings.batch_size
        )

    vectors = embedder.encode([record["prompt"] for record in records])
    clusters, cross_policy = duplicate_clusters(records, vectors, dedup_settings.threshold)

    removed_ids: set[str] = set()
    duplicate_rows: list[dict[str, Any]] = []
    for cluster in clusters:
        representative = choose_representative(cluster)
        duplicates = [record for record in cluster if record["id"] != representative["id"]]
        removed_ids.update(record["id"] for record in duplicates)
        duplicate_rows.append({
            "representative_id": representative["id"],
            "duplicate_ids": [record["id"] for record in duplicates],
            "policy_id": representative["violated_policy"],
        })

    retained = [record for record in records if record["id"] not in removed_ids]

    run_id = settings.run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(settings.output_dir) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    output_sha256 = {
        "deduped_prompts.jsonl": _write_jsonl(out_dir / "deduped_prompts.jsonl", retained),
        "semantic_duplicates.jsonl": _write_jsonl(
            out_dir / "semantic_duplicates.jsonl", duplicate_rows
        ),
    }

    source_path = Path(input)
    policy_of = {record["id"]: record["violated_policy"] for record in records}
    by_policy: dict[str, int] = {}
    for record in retained:
        policy_id = record["violated_policy"]
        by_policy[policy_id] = by_policy.get(policy_id, 0) + 1

    manifest = {
        "run_id": run_id,
        "deduped_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source_path),
        "source_sha256": text_sha256(source_path.read_text(encoding="utf-8")),
        "source_records": len(records),
        "retained": len(retained),
        "removed": len(removed_ids),
        "clusters": len(clusters),
        "provider": getattr(embedder, "provider", dedup_settings.provider),
        "model": getattr(embedder, "model", dedup_settings.model),
        "threshold": dedup_settings.threshold,
        "by_policy": by_policy,
        "cross_policy_candidates": len(cross_policy),
        "cross_policy_audit": [
            {"a_id": a_id, "b_id": b_id, "similarity": similarity,
             "policy_ids": sorted({policy_of[a_id], policy_of[b_id]})}
            for a_id, b_id, similarity in cross_policy
        ],
        "output_sha256": output_sha256,
    }
    (out_dir / "dedup_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("deduplicated %d record(s): %d retained, %d removed in %d cluster(s) -> %s",
                len(records), len(retained), len(removed_ids), len(clusters), out_dir)
    return manifest


def main() -> None:
    import fire

    fire.Fire({"run": run})


if __name__ == "__main__":
    main()
