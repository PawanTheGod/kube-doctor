"""Backend selection by name, used by the CLI and the eval harness."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from kube_doctor.backends.base import Backend, BackendConfigError

#: Backend names accepted on the command line.
BACKEND_NAMES: Final[tuple[str, ...]] = ("gemini", "openai-compat")
#: Name used when the caller does not choose.
DEFAULT_BACKEND: Final[str] = "gemini"


def build_backend(
    name: str = DEFAULT_BACKEND,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
    fallback_model: str | None = None,
    client_factory: Callable[[object], object] | None = None,
) -> Backend:
    """Instantiate the backend named by ``name``.

    Imports are deferred so that, for example, using a local OpenAI-compatible
    endpoint does not require the Gemini SDK to import cleanly.
    """
    normalised = name.strip().lower()
    if normalised == "gemini":
        from kube_doctor.backends.gemini import GeminiBackend

        return GeminiBackend(
            model=model,
            api_key=api_key,
            timeout=timeout,
            fallback_model=fallback_model,
            client_factory=client_factory,
        )
    if normalised in {"openai-compat", "openai", "local", "ollama"}:
        from kube_doctor.backends.openai_compat import OpenAICompatBackend

        return OpenAICompatBackend(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            client_factory=client_factory,
        )
    msg = f"unknown backend {name!r}; choose one of {', '.join(BACKEND_NAMES)}"
    raise BackendConfigError(msg)
