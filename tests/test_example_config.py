"""configs/example.yaml is the template people copy — it must stay complete.

`max_prompts_per_call` was silently absent from it for a while. The pipeline still
worked, because the dataclass supplies a default, which is exactly why nobody
noticed: the most important knob for the truncation failure mode was invisible to
anyone reading the config. These tests make that class of omission fail loudly.
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from harmful_prompt.config import RunConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "configs"
EXAMPLE = CONFIG_DIR / "example.yaml"


def test_example_config_documents_every_key():
    """Every RunConfig field must appear in the example, so none is invisible."""
    present = set(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {})
    known = {f.name for f in fields(RunConfig)}
    missing = sorted(known - present)
    assert not missing, (
        f"configs/example.yaml is missing {missing}. A key that only exists as a "
        f"dataclass default is invisible to anyone reading the config."
    )


def test_example_config_has_no_unknown_keys():
    """Guards against a stale key left behind after a rename."""
    present = set(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {})
    known = {f.name for f in fields(RunConfig)}
    assert not sorted(present - known)


@pytest.mark.parametrize("config_path", sorted(CONFIG_DIR.glob("*.yaml")))
def test_every_shipped_config_loads(config_path):
    """Each config in configs/ must parse and validate."""
    config = load_config(config_path)
    assert config.total > 0
    assert Path(config.policy_file).exists(), f"policy_file missing: {config.policy_file}"


@pytest.mark.parametrize("config_path", sorted(CONFIG_DIR.glob("*.yaml")))
def test_every_shipped_config_points_at_real_aux_documents(config_path):
    for document in load_config(config_path).aux_documents:
        assert Path(document).exists(), f"aux document missing: {document}"


def test_documentation_lists_every_key():
    """docs/reports/datagen-doc.md is the user-facing parameter reference."""
    doc = (REPO_ROOT / "docs" / "reports" / "datagen-doc.md").read_text(encoding="utf-8")
    undocumented = [f.name for f in fields(RunConfig) if f"`{f.name}`" not in doc]
    assert not undocumented, f"undocumented config keys: {undocumented}"
