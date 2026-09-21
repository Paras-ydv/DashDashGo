"""Acquisition: log in, navigate, filter and download a report - with retries.

Each attempt uses a fresh browser. Every step is recorded as a stage
(``acquisition.login`` ...). When a step fails, a full-page screenshot and the
page HTML are stored before the error propagates, so a failed run always comes
with evidence of what the browser was looking at.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
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
                    stored_name = report.source.export.stored_filename(ctx.run.run_date, path.name)
                    handle.message = (
                        path.name if stored_name == path.name else f"{path.name} -> {stored_name}"
                    )
                with self._step(tracker, ctx, page, "validate", number, report) as handle:
                    info = validate_download(path, report.source.export.format)
                    handle.message = f"{info.size:,} bytes, sha256 {info.sha256[:12]}"
            except BaseException:
                failed = True
                raise
            finally:
                self._finish_trace(session, ctx, number, failed)

        key = ctx.artifacts.key("raw", stored_name)
        self._storage.put_file(path, key)
        if stored_name != path.name:
            log.info("Stored %s as %s", path.name, stored_name)
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
        # After a failed navigation Chromium is still swapping in its error page;
        # reading the DOM mid-navigation fails, so let it settle first (best effort).
        with suppress(PlaywrightError):
            page.wait_for_load_state("load", timeout=5_000)
        # Each piece of evidence is captured independently: one failing must
        # not cost us the other.
        html_key = self._store_evidence(
            "page HTML",
            # The DOM can hold typed form values (e.g. the password field).
            lambda: redactor.redact(page.content()).encode(),
            ctx.artifacts.key("failures", f"{prefix}.html"),
        )

        def screenshot() -> bytes:
            if _page_is_blank(page):
                # Nothing rendered (DNS failure, connection refused, ...): a white
                # screenshot is useless evidence, so draw what happened instead.
                page.set_content(_diagnostic_html(report, step, number, page.url, error))
            return page.screenshot(full_page=True)

        shot_key = self._store_evidence(
            "screenshot", screenshot, ctx.artifacts.key("screenshots", f"{prefix}.png")
        )
        error.artifacts.extend(key for key in (shot_key, html_key) if key)
        if shot_key:
            log.info("Saved failure screenshot %s (page: %s)", shot_key, page.url)

    def _store_evidence(self, what: str, produce: Callable[[], bytes], key: str) -> str | None:
        try:
            return self._storage.put_bytes(produce(), key).key
        except (PlaywrightError, StorageError) as exc:
            log.warning("Could not capture failure %s: %s", what, _first_line(exc))
            return None

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


def _page_is_blank(page: Page) -> bool:
    """True when the browser shows nothing: no text and no visual elements."""
    if page.url in ("", "about:blank"):
        return True
    result: bool = page.evaluate(
        "() => !document.body || (document.body.innerText.trim() === ''"
        " && !document.querySelector('img, svg, canvas, video, iframe'))"
    )
    return result


def _diagnostic_html(
    report: ReportConfig, step: str, attempt: int, page_url: str, error: DashDashGoError
) -> str:
    rows = [
        ("Report", report.name),
        ("Step", f"{step} (attempt {attempt})"),
        ("Dashboard", report.source.base_url),
        ("Browser URL", page_url or "about:blank"),
        ("Error", f"{error.error_type}: {redactor.redact(error.message)}"),
        ("Captured", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")),
    ]
    cells = "".join(
        f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>" for label, value in rows
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin: 0; font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
       background: #f6f6f4; color: #151514;
       display: grid; place-items: center; min-height: 100vh; }}
.card {{ background: #fff; border: 1px solid #e3e2dd; border-top: 4px solid #d03b3b;
        border-radius: 10px; padding: 28px 32px; width: 860px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }} p {{ margin: 0 0 18px; color: #52514e; }}
th {{ text-align: left; color: #8a8984; font-weight: 500; padding: 6px 18px 6px 0;
     vertical-align: top; white-space: nowrap; }}
td {{ font-family: ui-monospace, Menlo, monospace; font-size: 13px; padding: 6px 0;
     word-break: break-word; }}
</style></head><body><div class="card">
<h1>The page did not load</h1>
<p>DashDashGo could not render anything at this step, so this card was drawn in the
browser in place of an empty screenshot.</p>
<table>{cells}</table></div></body></html>"""


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
