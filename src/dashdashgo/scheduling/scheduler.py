"""Cron scheduling of report pipelines, driven by each report's ``schedule`` block.

A scheduled run is just ``RunService.submit(name, trigger=SCHEDULE)`` - exactly
what the UI's "Run now" button does - so there is no second code path to keep
in sync. Schedules are re-read from the config files periodically, so editing a
cron expression takes effect without a restart.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from dashdashgo.config.loader import ReportRegistry
from dashdashgo.config.models import ScheduleConfig
from dashdashgo.errors import DashDashGoError
from dashdashgo.metadata.models import Trigger, utcnow
from dashdashgo.orchestration.runner import RunService
from dashdashgo.scheduling.cron import cron_trigger
from dashdashgo.storage import StorageBackend

log = logging.getLogger(__name__)

_REPORT_JOB = "report:"
SYNC_INTERVAL_MINUTES = 5


class ReportScheduler:
    def __init__(
        self,
        *,
        registry: ReportRegistry,
        run_service: RunService,
        storage: StorageBackend,
        retention_days: int,
    ) -> None:
        self._registry = registry
        self._run_service = run_service
        self._storage = storage
        self._retention_days = retention_days
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._schedules: dict[str, ScheduleConfig] = {}
        self._sync_lock = threading.Lock()  # sync runs from the job thread and API saves

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def start(self) -> None:
        self.sync()
        self._scheduler.add_job(
            self.sync, IntervalTrigger(minutes=SYNC_INTERVAL_MINUTES), id="sync-schedules"
        )
        if self._retention_days > 0:
            self._scheduler.add_job(
                self.prune, CronTrigger(hour=3, minute=15, timezone="UTC"), id="prune-artifacts"
            )
        self._scheduler.start()
        log.info("Scheduler started with %d report schedule(s)", len(self._schedules))

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def sync(self) -> None:
        """Add, update or remove report jobs to match the config files."""
        with self._sync_lock:
            self._sync()

    def _sync(self) -> None:
        valid, invalid = self._registry.load_all()
        for name, error in invalid.items():
            log.error("Not scheduling '%s': %s", name, error.message)
        wanted = {
            name: cfg.schedule
            for name, cfg in valid.items()
            if cfg.enabled and cfg.schedule.enabled and cfg.schedule.cron
        }
        for name in set(self._schedules) - set(wanted):
            self._scheduler.remove_job(_REPORT_JOB + name)
            log.info("Unscheduled '%s'", name)
        for name, schedule in wanted.items():
            if self._schedules.get(name) == schedule:
                continue
            assert schedule.cron is not None
            self._scheduler.add_job(
                self._fire,
                cron_trigger(schedule.cron, schedule.timezone),
                args=[name],
                id=_REPORT_JOB + name,
                replace_existing=True,
                max_instances=1,
                coalesce=True,  # after downtime, run once - not once per missed slot
                misfire_grace_time=3600,
            )
            log.info("Scheduled '%s': %s (%s)", name, schedule.cron, schedule.timezone)
        self._schedules = wanted

    def _fire(self, name: str) -> None:
        try:
            self._run_service.submit(name, trigger=Trigger.SCHEDULE)
        except DashDashGoError as exc:
            log.error("Scheduled run of '%s' not started: %s", name, exc.message)

    def prune(self) -> None:
        cutoff = (utcnow() - timedelta(days=self._retention_days)).date()
        removed = self._storage.prune(cutoff)
        log.info("Retention: removed %d artifact(s) older than %s", removed, cutoff)

    def next_run(self, name: str) -> datetime | None:
        job = self._scheduler.get_job(_REPORT_JOB + name)
        next_time: datetime | None = job.next_run_time if job else None
        return next_time
