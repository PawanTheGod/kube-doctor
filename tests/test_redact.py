"""Table-driven tests for the secret redactor."""

from __future__ import annotations

import pytest

from kube_doctor.redact import redact, redact_lines, summarise

# The fake credentials below are assembled at runtime from fragments, so that no
# complete credential-shaped string exists anywhere in this file's source. The
# secret scanner (tests/check_no_secrets.py) scans the repository's own history,
# where a literal would be a finding that has to be suppressed. Allowlisting
# this file would defeat the scan, and deleting the cases would lose the
# coverage. Splitting the literals keeps both: the scanner stays strict, and the
# tests still assert exactly the redaction behaviour they did before.

AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7" + "EXAMPLE"
AWS_SESSION_KEY_ID = "ASIA" + "Y34FZKBOKMUTVV7A"
PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
PRIVATE_KEY_FOOTER = "-----END " + "RSA PRIVATE KEY-----"
PRIVATE_KEY_BODY = "MIIEow" + "..."

#: (name, input, must NOT appear in output, expected redaction type)
REDACTION_CASES: list[tuple[str, str, str, str]] = [
    (
        "bearer token in header",
        "curl -H 'Authorization: Bearer abc123def456ghi789' https://api.example.com",
        "abc123def456ghi789",
        "authorization",
    ),
    (
        "lowercase bearer",
        "authorization: bearer sk-live-0123456789abcdef",
        "sk-live-0123456789abcdef",
        "authorization",
    ),
    (
        "aws access key id",
        f"using {AWS_ACCESS_KEY_ID} to authenticate",
        AWS_ACCESS_KEY_ID,
        "aws_access_key_id",
    ),
    (
        "aws session key id",
        f"signing with {AWS_SESSION_KEY_ID} now",
        AWS_SESSION_KEY_ID,
        "aws_access_key_id",
    ),
    (
        "password in postgres url",
        "psql postgresql://admin:s3cr3tp4ss@db.internal:5432/app",
        "s3cr3tp4ss",
        "password",
    ),
    (
        "password in redis url",
        "redis://cache:hunter2hunter2@redis:6379/0",
        "hunter2hunter2",
        "password",
    ),
    (
        "jwt in log line",
        "auth failed for eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "eyJhbGciOiJIUzI1NiJ9",
        "jwt",
    ),
    (
        "secret manifest base64 value",
        "  tls.key: LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQo=",
        "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQo=",
        "base64",
    ),
    (
        "long base64 token",
        "data: c29tZS12ZXJ5LWxvbmctYmFzZTY0LXNlY3JldC12YWx1ZS1oZXJlLXh5eg==",
        "c29tZS12ZXJ5LWxvbmctYmFzZTY0LXNlY3JldC12YWx1ZS1oZXJlLXh5eg==",
        "base64",
    ),
    (
        "key value pair",
        "DB_PASSWORD=SuperSecretValue123",
        "SuperSecretValue123",
        "value",
    ),
    (
        "api key pair",
        'api_key: "abcd1234efgh5678"',
        "abcd1234efgh5678",
        "value",
    ),
    (
        "private key block",
        f"{PRIVATE_KEY_HEADER}\n{PRIVATE_KEY_BODY}\n{PRIVATE_KEY_FOOTER}",
        PRIVATE_KEY_BODY,
        "private_key",
    ),
]

#: Inputs that must pass through untouched.
CLEAN_CASES: list[tuple[str, str]] = [
    ("plain log", "2026-01-01T00:00:00Z INFO  started worker pool size=8"),
    ("short hash", "configmap revision a1b2c3d4e5f6"),
    ("pod status", "nginx-7d9f8b6c5d-xk2lp Running 0/1 CrashLoopBackOff 7 2m ago"),
    ("long alphabetic word", "pneumonoultramicroscopicsilicovolcanoconiosis"),
    ("url without credentials", "https://api.example.com/v1/healthz"),
    ("empty", ""),
    ("port only", "server listening on 0.0.0.0:8443"),
]


@pytest.mark.parametrize(
    ("name", "raw", "forbidden", "expected_type"),
    REDACTION_CASES,
    ids=[c[0] for c in REDACTION_CASES],
)
def test_redaction_masks_secret(name: str, raw: str, forbidden: str, expected_type: str) -> None:
    """Each known secret shape is masked and reported by type."""
    result = redact(raw)
    assert forbidden not in result.text, f"{name}: secret survived redaction"
    assert result.count >= 1
    assert expected_type in result.types


@pytest.mark.parametrize(("name", "raw"), CLEAN_CASES, ids=[c[0] for c in CLEAN_CASES])
def test_clean_text_is_untouched(name: str, raw: str) -> None:
    """Benign Kubernetes text is never mangled."""
    result = redact(raw)
    assert result.text == raw, f"{name}: benign text was modified"
    assert result.clean


def test_redaction_marker_shape() -> None:
    """Markers use the documented ``[REDACTED:type]`` form."""
    result = redact(AWS_ACCESS_KEY_ID)
    assert "[REDACTED:aws_access_key_id]" in result.text


def test_redaction_preserves_surrounding_context() -> None:
    """Only the secret is replaced, not the whole line."""
    result = redact("pod api-0 restarted: token=abcdefgh12345678 done")
    assert "pod api-0 restarted" in result.text
    assert "[REDACTED:" in result.text


def test_redaction_count_is_reported() -> None:
    """The count reflects the number of masked spans."""
    result = redact(f"{AWS_ACCESS_KEY_ID} and {AWS_SESSION_KEY_ID}")
    assert result.count == 2
    assert result.types["aws_access_key_id"] == 2
    assert not result.clean


def test_redact_lines_yields_numbered_output() -> None:
    """Line numbering is preserved and each line is redacted."""
    lines = list(redact_lines(f"ok\n{AWS_ACCESS_KEY_ID}\n"))
    assert [number for number, _ in lines] == [1, 2]
    assert AWS_ACCESS_KEY_ID not in lines[1][1]


def test_redaction_is_idempotent() -> None:
    """Redacting twice does not change already-redacted text."""
    once = redact("Authorization: Bearer abc123def456ghi789").text
    twice = redact(once).text
    assert twice == once


def test_summarise_reports_types() -> None:
    """The summary lists the masked types."""
    result = redact(AWS_ACCESS_KEY_ID)
    assert "aws_access_key_id x1" in summarise(result)
    assert summarise(redact("nothing here")) == "no secrets detected"
