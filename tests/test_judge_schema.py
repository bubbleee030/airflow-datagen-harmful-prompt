"""Judge output is parsed strictly, and never auto-accepted on a shaky result.

Reasoner models wrap their answer in think blocks and fences, so parsing has to
be tolerant about packaging. It must not be tolerant about meaning: a verdict
that names another policy, or that needs facts nobody supplied, is quarantined
rather than trusted.
"""
from __future__ import annotations

import pytest

from harmful_prompt.config import JudgeSettings
from harmful_prompt.judge_schema import JudgeParseError, audit_judgment, parse_judge_response

VALID = '{"verdict":"violation","matched_policy_id":"A1","severity":"medium",' \
        '"requires_external_context":false,"decision_summary":"The request crosses the rule."}'


@pytest.mark.parametrize("raw", [
    VALID,
    "```json\n" + VALID + "\n```",
    "<think>private reasoning</think>\n" + VALID,
    "[THINK]private reasoning[/THINK]\n" + VALID,
])
def test_parse_normalizes_supported_wrappers(raw):
    result = parse_judge_response(raw, "A1")
    assert result.verdict == "violation"
    assert result.severity == "medium"
    assert "private reasoning" not in result.decision_summary


@pytest.mark.parametrize("raw", [
    '{"verdict":"maybe","matched_policy_id":null,"severity":null,'
    '"requires_external_context":false,"decision_summary":"x"}',
    '{"verdict":"non_violation","matched_policy_id":null,"severity":"low",'
    '"requires_external_context":false,"decision_summary":"x"}',
    '{"verdict":"violation","matched_policy_id":"OTHER","severity":"low",'
    '"requires_external_context":false,"decision_summary":"x"}',
])
def test_parse_rejects_invalid_or_cross_policy_results(raw):
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, "A1")


def test_audit_accepts_only_policy_and_severity_match():
    result = parse_judge_response(VALID, "A1")
    record = {"violated_policy": "A1", "severity": "medium"}
    audit = audit_judgment(result, record, JudgeSettings(enabled=True))
    assert audit == {"policy_match": True, "severity_match": True, "accepted": True}


def test_external_context_never_auto_accepts():
    raw = VALID.replace("false", "true")
    result = parse_judge_response(raw, "A1")
    audit = audit_judgment(result, {"violated_policy": "A1", "severity": "medium"},
                           JudgeSettings(enabled=True))
    assert audit["accepted"] is False


def test_severity_mismatch_blocks_acceptance_only_when_configured():
    result = parse_judge_response(VALID, "A1")
    record = {"violated_policy": "A1", "severity": "high"}
    strict = audit_judgment(result, record,
                            JudgeSettings(enabled=True, require_severity_match=True))
    assert strict == {"policy_match": True, "severity_match": False, "accepted": False}
    lenient = audit_judgment(result, record,
                             JudgeSettings(enabled=True, require_severity_match=False))
    assert lenient["accepted"] is True


def test_non_violation_never_matches_the_claimed_policy():
    raw = ('{"verdict":"non_violation","matched_policy_id":null,"severity":null,'
           '"requires_external_context":false,"decision_summary":"Within bounds."}')
    result = parse_judge_response(raw, "A1")
    audit = audit_judgment(result, {"violated_policy": "A1", "severity": "low"},
                           JudgeSettings(enabled=True))
    assert audit == {"policy_match": False, "severity_match": False, "accepted": False}


@pytest.mark.parametrize("raw", [
    '{"verdict":"violation","matched_policy_id":"A1","severity":"medium",'
    '"requires_external_context":"false","decision_summary":"x"}',
    '{"verdict":"violation","matched_policy_id":"A1","severity":"medium",'
    '"decision_summary":"x"}',
    "I cannot judge that.",
    "<think>never closed",
])
def test_parse_rejects_unusable_payloads(raw):
    with pytest.raises(JudgeParseError):
        parse_judge_response(raw, "A1")


def test_severity_is_recorded_but_does_not_gate_by_default():
    """Judge-vs-human severity kappa measured 0.083 -- gating on it gates on noise.

    The comparison is still recorded, so a caller who wants it can filter later;
    it just no longer decides acceptance on its own.
    """
    result = parse_judge_response(VALID, "A1")
    record = {"violated_policy": "A1", "severity": "high"}   # judge said medium
    audit = audit_judgment(result, record, JudgeSettings(enabled=True))
    assert audit["severity_match"] is False       # still measured
    assert audit["accepted"] is True              # but no longer blocking

    strict = audit_judgment(result, record,
                            JudgeSettings(enabled=True, require_severity_match=True))
    assert strict["accepted"] is False            # opt back in if you want it
