"""Command line interface."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Annotated

import typer
from pydantic import ValidationError

from seedpath import __version__
from seedpath.api.app import create_app
from seedpath.config import Settings
from seedpath.logging import configure_logging, get_logger
from seedpath.preflight import render, run_preflight

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Place *arr grabs directly in the media library and seed from there.",
)
log = get_logger(__name__)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port.")] = None,
    log_level: Annotated[str | None, typer.Option(help="DEBUG, INFO, WARNING, ...")] = None,
) -> None:
    """Run the proxy and its workers."""
    settings = Settings()
    overrides: dict[str, object] = {}
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    if log_level is not None:
        overrides["log_level"] = log_level
    if overrides:
        settings = settings.model_copy(update=overrides)
    configure_logging(settings.log_level, settings.log_format)

    if not settings.apps:
        log.warning("startup.no_apps", hint="set RADARR_URL/RADARR_API_KEY etc.")
    if not settings.library_roots:
        log.warning("startup.no_library_roots", hint="placement will fail closed")

    import uvicorn

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )


@app.command()
def preflight(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Check every configured *arr app against the required settings."""
    settings = Settings()
    configure_logging("WARNING", "console")
    results = asyncio.run(run_preflight(settings))
    render(results, as_json=as_json)
    failures = [result for result in results if result.status == "fail"]
    raise typer.Exit(code=1 if failures else 0)


@app.command(name="config")
def show_config() -> None:
    """Print the effective configuration with secrets redacted."""
    settings = Settings()
    typer.echo(json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True))


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


def main() -> None:
    """Console-script entry point.

    Accepts the flags documented in BRIEF.md (``seedpath --preflight``) as
    aliases for the subcommands.
    """
    argv = sys.argv[1:]
    if argv and argv[0] == "--preflight":
        argv[0] = "preflight"
    elif argv and argv[0] in {"--version", "-V"}:
        argv[0] = "version"
    try:
        app(args=argv)
    except ValidationError as exc:
        typer.echo(f"configuration error:\n{exc}", err=True)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
