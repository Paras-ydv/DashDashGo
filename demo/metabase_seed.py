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
import uuid
from dataclasses import dataclass
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

SUPPORT_TICKETS_SQL = """\
SELECT
    ticket_date                AS "Date",
    channel                    AS "Channel",
    priority                   AS "Priority",
    team                       AS "Team",
    tickets_opened             AS "Opened",
    tickets_resolved           AS "Resolved",
    median_first_response_min  AS "Median First Response (min)",
    csat_score                 AS "CSAT",
    sla_breaches               AS "SLA Breaches"
FROM support.ticket_daily
WHERE {{created_date}} AND {{priority}}
ORDER BY 1, 2, 3
"""

CAMPAIGNS_SQL = """\
SELECT
    report_date  AS "Date",
    channel      AS "Channel",
    campaign_id  AS "Campaign ID",
    campaign     AS "Campaign",
    impressions  AS "Impressions",
    clicks       AS "Clicks",
    conversions  AS "Conversions",
    spend        AS "Spend",
    revenue      AS "Revenue"
FROM marketing.campaign_daily
WHERE {{report_date}}
ORDER BY 1, 3
"""

WEB_TRAFFIC_SQL = """\
SELECT hour_start, page, sessions, pageviews, bounce_rate, avg_session_seconds
FROM web.traffic_hourly
WHERE {{hour_start}}
ORDER BY 1, 2
"""

INVENTORY_SQL = """\
-- Includes the summary row the upstream report appends; the pipeline drops it.
SELECT * FROM (
    SELECT snapshot_date, warehouse, sku, product, on_hand, reserved,
           reorder_point, unit_cost, supplier
    FROM ops.inventory_snapshot
    UNION ALL
    SELECT current_date, 'ALL', 'TOTAL', 'All products', sum(on_hand), sum(reserved),
           NULL, NULL, NULL
    FROM ops.inventory_snapshot
) AS inventory
ORDER BY warehouse = 'ALL', warehouse, sku
"""

MRR_SQL = """\
-- The billing system re-exports its latest row for reconciliation, so one row
-- appears twice - a realistic duplicate the pipeline must remove.
SELECT * FROM (
    SELECT month AS "Month", plan AS "Plan", new_mrr AS "New MRR",
           expansion_mrr AS "Expansion MRR", contraction_mrr AS "Contraction MRR",
           churned_mrr AS "Churned MRR", ending_mrr AS "Ending MRR", customers AS "Customers"
    FROM billing.mrr_monthly
    UNION ALL
    SELECT month, plan, new_mrr, expansion_mrr, contraction_mrr, churned_mrr, ending_mrr, customers
    FROM billing.mrr_monthly
    WHERE month = (SELECT max(month) FROM billing.mrr_monthly) AND plan = 'Growth'
) AS mrr
ORDER BY 1, 2
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
    for _ in range(150):  # up to 5 minutes on slow machines
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
    visualization_settings: dict[str, Any] | None = None,
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
            "visualization_settings": visualization_settings or {},
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


@dataclass(frozen=True)
class FieldFilter:
    """A native-query field filter exposed as a card and dashboard parameter."""

    slug: str
    name: str
    field_id: int
    widget: str  # "date/all-options" or "string/="

    @property
    def tag_id(self) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dashdashgo-demo/{self.slug}"))

    def template_tag(self) -> dict[str, Any]:
        return {
            "id": self.tag_id,
            "name": self.slug,
            "display-name": self.name,
            "type": "dimension",
            "dimension": ["field", self.field_id, None],
            "widget-type": self.widget,
        }

    def card_parameter(self) -> dict[str, Any]:
        return {
            "id": self.tag_id,
            "name": self.name,
            "slug": self.slug,
            "type": self.widget,
            "target": ["dimension", ["template-tag", self.slug]],
        }

    def dashboard_parameter(self) -> dict[str, Any]:
        return {
            "id": self.tag_id[:8],
            "name": self.name,
            "slug": self.slug,
            "type": self.widget,
            "sectionId": "date" if self.widget.startswith("date") else "string",
        }

    def mapping(self, card_id: int) -> dict[str, Any]:
        return {
            "parameter_id": self.tag_id[:8],
            "card_id": card_id,
            "target": ["dimension", ["template-tag", self.slug]],
        }


def filtered_card(
    api: MetabaseAPI,
    *,
    name: str,
    collection_id: int,
    db_id: int,
    sql: str,
    filters: list[FieldFilter],
    visualization_settings: dict[str, Any] | None = None,
) -> int:
    return ensure_card(
        api,
        name=name,
        collection_id=collection_id,
        db_id=db_id,
        sql=sql,
        template_tags={f.slug: f.template_tag() for f in filters},
        parameters=[f.card_parameter() for f in filters],
        visualization_settings=visualization_settings,
    )


def filtered_dashboard(
    api: MetabaseAPI, *, name: str, collection_id: int, card_id: int, filters: list[FieldFilter]
) -> int:
    return ensure_dashboard(
        api,
        name=name,
        collection_id=collection_id,
        card_id=card_id,
        parameters=[f.dashboard_parameter() for f in filters],
        parameter_mappings=[f.mapping(card_id) for f in filters],
    )


def _currency(*columns: str) -> dict[str, Any]:
    """Column formatting so 'Keep the data formatted' exports produce $1,234.56."""
    return {
        "column_settings": {
            json.dumps(["name", c]): {"number_style": "currency", "currency": "USD"}
            for c in columns
        }
    }


def seed(api: MetabaseAPI) -> None:
    ensure_admin(api, os.environ["METABASE_USERNAME"], os.environ["METABASE_PASSWORD"])
    db_id = ensure_warehouse(
        api, os.environ.get("DEMO_WAREHOUSE_HOST", "postgres"), os.environ["DEMO_READER_PASSWORD"]
    )

    def field(schema: str, table: str, column: str) -> int:
        return wait_for_field(api, db_id, schema, table, column)

    # Scenario 1: Sales Report dashboard -> Weekly Sales (CSV)
    sales = ensure_collection(api, "Sales")
    weekly_sales = ensure_card(
        api, name="Weekly Sales", collection_id=sales, db_id=db_id, sql=WEEKLY_SALES_SQL
    )
    ensure_dashboard(api, name="Sales Report", collection_id=sales, card_id=weekly_sales)

    # Scenario 2: Customer Usage dashboard filtered to the last 7 days (XLSX)
    customer_success = ensure_collection(api, "Customer Success")
    usage_date = FieldFilter(
        "usage_date",
        "Usage Date",
        field("product", "usage_metrics_daily", "usage_date"),
        "date/all-options",
    )
    usage_card = filtered_card(
        api,
        name="Daily Customer Usage",
        collection_id=customer_success,
        db_id=db_id,
        sql=CUSTOMER_USAGE_SQL,
        filters=[usage_date],
    )
    filtered_dashboard(
        api,
        name="Customer Usage",
        collection_id=customer_success,
        card_id=usage_card,
        filters=[usage_date],
    )

    # Scenario 3: Finance Archive -> Q4 Budget Review (JSON)
    finance = ensure_collection(api, "Finance Archive")
    ensure_card(
        api, name="Q4 Budget Review", collection_id=finance, db_id=db_id, sql=BUDGET_REVIEW_SQL
    )

    # Support Overview dashboard: date + multi-select category filter (CSV)
    created_date = FieldFilter(
        "created_date",
        "Created Date",
        field("support", "ticket_daily", "ticket_date"),
        "date/all-options",
    )
    priority = FieldFilter(
        "priority", "Priority", field("support", "ticket_daily", "priority"), "string/="
    )
    tickets_card = filtered_card(
        api,
        name="Daily Ticket Volume",
        collection_id=customer_success,
        db_id=db_id,
        sql=SUPPORT_TICKETS_SQL,
        filters=[created_date, priority],
    )
    filtered_dashboard(
        api,
        name="Support Overview",
        collection_id=customer_success,
        card_id=tickets_card,
        filters=[created_date, priority],
    )

    # Marketing Performance dashboard, currency-formatted columns (formatted CSV)
    marketing = ensure_collection(api, "Marketing")
    report_date = FieldFilter(
        "report_date",
        "Report Date",
        field("marketing", "campaign_daily", "report_date"),
        "date/all-options",
    )
    campaigns_card = filtered_card(
        api,
        name="Campaign Performance",
        collection_id=marketing,
        db_id=db_id,
        sql=CAMPAIGNS_SQL,
        filters=[report_date],
        visualization_settings=_currency("Spend", "Revenue"),
    )
    filtered_dashboard(
        api,
        name="Marketing Performance",
        collection_id=marketing,
        card_id=campaigns_card,
        filters=[report_date],
    )

    # Hourly Web Traffic question with a date-time filter (CSV)
    hour_start = FieldFilter(
        "hour_start", "Hour", field("web", "traffic_hourly", "hour_start"), "date/all-options"
    )
    filtered_card(
        api,
        name="Hourly Web Traffic",
        collection_id=marketing,
        db_id=db_id,
        sql=WEB_TRAFFIC_SQL,
        filters=[hour_start],
    )

    # Operations: today's inventory snapshot with nested supplier JSON (JSON)
    operations = ensure_collection(api, "Operations")
    ensure_card(
        api, name="Inventory Snapshot", collection_id=operations, db_id=db_id, sql=INVENTORY_SQL
    )

    # Finance Archive: SaaS Metrics dashboard -> MRR Movements (XLSX)
    mrr_card = ensure_card(
        api, name="MRR Movements", collection_id=finance, db_id=db_id, sql=MRR_SQL
    )
    ensure_dashboard(api, name="SaaS Metrics", collection_id=finance, card_id=mrr_card)
    log.info("Metabase demo content is ready")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    api = MetabaseAPI(os.environ.get("METABASE_URL", "http://metabase:3000"))
    for attempt in range(1, 4):
        try:
            api.wait_until_healthy()
            seed(api)
            return 0
        except KeyError as exc:
            log.error("Missing environment variable %s", exc)
            return 1
        except (RuntimeError, TimeoutError, OSError) as exc:
            # Metabase can still be finishing migrations right after it reports
            # healthy; seeding is idempotent, so simply try again.
            log.warning("Seeding attempt %d failed: %s", attempt, exc)
            time.sleep(10)
    log.error("Seeding failed after 3 attempts")
    return 1


if __name__ == "__main__":
    sys.exit(main())
