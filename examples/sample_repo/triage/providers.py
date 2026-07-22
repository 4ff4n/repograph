"""LLM provider integration (Fireworks AI, OpenAI-compatible).

This module talks to a hosted model. It is deliberately written with a couple
of real-world smells so RepoGraph's security agent has something to find:
the base URL is taken from config without validation, and the raw ticket text
(which may contain PII) is forwarded to the provider unfiltered.
"""

from __future__ import annotations

import os

from openai import OpenAI

from .config import Settings


class LLMProvider:
    """Thin wrapper over an OpenAI-compatible chat endpoint."""

    def __init__(self, settings: Settings):
        self.settings = settings
        # base_url comes straight from config with no scheme/host allowlist.
        self._client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=os.environ.get("FIREWORKS_API_KEY", ""),
        )

    def classify(self, ticket_text: str) -> str:
        """Ask the model to categorise a support ticket."""
        resp = self._client.chat.completions.create(
            model=self.settings.model,
            max_tokens=200,
            temperature=0.0,
            messages=[
                {"role": "system", "content": "Classify the support ticket. "
                 "Reply with one word: billing, technical, account, or other."},
                # raw user text forwarded with no redaction of emails / card numbers
                {"role": "user", "content": ticket_text},
            ],
        )
        return (resp.choices[0].message.content or "other").strip().lower()

    def summarize(self, ticket_text: str) -> str:
        """Produce a one-line summary for the dashboard."""
        resp = self._client.chat.completions.create(
            model=self.settings.model,
            max_tokens=120,
            messages=[
                {"role": "system", "content": "Summarise this ticket in one sentence."},
                {"role": "user", "content": ticket_text},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
