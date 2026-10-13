"""Optional Gradio front end.

Install with the extra: ``pip install -e ".[ui]"``. This module calls
:func:`kube_doctor.pipeline.run` and does nothing else, so the CLI, the Agent
Skill, and the eval harness keep exactly one implementation of the pipeline.
Result presentation lives in :mod:`kube_doctor.ui.panels`, kept free of
Gradio imports so it stays unit-testable offline.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import gradio as gr

from kube_doctor.backends.base import BackendError
from kube_doctor.backends.factory import build_backend
from kube_doctor.pipeline import InputError, ModelOutputError, PipelineResult, run
from kube_doctor.ui.panels import (
    banner_html,
    body_markdown,
    commands_block,
    error_banner_html,
    metadata_dict,
    observations_markdown,
)

logger = logging.getLogger(__name__)

#: Backends offered in the dropdown, in the order they should be tried.
BACKEND_CHOICES = ["gemini", "openai-compat"]

#: Gradio 6 moved ``css``/``theme`` from ``Blocks()`` to ``launch()``.
#: Sniff the major version so the declared ``gradio>=4.36`` range keeps
#: working everywhere with warning-free logs.
try:
    _GRADIO_MAJOR = int(str(getattr(gr, "__version__", "0")).split(".")[0])
except ValueError:  # pragma: no cover - non-numeric dev version
    _GRADIO_MAJOR = 0

#: Enterprise design tokens: ivory ground, white panels, charcoal ink, one
#: restrained burnt-amber accent. Element selectors only, so the styling
#: survives Gradio version changes; no whole-page glassmorphism.
_CSS = """
.gradio-container { background: #f6f3ec !important; }
.ksd-wrap { max-width: 960px; margin: 0 auto; padding: 48px 24px 72px; }
.ksd-head { display: flex; justify-content: space-between; align-items:
    center; gap: 16px; margin-bottom: 32px; }
.ksd-brand { font-size: 15px; font-weight: 650; letter-spacing: -0.01em;
    color: #201d19; }
.ksd-home { color: inherit; text-decoration: none; }
.ksd-home:hover { color: #a34e12; }
.ksd-badge { font-size: 11px; font-weight: 600; letter-spacing: 0.12em;
    color: #57534a; border: 1px solid #d8d1bf; border-radius: 999px;
    padding: 6px 12px; background: #fffdf8; white-space: nowrap; }
.ksd-eyebrow { font-size: 12px; font-weight: 700; letter-spacing: 0.18em;
    color: #a34e12; margin-bottom: 12px; }
.ksd-hero { font-size: 38px; line-height: 1.1; font-weight: 700;
    letter-spacing: -0.025em; color: #201d19; margin: 0 0 12px; }
.ksd-sub { font-size: 16px; line-height: 1.55; color: #57534a; margin: 0 0
    10px; max-width: 64ch; }
.ksd-examples { font-size: 13px; color: #857e70; margin: 0 0 28px; }
.ksd-section { font-size: 13px; font-weight: 700; letter-spacing: 0.14em;
    color: #57534a; margin: 30px 0 12px; }
.ksd-panel { background: #fffdf8; border: 1px solid #e6e0d2;
    border-radius: 16px; padding: 20px;
    box-shadow: 0 1px 2px rgba(32, 29, 25, 0.05); }
.ksd-drop { border: 1.5px dashed #c9c1af; border-radius: 14px;
    background: #fbfaf6; }
.ksd-run button { width: 100%; background: #23201b !important; color:
    #fffdf8 !important; border-radius: 12px !important; font-size: 16px
    !important; font-weight: 600 !important; padding: 13px !important;
    margin-top: 20px; transition: background 0.2s ease, transform 0.15s
    ease; }
.ksd-run button:hover { background: #38332b !important; }
.ksd-run button:active { transform: scale(0.995); }
.ksd-banner { border-radius: 16px; padding: 20px 24px; margin-top: 8px;
    border: 1px solid #e6e0d2; background: #fffdf8;
    box-shadow: 0 1px 2px rgba(32, 29, 25, 0.05); }
.ksd-banner-eyebrow { font-size: 12px; font-weight: 700;
    letter-spacing: 0.14em; margin-bottom: 8px; }
.ksd-banner-title { font-size: 22px; font-weight: 700;
    letter-spacing: -0.015em; color: #201d19; margin-bottom: 8px; }
.ksd-banner-sub { font-size: 15px; line-height: 1.55; color: #57534a; }
.ksd-banner-meta { font-size: 12px; color: #857e70; margin-top: 10px; }
.ksd-banner--ok { border-left: 5px solid #2f6b3a; }
.ksd-banner--ok .ksd-banner-eyebrow { color: #2f6b3a; }
.ksd-banner--warn { border-left: 5px solid #a34e12; }
.ksd-banner--warn .ksd-banner-eyebrow { color: #a34e12; }
.ksd-banner--error { border-left: 5px solid #9c2b1f; }
.ksd-banner--error .ksd-banner-eyebrow { color: #9c2b1f; }
.ksd-body { background: #fffdf8; border: 1px solid #e6e0d2;
    border-radius: 16px; padding: 8px 24px 20px; margin-top: 16px;
    box-shadow: 0 1px 2px rgba(32, 29, 25, 0.05); }
.ksd-body h3 { font-size: 17px; color: #201d19; margin-top: 20px; }
.ksd-foot { margin-top: 44px; padding-top: 20px;
    border-top: 1px solid #e0d9c6; font-size: 13px; line-height: 1.6;
    color: #857e70; text-align: center; }
:focus-visible { outline: 2px solid #a34e12; outline-offset: 2px; }
@media (max-width: 640px) { .ksd-hero { font-size: 30px; } .ksd-wrap {
    padding: 32px 16px 56px; } }
"""

_HEADER_HTML = """
<div class="ksd-head"><div class="ksd-brand"><a class="ksd-home"
href="/">Kube Snapshot Doctor</a></div>
<div class="ksd-badge">READ-ONLY &middot; NO CLUSTER ACCESS</div></div>
"""

_HERO_HTML = """
<div class="ksd-eyebrow">KUBERNETES DIAGNOSTICS</div>
<h1 class="ksd-hero">Find the signal. Understand the failure.</h1>
<p class="ksd-sub">Share a screenshot of the failing workload and get a
ranked, evidence-backed diagnosis. Nothing is executed and nothing connects
to your cluster.</p>
<p class="ksd-examples">Try: a CrashLoopBackOff pod table &middot; an
ImagePullBackOff event list &middot; an OOMKilled describe output &middot;
a throttled CPU panel</p>
"""

_FOOTER_HTML = """
<div class="ksd-foot">Kube Snapshot Doctor only reads. It never accesses a
live Kubernetes cluster and never executes the commands it suggests.
Suggested commands are labelled read-only or mutating — review before
running.</div>
"""


def _failed(message: str) -> tuple[str, str, str, str, dict[str, Any]]:
    """Build the five outputs for a run that produced no diagnosis."""
    return (
        error_banner_html(message),
        f"**{message}**",
        "",
        "_Not available — the run ended before transcription._",
        {},
    )


def diagnose(
    image: object,
    logs: str | None,
    namespace: str | None,
    context: str | None,
    backend_name: str,
) -> tuple[str, str, str, str, dict[str, Any]]:
    """Run the pipeline and return status, body, commands, and details."""
    if image is None and not (logs or "").strip():
        return _failed("Upload a screenshot, or paste logs, before running the diagnosis.")
    try:
        backend: object = build_backend(backend_name or BACKEND_CHOICES[0])
    except BackendError as exc:
        return _failed(f"Backend error: {exc} Set the model and credentials, then try again.")
    try:
        result: PipelineResult = run(
            image,  # type: ignore[arg-type]
            backend,  # type: ignore[arg-type]
            logs_text=logs or None,
            namespace=namespace or "",
            context=context or "",
        )
    except InputError as exc:
        return _failed(f"Bad input: {exc}")
    except ModelOutputError as exc:
        return _failed(f"The model's output could not be repaired: {exc}")
    except BackendError as exc:
        return _failed(f"Backend error: {exc}")
    return (
        banner_html(result),
        body_markdown(result),
        commands_block(result),
        observations_markdown(result),
        metadata_dict(result),
    )


def build_app() -> gr.Blocks:
    """Build the Gradio app."""
    blocks_kwargs: dict[str, object] = {} if _GRADIO_MAJOR >= 6 else {"css": _CSS}
    with gr.Blocks(title="Kube Snapshot Doctor", **blocks_kwargs) as app:  # type: ignore[arg-type]
        with gr.Column(elem_classes=["ksd-wrap"]):
            gr.HTML(_HEADER_HTML)
            gr.HTML(_HERO_HTML)
            image_input = gr.Image(
                type="filepath",
                label="Screenshot — drag & drop, upload, or paste",
                sources=["upload", "clipboard"],
                elem_classes=["ksd-drop"],
            )
            logs_input = gr.Textbox(
                label="Logs, pod events, or error messages (optional)",
                lines=6,
                placeholder="kubectl logs <pod> --previous --tail=200",
                elem_classes=["ksd-panel"],
            )
            with gr.Accordion("Optional context", open=False):
                with gr.Row():
                    namespace_input = gr.Textbox(label="Namespace", scale=1)
                    backend_input = gr.Dropdown(
                        label="Backend",
                        choices=BACKEND_CHOICES,
                        value=BACKEND_CHOICES[0],
                        scale=1,
                    )
                context_input = gr.Textbox(
                    label="Context",
                    lines=2,
                    placeholder="What changed? What have you already tried?",
                )
            run_button = gr.Button(
                "Diagnose", variant="primary", size="lg", elem_classes=["ksd-run"]
            )
            gr.HTML('<div class="ksd-section">DIAGNOSIS</div>')
            status_output = gr.HTML(
                value='<div class="ksd-banner">'
                '<div class="ksd-banner-eyebrow">READY</div>'
                '<div class="ksd-banner-title">Awaiting input</div>'
                '<div class="ksd-banner-sub">Your ranked finding, evidence, '
                "and next steps will appear here.</div></div>"
            )
            body_output = gr.Markdown(elem_classes=["ksd-body"])
            commands_output = gr.Code(
                label="Suggested commands — review before running; nothing was executed",
                language="shell",
            )
            with gr.Accordion("Technical details", open=False):
                observations_output = gr.Markdown(label="Stage 1 observations")
                metadata_output = gr.JSON(label="Run metadata")
            gr.HTML(_FOOTER_HTML)
        run_button.click(
            fn=diagnose,
            inputs=[image_input, logs_input, namespace_input, context_input, backend_input],
            outputs=[
                status_output,
                body_output,
                commands_output,
                observations_output,
                metadata_output,
            ],
        )
    return app


def _server_port() -> int:
    """Return the port to listen on, honouring ``PORT`` for hosted platforms."""
    try:
        return int(os.environ.get("PORT", "7860"))
    except ValueError:
        return 7860


def main() -> None:
    """Launch the app."""
    logging.basicConfig(level=logging.WARNING)
    launch_kwargs: dict[str, object] = {"css": _CSS} if _GRADIO_MAJOR >= 6 else {}
    build_app().launch(  # type: ignore[arg-type]
        server_name="0.0.0.0", server_port=_server_port(), **launch_kwargs
    )


if __name__ == "__main__":  # pragma: no cover
    main()
