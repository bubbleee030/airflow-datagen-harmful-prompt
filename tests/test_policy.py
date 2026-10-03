"""Policy loading, severity normalisation, and selection."""
from __future__ import annotations

import json

import pytest

from harmful_prompt.policy import (
    DEFAULT_SEVERITY,
    Policy,
    PolicyError,
    load_policies,
    normalise_severity_key,
    parse_policy,
    select_policies,
)


def write_jsonl(tmp_path, rows, name="policies.jsonl"):
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


# --- severity normalisation ------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("low", "low"), ("Low", "low"), ("minor", "low"), ("Minor", "low"),
    ("Level 1", "low"), ("1", "low"), ("L1", "low"),
    ("medium", "medium"), ("moderate", "medium"), ("Moderate", "medium"),
    ("Level 2", "medium"), ("2", "medium"),
    ("high", "high"), ("severe", "high"), ("Severe", "high"),
    ("Level 3", "high"), ("3", "high"), ("critical", "high"),
])
def test_severity_aliases_normalise(raw, expected):
    assert normalise_severity_key(raw) == expected


def test_compound_severity_label_normalises():
    """The old dataset used 'Level 2 / Moderate' as a single label."""
    assert normalise_severity_key("Level 2 / Moderate") == "medium"


def test_unknown_severity_is_rejected():
    with pytest.raises(PolicyError, match="unrecognised severity"):
        normalise_severity_key("catastrophic")


# --- minimal policy --------------------------------------------------------

def test_two_field_policy_is_accepted_and_gets_default_severity():
    policy = parse_policy({"policy_id": "A", "policy": "Reject fraud."})
    assert policy.policy_id == "A"
    assert policy.severity == DEFAULT_SEVERITY
    assert policy.example_prompts == ()


def test_missing_policy_id_is_rejected():
    with pytest.raises(PolicyError, match="policy_id"):
        parse_policy({"policy": "text"})


def test_blank_policy_text_is_rejected():
    with pytest.raises(PolicyError, match="policy must be"):
        parse_policy({"policy_id": "A", "policy": "   "})


# --- severity supplied by the user ----------------------------------------

def test_partial_severity_is_filled_from_defaults():
    """A user who defines only 'high' still gets a complete three-level scale."""
    policy = parse_policy({
        "policy_id": "A", "policy": "x",
        "severity": {"high": "mass exploitation"},
    })
    assert policy.severity["high"] == "mass exploitation"
    assert policy.severity["low"] == DEFAULT_SEVERITY["low"]
    assert set(policy.severity) == {"low", "medium", "high"}


def test_legacy_minor_moderate_severe_is_accepted():
    policy = parse_policy({
        "policy_id": "A1", "policy": "x",
        "severity": {"minor": "probe", "moderate": "evade", "severe": "fraud"},
    })
    assert policy.severity == {"low": "probe", "medium": "evade", "high": "fraud"}


def test_duplicate_severity_after_aliasing_is_rejected():
    """'low' and 'minor' both mean low -- that is a contradiction, not a merge."""
    with pytest.raises(PolicyError, match="defined twice"):
        parse_policy({
            "policy_id": "A", "policy": "x",
            "severity": {"low": "a", "minor": "b"},
        })


# --- examples --------------------------------------------------------------

def test_singular_example_prompt_becomes_a_tuple():
    policy = parse_policy({"policy_id": "A", "policy": "x", "example_prompt": "先開通再補件"})
    assert policy.example_prompts == ("先開通再補件",)


def test_plural_example_prompts_list_is_accepted():
    policy = parse_policy({"policy_id": "A", "policy": "x", "example_prompts": ["a", "b"]})
    assert policy.example_prompts == ("a", "b")


def test_empty_example_prompt_is_dropped():
    policy = parse_policy({"policy_id": "A", "policy": "x", "example_prompt": "   "})
    assert policy.example_prompts == ()


# --- zh-TW preference ------------------------------------------------------

def test_statement_prefers_traditional_chinese():
    policy = parse_policy({"policy_id": "A", "policy": "English", "policy_zh_TW": "中文"})
    assert policy.statement == "中文"


def test_statement_falls_back_to_english():
    assert parse_policy({"policy_id": "A", "policy": "English"}).statement == "English"


# --- file loading ----------------------------------------------------------

def test_load_skips_blanks_and_comments(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text(
        '# a comment\n\n{"policy_id":"A","policy":"x"}\n\n# another\n'
        '{"policy_id":"B","policy":"y"}\n',
        encoding="utf-8",
    )
    assert [p.policy_id for p in load_policies(path)] == ["A", "B"]


def test_duplicate_policy_id_is_rejected(tmp_path):
    path = write_jsonl(tmp_path, [
        {"policy_id": "A", "policy": "x"},
        {"policy_id": "A", "policy": "y"},
    ])
    with pytest.raises(PolicyError, match="duplicate policy_id"):
        load_policies(path)


def test_invalid_json_reports_the_line_number(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text('{"policy_id":"A","policy":"x"}\n{not json}\n', encoding="utf-8")
    with pytest.raises(PolicyError, match="line 2"):
        load_policies(path)


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text("# only a comment\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="no policies"):
        load_policies(path)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(PolicyError, match="not found"):
        load_policies(tmp_path / "nope.jsonl")


# --- selection -------------------------------------------------------------

def test_select_preserves_file_order_not_argument_order():
    policies = [Policy(pid, "x", dict(DEFAULT_SEVERITY)) for pid in ("A", "B", "C")]
    assert [p.policy_id for p in select_policies(policies, ["C", "A"])] == ["A", "C"]


def test_select_with_no_filter_returns_everything():
    policies = [Policy(pid, "x", dict(DEFAULT_SEVERITY)) for pid in ("A", "B")]
    assert len(select_policies(policies, None)) == 2
    assert len(select_policies(policies, [])) == 2


def test_select_unknown_policy_id_is_rejected():
    policies = [Policy("A", "x", dict(DEFAULT_SEVERITY))]
    with pytest.raises(PolicyError, match="not in file"):
        select_policies(policies, ["Z"])


def test_policy_accepts_judge_fields_and_has_stable_identity():
    row = {
        "policy_id": "A",
        "policy": "Reject disallowed requests.",
        "policy_version": "v2",
        "definitions": {"restricted": "content outside the allowed scope"},
        "example_prompts": ["request outside the allowed scope"],
        "allowed_examples": ["request inside the allowed scope"],
    }
    first = parse_policy(row)
    second = parse_policy(dict(reversed(list(row.items()))))
    assert first.allowed_examples == ("request inside the allowed scope",)
    assert first.definitions == {"restricted": "content outside the allowed scope"}
    assert first.sha256 == second.sha256
    assert first.effective_version == "v2"


def test_policy_without_version_uses_digest_version():
    policy = parse_policy({"policy_id": "A", "policy": "Reject disallowed requests."})
    assert policy.effective_version == f"sha256:{policy.sha256[:12]}"


def test_invalid_definitions_are_rejected():
    with pytest.raises(PolicyError, match="definitions"):
        parse_policy({"policy_id": "A", "policy": "x", "definitions": ["not-a-map"]})


# --- per-severity examples --------------------------------------------------

def test_examples_can_be_keyed_by_severity():
    """A generator asked for `low` should see low examples, not six high ones."""
    policy = parse_policy({
        "policy_id": "A2",
        "policy": "rule",
        "severity": {"low": "a", "medium": "b", "high": "c"},
        "example_prompts": {
            "minor": ["mild one", "mild two"],
            "moderate": ["middling one"],
            "severe": ["severe one"],
        },
    })
    assert policy.examples_for("low") == ("mild one", "mild two")
    assert policy.examples_for("medium") == ("middling one",)
    assert policy.examples_for("high") == ("severe one",)
    # the flat view still works, for anything that wants every example
    assert set(policy.example_prompts) == {"mild one", "mild two", "middling one", "severe one"}


def test_a_flat_example_list_still_applies_to_every_severity():
    """Existing policy files use a flat list; they must keep working unchanged."""
    policy = parse_policy({
        "policy_id": "A1",
        "policy": "rule",
        "severity": {"low": "a", "medium": "b", "high": "c"},
        "example_prompts": ["one", "two"],
    })
    assert policy.examples_for("low") == ("one", "two")
    assert policy.examples_for("high") == ("one", "two")
    assert policy.example_prompts == ("one", "two")


def test_severity_keyed_examples_accept_the_same_level_aliases():
    policy = parse_policy({
        "policy_id": "A3",
        "policy": "rule",
        "severity": {"minor": "a", "moderate": "b", "severe": "c"},
        "example_prompts": {"Level 1": ["l1"], "2": ["l2"], "high": ["l3"]},
    })
    assert policy.examples_for("low") == ("l1",)
    assert policy.examples_for("medium") == ("l2",)
    assert policy.examples_for("high") == ("l3",)


def test_a_severity_with_no_examples_falls_back_to_the_others():
    """Better to show some examples than none -- but never silently mislabel them."""
    policy = parse_policy({
        "policy_id": "A2",
        "policy": "rule",
        "severity": {"low": "a", "medium": "b", "high": "c"},
        "example_prompts": {"high": ["severe one"]},
    })
    assert policy.examples_for("high") == ("severe one",)
    assert policy.examples_for("low") == ()          # no low examples exist
    assert policy.has_severity_examples is True


def test_policy_reports_whether_examples_are_severity_keyed():
    flat = parse_policy({"policy_id": "X", "policy": "r",
                         "severity": {"low": "a", "medium": "b", "high": "c"},
                         "example_prompts": ["one"]})
    assert flat.has_severity_examples is False


def test_unknown_severity_key_in_examples_is_rejected():
    with pytest.raises(PolicyError, match="example_prompts"):
        parse_policy({"policy_id": "X", "policy": "r",
                      "severity": {"low": "a", "medium": "b", "high": "c"},
                      "example_prompts": {"catastrophic": ["x"]}})


def test_policy_hash_distinguishes_severity_keyed_from_flat_examples():
    """Same examples, different targeting, must not share a provenance digest."""
    flat = parse_policy({"policy_id": "X", "policy": "r",
                         "severity": {"low": "a", "medium": "b", "high": "c"},
                         "example_prompts": ["one", "two"]})
    keyed = parse_policy({"policy_id": "X", "policy": "r",
                          "severity": {"low": "a", "medium": "b", "high": "c"},
                          "example_prompts": {"low": ["one"], "high": ["two"]}})
    assert flat.example_prompts == keyed.example_prompts    # same flat view
    assert flat.sha256 != keyed.sha256                      # but different policies
