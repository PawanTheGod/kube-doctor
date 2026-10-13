"""Unified server tests: landing, console workspace, API, health.

Offline: no network, no credentials, no live model calls.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kube_doctor.web import build_web_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Build one test client for the module."""
    return TestClient(build_web_app(), raise_server_exceptions=False)


def test_landing_is_the_initial_page(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Understand what broke. Know what to investigate next." in body
    assert "/workspace" in body
    assert "No live cluster access" in body
    assert "Illustrative demo" in body


def test_landing_has_no_fabricated_metrics(client: TestClient):
    body = client.get("/").text.lower()
    for invented in ("testimonial", "uptime", "99.9%", "enterprise customers"):
        assert invented not in body


def test_landing_assets_serve(client: TestClient):
    assert "text/css" in client.get("/styles.css").headers["content-type"]
    assert "javascript" in client.get("/app.js").headers["content-type"]
    favicon = client.get("/favicon.svg")
    assert "svg" in favicon.headers["content-type"]
    assert "<svg" in favicon.text


def test_health_and_unknown_paths(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/no-such-page").status_code == 404


def test_workspace_mount_serves(client: TestClient):
    assert "text/html" in client.get("/workspace/").headers["content-type"]
    response = client.get("/workspace")
    assert response.status_code == 200
    body = response.text
    assert "Diagnostic Workspace" in body
    assert "workspace.js" in body


def test_workspace_app_calls_diagnose_api(client: TestClient):
    assert "/api/diagnose" in client.get("/workspace.js").text


def test_workspace_assets_serve(client: TestClient):
    assert "text/css" in client.get("/workspace.css").headers["content-type"]
    assert "javascript" in client.get("/workspace.js").headers["content-type"]


def test_backends_endpoint_lists_no_secrets(client: TestClient):
    assert client.get("/api/backends").json() == {"backends": ["gemini", "openai-compat"]}


def test_diagnose_rejects_empty_input(client: TestClient):
    response = client.post("/api/diagnose", data={"backend": "gemini"})
    assert response.status_code == 400
    assert set(response.json()) == {"error"}


def test_diagnose_rejects_unknown_backend(client: TestClient):
    response = client.post("/api/diagnose", data={"backend": "nope", "logs": "x"})
    assert response.status_code == 400


def test_diagnose_rejects_bad_file_type(client: TestClient):
    response = client.post(
        "/api/diagnose",
        files={"image": ("note.txt", b"hello", "text/plain")},
        data={"backend": "gemini"},
    )
    assert response.status_code == 400


_DIAGNOSE_OK = (
    '{"summary": "OOM.", "causes": [{"category": "oom_killed", '
    '"title": "t", "confidence": "high", "evidence": ["e"]}], '
    '"next_steps": [], "missing_info": [], "model_id": ""}'
)
_OBSERVE_LOGS = (
    '{"image_legible": false, "items": [], '
    '"notes": "no image supplied; diagnose from the logs alone"}'
)
_OBSERVE_IMAGE = (
    '{"image_legible": true, "items": [{"kind": "pod_status", '
    '"text": "OOMKilled 137", "source": "image"}], "notes": ""}'
)


def _fake_factory(replies):
    from tests.conftest import FakeBackend

    return lambda name: FakeBackend(list(replies))


def test_diagnose_logs_only_success(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "kube_doctor.web.build_backend", _fake_factory([_OBSERVE_LOGS, _DIAGNOSE_OK])
    )
    response = client.post("/api/diagnose", data={"logs": "OOMKilled 137"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["diagnosis"]["causes"][0]["category"] == "oom_killed"
    assert payload["backend"] == "FakeBackend"


def test_diagnose_image_upload_success(client: TestClient, monkeypatch):
    from tests.conftest import png_bytes

    monkeypatch.setattr(
        "kube_doctor.web.build_backend", _fake_factory([_OBSERVE_IMAGE, _DIAGNOSE_OK])
    )
    response = client.post(
        "/api/diagnose",
        files={"image": ("shot.png", png_bytes(), "image/png")},
        data={"backend": "gemini"},
    )
    assert response.status_code == 200
    assert response.json()["diagnosis"]["causes"][0]["category"] == "oom_killed"


def test_no_secrets_in_landing(client: TestClient):
    body = client.get("/").text + client.get("/app.js").text
    for shape in ("AIza", "sk-ant-", "sk-proj-", "hf_", "PRIVATE KEY"):
        assert shape not in body
