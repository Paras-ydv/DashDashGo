"""Login and cross-site request protection for the UI and API."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.unit.test_config_api import make_client

AUTH = ("admin", "correct-horse-battery")


@pytest.fixture
def secured(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path, auth_username=AUTH[0], auth_password=AUTH[1]) as client:
        yield client


def test_everything_but_health_and_static_needs_login(secured: TestClient) -> None:
    for path in ("/", "/runs", "/api/reports", "/api/runs", "/docs"):
        response = secured.get(path)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"].startswith("Basic")
    assert secured.get("/api/health").status_code in (200, 503)
    assert secured.get("/static/app.js").status_code == 200


def test_login_with_the_right_credentials_only(secured: TestClient) -> None:
    assert secured.get("/api/reports", auth=AUTH).status_code == 200
    assert secured.get("/api/reports", auth=(AUTH[0], "wrong")).status_code == 401
    assert secured.get("/api/reports", auth=("someone", AUTH[1])).status_code == 401
    assert (
        secured.get("/api/reports", headers={"Authorization": "Basic !!notbase64"}).status_code
        == 401
    )


def test_auth_is_off_unless_both_settings_are_set(tmp_path: Path) -> None:
    with make_client(tmp_path, auth_username="admin") as client:
        assert client.get("/api/reports").status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example.com"},
        {"Origin": "null"},
    ],
)
def test_cross_site_writes_are_refused(secured: TestClient, headers: dict[str, str]) -> None:
    response = secured.post("/api/reports/weekly_sales/runs", auth=AUTH, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "cross-site request refused"}


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, scripts, the CLI
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
        {"Origin": "http://testserver"},
    ],
)
def test_same_origin_and_non_browser_writes_are_allowed(
    secured: TestClient, headers: dict[str, str]
) -> None:
    response = secured.post(
        "/api/reports/weekly_sales/config/validate",
        auth=AUTH,
        headers=headers,
        json={"yaml": "name: weekly_sales\n"},
    )
    assert response.status_code != 403


def test_cross_site_reads_are_not_blocked(secured: TestClient) -> None:
    response = secured.get("/api/reports", auth=AUTH, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200
