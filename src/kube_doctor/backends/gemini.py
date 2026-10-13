"""Gemini API backend (the default) using ``google-genai``.

Open-weight Gemma models served through the Gemini API may not support system
instructions or JSON mode reliably, so this backend keeps the entire prompt in
the user turn and lets the pipeline's validation and single repair retry handle
format mistakes.

The Gemini service returns 500, 503, and 504 while a model is loading or a
region is saturated, which is routine rather than exceptional. That is handled
by the shared retry policy in :mod:`kube_doctor.backends.base`, plus an optional
fallback model for when the primary stays unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final

from kube_doctor.backends.base import (
    DEFAULT_MAX_ATTEMPTS,
    BackendConfigError,
    BackendError,
    BackendRetryExhausted,
    call_with_retries,
    require_env,
    resolve_fallback_model,
    resolve_model_id,
    resolve_timeout,
    warn_to_stderr,
)

logger = logging.getLogger(__name__)

#: Sampling temperature kept low: we want transcription and ranking, not prose.
DEFAULT_TEMPERATURE: Final[float] = 0.1


class GeminiBackend:
    """Talk to a Gemini-API model id, typically an open-weight Gemma release."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        fallback_model: str | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        """Build the backend, resolving configuration from the environment."""
        self._primary_model = resolve_model_id(model)
        self._api_key = (api_key or require_env("GEMINI_API_KEY")).strip()
        if not self._api_key:
            msg = "GEMINI_API_KEY is empty."
            raise BackendConfigError(msg)
        self._timeout = resolve_timeout(timeout)
        self._max_attempts = max_attempts
        self._fallback_model = resolve_fallback_model(fallback_model)
        self._client_factory = client_factory
        self._client: Any = None
        self._active_model = self._primary_model
        self._used_fallback = False

    @property
    def model_id(self) -> str:
        """Return the model that answered the most recent call.

        After a fallback this is the fallback's id, which is what the pipeline
        records in ``diagnosis.model_id``.
        """
        return self._active_model

    @property
    def primary_model_id(self) -> str:
        """Return the configured primary model id, whether or not it answered."""
        return self._primary_model

    @property
    def fallback_model_id(self) -> str | None:
        """Return the configured fallback model id, or ``None``."""
        return self._fallback_model

    @property
    def used_fallback(self) -> bool:
        """Report whether the most recent reply came from the fallback model."""
        return self._used_fallback

    @property
    def timeout_seconds(self) -> float:
        """Return the resolved per-request timeout in seconds."""
        return self._timeout

    def _client_or_create(self) -> Any:
        """Create the ``google.genai`` client on first use."""
        if self._client is not None:
            return self._client
        self._client = self._create_client()
        return self._client

    def _create_client(self) -> Any:
        """Instantiate the SDK client with the configured timeout."""
        if self._client_factory is not None:
            return self._client_factory(self._api_key)
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - dependency is declared
            msg = "google-genai is not installed; run `pip install -e .`."
            raise BackendConfigError(msg) from exc
        # The SDK takes the timeout in milliseconds.
        return genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(timeout=int(self._timeout * 1000)),
        )

    def _contents(self, prompt: str, image_bytes: bytes | None, mime: str | None) -> Any:
        """Build the request contents, with the image inline when present."""
        from google.genai import types  # imported lazily for clearer errors

        parts: list[Any] = [types.Part.from_text(text=prompt)]
        if image_bytes:
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=mime or "image/png"))
        return [types.Content(role="user", parts=parts)]

    def _generate_with_model(
        self, model: str, prompt: str, image_bytes: bytes | None, mime: str | None
    ) -> str:
        """Make one API call against ``model`` and return the concatenated text."""
        from google.genai import types  # imported lazily for clearer errors

        client = self._client_or_create()
        config = types.GenerateContentConfig(temperature=DEFAULT_TEMPERATURE)
        response = client.models.generate_content(
            model=model,
            contents=self._contents(prompt, image_bytes, mime),
            config=config,
        )
        text = getattr(response, "text", None)
        if not text:
            candidates = getattr(response, "candidates", None) or []
            chunks: list[str] = []
            for candidate in candidates:
                content = getattr(candidate, "content", None)
                for part in getattr(content, "parts", None) or []:
                    chunk = getattr(part, "text", "")
                    if chunk:
                        chunks.append(chunk)
            text = "".join(chunks)
        if not text:
            msg = f"Gemini returned an empty reply for model {model}."
            raise BackendError(msg)
        return text

    def generate(
        self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
    ) -> str:
        """Generate text, retrying a busy service and falling back if configured.

        The primary model gets the full retry budget. Only when it is exhausted
        on a retryable error, and only when ``KUBE_DOCTOR_FALLBACK_MODEL`` is
        set, is the fallback tried: once, without retries, since a second busy
        model is not going to be rescued by waiting.
        """
        self._active_model = self._primary_model
        self._used_fallback = False
        try:
            text = call_with_retries(
                lambda: self._generate_with_model(self._primary_model, prompt, image_bytes, mime),
                max_attempts=self._max_attempts,
            )
        except BackendRetryExhausted as exc:
            if self._fallback_model is None or not exc.retryable:
                raise
            reason = exc.last_error if exc.last_error is not None else exc
            logger.warning(
                "primary model %s unavailable after %d attempts (%s); trying fallback %s",
                self._primary_model,
                self._max_attempts,
                reason,
                self._fallback_model,
            )
            try:
                text = self._generate_with_model(self._fallback_model, prompt, image_bytes, mime)
            except Exception as fallback_exc:
                msg = (
                    f"primary model {self._primary_model} and fallback "
                    f"{self._fallback_model} both failed: {reason}; {fallback_exc}"
                )
                raise BackendError(msg) from fallback_exc
            self._active_model = self._fallback_model
            self._used_fallback = True
            warn_to_stderr(
                f"the primary model {self._primary_model} was unavailable "
                f"({reason}); this diagnosis was produced by the fallback model "
                f"{self._fallback_model} instead."
            )
        # A non-retryable failure on the primary propagates untouched:
        # ``call_with_retries`` re-raises it, and another model would reject the
        # same request for the same reason.
        return text


def list_gemini_models(api_key: str, *, timeout: float | None = None) -> list[str]:
    """List model ids the API key can reach, so users can pick one.

    This exists because model ids must not be hardcoded; run it once to find
    the current open-weight Gemma id.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - dependency is declared
        msg = "google-genai is not installed; run `pip install -e .`."
        raise BackendConfigError(msg) from exc
    seconds = resolve_timeout(timeout)
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=int(seconds * 1000)),
    )
    return sorted(str(model.name) for model in client.models.list())
