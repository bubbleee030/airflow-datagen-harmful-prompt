"""Near-duplicate detection over embeddings, with no model download in tests.

Deduplication runs as its own stage so a threshold can be re-tuned without
regenerating or re-judging anything. Every choice it makes is deterministic:
given the same vectors it must pick the same representative every time, or two
runs of the same dataset disagree about what to keep.

Records claiming different policies are never merged. A prompt that violates two
different rules is two data points, so cross-policy similarity is reported for a
human to audit rather than acted on.
"""
from __future__ import annotations

import json

import pytest
import yaml

from harmful_prompt import semantic_dedup
from harmful_prompt.semantic_dedup import choose_representative, duplicate_clusters


def records():
    return [
        {"id": "b", "prompt": "short text", "violated_policy": "A1"},
        {"id": "a", "prompt": "a much longer equivalent text", "violated_policy": "A1"},
        {"id": "c", "prompt": "different policy text", "violated_policy": "A2"},
    ]


def read_jsonl(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


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


def test_representative_falls_back_to_id_on_equal_length():
    tied = [
        {"id": "z", "prompt": "same size"},
        {"id": "y", "prompt": "same size"},
    ]
    assert choose_representative(tied)["id"] == "y"


def test_below_threshold_pairs_are_not_clustered():
    vectors = [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]
    clusters, _ = duplicate_clusters(records(), vectors, threshold=0.90)
    assert clusters == []


@pytest.mark.parametrize("vectors", [
    [[1.0, 0.0], [0.95, 0.05]],
    [[1.0, 0.0], [0.95, 0.05], [1.0]],
    [[1.0, 0.0], [0.95, 0.05], [0.0, 0.0]],
    [[1.0, 0.0], [0.95, 0.05], [float("nan"), 0.0]],
])
def test_unusable_vectors_are_rejected(vectors):
    with pytest.raises(semantic_dedup.DedupError):
        duplicate_clusters(records(), vectors, threshold=0.90)


class FakeEmbedder:
    """Returns fixed vectors in source order; never loads a model."""

    provider = "fake"
    model = "fake-vectors"

    def __init__(self):
        self.seen = None

    def encode(self, texts):
        self.seen = list(texts)
        return [[1.0, 0.0], [0.95, 0.05], [1.0, 0.0]]


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "p.jsonl").write_text(
        json.dumps({"policy_id": "A1", "policy": "Reject out-of-scope requests."}) + "\n"
        + json.dumps({"policy_id": "A2", "policy": "Reject unrelated requests."}) + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "run.yaml"
    config.write_text(yaml.safe_dump({
        "policy_file": "policies/p.jsonl",
        "domain": "abstract service desk",
        "total": 3,
        "output_dir": "out",
        "run_id": "deduprun",
        "semantic_dedup": {"enabled": True, "threshold": 0.90},
    }, allow_unicode=True), encoding="utf-8")
    source = tmp_path / "accepted.jsonl"
    source.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records()) + "\n",
        encoding="utf-8",
    )
    return tmp_path, config, source


def test_runner_writes_deduped_artifacts(workspace):
    tmp_path, config, source = workspace
    embedder = FakeEmbedder()
    manifest = semantic_dedup.run(config=str(config), input=str(source), embedder=embedder)
    out = tmp_path / "out" / "deduprun"

    assert [row["id"] for row in read_jsonl(out / "deduped_prompts.jsonl")] == ["b", "c"]
    duplicates = read_jsonl(out / "semantic_duplicates.jsonl")
    assert duplicates == [
        {"representative_id": "b", "duplicate_ids": ["a"], "policy_id": "A1"}
    ]
    assert manifest["source_records"] == 3
    assert manifest["retained"] == 2
    assert manifest["removed"] == 1
    assert manifest["clusters"] == 1
    assert len(manifest["source_sha256"]) == 64
    assert manifest["provider"] == "fake"
    assert manifest["model"] == "fake-vectors"
    assert manifest["threshold"] == 0.90
    assert manifest["cross_policy_candidates"] == 2


def test_runner_embeds_prompts_in_source_order(workspace):
    tmp_path, config, source = workspace
    embedder = FakeEmbedder()
    semantic_dedup.run(config=str(config), input=str(source), embedder=embedder)
    assert embedder.seen == [
        "short text", "a much longer equivalent text", "different policy text",
    ]


def test_cross_policy_similarity_is_reported_not_merged(workspace):
    """A prompt breaking two rules is two data points, not one duplicate pair."""
    tmp_path, config, source = workspace
    manifest = semantic_dedup.run(config=str(config), input=str(source),
                                  embedder=FakeEmbedder())
    out = tmp_path / "out" / "deduprun"
    audit = manifest["cross_policy_audit"]
    assert [(entry["a_id"], entry["b_id"]) for entry in audit] == [("b", "c"), ("a", "c")]
    assert all(entry["policy_ids"] == ["A1", "A2"] for entry in audit)
    assert "c" in [row["id"] for row in read_jsonl(out / "deduped_prompts.jsonl")]


def test_source_file_is_never_overwritten(workspace):
    tmp_path, config, source = workspace
    before = source.read_text(encoding="utf-8")
    semantic_dedup.run(config=str(config), input=str(source), embedder=FakeEmbedder())
    assert source.read_text(encoding="utf-8") == before


def test_optional_dependency_is_not_imported_when_an_embedder_is_injected(workspace,
                                                                         monkeypatch):
    """The base install has no sentence-transformers; injecting must not need it."""
    import builtins

    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise AssertionError("optional dependency imported despite injected embedder")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    tmp_path, config, source = workspace
    semantic_dedup.run(config=str(config), input=str(source), embedder=FakeEmbedder())


def test_missing_optional_dependency_names_the_requirements_file(monkeypatch):
    """The base install lacks sentence-transformers; the error must say how to fix it."""
    import builtins

    from harmful_prompt.embedding import SentenceTransformerEmbedder

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise ImportError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(RuntimeError, match="requirements-semantic.txt"):
        SentenceTransformerEmbedder(model="BAAI/bge-m3")
