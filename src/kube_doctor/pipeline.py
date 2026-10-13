"""The two-stage diagnosis pipeline: OBSERVE, then DIAGNOSE.

Stage separation is the core design choice. Stage 1 is told to transcribe only
what is visible; stage 2 is told to reason only from that transcription. That
keeps the interpretation prompt free of pixels, which is where a vision model
invents things, and it makes each stage independently testable.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Final

from kube_doctor.backends.base import Backend
from kube_doctor.jsonparse import extract_first_object
from kube_doctor.redact import RedactionResult, redact, summarise
from kube_doctor.safety import lint_diagnosis, normalize_payload
from kube_doctor.schema import Diagnosis, Observations, unknown_diagnosis

logger = logging.getLogger(__name__)

#: Image types we accept, mapped to the mime type sent to the backend.
MIME_BY_SUFFIX: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
#: Default mime type when the file name says nothing useful.
DEFAULT_MIME: Final[str] = "image/png"
#: Pillow format name to mime type, for files without a useful suffix.
PILLOW_MIME: Final[dict[str, str]] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}
#: How many log characters to send; more just burns context and tokens.
MAX_LOG_CHARS: Final[int] = 24_000
#: How much of the failure-mode reference to inject.
MAX_REFERENCE_CHARS: Final[int] = 12_000


class PipelineError(Exception):
    """Base class for pipeline failures."""


class InputError(PipelineError):
    """The image or log file could not be read."""


class ModelOutputError(PipelineError):
    """The model produced output that did not validate, even after repair."""


def load_prompt(name: str) -> str:
    """Load a prompt template from the packaged ``prompts`` directory."""
    return resources.files("kube_doctor").joinpath(f"prompts/{name}").read_text(encoding="utf-8")


def load_reference() -> str:
    """Load the failure-mode reference injected into the diagnose prompt.

    The canonical file is the one shipped with the Agent Skill; packaging maps
    it into the wheel so the CLI and the skill never drift apart.
    """
    try:
        text = (
            resources.files("kube_doctor")
            .joinpath("references/failure-modes.md")
            .read_text(encoding="utf-8")
        )
    except (
        FileNotFoundError,
        ModuleNotFoundError,
        OSError,
    ):  # pragma: no cover - packaging fallback
        text = ""
    if not text.strip():
        repo_copy = (
            Path(__file__).resolve().parents[2]
            / "skills"
            / "kube-snapshot-doctor"
            / "references"
            / "failure-modes.md"
        )
        if repo_copy.exists():
            text = repo_copy.read_text(encoding="utf-8")
    if not text.strip():  # pragma: no cover - only when the reference is missing entirely
        logger.warning("failure-mode reference not found; diagnosing without it")
        return "(no failure-mode reference available)"
    return text[:MAX_REFERENCE_CHARS]


@dataclass
class PipelineResult:
    """Everything the renderers and the eval harness need."""

    diagnosis: Diagnosis
    observations: Observations | None = None
    redactions: int = 0
    redaction_summary: str = ""
    model_id: str = ""
    primary_model_id: str = ""
    used_fallback: bool = False
    backend: str = ""
    stages_run: list[str] = field(default_factory=list)
    repaired: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the whole run."""
        return {
            "diagnosis": self.diagnosis.model_dump(mode="json"),
            "observations": self.observations.model_dump(mode="json")
            if self.observations
            else None,
            "redactions": self.redactions,
            "redaction_summary": self.redaction_summary,
            "model_id": self.model_id,
            "primary_model_id": self.primary_model_id,
            "used_fallback": self.used_fallback,
            "backend": self.backend,
            "stages_run": self.stages_run,
            "repaired": self.repaired,
            "notes": self.notes,
        }


def detect_mime(path: Path, image_bytes: bytes) -> str:
    """Guess the image mime type from the suffix, falling back to Pillow."""
    suffix_mime = MIME_BY_SUFFIX.get(path.suffix.lower())
    if suffix_mime:
        return suffix_mime
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as probe:
            fmt = probe.format
        if fmt:
            return PILLOW_MIME.get(fmt, DEFAULT_MIME)
    except Exception:
        pass
    return DEFAULT_MIME


def read_image(image_path: str | Path) -> tuple[bytes, str]:
    """Read an image file, returning its bytes and mime type."""
    path = Path(image_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"cannot read image {path}: {exc}"
        raise InputError(msg) from exc
    if not data:
        msg = f"image {path} is empty"
        raise InputError(msg)
    return data, detect_mime(path, data)


def read_logs(logs_path: str | Path | None, logs_text: str | None) -> str:
    """Read log text from a path or from an already-loaded string."""
    if logs_text:
        return logs_text[:MAX_LOG_CHARS]
    if logs_path is None:
        return ""
    path = Path(logs_path)
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_LOG_CHARS]
    except OSError as exc:
        msg = f"cannot read logs {path}: {exc}"
        raise InputError(msg) from exc


def _format_observations(observations: Observations) -> str:
    """Render observations as compact, unambiguous text for the diagnose prompt."""
    if not observations.items:
        return "(no legible observations)"
    lines = [f"- [{item.kind}] {item.text}" for item in observations.items]
    rendered = "\n".join(lines)
    if observations.notes.strip():
        rendered = f"{rendered}\n- [notes] {observations.notes.strip()}"
    return rendered


def _build_context(context: str, namespace: str) -> str:
    """Combine the free-text context and namespace into one block."""
    parts: list[str] = []
    if namespace.strip():
        parts.append(f"namespace: {namespace.strip()}")
    if context.strip():
        parts.append(context.strip())
    return "\n".join(parts) if parts else "(no additional context supplied)"


def _render(template: str, **values: str) -> str:
    """Fill ``{{name}}`` placeholders in a prompt template."""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _validate_json_object(
    raw: str, model: type[Diagnosis] | type[Observations], *, normalize: bool
) -> Any:
    """Parse and validate one model reply, normalising risky steps first."""
    payload = extract_first_object(raw)
    if normalize:
        payload = normalize_payload(payload)
    return model.model_validate(payload)


def _short_error(exc: Exception) -> str:
    """Render a validation error compactly for the repair prompt."""
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
    return str(exc)[:2000]


def run(
    image_path: str | Path | None = None,
    backend: Backend | None = None,
    *,
    logs_path: str | Path | None = None,
    logs_text: str | None = None,
    context: str = "",
    namespace: str = "",
) -> PipelineResult:
    """Run OBSERVE then DIAGNOSE and return a validated, linted diagnosis.

    ``image_path`` may be ``None`` when logs are supplied on their own. At
    least one of image or logs is required; a run with neither cannot produce
    evidence and is rejected as bad input.
    """
    if backend is None:
        msg = "a backend is required"
        raise InputError(msg)
    if image_path is None and not logs_path and not logs_text:
        msg = "supply an image, logs, or both"
        raise InputError(msg)

    model_id = backend.model_id
    primary_model_id = getattr(backend, "primary_model_id", model_id)
    stages: list[str] = []
    repaired = False

    image_bytes: bytes | None = None
    mime: str | None = None
    if image_path is not None:
        image_bytes, mime = read_image(image_path)

    redaction_result: RedactionResult = redact(read_logs(logs_path, logs_text))
    logs_supplied = bool(redaction_result.text.strip())
    if redaction_result.count:
        logger.info("redacted secrets before model call: %s", summarise(redaction_result))

    observe_prompt = load_prompt("observe.md")
    if not image_bytes:
        observe_prompt = (
            "No image is available for this request. "
            "Return this exact JSON object and nothing else:\n"
            '{"image_legible": false, "items": [], '
            '"notes": "no image supplied; diagnose from the logs alone"}'
        )
    raw_observations = backend.generate(observe_prompt, image_bytes, mime)
    stages.append("observe")
    try:
        observations = _validate_json_object(raw_observations, Observations, normalize=False)
    except Exception as exc:
        logger.info("observe output invalid, attempting repair: %s", _short_error(exc))
        repair_prompt = _render(
            load_prompt("repair.md"),
            error=_short_error(exc),
            previous=raw_observations[:4000],
        )
        raw_observations = backend.generate(repair_prompt, image_bytes, mime)
        stages.append("observe-repair")
        try:
            observations = _validate_json_object(raw_observations, Observations, normalize=False)
        except Exception as second_exc:
            msg = f"observe stage produced unusable output: {_short_error(second_exc)}"
            raise ModelOutputError(msg) from second_exc
        repaired = True

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("observations: %s", observations.model_dump_json(indent=2))

    if not observations.image_legible and not logs_supplied:
        reason = "the screenshot is not legible and no logs were supplied"
        logger.info("%s", reason)
        return PipelineResult(
            diagnosis=unknown_diagnosis(reason, model_id=backend.model_id),
            observations=observations,
            redactions=redaction_result.count,
            redaction_summary=summarise(redaction_result),
            model_id=backend.model_id,
            primary_model_id=primary_model_id,
            used_fallback=bool(getattr(backend, "used_fallback", False)),
            backend=type(backend).__name__,
            stages_run=stages,
            repaired=repaired,
            notes=reason,
        )

    diagnose_prompt = _render(
        load_prompt("diagnose.md"),
        reference=load_reference(),
        observations=_format_observations(observations),
        logs=redaction_result.text.strip() or "(no logs supplied)",
        context=_build_context(context, namespace),
    )
    raw_diagnosis = backend.generate(diagnose_prompt, None, None)
    stages.append("diagnose")
    try:
        diagnosis = _validate_json_object(raw_diagnosis, Diagnosis, normalize=True)
    except Exception as exc:
        logger.info("diagnose output invalid, attempting repair: %s", _short_error(exc))
        repair_prompt = _render(
            load_prompt("repair.md"),
            error=_short_error(exc),
            previous=raw_diagnosis[:4000],
        )
        raw_diagnosis = backend.generate(repair_prompt, None, None)
        stages.append("diagnose-repair")
        try:
            diagnosis = _validate_json_object(raw_diagnosis, Diagnosis, normalize=True)
        except Exception as second_exc:
            msg = f"diagnose stage produced unusable output: {_short_error(second_exc)}"
            raise ModelOutputError(msg) from second_exc
        repaired = True

    # Re-read the model id: a backend that fell back reports the fallback here,
    # and the diagnosis must name the model that actually produced it.
    model_id = backend.model_id
    if getattr(backend, "used_fallback", False):
        logger.info("fallback model %s produced this diagnosis", model_id)

    diagnosis = diagnosis.model_copy(update={"model_id": diagnosis.model_id or model_id})
    diagnosis = lint_diagnosis(diagnosis)
    logger.debug("diagnosis: %s", diagnosis.model_dump_json(indent=2))

    return PipelineResult(
        diagnosis=diagnosis,
        observations=observations,
        redactions=redaction_result.count,
        redaction_summary=summarise(redaction_result),
        model_id=model_id,
        primary_model_id=primary_model_id,
        used_fallback=bool(getattr(backend, "used_fallback", False)),
        backend=type(backend).__name__,
        stages_run=stages,
        repaired=repaired,
    )
