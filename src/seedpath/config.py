"""Configuration.

Everything is driven by environment variables so the same image works in
Docker Compose, Kubernetes and a local ``uv run seedpath`` session. See
``docs/configuration.md`` and ``.env.example`` for the full surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from seedpath.errors import ConfigError
from seedpath.logging import LogFormat
from seedpath.util import Duration, normalize_posix_path

AppKind = Literal["radarr", "sonarr", "lidarr", "readarr", "whisparr"]

API_VERSIONS: dict[AppKind, str] = {
    "radarr": "v3",
    "sonarr": "v3",
    "lidarr": "v1",
    "readarr": "v1",
    "whisparr": "v3",
}

#: Category -> app instance name. These are the categories documented in the
#: Servarr setup guides, not an invention of seedpath.
DEFAULT_CATEGORY_MAP: dict[str, str] = {
    "radarr": "radarr",
    "tv-sonarr": "sonarr",
    "lidarr": "lidarr",
    "readarr": "readarr",
    "whisparr": "whisparr",
}


class AppConfig(BaseModel):
    """One configured *arr instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: AppKind
    url: str
    api_key: SecretStr

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            msg = f"app URL must be http(s)://host[:port][/base]: {value!r}"
            raise ValueError(msg)
        return value.rstrip("/")

    @property
    def api_version(self) -> str:
        return API_VERSIONS[self.kind]

    @property
    def api_prefix(self) -> str:
        return f"/api/{self.api_version}"


class Settings(BaseSettings):
    """Runtime settings, populated from the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- rTorrent / ruTorrent -------------------------------------------------
    rt_url: str = "http://rtorrent:8000/RPC2"
    rt_user: str = ""
    rt_pass: SecretStr = SecretStr("")
    rt_verify_tls: bool = True

    # --- *arr instances -------------------------------------------------------
    radarr_url: str | None = None
    radarr_api_key: SecretStr | None = None
    sonarr_url: str | None = None
    sonarr_api_key: SecretStr | None = None
    lidarr_url: str | None = None
    lidarr_api_key: SecretStr | None = None
    readarr_url: str | None = None
    readarr_api_key: SecretStr | None = None
    whisparr_url: str | None = None
    whisparr_api_key: SecretStr | None = None
    #: Escape hatch for multiple instances / non-default names:
    #: ``[{"name": "radarr4k", "kind": "radarr", "url": "...", "api_key": "..."}]``
    apps_json: str | None = None

    # --- Placement policy -----------------------------------------------------
    library_roots: list[str] = Field(default_factory=list)
    category_map: dict[str, str] | None = None
    dry_run: bool = False

    # --- Timing ---------------------------------------------------------------
    resolve_timeout: Duration = 10.0
    rescan_poll: Duration = 30.0
    reconcile_interval: Duration = 300.0
    load_timeout: Duration = 30.0
    upstream_timeout: Duration = 30.0

    # --- HTTP server ----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    rpc_path: str = "/RPC2"
    ingress_auth: bool = True
    max_body_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)

    # --- Runtime / operations -------------------------------------------------
    state_path: Path = Path("./data/state.db")
    log_level: str = "INFO"
    log_format: LogFormat = "json"
    worker_count: int = Field(default=2, ge=1, le=32)
    worker_queue_size: int = Field(default=1000, ge=1)
    background_workers: bool = True

    apps: tuple[AppConfig, ...] = Field(default_factory=tuple, init=False)

    @field_validator("rpc_path")
    @classmethod
    def _validate_rpc_path(cls, value: str) -> str:
        if not value.startswith("/"):
            msg = "rpc_path must start with '/'"
            raise ValueError(msg)
        return value

    @field_validator("rt_url")
    @classmethod
    def _validate_rt_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            msg = f"RT_URL must be http(s)://host[:port]/RPC2, got {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("library_roots")
    @classmethod
    def _normalize_roots(cls, value: list[str]) -> list[str]:
        return [normalize_posix_path(root) for root in value]

    @model_validator(mode="after")
    def _assemble_apps(self) -> Self:
        apps: list[AppConfig] = []
        for kind in ("radarr", "sonarr", "lidarr", "readarr", "whisparr"):
            raw_url = getattr(self, f"{kind}_url")
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            raw_key = getattr(self, f"{kind}_api_key")
            key = raw_key.get_secret_value().strip() if raw_key is not None else ""
            # Empty placeholders in .env.example count as "unset".
            if not url and not key:
                continue
            if not url or not key:
                msg = f"{kind.upper()}_URL and {kind.upper()}_API_KEY must be set together"
                raise ValueError(msg)
            apps.append(AppConfig(name=kind, kind=kind, url=url, api_key=SecretStr(key)))

        if self.apps_json:
            try:
                raw = json.loads(self.apps_json)
            except json.JSONDecodeError as exc:
                msg = f"APPS_JSON is not valid JSON: {exc}"
                raise ValueError(msg) from exc
            if not isinstance(raw, list):
                msg = "APPS_JSON must be a JSON array"
                raise ValueError(msg)
            for entry in raw:
                if not isinstance(entry, dict):
                    msg = "APPS_JSON entries must be objects"
                    raise ValueError(msg)
                apps.append(AppConfig.model_validate(entry))

        names = [app.name for app in apps]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            msg = f"duplicate app instance name(s): {', '.join(duplicates)}"
            raise ValueError(msg)

        self.apps = tuple(apps)

        if self.category_map is None:
            configured = {app.name for app in apps}
            self.category_map = {
                category: app for category, app in DEFAULT_CATEGORY_MAP.items() if app in configured
            } or {app.name: app.name for app in apps}
        else:
            missing = sorted(set(self.category_map.values()) - set(names))
            if missing:
                msg = f"CATEGORY_MAP references unconfigured app(s): {', '.join(missing)}"
                raise ValueError(msg)
            if not self.category_map:
                msg = "CATEGORY_MAP must not be empty"
                raise ValueError(msg)
        return self

    def app(self, name: str) -> AppConfig:
        for app in self.apps:
            if app.name == name:
                return app
        msg = f"unknown app instance: {name!r}"
        raise ConfigError(msg)

    def app_for_category(self, category: str) -> AppConfig | None:
        category_map = self.category_map
        if category_map is None:  # pragma: no cover - set by the model validator
            msg = "category map was not initialised"
            raise ConfigError(msg)
        name = category_map.get(category)
        return self.app(name) if name is not None else None

    @property
    def rt_username(self) -> str:
        return self.rt_user

    @property
    def rt_secret(self) -> str:
        return self.rt_pass.get_secret_value()

    @property
    def configured_category_map(self) -> dict[str, str]:
        category_map = self.category_map
        if category_map is None:  # pragma: no cover - set by the model validator
            msg = "category map was not initialised"
            raise ConfigError(msg)
        return dict(category_map)
