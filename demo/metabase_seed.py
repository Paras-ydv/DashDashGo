"""Seed the demo Metabase instance with the dashboards DashDashGo acquires reports from.

This is *test-environment* tooling, not part of the DashDashGo product. It is
idempotent: every object is looked up by name first and only created when
missing, so it is safe to run on every `docker compose up`.

What it creates (mirroring the three scenarios in the assignment):

    Sales/Sales Report            dashboard -> "Weekly Sales" question       (CSV)
    Customer Success/Customer Usage dashboard with a "Usage Date" filter
                                  -> "Daily Customer Usage" question         (XLSX)
    Finance Archive/Q4 Budget Review question                                (JSON)

Only the standard library is used so the script runs anywhere Python does.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("metabase-seed")

WAREHOUSE_NAME = "Demo Warehouse"

WEEKLY_SALES_SQL = """\
-- Previous complete ISO week (Monday..Sunday), one row per day/region/product.
SELECT
    sale_date    AS "Date",
    region       AS "Region",
    product_name AS "Product",
    category     AS "Category",
    orders       AS "Orders",
    units        AS "Units Sold",
    revenue      AS "Revenue"
FROM sales.daily_product_sales
WHERE sale_date >= date_trunc('week', current_date) - interval '7 days'
  AND sale_date <  date_trunc('week', current_date)
ORDER BY 1, 2, 3
"""

CUSTOMER_USAGE_SQL = """\
-- Long format: one row per day, account and metric.
SELECT
    usage_date   AS "Usage Date",
    account_id   AS "Account ID",
    account_name AS "Account",
    plan         AS "Plan",
    metric       AS "Metric",
    value        AS "Value"
FROM product.usage_metrics_daily
WHERE {{usage_date}}
ORDER BY 1, 2, 5
"""

BUDGET_REVIEW_SQL = """\
SELECT line_id, fiscal_quarter, period_month, department, cost_center,
       category, budget, actual, details
FROM finance.budget_lines
ORDER BY line_id
"""


class MetabaseAPI:
    """Minimal JSON client for the Metabase REST API."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id: str | None = None

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.session_id:
            req.add_header("X-Metabase-Session", self.session_id)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        return json.loads(raw) if raw else None

    def wait_until_healthy(self, timeout_s: int = 300) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.request("GET", "/api/health").get("status") == "ok":
                    return
            except (OSError, RuntimeError):
                pass
            time.sleep(3)
        raise TimeoutError(f"Metabase at {self.base_url} not healthy after {timeout_s}s")


def ensure_admin(api: MetabaseAPI, email: str, password: str) -> None:
    props = api.request("GET", "/api/session/properties")
    if not props.get("has-user-setup"):
        log.info("Running first-time Metabase setup for %s", email)
        api.request(
            "POST",
            "/api/setup",
            {
                "token": props["setup-token"],
                "user": {
                    "email": email,
                    "password": password,
                    "first_name": "DashDashGo",
                    "last_name": "Analyst",
                    "site_name": "DashDashGo Demo",
                },
                "prefs": {
                    "site_name": "DashDashGo Demo",
                    "site_locale": "en",
                    "allow_tracking": False,
                },
            },
        )
    api.session_id = api.request("POST", "/api/session", {"username": email, "password": password})[
        "id"
    ]


def ensure_warehouse(api: MetabaseAPI, host: str, reader_password: str) -> int:
    databases = api.request("GET", "/api/database")["data"]
    existing = next((db for db in databases if db["name"] == WAREHOUSE_NAME), None)
    if existing:
        return int(existing["id"])
    log.info("Registering '%s' database", WAREHOUSE_NAME)
    created = api.request(
        "POST",
        "/api/database",
        {
            "engine": "postgres",
            "name": WAREHOUSE_NAME,
            "details": {
                "host": host,
                "port": 5432,
                "dbname": "warehouse",
                "user": "metabase_reader",
                "password": reader_password,
                "ssl": False,
            },
        },
    )
    return int(created["id"])


def wait_for_field(api: MetabaseAPI, db_id: int, schema: str, table: str, field: str) -> int:
    """Field filters need Metabase's synced field id; sync is asynchronous."""
    api.request("POST", f"/api/database/{db_id}/sync_schema")
    for _ in range(60):
        metadata = api.request("GET", f"/api/database/{db_id}/metadata")
        for tbl in metadata.get("tables", []):
            if tbl["schema"] == schema and tbl["name"] == table:
                for fld in tbl.get("fields", []):
                    if fld["name"] == field:
                        return int(fld["id"])
        time.sleep(2)
    raise TimeoutError(f"Metabase never synced {schema}.{table}.{field}")


def ensure_collection(api: MetabaseAPI, name: str) -> int:
    for coll in api.request("GET", "/api/collection"):
        if coll.get("name") == name and not coll.get("archived") and coll.get("location") == "/":
            return int(coll["id"])
    log.info("Creating collection '%s'", name)
    return int(api.request("POST", "/api/collection", {"name": name, "parent_id": None})["id"])


def _collection_item(api: MetabaseAPI, collection_id: int, model: str, name: str) -> int | None:
    items = api.request("GET", f"/api/collection/{collection_id}/items?models={model}")["data"]
    return next((int(i["id"]) for i in items if i["name"] == name), None)


def ensure_card(
    api: MetabaseAPI,
    *,
    name: str,
    collection_id: int,
    db_id: int,
    sql: str,
    template_tags: dict[str, Any] | None = None,
    parameters: list[dict[str, Any]] | None = None,
) -> int:
    existing = _collection_item(api, collection_id, "card", name)
    if existing is not None:
        return existing
    log.info("Creating question '%s'", name)
    card = api.request(
        "POST",
        "/api/card",
        {
            "name": name,
            "type": "question",
            "display": "table",
            "visualization_settings": {},
            "collection_id": collection_id,
            "parameters": parameters or [],
            "dataset_query": {
                "type": "native",
                "database": db_id,
                "native": {"query": sql, "template-tags": template_tags or {}},
            },
        },
    )
    return int(card["id"])


def ensure_dashboard(
    api: MetabaseAPI,
    *,
    name: str,
    collection_id: int,
    card_id: int,
    parameters: list[dict[str, Any]] | None = None,
    parameter_mappings: list[dict[str, Any]] | None = None,
) -> int:
    existing = _collection_item(api, collection_id, "dashboard", name)
    if existing is not None:
        return existing
    log.info("Creating dashboard '%s'", name)
    dashboard_id = int(
        api.request(
            "POST",
            "/api/dashboard",
            {"name": name, "collection_id": collection_id, "parameters": parameters or []},
        )["id"]
    )
    api.request(
        "PUT",
        f"/api/dashboard/{dashboard_id}",
        {
            "dashcards": [
                {
                    "id": -1,
                    "card_id": card_id,
                    "row": 0,
                    "col": 0,
                    "size_x": 24,
                    "size_y": 12,
                    "parameter_mappings": parameter_mappings or [],
                }
            ]
        },
    )
    return dashboard_id


def seed(api: MetabaseAPI) -> None:
    ensure_admin(api, os.environ["METABASE_USERNAME"], os.environ["METABASE_PASSWORD"])
    db_id = ensure_warehouse(
        api, os.environ.get("DEMO_WAREHOUSE_HOST", "postgres"), os.environ["DEMO_READER_PASSWORD"]
    )

    # Scenario 1: Sales Report dashboard -> Weekly Sales (CSV)
    sales = ensure_collection(api, "Sales")
    weekly_sales = ensure_card(
        api, name="Weekly Sales", collection_id=sales, db_id=db_id, sql=WEEKLY_SALES_SQL
    )
    ensure_dashboard(api, name="Sales Report", collection_id=sales, card_id=weekly_sales)

    # Scenario 2: Customer Usage dashboard filtered to the last 7 days (XLSX)
    usage_field = wait_for_field(api, db_id, "product", "usage_metrics_daily", "usage_date")
    tag_id = "4d1f6a2e-9d0b-4c55-8f55-2b6f1c1a7e01"
    usage_param = {
        "id": tag_id,
        "name": "Usage Date",
        "slug": "usage_date",
        "type": "date/all-options",
        "target": ["dimension", ["template-tag", "usage_date"]],
    }
    customer_success = ensure_collection(api, "Customer Success")
    usage_card = ensure_card(
        api,
        name="Daily Customer Usage",
        collection_id=customer_success,
        db_id=db_id,
        sql=CUSTOMER_USAGE_SQL,
        template_tags={
            "usage_date": {
                "id": tag_id,
                "name": "usage_date",
                "display-name": "Usage Date",
                "type": "dimension",
                "dimension": ["field", usage_field, None],
                "widget-type": "date/all-options",
            }
        },
        parameters=[usage_param],
    )
    dashboard_param_id = "b7c3e0d2"
    ensure_dashboard(
        api,
        name="Customer Usage",
        collection_id=customer_success,
        card_id=usage_card,
        parameters=[
            {
                "id": dashboard_param_id,
                "name": "Usage Date",
                "slug": "usage_date",
                "type": "date/all-options",
                "sectionId": "date",
            }
        ],
        parameter_mappings=[
            {
                "parameter_id": dashboard_param_id,
                "card_id": usage_card,
                "target": ["dimension", ["template-tag", "usage_date"]],
            }
        ],
    )

    # Scenario 3: Finance Archive -> Q4 Budget Review (JSON)
    finance = ensure_collection(api, "Finance Archive")
    ensure_card(
        api, name="Q4 Budget Review", collection_id=finance, db_id=db_id, sql=BUDGET_REVIEW_SQL
    )
    log.info("Metabase demo content is ready")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    api = MetabaseAPI(os.environ.get("METABASE_URL", "http://metabase:3000"))
    try:
        api.wait_until_healthy()
        seed(api)
    except (KeyError, RuntimeError, TimeoutError) as exc:
        log.error("Seeding failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
