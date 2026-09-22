"""Exception hierarchy.

Every error raised by DashDashGo code derives from :class:`DashDashGoError` and
declares two things the orchestrator relies on:

* ``retryable`` - whether repeating the failed operation could plausibly succeed
  (a timeout: yes; a wrong password or a malformed file: no).
* ``stage`` - the pipeline stage the error belongs to, used for run metadata.

Errors may carry ``artifacts`` (storage keys of screenshots, traces, ...) that
were captured while handling them, so the UI can link failures to evidence.
"""

from __future__ import annotations

from typing import Any


class DashDashGoError(Exception):
    retryable: bool = False
    stage: str = "pipeline"

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        if retryable is not None:
            self.retryable = retryable
        self.artifacts: list[str] = []

    @property
    def error_type(self) -> str:
        return type(self).__name__


# --- configuration ------------------------------------------------------------


class ConfigurationError(DashDashGoError):
    """Invalid configuration. ``problems`` lists (location, message) pairs, e.g.
    ``("retry.max_attempts", "Input should be greater than or equal to 1")``,
    so editors can point at the exact field."""

    stage = "config"

    def __init__(self, message: str, *, problems: list[tuple[str, str]] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class ReportNotConfiguredError(ConfigurationError):
    """The requested report has no configuration file."""


class ConfigConflictError(DashDashGoError):
    """A config changed on disk since it was opened, or a new name is already taken."""

    stage = "config"


# --- acquisition --------------------------------------------------------------


class AcquisitionError(DashDashGoError):
    stage = "acquisition"


class BrowserError(AcquisitionError):
    """The browser could not be launched or crashed."""

    retryable = True


class AuthenticationError(AcquisitionError):
    """The dashboard rejected the credentials. Retrying would only risk a lockout."""


class NavigationError(AcquisitionError):
    """A page did not load or an expected element did not appear in time."""

    retryable = True


class ReportNotFoundError(AcquisitionError):
    """The configured collection/dashboard/question does not exist."""


class DownloadError(AcquisitionError):
    """The export did not produce a usable file."""

    retryable = True


# --- ingestion ----------------------------------------------------------------


class IngestionError(DashDashGoError):
    stage = "ingestion"


class ReportFormatError(IngestionError):
    """The downloaded file cannot be parsed as the configured format."""

    stage = "parse"


class SchemaDriftError(IngestionError):
    """The source report no longer has the columns the pipeline expects."""

    stage = "parse"


class TransformationError(IngestionError):
    stage = "transform"


class DataQualityError(IngestionError):
    """The dataset failed a quality policy. ``row_errors`` maps positions in the
    transformed frame to the problems that caused the failure (the rows are
    stored for inspection even though nothing is loaded)."""

    stage = "quality"

    def __init__(self, message: str, *, row_errors: dict[int, list[str]] | None = None) -> None:
        super().__init__(message)
        self.row_errors = row_errors or {}
        self.rejected: Any = None  # pandas DataFrame of those rows, attached by ingestion


# --- warehouse ----------------------------------------------------------------


class WarehouseError(DashDashGoError):
    stage = "load"


class WarehouseConnectionError(WarehouseError):
    retryable = True


class SchemaMismatchError(WarehouseError):
    """The destination table exists but its columns differ from the config."""

    stage = "preflight"


class LoadError(WarehouseError):
    retryable = True


class VerificationError(WarehouseError):
    stage = "verify"


# --- other --------------------------------------------------------------------


class StorageError(DashDashGoError):
    stage = "storage"


class ConcurrentRunError(DashDashGoError):
    """Another run of the same report is already in progress."""
