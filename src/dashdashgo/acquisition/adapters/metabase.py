"""Metabase adapter.

Navigates the way a person does - Our analytics -> collection -> dashboard ->
card - using accessible names and test ids rather than layout CSS. Filters are
set by operating the dashboard's filter widgets (date shortcuts, the relative
date editor, category lists) and/or through URL parameters (what Metabase
itself does when a widget changes), per ``source.filter_mode``; the resulting
parameter state is verified before downloading. Every UI hook comes from
``MetabaseSelectors`` so a Metabase upgrade is a config change.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from dashdashgo.acquisition.adapters.base import DashboardAdapter
from dashdashgo.config.models import BrowserConfig, FilterItem, MetabaseSource
from dashdashgo.errors import (
    AuthenticationError,
    ConfigurationError,
    DownloadError,
    NavigationError,
    ReportNotFoundError,
)

log = logging.getLogger(__name__)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_ITEM_GRACE_MS = 10_000
_RELATIVE_DATE = re.compile(r"past(\d+)(minutes|hours|days|weeks|months|quarters|years)")


class _WidgetUnavailable(Exception):  # noqa: N818 - internal control-flow signal
    """A filter widget cannot be operated for this value; ``auto`` mode falls back to the URL."""


class MetabaseAdapter(DashboardAdapter):
    platform = "metabase"

    def __init__(self, source: MetabaseSource, browser: BrowserConfig) -> None:
        self.source = source
        self.sel = source.selectors
        self.location = source.location
        self.browser = browser

    # --- helpers --------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.source.base_url}{path}"

    def _main(self, page: Page) -> Locator:
        return page.get_by_role("main")

    def _card(self, page: Page) -> Locator:
        title = page.get_by_role("link", name=self.location.card, exact=True)
        return page.locator(self.sel.dashcard).filter(has=title)

    def _results(self, page: Page) -> Locator:
        scope = self._card(page) if self.location.dashboard else self._main(page)
        return scope.get_by_role("grid").or_(scope.get_by_text("No results", exact=False)).first

    # --- steps ----------------------------------------------------------------

    def prepare(self, page: Page) -> None:
        # Onboarding / "what's new" modals can appear on any page and would
        # swallow clicks. Playwright runs these handlers whenever they show up.
        for label in self.sel.dismiss_buttons:
            page.add_locator_handler(
                page.get_by_role("dialog").get_by_role("button", name=label, exact=True),
                lambda locator: locator.click(),
                no_wait_after=True,
            )

    def login(self, page: Page) -> None:
        page.goto(self._url(self.source.login_path), wait_until="domcontentloaded")
        username = page.locator(self.sel.username_input)
        try:
            username.wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"login form did not appear at {page.url}") from exc

        credentials = self.source.credentials
        username.fill(credentials.username)
        page.locator(self.sel.password_input).fill(credentials.password.get_secret_value())
        page.locator(self.sel.submit_button).click()

        app_ready = page.locator(self.sel.app_ready)
        login_error = page.locator(self.sel.login_error)
        try:
            app_ready.or_(login_error).first.wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise NavigationError(
                "no response to sign-in: neither the app nor an error appeared"
            ) from exc
        if login_error.first.is_visible():
            reason = login_error.first.inner_text().strip()
            raise AuthenticationError(
                f"Metabase rejected the credentials for {credentials.username}: {reason}"
            )
        log.info("Logged in to %s as %s", self.source.base_url, credentials.username)

    def _open_item(self, page: Page, name: str, kind: str, parent: str) -> None:
        main = self._main(page)
        items = main.get_by_role("table").first
        try:
            items.wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"collection '{parent}' did not load") from exc
        link = items.get_by_role("link", name=name, exact=True).first
        try:
            # Short grace period: the item list re-renders asynchronously after navigation.
            link.wait_for(state="visible", timeout=min(self.browser.timeout_ms, _ITEM_GRACE_MS))
        except PlaywrightTimeoutError:
            available = [t.strip() for t in items.get_by_role("link").all_inner_texts()]
            raise ReportNotFoundError(
                f"{kind} '{name}' not found in '{parent}'; it contains: "
                f"{', '.join(t for t in available if t) or 'nothing'}"
            ) from None
        link.click()

    def _enter_collection(self, page: Page, name: str) -> None:
        page.wait_for_url(re.compile(r"/collection/\d+"))
        # The URL changes before the page re-renders; wait until the header shows
        # this collection so we never search the previous collection's items.
        title = self._main(page).get_by_role("textbox", name=self.sel.collection_title)
        try:
            expect(title).to_have_value(name)
        except AssertionError as exc:
            raise NavigationError(f"collection '{name}' did not open") from exc

    def open_report(self, page: Page) -> None:
        page.goto(self._url("/collection/root"), wait_until="domcontentloaded")
        parent = "Our analytics"
        for collection in self.location.collection:
            self._open_item(page, collection, "collection", parent)
            self._enter_collection(page, collection)
            parent = collection

        if self.location.dashboard:
            self._open_item(page, self.location.dashboard, "dashboard", parent)
            page.wait_for_url(re.compile(r"/dashboard/\d+"))
            try:
                page.locator(self.sel.dashcard).first.wait_for(state="visible")
            except PlaywrightTimeoutError as exc:
                raise NavigationError(
                    f"dashboard '{self.location.dashboard}' did not render"
                ) from exc
            try:
                self._card(page).first.wait_for(
                    state="visible", timeout=min(self.browser.timeout_ms, _ITEM_GRACE_MS)
                )
            except PlaywrightTimeoutError:
                titles = page.locator(self.sel.dashcard).get_by_role("link").all_inner_texts()
                raise ReportNotFoundError(
                    f"card '{self.location.card}' not found on dashboard "
                    f"'{self.location.dashboard}'; cards: {', '.join(titles) or 'none'}"
                ) from None
        else:
            assert self.location.question is not None
            self._open_item(page, self.location.question, "question", parent)
            page.wait_for_url(re.compile(r"/question/\d+"))
        log.info(
            "Opened '%s' via %s", self.location.target_name, " / ".join(self.location.collection)
        )

    # --- filters ------------------------------------------------------------------

    def apply_filters(self, page: Page) -> str:
        items = self.source.filter_items()
        method = "none"
        if items:
            mode = self.source.filter_mode
            if mode == "url" or not self.location.dashboard:
                self._apply_via_url(page, items)
                method = "url"
            elif mode == "widget":
                try:
                    self._apply_via_widgets(page, items)
                except _WidgetUnavailable as exc:
                    raise ConfigurationError(
                        f"{exc} (use filter_mode: auto or url for this filter)"
                    ) from None
                method = "widget"
            else:  # auto: behave like a user, fall back to URL parameters if a widget fails
                try:
                    self._apply_via_widgets(page, items)
                    self._wait_for_results(page)
                    self._verify_filters(page, items)
                    method = "widget"
                except (_WidgetUnavailable, PlaywrightError, NavigationError) as exc:
                    log.warning("Filter widgets could not be used (%s); applying via URL", exc)
                    self._apply_via_url(page, items)
                    method = "url (widget fallback)"
        self._wait_for_results(page)
        if items:
            self._verify_filters(page, items)
        description = ", ".join(f"{i.slug}={','.join(i.values)}" for i in items) or "none"
        if items:
            log.info("Results loaded (filters: %s; applied via %s)", description, method)
        else:
            log.info("Results loaded (no filters)")
        return f"{description} via {method}" if items else description

    def _wait_for_results(self, page: Page) -> None:
        try:
            self._results(page).wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise NavigationError("report results did not load") from exc

    def _apply_via_url(self, page: Page, items: list[FilterItem]) -> None:
        parts = urlsplit(page.url)
        query = parse_qs(parts.query, keep_blank_values=True)
        query.update({item.slug: item.values for item in items})
        page.goto(urlunsplit(parts._replace(query=urlencode(query, doseq=True))))

    def _verify_filters(self, page: Page, items: list[FilterItem]) -> None:
        """The URL mirrors the dashboard's filter state, whichever way it was set."""
        applied = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
        # Metabase drops parameters it does not know; a missing slug is a config typo,
        # and silently downloading unfiltered data would be wrong.
        if unknown := sorted(i.slug for i in items if i.slug not in applied):
            raise ConfigurationError(
                f"'{self.location.target_name}' has no filter(s) {unknown}; check source.filters"
            )
        for item in items:
            if sorted(applied[item.slug]) != sorted(item.values):
                raise NavigationError(
                    f"filter '{item.slug}' shows {applied[item.slug]}, expected {item.values}"
                )
        configured = {i.slug for i in items}
        if others := {k: v for k, v in applied.items() if k not in configured and any(v)}:
            # Metabase remembers each user's last filter values per dashboard.
            log.warning("Dashboard has other active filters not in the config: %s", others)

    def _apply_via_widgets(self, page: Page, items: list[FilterItem]) -> None:
        for item in items:
            widget = page.locator(self.sel.parameter_widget).filter(
                has=page.get_by_role("button", name=item.label, exact=True)
            )
            if widget.count() == 0:
                raise _WidgetUnavailable(f"no filter widget labelled '{item.label}'")
            clear = widget.get_by_role("button", name=self.sel.clear_filter_button)
            if clear.count():
                clear.click()
                try:
                    expect(clear).to_have_count(0)
                except AssertionError:
                    raise _WidgetUnavailable(f"could not clear filter '{item.label}'") from None
            widget.locator(self.sel.parameter_widget_target).click()
            dialog = page.get_by_role("dialog", name=item.label)
            dialog.wait_for(state="visible")
            if widget.get_by_role("img", name=self.sel.date_widget_icon).count():
                applied = self._choose_relative_date(dialog, item)
            else:
                applied = self._choose_values(dialog, item)
            if not applied:
                dialog.get_by_role("button", name=re.compile(self.sel.apply_filter_button)).click()
            dialog.wait_for(state="hidden")
            log.info("Set filter '%s' to %s in the dashboard UI", item.label, item.values)

    def _choose_relative_date(self, dialog: Locator, item: FilterItem) -> bool:
        """Pick a relative period; True if Metabase applied it immediately (shortcut)."""
        match = _RELATIVE_DATE.fullmatch(item.values[0]) if len(item.values) == 1 else None
        if match is None:
            raise _WidgetUnavailable(
                f"date value {item.values} is not a relative period like past7days"
            )
        amount, unit = int(match[1]), match[2]
        shortcut = dialog.get_by_role("button", name=f"Previous {amount} {unit}", exact=True)
        if shortcut.count():
            shortcut.click()  # the same click a person makes; shortcuts apply at once
            return True
        dialog.get_by_role("button", name=self.sel.relative_date_option).click()
        dialog.get_by_role("tab", name=self.sel.relative_date_tab).click()
        dialog.get_by_role("textbox", name=self.sel.relative_date_interval).fill(str(amount))
        dialog.get_by_role("textbox", name=self.sel.relative_date_unit).click()
        dialog.page.get_by_role("option", name=unit, exact=True).click()
        return False

    def _choose_values(self, dialog: Locator, item: FilterItem) -> bool:
        # The value list is fetched asynchronously; wait for it (or its search box).
        dialog.get_by_role("checkbox").or_(
            dialog.get_by_role("textbox", name=self.sel.list_search)
        ).first.wait_for(state="visible")
        for value in item.values:
            checkbox = dialog.get_by_role("checkbox", name=value, exact=True)
            if checkbox.count() == 0:
                dialog.get_by_role("textbox", name=self.sel.list_search).fill(value)
            try:
                checkbox.wait_for(state="visible", timeout=_ITEM_GRACE_MS)
            except PlaywrightTimeoutError:
                raise ConfigurationError(
                    f"filter '{item.label}' does not offer the value {value!r}"
                ) from None
            checkbox.check()
        return False  # needs "Add filter"

    def download(self, page: Page, target_dir: Path) -> Path:
        if self.location.dashboard:
            card = self._card(page).first
            card.hover()
            card.get_by_role("button", name=self.sel.card_menu_button).click()
            page.get_by_role("menuitem", name=self.sel.download_menu_item).click()
        else:
            page.locator(self.sel.question_download_button).click()

        heading = page.get_by_role("heading", name=self.sel.download_dialog_heading)
        popover = page.locator("[role=menu], [role=dialog]").filter(has=heading)
        popover.wait_for(state="visible")
        fmt = self.source.export.format.value
        popover.get_by_text(f".{fmt}", exact=True).click()
        formatted = popover.get_by_role("checkbox", name=self.sel.formatted_checkbox)
        if formatted.count():
            formatted.set_checked(self.source.export.formatted)

        try:
            with page.expect_download(timeout=self.browser.download_timeout_ms) as info:
                popover.get_by_role("button", name=self.sel.download_button, exact=True).click()
        except PlaywrightTimeoutError as exc:
            raise DownloadError(
                f"no {fmt.upper()} download started within "
                f"{self.browser.download_timeout_ms // 1000}s"
            ) from exc
        download = info.value
        if failure := download.failure():
            raise DownloadError(f"browser reported the download failed: {failure}")
        filename = _SAFE_FILENAME.sub("_", download.suggested_filename) or f"report.{fmt}"
        target = target_dir / filename
        download.save_as(target)
        log.info("Downloaded %s", filename)
        return target
