#!/usr/bin/env python3
"""Score the diagnosis pipeline over the committed fixtures.

Metrics per case:

* **json_valid** — the run produced a validated ``Diagnosis``.
* **category** — the top-ranked cause matches ``expected.category``.
* **top3_hit** — the expected category appears anywhere in the ranked causes.
* **keywords** — recall of the ``must_mention`` phrases in the diagnosis text.
* **no_hallucination** — every resource-shaped name in the output also appears
  in the inputs.
* **safety** — no command labelled ``read_only`` actually changes state.

Usage::

    python evals/run_evals.py --backend gemini
    python evals/run_evals.py --backend openai-compat --threshold 0.8
    python evals/run_evals.py --backend fake        # offline smoke test for CI

``--backend fake`` replays a scripted reply per fixture, so the harness, the
scoring, and the report are all exercised without credentials. It proves the
plumbing, not the model's accuracy; the numbers that say something about the
model come from a real run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

ROOT: Final[Path] = Path(__file__).resolve().parent
REPO: Final[Path] = ROOT.parent
sys.path.insert(0, str(REPO / "src"))

from kube_doctor.backends.base import Backend  # noqa: E402
from kube_doctor.backends.factory import build_backend  # noqa: E402
from kube_doctor.pipeline import PipelineError, PipelineResult, run  # noqa: E402
from kube_doctor.safety import audit_read_only_safety  # noqa: E402
from kube_doctor.schema import Category  # noqa: E402

FIXTURES: Final[Path] = ROOT / "fixtures"
#: Written after every run; committed for the README table.
RESULTS: Final[Path] = ROOT / "results.md"

#: Words that look like Kubernetes resource names, used by the hallucination check.
RESOURCE_NAME: Final[re.Pattern[str]] = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){1,}\b")
#: Names that are ordinary English or prompt text, not invented resources.
NAME_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "read-only",
        "next-step",
        "kube-doctor",
        "kube-system",
        "cluster-wide",
        "state-changing",
        "container-created",
        "well-known",
    }
)
#: Keywords whose presence is expected, checked case-insensitively.
KEYWORD_FIELDS: Final[tuple[str, ...]] = ("summary", "causes", "next_steps", "missing_info")


class ScriptedBackend(Backend):
    """Offline backend that replays a fixture-appropriate reply.

    It exists so CI can run the harness end to end. It is not a model and its
    numbers carry no information about model accuracy.
    """

    def __init__(self, fixtures: dict[str, dict[str, Any]]) -> None:
        self.fixtures = fixtures
        self.stage = 0
        self.case = ""

    @property
    def model_id(self) -> str:
        return "scripted-fixture-reply (offline; not a model)"

    @property
    def primary_model_id(self) -> str:
        return self.model_id

    @property
    def used_fallback(self) -> bool:
        return False

    def generate(
        self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
    ) -> str:
        if image_bytes is not None:
            self.stage = 1
            return json.dumps(self.fixtures[self.case]["observations"])
        return json.dumps(self.fixtures[self.case]["diagnosis"])


@dataclass
class CaseResult:
    """Scores for a single fixture."""

    name: str
    expected_category: str
    json_valid: bool = False
    category_correct: bool = False
    top3_hit: bool = False
    keyword_recall: float = 0.0
    hallucination_free: bool = True
    hallucinated: list[str] = field(default_factory=list)
    safety_ok: bool = True
    offences: list[str] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    error: str = ""
    #: Model that actually answered this case, which may not be the primary.
    model_id: str = ""
    #: Model the run was configured with, and therefore the only one whose
    #: numbers may be published.
    primary_model_id: str = ""
    used_fallback: bool = False

    @property
    def passed(self) -> bool:
        """Report whether the case met every gate."""
        return (
            self.json_valid
            and self.category_correct
            and self.hallucination_free
            and self.safety_ok
            and not self.forbidden_hits
        )


def load_fixture(directory: Path) -> dict[str, Any]:
    """Read one fixture directory into a dict."""
    expected = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    logs_path = directory / "logs.txt"
    return {
        "name": directory.name,
        "screenshot": directory / "screenshot.png",
        "logs": logs_path if logs_path.exists() and logs_path.stat().st_size else None,
        "expected": expected,
    }


def claim_text(result: PipelineResult) -> str:
    """Flatten only the parts of a diagnosis that make claims about the cluster.

    Cause titles, cause evidence, and suggested commands are where a model
    invents a resource name. Step descriptions and rationales are prose, and a
    hyphenated English phrase in one ("re-capture the screenshot") is not a
    hallucinated resource, so they are excluded from the check below.
    """
    diagnosis = result.diagnosis
    parts: list[str] = []
    for cause in diagnosis.causes:
        parts.append(cause.title)
        parts.extend(cause.evidence)
    for step in diagnosis.next_steps:
        if step.command:
            parts.append(step.command)
    return " ".join(parts)


def diagnosis_text(result: PipelineResult) -> str:
    """Flatten a whole diagnosis into one searchable blob."""
    diagnosis = result.diagnosis
    parts = [diagnosis.summary, claim_text(result), *diagnosis.missing_info]
    for step in diagnosis.next_steps:
        parts.extend([step.description, step.rationale])
    return " ".join(parts)


def input_text(fixture: dict[str, Any]) -> str:
    """Collect the text the model was allowed to see, plus the fixture metadata."""
    chunks = [fixture["name"]]
    if fixture["logs"]:
        chunks.append(fixture["logs"].read_text(encoding="utf-8"))
    expected = fixture["expected"]
    chunks.append(" ".join(expected.get("must_mention", [])))
    return " ".join(chunks).lower()


def invented_names(output: str, corpus: str) -> list[str]:
    """Return resource-shaped names in the output that are absent from the inputs."""
    found: set[str] = set()
    for match in RESOURCE_NAME.findall(output.lower()):
        if match in NAME_ALLOWLIST:
            continue
        if match not in corpus:
            found.add(match)
    return sorted(found)


def score_case(
    fixture: dict[str, Any], result: PipelineResult, primary_model_id: str = ""
) -> CaseResult:
    """Score one successful run against its expectations."""
    expected = fixture["expected"]
    expected_category = str(expected["category"])
    diagnosis = result.diagnosis
    ranked = [cause.category.value for cause in diagnosis.causes]

    text = diagnosis_text(result)
    lowered = text.lower()
    must_mention = [str(item) for item in expected.get("must_mention", [])]
    hits = [keyword for keyword in must_mention if keyword.lower() in lowered]
    recall = len(hits) / len(must_mention) if must_mention else 1.0

    corpus = input_text(fixture)
    invented = invented_names(claim_text(result), corpus)
    forbidden = [
        str(item) for item in expected.get("forbidden_claims", []) if item.lower() in lowered
    ]
    answered_by = result.model_id or primary_model_id

    return CaseResult(
        name=fixture["name"],
        expected_category=expected_category,
        json_valid=True,
        category_correct=bool(ranked) and ranked[0] == expected_category,
        top3_hit=expected_category in ranked,
        keyword_recall=recall,
        hallucination_free=not invented,
        hallucinated=invented,
        safety_ok=not audit_read_only_safety(diagnosis),
        offences=audit_read_only_safety(diagnosis),
        forbidden_hits=forbidden,
        model_id=answered_by,
        primary_model_id=primary_model_id or answered_by,
        used_fallback=bool(primary_model_id) and answered_by != primary_model_id,
    )


def format_table(
    results: list[CaseResult], threshold: float, backend_name: str, model_id: str
) -> str:
    """Render the results as a Markdown table plus a summary line."""
    accuracy = (
        sum(1 for result in results if result.category_correct) / len(results) if results else 0.0
    )
    lines = [
        "## Case results",
        "",
        f"- backend: `{backend_name}`",
        f"- primary model: `{model_id}`",
        f"- cases: {len(results)}",
        f"- top-1 category accuracy: **{accuracy:.0%}** (threshold {threshold:.0%})",
        "",
        "| Case | Expected | JSON | Top-1 | Top-3 | Keywords | No hallucination | Safety | Model |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            f"| `{result.name}` | `{result.expected_category}` | "
            f"{'ok' if result.json_valid else 'FAIL'} | "
            f"{'ok' if result.category_correct else 'miss'} | "
            f"{'ok' if result.top3_hit else 'miss'} | "
            f"{result.keyword_recall:.0%} | "
            f"{'ok' if result.hallucination_free else 'HALLUCINATED'} | "
            f"{'ok' if result.safety_ok else 'UNSAFE'} | "
            f"`{result.model_id or 'n/a'}`{' (FALLBACK)' if result.used_fallback else ''} |"
        )
    lines.append("")
    for result in results:
        if result.error:
            lines.append(f"- `{result.name}`: error: {result.error}")
        if result.used_fallback:
            lines.append(
                f"- `{result.name}`: answered by fallback model `{result.model_id}`, "
                f"not the primary `{result.primary_model_id}`"
            )
        for name in result.hallucinated:
            lines.append(f"- `{result.name}`: invented name `{name}`")
        for command in result.offences:
            lines.append(f"- `{result.name}`: unsafe command labelled read-only: `{command}`")
        for claim in result.forbidden_hits:
            lines.append(f"- `{result.name}`: forbidden claim `{claim}`")
    return "\n".join(lines)


def fallback_report(results: list[CaseResult]) -> str:
    """Return the banner shown when a run mixed in a fallback model."""
    affected = [result for result in results if result.used_fallback]
    if not affected:
        return ""
    names = ", ".join(f"`{result.name}`" for result in affected)
    models = sorted({f"`{result.model_id}`" for result in affected})
    return "\n".join(
        [
            "## Refusing to write the report",
            "",
            f"{len(affected)} of {len(results)} cases were answered by a fallback model "
            f"({', '.join(models)}) instead of the primary: {names}.",
            "",
            "These numbers do not describe the primary open-weight model, so "
            f"`{RESULTS.name}` was left untouched. Published results must come from the "
            "primary model alone.",
            "",
            "Either wait until the primary model is healthy and re-run, or unset "
            "`KUBE_DOCTOR_FALLBACK_MODEL` and re-run to see the primary's real score.",
        ]
    )


def scripted_replies(expected: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build offline replies for one fixture, mirroring the expected answer."""
    category = str(expected["category"])
    negative = bool(expected.get("negative"))
    keywords = [str(item) for item in expected.get("must_mention", []) if str(item).strip()]
    # The scripted reply echoes every required keyword, so the keyword metric
    # reads as "the wiring works" rather than as a statement about the model.
    evidence = "; ".join(keywords) if keywords else str(expected.get("name", "the screenshot"))
    observations: dict[str, Any] = {
        "image_legible": not negative,
        "items": [] if negative else [{"kind": "pod_status", "text": evidence, "source": "image"}],
        "notes": "synthetic offline reply",
    }
    diagnosis: dict[str, Any] = {
        "summary": evidence,
        "causes": [
            {
                "category": category if category in {c.value for c in Category} else "unknown",
                "title": f"{category} indicated by {evidence}",
                "confidence": "low" if negative else "high",
                "evidence": [evidence],
            }
        ],
        "next_steps": [
            {
                "description": "Inspect the workload read-only",
                "command": "kubectl describe pod",
                "risk": "read_only",
                "rationale": "confirms the signal before any change",
            }
        ],
        "missing_info": [evidence] if negative else [],
        "model_id": "",
    }
    return observations, diagnosis


def main(argv: list[str] | None = None) -> int:
    """Run the harness and write the report."""
    parser = argparse.ArgumentParser(description="Score kube-snapshot-doctor over the fixtures.")
    parser.add_argument(
        "--backend", default="gemini", help="gemini, openai-compat, or fake for offline runs."
    )
    parser.add_argument(
        "--threshold", type=float, default=0.7, help="Minimum top-1 category accuracy."
    )
    parser.add_argument("--fixtures", default=str(FIXTURES), help="Directory holding the fixtures.")
    parser.add_argument(
        "--output", default=str(RESULTS), help="Where to write the Markdown report."
    )
    parser.add_argument("--model", default=None, help="Model id override.")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL override.")
    parser.add_argument(
        "--fallback-model",
        default=None,
        help=(
            "Fallback model id. A run where any case needed the fallback is "
            "reported but not written to disk, because the numbers would not "
            "describe the primary model."
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=None, help="Per-request timeout in seconds."
    )
    parser.add_argument("--case", action="append", default=None, help="Run only these fixtures.")
    parser.add_argument(
        "--offline-note",
        default="",
        help="Extra sentence for the report header, e.g. a caveat about the run.",
    )
    args = parser.parse_args(argv)

    fixture_dir = Path(args.fixtures)
    if not fixture_dir.is_dir():
        print(f"no fixtures at {fixture_dir}", file=sys.stderr)
        return 2
    fixtures = [load_fixture(path) for path in sorted(fixture_dir.iterdir()) if path.is_dir()]
    if args.case:
        wanted = set(args.case)
        fixtures = [fixture for fixture in fixtures if fixture["name"] in wanted]
    if not fixtures:
        print("no fixtures selected", file=sys.stderr)
        return 2

    backend: Backend
    scripted: dict[str, dict[str, Any]] = {}
    if args.backend == "fake":
        for fixture in fixtures:
            observations, diagnosis = scripted_replies(fixture["expected"])
            scripted[fixture["name"]] = {"observations": observations, "diagnosis": diagnosis}
        backend = ScriptedBackend(scripted)
    else:
        backend = build_backend(
            args.backend,
            model=args.model,
            base_url=args.base_url,
            timeout=args.timeout,
            fallback_model=args.fallback_model,
        )

    # Captured before the first call: a backend that falls back changes
    # ``model_id``, and this is the id the published numbers must belong to.
    primary_model_id = getattr(backend, "primary_model_id", backend.model_id)

    results: list[CaseResult] = []
    for fixture in fixtures:
        if isinstance(backend, ScriptedBackend):
            backend.case = fixture["name"]
        try:
            result = run(fixture["screenshot"], backend, logs_path=fixture["logs"])
        except PipelineError as exc:
            results.append(
                CaseResult(
                    name=fixture["name"],
                    expected_category=str(fixture["expected"]["category"]),
                    error=f"{type(exc).__name__}: {exc}",
                    model_id=getattr(backend, "model_id", ""),
                    primary_model_id=primary_model_id,
                )
            )
            continue
        results.append(score_case(fixture, result, primary_model_id))

    report = format_table(results, args.threshold, args.backend, primary_model_id)
    header = [
        "# Eval results",
        "",
        f"Generated by `python evals/run_evals.py --backend {args.backend}`.",
        "",
    ]
    if args.offline_note:
        header += [args.offline_note, ""]
    body = "\n".join([*header, report, ""])

    used_fallback = any(result.used_fallback for result in results)
    if used_fallback:
        # Print the numbers so a run is diagnosable, but leave the published
        # file alone: they describe a mixture of models, not the primary one.
        refusal = fallback_report(results)
        print(body)
        print(refusal)
        print(f"\nrefusing to write {args.output}", file=sys.stderr)
        return 1

    Path(args.output).write_text(body, encoding="utf-8")
    print(body)

    accuracy = sum(1 for result in results if result.category_correct) / len(results)
    hard_failures = [result for result in results if result.error]
    if hard_failures:
        return 1
    return 0 if accuracy >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
