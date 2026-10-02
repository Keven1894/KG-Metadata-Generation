"""Provider-dispatch chat wrapper for cross-family judging (docs/rag-design/08).

Local to this project on purpose: `core.llm` (HistoryChat, shared by other projects) is
OpenAI-only and we do not want to change its blast radius. This module adds Anthropic support
ADDITIVELY, only for the judge roles that need a non-OpenAI model.

Judge registry — confirmed live model slugs (2026-07-10, via `models.list()` on both APIs):
    self  -> openai    / gpt-5.2          (existing generator; self-graded baseline)
    opus  -> anthropic / claude-opus-4-8  (primary independent, cross-family judge)
    gpt55 -> openai     / gpt-5.5         (same-vendor robustness check, NOT cross-family)
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

JUDGES: dict[str, tuple[str, str]] = {
    "self": ("openai", "gpt-5.2"),
    "opus": ("anthropic", "claude-opus-4-8"),
    "gpt55": ("openai", "gpt-5.5"),
}

# gpt-5.5 (and some other gpt-5.x variants) reject temperature != 1 outright — "does not support 0
# with this model. Only the default (1) value is supported." Drop temperature for these rather
# than erroring; determinism-via-temperature=0 isn't available on this model regardless.
_NO_CUSTOM_TEMPERATURE = {"gpt-5.5"}

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set (expected in historychat/.env, loaded via "
                "core.config's python-dotenv call)."
            )
        _anthropic_client = anthropic.Anthropic(api_key=key)
    return _anthropic_client


def chat_multi(
    messages: list[dict[str, str]],
    judge: str,
    max_tokens: int = 1024,
    temperature: float = 0,
    **kwargs: Any,
) -> str:
    """Dispatch a chat call to the provider behind `judge` (a key in JUDGES).

    `messages` uses the OpenAI convention: an optional {"role": "system", ...} first, then
    {"role": "user", ...}. Anthropic takes system as a separate top-level kwarg, so we split it
    here — callers do not need to know which provider they're talking to.
    """
    if judge not in JUDGES:
        raise ValueError(f"unknown judge '{judge}'; choices: {sorted(JUDGES)}")
    provider, model = JUDGES[judge]

    if provider == "openai":
        from core.llm import chat

        if model in _NO_CUSTOM_TEMPERATURE:
            return chat(messages, model=model, max_tokens=max_tokens, temperature=1, **kwargs)
        return chat(messages, model=model, max_tokens=max_tokens, temperature=temperature, **kwargs)

    if provider == "anthropic":
        system = ""
        user_messages = []
        for m in messages:
            if m["role"] == "system":
                system = (system + "\n\n" + m["content"]).strip()
            else:
                user_messages.append({"role": m["role"], "content": m["content"]})
        # `temperature` is deprecated/rejected outright on current Claude models (Opus 4.x) —
        # unlike OpenAI, Anthropic dropped explicit temperature control here; omit it rather
        # than erroring. Determinism is not available via this knob on this model either way.
        client = _get_anthropic_client()
        resp = client.messages.create(
            model=model,
            system=system or anthropic.NOT_GIVEN,
            messages=user_messages,
            max_tokens=max_tokens,
        )
        return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")

    raise ValueError(f"unknown provider '{provider}' for judge '{judge}'")
