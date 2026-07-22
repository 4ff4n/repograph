"""Thin LLM client used by all agents.

Defaults to Fireworks AI (OpenAI-compatible endpoint). Any OpenAI-compatible
provider works by overriding REPOGRAPH_BASE_URL / REPOGRAPH_API_KEY_ENV.

Environment variables
---------------------
FIREWORKS_API_KEY      : API key (default key variable)
REPOGRAPH_MODEL        : model id (default: Qwen 2.5 Coder 32B on Fireworks)
REPOGRAPH_BASE_URL     : override the API base URL
REPOGRAPH_API_KEY_ENV  : name of the env var holding the key, if not Fireworks
"""

from __future__ import annotations

import json
import os

DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_MODEL = "accounts/fireworks/models/kimi-k2p6"


class ModelUnavailable(RuntimeError):
    """The configured model is not deployed for this account."""


class LLMClient:
    def __init__(self, model: str | None = None):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        key_env = os.getenv("REPOGRAPH_API_KEY_ENV", "FIREWORKS_API_KEY")
        self.api_key = os.getenv(key_env, "")
        self.base_url = os.getenv("REPOGRAPH_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.getenv("REPOGRAPH_MODEL", DEFAULT_MODEL)
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        return self._client

    def complete_json(self, system: str, user: str,
                      max_tokens: int = 1500, temperature: float = 0.1) -> dict:
        """One chat completion that must return a JSON object."""
        client = self._get_client()
        try:
            resp = client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:
            # Serverless providers retire model deployments regularly, so turn
            # the opaque 404 into instructions the user can act on.
            if "not found" in str(exc).lower() or "NOT_FOUND" in str(exc):
                raise ModelUnavailable(
                    f"model '{self.model}' is not deployed for this account.\n"
                    f"  List what you can use:\n"
                    f"    curl -s {self.base_url}/models \\\n"
                    f"      -H \"Authorization: Bearer $FIREWORKS_API_KEY\" "
                    f"| grep '\"id\"'\n"
                    f"  Then pass --model <id>, or set REPOGRAPH_MODEL in .env"
                ) from exc
            raise
        text = (resp.choices[0].message.content or "").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
            raise
