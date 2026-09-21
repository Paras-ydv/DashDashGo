"""Artifact storage abstraction.

Pipelines store raw downloads, processed datasets, rejected rows, screenshots,
traces and logs through :class:`StorageBackend` using string keys such as
``raw/weekly_sales/2026-09-21/<run_id>/report.csv``. Only the backend knows
where bytes physically live, so an S3/GCS backend can be added without touching
pipeline code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

Area = Literal["raw", "processed", "failures", "screenshots", "logs"]
AREAS: tuple[Area, ...] = ("raw", "processed", "failures", "screenshots", "logs")


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size: int
    modified_at: datetime


class StorageBackend(ABC):
    @abstractmethod
    def put_file(self, source: Path, key: str) -> StoredObject: ...

    @abstractmethod
    def put_bytes(self, data: bytes, key: str) -> StoredObject: ...

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str) -> list[StoredObject]: ...

    @abstractmethod
    def writer(self, key: str) -> AbstractContextManager[Path]:
        """Yield a local path to write to; the file is committed to ``key`` on exit.

        Used for files produced incrementally (run logs). Local storage writes
        in place; a remote backend would upload the file when the block exits.
        """

    @property
    @abstractmethod
    def location(self) -> str:
        """Human-readable place artifacts are written to (path or bucket URL)."""

    @abstractmethod
    def prune(self, older_than: date) -> int:
        """Delete run artifacts from dates before ``older_than``; return objects removed."""


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """Builds storage keys for one run: ``<area>/<report>/<date>/<run_id>/<name>``."""

    report: str
    run_date: date
    run_id: str

    def key(self, area: Area, name: str) -> str:
        return f"{area}/{self.report}/{self.run_date.isoformat()}/{self.run_id}/{name}"

    def prefixes(self) -> Iterator[tuple[Area, str]]:
        for area in AREAS:
            yield area, self.key(area, "")
