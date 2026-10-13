"""Tests for backend plumbing, retries, and the message builders.

The live tests are skipped unless ``KUBE_DOCTOR_LIVE=1`` and real credentials
are present, so CI stays offline.
"""

from __future__ import annotations

import base64
import os
import random
import typing

import pytest

from kube_doctor.backends.base import (
    DEFAULT_MAX_ATTEMPTS,
    Backend,
    BackendConfigError,
    BackendError,
    call_with_retries,
    describe,
    is_transient,
    require_env,
    resolve_model_id,
)
from kube_doctor.backends.factory import BACKEND_NAMES, DEFAULT_BACKEND, build_backend
from kube_doctor.backends.openai_compat import OpenAICompatBackend

# --------------------------------------------------------------------------- #
# env helpers
# --------------------------------------------------------------------------- #


def test_resolve_model_id_prefers_explicit() -> None:
    """An explicit model id wins over the environment."""
    assert resolve_model_id("gemma-4-x", {"KUBE_DOCTOR_MODEL": "other"}) == "gemma-4-x"


def test_resolve_model_id_falls_back_to_env() -> None:
    """The environment supplies the model id when none is passed."""
    assert resolve_model_id(None, {"KUBE_DOCTOR_MODEL": " gemma-4-y "}) == "gemma-4-y"


def test_resolve_model_id_refuses_to_guess() -> None:
    """An unset model id is a config error, never a hardcoded default."""
    with pytest.raises(BackendConfigError, match="KUBE_DOCTOR_MODEL"):
        resolve_model_id(None, {})


def test_require_env_rejects_blank() -> None:
    """A blank variable counts as unset."""
    with pytest.raises(BackendConfigError, match="TOKEN"):
        require_env("TOKEN", {"TOKEN": "   "})


def test_require_env_returns_value() -> None:
    """A set variable is returned verbatim."""
    assert require_env("TOKEN", {"TOKEN": "abc"}) == "abc"


# --------------------------------------------------------------------------- #
# retry policy
# --------------------------------------------------------------------------- #


def test_is_transient_detects_status_markers() -> None:
    """429 and 5xx messages are transient."""
    assert is_transient(RuntimeError("HTTP 429 rate limit"))
    assert is_transient(RuntimeError("503 Service Unavailable"))
    assert is_transient(TimeoutError("timed out"))


def test_is_transient_rejects_client_errors() -> None:
    """A 400 or a config error is not retried."""
    assert not is_transient(RuntimeError("400 Bad Request"))
    assert not is_transient(ValueError("bad model"))


def test_call_with_retries_returns_first_success() -> None:
    """A happy path runs exactly once."""
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        return "ok"

    assert call_with_retries(operation, sleep=lambda _s: None) == "ok"
    assert len(calls) == 1


def test_call_with_retries_retries_transient_then_succeeds() -> None:
    """A transient failure is retried and the eventual success is returned."""
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("429 rate limit")
        return "recovered"

    slept: list[float] = []
    result = call_with_retries(
        operation,
        sleep=slept.append,
        rng=random.Random(0),
    )
    assert result == "recovered"
    assert len(attempts) == 3
    assert slept[0] < slept[1], "backoff must grow between attempts"


def test_call_with_retries_raises_after_exhausting_attempts() -> None:
    """Persistent transient failures end in a BackendError."""
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        raise RuntimeError("503 unavailable")

    slept: list[float] = []
    with pytest.raises(BackendError, match=f"after {DEFAULT_MAX_ATTEMPTS} attempts"):
        call_with_retries(operation, sleep=slept.append, rng=random.Random(0))
    assert len(attempts) == DEFAULT_MAX_ATTEMPTS
    assert len(slept) == DEFAULT_MAX_ATTEMPTS - 1


def test_call_with_retries_does_not_retry_permanent_errors() -> None:
    """A ValueError surfaces immediately without sleeping."""
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        call_with_retries(operation, sleep=lambda _s: None)
    assert len(calls) == 1


def test_call_with_retries_does_not_swallow_backend_errors() -> None:
    """A BackendError subclass propagates unchanged."""

    def operation() -> str:
        msg = "not configured"
        raise BackendConfigError(msg)

    with pytest.raises(BackendConfigError):
        call_with_retries(operation, sleep=lambda _s: None)


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #


def test_backend_names_and_default() -> None:
    """The advertised backend names are stable."""
    assert BACKEND_NAMES == ("gemini", "openai-compat")
    assert DEFAULT_BACKEND == "gemini"


def test_build_backend_rejects_unknown_name() -> None:
    """An unknown backend name is a config error listing the valid names."""
    with pytest.raises(BackendConfigError, match="openai-compat"):
        build_backend("magic", model="m", api_key="k")


def test_build_backend_openai_compat() -> None:
    """The OpenAI-compatible backend is selected by several aliases."""
    backend = build_backend("openai-compat", model="gemma3:4b", base_url="http://x/v1")
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.base_url == "http://x/v1"
    assert build_backend("OLLAMA", model="m", base_url="http://x/v1").model_id == "m"


def test_build_backend_gemini_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting gemini without a key fails fast and names the variable."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(BackendConfigError, match="GEMINI_API_KEY"):
        build_backend("gemini", model="gemma-4-test")


# --------------------------------------------------------------------------- #
# message construction (no network)
# --------------------------------------------------------------------------- #


def test_openai_compat_embeds_image_as_data_url(sample_png: bytes) -> None:
    """Images become base64 data URLs alongside the prompt text."""
    backend = OpenAICompatBackend(
        model="m", base_url="http://x/v1", client_factory=lambda _s: object()
    )
    messages = backend._messages("look", sample_png, "image/png")
    content = messages[0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image_url"
    expected = base64.b64encode(sample_png).decode("ascii")
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{expected}"


def test_openai_compat_omits_image_when_absent() -> None:
    """A text-only call sends text content only."""
    backend = OpenAICompatBackend(
        model="m", base_url="http://x/v1", client_factory=lambda _s: object()
    )
    messages = backend._messages("text only", None, None)
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "text only"}]}]


def test_openai_compat_uses_placeholder_key_for_local_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local servers need no key, so a placeholder is sent instead of failing."""
    monkeypatch.delenv("KUBE_DOCTOR_API_KEY", raising=False)
    monkeypatch.delenv("KUBE_DOCTOR_BASE_URL", raising=False)
    backend = OpenAICompatBackend(model="m", client_factory=lambda _s: object())
    assert backend.base_url == "http://localhost:11434/v1"


def test_openai_compat_default_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """KUBE_DOCTOR_BASE_URL overrides the built-in localhost default."""
    monkeypatch.setenv("KUBE_DOCTOR_BASE_URL", "http://vllm.internal:8000/v1")
    backend = OpenAICompatBackend(model="m", client_factory=lambda _s: object())
    assert backend.base_url == "http://vllm.internal:8000/v1"


# --------------------------------------------------------------------------- #
# live tests (opt-in)
# --------------------------------------------------------------------------- #

requires_live = pytest.mark.skipif(
    os.environ.get("KUBE_DOCTOR_LIVE") != "1",
    reason="set KUBE_DOCTOR_LIVE=1 with real credentials to run live backend tests",
)


@requires_live
@pytest.mark.live
def test_live_gemini_image_call(sample_png: bytes) -> None:
    """One real image call through the Gemini backend."""
    backend = build_backend("gemini")
    reply = backend.generate("Describe this image in one short sentence.", sample_png, "image/png")
    assert reply.strip()


@requires_live
@pytest.mark.live
def test_live_openai_compat_image_call(sample_png: bytes) -> None:
    """One real image call through the local OpenAI-compatible backend."""
    backend = build_backend("openai-compat")
    reply = backend.generate("Describe this image in one short sentence.", sample_png, "image/png")
    assert reply.strip()


def test_fake_backend_satisfies_protocol(fake_backend_factory: typing.Any) -> None:
    """The test double structurally satisfies the Backend protocol."""
    fake = fake_backend_factory(["hi"])
    assert isinstance(fake, Backend)
    assert fake.generate("p") == "hi"
    assert describe(fake)["model_id"] == "fake-model-v1"
