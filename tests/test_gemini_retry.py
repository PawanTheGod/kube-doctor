"""Tests for the retry policy, the Gemini backend, and the fallback model.

The Gemini service returns 500/503/504 routinely while a model loads or a
region saturates, so these tests are the ones that decide whether the tool
survives a bad afternoon on Google's side.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import pytest

from kube_doctor.backends.base import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    NON_RETRYABLE_STATUS_CODES,
    RETRY_AFTER_MAX_SECONDS,
    RETRYABLE_STATUS_CODES,
    THIRD_PARTY_LOGGERS,
    BackendConfigError,
    BackendError,
    BackendRetryExhausted,
    BackendTransientError,
    backoff_delay,
    call_with_retries,
    is_transient,
    resolve_fallback_model,
    resolve_timeout,
    retry_after_seconds,
    silence_third_party_loggers,
    status_code_of,
)
from kube_doctor.backends.gemini import GeminiBackend
from kube_doctor.backends.openai_compat import OpenAICompatBackend

# --------------------------------------------------------------------------- #
# test doubles
# --------------------------------------------------------------------------- #


class FakeApiError(Exception):
    """An error shaped like the SDK's: a status code and optional headers."""

    def __init__(self, code: int, message: str = "", headers: dict[str, str] | None = None) -> None:
        """Record the status code the way ``google-genai`` does."""
        super().__init__(message or f"HTTP {code}")
        self.code = code
        self.response = type("Response", (), {"status_code": code, "headers": headers or {}})()


class FakeResponse:
    """Minimal stand-in for a ``generate_content`` response."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.candidates: list[Any] = []


class FakeModels:
    """Records requests and replays a scripted sequence of outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        #: Each item is either a string to return, or an exception to raise.
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> FakeResponse:
        """Record the call and produce the next scripted outcome."""
        self.calls.append(kwargs)
        if not self.outcomes:
            msg = "fake client ran out of scripted outcomes"
            raise AssertionError(msg)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome)


class FakeClient:
    """Stand-in for the ``google.genai`` client."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.models = FakeModels(outcomes)


def gemini_backend(outcomes: list[Any], **kwargs: Any) -> tuple[GeminiBackend, FakeClient]:
    """Build a Gemini backend over a fake client."""
    client = FakeClient(outcomes)
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("model", "gemma-4-31b-it")
    backend = GeminiBackend(client_factory=lambda _k: client, **kwargs)
    return backend, client


# --------------------------------------------------------------------------- #
# status extraction and classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", sorted(RETRYABLE_STATUS_CODES))
def test_retryable_statuses_are_transient(code: int) -> None:
    """408, 429, 500, 502, 503, and 504 are all retried."""
    assert is_transient(FakeApiError(code))


@pytest.mark.parametrize("code", sorted(NON_RETRYABLE_STATUS_CODES))
def test_bad_request_statuses_are_not_transient(code: int) -> None:
    """400, 401, 403, and 404 are never retried."""
    assert not is_transient(FakeApiError(code))


def test_status_code_is_read_from_code_attribute() -> None:
    """The SDK's ``code`` attribute is the primary source."""
    assert status_code_of(FakeApiError(503)) == 503


def test_status_code_is_read_from_response_status() -> None:
    """A response object is used when there is no ``code`` attribute."""

    class WithResponse(Exception):
        def __init__(self) -> None:
            super().__init__("gateway")
            self.response = type("R", (), {"status_code": 504})()

    assert status_code_of(WithResponse()) == 504


def test_status_code_falls_back_to_message() -> None:
    """A bare SDK string still yields a status."""
    assert status_code_of(RuntimeError("503 Service Unavailable")) == 503


def test_status_code_ignores_unrelated_numbers() -> None:
    """Numbers that are not statuses do not produce a false classification."""
    assert status_code_of(ValueError("model gemma-4-31b-it failed after 2500 tokens")) is None
    assert not is_transient(ValueError("model gemma-4-31b-it failed after 2500 tokens"))


def test_config_errors_are_never_transient() -> None:
    """A configuration error must reach the operator immediately."""
    assert not is_transient(BackendConfigError("KUBE_DOCTOR_MODEL is not set"))


def test_explicit_transient_marker_is_transient() -> None:
    """A caller can declare a failure retryable without a status code."""
    assert is_transient(BackendTransientError("overloaded"))


def test_phrase_only_failures_are_transient() -> None:
    """A socket-level failure with no status is still retryable."""
    assert is_transient(ConnectionError("connection reset by peer"))
    assert is_transient(TimeoutError("read timed out"))


# --------------------------------------------------------------------------- #
# retry-after
# --------------------------------------------------------------------------- #


def test_retry_after_seconds_from_header() -> None:
    """A numeric ``Retry-After`` header is honoured."""
    error = FakeApiError(503, headers={"retry-after": "7"})
    assert retry_after_seconds(error) == 7.0


def test_retry_after_seconds_from_attribute() -> None:
    """An explicit attribute is used when present."""
    error = FakeApiError(429)
    error.retry_after = 12
    assert retry_after_seconds(error) == 12.0


def test_retry_after_seconds_from_http_date() -> None:
    """The HTTP-date form is converted to a delay from now."""
    error = FakeApiError(503, headers={"Retry-After": "Wed, 01 Feb 2028 00:00:10 GMT"})
    delay = retry_after_seconds(error)
    assert delay is not None
    assert 0 < delay <= RETRY_AFTER_MAX_SECONDS


def test_retry_after_is_clamped() -> None:
    """An absurd Retry-After cannot park the CLI indefinitely."""
    assert retry_after_seconds(FakeApiError(503, headers={"retry-after": "99999"})) == (
        RETRY_AFTER_MAX_SECONDS
    )


def test_retry_after_absent() -> None:
    """No header means no server-requested delay."""
    assert retry_after_seconds(FakeApiError(503)) is None


# --------------------------------------------------------------------------- #
# backoff schedule
# --------------------------------------------------------------------------- #


def test_backoff_follows_the_documented_schedule() -> None:
    """Delays grow as 2, 4, 8, 16 and never exceed the nominal value."""
    rng = random.Random(1234)
    nominals = [2.0, 4.0, 8.0, 16.0]
    for attempt, nominal in enumerate(nominals, start=1):
        delay = backoff_delay(attempt, rng=rng)
        assert nominal / 2 <= delay <= nominal


def test_backoff_is_capped() -> None:
    """The cap holds however many attempts are made."""
    rng = random.Random(0)
    assert backoff_delay(10, cap=30.0, rng=rng) <= 30.0


def test_backoff_jitter_varies() -> None:
    """Jitter actually varies, so concurrent clients do not align."""
    rng = random.Random(7)
    samples = {backoff_delay(3, rng=rng) for _ in range(20)}
    assert len(samples) > 1


def test_default_backoff_base_is_two_seconds() -> None:
    """The first retry is about two seconds out."""
    assert DEFAULT_BACKOFF_BASE_SECONDS == 2.0


# --------------------------------------------------------------------------- #
# call_with_retries
# --------------------------------------------------------------------------- #


def test_retries_on_503_then_succeeds() -> None:
    """Two 503s then a success: the caller gets the text and sleeps twice."""
    outcomes: list[Any] = [FakeApiError(503), FakeApiError(503), "done"]
    attempts: list[int] = []
    slept: list[float] = []

    def operation() -> str:
        attempts.append(1)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    result = call_with_retries(
        operation, sleep=slept.append, rng=random.Random(0), backoff_base=2.0
    )
    assert result == "done"
    assert len(attempts) == 3
    assert len(slept) == 2
    assert slept[0] < slept[1], "backoff must grow between attempts"


def test_exhausts_five_attempts_on_persistent_503() -> None:
    """A service that never recovers costs five attempts and then raises."""
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        msg = "HTTP 503 Service Unavailable"
        raise FakeApiError(503, msg)

    slept: list[float] = []
    with pytest.raises(BackendRetryExhausted) as caught:
        call_with_retries(operation, sleep=slept.append, rng=random.Random(0))
    assert len(attempts) == DEFAULT_MAX_ATTEMPTS == 5
    assert len(slept) == 4, "four sleeps for five attempts"
    assert "after 5 attempts" in str(caught.value)
    assert caught.value.retryable is True


def test_does_not_retry_bad_request() -> None:
    """A 400 is raised on the first attempt."""
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        msg = "HTTP 400 Invalid JSON payload"
        raise FakeApiError(400, msg)

    with pytest.raises(FakeApiError):
        call_with_retries(operation, sleep=lambda _s: pytest.fail("must not sleep"))
    assert len(attempts) == 1


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_does_not_retry_client_errors(code: int) -> None:
    """Every client error is surfaced immediately, without sleeping."""
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        raise FakeApiError(code)

    with pytest.raises(FakeApiError):
        call_with_retries(operation, sleep=lambda _s: pytest.fail("must not sleep"))
    assert len(attempts) == 1


def test_retry_after_beats_the_computed_schedule() -> None:
    """A server-requested delay is used verbatim instead of the backoff."""
    slept: list[float] = []
    outcomes: list[Any] = [FakeApiError(503, headers={"retry-after": "9"}), "done"]

    def operation() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    call_with_retries(operation, sleep=slept.append, rng=random.Random(0), backoff_base=2.0)
    assert slept == [9.0]


def test_backend_transient_error_is_retried() -> None:
    """A BackendTransientError is retryable, which it was not before."""
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        if len(attempts) < 2:
            msg = "overloaded"
            raise BackendTransientError(msg)
        return "ok"

    assert call_with_retries(operation, sleep=lambda _s: None, rng=random.Random(0)) == "ok"
    assert len(attempts) == 2


# --------------------------------------------------------------------------- #
# timeout
# --------------------------------------------------------------------------- #


def test_timeout_defaults_to_120_seconds() -> None:
    """An unset KUBE_DOCTOR_TIMEOUT yields 120s."""
    assert resolve_timeout(None, {}) == 120.0
    assert DEFAULT_TIMEOUT_SECONDS == 120.0


def test_timeout_is_read_from_the_environment() -> None:
    """KUBE_DOCTOR_TIMEOUT overrides the default."""
    assert resolve_timeout(None, {"KUBE_DOCTOR_TIMEOUT": "30"}) == 30.0


def test_explicit_timeout_wins_over_the_environment() -> None:
    """A CLI flag beats the environment variable."""
    assert resolve_timeout(45.0, {"KUBE_DOCTOR_TIMEOUT": "30"}) == 45.0


@pytest.mark.parametrize("raw", ["0", "-5", "abc", ""])
def test_blank_timeout_falls_back_to_the_default(raw: str) -> None:
    """An empty value means "unset"; an invalid one is a config error."""
    if raw:
        with pytest.raises(BackendConfigError, match="KUBE_DOCTOR_TIMEOUT"):
            resolve_timeout(None, {"KUBE_DOCTOR_TIMEOUT": raw})
    else:
        assert resolve_timeout(None, {"KUBE_DOCTOR_TIMEOUT": raw}) == DEFAULT_TIMEOUT_SECONDS


def test_gemini_backend_reads_timeout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend picks the environment timeout up at construction."""
    monkeypatch.setenv("KUBE_DOCTOR_TIMEOUT", "17")
    backend = GeminiBackend(model="gemma-4-31b-it", api_key="k", client_factory=lambda _k: object())
    assert backend.timeout_seconds == 17.0


def test_gemini_client_gets_http_options_in_milliseconds() -> None:
    """The real client construction path passes HttpOptions(timeout=...)."""
    captured: dict[str, Any] = {}

    def recording_client(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "client"

    import google.genai as genai_module

    original = genai_module.Client
    genai_module.Client = recording_client  # type: ignore[assignment]
    try:
        backend = GeminiBackend(
            model="gemma-4-31b-it", api_key="k", timeout=12.5, client_factory=None
        )
        client = backend._create_client()
    finally:
        genai_module.Client = original  # type: ignore[assignment]
    assert client == "client"
    http_options = captured["http_options"]
    assert getattr(http_options, "timeout", None) == 12_500


def test_openai_compat_also_honours_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local backend uses the same resolved timeout."""
    monkeypatch.setenv("KUBE_DOCTOR_TIMEOUT", "8")
    backend = OpenAICompatBackend(
        model="m", base_url="http://x/v1", client_factory=lambda _s: object()
    )
    assert backend.timeout_seconds == 8.0


# --------------------------------------------------------------------------- #
# fallback model
# --------------------------------------------------------------------------- #


def test_fallback_model_is_read_from_the_environment() -> None:
    """KUBE_DOCTOR_FALLBACK_MODEL is honoured."""
    assert resolve_fallback_model(None, {"KUBE_DOCTOR_FALLBACK_MODEL": "gemma-3-27b-it"}) == (
        "gemma-3-27b-it"
    )


def test_fallback_model_is_optional() -> None:
    """An unset fallback means no fallback."""
    assert resolve_fallback_model(None, {}) is None
    assert resolve_fallback_model(None, {"KUBE_DOCTOR_FALLBACK_MODEL": "  "}) is None


def test_survives_two_503s_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The requested case: a busy service recovers within the retry budget."""
    backend, client = gemini_backend([FakeApiError(503), FakeApiError(503), '{"ok": true}'])
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    assert backend.generate("hello") == '{"ok": true}'
    assert len(client.models.calls) == 3
    assert len(slept) == 2
    assert backend.model_id == "gemma-4-31b-it"
    assert backend.used_fallback is False


def test_always_500_raises_after_five_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The requested case: a service that never recovers ends the run."""
    backend, client = gemini_backend(
        [FakeApiError(500) for _ in range(5)], model="gemma-4-26b-a4b-it"
    )
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    with pytest.raises(BackendRetryExhausted) as caught:
        backend.generate("hello")
    assert len(client.models.calls) == 5
    assert len(slept) == 4
    assert caught.value.retryable is True
    assert backend.used_fallback is False


def test_fallback_is_tried_once_and_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A busy primary hands over to the fallback, and says so on stderr."""
    backend, client = gemini_backend(
        [FakeApiError(503) for _ in range(5)] + ['{"from": "fallback"}'],
        fallback_model="gemma-3-27b-it",
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert backend.generate("hello") == '{"from": "fallback"}'
    # Five primary attempts, then exactly one fallback attempt.
    assert len(client.models.calls) == 6
    assert client.models.calls[-1]["model"] == "gemma-3-27b-it"
    assert backend.model_id == "gemma-3-27b-it"
    assert backend.primary_model_id == "gemma-4-31b-it"
    assert backend.used_fallback is True
    stderr = capsys.readouterr().err
    assert "fallback model" in stderr
    assert "gemma-3-27b-it" in stderr


def test_fallback_flag_resets_between_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """A later successful primary call is no longer a fallback."""
    backend, _client = gemini_backend(
        [FakeApiError(503), '{"primary": true}'], fallback_model="gemma-3-27b-it"
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    backend.generate("hello")
    assert backend.model_id == "gemma-4-31b-it"
    assert backend.used_fallback is False


def test_no_fallback_configured_means_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a fallback the exhausted error reaches the caller."""
    backend, client = gemini_backend([FakeApiError(503) for _ in range(5)])
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(BackendRetryExhausted):
        backend.generate("hello")
    assert len(client.models.calls) == 5


def test_client_error_does_not_trigger_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 fails fast; another model would reject the same request."""
    backend, client = gemini_backend(
        [FakeApiError(400, "invalid payload")], fallback_model="gemma-3-27b-it"
    )
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("must not sleep"))
    with pytest.raises(FakeApiError):
        backend.generate("hello")
    assert len(client.models.calls) == 1
    assert backend.used_fallback is False


def test_fallback_failure_reports_both_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """If both models are unavailable the error names both."""
    backend, _client = gemini_backend(
        [FakeApiError(503) for _ in range(5)] + [FakeApiError(500, "fallback also down")],
        fallback_model="gemma-3-27b-it",
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(BackendError) as caught:
        backend.generate("hello")
    message = str(caught.value)
    assert "gemma-4-31b-it" in message
    assert "gemma-3-27b-it" in message


def test_fallback_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback gets one attempt: a second busy model is not worth waiting on."""
    outcomes: list[Any] = [FakeApiError(503) for _ in range(6)]
    backend, client = gemini_backend(outcomes, fallback_model="gemma-3-27b-it")
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    with pytest.raises(BackendError):
        backend.generate("hello")
    assert len(client.models.calls) == 6, "five primary attempts plus one fallback"
    assert len(slept) == 4, "no extra sleeps for the fallback attempt"


def test_fallback_receives_the_same_image() -> None:
    """The fallback is given the screenshot, not a degraded text-only prompt."""
    backend, client = gemini_backend(
        [FakeApiError(503), '{"ok": true}'],
        fallback_model="gemma-3-27b-it",
        max_attempts=1,
    )
    backend.generate("describe", b"\x89PNG", "image/png")
    primary_call, fallback_call = client.models.calls
    assert primary_call["model"] == "gemma-4-31b-it"
    assert fallback_call["model"] == "gemma-3-27b-it"
    assert fallback_call["contents"] == primary_call["contents"]


def test_fallback_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """KUBE_DOCTOR_FALLBACK_MODEL configures the net without a flag."""
    monkeypatch.setenv("KUBE_DOCTOR_FALLBACK_MODEL", "gemma-3-27b-it")
    monkeypatch.setattr("time.sleep", lambda _s: None)
    backend, _client = gemini_backend([FakeApiError(429) for _ in range(5)] + ["done"])
    backend.generate("hello")
    assert backend.fallback_model_id == "gemma-3-27b-it"
    assert backend.used_fallback is True


# --------------------------------------------------------------------------- #
# log silencing
# --------------------------------------------------------------------------- #


def test_third_party_loggers_are_pinned_to_warning() -> None:
    """httpx, httpcore, and google_genai never drop below WARNING."""
    original = {name: logging.getLogger(name).level for name in THIRD_PARTY_LOGGERS}
    try:
        for name in THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        silence_third_party_loggers()
        for name in THIRD_PARTY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        for name, level in original.items():
            logging.getLogger(name).setLevel(level)


def test_cli_verbose_still_silences_third_party_loggers(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """--verbose does not re-enable SDK debug output."""
    from kube_doctor import cli

    original = {name: logging.getLogger(name).level for name in THIRD_PARTY_LOGGERS}
    try:
        cli._configure_logging(verbose=True)
        for name in THIRD_PARTY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
        assert logging.getLogger("kube_doctor.pipeline").isEnabledFor(logging.DEBUG)
    finally:
        for name, level in original.items():
            logging.getLogger(name).setLevel(level)
