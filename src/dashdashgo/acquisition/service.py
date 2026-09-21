"""Acquisition: log in, navigate, filter and download a report - with retries.

Each attempt uses a fresh browser. Every step is recorded as a stage
(``acquisition.login`` ...). When a step fails, a full-page screenshot and the
page HTML are stored before the error propagates, so a failed run always comes
with evidence of what the browser was looking at.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from dashdashgo.acquisition.adapters import DashboardAdapter, create_adapter
from dashdashgo.acquisition.browser import BrowserSession, browser_session
from dashdashgo.acquisition.validation import validate_download
from dashdashgo.config.models import ReportConfig
from dashdashgo.errors import DashDashGoError, NavigationError, StorageError
from dashdashgo.metadata.tracker import RunTracker, StageHandle
from dashdashgo.observability.logging import redactor
from dashdashgo.orchestration.context import RunContext
from dashdashgo.storage import StorageBackend
from dashdashgo.utils.retry import RetryPolicy, call_with_retry

log = logging.getLogger(__name__)

AdapterFactory = Callable[[ReportConfig], DashboardAdapter]


@dataclass(frozen=True)
class AcquiredReport:
    local_path: Path
    storage_key: str
    size: int
    sha256: str
    attempts: int


class AcquisitionService:
    def __init__(
        self, storage: StorageBackend, adapter_factory: AdapterFactory = create_adapter
    ) -> None:
        self._storage = storage
        self._adapter_factory = adapter_factory

    def acquire(self, report: ReportConfig, ctx: RunContext, tracker: RunTracker) -> AcquiredReport:
        adapter = self._adapter_factory(report)

        def attempt(number: int) -> AcquiredReport:
            tracker.update(attempts=number)
            with tracker.stage("acquisition", attempt=number) as stage:
                result = self._attempt(report, adapter, ctx, tracker, number)
                stage.message = f"{result.size:,} bytes"
                stage.details = {"file": result.storage_key, "sha256": result.sha256}
                return result

        return call_with_retry(
            attempt, RetryPolicy.from_config(report.retry), description="Report acquisition"
        )

    def _attempt(
        self,
        report: ReportConfig,
        adapter: DashboardAdapter,
        ctx: RunContext,
        tracker: RunTracker,
        number: int,
    ) -> AcquiredReport:
        download_dir = ctx.workdir / f"attempt-{number}"
        download_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            with tracker.stage("acquisition.browser", attempt=number) as handle:
                session = stack.enter_context(browser_session(report.browser))
                handle.message = f"{report.browser.engine}, headless={report.browser.headless}"
            page = session.page
            failed = False
            try:
                adapter.prepare(page)
                with self._step(tracker, ctx, page, "login", number, report):
                    adapter.login(page)
                session.start_trace()
                with self._step(tracker, ctx, page, "navigate", number, report):
                    adapter.open_report(page)
                with self._step(tracker, ctx, page, "filters", number, report) as handle:
                    handle.message = adapter.apply_filters(page)
                with self._step(tracker, ctx, page, "download", number, report) as handle:
                    path = adapter.download(page, download_dir)
                    handle.message = path.name
                with self._step(tracker, ctx, page, "validate", number, report) as handle:
                    info = validate_download(path, report.source.export.format)
                    handle.message = f"{info.size:,} bytes, sha256 {info.sha256[:12]}"
            except BaseException:
                failed = True
                raise
            finally:
                self._finish_trace(session, ctx, number, failed)

        key = ctx.artifacts.key("raw", path.name)
        self._storage.put_file(path, key)
        return AcquiredReport(path, key, info.size, info.sha256, number)

    @contextmanager
    def _step(
        self,
        tracker: RunTracker,
        ctx: RunContext,
        page: Page,
        name: str,
        number: int,
        report: ReportConfig,
    ) -> Iterator[StageHandle]:
        with tracker.stage(f"acquisition.{name}", attempt=number) as handle:
            try:
                yield handle
            except DashDashGoError as exc:
                self._capture_failure(page, ctx, name, number, report, exc)
                raise
            except PlaywrightTimeoutError as exc:
                error = NavigationError(f"{name}: timed out ({_first_line(exc)})")
                self._capture_failure(page, ctx, name, number, report, error)
                raise error from exc
            except PlaywrightError as exc:
                error = NavigationError(f"{name}: browser error ({_first_line(exc)})")
                self._capture_failure(page, ctx, name, number, report, error)
                raise error from exc

    def _capture_failure(
        self,
        page: Page,
        ctx: RunContext,
        step: str,
        number: int,
        report: ReportConfig,
        error: DashDashGoError,
    ) -> None:
        if not report.browser.screenshot_on_failure:
            return
        prefix = f"attempt{number}_{step}_failure"
        try:
            shot = self._storage.put_bytes(
                page.screenshot(full_page=True), ctx.artifacts.key("screenshots", f"{prefix}.png")
            )
            # The DOM can hold typed form values (e.g. the password field).
            html = self._storage.put_bytes(
                redactor.redact(page.content()).encode(),
                ctx.artifacts.key("failures", f"{prefix}.html"),
            )
            error.artifacts.extend([shot.key, html.key])
            log.info("Saved failure screenshot %s (page: %s)", shot.key, page.url)
        except (PlaywrightError, StorageError) as exc:
            log.warning("Could not capture failure evidence: %s", _first_line(exc))

    def _finish_trace(
        self, session: BrowserSession, ctx: RunContext, number: int, failed: bool
    ) -> None:
        mode = session.config.trace
        keep = mode == "always" or (mode == "on_failure" and failed)
        local = ctx.workdir / f"attempt{number}_trace.zip" if keep else None
        saved = session.stop_trace(local)
        if saved:
            key = ctx.artifacts.key("failures" if failed else "logs", saved.name)
            self._storage.put_file(saved, key)
            log.info("Saved Playwright trace %s (open with `playwright show-trace`)", key)


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
