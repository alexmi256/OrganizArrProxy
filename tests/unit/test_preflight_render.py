"""Preflight presentation helpers."""

from __future__ import annotations

import json

from seedpath.preflight import CheckResult, build_table, render


def test_build_table_contains_every_row() -> None:
    results = [
        CheckResult("radarr", "Completed Download Handling", "pass", "disabled"),
        CheckResult("sonarr", "Add stopped", "fail", "must be enabled"),
        CheckResult("seedpath", "Library roots", "warn", "unrestricted"),
    ]
    table = build_table(results)
    assert table.row_count == 3


def test_render_json(capsys) -> None:
    results = [CheckResult("radarr", "Check", "pass", "ok")]
    render(results, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"app": "radarr", "check": "Check", "status": "pass", "detail": "ok"}]


def test_render_table(capsys) -> None:
    results = [CheckResult("radarr", "Check", "fail", "broken")]
    render(results, as_json=False)
    output = capsys.readouterr().out
    assert "seedpath preflight" in output
    assert "1 check(s) failed" in output
