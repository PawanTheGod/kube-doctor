"""Enterprise result panels for the Gradio UI.

Pure functions over :class:`kube_doctor.pipeline.PipelineResult`. Every
string rendered here comes from the real diagnostic response — category,
confidence, evidence, steps, risk labels, missing info, observations, and
run metadata. Nothing is invented: an ``unknown`` top cause renders an
insufficient-evidence state, never a fabricated root cause.

No Gradio imports in this module, so all of it is unit-testable offline.
"""

from __future__ import annotations

import html
from typing import Any

from kube_doctor.pipeline import PipelineResult
from kube_doctor.render import WITHHELD_LABEL
from kube_doctor.schema import Category, Risk

#: Copy-paste header for the suggested-commands block. States plainly that
#: nothing was executed; the pipeline never runs commands.
_COMMANDS_PREAMBLE: tuple[str, ...] = (
    "# Suggested commands — review before running.",
    "# Nothing here was executed.",
)


def _one_line(text: str) -> str:
    """Collapse a model string to a single line for list rendering."""
    return " ".join(text.split())


def is_insufficient(result: PipelineResult) -> bool:
    """Report whether the run declined to diagnose for lack of evidence."""
    causes = result.diagnosis.causes
    return bool(causes) and causes[0].category is Category.UNKNOWN


def _banner(variant: str, eyebrow: str, headline: str, sub: str, meta: str) -> str:
    """Wrap banner content in the ``ksd-banner`` variant container."""
    return (
        f'<div class="ksd-banner ksd-banner--{variant}">'
        f'<div class="ksd-banner-eyebrow">{html.escape(eyebrow)}</div>'
        f'<div class="ksd-banner-title">{html.escape(headline)}</div>'
        f'<div class="ksd-banner-sub">{html.escape(sub)}</div>'
        f'<div class="ksd-banner-meta">{html.escape(meta)}</div></div>'
    )


def banner_html(result: PipelineResult) -> str:
    """Render the primary-finding status banner for a completed run."""
    top = result.diagnosis.causes[0]
    meta = f"Model {result.model_id or 'unknown'} · {result.backend or 'backend'}"
    if is_insufficient(result):
        return _banner(
            "warn",
            "INSUFFICIENT EVIDENCE",
            "No reliable diagnosis possible",
            result.diagnosis.summary.strip(),
            meta,
        )
    return _banner(
        "ok",
        f"TOP FINDING · {top.category.value} · {top.confidence.value.upper()} CONFIDENCE",
        top.title.strip(),
        result.diagnosis.summary.strip(),
        meta,
    )


def error_banner_html(message: str) -> str:
    """Render the failure banner for runs that produced no diagnosis."""
    return _banner("error", "RUN FAILED", "Diagnosis failed", message.strip(), "")


def body_markdown(result: PipelineResult) -> str:
    """Render the structured diagnosis body: explanation, causes, steps."""
    diagnosis = result.diagnosis
    lines: list[str] = ["### Explanation", "", diagnosis.summary.strip(), ""]
    lines += ["### Ranked causes", ""]
    for index, cause in enumerate(diagnosis.causes, start=1):
        lines.append(
            f"{index}. **{_one_line(cause.title)}** — `{cause.category.value}` "
            f"({cause.confidence.value.upper()} confidence)"
        )
        for item in cause.evidence:
            lines.append(f"   - evidence: {_one_line(item)}")
    lines.append("")
    lines += ["### Recommended next steps", ""]
    steps = sorted(diagnosis.next_steps, key=lambda item: 0 if item.risk is Risk.READ_ONLY else 1)
    if not steps:
        lines.append("_No steps suggested._")
    for step in steps:
        if step.risk is Risk.READ_ONLY:
            lines.append(f"- [x] **{_one_line(step.description)}** (`READ-ONLY`)")
        else:
            lines.append(
                f"- [ ] **{_one_line(step.description)}** (`MUTATING — review before running`)"
            )
        if step.command:
            lines.append("  ```text")
            lines.append(f"  {step.command.strip()}")
            lines.append("  ```")
        elif WITHHELD_LABEL in step.rationale:
            lines.append(f"  - _{WITHHELD_LABEL}_")
        lines.append(f"  - why: {_one_line(step.rationale)}")
    lines.append("")
    lines += ["### Missing info — what would raise confidence", ""]
    if diagnosis.missing_info:
        lines += [f"- {_one_line(item)}" for item in diagnosis.missing_info]
    else:
        lines.append("_Nothing material; the evidence above was sufficient._")
    if result.redactions:
        lines += [
            "",
            f"_{result.redactions} secret value(s) redacted from the logs before the model call._",
        ]
    lines.append("")
    return "\n".join(lines)


def commands_block(result: PipelineResult) -> str:
    """Return copyable suggested commands, or ``""`` when there are none."""
    commands = [
        step.command.strip()
        for step in result.diagnosis.next_steps
        if step.command and step.command.strip()
    ]
    if not commands:
        return ""
    return "\n".join([*_COMMANDS_PREAMBLE, *commands])


def observations_markdown(result: PipelineResult) -> str:
    """Render the raw Stage 1 observations for the technical accordion."""
    observations = result.observations
    if observations is None:
        return "_No observations — the run ended before transcription._"
    if not observations.items:
        return "_(No legible observations.)_"
    lines = [f"- [{item.kind.value}] {_one_line(item.text)}" for item in observations.items]
    if observations.notes.strip():
        lines.append(f"- [notes] {_one_line(observations.notes)}")
    return "\n".join(lines)


def metadata_dict(result: PipelineResult) -> dict[str, Any]:
    """Return run metadata for the technical accordion JSON view."""
    return {
        "model": result.model_id,
        "primary_model": result.primary_model_id,
        "backend": result.backend,
        "stages": result.stages_run,
        "redactions": result.redactions,
        "redaction_summary": result.redaction_summary,
        "repaired": result.repaired,
        "used_fallback": result.used_fallback,
        "notes": result.notes,
    }
