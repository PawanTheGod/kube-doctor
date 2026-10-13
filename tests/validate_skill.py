#!/usr/bin/env python3
"""Validate ``skills/kube-snapshot-doctor/SKILL.md`` against the skill rules.

Checks the things the Agent Skills specification and the submission checklist
actually care about, so a regression is caught in CI instead of at review:

* the frontmatter parses, and ``---`` is the first line of the file
* ``name`` equals the directory name, is lowercase, hyphen-separated, and
  within the length limit
* ``description`` exists, is under 1024 characters, and says what the skill does
  and when to use it
* only frontmatter keys the spec allows are present
* the body is under 500 lines
* the wrapper script exists and is referenced by the body
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "kube-snapshot-doctor"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "diagnose.sh"
REFERENCE = SKILL_DIR / "references" / "failure-modes.md"

#: Frontmatter keys allowed by the Agent Skills specification.
ALLOWED_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
#: Maximum length of the ``name`` field.
MAX_NAME_LENGTH = 64
#: Maximum length of the ``description`` field.
MAX_DESCRIPTION_LENGTH = 1024
#: Maximum body length, in lines.
MAX_BODY_LINES = 500
#: A directory name must be lowercase with hyphens as separators.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure when ``condition`` is false."""
    if not condition:
        failures.append(message)


def main() -> int:
    """Run every check and report."""
    if not SKILL_MD.exists():
        print(f"missing {SKILL_MD}", file=sys.stderr)
        return 1

    text = SKILL_MD.read_text(encoding="utf-8")
    lines = text.splitlines()

    check(lines and lines[0] == "---", "the file must start with a `---` frontmatter fence")
    try:
        end = lines.index("---", 1)
    except ValueError:
        print("no closing `---` in the frontmatter", file=sys.stderr)
        return 1

    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        print(f"frontmatter is not valid YAML: {exc}", file=sys.stderr)
        return 1
    body_lines = lines[end + 1 :]

    unexpected = set(frontmatter) - ALLOWED_KEYS
    check(not unexpected, f"frontmatter has keys the spec does not allow: {sorted(unexpected)}")

    name = str(frontmatter.get("name", ""))
    check(bool(name), "frontmatter is missing `name`")
    check(
        name == SKILL_DIR.name,
        f"`name` ({name!r}) must equal the directory name ({SKILL_DIR.name!r})",
    )
    check(bool(NAME_PATTERN.match(name)), f"`name` ({name!r}) must be lowercase with hyphens")
    check(len(name) <= MAX_NAME_LENGTH, f"`name` must be at most {MAX_NAME_LENGTH} characters")

    description = str(frontmatter.get("description", ""))
    check(bool(description), "frontmatter is missing `description`")
    check(
        len(description) <= MAX_DESCRIPTION_LENGTH,
        f"`description` is {len(description)} characters, limit is {MAX_DESCRIPTION_LENGTH}",
    )
    lowered = description.lower()
    check(
        any(word in lowered for word in ("use when", "use it when", "ask")),
        "`description` must say when to use the skill, not only what it does",
    )
    check(
        any(word in lowered for word in ("screenshot", "image", "pod", "grafana", "kubectl")),
        "`description` must name the inputs the skill handles",
    )

    check("license" in frontmatter, "frontmatter is missing `license`")
    check("compatibility" in frontmatter, "frontmatter is missing `compatibility`")
    check("metadata" in frontmatter, "frontmatter is missing `metadata`")
    check(
        isinstance(frontmatter.get("metadata"), dict),
        "`metadata` must be a YAML map",
    )

    check(
        len(body_lines) <= MAX_BODY_LINES,
        f"body is {len(body_lines)} lines, limit is {MAX_BODY_LINES}",
    )
    body = "\n".join(body_lines)
    check(SCRIPT.exists(), f"missing wrapper script {SCRIPT.relative_to(ROOT)}")
    check(
        "diagnose.sh" in body,
        "the body must tell the agent how to run scripts/diagnose.sh",
    )
    check(REFERENCE.exists(), f"missing reference {REFERENCE.relative_to(ROOT)}")
    check(
        "references/failure-modes.md" in body,
        "the body should point at references/failure-modes.md for depth",
    )
    check(
        "MUTATING" in body,
        "the body must state the rule about mutating commands",
    )

    if failures:
        print("SKILL.md validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(
        f"SKILL.md is valid: name={name!r}, description={len(description)} chars, "
        f"body={len(body_lines)} lines"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
