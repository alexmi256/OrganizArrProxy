"""Configuration parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from seedpath.config import Settings
from tests.support import make_settings


def test_empty_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.apps == ()
    assert settings.configured_category_map == {}
    assert settings.rt_url == "http://rtorrent:8000/RPC2"
    assert settings.dry_run is False


def test_documented_rt_pass_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RT_USER", "rtorrent")
    monkeypatch.setenv("RT_PASS", "secret")
    settings = Settings(_env_file=None)
    assert settings.rt_secret == "secret"


def test_convenience_apps_and_default_categories() -> None:
    settings = Settings(
        _env_file=None,
        radarr_url="http://radarr:7878",
        radarr_api_key="r",
        sonarr_url="http://sonarr:8989",
        sonarr_api_key="s",
    )
    assert [app.name for app in settings.apps] == ["radarr", "sonarr"]
    assert settings.configured_category_map == {"radarr": "radarr", "tv-sonarr": "sonarr"}
    assert settings.app("sonarr").api_prefix == "/api/v3"
    assert settings.app_for_category("tv-sonarr") is not None
    assert settings.app_for_category("unknown") is None


def test_api_version_per_app() -> None:
    settings = make_settings(
        tmp_path=Path("/tmp"),
        lidarr_url="http://lidarr:8686",
        lidarr_api_key="l",
    )
    assert settings.app("lidarr").api_prefix == "/api/v1"


def test_url_and_key_must_be_set_together() -> None:
    with pytest.raises(ValidationError, match="together"):
        Settings(_env_file=None, radarr_url="http://radarr:7878")
    with pytest.raises(ValidationError, match="together"):
        Settings(_env_file=None, radarr_api_key="key")


def test_empty_placeholders_are_ignored() -> None:
    settings = Settings(_env_file=None, radarr_url="", radarr_api_key="")
    assert settings.apps == ()


def test_invalid_url_rejected() -> None:
    with pytest.raises(ValidationError, match="http"):
        Settings(_env_file=None, radarr_url="radarr:7878", radarr_api_key="key")
    with pytest.raises(ValidationError, match="RT_URL"):
        Settings(_env_file=None, rt_url="rtorrent:8000/RPC2")


def test_apps_json_supports_multiple_instances() -> None:
    settings = Settings(
        _env_file=None,
        apps_json=(
            '[{"name":"radarr4k","kind":"radarr","url":"http://radarr4k:7878",'
            '"api_key":"key"},{"name":"radarr","kind":"radarr",'
            '"url":"http://radarr:7878","api_key":"key2"}]'
        ),
        category_map={"radarr": "radarr", "radarr4k": "radarr4k"},
    )
    assert {app.name for app in settings.apps} == {"radarr", "radarr4k"}


def test_duplicate_app_names_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        Settings(
            _env_file=None,
            radarr_url="http://radarr:7878",
            radarr_api_key="key",
            apps_json=(
                '[{"name":"radarr","kind":"radarr","url":"http://other:7878","api_key":"key"}]'
            ),
        )


def test_category_map_must_reference_configured_app() -> None:
    with pytest.raises(ValidationError, match="unconfigured"):
        Settings(
            _env_file=None,
            radarr_url="http://radarr:7878",
            radarr_api_key="key",
            category_map={"tv-sonarr": "sonarr"},
        )


def test_category_map_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="empty"):
        Settings(
            _env_file=None,
            radarr_url="http://radarr:7878",
            radarr_api_key="key",
            category_map={},
        )


def test_duration_strings() -> None:
    settings = make_settings(tmp_path=Path("/tmp"), resolve_timeout="1m", rescan_poll="500ms")
    assert settings.resolve_timeout == 60.0
    assert settings.rescan_poll == 0.5


def test_invalid_duration_rejected() -> None:
    with pytest.raises(ValidationError, match="duration"):
        Settings(_env_file=None, resolve_timeout="soon")


def test_library_roots_are_normalized() -> None:
    settings = make_settings(tmp_path=Path("/tmp"), library_roots=["/data/movies/", "/data//tv"])
    assert settings.library_roots == ["/data/movies", "/data/tv"]


def test_secrets_are_redacted_in_dump() -> None:
    settings = make_settings(tmp_path=Path("/tmp"))
    dumped = settings.model_dump(mode="json")
    assert dumped["radarr_api_key"] == "**********"
    assert "radarr-key" not in str(dumped)
