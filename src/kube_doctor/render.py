"""Renderers: JSON for machines, Markdown for humans.

Markdown is the default human format: summary, ranked causes with their
evidence, a checklist of next steps with read-only first, and what is missing.
"""

from __future__ import annotations

import json
from typing import Any

from kube_doctor.schema import Diagnosis, Risk

#: Marker for read-only steps in the markdown checklist.
CHECKBOX_DONE: str = "- [x]"
#: Marker for mutating steps.
CHECKBOX_TODO: str = "- [ ]"
#: Label used for steps whose command was withheld by the safety lint.
WITHHELD_LABEL: str = "command withheld by the safety lint"


def to_json(result: Any) -> str:
    """Serialise a pipeline result (or a bare diagnosis) as pretty JSON."""
    if hasattr(result, "to_dict"):
        payload: Any = result.to_dict()
    elif isinstance(result, Diagnosis):
        payload = result.model_dump(mode="json")
    else:
        payload = result
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)


def _escape(text: str) -> str:
    """Make a string safe to drop into a markdown list item."""
    return text.replace("\n", " ").strip()


def _confidence_marker(confidence: str) -> str:
    """Render confidence as a short uppercase tag."""
    return confidence.upper()


def diagnosis_to_markdown(diagnosis: Diagnosis, *, title: str = "Kubernetes diagnosis") -> str:
    """Render a validated diagnosis as Markdown."""
    lines: list[str] = [f"# {title}", ""]
    if diagnosis.model_id:
        lines.append(f"*Model: `{diagnosis.model_id}`*")
        lines.append("")
    lines += ["## Summary", "", diagnosis.summary.strip(), ""]

    lines += ["## Ranked causes", ""]
    for index, cause in enumerate(diagnosis.causes, start=1):
        lines.append(
            f"{index}. **{cause.title.strip()}** "
            f"— `{cause.category.value}` ({_confidence_marker(cause.confidence.value)} confidence)"
        )
        for item in cause.evidence:
            lines.append(f"   - evidence: {_escape(item)}")
    lines.append("")

    lines += ["## Next steps", ""]
    # Sort defensively: the pipeline already orders these, but the renderer is
    # the last place that can guarantee the read-first reading order.
    steps = sorted(diagnosis.next_steps, key=lambda item: 0 if item.risk is Risk.READ_ONLY else 1)
    if not steps:
        lines.append("_No steps suggested._")
    for step in steps:
        marker = CHECKBOX_DONE if step.risk is Risk.READ_ONLY else CHECKBOX_TODO
        label = "read-only" if step.risk is Risk.READ_ONLY else "MUTATING"
        lines.append(f"{marker} **{_escape(step.description)}** ({label})")
        if step.command:
            lines.append(f"     - `$ {step.command.strip()}`")
        elif WITHHELD_LABEL in step.rationale:
            lines.append(f"     - _{WITHHELD_LABEL}_")
        lines.append(f"     - why: {_escape(step.rationale)}")
    lines.append("")

    lines += ["## Missing info", ""]
    if diagnosis.missing_info:
        lines += [f"- {_escape(item)}" for item in diagnosis.missing_info]
    else:
        lines.append("_Nothing material; the evidence above was sufficient._")
    lines.append("")

    lines += [
        "---",
        "",
        "_Kube Snapshot Doctor only reads. It never connects to a cluster and never runs these "
        "commands. Review anything marked MUTATING before you run it._",
        "",
    ]
    return "\n".join(lines)


def result_to_markdown(result: Any) -> str:
    """Render a whole pipeline result, including redaction and stage metadata."""
    diagnosis: Diagnosis = result.diagnosis
    parts = [diagnosis_to_markdown(diagnosis)]
    footer: list[str] = []
    redactions = getattr(result, "redactions", 0)
    if redactions:
        footer.append(f"- Secrets redacted before the model call: {redactions}")
    stages = getattr(result, "stages_run", [])
    if stages:
        footer.append(f"- Stages: {', '.join(stages)}")
    if getattr(result, "repaired", False):
        footer.append("- The model's first reply was invalid and was repaired once.")
    if footer:
        parts.append("\n".join(footer))
    return "\n".join(parts)


def render(result: Any, output_format: str) -> str:
    """Render a pipeline result in the requested format."""
    if output_format == "json":
        return to_json(result)
    return result_to_markdown(result)
