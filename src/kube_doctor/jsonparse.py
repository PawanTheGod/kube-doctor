"""Best-effort extraction of the first JSON object from model output."""

from __future__ import annotations

import json
from typing import Any

_FENCE_PREFIXES = ("```json", "```JSON", "```")


def strip_code_fences(text: str) -> str:
    """Remove a leading and trailing markdown code fence, if present."""
    stripped = text.strip()
    for prefix in _FENCE_PREFIXES:
        if stripped.startswith(prefix):
            remainder = stripped[len(prefix) :]
            if remainder.startswith("\n"):
                remainder = remainder[1:]
            if remainder.rstrip().endswith("```"):
                return remainder.rstrip()[: -len("```")].rstrip()
            return remainder
    return stripped


def extract_first_object(raw: str) -> dict[str, Any]:
    """Return the first balanced JSON object in ``raw``.

    Scans for the first ``{`` and tracks brace depth while ignoring braces
    inside JSON strings, so nested objects survive. Raises ``ValueError``
    when no parsable object is found.
    """
    text = strip_code_fences(raw)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    msg = f"no JSON object found in model output: {text[:200]!r}"
    raise ValueError(msg)
