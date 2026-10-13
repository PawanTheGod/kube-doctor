"""UI panel tests: structured rendering from real pipeline results.

All offline: ``FakeBackend`` replays canned replies, so no network, no
credentials, and no live model calls.
"""

from __future__ import annotations

import json

from tests.conftest import FakeBackend, png_bytes

from kube_doctor.pipeline import PipelineResult, run
from kube_doctor.render import WITHHELD_LABEL
from kube_doctor.schema import (
    Category,
    Cause,
    Confidence,
    Diagnosis,
    Risk,
    Step,
)
from kube_doctor.ui.panels import (
    banner_html,
    body_markdown,
    commands_block,
    error_banner_html,
    is_insufficient,
    metadata_dict,
    observations_markdown,
)

OBSERVE_OK = json.dumps(
    {
        "image_legible": True,
        "items": [
            {
                "kind": "pod_status",
                "text": "Last State: Terminated, Reason: OOMKilled, Exit Code: 137",
                "source": "image",
            }
        ],
        "notes": "",
    }
)

DIAGNOSE_OOM = json.dumps(
    {
        "summary": "The worker is OOM killed with exit code 137.",
        "causes": [
            {
                "category": "oom_killed",
                "title": "Memory limit below the working set",
                "confidence": "high",
                "evidence": ["Last State: Terminated, Reason: OOMKilled, Exit Code: 137"],
            }
        ],
        "next_steps": [
            {
                "description": "Confirm the limit and last state",
                "command": "kubectl describe pod batch-worker-abc",
                "risk": "read_only",
                "rationale": "Shows limits and last termination state.",
            },
            {
                "description": "Raise the memory limit",
                "command": "kubectl set resources deployment worker && echo done",
                "risk": "read_only",
                "rationale": "Needs an edit.",
            },
        ],
        "missing_info": ["live memory usage"],
        "model_id": "",
    }
)

DIAGNOSE_UNKNOWN = json.dumps(
    {
        "summary": "Nothing legible to diagnose from.",
        "causes": [
            {
                "category": "unknown",
                "title": "Insufficient diagnostic data",
                "confidence": "low",
                "evidence": ["(no legible observations)"],
            }
        ],
        "next_steps": [],
        "missing_info": ["a full-resolution screenshot"],
        "model_id": "",
    }
)


def _run_ok(tmp_path, diagnose_reply=DIAGNOSE_OOM) -> PipelineResult:
    shot = tmp_path / "shot.png"
    shot.write_bytes(png_bytes())
    return run(shot, FakeBackend([OBSERVE_OK, diagnose_reply]))


def test_banner_ok_names_category_and_confidence(tmp_path):
    banner = banner_html(_run_ok(tmp_path))
    assert "oom_killed" in banner
    assert "HIGH" in banner
    assert "Memory limit below the working set" in banner


def test_banner_escapes_model_html(tmp_path):
    result = _run_ok(tmp_path)
    result.diagnosis.causes[0].title = "<script>alert(1)</script>"
    banner = banner_html(result)
    assert "<script>" not in banner
    assert "&lt;script&gt;" in banner


def test_unknown_renders_insufficient_state(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(png_bytes())
    illegible = json.dumps({"image_legible": False, "items": [], "notes": "blurry"})
    result = run(shot, FakeBackend([illegible, DIAGNOSE_UNKNOWN]), logs_text="some log")
    assert is_insufficient(result)
    banner = banner_html(result)
    assert "No reliable diagnosis possible" in banner
    assert "Insufficient diagnostic data" not in banner  # title stays in the body
    body = body_markdown(result)
    assert "Insufficient diagnostic data" in body
    assert "a full-resolution screenshot" in body
    assert commands_block(result) == ""


def test_short_circuit_needs_no_logs_and_stays_unknown(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(png_bytes())
    illegible = json.dumps({"image_legible": False, "items": [], "notes": "blurry"})
    result = run(shot, FakeBackend([illegible]))
    assert is_insufficient(result)
    assert "No reliable diagnosis possible" in banner_html(result)


def test_withheld_command_excluded_from_copy_block(tmp_path):
    result = _run_ok(tmp_path)
    result.diagnosis.next_steps.append(
        Step(
            description="Delete the pod to force a reschedule",
            command="kubectl delete pod batch-worker-abc",
            risk=Risk.MUTATING,
            rationale="Recreates the pod.",
        )
    )
    commands = commands_block(result)
    assert "kubectl describe pod batch-worker-abc" in commands
    assert "kubectl delete pod batch-worker-abc" in commands
    assert "Nothing here was executed" in commands
    assert "&&" not in commands
    body = body_markdown(result)
    assert "(command withheld:" in body
    assert "MUTATING" in body
    assert "READ-ONLY" in body


def test_long_command_preserved_verbatim(tmp_path):
    result = _run_ok(tmp_path)
    long_command = "kubectl " + " ".join(f"--flag-{i}=value-{i}" for i in range(40))
    assert len(long_command) > 300
    result.diagnosis.next_steps[0].command = long_command
    assert long_command in commands_block(result)
    assert long_command in body_markdown(result)


def test_logs_without_screenshot_supported():
    observe_no_image = json.dumps(
        {
            "image_legible": False,
            "items": [],
            "notes": "no image supplied; diagnose from the logs alone",
        }
    )
    result = run(None, FakeBackend([observe_no_image, DIAGNOSE_OOM]), logs_text="OOMKilled 137")
    assert not is_insufficient(result)
    assert "oom_killed" in banner_html(result)


def test_metadata_and_observations_shapes(tmp_path):
    result = _run_ok(tmp_path)
    meta = metadata_dict(result)
    assert meta["model"] == "fake-model-v1"
    assert meta["backend"] == "FakeBackend"
    assert meta["stages"] == ["observe", "diagnose"]
    assert meta["repaired"] is False
    assert "OOMKilled" in observations_markdown(result)


def test_error_banner_names_failure():
    banner = error_banner_html("boom")
    assert "Diagnosis failed" in banner
    assert "boom" in banner


def test_observations_none_placeholder(tmp_path):
    result = _run_ok(tmp_path)
    result.observations = None
    assert "before transcription" in observations_markdown(result)


def test_withheld_label_branch_matches_cli_renderer():
    diagnosis = Diagnosis(
        summary="s",
        causes=[
            Cause(
                category=Category.UNKNOWN,
                title="t",
                confidence=Confidence.LOW,
                evidence=["e"],
            )
        ],
        next_steps=[
            Step(
                description="d",
                command=None,
                risk=Risk.READ_ONLY,
                rationale=f"advice kept {WITHHELD_LABEL}",
            )
        ],
        missing_info=[],
    )
    result = PipelineResult(diagnosis=diagnosis)
    assert WITHHELD_LABEL in body_markdown(result)
    assert commands_block(result) == ""
