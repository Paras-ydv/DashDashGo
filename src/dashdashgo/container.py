"""Composition root: builds and wires every service from Settings.

Nothing else in the codebase constructs infrastructure objects, which keeps
modules free of global state and lets tests swap any dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

from dashdashgo.acquisition.service import AcquisitionService
from dashdashgo.config.loader import ReportRegistry
from dashdashgo.ingestion.service import IngestionService
from dashdashgo.metadata.repository import ClickHouseRunRepository, RunRepository
from dashdashgo.observability.logging import redactor
from dashdashgo.orchestration.pipeline import PipelineOrchestrator
from dashdashgo.orchestration.runner import ReportLock, RunService
from dashdashgo.settings import Settings
from dashdashgo.storage import LocalStorage, StorageBackend
from dashdashgo.warehouse.client import ClickHouse
from dashdashgo.warehouse.loader import WarehouseLoader
from dashdashgo.warehouse.reader import ReportDataReader


@dataclass
class Container:
    settings: Settings
    clickhouse: ClickHouse
    registry: ReportRegistry
    storage: StorageBackend
    runs: RunRepository
    data_reader: ReportDataReader
    run_service: RunService


def build_container(settings: Settings) -> Container:
    redactor.register(settings.clickhouse_password.get_secret_value())
    clickhouse = ClickHouse(settings)
    storage = LocalStorage(settings.storage_root)
    runs = ClickHouseRunRepository(clickhouse, settings.clickhouse_metadata_database)
    registry = ReportRegistry(settings.reports_dir)
    orchestrator = PipelineOrchestrator(
        acquisition=AcquisitionService(storage),
        ingestion=IngestionService(),
        loader=WarehouseLoader(clickhouse),
        runs=runs,
        storage=storage,
    )
    run_service = RunService(
        registry=registry,
        orchestrator=orchestrator,
        runs=runs,
        lock=ReportLock(settings.storage_root / ".locks"),
        max_workers=settings.max_concurrent_runs,
    )
    return Container(
        settings=settings,
        clickhouse=clickhouse,
        registry=registry,
        storage=storage,
        runs=runs,
        data_reader=ReportDataReader(clickhouse),
        run_service=run_service,
    )
