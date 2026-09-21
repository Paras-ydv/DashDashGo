"""Retry policy, storage, logging/redaction and download validation."""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import date
from pathlib import Path

import pytest

from dashdashgo.acquisition.validation import validate_download
from dashdashgo.config.models import ReportFormat
from dashdashgo.errors import (
    AuthenticationError,
    DownloadError,
    NavigationError,
    StorageError,
    WarehouseConnectionError,
)
from dashdashgo.observability.logging import (
    ContextFilter,
    TextFormatter,
    log_context,
    redactor,
    run_log_file,
)
from dashdashgo.storage import LocalStorage, RunArtifacts
from dashdashgo.utils.retry import RetryPolicy, call_with_retry, is_retryable

# --- retry -------------------------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(max_attempts=5, initial_delay=2, multiplier=2, max_delay=10)
    assert [policy.delay_after(n) for n in range(1, 5)] == [2, 4, 8, 10]


def test_retries_retryable_errors_until_success() -> None:
    sleeps: list[float] = []
    calls: list[int] = []

    def flaky(attempt: int) -> str:
        calls.append(attempt)
        if attempt < 3:
            raise NavigationError("timeout")
        return "ok"

    policy = RetryPolicy(max_attempts=3, initial_delay=1, multiplier=2)
    assert call_with_retry(flaky, policy, description="t", sleep=sleeps.append) == "ok"
    assert calls == [1, 2, 3]
    assert sleeps == [1, 2]


def test_non_retryable_errors_fail_immediately() -> None:
    calls: list[int] = []

    def bad_password(attempt: int) -> None:
        calls.append(attempt)
        raise AuthenticationError("rejected")

    with pytest.raises(AuthenticationError):
        call_with_retry(
            bad_password, RetryPolicy(max_attempts=5), description="t", sleep=lambda _: None
        )
    assert calls == [1]


def test_gives_up_after_max_attempts() -> None:
    calls: list[int] = []

    def down(attempt: int) -> None:
        calls.append(attempt)
        raise WarehouseConnectionError("down")

    with pytest.raises(WarehouseConnectionError):
        call_with_retry(
            down,
            RetryPolicy(max_attempts=3, initial_delay=0),
            description="t",
            sleep=lambda _: None,
        )
    assert calls == [1, 2, 3]


def test_retryability_classification() -> None:
    assert is_retryable(NavigationError("x"))
    assert is_retryable(DownloadError("x"))
    assert not is_retryable(DownloadError("wrong format", retryable=False))
    assert not is_retryable(AuthenticationError("x"))
    assert is_retryable(ConnectionError())
    assert not is_retryable(ValueError())


# --- storage -----------------------------------------------------------------------


def test_run_artifact_keys_are_partitioned_by_report_date_run() -> None:
    keys = RunArtifacts("weekly_sales", date(2026, 9, 21), "abc123")
    assert keys.key("raw", "report.csv") == "raw/weekly_sales/2026-09-21/abc123/report.csv"


def test_local_storage_roundtrip_and_listing(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    src = tmp_path / "in.csv"
    src.write_text("a,b\n")
    stored = storage.put_file(src, "raw/r/2026-09-21/run1/in.csv")
    storage.put_bytes(b"png", "screenshots/r/2026-09-21/run1/shot.png")
    assert stored.size == 4
    assert storage.read_bytes("raw/r/2026-09-21/run1/in.csv") == b"a,b\n"
    assert [o.key for o in storage.list("screenshots/r/")] == [
        "screenshots/r/2026-09-21/run1/shot.png"
    ]
    assert storage.list("raw/unknown/") == []


@pytest.mark.parametrize("key", ["../outside.txt", "raw/../../etc/passwd", "/", ""])
def test_local_storage_rejects_path_traversal(tmp_path: Path, key: str) -> None:
    storage = LocalStorage(tmp_path / "root")
    with pytest.raises(StorageError):
        storage.put_bytes(b"x", key)


def test_prune_removes_only_old_date_partitions(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    storage.put_bytes(b"old", "raw/r/2026-01-01/run1/a.csv")
    storage.put_bytes(b"new", "raw/r/2026-09-21/run2/a.csv")
    assert storage.prune(date(2026, 6, 1)) == 1
    assert not storage.exists("raw/r/2026-01-01/run1/a.csv")
    assert storage.exists("raw/r/2026-09-21/run2/a.csv")


# --- logging -----------------------------------------------------------------------


def test_secrets_are_redacted_from_messages_and_tracebacks() -> None:
    redactor.register("hunter2-secret")
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "login with %s", ("hunter2-secret",), None
    )
    try:
        raise RuntimeError("password=hunter2-secret")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    ContextFilter().filter(record)
    output = TextFormatter().format(record)
    assert "hunter2-secret" not in output
    assert "********" in output


def test_run_log_file_captures_only_its_run(tmp_path: Path) -> None:
    logger = logging.getLogger("dashdashgo.test")
    logger.setLevel(logging.INFO)
    path = tmp_path / "run.log"
    with run_log_file(path, "run-a"):
        with log_context(run_id="run-a", report="r", stage="parse"):
            logger.info("belongs to a")
        with log_context(run_id="run-b"):
            logger.info("belongs to b")
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["message"] for line in lines] == ["belongs to a"]
    assert lines[0]["run_id"] == "run-a" and lines[0]["stage"] == "parse"


# --- download validation ------------------------------------------------------------


def test_valid_downloads_return_hash(tmp_path: Path) -> None:
    csv_file = tmp_path / "r.csv"
    csv_file.write_text("a,b\n1,2\n")
    info = validate_download(csv_file, ReportFormat.CSV)
    assert info.size == 8 and len(info.sha256) == 64

    xlsx = tmp_path / "r.xlsx"
    with zipfile.ZipFile(xlsx, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    validate_download(xlsx, ReportFormat.XLSX)


@pytest.mark.parametrize(
    ("content", "fmt", "message", "retryable"),
    [
        (b"", ReportFormat.CSV, "empty", True),
        (b"<!DOCTYPE html><html>502</html>", ReportFormat.CSV, "HTML page", True),
        (b"a,b\n", ReportFormat.XLSX, "not an XLSX", False),
        (b"a,b\n", ReportFormat.JSON, "does not contain JSON", False),
        (b"\x00\x01binary", ReportFormat.CSV, "binary", False),
    ],
)
def test_bad_downloads_are_rejected(
    tmp_path: Path, content: bytes, fmt: ReportFormat, message: str, retryable: bool
) -> None:
    path = tmp_path / "download"
    path.write_bytes(content)
    with pytest.raises(DownloadError, match=message) as info:
        validate_download(path, fmt)
    assert info.value.retryable is retryable


def test_missing_download(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match="missing"):
        validate_download(tmp_path / "nope.csv", ReportFormat.CSV)
