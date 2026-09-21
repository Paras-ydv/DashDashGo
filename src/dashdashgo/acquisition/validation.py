"""Checks that a downloaded file is really the report we asked for.

A Playwright download event only means *some* bytes arrived. Dashboards
sometimes serve an HTML error page, an empty file or a JSON error body with a
.csv name; these checks catch that before parsing starts.
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from dashdashgo.config.models import ReportFormat
from dashdashgo.errors import DownloadError

MAX_REPORT_BYTES = 2 * 1024**3


@dataclass(frozen=True, slots=True)
class DownloadInfo:
    size: int
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_download(path: Path, fmt: ReportFormat) -> DownloadInfo:
    if not path.is_file():
        raise DownloadError(f"downloaded file is missing: {path.name}")
    size = path.stat().st_size
    if size == 0:
        raise DownloadError(f"downloaded file {path.name} is empty")
    if size > MAX_REPORT_BYTES:
        raise DownloadError(f"downloaded file is {size} bytes; refusing files over 2 GiB")

    with path.open("rb") as handle:
        head = handle.read(512)
    text_head = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text_head.startswith((b"<!doctype", b"<html")):
        raise DownloadError(f"{path.name} is an HTML page, not a {fmt.value.upper()} report")

    if fmt is ReportFormat.XLSX:
        if not zipfile.is_zipfile(path):
            raise DownloadError(f"{path.name} is not an XLSX workbook", retryable=False)
        with zipfile.ZipFile(path) as archive:
            if "xl/workbook.xml" not in archive.namelist():
                raise DownloadError(
                    f"{path.name} is a zip file but not an XLSX workbook", retryable=False
                )
    elif fmt is ReportFormat.JSON:
        if not text_head.startswith((b"[", b"{")):
            raise DownloadError(f"{path.name} does not contain JSON", retryable=False)
    elif fmt is ReportFormat.CSV and b"\x00" in head:
        raise DownloadError(f"{path.name} looks binary, not CSV text", retryable=False)

    return DownloadInfo(size=size, sha256=_sha256(path))
