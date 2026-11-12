"""Wiring for one running seedpath instance."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from seedpath.clients.arr import ArrClient
from seedpath.clients.rtorrent import RTorrentClient
from seedpath.config import Settings
from seedpath.core.resolver import Resolver
from seedpath.core.state import StateStore
from seedpath.core.worker import PlacementWorker
from seedpath.logging import get_logger
from seedpath.proxy.service import ProxyService

log = get_logger(__name__)


@dataclass
class Runtime:
    """Owns every resource with a lifecycle."""

    settings: Settings
    store: StateStore
    torrent: RTorrentClient
    apps: dict[str, ArrClient]
    resolver: Resolver
    worker: PlacementWorker
    proxy: ProxyService
    arr_http: httpx.AsyncClient

    async def aclose(self) -> None:
        await self.worker.stop()
        await self.store.close()
        await self.torrent.aclose()
        await self.arr_http.aclose()


async def build_runtime(settings: Settings) -> Runtime:
    store = StateStore(settings.state_path)
    await store.connect()

    torrent = RTorrentClient(
        settings.rt_url,
        username=settings.rt_user,
        password=settings.rt_secret,
        timeout=settings.upstream_timeout,
        verify_tls=settings.rt_verify_tls,
    )

    arr_http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.upstream_timeout),
        trust_env=False,
        follow_redirects=False,
    )
    apps = {app.name: ArrClient(app, client=arr_http) for app in settings.apps}

    resolver = Resolver(
        apps,
        settings.configured_category_map,
        settings.library_roots,
        resolve_timeout=settings.resolve_timeout,
    )
    worker = PlacementWorker(settings, store, torrent, resolver, apps)
    proxy = ProxyService(settings, worker)

    log.info(
        "runtime.ready",
        apps=sorted(apps),
        categories=settings.configured_category_map,
        dry_run=settings.dry_run,
        state=str(settings.state_path),
    )
    return Runtime(
        settings=settings,
        store=store,
        torrent=torrent,
        apps=apps,
        resolver=resolver,
        worker=worker,
        proxy=proxy,
        arr_http=arr_http,
    )


__all__ = ["Runtime", "build_runtime"]
