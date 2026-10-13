"""CLI tests: exit codes, formats, stdin, and error paths."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kube_doctor import cli
from kube_doctor.backends.base import Backend, BackendConfigError, BackendError
from kube_doctor.schema import Category, Cause, Confidence, Diagnosis, Risk, Step

runner = CliRunner()

OBSERVATIONS_OK = {
    "image_legible": True,
    "items": [{"kind": "pod_status", "text": "api-0 OOMKilled 12 9m", "source": "image"}],
    "notes": "",
}

DIAGNOSIS_OK = {
    "summary": "The api container was OOM killed because its memory limit is too low.",
    "causes": [
        {
            "category": "oom_killed",
            "title": "Memory limit below working set",
            "confidence": "high",
            "evidence": ["Reason: OOMKilled", "Exit Code: 137"],
        }
    ],
    "next_steps": [
        {
            "description": "Confirm the limit and the last state",
            "command": "kubectl describe pod api-0",
            "risk": "read_only",
            "rationale": "describe shows limits and the last termination reason.",
        }
    ],
    "missing_info": ["node memory pressure events"],
    "model_id": "",
}


class StubBackend(Backend):
    """Backend that replays a fixed reply sequence."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "stub-model"

    def generate(
        self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
    ) -> str:
        self.calls += 1
        if not self.replies:
            msg = "stub exhausted"
            raise BackendError(msg)
        return self.replies.pop(0)


@pytest.fixture
def stub_backend(monkeypatch: pytest.MonkeyPatch) -> StubBackend:
    """Install a stub backend into the CLI and return it."""
    backend = StubBackend([json.dumps(OBSERVATIONS_OK), json.dumps(DIAGNOSIS_OK)])

    def _factory(*_args: Any, **_kwargs: Any) -> Backend:
        return backend

    monkeypatch.setattr(cli, "build_backend", _factory)
    return backend


@pytest.fixture
def image_file(tmp_path: Path, sample_png: bytes) -> Path:
    """Write a real PNG to disk."""
    path = tmp_path / "shot.png"
    path.write_bytes(sample_png)
    return path


# --------------------------------------------------------------------------- #
# success paths
# --------------------------------------------------------------------------- #


def test_markdown_is_the_default_format(stub_backend: StubBackend, image_file: Path) -> None:
    """Markdown output contains the summary, causes, steps, and missing info."""
    result = runner.invoke(cli.app, [str(image_file)])
    assert result.exit_code == cli.EXIT_OK
    output = result.stdout
    assert "Kubernetes diagnosis" in output
    assert "Memory limit below working set" in output
    assert "oom_killed" in output
    assert "kubectl describe pod api-0" in output
    assert "node memory pressure events" in output
    assert "Evidence" in output or "evidence" in output


def test_json_format_is_machine_readable(stub_backend: StubBackend, image_file: Path) -> None:
    """``--format json`` emits the whole run as JSON."""
    result = runner.invoke(cli.app, [str(image_file), "--format", "json"])
    assert result.exit_code == cli.EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["diagnosis"]["causes"][0]["category"] == "oom_killed"
    assert payload["model_id"] == "stub-model"
    assert payload["observations"]["image_legible"] is True


def test_namespace_and_context_options(monkeypatch: pytest.MonkeyPatch, image_file: Path) -> None:
    """Namespace and context are forwarded to the model call."""
    seen: list[str] = []

    class _Capture(Backend):
        @property
        def model_id(self) -> str:
            return "capture"

        def generate(
            self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
        ) -> str:
            seen.append(prompt)
            return json.dumps(OBSERVATIONS_OK if image_bytes else DIAGNOSIS_OK)

    monkeypatch.setattr(cli, "build_backend", lambda *a, **k: _Capture())
    result = runner.invoke(
        cli.app,
        [
            str(image_file),
            "--namespace",
            "payments",
            "--context",
            "rolled back an hour ago",
        ],
    )
    assert result.exit_code == cli.EXIT_OK
    assert "namespace: payments" in seen[1]
    assert "rolled back an hour ago" in seen[1]


def test_image_from_stdin(stub_backend: StubBackend, sample_png: bytes) -> None:
    """``-`` reads the image bytes from stdin."""
    result = runner.invoke(cli.app, ["-"], input=sample_png)
    assert result.exit_code == cli.EXIT_OK, result.stdout
    assert "Kubernetes diagnosis" in result.stdout


def test_logs_are_redacted_before_the_model_call(
    monkeypatch: pytest.MonkeyPatch, image_file: Path
) -> None:
    """Secrets in logs are masked and the user is told how many."""
    seen: list[str] = []

    class _Capture(Backend):
        @property
        def model_id(self) -> str:
            return "capture"

        def generate(
            self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
        ) -> str:
            seen.append(prompt)
            return json.dumps(OBSERVATIONS_OK if image_bytes else DIAGNOSIS_OK)

    monkeypatch.setattr(cli, "build_backend", lambda *a, **k: _Capture())
    log_file = image_file.parent / "pod.log"
    log_file.write_text("Authorization: Bearer abc123def456ghi789", encoding="utf-8")
    result = runner.invoke(cli.app, [str(image_file), "--logs", str(log_file)])
    assert result.exit_code == cli.EXIT_OK
    assert "abc123def456ghi789" not in "".join(seen)
    assert "redacted" in result.output.lower()


def test_verbose_logs_observations(monkeypatch: pytest.MonkeyPatch, image_file: Path) -> None:
    """``--verbose`` surfaces the intermediate observations on stderr."""
    monkeypatch.setattr(
        cli,
        "build_backend",
        lambda *a, **k: StubBackend([json.dumps(OBSERVATIONS_OK), json.dumps(DIAGNOSIS_OK)]),
    )
    result = runner.invoke(cli.app, [str(image_file), "--verbose"])
    assert result.exit_code == cli.EXIT_OK
    assert "OOMKilled" in result.output


# --------------------------------------------------------------------------- #
# error paths
# --------------------------------------------------------------------------- #


def test_missing_image_exits_two(tmp_path: Path) -> None:
    """A missing image file is bad input."""
    result = runner.invoke(cli.app, [str(tmp_path / "nope.png")])
    assert result.exit_code == cli.EXIT_BAD_INPUT
    assert "not found" in result.output


def test_missing_logs_exits_two(image_file: Path, stub_backend: StubBackend) -> None:
    """A missing log file is bad input."""
    result = runner.invoke(cli.app, [str(image_file), "--logs", "nope.log"])
    assert result.exit_code == cli.EXIT_BAD_INPUT
    assert "log file not found" in result.output


def test_bad_format_exits_two(image_file: Path) -> None:
    """An unknown output format is bad input."""
    result = runner.invoke(cli.app, [str(image_file), "--format", "yaml"])
    assert result.exit_code == cli.EXIT_BAD_INPUT


def test_backend_config_error_exits_three(
    monkeypatch: pytest.MonkeyPatch, image_file: Path
) -> None:
    """A missing API key is a backend error, not bad input."""

    def _boom(*_args: Any, **_kwargs: Any) -> Backend:
        msg = "GEMINI_API_KEY is not set"
        raise BackendConfigError(msg)

    monkeypatch.setattr(cli, "build_backend", _boom)
    result = runner.invoke(cli.app, [str(image_file)])
    assert result.exit_code == cli.EXIT_BACKEND_ERROR
    assert "backend error" in result.output


def test_backend_runtime_error_exits_three(
    monkeypatch: pytest.MonkeyPatch, image_file: Path
) -> None:
    """A runtime failure inside the pipeline is still a backend error."""

    class _Exploding(Backend):
        @property
        def model_id(self) -> str:
            return "exploding"

        def generate(
            self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
        ) -> str:
            msg = "rate limit exceeded after retries"
            raise BackendError(msg)

    monkeypatch.setattr(cli, "build_backend", lambda *a, **k: _Exploding())
    result = runner.invoke(cli.app, [str(image_file)])
    assert result.exit_code == cli.EXIT_BACKEND_ERROR


def test_unrepairable_output_exits_four(monkeypatch: pytest.MonkeyPatch, image_file: Path) -> None:
    """Two invalid replies end with exit code 4."""
    monkeypatch.setattr(cli, "build_backend", lambda *a, **k: StubBackend(["nope", "still nope"]))
    result = runner.invoke(cli.app, [str(image_file)])
    assert result.exit_code == cli.EXIT_INVALID_MODEL_OUTPUT
    assert "could not be repaired" in result.output


def test_unknown_backend_name_exits_three(image_file: Path) -> None:
    """An unknown backend name is a backend error."""
    result = runner.invoke(cli.app, [str(image_file), "--backend", "telepathy"])
    assert result.exit_code == cli.EXIT_BACKEND_ERROR
    assert "unknown backend" in result.output


def test_list_models_without_key_exits_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--list-models`` without a key is a backend error."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["-", "--list-models"])
    assert result.exit_code == cli.EXIT_BACKEND_ERROR


def test_help_is_available() -> None:
    """``--help`` works without credentials."""
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "IMAGE" in result.stdout


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #


def test_renderer_orders_read_only_before_mutating() -> None:
    """Markdown puts read-only steps first regardless of model order."""
    from kube_doctor.render import diagnosis_to_markdown

    diagnosis = Diagnosis(
        summary="Two things to do.",
        causes=[
            Cause(
                category=Category.OOM_KILLED, title="t", confidence=Confidence.HIGH, evidence=["e"]
            )
        ],
        next_steps=[
            Step(
                description="Restart it",
                command="kubectl delete pod api-0",
                risk=Risk.MUTATING,
                rationale="r",
            ),
            Step(
                description="Look at it",
                command="kubectl describe pod api-0",
                risk=Risk.READ_ONLY,
                rationale="r",
            ),
        ],
    )
    markdown = diagnosis_to_markdown(diagnosis)
    assert markdown.index("Look at it") < markdown.index("Restart it")
    assert "MUTATING" in markdown
