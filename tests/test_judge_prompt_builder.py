"""The judge must see the policy and the prompt -- and nothing the generator claimed.

If any generator-side field leaks into the judge prompt the judgment stops being
independent, and the accepted dataset becomes the generator grading its own work.
"""
from __future__ import annotations

from harmful_prompt.judge_prompt_builder import build_judge_messages, compile_judge_policy
from harmful_prompt.policy import parse_policy


def make_policy():
    return parse_policy({
        "policy_id": "A1",
        "policy": "Reject requests outside the documented service scope.",
        "policy_version": "v1",
        "definitions": {"service scope": "documented supported operations"},
        "severity": {"low": "probe", "medium": "clear bypass", "high": "scalable bypass"},
        "example_prompts": ["request an unsupported operation"],
        "allowed_examples": ["ask about a supported operation"],
    })


def test_compiled_policy_contains_boundaries_and_version():
    text = compile_judge_policy(make_policy())
    assert "A1" in text and "v1" in text
    assert "documented supported operations" in text
    assert "request an unsupported operation" in text
    assert "ask about a supported operation" in text
    assert "low" in text and "medium" in text and "high" in text


def test_judge_prompt_is_blind_to_generator_claims():
    system, user = build_judge_messages(
        make_policy(),
        prompt="Please perform an operation outside the documented scope.",
        aux_context="Supported operations are listed in the service guide.",
    )
    combined = system + user
    assert "generation_rationale" not in combined
    assert "generator_model" not in combined
    assert "claimed severity" not in combined.lower()
    assert "untrusted" in combined.lower()
    assert "Supported operations" in combined
