"""No credential may be hardcoded anywhere in this repository.

Generation needs an NCHC API key and publication needs a GitLab token. Neither
may ever be committed: they come from the environment, and the config stores only
the *name* of the variable that holds them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", "output", "output_smoke",
    "dataset", "demo_out", ".venv", "venv", "node_modules",
}

CREDENTIAL_PATTERNS = {
    "NCHC api key": re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    "GitLab token": re.compile(r"glpat-[A-Za-z0-9._-]{16,}"),
    "Argilla api key": re.compile(r"s3_[A-Za-z0-9_-]{20,}"),
    "HuggingFace token": re.compile(r"hf_[A-Za-z0-9]{20,}"),
}

SCANNED_SUFFIXES = {".py", ".sh", ".md", ".yaml", ".yml", ".json", ".jsonl", ".txt", ".ipynb"}


def _scanned_files() -> list[Path]:
    return [
        path for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in SCANNED_SUFFIXES
        and not EXCLUDED_DIRS & set(path.relative_to(REPO_ROOT).parts)
    ]


@pytest.mark.parametrize("label,pattern", sorted(CREDENTIAL_PATTERNS.items()))
def test_no_hardcoded_credentials(label, pattern):
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _scanned_files()
        if pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, f"{label} found in: {offenders}. Use an environment variable."


def test_env_example_declares_the_key_without_a_value():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "NCHC_API_KEY=" in text
    assert not re.search(r"NCHC_API_KEY=\S", text), ".env.example must not carry a real key"


def test_gitignore_excludes_dotenv():
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignored
