"""Async chat-completions client.

Follows the conventions already used by airflow-model-evaluation: AsyncOpenAI
against an OpenAI-compatible base_url, a semaphore to bound concurrency, and
bounded retries with backoff. Differences are deliberate:

- A failed call returns a ``Completion`` carrying the error rather than an empty
  string, so the caller can report how many calls failed and why. Silent "" is
  how a run ends up with a third of the data it claimed.
- Retries distinguish rate limits (worth waiting for) from refusals and bad
  requests (retrying will not help).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Completion:
    """One model response, successful or not."""

    content: str
    ok: bool
    attempts: int
    error: str | None = None
    # Why the model stopped. "length" means it was cut off at max_tokens, which
    # looks identical to a rambling reply once you only have the text.
    finish_reason: str | None = None
    completion_tokens: int | None = None


class ChatClient:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        concurrency: int = 4,
        max_retries: int = 3,
        timeout: float = 300.0,
        temperature: float = 1.0,
        max_tokens: int = 2048,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)

        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def complete(
        self,
        system: str,
        user: str,
        model: str | None = None,
        extra_body: dict | None = None,
    ) -> Completion:
        """One chat completion, with retries. Never raises for model-side errors."""
        from openai import APIStatusError, OpenAIError

        last_error = "unknown error"
        for attempt in range(1, self.max_retries + 1):
            try:
                async with self._semaphore:
                    response = await asyncio.wait_for(
                        self._client.chat.completions.create(
                            model=model or self.model,
                            messages=[
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                            temperature=self.temperature,
                            max_tokens=self.max_tokens,
                            **({"extra_body": extra_body} if extra_body else {}),
                        ),
                        timeout=self.timeout,
                    )
                choice = response.choices[0]
                content = (choice.message.content or "").strip()
                finish_reason = getattr(choice, "finish_reason", None)
                usage = getattr(response, "usage", None)
                completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
                if not content:
                    last_error = "model returned empty content"
                    logger.warning("empty content on attempt %d/%d", attempt, self.max_retries)
                    await asyncio.sleep(2 * attempt)
                    continue
                if finish_reason == "length":
                    logger.warning("response hit max_tokens (%s); it is truncated",
                                   self.max_tokens)
                return Completion(content=content, ok=True, attempts=attempt,
                                  finish_reason=finish_reason,
                                  completion_tokens=completion_tokens)

            except asyncio.TimeoutError:
                last_error = f"timeout after {self.timeout}s"
                logger.warning("timeout on attempt %d/%d", attempt, self.max_retries)
                await asyncio.sleep(2 * attempt)

            except APIStatusError as error:
                status = getattr(error, "status_code", None)
                last_error = f"HTTP {status}"
                if status == 429:
                    wait = 10 * attempt
                    logger.warning("rate limited; waiting %ds", wait)
                    await asyncio.sleep(wait)
                elif status is not None and 400 <= status < 500 and status != 429:
                    # A 4xx that is not a rate limit will not fix itself.
                    logger.error("non-retryable %s: %s", last_error, error)
                    return Completion("", False, attempt, last_error)
                else:
                    await asyncio.sleep(5 * attempt)

            except OpenAIError as error:
                last_error = f"{type(error).__name__}: {error}"
                logger.warning("api error on attempt %d/%d: %s", attempt, self.max_retries, error)
                await asyncio.sleep(5 * attempt)

            except Exception as error:  # noqa: BLE001 - one bad call must not kill the run
                last_error = f"{type(error).__name__}: {error}"
                logger.warning("unexpected error on attempt %d/%d: %s", attempt, self.max_retries, error)
                await asyncio.sleep(5 * attempt)

        return Completion("", False, self.max_retries, last_error)

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception as error:  # noqa: BLE001
            logger.debug("error closing client: %s", error)
