"""Pydantic v2 schemas for observations, causes, steps, and diagnoses.

The models are the contract between the model, the pipeline, the safety lint,
and the renderers. Keep them strict: a diagnosis that does not validate is
repaired, not silently accepted.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Command fragments that always indicate a state-changing invocation.
MUTATING_FRAGMENTS: Final[tuple[str, ...]] = (
    "delete",
    "apply",
    "patch",
    "scale",
    "edit",
    "rollout restart",
    "drain",
    "cordon",
    "kubectl exec",
)

_MUTATING_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\b|\s)(?:" + "|".join(re.escape(fragment) for fragment in MUTATING_FRAGMENTS) + r")\b",
    re.IGNORECASE,
)


class Category(str, Enum):
    """Failure-mode taxonomy. ``unknown`` is the honest default."""

    OOM_KILLED = "oom_killed"
    CRASH_LOOP_APP_ERROR = "crash_loop_app_error"
    IMAGE_PULL = "image_pull"
    UNSCHEDULABLE = "unschedulable"
    CONFIG_ERROR = "config_error"
    PROBE_FAILURE = "probe_failure"
    RESOURCE_THROTTLING = "resource_throttling"
    NETWORK_DNS = "network_dns"
    UNKNOWN = "unknown"


class ObservationKind(str, Enum):
    """What sort of thing an observation describes."""

    POD_STATUS = "pod_status"
    EVENT = "event"
    LOG_LINE = "log_line"
    METRIC = "metric"
    COMMAND = "command"
    OTHER = "other"


class Source(str, Enum):
    """Which input an observation came from."""

    IMAGE = "image"
    LOGS = "logs"


class Confidence(str, Enum):
    """Self-reported confidence, capped by evidence quality in the prompt."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Risk(str, Enum):
    """Whether a suggested step can change cluster state."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"


class _Strict(BaseModel):
    """Base model: reject unknown keys so typos surface as validation errors."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Observation(_Strict):
    """One literally visible fact extracted from an input."""

    kind: ObservationKind
    text: str = Field(min_length=1)
    source: Source


class Observations(_Strict):
    """Stage 1 output: what the screenshot shows, without interpretation."""

    image_legible: bool
    items: list[Observation] = Field(default_factory=list)
    notes: str = ""


class Cause(_Strict):
    """A ranked candidate cause, always backed by evidence."""

    category: Category
    title: str = Field(min_length=1)
    confidence: Confidence
    evidence: list[str] = Field(min_length=1)

    @field_validator("evidence")
    @classmethod
    def _no_blank_evidence(cls, value: list[str]) -> list[str]:
        """Drop empty evidence strings; at least one must survive."""
        kept = [item for item in value if item.strip()]
        if not kept:
            msg = "evidence must contain at least one non-empty citation"
            raise ValueError(msg)
        return kept


class Step(_Strict):
    """A next step, optionally with a command the operator can copy."""

    description: str = Field(min_length=1)
    command: str | None = None
    risk: Risk
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _enforce_risk(self) -> Step:
        """Force ``risk=mutating`` for state-changing commands.

        The model cannot downgrade a mutating command to ``read_only``.
        """
        if self.command and self._looks_mutating(self.command) and self.risk is Risk.READ_ONLY:
            msg = (
                f"command looks state-changing but risk is read_only: {self.command.strip()!r}. "
                "Mark it as mutating."
            )
            raise ValueError(msg)
        return self

    @staticmethod
    def _looks_mutating(command: str) -> bool:
        """Report whether a command contains a known mutating fragment."""
        # Collapse whitespace first so "rollout   restart" matches the same way
        # "rollout restart" does.
        return bool(_MUTATING_RE.search(" ".join(command.split())))


class Diagnosis(_Strict):
    """The final, validated Stage 2 output."""

    summary: str = Field(min_length=1)
    causes: list[Cause] = Field(min_length=1, max_length=3)
    next_steps: list[Step] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    model_id: str = ""

    @field_validator("missing_info")
    @classmethod
    def _drop_blank_missing_info(cls, value: list[str]) -> list[str]:
        """Remove empty strings from ``missing_info``."""
        return [item for item in value if item.strip()]

    def ranked_categories(self) -> list[Category]:
        """Return cause categories in the order the model ranked them."""
        return [cause.category for cause in self.causes]


def unknown_diagnosis(reason: str, model_id: str = "") -> Diagnosis:
    """Build the honest fallback used when evidence is insufficient."""
    return Diagnosis(
        summary="Not enough legible evidence in the inputs to name a cause.",
        causes=[
            Cause(
                category=Category.UNKNOWN,
                title="Insufficient or illegible input",
                confidence=Confidence.LOW,
                evidence=[reason],
            )
        ],
        next_steps=[
            Step(
                description=(
                    "Capture the resource status as text, "
                    "or re-shoot the screenshot at full resolution."
                ),
                command="kubectl get pods -o wide",
                risk=Risk.READ_ONLY,
                rationale="Text output is easier to read than a screenshot and avoids OCR errors.",
            )
        ],
        missing_info=[reason],
        model_id=model_id,
    )


def coerce_json_object(raw: str) -> dict[str, Any]:
    """Extract the first JSON object from arbitrary model output.

    Handles bare JSON, fenced code blocks, and prose around the object.
    """
    from kube_doctor.jsonparse import extract_first_object

    return extract_first_object(raw)
