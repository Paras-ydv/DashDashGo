"""Local filesystem storage backend (a Docker volume in production)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from dashdashgo.errors import StorageError
from dashdashgo.storage.base import AREAS, StorageBackend, StoredObject


class LocalStorage(StorageBackend):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @property
    def location(self) -> str:
        return str(self.root)

    def _path(self, key: str) -> Path:
        """Resolve a key inside the root; reject traversal such as ``../../etc/passwd``."""
        relative = PurePosixPath(key.lstrip("/"))
        if ".." in relative.parts or not relative.parts:
            raise StorageError(f"invalid storage key: {key!r}")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError(f"invalid storage key: {key!r}")
        return path

    def _stored(self, key: str, path: Path) -> StoredObject:
        stat = path.stat()
        return StoredObject(key, stat.st_size, datetime.fromtimestamp(stat.st_mtime, UTC))

    def put_file(self, source: Path, key: str) -> StoredObject:
        target = self._path(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        except OSError as exc:
            raise StorageError(f"cannot store {source.name} as {key}: {exc}") from exc
        return self._stored(key, target)

    def put_bytes(self, data: bytes, key: str) -> StoredObject:
        target = self._path(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_bytes(data)
            tmp.replace(target)  # atomic: readers never see a half-written file
        except OSError as exc:
            raise StorageError(f"cannot write {key}: {exc}") from exc
        return self._stored(key, target)

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"no such artifact: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[StoredObject]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        return [
            self._stored(p.relative_to(self.root).as_posix(), p)
            for p in sorted(base.rglob("*"))
            if p.is_file() and not p.name.startswith(".")
        ]

    @contextmanager
    def writer(self, key: str) -> Iterator[Path]:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        yield path

    def prune(self, older_than: date) -> int:
        removed = 0
        for area in AREAS:
            area_dir = self.root / area
            if not area_dir.is_dir():
                continue
            for date_dir in area_dir.glob("*/*"):
                try:
                    run_date = date.fromisoformat(date_dir.name)
                except ValueError:
                    continue  # not a date partition - leave it alone
                if date_dir.is_dir() and run_date < older_than:
                    removed += sum(1 for p in date_dir.rglob("*") if p.is_file())
                    shutil.rmtree(date_dir)
        return removed
