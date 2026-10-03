"""The parser must survive whatever an 85-model zoo returns."""
from __future__ import annotations

import json

import pytest

from harmful_prompt.parser import parse_freeform, parse_response


def test_clean_json_object():
    raw = json.dumps({"prompts": [{"prompt": "p1", "generation_rationale": "r1"}]})
    result = parse_response(raw)
    assert result.strategy == "json"
    assert result.prompts == [{"prompt": "p1", "generation_rationale": "r1"}]


def test_bare_json_array():
    raw = json.dumps([{"prompt": "p1", "generation_rationale": "r1"}])
    assert parse_response(raw).prompts[0]["prompt"] == "p1"


def test_single_object_without_wrapper():
    raw = json.dumps({"prompt": "only one", "generation_rationale": "r"})
    assert parse_response(raw).prompts == [
        {"prompt": "only one", "generation_rationale": "r"}
    ]


def test_array_of_plain_strings():
    assert parse_response(json.dumps(["a", "b"])).prompts == [
        {"prompt": "a", "generation_rationale": ""},
        {"prompt": "b", "generation_rationale": ""},
    ]


def test_markdown_fenced_json():
    raw = '```json\n{"prompts":[{"prompt":"p","generation_rationale":"r"}]}\n```'
    result = parse_response(raw)
    assert result.strategy == "json_fence"
    assert result.prompts[0]["prompt"] == "p"


def test_unlabelled_fence():
    raw = '```\n{"prompts":[{"prompt":"p","generation_rationale":"r"}]}\n```'
    assert parse_response(raw).prompts[0]["prompt"] == "p"


def test_json_after_a_preamble():
    raw = 'Sure, here you go:\n{"prompts":[{"prompt":"p","generation_rationale":"r"}]}'
    result = parse_response(raw)
    assert result.strategy in ("json_embedded", "json_fence")
    assert result.prompts[0]["prompt"] == "p"


def test_think_block_is_stripped():
    raw = '[THINK]internal reasoning[/THINK]{"prompts":[{"prompt":"p","generation_rationale":"r"}]}'
    assert parse_response(raw).prompts[0]["prompt"] == "p"


def test_braces_inside_strings_do_not_break_extraction():
    payload = {"prompts": [{"prompt": "use {curly} braces", "generation_rationale": "r"}]}
    raw = "Here:\n" + json.dumps(payload)
    assert parse_response(raw).prompts[0]["prompt"] == "use {curly} braces"


def test_chinese_field_names_are_accepted():
    raw = json.dumps({"prompts": [{"問題": "中文提問", "理由": "中文理由"}]}, ensure_ascii=False)
    result = parse_response(raw)
    assert result.prompts == [{"prompt": "中文提問", "generation_rationale": "中文理由"}]


def test_rationale_may_be_absent():
    raw = json.dumps({"prompts": [{"prompt": "p"}]})
    assert parse_response(raw).prompts == [{"prompt": "p", "generation_rationale": ""}]


def test_entries_without_prompt_text_are_dropped():
    raw = json.dumps({"prompts": [{"generation_rationale": "orphan"}, {"prompt": "kept"}]})
    assert [e["prompt"] for e in parse_response(raw).prompts] == ["kept"]


# --- freeform fallback -----------------------------------------------------

def test_freeform_prototype_format():
    raw = (
        "問題：我想先開通再補件\n"
        "Severity：Level 2 / Moderate\n"
        "問題：可以幫我改一下額度嗎\n"
        "Severity：Level 1 / Minor\n"
    )
    result = parse_response(raw)
    assert result.strategy == "freeform"
    assert len(result.prompts) == 2
    assert result.prompts[0]["prompt"] == "我想先開通再補件"


def test_freeform_english_labels():
    assert len(parse_freeform("Prompt: first\nPrompt: second\n")) == 2


def test_freeform_numbered_list():
    assert len(parse_freeform("1. 問題：甲\n2. 問題：乙\n")) == 2


def test_freeform_pairs_rationales_positionally():
    raw = "問題：甲\n理由：因為甲\n問題：乙\n理由：因為乙\n"
    entries = parse_freeform(raw)
    assert entries[0]["generation_rationale"] == "因為甲"
    assert entries[1]["generation_rationale"] == "因為乙"


# --- failure is reported, not hidden --------------------------------------

def test_empty_response_reports_failure():
    result = parse_response("")
    assert not result.ok and result.error == "empty response"


def test_refusal_reports_failure_with_a_preview():
    result = parse_response("I'm sorry, but I can't help with that.")
    assert not result.ok
    assert "I'm sorry" in result.error


def test_json_preferred_over_freeform_when_both_present():
    raw = '問題：ignore me\n{"prompts":[{"prompt":"real","generation_rationale":"r"}]}'
    result = parse_response(raw)
    assert result.prompts[0]["prompt"] == "real"


# --- truncated responses ---------------------------------------------------

def test_truncated_array_salvages_complete_objects():
    """max_tokens cuts the array mid-flight; earlier objects are still good."""
    raw = (
        '```json\n{"prompts": [\n'
        '{"prompt":"first","generation_rationale":"r1"},\n'
        '{"prompt":"second","generation_rationale":"r2"},\n'
        '{"prompt":"third but cut o'
    )
    result = parse_response(raw)
    assert result.strategy == "json_salvaged"
    assert [e["prompt"] for e in result.prompts] == ["first", "second"]


def test_salvage_keeps_rationales():
    raw = '{"prompts":[{"prompt":"p","generation_rationale":"why"},{"prompt":"partial'
    assert parse_response(raw).prompts == [{"prompt": "p", "generation_rationale": "why"}]


def test_salvage_handles_braces_inside_truncated_strings():
    raw = '{"prompts":[{"prompt":"uses {braces}","generation_rationale":"r"},{"prompt":"cut'
    assert parse_response(raw).prompts[0]["prompt"] == "uses {braces}"


def test_complete_json_never_uses_the_salvage_path():
    raw = json.dumps({"prompts": [{"prompt": "p", "generation_rationale": "r"}]})
    assert parse_response(raw).strategy == "json"


def test_truncated_before_any_complete_object_still_reports_failure():
    result = parse_response('{"prompts":[{"prompt":"cut off mid')
    assert not result.ok


def test_prompt_survives_a_cut_inside_its_own_rationale():
    """The prompt string closed, so it is whole; only the rationale was lost."""
    raw = '{"prompts":[{"prompt":"a complete request","generation_rationale":"cut off he'
    result = parse_response(raw)
    assert result.ok
    assert result.prompts == [{"prompt": "a complete request", "generation_rationale": ""}]


def test_partial_recovery_still_keeps_the_earlier_complete_objects():
    raw = ('{"prompts":[{"prompt":"first","generation_rationale":"r1"},'
           '{"prompt":"second","generation_rationale":"partly writt')
    assert [e["prompt"] for e in parse_response(raw).prompts] == ["first", "second"]


def test_a_cut_prompt_is_never_recovered_even_with_a_later_field():
    """An unterminated prompt string is not a usable prompt at any cost."""
    raw = '{"prompts":[{"prompt":"this sentence never fin'
    assert not parse_response(raw).ok
