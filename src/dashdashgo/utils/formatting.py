"""Human-readable formatting shared by the web UI and the CLI."""

from __future__ import annotations

import math
from datetime import UTC, datetime


def fmt_duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    seconds = ms / 1000
    if seconds < 1:
        return f"{int(ms)} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def fmt_count(value: float | None) -> str:
    if value is None:
        return "—"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e4, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.1f}{suffix}"
    return f"{int(value):,}"


def fmt_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    power = min(int(math.log(size, 1024)) if size > 0 else 0, len(units) - 1)
    return f"{size / 1024**power:.1f} {units[power]}" if power else f"{size} B"


def fmt_ago(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    delta = (datetime.now(UTC) - moment).total_seconds()
    future = delta < 0
    delta = abs(delta)
    for unit, seconds in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= seconds:
            amount = f"{int(delta // seconds)}{unit}"
            return f"in {amount}" if future else f"{amount} ago"
    return "in <1m" if future else "just now"


def fmt_time(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if moment else "—"
