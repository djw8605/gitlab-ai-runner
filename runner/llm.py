"""OpenAI-compatible LLM client wrapper for the vLLM endpoint."""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Safe defaults
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT = 300.0  # 5 minutes; LLM calls can be slow


class LLMError(Exception):
    """Raised when the LLM API returns an error."""


class LLMClient:
    """Wrapper for an OpenAI-compatible chat completions endpoint (e.g. vLLM)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        # base_url should include /v1, e.g. http://vllm-svc:8000/v1
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> "LLMClient":
        """Construct from standard environment variables."""
        return cls(
            base_url=os.environ["LLM_BASE_URL"],
            model=os.environ["LLM_MODEL"],
            api_key=os.environ["LLM_API_KEY"],
        )

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        stop: Optional[list[str]] = None,
    ) -> str:
        """Send a chat completion request and return the assistant message text.

        Args:
            messages: List of {"role": ..., "content": ...} dicts.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            stop: Optional stop sequences.

        Returns:
            The assistant's reply as a plain string.

        Raises:
            LLMError: If the API returns an error or an unexpected response.
        """
        payload: dict = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop:
            payload["stop"] = stop

        url = f"{self._base_url}/chat/completions"
        logger.debug("LLM request to %s with model %s", url, self._model)

        try:
            resp = httpx.post(
                url,
                headers=self._headers,
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"LLM API HTTP error {exc.response.status_code}: {exc.response.text[:500]}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM API request timed out after {self._timeout}s") from exc

        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response structure: {data!r}") from exc

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        """Convenience wrapper for a single system+user turn."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.chat(messages, max_tokens=max_tokens, temperature=temperature)
