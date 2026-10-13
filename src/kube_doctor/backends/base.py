"""Backend protocol, error types, and the shared retry policy.

A backend is anything that can turn a prompt (plus an optional image) into
text. Everything above this layer works against :class:`Backend`, so tests can
substitute a fake and the pipeline stays testable offline.

The retry policy lives here rather than in a backend because the Gemini API and
any OpenAI-compatible endpoint fail the same way: a 5xx while the service is
overloaded, a 429 when the quota is momentarily exhausted, or a socket that
closes mid-request. What is deliberately *not* retried is a request that can
never succeed: a 400, 401, 403, or 404. Retrying those only burns the user's
quota and delays the error message they actually need to read.
"""

from __future__ import annotations

import logging
import os
import random
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Default per-request timeout in seconds. Vision models are slow to start.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0
#: Attempts per model: 1 initial call plus 4 retries.
DEFAULT_MAX_ATTEMPTS: Final[int] = 5
#: First backoff delay, doubled on each retry: about 2s, 4s, 8s, 16s.
DEFAULT_BACKOFF_BASE_SECONDS: Final[float] = 2.0
#: Upper bound for a single computed backoff delay.
DEFAULT_BACKOFF_MAX_SECONDS: Final[float] = 30.0
#: Upper bound for a server-requested ``Retry-After``, so one malformed header
#: cannot park the CLI for an hour.
RETRY_AFTER_MAX_SECONDS: Final[float] = 300.0

#: Statuses worth retrying: the service is busy or temporarily broken.
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})
#: Statuses that will never succeed on a retry, named explicitly because they are
#: the ones an operator most needs to see immediately.
NON_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({400, 401, 403, 404})
#: Matches an HTTP status embedded in an exception message.
_STATUS_IN_TEXT: Final[re.Pattern[str]] = re.compile(r"(?<!\d)([1-5]\d{2})(?!\d)")
#: Matches a ``Retry-After`` header echoed into an exception message.
_RETRY_AFTER_IN_TEXT: Final[re.Pattern[str]] = re.compile(
    r"(?i)retry[-_ ]after\s*[:=]?\s*(\d+(?:\.\d+)?)"
)
#: Phrases that mean "the service is busy" when no status code is available.
_TRANSIENT_PHRASES: Final[tuple[str, ...]] = (
    "rate limit",
    "rate-limit",
    "overloaded",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "internal error",
    "bad gateway",
    "gateway timeout",
    "deadline exceeded",
    "connection reset",
    "connection error",
    "server disconnected",
)
#: Third-party loggers pinned to WARNING even under ``--verbose``, so verbose
#: output shows the observe-stage observations and nothing else.
THIRD_PARTY_LOGGERS: Final[tuple[str, ...]] = ("httpx", "httpcore", "google_genai")


class BackendError(Exception):
    """Base class for every backend failure surfaced to the CLI."""


class BackendConfigError(BackendError):
    """Missing or invalid configuration, for example an absent API key."""


class BackendTransientError(BackendError):
    """A retryable failure: rate limits, timeouts, and 5xx responses."""


class BackendInvalidOutputError(BackendError):
    """The model replied, but the reply was unusable."""


class BackendRetryExhausted(BackendError):
    """Every attempt failed.

    ``last_error`` is the error that ended the sequence, so a caller can still
    ask whether the final failure was retryable, which is what decides whether
    falling back to another model is worth attempting.
    """

    def __init__(self, message: str, last_error: BaseException | None = None) -> None:
        """Record the message and the error that ended the attempt sequence."""
        super().__init__(message)
        self.last_error = last_error

    @property
    def retryable(self) -> bool:
        """Report whether the final failure was itself retryable."""
        return is_transient(self.last_error)


def warn_to_stderr(message: str) -> None:
    """Print a warning straight to stderr.

    Used for facts the operator must see regardless of how logging happens to be
    configured, such as a diagnosis produced by a fallback model rather than by
    the primary one.
    """
    print(f"warning: {message}", file=sys.stderr)


def silence_third_party_loggers() -> None:
    """Pin the noisy HTTP and SDK loggers to WARNING.

    Called after logging is configured, including in verbose mode, so that
    ``--verbose`` surfaces the intermediate observations and nothing else.
    """
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


@runtime_checkable
class Backend(Protocol):
    """Minimal contract every model backend implements."""

    @property
    def model_id(self) -> str:
        """Identifier of the model that answered the most recent call."""
        ...

    def generate(
        self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
    ) -> str:
        """Return the model's raw text reply for ``prompt``.

        ``image_bytes`` is the screenshot to look at, if any. Implementations
        raise :class:`BackendError` subclasses on failure.
        """
        ...


def _as_status_code(value: Any) -> int | None:
    """Coerce an attribute that may hold a status code into an ``int``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 100 <= value <= 599 else None
    if isinstance(value, str):
        digits = value.strip()
        if digits.isdigit():
            code = int(digits)
            return code if 100 <= code <= 599 else None
    inner = getattr(value, "value", None)
    if inner is not None and inner is not value:
        return _as_status_code(inner)
    return None


def status_code_of(exc: BaseException) -> int | None:
    """Extract the HTTP status carried by an exception, if there is one.

    Checks the shapes the SDKs in play actually use: a ``code``/``status_code``
    attribute, a response object with a status, and finally the message text.
    """
    for attribute in ("code", "status_code", "status"):
        code = _as_status_code(getattr(exc, attribute, None))
        if code is not None:
            return code
    response = getattr(exc, "response", None)
    if response is not None:
        for attribute in ("status_code", "status"):
            code = _as_status_code(getattr(response, attribute, None))
            if code is not None:
                return code
    match = _STATUS_IN_TEXT.search(f"{type(exc).__name__}: {exc}")
    return int(match.group(1)) if match else None


def _parse_retry_after(value: Any) -> float | None:
    """Interpret a ``Retry-After`` value as seconds to wait from now."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return max(0.0, min(float(value), RETRY_AFTER_MAX_SECONDS))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit():
        return max(0.0, min(float(text), RETRY_AFTER_MAX_SECONDS))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:  # pragma: no cover - older stdlib returned None
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = (when - datetime.now(UTC)).total_seconds()
    return max(0.0, min(seconds, RETRY_AFTER_MAX_SECONDS))


def retry_after_seconds(exc: BaseException) -> float | None:
    """Return the server-requested delay for a failed call, if it sent one.

    Supports both forms of ``Retry-After``: a delay in seconds, and an HTTP date.
    """
    direct = _parse_retry_after(getattr(exc, "retry_after", None))
    if direct is not None:
        return direct
    for container in (getattr(exc, "response", None), exc):
        headers = getattr(container, "headers", None)
        if not headers:
            continue
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
            try:
                parsed = _parse_retry_after(headers.get(key))
            except (AttributeError, TypeError):  # pragma: no cover - exotic header maps
                continue
            if parsed is not None:
                return parsed
    match = _RETRY_AFTER_IN_TEXT.search(str(exc))
    return _parse_retry_after(match.group(1)) if match else None


def is_transient(exc: BaseException | None) -> bool:
    """Report whether an exception is worth retrying.

    A status code, when one is present, decides outright. Without one, the error
    text is inspected for the phrases an overloaded service actually produces.
    Configuration and output errors are never retried.
    """
    if exc is None:
        return False
    if isinstance(exc, BackendConfigError | BackendInvalidOutputError):
        return False
    if isinstance(exc, BackendTransientError):
        return True
    code = status_code_of(exc)
    if code is not None:
        if code in NON_RETRYABLE_STATUS_CODES:
            return False
        return code in RETRYABLE_STATUS_CODES
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(phrase in text for phrase in _TRANSIENT_PHRASES)


def backoff_delay(
    attempt: int,
    *,
    base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    cap: float = DEFAULT_BACKOFF_MAX_SECONDS,
    rng: random.Random | None = None,
) -> float:
    """Return the jittered delay to wait after a failed attempt.

    The nominal schedule is ``base * 2 ** (attempt - 1)``, capped. Jitter spreads
    each delay over ``[nominal / 2, nominal]``, so retries from concurrent
    invocations do not arrive together and the delay never exceeds the schedule
    the log message advertises.
    """
    random_source = rng or random
    nominal = min(cap, base * (2 ** (max(1, attempt) - 1)))
    return random_source.uniform(nominal / 2, nominal)


def call_with_retries(
    operation: Callable[[], str],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    backoff_max: float = DEFAULT_BACKOFF_MAX_SECONDS,
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Run ``operation``, retrying transient failures with jittered backoff.

    A ``Retry-After`` on the failing response takes precedence over the computed
    schedule, because the server knows when it will be back. Errors that cannot
    succeed on a retry are re-raised immediately.

    ``sleep`` and ``rng`` default to ``time.sleep`` and :mod:`random`, resolved
    at call time so a test can substitute either without waiting.
    """
    sleeper = sleep if sleep is not None else time.sleep
    random_source = rng if rng is not None else random
    attempts = max(1, max_attempts)
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not is_transient(exc):
                raise
            last_error = exc
        if attempt == attempts:
            break
        requested = retry_after_seconds(last_error) if last_error is not None else None
        delay = (
            requested
            if requested is not None
            else backoff_delay(attempt, base=backoff_base, cap=backoff_max, rng=random_source)
        )
        logger.warning(
            "attempt %d/%d failed (%s), retrying in %.1fs",
            attempt,
            attempts,
            last_error,
            delay,
        )
        sleeper(delay)
    msg = f"backend failed after {attempts} attempts: {last_error}"
    raise BackendRetryExhausted(msg, last_error) from last_error


def resolve_model_id(explicit: str | None = None, env: dict[str, str] | None = None) -> str:
    """Resolve the model id, refusing to guess.

    The model id is never hardcoded: Gemma model ids differ per API surface and
    per release, so callers must set ``KUBE_DOCTOR_MODEL`` or pass one in.
    """
    environment = env if env is not None else dict(os.environ)
    resolved = (explicit or environment.get("KUBE_DOCTOR_MODEL") or "").strip()
    if not resolved:
        msg = (
            "KUBE_DOCTOR_MODEL is not set. Model ids are not hardcoded on purpose: "
            "run `kube-doctor - --list-models` to see the ids your key can reach, "
            "then export the one you want as KUBE_DOCTOR_MODEL."
        )
        raise BackendConfigError(msg)
    return resolved


def resolve_fallback_model(
    explicit: str | None = None, env: dict[str, str] | None = None
) -> str | None:
    """Return the optional fallback model id, or ``None`` when it is unset.

    Read from ``KUBE_DOCTOR_FALLBACK_MODEL``. A fallback is consulted only after
    the primary has exhausted its retries on a retryable error, so it is a safety
    net for an overloaded service rather than a silent model switch.
    """
    environment = env if env is not None else dict(os.environ)
    value = (explicit or environment.get("KUBE_DOCTOR_FALLBACK_MODEL") or "").strip()
    return value or None


def resolve_timeout(explicit: float | None = None, env: dict[str, str] | None = None) -> float:
    """Resolve the per-request timeout, in seconds, from the environment.

    Read from ``KUBE_DOCTOR_TIMEOUT``, defaulting to
    :data:`DEFAULT_TIMEOUT_SECONDS`. A non-numeric or non-positive value is a
    configuration error rather than a setting that is silently ignored.
    """
    environment = env if env is not None else dict(os.environ)
    if explicit is not None:
        return _validate_timeout(explicit, "timeout")
    raw = (environment.get("KUBE_DOCTOR_TIMEOUT") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError as exc:
        msg = f"KUBE_DOCTOR_TIMEOUT must be a positive number of seconds, got {raw!r}."
        raise BackendConfigError(msg) from exc
    return _validate_timeout(seconds, "KUBE_DOCTOR_TIMEOUT")


def _validate_timeout(value: float, source: str) -> float:
    """Reject a non-positive or non-finite timeout."""
    if value != value or value in {float("inf"), float("-inf")} or value <= 0:
        msg = f"{source} must be a positive number of seconds, got {value!r}."
        raise BackendConfigError(msg)
    return float(value)


def require_env(name: str, env: dict[str, str] | None = None) -> str:
    """Return a non-empty environment variable or raise a config error."""
    environment = env if env is not None else dict(os.environ)
    value = (environment.get(name) or "").strip()
    if not value:
        msg = f"{name} is not set; export it before running the diagnosis."
        raise BackendConfigError(msg)
    return value


def describe(backend: Backend) -> dict[str, Any]:
    """Return a small serialisable description, used in logs and evals."""
    return {
        "model_id": backend.model_id,
        "primary_model_id": getattr(backend, "primary_model_id", backend.model_id),
        "used_fallback": bool(getattr(backend, "used_fallback", False)),
        "backend": type(backend).__name__,
    }
