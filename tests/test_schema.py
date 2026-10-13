"""Tests for the pydantic schemas and their validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kube_doctor.jsonparse import extract_first_object, strip_code_fences
from kube_doctor.schema import (
    Category,
    Cause,
    Confidence,
    Diagnosis,
    Observation,
    ObservationKind,
    Observations,
    Risk,
    Source,
    Step,
    unknown_diagnosis,
)

# --------------------------------------------------------------------------- #
# observations
# --------------------------------------------------------------------------- #


def test_observation_round_trip() -> None:
    """A minimal observation validates with the documented field set."""
    obs = Observation(
        kind=ObservationKind.POD_STATUS, text="nginx-0 Running 0/1", source=Source.IMAGE
    )
    assert obs.kind is ObservationKind.POD_STATUS
    assert obs.source is Source.IMAGE


def test_observation_rejects_unknown_kind() -> None:
    """An unknown ``kind`` string is a validation error, not a passthrough."""
    with pytest.raises(ValidationError):
        Observation(kind="wat", text="x", source="image")  # type: ignore[arg-type]


def test_observation_rejects_extra_keys() -> None:
    """Extra keys are rejected so model typos surface."""
    with pytest.raises(ValidationError):
        Observation(kind="event", text="x", source="logs", hint="y")  # type: ignore[call-arg]


def test_observation_rejects_blank_text() -> None:
    """Whitespace-only text is stripped and rejected as too short."""
    with pytest.raises(ValidationError):
        Observation(kind="event", text="   ", source="logs")


def test_observations_defaults() -> None:
    """Observations allows an empty item list and empty notes."""
    obs = Observations(image_legible=False)
    assert obs.items == []
    assert obs.notes == ""


# --------------------------------------------------------------------------- #
# cause
# --------------------------------------------------------------------------- #


def test_cause_requires_evidence() -> None:
    """A cause with no evidence items is rejected."""
    with pytest.raises(ValidationError):
        Cause(
            category=Category.OOM_KILLED,
            title="OOM",
            confidence=Confidence.HIGH,
            evidence=[],
        )


def test_cause_rejects_blank_only_evidence() -> None:
    """Evidence that is entirely blank is rejected after stripping."""
    with pytest.raises(ValidationError):
        Cause(
            category=Category.OOM_KILLED,
            title="OOM",
            confidence=Confidence.HIGH,
            evidence=["", "   "],
        )


def test_cause_drops_blank_evidence_entries() -> None:
    """Mixed evidence keeps only the non-empty citations."""
    cause = Cause(
        category=Category.IMAGE_PULL,
        title="bad tag",
        confidence=Confidence.MEDIUM,
        evidence=["ErrImagePull", "", "  "],
    )
    assert cause.evidence == ["ErrImagePull"]


def test_cause_rejects_invalid_category() -> None:
    """Categories outside the enum are rejected."""
    with pytest.raises(ValidationError):
        Cause(
            category="disk_full",  # type: ignore[arg-type]
            title="x",
            confidence=Confidence.LOW,
            evidence=["y"],
        )


# --------------------------------------------------------------------------- #
# step risk validator
# --------------------------------------------------------------------------- #

MUTATING_COMMANDS = [
    "kubectl delete pod nginx",
    "kubectl apply -f deploy.yaml",
    "kubectl patch deploy api -p {}",
    "kubectl scale deploy api --replicas=3",
    "kubectl edit deploy api",
    "kubectl rollout restart deploy api",
    "kubectl drain node-1",
    "kubectl cordon node-1",
    "kubectl exec -it api -- sh",
    "KUBECTL DELETE POD NGINX",
    "kubectl  delete   pod nginx",
]

READ_ONLY_COMMANDS = [
    "kubectl get pods",
    "kubectl describe pod api-0",
    "kubectl logs api-0 --previous",
    "kubectl get events --sort-by=.lastTimestamp",
    "helm list -A",
    "helm status api",
    "kubectl top pods",
    "cat /var/log/pods/api.log",
    "kubectl describe node node-1",
    "kubectl get configmap api-config -o name",
]


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_step_rejects_mutating_command_labeled_read_only(command: str) -> None:
    """Every known mutating fragment forces risk=mutating."""
    with pytest.raises(ValidationError, match="state-changing"):
        Step(description="d", command=command, risk=Risk.READ_ONLY, rationale="r")


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_step_allows_mutating_command_labeled_mutating(command: str) -> None:
    """The same commands validate when labelled correctly."""
    step = Step(description="d", command=command, risk=Risk.MUTATING, rationale="r")
    assert step.risk is Risk.MUTATING


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_step_allows_read_only_commands(command: str) -> None:
    """Read-only commands stay valid with risk=read_only."""
    step = Step(description="d", command=command, risk=Risk.READ_ONLY, rationale="r")
    assert step.risk is Risk.READ_ONLY


def test_step_command_is_optional() -> None:
    """A step without a command is valid."""
    step = Step(description="check quotas", risk=Risk.READ_ONLY, rationale="explains why")
    assert step.command is None


def test_step_looks_mutating_is_case_and_space_insensitive() -> None:
    """Detection normalises case and repeated whitespace."""
    assert Step._looks_mutating("KUBECTL   ROLLOUT   RESTART deploy")
    assert not Step._looks_mutating("kubectl get pods")


# --------------------------------------------------------------------------- #
# diagnosis
# --------------------------------------------------------------------------- #


def _cause(category: Category = Category.CRASH_LOOP_APP_ERROR) -> Cause:
    return Cause(category=category, title="t", confidence=Confidence.MEDIUM, evidence=["e"])


def test_diagnosis_rejects_more_than_three_causes() -> None:
    """At most three ranked causes are allowed."""
    with pytest.raises(ValidationError):
        Diagnosis(
            summary="s",
            causes=[_cause() for _ in range(4)],
        )


def test_diagnosis_rejects_empty_causes() -> None:
    """A diagnosis must name at least one cause."""
    with pytest.raises(ValidationError):
        Diagnosis(summary="s", causes=[])


def test_diagnosis_defaults() -> None:
    """Optional collections default to empty."""
    diag = Diagnosis(summary="s", causes=[_cause()])
    assert diag.next_steps == []
    assert diag.missing_info == []
    assert diag.model_id == ""


def test_diagnosis_drops_blank_missing_info() -> None:
    """Blank ``missing_info`` entries are removed."""
    diag = Diagnosis(summary="s", causes=[_cause()], missing_info=["a", "  ", "b"])
    assert diag.missing_info == ["a", "b"]


def test_diagnosis_ranked_categories_preserves_order() -> None:
    """Ranked categories reflect the model's ordering."""
    diag = Diagnosis(
        summary="s",
        causes=[
            _cause(Category.OOM_KILLED),
            _cause(Category.PROBE_FAILURE),
        ],
    )
    assert diag.ranked_categories() == [Category.OOM_KILLED, Category.PROBE_FAILURE]


def test_unknown_diagnosis_is_valid() -> None:
    """The fallback diagnosis validates and is category ``unknown``."""
    diag = unknown_diagnosis("image is too blurry to read")
    assert diag.causes[0].category is Category.UNKNOWN
    assert diag.missing_info == ["image is too blurry to read"]
    assert diag.causes[0].evidence == ["image is too blurry to read"]


# --------------------------------------------------------------------------- #
# json parsing
# --------------------------------------------------------------------------- #


def test_strip_code_fences() -> None:
    """A fenced block loses its fences."""
    assert strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


@pytest.mark.parametrize(
    "raw",
    [
        '{"image_legible": true, "items": []}',
        '```json\n{"image_legible": true, "items": []}\n```',
        'Here you go:\n{"image_legible": true, "items": []}\nHope that helps.',
    ],
)
def test_extract_first_object(raw: str) -> None:
    """Bare, fenced, and prose-wrapped objects all parse."""
    assert extract_first_object(raw) == {"image_legible": True, "items": []}


def test_extract_first_object_handles_nesting_and_braces_in_strings() -> None:
    """Nested objects and braces inside strings are handled."""
    raw = 'noise {"a": {"b": [1, 2]}, "text": "a } brace"} tail'
    assert extract_first_object(raw) == {"a": {"b": [1, 2]}, "text": "a } brace"}


def test_extract_first_object_skips_invalid_leading_brace() -> None:
    """A stray brace before the real object is skipped."""
    raw = '{not json at all} {"ok": true}'
    assert extract_first_object(raw) == {"ok": True}


def test_extract_first_object_raises_without_object() -> None:
    """No object at all is a ValueError the pipeline can repair."""
    with pytest.raises(ValueError, match="no JSON object"):
        extract_first_object("I am not JSON.")
