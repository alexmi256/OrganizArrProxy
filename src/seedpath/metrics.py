"""Prometheus metrics.

Optional but cheap: one ``/metrics`` endpoint gives operators grab counts,
retarget latency, resolve failures and fail-open counts without a database
query.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

GRABS = Counter(
    "seedpath_grabs_total",
    "Load calls accepted from an *arr app.",
    ("category",),
)
PLACEMENTS = Counter(
    "seedpath_placements_total",
    "Placement outcomes.",
    ("app", "outcome"),
)
RESOLVE_FAILURES = Counter(
    "seedpath_resolve_failures_total",
    "Destination resolution failures.",
    ("app",),
)
RESCANS = Counter(
    "seedpath_rescans_total",
    "Rescan command outcomes.",
    ("app", "outcome"),
)
RETARGET_SECONDS = Histogram(
    "seedpath_retarget_seconds",
    "Time from accepting a load to starting the torrent in its destination.",
    ("app",),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
QUEUE_DEPTH = Gauge(
    "seedpath_worker_queue_depth",
    "Placement jobs waiting for a worker.",
)
ACTIVE_TORRENTS = Gauge(
    "seedpath_active_torrents",
    "Torrents currently between RECEIVED and ADOPTED.",
)


def render() -> tuple[bytes, str]:
    """Return the exposition body and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
