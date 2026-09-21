"""Standard cron expressions -> APScheduler triggers.

APScheduler 3 numbers the day-of-week field from Monday = 0, while standard
cron (and everyone reading a report config) uses Sunday = 0 / Monday = 1. So
``0 8 * * 1`` would silently fire on *Tuesday*. This module translates numeric
days to names (``mon``), which both interpret identically.
"""

from __future__ import annotations

import re

from apscheduler.triggers.cron import CronTrigger

_DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_NUMBER = re.compile(r"(?<![/\d])\d+")  # a day number, not the step in */2


def _translate_day_of_week(field: str) -> str:
    def name(match: re.Match[str]) -> str:
        number = int(match.group())
        if number > 7:
            raise ValueError(f"day-of-week {number} out of range 0-7")
        return _DAY_NAMES[number]

    return _DAY_NUMBER.sub(name, field)


def cron_trigger(expression: str, timezone: str) -> CronTrigger:
    """Build a trigger from a 5-field cron expression with standard semantics."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            f"expected 5 fields (minute hour day month day-of-week), got {len(fields)}"
        )
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_translate_day_of_week(day_of_week),
        timezone=timezone,
    )
