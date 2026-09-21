"""Structured logging with run context and secret redaction.

* Every record is stamped with ``run_id``, ``report`` and ``stage`` from
  context variables, so any module can just ``log.info(...)`` and the output
  still says which run and stage it belongs to.
* Secret values (passwords, tokens) registered with :data:`redactor` are masked
  in every message and traceback before any handler sees them.
* Each run gets its own JSON-lines log file (see :func:`run_log_file`), which
  the UI and API serve per run.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_report: ContextVar[str | None] = ContextVar("report", default=None)
_stage: ContextVar[str | None] = ContextVar("stage", default=None)

MASK = "********"


class SecretRedactor:
    """Thread-safe registry of secret values that must never appear in logs."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, *values: str | None) -> None:
        with self._lock:
            # Very short values would mask ordinary words; secrets are never that short.
            self._secrets.update(v for v in values if v and len(v) >= 4)

    def redact(self, text: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, MASK)
        return text


redactor = SecretRedactor()


@contextmanager
def log_context(
    *, run_id: str | None = None, report: str | None = None, stage: str | None = None
) -> Iterator[None]:
    tokens = []
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    if report is not None:
        tokens.append((_report, _report.set(report)))
    if stage is not None:
        tokens.append((_stage, _stage.set(stage)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_stage() -> str | None:
    return _stage.get()


class ContextFilter(logging.Filter):
    """Stamps records with run context and redacts secrets from the message."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = getattr(record, "run_id", None) or _run_id.get()
        record.report = getattr(record, "report", None) or _report.get()
        record.stage = getattr(record, "stage", None) or _stage.get()
        if not getattr(record, "_redacted", False):
            record.msg = redactor.redact(record.getMessage())
            record.args = None
            record._redacted = True
        return True


class _RedactingFormatter(logging.Formatter):
    def formatException(self, ei: Any) -> str:  # noqa: N802 - stdlib API
        return redactor.redact(super().formatException(ei))


class TextFormatter(_RedactingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        context = "".join(
            f"[{label}={value}] "
            for label, value in (
                ("run", getattr(record, "run_id", None)),
                ("report", getattr(record, "report", None)),
                ("stage", getattr(record, "stage", None)),
            )
            if value
        )
        timestamp = datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} {record.levelname:<7} {context}{record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(_RedactingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", None),
            "report": getattr(record, "report", None),
            "stage": getattr(record, "stage", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("apscheduler", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Connection failures are reported by our own error handling with context;
    # the drivers' low-level retry chatter would only duplicate it.
    for driver in ("urllib3", "clickhouse_connect"):
        logging.getLogger(driver).setLevel(logging.ERROR)


class _RunFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        return (getattr(record, "run_id", None) or _run_id.get()) == self.run_id


@contextmanager
def run_log_file(path: Path, run_id: str) -> Iterator[None]:
    """Capture every record of one run (and only that run) into a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.addFilter(ContextFilter())
    handler.addFilter(_RunFilter(run_id))
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()
