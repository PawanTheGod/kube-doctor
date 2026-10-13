"""Redact secrets from log text before it reaches a model API.

This is a defence in depth, not a guarantee. It handles the secret shapes that
show up in Kubernetes logs and Secret manifests; it cannot redact pixels inside
a screenshot. See the security model in the README for the honest limits.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Final


#: One known secret shape.
@dataclass(frozen=True)
class Redaction:
    """One secret type that the redactor knows how to mask."""

    name: str
    pattern: re.Pattern[str]
    replacement: str | Callable[[re.Match[str]], str]

    def apply(self, text: str) -> tuple[str, int]:
        """Mask every match, returning the new text and the match count."""
        return self.pattern.subn(self.replacement, text)


def _placeholder(kind: str) -> str:
    """Render the canonical ``[REDACTED:type]`` marker."""
    return f"[REDACTED:{kind}]"


#: Ordered so that more specific shapes are masked before generic ones.
REDACTIONS: Final[tuple[Redaction, ...]] = (
    Redaction(
        name="jwt",
        pattern=re.compile(
            r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b",
        ),
        replacement=_placeholder("jwt"),
    ),
    Redaction(
        name="authorization",
        pattern=re.compile(
            r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}",
        ),
        replacement=lambda match: f"{match.group(1)} {_placeholder('authorization')}",
    ),
    Redaction(
        name="aws_access_key_id",
        pattern=re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|A3T)[A-Z0-9]{16}\b"),
        replacement=_placeholder("aws_access_key_id"),
    ),
    Redaction(
        # The rule name is the ``[REDACTED:<name>]`` marker, so the audit
        # trail and the masked text always agree.
        name="password",
        pattern=re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):([^/\s@]{2,})@"),
        replacement=lambda match: f"{match.group(1)}:{_placeholder('password')}@",
    ),
    Redaction(
        name="private_key",
        pattern=re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        replacement=_placeholder("private_key"),
    ),
    Redaction(
        name="value",
        # The key may carry a prefix (``DB_PASSWORD``, ``x-api-key``), so match
        # the whole key rather than a bare keyword. A bare ``\b`` would miss
        # those, because an underscore counts as a word character.
        pattern=re.compile(
            r"(?i)([A-Za-z0-9._-]*(?:password|passwd|pwd|secret|token|api[_-]?key))\s*[=:]\s*\S{4,}"
        ),
        replacement=lambda match: f"{match.group(1)}={_placeholder('value')}",
    ),
    Redaction(
        name="base64",
        # 41+ base64 characters, mixing letters and digits, which is what
        # encoded Secret data looks like. Long all-letter words and short hex
        # ids survive; trailing ``=`` padding is part of the match.
        pattern=re.compile(
            r"(?<![A-Za-z0-9+/=-])"
            r"(?=[A-Za-z0-9+/]{41,}={0,2}(?![A-Za-z0-9+/=-]))"
            r"(?=[A-Za-z0-9+/]*[A-Za-z])(?=[A-Za-z0-9+/]*\d)"
            r"[A-Za-z0-9+/]{41,}={0,2}"
            r"(?![A-Za-z0-9+/=-])"
        ),
        replacement=_placeholder("base64"),
    ),
)


@dataclass
class RedactionResult:
    """Redacted text plus an audit trail of what was masked."""

    text: str
    count: int = 0
    types: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """Report whether anything was redacted."""
        return self.count == 0


def redact(text: str, *, redactions: tuple[Redaction, ...] = REDACTIONS) -> RedactionResult:
    """Mask known secret shapes in ``text``.

    Returns the cleaned text, the number of redactions, and a per-type count so
    callers can warn the user about what was found.
    """
    if not text:
        return RedactionResult(text=text)
    cleaned = text
    total = 0
    types: dict[str, int] = {}
    for rule in redactions:
        cleaned, count = rule.apply(cleaned)
        if count:
            types[rule.name] = types.get(rule.name, 0) + count
            total += count
    return RedactionResult(text=cleaned, count=total, types=types)


def redact_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, redacted_line)`` pairs."""
    for number, line in enumerate(text.splitlines(), start=1):
        yield number, redact(line).text


def summarise(result: RedactionResult) -> str:
    """Render a one-line human summary of what was masked."""
    if not result.types:
        return "no secrets detected"
    parts = ", ".join(f"{name} x{count}" for name, count in sorted(result.types.items()))
    return f"{result.count} redaction(s): {parts}"
