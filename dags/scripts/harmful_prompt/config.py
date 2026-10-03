"""Configuration loading, validation, and path resolution for harmful-prompt runs."""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_BASE_URL = "https://your-openai-compatible-endpoint/v1"
DEFAULT_MODEL = "Mistral-Large-3-675B-Instruct-2512"


class ConfigError(ValueError):
    """The run configuration is missing something or contradicts itself."""


@dataclass(frozen=True)
class GeneratorSpec:
    model: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ConfigError("generator model must be a non-empty string")
        if not isinstance(self.weight, (int, float)) or isinstance(self.weight, bool):
            raise ConfigError("generator weight must be a finite positive number")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ConfigError("generator weight must be a finite positive number")


@dataclass(frozen=True)
class JudgeSettings:
    enabled: bool = False
    model: str = "gpt-oss-safeguard-120b"
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    concurrency: int = 2
    max_retries: int = 3
    timeout: float = 300.0
    reasoning_effort: str | None = "high"
    # Off by default: measured against 50 human labels, judge-vs-human severity
    # agreement was kappa 0.083 -- chance. Gating acceptance on it cost 2.5x the
    # yield (18% -> 46%) for 4 points of purity (93% -> 89%). The comparison is
    # still recorded on every record; it just no longer decides acceptance.
    require_severity_match: bool = False
    include_aux_context: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ConfigError("judge model must be a non-empty string")
        if not isinstance(self.temperature, (int, float)) or not 0 <= self.temperature <= 2:
            raise ConfigError("judge temperature must be between 0 and 2")
        for name in ("max_tokens", "concurrency"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"judge {name} must be a positive integer")
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool) or self.max_retries < 0:
            raise ConfigError("judge max_retries must not be negative")
        if (not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool)
                or not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ConfigError("judge timeout must be a finite positive number")
        if self.reasoning_effort not in {None, "low", "medium", "high"}:
            raise ConfigError("judge reasoning_effort must be one of None, low, medium, high")


@dataclass(frozen=True)
class SemanticDedupSettings:
    enabled: bool = False
    provider: str = "sentence_transformers"
    model: str = "BAAI/bge-m3"
    threshold: float = 0.90
    batch_size: int = 16

    def __post_init__(self) -> None:
        if self.provider != "sentence_transformers":
            raise ConfigError("semantic_dedup provider must be sentence_transformers")
        if not isinstance(self.threshold, (int, float)) or not 0 < self.threshold <= 1:
            raise ConfigError("semantic_dedup threshold must be in (0, 1]")
        if not isinstance(self.batch_size, int) or isinstance(self.batch_size, bool) or self.batch_size <= 0:
            raise ConfigError("semantic_dedup batch_size must be a positive integer")


@dataclass
class RunConfig:
    policy_file: str
    domain: str
    total: int = 30
    policy_ids: list[str] = field(default_factory=list)
    language: str = "Traditional Chinese (Taiwan)"
    severity_ratio: dict[str, float] = field(default_factory=dict)
    aux_documents: list[str] = field(default_factory=list)
    aux_char_budget: int = 6000
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = "NCHC_API_KEY"
    temperature: float = 1.0
    max_tokens: int = 4096
    max_prompts_per_call: int = 5
    concurrency: int = 4
    max_retries: int = 3
    timeout: float = 300.0
    generators: list[GeneratorSpec] = field(default_factory=list)
    judge: JudgeSettings = field(default_factory=JudgeSettings)
    semantic_dedup: SemanticDedupSettings = field(default_factory=SemanticDedupSettings)
    output_dir: str = "output"
    run_id: str | None = None

    def __post_init__(self) -> None:
        if not str(self.policy_file).strip():
            raise ConfigError("policy_file is required")
        if not str(self.domain).strip():
            raise ConfigError("domain is required (e.g. 'TAIWAN AI RAP 客服')")
        for name in ("total", "max_tokens", "concurrency", "aux_char_budget", "max_prompts_per_call"):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer, got {value!r}")
        if self.max_retries < 0:
            raise ConfigError("max_retries must not be negative")
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise ConfigError(f"temperature must be between 0 and 2, got {self.temperature}")
        if float(self.timeout) <= 0:
            raise ConfigError("timeout must be positive")
        if isinstance(self.policy_ids, str):
            self.policy_ids = [self.policy_ids]
        names = [spec.model for spec in self.generators]
        if len(names) != len(set(names)):
            raise ConfigError("generator model names must be unique")

    def effective_generators(self, force_model: str | None = None) -> list[GeneratorSpec]:
        if force_model:
            return [GeneratorSpec(force_model.strip(), 1.0)]
        if not self.generators:
            return [GeneratorSpec(self.model, 1.0)]
        total = sum(spec.weight for spec in self.generators)
        return [GeneratorSpec(spec.model, spec.weight / total) for spec in self.generators]

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ConfigError(f"environment variable {self.api_key_env} is not set. Export it, or point api_key_env at the variable that holds your key.")
        return key

    def manifest_safe(self) -> dict[str, Any]:
        """This config as plain data, with every credential variable name removed.

        Nested stage settings inherit `api_key_env`, so stripping only the
        top-level key would still publish which variable holds the key.
        """
        def scrub(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items() if k != "api_key_env"}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        return scrub(asdict(self))

    def resolve_paths(self, base: Path) -> None:
        def resolve(value: str) -> str:
            candidate = Path(value)
            resolved = candidate if candidate.is_absolute() else base / candidate
            return os.path.normpath(str(resolved))
        self.policy_file = resolve(self.policy_file)
        self.aux_documents = [resolve(item) for item in self.aux_documents]
        self.output_dir = resolve(self.output_dir)


def _nested(raw: dict[str, Any], key: str, cls: type[Any]) -> Any:
    value = raw.pop(key, None)
    if value is None:
        return cls()
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    unknown = sorted(set(value) - {item.name for item in fields(cls)})
    if unknown:
        raise ConfigError(f"unknown {key} key(s): {unknown}")
    try:
        return cls(**value)
    except TypeError as error:
        raise ConfigError(f"invalid {key}: {error}") from None


def _generators(raw: dict[str, Any]) -> list[GeneratorSpec]:
    value = raw.pop("generators", None)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("generators must be a list")
    specs: list[GeneratorSpec] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("each generator must be a mapping")
        unknown = sorted(set(item) - {"model", "weight"})
        if unknown:
            raise ConfigError(f"unknown generator key(s): {unknown}")
        try:
            specs.append(GeneratorSpec(**item))
        except TypeError as error:
            raise ConfigError(f"invalid generator: {error}") from None
    return specs


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> RunConfig:
    source = Path(path)
    if not source.exists():
        raise ConfigError(f"config file not found: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"{source} is not valid YAML: {error}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{source} must contain a YAML mapping at the top level")
    if overrides:
        raw.update({key: value for key, value in overrides.items() if value is not None})
    raw = dict(raw)
    generators = _generators(raw)
    judge = _nested(raw, "judge", JudgeSettings)
    semantic_dedup = _nested(raw, "semantic_dedup", SemanticDedupSettings)
    known = {item.name for item in fields(RunConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"unknown config key(s): {unknown}. Known keys: {sorted(known)}")
    try:
        config = RunConfig(**raw, generators=generators, judge=judge, semantic_dedup=semantic_dedup)
    except TypeError as error:
        message = str(error)
        if "policy_file" in message:
            raise ConfigError("policy_file is required") from None
        if "domain" in message:
            raise ConfigError("domain is required (e.g. 'TAIWAN AI RAP 客服')") from None
        raise ConfigError(f"invalid config: {message}") from None
    config.judge = replace(config.judge, base_url=config.judge.base_url or config.base_url, api_key_env=config.judge.api_key_env or config.api_key_env)
    config.resolve_paths(source.parent.resolve())
    return config
