"""Artifact storage."""

from dashdashgo.storage.base import AREAS, Area, RunArtifacts, StorageBackend, StoredObject
from dashdashgo.storage.local import LocalStorage

__all__ = ["AREAS", "Area", "LocalStorage", "RunArtifacts", "StorageBackend", "StoredObject"]
