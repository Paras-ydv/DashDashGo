"""Dashboard adapter interface.

An adapter knows how one dashboard product works (its login form, how reports
are organised, how exports are triggered). The acquisition service drives any
adapter through the same four steps, so supporting Superset, Tableau, Looker,
... means writing one adapter class - the pipeline does not change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from playwright.sync_api import Page


class DashboardAdapter(ABC):
    platform: str

    @abstractmethod
    def prepare(self, page: Page) -> None:
        """Install page-level handlers (e.g. auto-dismiss onboarding modals)."""

    @abstractmethod
    def login(self, page: Page) -> None:
        """Sign in; raise AuthenticationError if the credentials are rejected."""

    @abstractmethod
    def open_report(self, page: Page) -> None:
        """Navigate to the report; raise ReportNotFoundError if it does not exist."""

    @abstractmethod
    def apply_filters(self, page: Page) -> str:
        """Apply configured filters and wait for results; return a short description."""

    @abstractmethod
    def download(self, page: Page, target_dir: Path) -> Path:
        """Trigger the export and return the path of the saved file."""
