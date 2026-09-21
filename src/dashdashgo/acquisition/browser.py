"""Playwright browser lifecycle for one acquisition attempt."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from dashdashgo.config.models import BrowserConfig
from dashdashgo.errors import BrowserError

log = logging.getLogger(__name__)


@dataclass
class BrowserSession:
    page: Page
    context: BrowserContext
    config: BrowserConfig
    tracing: bool = False

    def start_trace(self) -> None:
        """Start recording a Playwright trace (if enabled).

        Called only *after* login: traces record every typed value in plain
        text, so starting later keeps credentials out of stored artifacts.
        """
        if self.config.trace != "off" and not self.tracing:
            self.context.tracing.start(screenshots=True, snapshots=True)
            self.tracing = True

    def stop_trace(self, destination: Path | None) -> Path | None:
        """Stop tracing; write the trace to ``destination`` or discard it."""
        if not self.tracing:
            return None
        self.tracing = False
        try:
            if destination is None:
                self.context.tracing.stop()
                return None
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.context.tracing.stop(path=str(destination))
            return destination
        except PlaywrightError as exc:
            log.warning("Could not save Playwright trace: %s", str(exc).splitlines()[0])
            return None


@contextmanager
def browser_session(config: BrowserConfig) -> Iterator[BrowserSession]:
    """Launch a fresh, isolated browser (no shared cookies between attempts)."""
    with sync_playwright() as playwright:
        try:
            browser = getattr(playwright, config.engine).launch(
                headless=config.headless, args=config.launch_args
            )
        except PlaywrightError as exc:
            raise BrowserError(
                f"could not launch {config.engine}: {str(exc).splitlines()[0]}"
            ) from exc
        try:
            context = browser.new_context(
                viewport={"width": config.viewport.width, "height": config.viewport.height},
                accept_downloads=True,
            )
            context.set_default_timeout(config.timeout_ms)
            context.set_default_navigation_timeout(config.navigation_timeout_ms)
            page = context.new_page()
            log.info(
                "Browser started (%s, headless=%s, timeout=%ss)",
                config.engine,
                config.headless,
                config.timeout_ms // 1000,
            )
            yield BrowserSession(page=page, context=context, config=config)
        finally:
            browser.close()
