"""Preflight checks against a mocked Servarr API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import seedpath.preflight as preflight_module
from seedpath.config import Settings
from seedpath.preflight import CheckResult, run_preflight
from tests.support import make_settings

BASE_PAYLOADS: dict[str, Any] = {
    "downloadclient_config": {"enableCompletedDownloadHandling": False},
    "mediamanagement": {
        "createEmptyMovieFolders": True,
        "setPermissionsLinux": True,
        "chmodFolder": "775",
        "importExtraFiles": False,
        "deleteEmptyFolders": False,
        "autoRenameFolders": False,
    },
    "naming": {"renameMovies": False, "replaceIllegalCharacters": True},
    "downloadclient": [
        {
            "name": "rTorrent",
            "implementation": "RTorrent",
            "enable": True,
            "fields": [
                {"name": "addStopped", "value": True},
                {"name": "category", "value": "radarr"},
                {"name": "directory", "value": ""},
            ],
        }
    ],
    "remotepathmapping": [],
    "rootfolder": [{"path": "/data/movies"}],
    "health": [{"type": "warning", "message": "Completed Download Handling is disabled"}],
}


def _handler(payloads: dict[str, Any]) -> httpx.MockTransport:
    endpoints = {
        "config/downloadclient": payloads["downloadclient_config"],
        "config/mediamanagement": payloads["mediamanagement"],
        "config/naming": payloads["naming"],
        "downloadclient": payloads["downloadclient"],
        "remotepathmapping": payloads["remotepathmapping"],
        "rootfolder": payloads["rootfolder"],
        "health": payloads["health"],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for suffix in sorted(endpoints, key=len, reverse=True):
            if path.endswith(suffix):
                return httpx.Response(200, json=endpoints[suffix])
        return httpx.Response(404, json={"message": f"unexpected path {path}"})

    return httpx.MockTransport(handle)


async def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> list[CheckResult]:
    payloads = {**BASE_PAYLOADS, **overrides}
    settings = make_settings(
        tmp_path,
        sonarr_url=None,
        sonarr_api_key=None,
        category_map={"radarr": "radarr"},
    )

    async def fake_rtorrent(settings: Settings) -> list[CheckResult]:
        return [CheckResult("rtorrent", "Version", "pass", "0.9.8")]

    monkeypatch.setattr(preflight_module, "_check_rtorrent", fake_rtorrent)
    async with httpx.AsyncClient(transport=_handler(payloads)) as client:
        return await run_preflight(settings, arr_http=client)


def _failures(results: list[CheckResult]) -> list[CheckResult]:
    return [result for result in results if result.status == "fail"]


async def test_good_configuration_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = await _run(tmp_path, monkeypatch)
    assert _failures(results) == []
    checks = {result.check for result in results}
    assert "Completed Download Handling" in checks
    assert "Add stopped" in checks
    assert "Rename media files" in checks


async def test_completed_download_handling_must_be_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = await _run(
        tmp_path,
        monkeypatch,
        downloadclient_config={"enableCompletedDownloadHandling": True},
    )
    assert [result.check for result in _failures(results)] == ["Completed Download Handling"]


async def test_rename_must_be_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = await _run(
        tmp_path, monkeypatch, naming={"renameMovies": True, "renameEpisodes": True}
    )
    assert "Rename media files" in {result.check for result in _failures(results)}


async def test_add_stopped_must_be_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clients = [
        {
            "name": "rTorrent",
            "implementation": "RTorrent",
            "enable": True,
            "fields": [
                {"name": "addStopped", "value": False},
                {"name": "category", "value": "radarr"},
                {"name": "directory", "value": ""},
            ],
        }
    ]
    results = await _run(tmp_path, monkeypatch, downloadclient=clients)
    assert "Add stopped" in {result.check for result in _failures(results)}


async def test_category_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clients = [
        {
            "name": "rTorrent",
            "implementation": "RTorrent",
            "enable": True,
            "fields": [
                {"name": "addStopped", "value": True},
                {"name": "category", "value": "tv-sonarr"},
                {"name": "directory", "value": ""},
            ],
        }
    ]
    results = await _run(tmp_path, monkeypatch, downloadclient=clients)
    assert "Category" in {result.check for result in _failures(results)}


async def test_remote_path_mappings_must_be_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = await _run(
        tmp_path,
        monkeypatch,
        remotepathmapping=[{"host": "/downloads", "container": "/data"}],
    )
    assert "Remote path mappings" in {result.check for result in _failures(results)}


async def test_root_folder_outside_library_roots_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = await _run(tmp_path, monkeypatch, rootfolder=[{"path": "/mnt/not-the-library"}])
    assert "Root folders" in {result.check for result in _failures(results)}


async def test_empty_library_roots_fails_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        tmp_path,
        sonarr_url=None,
        sonarr_api_key=None,
        category_map={"radarr": "radarr"},
        library_roots=[],
    )

    async def fake_rtorrent(settings: Settings) -> list[CheckResult]:
        return [CheckResult("rtorrent", "Version", "pass", "0.9.8")]

    monkeypatch.setattr(preflight_module, "_check_rtorrent", fake_rtorrent)
    async with httpx.AsyncClient(transport=_handler(BASE_PAYLOADS)) as client:
        results = await run_preflight(settings, arr_http=client)

    failures = {result.check for result in results if result.status == "fail"}
    assert "Library roots" in failures
    assert "Root folders" in failures


async def test_unexpected_health_warning_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = await _run(
        tmp_path,
        monkeypatch,
        health=[
            {"type": "warning", "message": "Completed Download Handling is disabled"},
            {"type": "warning", "message": "Indexer unavailable"},
        ],
    )
    assert _failures(results) == []
    health = next(result for result in results if result.check == "Health")
    assert health.status == "warn"
    assert "Indexer unavailable" in health.detail
