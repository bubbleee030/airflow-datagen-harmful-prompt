"""Config loading, validation, and path resolution."""
from __future__ import annotations

import os

import pytest
import yaml

from harmful_prompt.config import ConfigError, RunConfig, load_config


def write_config(tmp_path, data, name="run.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


MINIMAL = {"policy_file": "p.jsonl", "domain": "TAIWAN AI RAP 客服"}


def test_minimal_config_loads(tmp_path):
    config = load_config(write_config(tmp_path, MINIMAL))
    assert config.domain == "TAIWAN AI RAP 客服"
    assert config.total == 30


def test_unknown_key_is_rejected(tmp_path):
    """A typo must fail loudly, not silently fall back to a default."""
    path = write_config(tmp_path, {**MINIMAL, "severity_ratios": {"low": 1}})
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(path)


def test_missing_policy_file_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="policy_file is required"):
        load_config(write_config(tmp_path, {"domain": "d"}))


def test_missing_domain_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="domain is required"):
        load_config(write_config(tmp_path, {"policy_file": "p.jsonl"}))


@pytest.mark.parametrize("field", ["total", "max_tokens", "concurrency", "aux_char_budget"])
def test_non_positive_integers_are_rejected(tmp_path, field):
    with pytest.raises(ConfigError, match=f"{field} must be a positive integer"):
        load_config(write_config(tmp_path, {**MINIMAL, field: 0}))


@pytest.mark.parametrize("value", [-0.1, 2.1])
def test_temperature_out_of_range_is_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="temperature"):
        load_config(write_config(tmp_path, {**MINIMAL, "temperature": value}))


def test_negative_retries_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="max_retries"):
        load_config(write_config(tmp_path, {**MINIMAL, "max_retries": -1}))


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("key: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_non_mapping_yaml_is_rejected(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


# --- path resolution -------------------------------------------------------

def test_relative_paths_resolve_against_the_config_not_the_cwd(tmp_path):
    """Running from any directory must load the same files."""
    nested = tmp_path / "configs"
    nested.mkdir()
    path = write_config(nested, {**MINIMAL, "policy_file": "policies/p.jsonl"})
    config = load_config(path)
    assert config.policy_file == str(nested / "policies" / "p.jsonl")


def test_parent_references_are_normalised(tmp_path):
    nested = tmp_path / "configs"
    nested.mkdir()
    config = load_config(write_config(nested, {**MINIMAL, "output_dir": "../output"}))
    assert ".." not in config.output_dir
    assert config.output_dir == str(tmp_path / "output")


def test_absolute_paths_are_left_alone(tmp_path):
    config = load_config(write_config(tmp_path, {**MINIMAL, "policy_file": "/abs/p.jsonl"}))
    assert config.policy_file == "/abs/p.jsonl"


# --- overrides -------------------------------------------------------------

def test_overrides_apply(tmp_path):
    config = load_config(write_config(tmp_path, MINIMAL), {"total": 7, "model": "other"})
    assert config.total == 7 and config.model == "other"


def test_none_overrides_are_ignored(tmp_path):
    """An unset CLI flag must not clobber the configured value."""
    config = load_config(write_config(tmp_path, {**MINIMAL, "total": 42}), {"total": None})
    assert config.total == 42


# --- api key ---------------------------------------------------------------

def test_api_key_read_from_named_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_KEY", "secret-value")
    config = load_config(write_config(tmp_path, {**MINIMAL, "api_key_env": "MY_KEY"}))
    assert config.api_key() == "secret-value"


def test_missing_api_key_names_the_variable(monkeypatch, tmp_path):
    monkeypatch.delenv("NCHC_API_KEY", raising=False)
    config = load_config(write_config(tmp_path, MINIMAL))
    with pytest.raises(ConfigError, match="NCHC_API_KEY"):
        config.api_key()


def test_config_never_stores_the_key_itself(tmp_path):
    """Only the variable *name* lives in config, so keys cannot leak via manifests."""
    config = load_config(write_config(tmp_path, MINIMAL))
    assert "api_key" not in {f for f in vars(config)}
    assert config.api_key_env == "NCHC_API_KEY"


def test_single_policy_id_string_becomes_a_list(tmp_path):
    config = load_config(write_config(tmp_path, {**MINIMAL, "policy_ids": "A1"}))
    assert config.policy_ids == ["A1"]


def test_generator_mix_and_stage_settings_load(tmp_path):
    path = write_config(tmp_path, {
        **MINIMAL,
        "generators": [
            {"model": "model-a", "weight": 7},
            {"model": "model-b", "weight": 3},
        ],
        "judge": {"enabled": True, "model": "judge-a", "reasoning_effort": "high"},
        "semantic_dedup": {"enabled": True, "threshold": 0.87},
    })
    config = load_config(path)
    assert [(g.model, g.weight) for g in config.effective_generators()] == [
        ("model-a", 0.7), ("model-b", 0.3)
    ]
    assert config.judge.model == "judge-a"
    assert config.judge.base_url == config.base_url
    assert config.judge.api_key_env == config.api_key_env
    assert config.semantic_dedup.threshold == 0.87


def test_model_override_forces_one_generator(tmp_path):
    config = load_config(write_config(tmp_path, {
        **MINIMAL,
        "generators": [{"model": "a", "weight": 1}, {"model": "b", "weight": 1}],
    }))
    assert [(g.model, g.weight) for g in config.effective_generators("forced")] == [
        ("forced", 1.0)
    ]


@pytest.mark.parametrize("generators", [
    [{"model": "a", "weight": 0}],
    [{"model": "a", "weight": 1}, {"model": "a", "weight": 2}],
    [{"model": "a", "weight": float("inf")}],
])
def test_invalid_generator_mix_is_rejected(tmp_path, generators):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, {**MINIMAL, "generators": generators}))


def test_unknown_nested_config_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown judge key"):
        load_config(write_config(tmp_path, {**MINIMAL, "judge": {"modle": "typo"}}))


@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
def test_non_finite_judge_timeout_is_rejected(tmp_path, timeout):
    with pytest.raises(ConfigError, match="judge timeout"):
        load_config(write_config(tmp_path, {**MINIMAL, "judge": {"timeout": timeout}}))
