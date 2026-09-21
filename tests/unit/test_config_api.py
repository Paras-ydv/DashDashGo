"""Config management API and editor pages."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dashdashgo.config.loader import ReportRegistry
from dashdashgo.config.store import ConfigStore
from tests.conftest import REPORTS_DIR, TEST_ENV


@pytest.fixture
def api(tmp_path: Path) -> Iterator[TestClient]:
    from dashdashgo.container import Container
    from dashdashgo.distribution.app import create_app
    from dashdashgo.settings import Settings
    from dashdashgo.storage import LocalStorage
    from tests.fakes import InMemoryRunRepository
    from tests.unit.test_service_layer import FakeClickHouse, FakeLoader, make_service

    directory = tmp_path / "reports"
    shutil.copytree(REPORTS_DIR, directory)
    registry = ReportRegistry(directory, TEST_ENV)
    service, _, _ = make_service(directory, tmp_path)
    container = Container(
        settings=Settings(),
        clickhouse=FakeClickHouse(),  # type: ignore[arg-type]
        registry=registry,
        storage=LocalStorage(tmp_path / "storage"),
        runs=InMemoryRunRepository(),
        data_reader=None,  # type: ignore[arg-type]
        run_service=service,
        config_store=ConfigStore(registry),
        loader=FakeLoader(),  # type: ignore[arg-type]
    )
    with TestClient(create_app(Settings(), container=container, scheduler=False)) as client:
        yield client


def test_api_edit_roundtrip(api: TestClient) -> None:
    doc = api.get("/api/reports/weekly_sales/config").json()
    assert "${METABASE_PASSWORD}" in doc["yaml"]  # raw file: references, never secrets

    edited = doc["yaml"].replace("max_attempts: 3", "max_attempts: 2")
    url = "/api/reports/weekly_sales/config"
    check: dict[str, Any] = api.post(f"{url}/validate", json={"yaml": edited}).json()
    assert check["valid"] and check["summary"]["destination"] == "analytics.sales_metrics"

    body = {"yaml": edited, "base_version": doc["version"]}
    assert api.put(url, json=body).status_code == 200
    assert api.put(url, json=body).status_code == 409  # base version is now stale
    assert len(api.get("/api/reports/weekly_sales/config/history").json()) == 1


def test_api_validation_reports_located_problems(api: TestClient) -> None:
    url = "/api/reports/weekly_sales/config/validate"
    body = api.post(url, json={"yaml": "name: weekly_sales\n"}).json()
    assert body["valid"] is False
    assert {"source", "destination"} <= {p["location"] for p in body["problems"]}


def test_api_create_warns_about_shared_table_and_archives(api: TestClient) -> None:
    params = {"name": "sales_copy", "source": "weekly_sales"}
    template = api.get("/api/config-templates", params=params).json()
    check = api.post("/api/reports/sales_copy/config/validate", json=template).json()
    assert any("also written by weekly_sales" in w for w in check["summary"]["warnings"])
    assert api.post("/api/reports", json={"name": "sales_copy", **template}).status_code == 201
    assert api.get("/reports/sales_copy/edit").status_code == 200
    assert api.delete("/api/reports/sales_copy").status_code == 200
    assert api.get("/api/reports/sales_copy/config").status_code == 404


def test_editor_pages_render(api: TestClient) -> None:
    page = api.get("/reports/new?source=customer_usage")
    assert page.status_code == 200 and "Create pipeline" in page.text
    # the transform reference is rendered from the registry
    assert "pivot" in api.get("/reports/weekly_sales/edit").text


def test_unknown_ui_pages_render_html_but_api_stays_json(api: TestClient) -> None:
    page = api.get("/runs/does-not-exist")
    assert page.status_code == 404 and "text/html" in page.headers["content-type"]
    assert "Not found" in page.text and "does-not-exist" in page.text
    assert api.get("/no/such/page").status_code == 404
    api_error = api.get("/api/runs/does-not-exist")
    assert api_error.status_code == 404 and api_error.json()["detail"]
