#!/usr/bin/env python3
"""Fail if credential-shaped strings are anywhere in the git history.

Redaction runs at request time, so the real risk is a key pasted into a
fixture, a log, or a committed screenshot caption. This scans every tracked
file and every commit in the reachable history. It reports the shapes, not the
values: the message never echoes a matched secret.

Three properties matter more than the patterns themselves.

*Decode.* Git output is decoded as UTF-8 with replacement, never with the
platform's locale encoding. On Windows the locale codec is cp1252, which
crashes on the byte ranges that UTF-8 text actually contains, and it crashes
inside the reader thread, so the failure surfaces as a truncated result rather
than an error.

*Binary files are skipped.* Git treats a file as binary when it contains a NUL
byte near the start, the same rule as ``git grep -I``. Such a file is not
decoded and not scanned, so an embedded blob cannot abort the scan.

*A failed scan is never a clean scan.* Every git call must succeed. If one
does not, the tool exits nonzero with the failing command, because "no secrets
found" from a scan that did not run is the worst possible answer.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: At least one credential-shaped string was found.
EXIT_FOUND = 1
#: The scan could not complete, so its answer is unknown rather than clean.
EXIT_CANNOT_SCAN = 2

#: NUL within this many bytes marks a file as binary, matching ``git grep -I``.
BINARY_SNIFF_BYTES = 8000

#: (label, pattern). Deliberately specific: broad patterns make this useless.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b")),
    ("slack token", re.compile(r"\bxox[abposr]-[0-9A-Za-z\-]{10,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("anthropic key", re.compile(r"\bsk-ant-[0-9A-Za-z_\-]{20,}\b")),
    ("openai key", re.compile(r"\bsk-proj-[0-9A-Za-z_\-]{20,}\b")),
    ("hugging face token", re.compile(r"\bhf_[0-9A-Za-z]{30,}\b")),
]

#: Placeholders that are fine to have in the repo.
SAFE_MARKERS = ("REDACTED", "your-", "example", "xxxx", "placeholder")

#: ``@@ -12,3 +14,5 @@`` — only the new-file start line is needed.
HUNK_HEADER = re.compile(r"^@@+ .*?\+(\d+)(?:,\d+)? @@")


class GitFailed(Exception):
    """A git command could not be run, or did not succeed."""


def is_placeholder(line: str) -> bool:
    """Report whether a line is clearly a placeholder rather than a real secret."""
    return any(marker in line for marker in SAFE_MARKERS)


def decode(data: bytes) -> str | None:
    """Return file content as text, or ``None`` when the file is binary.

    Binary files are skipped rather than decoded: a byte-level diff of a PNG is
    noise, and scanning it is what breaks the scan. Undecodable bytes in an
    otherwise-textual file become U+FFFD instead of raising.
    """
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_text(text: str, where: str, first_line: int = 1) -> list[str]:
    """Return one message per credential-shaped match in ``text``.

    ``where`` is a location prefix such as ``README.md`` or ``<commit>:<path>``,
    so every message identifies a file, and for history a commit and line.
    """
    found: list[str] = []
    for offset, line in enumerate(text.splitlines()):
        if is_placeholder(line):
            continue
        number = first_line + offset
        for label, pattern in PATTERNS:
            if pattern.search(line):
                found.append(f"{where}:{number}: possible {label}")
    return found


def scan_worktree() -> tuple[list[str], int]:
    """Scan every tracked file that exists on disk; return findings and the count."""
    findings: list[str] = []
    tracked = git("ls-files", "-z").split("\0")
    paths = [path for path in tracked if path]
    for relative in paths:
        path = ROOT / relative
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            msg = f"could not read tracked file {relative}: {exc}"
            raise GitFailed(msg) from exc
        text = decode(data)
        if text is None:
            continue
        findings += scan_text(text, relative)
    return findings, len(paths)


def parse_history(diff: str) -> list[str]:
    """Scan the added lines of a ``git log -p --unified=0`` dump.

    Each finding is ``<commit>:<path>:<line>``, taken from the hunk headers, so
    it can be opened directly instead of guessed at. Binary diffs appear as a
    ``Binary files ... differ`` marker and contribute no content lines.
    """
    findings: list[str] = []
    commit = ""
    path = ""
    number = 0
    for line in diff.splitlines():
        if line.startswith("commit "):
            parts = line.split()
            commit = parts[1][:12] if len(parts) > 1 else ""
            path = ""
            number = 0
        elif line.startswith("+++ "):
            target = line[4:].strip()
            # ``b/path`` is the new name; /dev/null means the file was deleted.
            path = "" if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@"):
            header = HUNK_HEADER.match(line)
            number = int(header.group(1)) if header else 0
        elif line.startswith("+"):
            # An added line advances the new-file counter and, when the hunk
            # belongs to a real file, is the thing worth scanning.
            if path and commit:
                findings += scan_text(line[1:], f"{commit}:{path}", first_line=number)
            number += 1
        elif line.startswith(" "):
            number += 1
    return findings


def scan_history() -> tuple[list[str], int]:
    """Scan every commit in the reachable history; return findings and the count."""
    log = git(
        "-c",
        "core.quotePath=false",
        "log",
        "--all",
        "--format=commit %H",
        "--unified=0",
        "--patch",
    )
    commits = sum(1 for line in log.splitlines() if line.startswith("commit "))
    return parse_history(log), commits


def git(*args: str) -> str:
    """Run a git command and return its stdout.

    Raises :class:`GitFailed` instead of returning an empty string on failure, so
    a caller cannot mistake "git did not work" for "nothing to report".
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"could not run `git {' '.join(args)}`: {exc}"
        raise GitFailed(msg) from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        msg = f"`git {' '.join(args)}` failed with exit code {result.returncode}"
        if detail:
            msg += f": {detail[-1]}"
        raise GitFailed(msg)
    return result.stdout


def main() -> int:
    """Scan the working tree and the history; return 1 on a hit, 2 on a failure."""
    try:
        tree_findings, files = scan_worktree()
        history_findings, commits = scan_history()
    except GitFailed as exc:
        print(f"secret scan could not complete: {exc}", file=sys.stderr)
        print(
            "This is not a pass. Fix git's availability, or run the scan in a clone "
            "with full history, and re-run.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_SCAN

    findings = tree_findings + history_findings
    if findings:
        print(
            "credential-shaped strings found; remove them and rewrite the history:",
            file=sys.stderr,
        )
        for finding in sorted(set(findings)):
            print(f"  - {finding}", file=sys.stderr)
        return EXIT_FOUND
    print(f"no credential-shaped strings in {files} tracked files or {commits} commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
