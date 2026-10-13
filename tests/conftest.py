"""Shared pytest fixtures."""

from __future__ import annotations

import base64
import io
import zlib

import pytest
from PIL import Image

from kube_doctor.backends.base import Backend, BackendError


def png_bytes(
    color: tuple[int, int, int] = (10, 20, 30), size: tuple[int, int] = (32, 16)
) -> bytes:
    """Build a small in-memory PNG so tests never touch the filesystem."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def sample_png() -> bytes:
    """Return raw PNG bytes."""
    return png_bytes()


@pytest.fixture
def sample_png_b64() -> str:
    """Return base64-encoded PNG bytes, as a data URL would embed them."""
    return base64.b64encode(png_bytes()).decode("ascii")


@pytest.fixture
def jpeg_bytes() -> bytes:
    """Return raw JPEG bytes."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeBackend(Backend):
    """Backend that replays canned replies, one per call."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []

    @property
    def model_id(self) -> str:
        """Report a fixed fake model id."""
        return "fake-model-v1"

    def generate(
        self, prompt: str, image_bytes: bytes | None = None, mime: str | None = None
    ) -> str:
        """Record the call and return the next canned reply."""
        self.calls.append({"prompt": prompt, "image": image_bytes, "mime": mime})
        if not self.replies:
            msg = "FakeBackend ran out of canned replies"
            raise BackendError(msg)
        return self.replies.pop(0)


@pytest.fixture
def fake_backend_factory():
    """Return a factory building a ``FakeBackend`` from canned replies."""
    return FakeBackend


def png_with_text_blob(payload: str) -> bytes:
    """Return PNG bytes whose IDAT contains ``payload``.

    Used to build log-ish binary fixtures without shipping binary test data.
    Not a valid readable image; only the byte payload matters for redaction tests.
    """
    raw = zlib.compress(payload.encode("utf-8"))
    chunks = [b"\x89PNG\r\n\x1a\n"]
    for chunk_type, data in ((b"IDAT", raw),):
        body = chunk_type + data
        chunks.append(len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big"))
    return b"".join(chunks)
