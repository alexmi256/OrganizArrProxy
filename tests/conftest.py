"""Shared pytest configuration."""

from __future__ import annotations

import pytest

from seedpath.config import Settings


@pytest.fixture(autouse=True)
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient shell variables from leaking into Settings tests."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
