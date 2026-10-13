"""``kube-doctor`` command line interface.

Exit codes are part of the contract, because the Agent Skill script and the
eval harness branch on them:

* ``0`` diagnosis produced
* ``2`` bad input (missing file, unreadable image, neither image nor logs)
* ``3`` backend error (missing key, unknown model id, rate limit, empty reply)
* ``4`` the model's output did not validate, even after one repair attempt
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console
from rich.logging import RichHandler

from kube_doctor.backends.base import BackendError, silence_third_party_loggers
from kube_doctor.backends.factory import DEFAULT_BACKEND, build_backend
from kube_doctor.pipeline import InputError, ModelOutputError, run
from kube_doctor.render import render

#: Process exit codes, named for readability at the call sites.
EXIT_OK: Final[int] = 0
EXIT_BAD_INPUT: Final[int] = 2
EXIT_BACKEND_ERROR: Final[int] = 3
EXIT_INVALID_MODEL_OUTPUT: Final[int] = 4
#: ``-`` reads the image from stdin.
STDIN_MARKER: Final[str] = "-"

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Diagnose a Kubernetes problem from a screenshot plus optional logs. "
        "Read-only: never touches a cluster."
    ),
)
console = Console()
error_console = Console(stderr=True)


def _configure_logging(verbose: bool) -> None:
    """Send logs to stderr, or nowhere unless ``--verbose`` is passed.

    The HTTP and SDK loggers are pinned to WARNING either way, so ``--verbose``
    adds our observations and retry notices without also dumping every httpx
    request line.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=error_console, rich_tracebacks=True, show_path=verbose)],
        force=True,
    )
    silence_third_party_loggers()


def _read_stdin_image() -> bytes:
    """Read image bytes from stdin."""
    buffer = getattr(sys.stdin, "buffer", None)
    data = buffer.read() if buffer is not None else sys.stdin.read().encode("bytes")
    if not data:
        msg = "no image data received on stdin"
        raise InputError(msg)
    return data


@app.command()
def diagnose(
    image: Annotated[
        str,
        typer.Argument(
            metavar="IMAGE",
            help="Path to a screenshot, or '-' to read the image from stdin.",
            show_default=False,
        ),
    ],
    logs: Annotated[
        str | None,
        typer.Option("--logs", help="Path to a log file to consider alongside the screenshot."),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option("--namespace", help="Kubernetes namespace the screenshot is about."),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option("--context", help="Free text: what changed, what you already tried."),
    ] = None,
    backend: Annotated[
        str,
        typer.Option("--backend", help="Model backend: gemini or openai-compat."),
    ] = DEFAULT_BACKEND,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: markdown or json."),
    ] = "markdown",
    model: Annotated[
        str | None,
        typer.Option("--model", help="Model id override; defaults to KUBE_DOCTOR_MODEL."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="OpenAI-compatible base URL override."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help="Per-request timeout in seconds; defaults to KUBE_DOCTOR_TIMEOUT.",
        ),
    ] = None,
    fallback_model: Annotated[
        str | None,
        typer.Option(
            "--fallback-model",
            help=(
                "Model to try once if the primary is unavailable; "
                "defaults to KUBE_DOCTOR_FALLBACK_MODEL."
            ),
        ),
    ] = None,
    list_models: Annotated[
        bool,
        typer.Option("--list-models", help="List the model ids this API key can reach, then exit."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Log the intermediate observations to stderr."),
    ] = False,
) -> None:
    """Diagnose a Kubernetes problem from a screenshot and optional logs."""
    _configure_logging(verbose)
    logger = logging.getLogger("kube_doctor")

    if output_format not in {"markdown", "json"}:
        error_console.print(
            f"[red]--format must be 'markdown' or 'json', got {output_format!r}[/red]"
        )
        raise typer.Exit(EXIT_BAD_INPUT)

    if list_models:
        raise typer.Exit(_list_models())

    image_path: Path | None = None
    try:
        if image == STDIN_MARKER:
            from tempfile import NamedTemporaryFile

            handle = NamedTemporaryFile(suffix=".png", delete=False)  # noqa: SIM115
            with handle:
                handle.write(_read_stdin_image())
            image_path = Path(handle.name)
            logger.debug("read image from stdin into %s", image_path)
        else:
            image_path = Path(image)
            if not image_path.exists():
                msg = f"image not found: {image_path}"
                raise InputError(msg)
    except InputError as exc:
        error_console.print(f"[red]bad input:[/red] {exc}")
        raise typer.Exit(EXIT_BAD_INPUT) from exc

    if logs is not None and not Path(logs).exists():
        error_console.print(f"[red]bad input:[/red] log file not found: {logs}")
        raise typer.Exit(EXIT_BAD_INPUT)

    try:
        model_backend = build_backend(
            backend,
            model=model,
            base_url=base_url,
            timeout=timeout,
            fallback_model=fallback_model,
        )
    except BackendError as exc:
        error_console.print(f"[red]backend error:[/red] {exc}")
        raise typer.Exit(EXIT_BACKEND_ERROR) from exc

    try:
        result = run(
            image_path,
            model_backend,
            logs_path=logs,
            context=context or "",
            namespace=namespace or "",
        )
    except InputError as exc:
        error_console.print(f"[red]bad input:[/red] {exc}")
        raise typer.Exit(EXIT_BAD_INPUT) from exc
    except ModelOutputError as exc:
        error_console.print(f"[red]the model's output could not be repaired:[/red] {exc}")
        raise typer.Exit(EXIT_INVALID_MODEL_OUTPUT) from exc
    except BackendError as exc:
        error_console.print(f"[red]backend error:[/red] {exc}")
        raise typer.Exit(EXIT_BACKEND_ERROR) from exc

    output = render(result, output_format)
    if output_format == "json":
        print(output)
    else:
        console.print(output)
    if result.used_fallback:
        error_console.print(
            f"[yellow]note:[/yellow] the primary model {result.primary_model_id} was "
            f"unavailable, so this diagnosis came from the fallback "
            f"{result.model_id}. Re-run when the primary is healthy to confirm it."
        )
    if result.redactions:
        error_console.print(
            f"[yellow]note:[/yellow] redacted {result.redactions} secret value(s) before sending"
        )
    raise typer.Exit(EXIT_OK)


def _list_models() -> int:
    """Print the model ids reachable with the configured credentials."""
    import os

    from kube_doctor.backends.gemini import list_gemini_models

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        error_console.print("[red]GEMINI_API_KEY is not set.[/red]")
        return EXIT_BACKEND_ERROR
    try:
        models = list_gemini_models(api_key)
    except BackendError as exc:
        error_console.print(f"[red]backend error:[/red] {exc}")
        return EXIT_BACKEND_ERROR
    for name in models:
        print(name)
    error_console.print(
        "[dim]Export the one you want as KUBE_DOCTOR_MODEL, e.g. "
        "KUBE_DOCTOR_MODEL=<id>. Open-weight Gemma ids are the ones with 'gemma' in the name.[/dim]"
    )
    return EXIT_OK


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
