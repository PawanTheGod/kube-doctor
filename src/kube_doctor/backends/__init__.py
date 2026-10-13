"""Model backends for Kube Snapshot Doctor."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import shim for type checkers
    from kube_doctor.backends.base import Backend
    from kube_doctor.backends.gemini import GeminiBackend
    from kube_doctor.backends.openai_compat import OpenAICompatBackend

__all__ = ["Backend", "GeminiBackend", "OpenAICompatBackend", "build_backend"]


def __getattr__(name: str) -> object:
    """Import backend classes lazily so optional SDKs stay optional."""
    if name == "Backend":
        from kube_doctor.backends.base import Backend as _Backend

        return _Backend
    if name == "GeminiBackend":
        from kube_doctor.backends.gemini import GeminiBackend as _Gemini

        return _Gemini
    if name == "OpenAICompatBackend":
        from kube_doctor.backends.openai_compat import OpenAICompatBackend as _OpenAICompat

        return _OpenAICompat
    if name == "build_backend":
        from kube_doctor.backends.factory import build_backend as _build

        return _build
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
