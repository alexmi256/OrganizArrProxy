"""Preflight: verify the *arr configuration seedpath depends on.

The checks mirror BRIEF.md §10. They are intentionally read-only except for
the state-directory probe, and report every failure rather than stopping at
the first one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from rich.console import Console
from rich.table import Table

from seedpath.clients.arr import ArrClient
from seedpath.clients.rtorrent import RTorrentClient
from seedpath.config import AppConfig, Settings
from seedpath.errors import ArrError
from seedpath.logging import get_logger
from seedpath.util import path_within_roots

log = get_logger(__name__)

Status = Literal["pass", "warn", "fail", "skip"]

CREATE_EMPTY_KEYS = {
    "radarr": "createEmptyMovieFolders",
    "whisparr": "createEmptyMovieFolders",
    "sonarr": "createEmptySeriesFolders",
    "lidarr": "createEmptyArtistFolders",
    "readarr": "createEmptyAuthorFolders",
}
AUTO_RENAME_KEYS = {"radarr": "autoRenameFolders", "whisparr": "autoRenameFolders"}


@dataclass(frozen=True, slots=True)
class CheckResult:
    app: str
    check: str
    status: Status
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"pass", "skip"}


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _field(client: dict[str, Any], name: str) -> Any:
    fields = client.get("fields")
    if not isinstance(fields, list):
        return None
    for field in fields:
        if isinstance(field, dict) and str(field.get("name", "")).lower() == name.lower():
            return field.get("value")
    return None


class _AppChecks:
    def __init__(self, settings: Settings, app: AppConfig, client: ArrClient) -> None:
        self._settings = settings
        self._app = app
        self._client = client
        self.results: list[CheckResult] = []

    def add(self, check: str, status: Status, detail: str = "") -> None:
        self.results.append(CheckResult(self._app.name, check, status, detail))

    async def _get(self, path: str) -> Any:
        return await self._client.get(path)

    async def run(self) -> list[CheckResult]:
        await self._completed_download_handling()
        await self._media_management()
        await self._naming()
        await self._download_client()
        await self._remote_path_mappings()
        await self._root_folders()
        await self._health()
        return self.results

    async def _completed_download_handling(self) -> None:
        try:
            data = await self._get("config/downloadclient")
        except ArrError as exc:
            self.add("Completed Download Handling", "fail", str(exc))
            return
        value = (
            _as_bool(data.get("enableCompletedDownloadHandling"))
            if isinstance(data, dict)
            else None
        )
        if value is False:
            self.add("Completed Download Handling", "pass", "disabled")
        elif value is True:
            self.add(
                "Completed Download Handling",
                "fail",
                "must be disabled, otherwise the importer moves the seeding file",
            )
        else:
            self.add("Completed Download Handling", "skip", "setting not exposed by this version")

    async def _media_management(self) -> None:
        try:
            data = await self._get("config/mediamanagement")
        except ArrError as exc:
            self.add("Media management", "fail", str(exc))
            return
        if not isinstance(data, dict):
            self.add("Media management", "fail", "unexpected response")
            return

        key = CREATE_EMPTY_KEYS[self._app.kind]
        value = _as_bool(data.get(key))
        if value is True:
            self.add("Create empty folders", "pass", key)
        else:
            self.add("Create empty folders", "fail", f"{key} must be enabled")

        value = _as_bool(data.get("setPermissionsLinux"))
        self.add(
            "Set permissions",
            "pass" if value else "fail",
            "setPermissionsLinux must be enabled",
        )

        chmod = str(data.get("chmodFolder", ""))
        if chmod in {"775", "0775"}:
            self.add("Folder permissions", "pass", chmod)
        else:
            self.add("Folder permissions", "fail", f"chmodFolder is {chmod!r}, expected 775")

        value = _as_bool(data.get("importExtraFiles"))
        self.add(
            "Import extra files",
            "pass" if value is False else "fail",
            "must be disabled so .nfo/.srt stay in the release folder",
        )

        value = _as_bool(data.get("deleteEmptyFolders"))
        self.add(
            "Delete empty folders",
            "pass" if value is False else "fail",
            "must be disabled",
        )

        auto_rename_key = AUTO_RENAME_KEYS.get(self._app.kind)
        if auto_rename_key:
            value = _as_bool(data.get(auto_rename_key))
            self.add(
                "Auto rename folders",
                "pass" if value is False else "fail",
                "must be disabled so the seeding folder is not renamed",
            )

    async def _naming(self) -> None:
        try:
            data = await self._get("config/naming")
        except ArrError as exc:
            self.add("Rename media files", "fail", str(exc))
            return
        if not isinstance(data, dict):
            self.add("Rename media files", "fail", "unexpected response")
            return
        enabled = [
            key
            for key, value in data.items()
            if "rename" in key.lower() and _as_bool(value) is True
        ]
        if enabled:
            self.add("Rename media files", "fail", f"still enabled: {', '.join(sorted(enabled))}")
        else:
            self.add("Rename media files", "pass", "no rename option enabled")

    async def _download_client(self) -> None:
        try:
            clients = await self._get("downloadclient")
        except ArrError as exc:
            self.add("rTorrent client", "fail", str(exc))
            return
        if not isinstance(clients, list):
            self.add("rTorrent client", "fail", "unexpected response")
            return
        rtorrent = next(
            (
                client
                for client in clients
                if isinstance(client, dict)
                and str(client.get("implementation", "")).lower() == "rtorrent"
            ),
            None,
        )
        if rtorrent is None:
            self.add("rTorrent client", "fail", "no rTorrent download client configured")
            return

        name = str(rtorrent.get("name", "rTorrent"))
        self.add(
            f"Client {name!r} enabled",
            "pass" if _as_bool(rtorrent.get("enable")) else "fail",
            "download client is disabled",
        )
        self.add(
            "Add stopped",
            "pass" if _as_bool(_field(rtorrent, "addStopped")) else "fail",
            "addStopped must be enabled so seedpath can retarget before download",
        )

        category = str(_field(rtorrent, "category") or "")
        expected = {
            category_name
            for category_name, app_name in self._settings.configured_category_map.items()
            if app_name == self._app.name
        }
        if not expected:
            self.add("Category", "warn", "no category maps to this app in CATEGORY_MAP")
        elif category in expected:
            self.add("Category", "pass", category)
        else:
            self.add(
                "Category",
                "fail",
                f"client category is {category!r}, expected one of {sorted(expected)}",
            )

        directory = str(_field(rtorrent, "directory") or "")
        if directory:
            self.add("Client directory", "warn", "should be empty; seedpath overrides it")
        else:
            self.add("Client directory", "pass", "empty")

    async def _remote_path_mappings(self) -> None:
        try:
            mappings = await self._get("remotepathmapping")
        except ArrError as exc:
            self.add("Remote path mappings", "fail", str(exc))
            return
        if isinstance(mappings, list) and not mappings:
            self.add("Remote path mappings", "pass", "none")
        else:
            self.add(
                "Remote path mappings",
                "fail",
                "must be empty; container paths have to agree instead",
            )

    async def _root_folders(self) -> None:
        try:
            folders = await self._get("rootfolder")
        except ArrError as exc:
            self.add("Root folders", "fail", str(exc))
            return
        if not isinstance(folders, list):
            self.add("Root folders", "fail", "unexpected response")
            return
        if not self._settings.library_roots:
            self.add("Root folders", "fail", "LIBRARY_ROOTS is empty; placement is impossible")
            return
        outside = [
            str(folder.get("path"))
            for folder in folders
            if isinstance(folder, dict)
            and not path_within_roots(str(folder.get("path", "")), self._settings.library_roots)
        ]
        if outside:
            self.add("Root folders", "fail", f"outside LIBRARY_ROOTS: {', '.join(outside)}")
        else:
            self.add("Root folders", "pass", f"{len(folders)} root folder(s) inside LIBRARY_ROOTS")

    async def _health(self) -> None:
        try:
            issues = await self._get("health")
        except ArrError as exc:
            self.add("Health", "fail", str(exc))
            return
        if not isinstance(issues, list) or not issues:
            self.add("Health", "pass", "no issues")
            return
        unexpected: list[str] = []
        errors: list[str] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            message = str(issue.get("message", ""))
            if str(issue.get("type", "")).lower() == "error":
                errors.append(message)
            elif "completed download handling" not in message.lower():
                unexpected.append(message)
        if errors:
            self.add("Health", "fail", "; ".join(errors))
        elif unexpected:
            self.add("Health", "warn", "; ".join(unexpected))
        else:
            self.add("Health", "pass", "only the expected CDH-disabled warning")


async def _check_rtorrent(settings: Settings) -> list[CheckResult]:
    client = RTorrentClient(
        settings.rt_url,
        username=settings.rt_user,
        password=settings.rt_secret,
        timeout=settings.upstream_timeout,
        verify_tls=settings.rt_verify_tls,
    )
    results: list[CheckResult] = []
    try:
        try:
            version = await client.version()
        except Exception as exc:  # Fault is also possible
            results.append(CheckResult("rtorrent", "Version", "fail", str(exc)))
            return results
        results.append(CheckResult("rtorrent", "Version", "pass", version))

        limit = await client.xmlrpc_size_limit()
        if limit is None:
            results.append(CheckResult("rtorrent", "XML-RPC size limit", "skip", "not queryable"))
        elif 0 < limit < 1024 * 1024:
            results.append(
                CheckResult(
                    "rtorrent",
                    "XML-RPC size limit",
                    "warn",
                    f"{limit} bytes; large multicall responses may be truncated",
                )
            )
        else:
            results.append(CheckResult("rtorrent", "XML-RPC size limit", "pass", str(limit)))
    finally:
        await client.aclose()
    return results


def _check_state_dir(settings: Settings) -> list[CheckResult]:
    path = Path(settings.state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [CheckResult("seedpath", "State directory", "fail", str(exc))]
    if not os.access(path.parent, os.W_OK):
        return [
            CheckResult("seedpath", "State directory", "fail", f"{path.parent} is not writable")
        ]
    return [CheckResult("seedpath", "State directory", "pass", str(path.parent))]


async def run_preflight(
    settings: Settings, *, arr_http: httpx.AsyncClient | None = None
) -> list[CheckResult]:
    """Run every check; failures are returned, not raised."""
    results: list[CheckResult] = _check_state_dir(settings)
    results.extend(await _check_rtorrent(settings))

    if not settings.library_roots:
        results.append(
            CheckResult(
                "seedpath",
                "Library roots",
                "fail",
                "LIBRARY_ROOTS is empty; placement is impossible",
            )
        )

    owns_http = arr_http is None
    http = arr_http or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.upstream_timeout),
        trust_env=False,
        follow_redirects=False,
    )
    try:
        clients = {app.name: ArrClient(app, client=http) for app in settings.apps}
        groups = await asyncio.gather(
            *[_AppChecks(settings, app, clients[app.name]).run() for app in settings.apps],
            return_exceptions=True,
        )
        for app, group in zip(settings.apps, groups, strict=True):
            if isinstance(group, BaseException):
                results.append(CheckResult(app.name, "Connection", "fail", str(group)))
            else:
                results.extend(group)
    finally:
        if owns_http:
            await http.aclose()
    return results


def build_table(results: list[CheckResult]) -> Table:
    styles = {"pass": "green", "warn": "yellow", "fail": "red", "skip": "dim"}
    table = Table(title="seedpath preflight", show_lines=False, header_style="bold")
    table.add_column("App")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for result in results:
        table.add_row(
            result.app,
            result.check,
            f"[{styles[result.status]}]{result.status}[/{styles[result.status]}]",
            result.detail,
        )
    return table


def render(results: list[CheckResult], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps([asdict(result) for result in results], indent=2) + "\n")
        return
    console = Console()
    console.print(build_table(results))
    failures = sum(1 for result in results if result.status == "fail")
    warnings = sum(1 for result in results if result.status == "warn")
    if failures:
        console.print(f"[red]{failures} check(s) failed[/red], {warnings} warning(s)")
    else:
        console.print(f"[green]preflight passed[/green], {warnings} warning(s)")


__all__ = ["CheckResult", "build_table", "render", "run_preflight"]
