"""The client must report *why* a response ended, not just what it contained.

Truncation is this pipeline's number one failure mode, and a truncated response
and a rambling one are indistinguishable from their text alone. `finish_reason`
is the only thing that separates "the model hit max_tokens" from "the model
wrote something we could not parse", so it has to survive into the record.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from harmful_prompt.client import ChatClient


def fake_openai(monkeypatch, content, finish_reason, usage_completion_tokens=None):
    """Replace openai.AsyncOpenAI with one that answers from fixed values."""
    import openai

    class FakeCompletions:
        async def create(self, **kwargs):
            usage = (SimpleNamespace(completion_tokens=usage_completion_tokens)
                     if usage_completion_tokens is not None else None)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason=finish_reason,
                )],
                usage=usage,
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            pass

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)


def build(**overrides):
    kwargs = dict(model="m", api_key="k", base_url="http://example.invalid/v1",
                  concurrency=1, max_retries=1, timeout=5, temperature=0.0,
                  max_tokens=100)
    kwargs.update(overrides)
    return ChatClient(**kwargs)


@pytest.mark.parametrize("reason", ["stop", "length"])
def test_finish_reason_is_carried_onto_the_completion(monkeypatch, reason):
    fake_openai(monkeypatch, '{"prompts": []}', reason)
    client = build()
    result = asyncio.run(client.complete("sys", "usr"))
    assert result.ok is True
    assert result.finish_reason == reason


def test_completion_token_count_is_recorded_when_the_api_reports_it(monkeypatch):
    fake_openai(monkeypatch, "text", "length", usage_completion_tokens=4096)
    client = build()
    result = asyncio.run(client.complete("sys", "usr"))
    assert result.completion_tokens == 4096


def test_missing_usage_is_not_an_error(monkeypatch):
    """Not every gateway returns usage; its absence must not break a run."""
    fake_openai(monkeypatch, "text", "stop")
    client = build()
    result = asyncio.run(client.complete("sys", "usr"))
    assert result.completion_tokens is None
    assert result.ok is True
