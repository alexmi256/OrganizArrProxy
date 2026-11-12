"""Runtime wiring and logging configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from seedpath.api.app import create_app
from seedpath.logging import configure_logging
from seedpath.runtime import build_runtime
from tests.support import make_settings


async def test_build_runtime_wires_clients(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runtime = await build_runtime(settings)
    try:
        assert set(runtime.apps) == {"radarr", "sonarr"}
        assert runtime.proxy is not None
        assert runtime.settings is settings
    finally:
        await runtime.aclose()


def test_app_builds_its_own_runtime(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, background=False)) as client:
        assert client.get("/livez").status_code == 200


@pytest.mark.parametrize("fmt", ["json", "console"])
def test_configure_logging(fmt: str) -> None:
    configure_logging("DEBUG", fmt)  # type: ignore[arg-type]


def test_configure_logging_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("NOTALEVEL")
