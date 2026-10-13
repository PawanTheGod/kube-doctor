"""Backend for any OpenAI-compatible vision endpoint.

Works with Ollama (``http://localhost:11434/v1``), vLLM, llama.cpp's server,
LM Studio, and hosted gateways. Images go out as base64 data URLs.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from typing import Any, Final

from kube_doctor.backends.base import (
    DEFAULT_MAX_ATTEMPTS,
    BackendConfigError,
    BackendError,
    call_with_retries,
    resolve_model_id,
    resolve_timeout,
)

logger = logging.getLogger(__name__)

#: Local endpoints need no key, so fall back to a placeholder the SDK accepts.
_PLACEHOLDER_API_KEY: Final[str] = "not-needed"
DEFAULT_BASE_URL: Final[str] = "http://localhost:11434/v1"


class OpenAICompatBackend:
    """Talk to an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        client_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        """Build the backend, resolving model, base URL, and key from the env."""
        import os

        self._model = resolve_model_id(model)
        self._base_url = (
            base_url or os.environ.get("KUBE_DOCTOR_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        env_key = api_key or os.environ.get("KUBE_DOCTOR_API_KEY") or ""
        self._api_key = env_key or _PLACEHOLDER_API_KEY
        self._timeout = resolve_timeout(timeout)
        self._max_attempts = max_attempts
        self._client_factory = client_factory
        self._client: Any = None

    @property
    def model_id(self) -> str:
        """Return the configured model id served by the local endpoint."""
        return self._model

    @property
    def primary_model_id(self) -> str:
        """Return the configured model id.

        This backend has no fallback: a local endpoint either serves the model or
        it does not, so the primary and the active model are always the same.
        """
        return self._model

    @property
    def used_fallback(self) -> bool:
        """Always false: this backend never switches models."""
        return False

    @property
    def timeout_seconds(self) -> float:
        """Return the resolved per-request timeout in seconds."""
        return self._timeout

    @property
    def base_url(self) -> str:
        """Return the OpenAI-compatible base URL in use."""
        return self._base_url

    def _client_or_create(self) -> Any:
        """Create the OpenAI client on first use."""
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory(self)
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            msg = "openai is not installed; run `pip install -e .`."
            raise BackendConfigError(msg) from exc
        self._client = OpenAI(base_url=self._base_url, api_key=self._api_key, timeout=self._timeout)
        return self._client

    def _messages(
        self, prompt: str, image_bytes: bytes | None, mime: str | None
    ) -> list[dict[str, Any]]:
        """Build the chat messages, embedding the image as a data URL."""
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image_bytes:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime or 'image/png'};base64,{encoded}"},
                }
            )
        return [{"role": "user", "content": content}]

    def _generate_once(
        self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
    ) -> str:
        """Make a single chat completion call and return the reply text."""
        client = self._client_or_create()
        response = client.chat.completions.create(
            model=self._model,
            messages=self._messages(prompt, image_bytes, mime),
            temperature=0.1,
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            msg = f"{self._base_url} returned no choices for model {self._model}."
            raise BackendError(msg)
        text = choices[0].message.content or ""
        if not text.strip():
            msg = f"{self._base_url} returned an empty reply for model {self._model}."
            raise BackendError(msg)
        return text

    def generate(
        self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
    ) -> str:
        """Generate text with retries on rate limits and 5xx responses."""
        return call_with_retries(
            lambda: self._generate_once(prompt, image_bytes, mime),
            max_attempts=self._max_attempts,
        )
