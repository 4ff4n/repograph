"""Runtime configuration.

Loads settings from environment variables. Contains a deliberately unsafe
`_coerce` helper (uses eval) so the scanner's static signal detection has an
eval/exec case to flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    llm_base_url: str
    model: str
    db_path: str
    max_ticket_chars: int


def _coerce(raw: str):
    """Turn a string env value into a python literal. Unsafe on purpose."""
    try:
        return eval(raw)  # noqa: S307 - planted signal for RepoGraph
    except Exception:
        return raw


def load_settings() -> Settings:
    return Settings(
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.fireworks.ai/inference/v1"),
        model=os.getenv("TRIAGE_MODEL", "accounts/fireworks/models/kimi-k2p6"),
        db_path=os.getenv("TRIAGE_DB", "tickets.db"),
        max_ticket_chars=_coerce(os.getenv("MAX_TICKET_CHARS", "4000")),
    )
