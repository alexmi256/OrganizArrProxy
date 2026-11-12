"""CLI smoke tests."""

from __future__ import annotations

import uvicorn
from typer.testing import CliRunner

import seedpath.cli as cli_module
from seedpath import __version__
from seedpath.cli import app
from seedpath.preflight import CheckResult

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_config_redacts_secrets(monkeypatch) -> None:
    monkeypatch.setenv("RADARR_URL", "http://radarr:7878")
    monkeypatch.setenv("RADARR_API_KEY", "super-secret")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "super-secret" not in result.stdout
    assert "**********" in result.stdout


def test_serve_passes_overrides_to_uvicorn(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(asgi_app: object, **kwargs: object) -> None:
        captured["app"] = asgi_app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(
        app, ["serve", "--host", "127.0.0.1", "--port", "9999", "--log-level", "DEBUG"]
    )
    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9999
    assert captured["log_config"] is None


def test_preflight_command(monkeypatch) -> None:
    async def fake_preflight(settings: object) -> list[CheckResult]:
        return [CheckResult("radarr", "Completed Download Handling", "pass")]

    seen: dict[str, object] = {}

    def fake_render(results: list[CheckResult], *, as_json: bool) -> None:
        seen["results"] = results
        seen["json"] = as_json

    monkeypatch.setattr(cli_module, "run_preflight", fake_preflight)
    monkeypatch.setattr(cli_module, "render", fake_render)
    result = runner.invoke(app, ["preflight", "--json"])
    assert result.exit_code == 0
    assert seen["json"] is True
    assert len(seen["results"]) == 1  # type: ignore[arg-type]


def test_preflight_exits_nonzero_on_failure(monkeypatch) -> None:
    async def fake_preflight(settings: object) -> list[CheckResult]:
        return [CheckResult("radarr", "Completed Download Handling", "fail")]

    monkeypatch.setattr(cli_module, "run_preflight", fake_preflight)
    monkeypatch.setattr(cli_module, "render", lambda results, *, as_json: None)
    result = runner.invoke(app, ["preflight"])
    assert result.exit_code == 1
