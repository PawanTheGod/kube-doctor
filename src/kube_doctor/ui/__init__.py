"""Optional Gradio front end. Import ``app`` only when the ``ui`` extra is installed."""

from __future__ import annotations

__all__ = ["app", "build_app", "diagnose", "main"]


def __getattr__(name: str) -> object:
    """Defer importing gradio until an attribute is actually used."""
    if name in __all__:
        from kube_doctor.ui import app as _app

        return getattr(_app, name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
