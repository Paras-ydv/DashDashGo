"""Terminal output helpers: aligned tables, status labels, JSON."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from dashdashgo.metadata.models import RunStatus

_ANSI = re.compile(r"\033\[[0-9;]*m")

_STATUS = {
    RunStatus.SUCCESS: ("✓", "32"),
    RunStatus.SKIPPED: ("↷", "90"),
    RunStatus.FAILED: ("✗", "31"),
    RunStatus.RUNNING: ("…", "34"),
    RunStatus.QUEUED: ("◷", "34"),
}
_STEP = {
    "success": ("✓", "32"),
    "failed": ("✗", "31"),
    "running": ("…", "34"),
    "skipped": ("-", "90"),
    "pending": ("·", "90"),
}


def _color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _color() else text


def status_label(status: RunStatus) -> str:
    symbol, code = _STATUS[status]
    return paint(f"{symbol} {status.value}", code)


def step_symbol(state: str) -> str:
    symbol, code = _STEP.get(state, ("·", "90"))
    return paint(symbol, code)


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Left-aligned text table; numbers are right-aligned."""
    cells = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(_strip(value)))

    def fmt(row: Sequence[str], raw: Sequence[Any] | None = None) -> str:
        parts = []
        for i, value in enumerate(row):
            pad = widths[i] - len(_strip(value))
            numeric = (
                raw is not None
                and isinstance(raw[i], int | float | Decimal)
                and not isinstance(raw[i], bool)
            )
            parts.append(" " * pad + value if numeric else value + " " * pad)
        return "  ".join(parts).rstrip()

    lines = [paint(fmt(list(headers)), "1"), fmt(["-" * w for w in widths])]
    lines += [fmt(row, raw) for row, raw in zip(cells, rows, strict=True)]
    return "\n".join(lines)


def _strip(text: str) -> str:
    """Text without ANSI colour codes (for width calculations)."""
    return _ANSI.sub("", text)


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))
