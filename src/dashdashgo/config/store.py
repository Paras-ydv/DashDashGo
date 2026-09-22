"""Read, validate, save and version report config files.

Used by the UI's config editor, the API and the ``dashdashgo config`` CLI.

* Every save is validated exactly like a run would validate it.
* Saves are version-checked (optimistic concurrency): if the file changed
  since it was opened, the save is refused instead of silently overwriting.
* The previous version is kept in ``reports/.history/<name>/`` before every
  save, so any edit can be inspected or rolled back.
* Deleting archives the file to ``reports/.archive/``; nothing is destroyed.
* Secrets must be ``${ENV_VAR}`` references: the editor can never be used to
  store a password in a file.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashdashgo.config.loader import (
    REPORT_NAME,
    ReportRegistry,
    is_env_reference,
    parse_yaml,
    validate_report,
)
from dashdashgo.config.models import ReportConfig
from dashdashgo.errors import ConfigConflictError, ConfigurationError, ReportNotConfiguredError

HISTORY_DIR = ".history"
ARCHIVE_DIR = ".archive"
MAX_HISTORY = 50
SEEDED_MANIFEST = ".seeded"  # names of bundled reports already offered to this directory
RESERVED_NAMES = {"new"}  # /reports/new is the UI's create page
_SECRET_HINT = "secrets must reference an environment variable, e.g. ${METABASE_PASSWORD}"
_SECRET_KEY = re.compile(r"(password|secret|token|api_?key)$", re.IGNORECASE)

BLANK_TEMPLATE = """\
# New DashDashGo pipeline. Every field is documented in the README (Configuration).
name: {name}
description: What this report contains and who uses it.

source:
  platform: metabase
  base_url: ${{METABASE_URL}}
  credentials:
    username: ${{METABASE_USERNAME}}
    password: ${{METABASE_PASSWORD}}     # secrets must be ${{ENV_VAR}} references
  location:
    collection: [Sales]                  # collection path from "Our analytics"
    dashboard: Sales Report              # or:  question: <question name>
    card: Weekly Sales                   # card title on the dashboard
  filters: {{}}                          # e.g. {{usage_date: past7days}}
  export:
    format: csv                          # csv | xlsx | json

ingestion:
  transforms:
    - normalize_columns
    - strip_whitespace
  quality:
    on_invalid_rows: quarantine          # fail | drop | quarantine
    max_invalid_ratio: 0.05
    rules: []

destination:
  database: analytics
  table: {name}
  order_by: [report_date]                # the natural key of a row
  columns:
    - {{name: report_date, type: Date}}

schedule:
  enabled: false
  cron: "0 8 * * 1"
  timezone: UTC
"""


@dataclass(frozen=True)
class ConfigDocument:
    name: str
    text: str
    version: str
    modified_at: datetime


@dataclass(frozen=True)
class ConfigVersion:
    id: str
    saved_at: datetime
    size: int


def content_version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _find_plaintext_secrets(node: Any, path: str = "") -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            location = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)) and not isinstance(value, dict | list):
                if value not in (None, "") and not is_env_reference(value):
                    problems.append((location, _SECRET_HINT))
            else:
                problems += _find_plaintext_secrets(value, location)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            problems += _find_plaintext_secrets(item, f"{path}.{index}")
    return problems


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


class ConfigStore:
    def __init__(self, registry: ReportRegistry) -> None:
        self._registry = registry
        self._dir = registry.reports_dir
        self._write_lock = threading.Lock()  # version check + write must be atomic

    # --- reading ------------------------------------------------------------------

    def read(self, name: str) -> ConfigDocument:
        path = self._registry.path_for(name)
        text = path.read_text(encoding="utf-8")
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        return ConfigDocument(name, text, content_version(text), modified)

    def template(self, name: str, source: str | None = None) -> str:
        """Starting text for a new report: a copy of ``source`` or the blank template."""
        if source:
            text = self.read(source).text
            return re.sub(r"(?m)^name:\s*\S+", f"name: {name}", text, count=1)
        return BLANK_TEMPLATE.format(name=name)

    # --- validation ---------------------------------------------------------------

    def check(self, name: str, text: str) -> ReportConfig:
        """Validate editor text as the config for ``name``; raise ConfigurationError."""
        source = f"{name}.yaml"
        raw = parse_yaml(text, source)
        secrets = _find_plaintext_secrets(raw)
        try:
            config = validate_report(raw, source=source, expected_name=name, env=self._registry.env)
        except ConfigurationError as exc:
            if not secrets:
                raise
            problems = secrets + exc.problems
        else:
            if not secrets:
                return config
            problems = secrets
        details = "\n".join(f"  - {loc}: {msg}" for loc, msg in problems)
        raise ConfigurationError(f"{source}: invalid configuration\n{details}", problems=problems)

    # --- writing ------------------------------------------------------------------

    def save(self, name: str, text: str, base_version: str) -> ConfigDocument:
        self.check(name, text)
        with self._write_lock:
            return self._save(name, text, base_version)

    def _save(self, name: str, text: str, base_version: str) -> ConfigDocument:
        current = self.read(name)
        if current.version != base_version:
            raise ConfigConflictError(
                f"{name}.yaml changed since it was opened (version {base_version} -> "
                f"{current.version}); reload it and re-apply your changes"
            )
        if current.text == text:
            return current
        self._snapshot(name, current.text)
        _write_atomic(self._dir / f"{name}.yaml", text)
        return self.read(name)

    def create(self, name: str, text: str) -> ConfigDocument:
        if not REPORT_NAME.fullmatch(name):
            raise ConfigurationError(
                f"invalid report name {name!r}: use lowercase letters, digits and underscores",
                problems=[("name", "use lowercase letters, digits and underscores (2-63 chars)")],
            )
        if name in RESERVED_NAMES:
            raise ConfigurationError(
                f"{name!r} is reserved", problems=[("name", f"{name!r} is reserved by the UI")]
            )
        self.check(name, text)
        with self._write_lock:
            if (self._dir / f"{name}.yaml").exists():
                raise ConfigConflictError(f"a report named {name!r} already exists")
            _write_atomic(self._dir / f"{name}.yaml", text)
        return self.read(name)

    def archive(self, name: str) -> str:
        path = self._registry.path_for(name)
        target = self._dir / ARCHIVE_DIR / f"{name}-{_timestamp()}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        return str(target.relative_to(self._dir))

    def seed_bundled(self, bundled_dir: Path) -> list[str]:
        """Add reports shipped with the application that this directory has never had.

        Add-only: an existing file is never overwritten, and a report that was
        seeded once is never re-added (so archiving or deleting it sticks). This
        lets new releases ship new reports into an existing, user-edited volume.
        """
        if not bundled_dir.is_dir() or bundled_dir.resolve() == self._dir.resolve():
            return []
        manifest = self._dir / SEEDED_MANIFEST
        seeded = set(manifest.read_text().split()) if manifest.is_file() else set()
        added = []
        for source in sorted(bundled_dir.glob("*.yaml")):
            if source.stem in seeded:
                continue
            target = self._dir / source.name
            if not target.exists():
                _write_atomic(target, source.read_text(encoding="utf-8"))
                added.append(source.stem)
            seeded.add(source.stem)
        _write_atomic(manifest, "".join(f"{name}\n" for name in sorted(seeded)))
        return added

    # --- history ------------------------------------------------------------------

    def _history_dir(self, name: str) -> Path:
        if not REPORT_NAME.fullmatch(name):
            raise ReportNotConfiguredError(f"no report named {name!r}")
        return self._dir / HISTORY_DIR / name

    def _snapshot(self, name: str, text: str) -> None:
        directory = self._history_dir(name)
        _write_atomic(directory / f"{_timestamp()}.yaml", text)
        for old in sorted(directory.glob("*.yaml"))[:-MAX_HISTORY]:
            old.unlink()

    def history(self, name: str) -> list[ConfigVersion]:
        directory = self._history_dir(name)
        versions = []
        for path in sorted(directory.glob("*.yaml"), reverse=True):
            saved = datetime.strptime(path.stem, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
            versions.append(ConfigVersion(path.stem, saved, path.stat().st_size))
        return versions

    def read_version(self, name: str, version_id: str) -> str:
        if not re.fullmatch(r"\d{8}T\d{12}Z", version_id):
            raise ReportNotConfiguredError(f"no version {version_id!r} of {name!r}")
        path = self._history_dir(name) / f"{version_id}.yaml"
        if not path.is_file():
            raise ReportNotConfiguredError(f"no version {version_id!r} of {name!r}")
        return path.read_text(encoding="utf-8")
