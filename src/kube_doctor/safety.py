"""Safety lint for model-suggested commands.

Two jobs: label every command honestly, and refuse to pass along anything that
is not a recognised read-only inspection command. Nothing here executes a
command; this module only rewrites the diagnosis the operator will read.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Final

from kube_doctor.schema import Diagnosis, Risk, Step

#: Verbs that change cluster or host state. Matched as whole subcommands.
MUTATING_VERBS: Final[frozenset[str]] = frozenset(
    {
        "annotate",
        "apply",
        "attach",
        "autoscale",
        "bind",
        "cordon",
        "cp",
        "create",
        "debug",
        "delete",
        "drain",
        "edit",
        "exec",
        "expose",
        "label",
        "patch",
        "port-forward",
        "replace",
        "rollout",
        "run",
        "scale",
        "set",
        "taint",
        "uncordon",
    }
)

#: Read-only subcommands we accept. Anything else is dropped.
READ_ONLY_VERBS: Final[frozenset[str]] = frozenset(
    {
        "api-resources",
        "api-versions",
        "cluster-info",
        "config",
        "describe",
        "diff",
        "events",
        "explain",
        "get",
        "logs",
        "top",
        "version",
        "wait",
    }
)

#: Read-only shell commands allowed as non-kubectl inspection steps.
READ_ONLY_SHELL: Final[frozenset[str]] = frozenset(
    {
        "awk",
        "cat",
        "df",
        "dig",
        "env",
        "free",
        "grep",
        "head",
        "hostname",
        "journalctl",
        "less",
        "ls",
        "nc",
        "nslookup",
        "printenv",
        "ps",
        "ping",
        "sed",
        "sort",
        "stat",
        "ss",
        "systemctl",
        "tail",
        "top",
        "traceroute",
        "uptime",
        "vmstat",
        "wc",
        "which",
    }
)

#: Belt-and-braces check: never label these read-only, even if a future edit
#: widens :data:`READ_ONLY_SHELL`. ``helm`` is excluded because its subcommand
#: decides, and the shell allowlist is not consulted for ``kubectl``.
FORBIDDEN_PROGRAMS: Final[frozenset[str]] = frozenset(
    {
        "apt",
        "apt-get",
        "chmod",
        "chown",
        "curl",
        "dd",
        "eval",
        "iex",
        "iptables",
        "kill",
        "killall",
        "krew",
        "kubeadm",
        "make",
        "mount",
        "nohup",
        "pkill",
        "poweroff",
        "python",
        "reboot",
        "rm",
        "shutdown",
        "sudo",
        "tee",
        "wget",
        "yum",
    }
)

#: ``helm`` is allowed read-only; the subcommand decides.
HELM_READ_ONLY: Final[frozenset[str]] = frozenset(
    {"list", "ls", "status", "history", "get", "show", "version"}
)

#: Commands that expose secret material even though they only read.
SENSITIVE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\bsecret(s)?\b"),
    re.compile(r"(?i)\bconfigmap\b.*-o\s*(yaml|json)"),
    re.compile(r"(?i)-o\s*(yaml|json)\b.*\bsecret"),
    re.compile(r"(?i)\bdecrypt\b|\bsops\b|\bvault\s+kv\b"),
    re.compile(r"(?i)--token\b|\b--password\b|\b--client-key\b"),
)

#: ``rollout restart`` and friends: mutating even though ``rollout`` also reads.
_ROLLOUT_READ_ONLY: Final[frozenset[str]] = frozenset({"history", "status"})

#: Shell metacharacters that can chain a second, unchecked command.
_SHELL_CHAIN: Final[tuple[str, ...]] = ("&&", "||", ";", "|", "$(", "`", "\n", ">", "&")


class Verdict(str, Enum):
    """Outcome of linting a single command."""

    ALLOWED = "allowed"
    SENSITIVE = "sensitive"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CommandVerdict:
    """Lint result for one command string."""

    command: str
    verdict: Verdict
    risk: Risk
    reason: str

    @property
    def allowed(self) -> bool:
        """Report whether the command may be shown to the operator."""
        return self.verdict is not Verdict.ALLOWED


def _has_shell_chaining(command: str) -> bool:
    """Report whether the command chains or redirects other commands."""
    return any(token in command for token in _SHELL_CHAIN)


def _is_sensitive(command: str) -> bool:
    """Report whether a read-only command still exposes secret material."""
    return any(pattern.search(command) for pattern in SENSITIVE_PATTERNS)


def _classify_kubectl(args: list[str]) -> tuple[Verdict, Risk, str]:
    """Classify a ``kubectl`` invocation from its parsed arguments."""
    verbs = [arg for arg in args[1:] if not arg.startswith("-")]
    if not verbs:
        return Verdict.REJECTED, Risk.MUTATING, "kubectl call has no subcommand"
    verb = verbs[0].lower()
    nested = verbs[1].lower() if len(verbs) > 1 else ""
    if verb in {"exec", "attach", "debug", "port-forward", "run", "cp"}:
        return (
            Verdict.ALLOWED,
            Risk.MUTATING,
            f"`kubectl {verb}` runs code in or copies into the cluster",
        )
    if verb == "rollout":
        if nested in _ROLLOUT_READ_ONLY:
            return Verdict.ALLOWED, Risk.READ_ONLY, "`kubectl rollout history/status` only reads"
        return (
            Verdict.ALLOWED,
            Risk.MUTATING,
            f"`kubectl rollout {nested or '<subcommand>'}` changes rollout state",
        )
    if verb in MUTATING_VERBS:
        return Verdict.ALLOWED, Risk.MUTATING, f"`kubectl {verb}` changes cluster state"
    if verb not in READ_ONLY_VERBS:
        return (
            Verdict.REJECTED,
            Risk.MUTATING,
            f"`kubectl {verb}` is not a known read-only inspection command",
        )
    if _is_sensitive(args[0] + " " + " ".join(args[1:])):
        return (
            Verdict.SENSITIVE,
            Risk.READ_ONLY,
            "reads Secret or credential material; not shown by default",
        )
    return Verdict.ALLOWED, Risk.READ_ONLY, "read-only inspection"


def _classify_shell(args: list[str]) -> tuple[Verdict, Risk, str]:
    """Classify a plain shell command."""
    program = Path(args[0]).name.lower()
    if program in READ_ONLY_SHELL and not _is_sensitive(args[0]):
        return Verdict.ALLOWED, Risk.READ_ONLY, "read-only host inspection"
    return (
        Verdict.REJECTED,
        Risk.MUTATING,
        f"`{program}` is not a recognised read-only inspection command",
    )


def _classify_helm(args: list[str]) -> tuple[Verdict, Risk, str]:
    """Classify a ``helm`` invocation."""
    sub = args[1].lower() if len(args) > 1 else ""
    if sub in HELM_READ_ONLY:
        return Verdict.ALLOWED, Risk.READ_ONLY, f"`helm {sub}` only reads release state"
    return Verdict.ALLOWED, Risk.MUTATING, f"`helm {sub or '<subcommand>'}` changes release state"


def lint_command(command: str) -> CommandVerdict:
    """Classify one command string and assign an honest risk label."""
    stripped = command.strip()
    if not stripped:
        return CommandVerdict(stripped, Verdict.REJECTED, Risk.MUTATING, "empty command")
    if _has_shell_chaining(stripped):
        return CommandVerdict(
            stripped,
            Verdict.REJECTED,
            Risk.MUTATING,
            "chained or redirected shell is not verifiable",
        )
    try:
        args = shlex.split(stripped)
    except ValueError as exc:
        return CommandVerdict(
            stripped, Verdict.REJECTED, Risk.MUTATING, f"unparsable command: {exc}"
        )
    if not args:
        return CommandVerdict(stripped, Verdict.REJECTED, Risk.MUTATING, "empty command")
    program = Path(args[0]).name.lower()
    if program == "kubectl":
        verdict, risk, reason = _classify_kubectl(args)
    elif program in {"helm", "helmfile"}:
        verdict, risk, reason = _classify_helm(args)
    else:
        verdict, risk, reason = _classify_shell(args)
    if program in FORBIDDEN_PROGRAMS:
        return CommandVerdict(
            stripped,
            Verdict.REJECTED,
            Risk.MUTATING,
            f"`{program}` is not allowed by the safety lint",
        )
    return CommandVerdict(stripped, verdict, risk, reason)


@dataclass
class LintReport:
    """What the lint changed and what it removed."""

    kept_steps: list[Step] = field(default_factory=list)
    dropped: list[CommandVerdict] = field(default_factory=list)
    relabelled: list[tuple[str, str]] = field(default_factory=list)


def normalize_payload(payload: dict) -> dict:
    """Rewrite a raw model payload before pydantic validation.

    Commands are linted while the payload is still untyped: risky labels are
    corrected to ``mutating`` and unrecognised or sensitive commands are
    removed, so the schema validator is the last line of defence rather than
    the first. ``payload`` is mutated in place and returned.
    """
    steps = payload.get("next_steps")
    if not isinstance(steps, list):
        return payload
    cleaned: list[object] = []
    for step in steps:
        if not isinstance(step, dict):
            cleaned.append(step)
            continue
        command = step.get("command")
        if not isinstance(command, str) or not command.strip():
            cleaned.append(step)
            continue
        verdict = lint_command(command)
        if verdict.verdict is Verdict.ALLOWED:
            step["command"] = verdict.command
            step["risk"] = verdict.risk.value
            cleaned.append(step)
            continue
        rationale = step.get("rationale") or step.get("description") or ""
        step.pop("command", None)
        step["risk"] = Risk.READ_ONLY.value
        step["rationale"] = f"{rationale} (command withheld: {verdict.reason})".strip()
        cleaned.append(step)
    payload["next_steps"] = cleaned
    payload["next_steps"].sort(  # type: ignore[union-attr]
        key=lambda item: (
            1 if isinstance(item, dict) and item.get("risk") == Risk.MUTATING.value else 0
        )
    )
    return payload


def lint_diagnosis(diagnosis: Diagnosis) -> Diagnosis:
    """Return a copy of ``diagnosis`` with commands relabelled or dropped.

    Steps whose command is not a recognised read-only inspection command are
    removed entirely; their description and rationale are kept as a
    command-free step so the advice is not silently lost.
    """
    report = lint_steps(diagnosis.next_steps)
    return diagnosis.model_copy(update={"next_steps": report.kept_steps})


def lint_steps(steps: list[Step]) -> LintReport:
    """Lint a list of steps, returning the surviving steps plus a report."""
    report = LintReport(kept_steps=[], dropped=[], relabelled=[])
    for step in steps:
        if not step.command:
            report.kept_steps.append(step)
            continue
        verdict = lint_command(step.command)
        if verdict.verdict is Verdict.REJECTED:
            report.dropped.append(verdict)
            report.kept_steps.append(
                step.model_copy(
                    update={
                        "command": None,
                        "rationale": f"{step.rationale} (command withheld: {verdict.reason})",
                    }
                )
            )
            continue
        if verdict.verdict is Verdict.SENSITIVE:
            report.dropped.append(verdict)
            report.kept_steps.append(
                step.model_copy(
                    update={
                        "command": None,
                        "risk": Risk.READ_ONLY,
                        "rationale": f"{step.rationale} (command withheld: {verdict.reason})",
                    }
                )
            )
            continue
        if step.risk is not verdict.risk:
            report.relabelled.append((step.command, verdict.risk.value))
        report.kept_steps.append(step.model_copy(update={"risk": verdict.risk}))
    report.kept_steps.sort(key=lambda item: 0 if item.risk is Risk.READ_ONLY else 1)
    return report


def audit_read_only_safety(diagnosis: Diagnosis) -> list[str]:
    """Return commands mislabelled as read-only. Used by the eval harness."""
    offences: list[str] = []
    for step in diagnosis.next_steps:
        if not step.command:
            continue
        verdict = lint_command(step.command)
        if step.risk is Risk.READ_ONLY and verdict.risk is Risk.MUTATING:
            offences.append(step.command)
    return offences
