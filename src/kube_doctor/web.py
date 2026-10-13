"""Unified public server: landing page + developer console workspace.

Architecture decision: the workspace is a purpose-built console UI backed
by a small typed JSON boundary in this module. Gradio remains available
standalone (``python -m kube_doctor.ui.app``), but the public product no
longer mounts it: a static console delivers real navigation, SEO-friendly
markup, and an AWS-style layout with no build step and no new
dependencies (``fastapi``/``uvicorn`` already ship with Gradio).

Routes:

* ``/`` — static marketing landing
* ``/workspace`` — developer console (static app, calls ``/api/diagnose``)
* ``POST /api/diagnose`` — multipart run of the pipeline; structured JSON
* ``GET /api/backends`` — public backend names (no secrets, no model ids)
* ``/healthz`` — container health check

The Python pipeline remains the single source of truth: redaction, the
safety lint, schemas, and backend retries all run unchanged, in-process.
Secrets stay server-side in environment variables; error responses never
leak tracebacks or configuration values.
"""

from __future__ import annotations

import logging
import os
import tempfile
from importlib import resources
from pathlib import Path
from typing import Final

from kube_doctor.backends.base import BackendConfigError, BackendError
from kube_doctor.backends.factory import build_backend
from kube_doctor.pipeline import InputError, ModelOutputError, run

try:  # Server-only dependencies; the pipeline itself never needs them.
    from fastapi import FastAPI, File, Form, UploadFile
    from fastapi.responses import JSONResponse, Response

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - declared dependency
    _FASTAPI_AVAILABLE = False

logger = logging.getLogger(__name__)

#: Directory holding the static landing assets, resolved from the package
#: so source checkouts and wheels behave identically.
LANDING_DIR = resources.files("kube_doctor.ui") / "landing"
#: Directory holding the static console workspace assets.
WORKSPACE_DIR = resources.files("kube_doctor.ui") / "workspace"

#: Static files served at the site root (path -> (directory, content type)).
_STATIC_FILES: Final[dict[str, tuple[str, str]]] = {
    "index.html": ("landing", "text/html; charset=utf-8"),
    "styles.css": ("landing", "text/css; charset=utf-8"),
    "app.js": ("landing", "text/javascript; charset=utf-8"),
    "favicon.svg": ("landing", "image/svg+xml"),
    "workspace.css": ("workspace", "text/css; charset=utf-8"),
    "workspace.js": ("workspace", "text/javascript; charset=utf-8"),
}

#: Backend names the API accepts (mirrors the factory aliases).
_BACKEND_NAMES: Final[tuple[str, ...]] = (
    "gemini",
    "openai-compat",
    "openai",
    "local",
    "ollama",
)

#: Image suffixes the pipeline can process.
_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
)
#: Largest accepted upload: screenshots bigger than this are user error.
_MAX_IMAGE_BYTES: Final[int] = 15 * 1024 * 1024
#: Largest accepted logs field; the pipeline truncates to 24k downstream.
_MAX_LOGS_CHARS: Final[int] = 200_000


def _read_static(name: str) -> tuple[bytes, str]:
    """Return a static asset and its content type, or raise ``KeyError``."""
    area, content_type = _STATIC_FILES[name]  # KeyError -> caller 404s
    base = WORKSPACE_DIR if area == "workspace" else LANDING_DIR
    return (base / name).read_bytes(), content_type


def build_web_app() -> object:
    """Build the FastAPI app serving landing, console, and the API."""
    if not _FASTAPI_AVAILABLE:
        msg = "fastapi is not installed; run `pip install -e .`."
        raise BackendConfigError(msg)
    api: FastAPI = FastAPI(title="Kube Snapshot Doctor")

    @api.get("/healthz")
    def healthz() -> dict[str, str]:
        """Report liveness for container orchestrators."""
        return {"status": "ok"}

    @api.get("/api/backends")
    def backends() -> dict[str, list[str]]:
        """List the backend names the diagnose endpoint accepts."""
        return {"backends": ["gemini", "openai-compat"]}

    @api.post("/api/diagnose")
    async def diagnose(
        image: UploadFile | None = File(default=None),  # noqa: B008 - FastAPI idiom
        logs: str = Form(default=""),
        namespace: str = Form(default=""),
        context: str = Form(default=""),
        backend: str = Form(default="gemini"),
    ) -> Response:
        """Run OBSERVE → DIAGNOSE and return the structured result as JSON."""
        backend_name = (backend or "gemini").strip().lower()
        if backend_name not in _BACKEND_NAMES:
            return JSONResponse(
                {"error": f"Unknown backend {backend!r}. Choose 'gemini' or 'openai-compat'."},
                status_code=400,
            )
        if len(logs) > _MAX_LOGS_CHARS or len(namespace) > 512 or len(context) > 8000:
            return JSONResponse({"error": "A text field exceeds its size limit."}, status_code=400)
        image_bytes: bytes | None = None
        suffix = ""
        if image is not None and image.filename:
            suffix = Path(image.filename).suffix.lower()
            if suffix not in _IMAGE_SUFFIXES:
                return JSONResponse(
                    {
                        "error": f"Unsupported image type {suffix!r}. "
                        f"Use one of: {', '.join(sorted(_IMAGE_SUFFIXES))}."
                    },
                    status_code=400,
                )
            image_bytes = await image.read()
            if len(image_bytes) > _MAX_IMAGE_BYTES:
                return JSONResponse({"error": "Screenshot exceeds 15 MB."}, status_code=400)
            if not image_bytes:
                return JSONResponse({"error": "Uploaded screenshot is empty."}, status_code=400)
        if image_bytes is None and not logs.strip():
            return JSONResponse(
                {"error": "Upload a screenshot, or paste logs, to diagnose."},
                status_code=400,
            )
        try:
            model_backend = build_backend(backend_name)
        except BackendConfigError as exc:
            return JSONResponse({"error": f"Backend not configured: {exc}"}, status_code=400)
        except BackendError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        tmp_path: Path | None = None
        try:
            image_arg: Path | None = None
            if image_bytes is not None:
                with tempfile.NamedTemporaryFile(suffix=suffix or ".png", delete=False) as handle:
                    handle.write(image_bytes)
                tmp_path = Path(handle.name)
                image_arg = tmp_path
            result = run(
                image_arg,
                model_backend,
                logs_text=logs or None,
                namespace=namespace,
                context=context,
            )
        except InputError as exc:
            return JSONResponse({"error": f"Bad input: {exc}"}, status_code=400)
        except ModelOutputError as exc:
            return JSONResponse(
                {"error": f"The model's output could not be repaired: {exc}"},
                status_code=500,
            )
        except BackendConfigError as exc:
            return JSONResponse({"error": f"Backend not configured: {exc}"}, status_code=400)
        except BackendError as exc:
            logger.warning("backend failure: %s", exc)
            return JSONResponse(
                {"error": "The model backend failed. Retry shortly."},
                status_code=502,
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return JSONResponse(result.to_dict())

    @api.get("/", response_class=Response)
    def landing() -> Response:
        """Serve the public landing page."""
        data, content_type = _read_static("index.html")
        return Response(content=data, media_type=content_type)

    @api.get("/workspace", response_class=Response)
    def workspace() -> Response:
        """Serve the developer console workspace."""
        data = (WORKSPACE_DIR / "index.html").read_bytes()
        return Response(content=data, media_type="text/html; charset=utf-8")

    @api.get("/{name}", response_class=Response)
    def static_asset(name: str) -> Response:
        """Serve landing/console CSS/JS/favicon; anything else is a 404."""
        try:
            data, content_type = _read_static(name)
        except KeyError:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return Response(content=data, media_type=content_type)

    return api


def _port() -> int:
    """Return the port to listen on, honouring ``PORT`` for hosted platforms."""
    try:
        return int(os.environ.get("PORT", "7860"))
    except ValueError:
        return 7860


def main() -> None:
    """Launch the unified server."""
    import uvicorn

    logging.basicConfig(level=logging.WARNING)
    uvicorn.run(build_web_app(), host="0.0.0.0", port=_port(), log_level="warning")  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    main()
