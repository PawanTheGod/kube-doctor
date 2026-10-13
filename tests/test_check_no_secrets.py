"""Tests for the credential scanner.

The scanner is the last line of defence, so its own failure modes matter more
than its patterns: a scan that crashes, skips history, or silently swallows a
git error and reports "clean" is worse than no scan at all. These tests drive it
against real temporary git repositories rather than mocks, because the bugs
worth catching here are exactly the ones a mock cannot reproduce: decoding,
binary diffs, and git's exit codes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module() -> Any:
    """Import ``tests/check_no_secrets.py`` as a module, since tests is not a package."""
    path = ROOT / "tests" / "check_no_secrets.py"
    spec = importlib.util.spec_from_file_location("check_no_secrets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_no_secrets"] = module
    spec.loader.exec_module(module)
    return module


scanner = load_module()


def run_git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and fail the test if it does not succeed."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Return an initialised git repository with one commit already made."""
    run_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    run_git(tmp_path, "config", "user.email", "scanner@example.invalid")
    run_git(tmp_path, "config", "user.name", "Scanner Test")
    run_git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("# scratch\n", encoding="utf-8")
    run_git(tmp_path, "add", "-A")
    run_git(tmp_path, "commit", "--quiet", "-m", "initial")
    return tmp_path


def commit(repo_path: Path, message: str) -> str:
    """Commit the working tree and return the new commit hash."""
    run_git(repo_path, "add", "-A")
    run_git(repo_path, "commit", "--quiet", "-m", message)
    return run_git(repo_path, "rev-parse", "HEAD").strip()


# The patterns match on an exact length, so a hand-written fake has to hit it.
# These helpers pad a seed to the length the scanner requires, which keeps the
# fixtures honest instead of relying on counting characters by eye.
AWS_BODY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
GOOGLE_BODY_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def fake_aws_key(seed: str) -> str:
    """Return an AWS-key-id-shaped string: ``AKIA`` plus exactly 16 characters."""
    return "AKIA" + (seed + AWS_BODY_ALPHABET)[:16]


def fake_google_key(seed: str) -> str:
    """Return a Google-API-key-shaped string: ``AIza`` plus exactly 35 characters."""
    return "AIza" + (seed + GOOGLE_BODY_ALPHABET)[:35]


# --------------------------------------------------------------------------- #
# decoding
# --------------------------------------------------------------------------- #


def test_own_repository_scans_without_crashing() -> None:
    """The scanner runs against this repository, binary fixtures and all."""
    assert scanner.main() in (0, scanner.EXIT_FOUND)


def test_git_output_is_decoded_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git output is decoded as UTF-8, not as the platform's locale codec.

    The bytes below include 0x90, which cp1252 cannot decode: on Windows that
    used to raise inside the reader thread, losing the output and leaving the
    scan silently empty.
    """
    captured: dict[str, Any] = {}

    class Result:
        returncode = 0
        stdout = "café \x90\n"
        stderr = ""

    def fake_run(args: list[str], **kwargs: Any) -> Result:
        captured.update(kwargs)
        captured["args"] = args
        return Result()

    monkeypatch.setattr(scanner.subprocess, "run", fake_run)
    output = scanner.git("log", "--oneline")
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert "\x90" in output


def test_history_with_binary_and_non_utf8_files_is_scanned(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A binary file and a non-UTF-8 file in history do not abort the scan.

    This is the reproduction: the commit below carries both a NUL-containing
    "PNG" and a text file holding bytes that are invalid in UTF-8 and undefined
    in cp1252, followed by a real-looking key. The key must still be reported.
    """
    (repo / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x90\x00\xff\xfe binary")
    (repo / "broken.log").write_bytes(b"panic: \x90\x81\x8d\xff latin-1 bytes")
    fake_key = fake_aws_key("QUIETFAKEKEY1234")
    (repo / "config.yaml").write_text(f"aws_key: {fake_key}\n", encoding="utf-8")
    sha = commit(repo, "add fixtures and a leaked key")

    monkeypatch.setattr(scanner, "ROOT", repo)
    exit_code = scanner.main()
    captured = capsys.readouterr()
    assert exit_code == scanner.EXIT_FOUND
    # Reportable as <commit>:<path>:<line>, never as a bare commit and line.
    assert f"{sha[:12]}:config.yaml:1: possible aws access key id" in captured.err
    assert "config.yaml:1: possible aws access key id" in captured.err


def test_binary_file_is_not_decoded() -> None:
    """A NUL byte marks a file as binary, the same rule as ``git grep -I``."""
    assert scanner.decode(b"\x89PNG\x00\x90binary") is None
    assert scanner.decode(b"plain utf-8 \x90 text") is not None
    # Undecodable bytes become U+FFFD rather than raising.
    assert "�" in scanner.decode(b"\x90\x81")


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #


def test_real_looking_key_is_flagged(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential-shaped string in a tracked file is reported and returns 1."""
    fake_key = fake_google_key("SyD0")
    (repo / "notes.txt").write_text(f"export GOOGLE_KEY={fake_key}\n", encoding="utf-8")
    commit(repo, "add notes")

    monkeypatch.setattr(scanner, "ROOT", repo)
    assert scanner.main() == scanner.EXIT_FOUND


def test_finding_reports_pattern_name_never_the_value(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message names the pattern; the matched value is never echoed."""
    fake_key = fake_aws_key("NOSECRETKEY0987")
    (repo / "notes.txt").write_text(f"key={fake_key}\n", encoding="utf-8")
    commit(repo, "add notes")

    monkeypatch.setattr(scanner, "ROOT", repo)
    assert scanner.main() == scanner.EXIT_FOUND
    captured = capsys.readouterr()
    assert "possible aws access key id" in captured.err
    assert fake_key not in captured.err
    assert fake_key not in captured.out


def test_history_finding_names_commit_path_and_line() -> None:
    """History findings come back as ``<commit>:<path>:<line>``."""
    fake_key = fake_aws_key("HISTORYKEY1234")
    # One context line then two additions in a hunk starting at new line 11:
    # the context is line 11, so the added lines are 12 and 13.
    diff = (
        "commit 0123456789abcdef0123456789abcdef01234567\n"
        "diff --git a/old/notes.txt b/old/notes.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/old/notes.txt\n"
        "+++ b/old/notes.txt\n"
        "@@ -10,1 +11,3 @@\n"
        " earlier context line\n"
        f"+key={fake_key}\n"
        "+trailing line\n"
    )
    findings = scanner.parse_history(diff)
    assert findings == ["0123456789ab:old/notes.txt:12: possible aws access key id"]
    assert fake_key not in "".join(findings)


def test_history_ignores_deleted_files_and_context() -> None:
    """A deleted file's hunk, and the ``---``/``+++`` headers, are not scanned."""
    diff = (
        "commit 0123456789abcdef0123456789abcdef01234567\n"
        "--- a/gone.txt\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-removed line\n"
    )
    assert scanner.parse_history(diff) == []


def test_placeholder_lines_are_skipped() -> None:
    """An obviously placeholder line is not a finding."""
    assert scanner.is_placeholder("api_key: REDACTED")
    assert not scanner.is_placeholder("api_key: abc")


# --------------------------------------------------------------------------- #
# a failed scan is not a clean scan
# --------------------------------------------------------------------------- #


def test_git_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A git command that fails must not be reported as a clean scan."""

    class Result:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository (or any of the parent directories): .git\n"

    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: Result())
    exit_code = scanner.main()
    captured = capsys.readouterr()
    assert exit_code == scanner.EXIT_CANNOT_SCAN != 0
    assert "could not complete" in captured.err
    assert "exit code 128" in captured.err
    assert "not a clean scan" in captured.err.lower() or "not a pass" in captured.err.lower()


def test_git_missing_raises_rather_than_returning_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git binary that cannot be run is an error, not an empty result."""

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(scanner.subprocess, "run", fake_run)
    with pytest.raises(scanner.GitFailed, match="could not run"):
        scanner.git("log")


def test_scan_outside_a_repository_does_not_report_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pointed at a directory that is not a repo, the scan refuses to answer."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    # Isolate from any enclosing repository so git genuinely has nothing to read.
    monkeypatch.setitem(
        __import__("os").environ,
        "GIT_CEILING_DIRECTORIES",
        str(tmp_path),
    )
    monkeypatch.setattr(scanner, "ROOT", outside)
    assert scanner.main() == scanner.EXIT_CANNOT_SCAN
    assert "could not complete" in capsys.readouterr().err


def test_exit_codes_are_distinct_and_nonzero() -> None:
    """A hit and an impossible scan are distinguishable by exit code."""
    assert scanner.EXIT_FOUND != 0
    assert scanner.EXIT_CANNOT_SCAN != 0
    assert scanner.EXIT_FOUND != scanner.EXIT_CANNOT_SCAN
