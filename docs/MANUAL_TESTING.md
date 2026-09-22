# Manual testing guide

This is a hands-on walkthrough of every DashDashGo feature: the demo dashboards, the pipelines,
the UI, the config editor, the CLI, the API, failure handling, scheduling, storage and
security. Each step has an **action** and the **expected result**. Work top to bottom; later
sections assume the earlier runs exist.

> **Conventions**
> - `APP` = the DashDashGo URL, `http://localhost:8000` by default. If you set `APP_PORT`
>   in `.env` (for example because port 8000 is taken), use that port instead.
> - `ddg` = `docker compose exec app dashdashgo`. Define it once:
>   `alias ddg='docker compose exec app dashdashgo'`
> - Run IDs look like `20260921-184709-3fa66f` (UTC time + random suffix). Yours will differ.
> - Row counts below come from the demo data: 7 days × 4 regions × 10 products = **280**
>   sales rows; 7 days × 10 accounts × 4 metrics = **280** usage rows → **70** after the
>   pivot; **54** finance lines.

**Which address is which?** `http://metabase:3000` and `clickhouse:8123` are *Docker-internal*
names that containers use to reach each other, which is why they appear in configs and logs.
From your browser or terminal use the published ports: **Metabase → http://localhost:3000**,
ClickHouse HTTP → `http://localhost:8123`, DashDashGo → `APP`.

---

## 0. Start from a clean slate

| # | Action | Expected |
|---|---|---|
| 0.1 | `make clean` (deletes all volumes: data, runs, configs) | Containers and volumes removed |
| 0.2 | `make env` (only if `.env` is missing) | `Created .env with generated secrets`; `.env` has no `change-me` values |
| 0.3 | `make up` | Builds, then prints the UI, API docs and Metabase URLs. The first start takes about 2–4 minutes |
| 0.4 | `docker compose ps -a` | `clickhouse`, `postgres`, `metabase`, `app` → **Up (healthy)**; `metabase-seed` → **Exited (0)** |
| 0.5 | `docker compose logs metabase-seed \| tail -3` | Ends with `Metabase demo content is ready` |
| 0.6 | `curl -s APP/api/health` | `{"status":"ok","version":"1.0.0","clickhouse":"up","scheduler":"running","reports":8}` with HTTP 200 |
| 0.7 | `docker compose logs app \| grep "Added bundled"` | `Added bundled report configs: customer_usage, inventory_snapshot, …, weekly_sales` (all 8, on first start) |

---

## 1. The demo dashboards (Metabase)

Open **http://localhost:3000** and log in with `METABASE_USERNAME` / `METABASE_PASSWORD` from `.env`.

| # | Action | Expected |
|---|---|---|
| 1.1 | Sidebar → **Sales** → **Sales Report** | A dashboard with one card, **Weekly Sales**: 280 rows, Monday–Sunday of the previous complete week. Some values are deliberately messy: `" Vista 27" Monitor"` (leading space), categories `displays` / `audio ` (inconsistent case and padding) |
| 1.2 | Sidebar → **Customer Success** → **Customer Usage** | A dashboard with a **Usage Date** filter and the card **Daily Customer Usage** in long format (one row per day/account/metric) |
| 1.3 | Open `http://localhost:3000/dashboard/<id>?usage_date=past7days` (the Customer Usage dashboard id is in the URL from 1.2) | Filter shows **Previous 7 days**, card shows **280 rows** |
| 1.4 | Sidebar → **Finance Archive** → **Q4 Budget Review** | A question with 54 rows; the `details` column holds nested JSON (owner, approval, forecast); `actual` is empty for Marketing in December |
| 1.4a | Collections **Marketing** (*Marketing Performance* dashboard, *Hourly Web Traffic* question), **Operations** (*Inventory Snapshot*), **Customer Success** → *Support Overview*, **Finance Archive** → *SaaS Metrics* | The sources of the five extra pipelines (section 2b). *Support Overview* has two filters: **Created Date** and **Priority** |
| 1.5 | Wrong password on the Metabase login page | Inline error *"Did not match stored password"*. DashDashGo detects exactly this in step 6.1 |

---

## 2. First pipeline runs (UI)

Open **APP**.

| # | Action | Expected |
|---|---|---|
| 2.1 | Overview | Tiles show **0 runs** and "—" for rates. The Pipelines table lists **8 pipelines**, for example `customer_usage` (XLSX, `30 6 * * *`, "next in …"), `q4_budget_review` (JSON, *On demand*) and `weekly_sales` (CSV, `0 8 * * 1`). *Last run* = "Never run" |
| 2.2 | Click **Run** next to `weekly_sales` | Redirects to the run page with a *Running* pill and a **Live** dot. The timeline fills in while you watch; *Acquire report* expands into *Start browser → Log in → Locate report → Apply filters → Download → Validate download* |
| 2.3 | Wait about 5 s | Status **Success**. Every step is ticked with a duration. Result: *Rows downloaded 280, rejected 0, loaded 280*. *Run again* button. Artifacts: `raw/…csv`, `processed/data.parquet`, `logs/run.log`, `run.json` |
| 2.4 | Logs panel on the same page | About 25 INFO lines, each tagged with its stage. Switch to **Errors** → none |
| 2.5 | Pipeline `customer_usage` → **Run now** | Success: downloaded **280**, loaded **70** (pivoted to one row per account-day). *Apply filters* step says `usage_date=past7days via widget`: Playwright clicked the *Usage Date* widget and chose *Previous 7 days* |
| 2.5a | Artifacts on the run pages of 2.3 / 2.5 / 2.6 | Raw files are stored as **`Weekly_Sales.csv`**, **`Customer_Usage_7Day.xlsx`**, **`Q4_Budget_Review.json`** (`export.filename`); the *Download* step shows `<metabase name> -> <stored name>` |
| 2.6 | Pipeline `q4_budget_review` → **Run now** | Success: downloaded **54**, loaded **54** |
| 2.7 | Back to Overview | Runs 3, success rate **100%**, rows loaded **404**, a green cell in each pipeline's *Recent runs* strip (hover it for a tooltip) |

---

## 2b. The five additional pipelines and filter modes

| # | Action | Expected |
|---|---|---|
| 2b.1 | Run **`support_tickets`** | Success, **180 rows** (30 days × 3 channels × High/Urgent). *Apply filters*: `created_date=past30days, priority=High,Urgent via widget`: both widgets were operated in the UI (`filter_mode: widget`). Stored as `Support_Tickets_<date>.csv` |
| 2b.2 | Data preview of `support_tickets` | Only `High` and `Urgent` priorities; some `csat` values are `null` (days without survey answers), not 0 |
| 2b.3 | Run **`marketing_campaigns`** | Success, **180 rows**. *Apply filters*: `report_date=past30days via url` (`filter_mode: url`). Open the raw CSV under Artifacts: values look like `"$2,760.94"`, `"165,460"`, `"August 22, 2026"` (a formatted export). Data preview: exact decimals `2760.94`, dates, plus derived `ctr` and `roas` |
| 2b.4 | Run **`inventory_snapshot`** | Success: **downloaded 25, rejected 2, loaded 22**. The TOTAL summary row is dropped by `filter_rows`; the two negative-stock rows are quarantined. *Validate data* links **Rejected rows (CSV)** with `on_hand: below minimum 0`. Preview shows `supplier_name`, `lead_time_days` and a `needs_reorder` true/false column |
| 2b.5 | Run **`web_traffic_hourly`** | Success, about **240 rows** (48 hours × 5 pages); `hour_start` is a `DateTime`. Run it again a few hours later: new hours are added, overlapping hours are replaced (one row per hour and page) |
| 2b.6 | Run **`mrr_monthly`** | Success: **downloaded 37, loaded 36**. The export repeats one row and `drop_duplicates` removes it; `net_new_mrr` is derived. Scheduled for the 1st of each month (`0 7 1 * *`) |
| 2b.7 | Same report, other filter mode: `ddg run customer_usage --set source.filter_mode=url --force` | Success; *Apply filters* says `… via url` |
| 2b.8 | Period without a shortcut: `ddg run customer_usage --set source.filters.usage_date=past14days --set source.filter_mode=widget --force` | Success with **560 downloaded** (14 days). The widget's *Relative date range* editor was used (interval 14, unit days) |
| 2b.9 | A value the widget doesn't offer: `ddg run support_tickets --set 'source.filters.priority=[Critical]'` | `FAILED … ConfigurationError: filter 'Priority' does not offer the value 'Critical'` (not retried) |
| 2b.10 | Watch it happen (local, not Docker): `CLICKHOUSE_HOST=localhost METABASE_URL=http://localhost:3000 uv run dashdashgo run support_tickets --headed --force` | A Chromium window clears and sets both filters, then downloads |

---

## 3. Data correctness and idempotency

| # | Action | Expected |
|---|---|---|
| 3.1 | Pipeline `weekly_sales` → *Data preview* | 280 rows total. `category` values are exactly **Accessories, Audio, Displays, Home Office** (trimmed and title-cased); `product` has no leading space; `revenue` shows 2 decimals |
| 3.2 | Pipeline `q4_budget_review` → *Data preview* | Flattened columns `owner_name`, `owner_email`, `approval_status`, `approved_by`, `approved_at`, `variance_amount = actual − budget`; Marketing December rows show `null` for actual/variance and `pending` approval |
| 3.3 | Run `weekly_sales` again (**Run now**) | Status **Skipped**. A notice says the data is identical to what run `<first run id>` already ingested. *Load/Verify* show as skipped; *Loaded* = 0 |
| 3.4 | **Force reload** on the pipeline page | Success, loaded 280. The *Check for duplicates* step says "… (forced reload)" |
| 3.5 | Data preview again | Still **280 rows total**, not 560: ReplacingMergeTree + `FINAL` keeps one row per `(report_date, region, product)`, and `_run_id` now shows the forced run |
| 3.6 | Direct check in ClickHouse: `docker compose exec clickhouse sh -c 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "SELECT count() FROM analytics.sales_metrics FINAL"'` | `280` |
| 3.7 | Same, with `SHOW CREATE TABLE analytics.sales_metrics` | `ReplacingMergeTree(_ingested_at)`, `PARTITION BY toYYYYMM(report_date)`, `ORDER BY (report_date, region, product)`, `Decimal(18, 2)` revenue, `LowCardinality(String)` region/category |

---

## 4. UI tour

| # | Page | Check |
|---|---|---|
| 4.1 | Pipeline page (`APP/reports/weekly_sales`) | Five-stage flow (Source → Acquire → Transform → Validate → Load & distribute) derived from the config; tiles; **Run duration** chart (hover a bar → duration, status, time, rows); run history; data preview; **CSV** button downloads `weekly_sales.csv` |
| 4.2 | Same page → expand *Configuration* | Resolved YAML with `password: '**********'`, never the real value |
| 4.3 | Same page → expand *Destination schema* | The generated `CREATE TABLE` |
| 4.4 | **Runs** (sidebar) | All runs, newest first. Filter *Pipeline* = `weekly_sales` and *Status* = Skipped → only the skipped run |
| 4.5 | Click any run ID | Run page (see 2.3) |
| 4.6 | Sidebar **API reference** | FastAPI docs listing every endpoint |
| 4.7 | **Toggle theme** | Switches light/dark; the choice survives a reload |
| 4.8 | Open `APP/runs/does-not-exist` | A styled **Not found** page (HTTP 404), not raw JSON |
| 4.9 | Narrow the window to phone width | Sidebar collapses to a top bar; no horizontal page scroll |

---

## 5. Config editor (UI)

| # | Action | Expected |
|---|---|---|
| 5.1 | Pipeline `weekly_sales` → **Edit config** | Editor with line numbers. The Validation panel turns green: **Valid**, with a summary (source path, destination, transforms, schedule) and the table DDL |
| 5.2 | Change `max_attempts: 3` to `max_attempts: 0` | Within about a second: **Invalid · 1 problem** `retry.max_attempts - Input should be greater than or equal to 1 · line N`; a ● marks the line in the gutter. Click the problem → cursor jumps to that line |
| 5.3 | Also replace `${METABASE_PASSWORD}` with `hunter2` | A second problem: `source.credentials.password - secrets must reference an environment variable` |
| 5.4 | Press **Save** | Nothing is saved; the problems stay listed. *Unsaved changes* is shown |
| 5.5 | Fix both (`max_attempts: 4`, restore `${METABASE_PASSWORD}`), press **⌘/Ctrl+S** | Redirects to the pipeline page; the Acquire stage now says *Up to 4 attempts* |
| 5.6 | **Edit config** again | *History* panel lists one earlier version. **Load** → the old text appears (not saved yet); Save → restored |
| 5.7 | Conflict: open **Edit config** in two tabs, save a change in tab 1, then save in tab 2 | Tab 2 shows a toast: *weekly_sales.yaml changed since it was opened … reload it* and does not overwrite tab 1's change |
| 5.8 | Schema-drift warning: change `revenue` type to `Float64` (do not save) | Still valid, but a ⚠ warning: *analytics.sales_metrics already exists with a different schema (revenue: table has Decimal(18, 2), config says Float64). Runs will fail preflight…* Revert the change |
| 5.9 | Close the tab with unsaved edits | The browser asks for confirmation |
| 5.10 | **New pipeline** (sidebar) → *Start from* = *Copy of weekly_sales* → Name `sales_copy` | `name:` updates automatically, **Valid**, and a ⚠ warning *analytics.sales_metrics is also written by weekly_sales…* |
| 5.11 | In the editor change `table: sales_metrics` to `table: sales_metrics_copy` → **Create pipeline** | Redirects to `APP/reports/sales_copy`; it appears in the sidebar and on the overview |
| 5.12 | **Run now** on `sales_copy` | Success, 280 rows into the new table `analytics.sales_metrics_copy` (created on first run, see *Prepare destination table*) |
| 5.13 | **New pipeline** with the *Blank template*, name `blank_test` → **Create pipeline** → **Run now** | Created. The run **fails at quality** with `DataQualityError: … share a natural key [report_date]`: the template's single-column key is not unique for this report, which is the expected guard against silently collapsing rows. The template is a starting point to edit |
| 5.14 | `blank_test` → **Edit config** → **Archive** → confirm | Back on the overview; `blank_test` is gone from the sidebar. Its run history remains under **Runs** |
| 5.15 | Invalid config on the overview: `docker compose exec app sh -c 'echo "name: broken" > /data/reports/broken.yaml'`, reload the overview | A row **broken — Invalid config** with the error and a **Fix** button opening the editor. Clean up: `ddg config archive broken --yes` |

---

## 6. Failure handling

Each failure is recorded with its stage, a message, evidence and a **Retry** button.

| # | Action | Expected |
|---|---|---|
| 6.1 | **Wrong password** (per-run override): `ddg run q4_budget_review --set source.credentials.password=WrongPass123` | About 2 s. `✗ FAILED … Error at acquisition: AuthenticationError: Metabase rejected the credentials … Did not match stored password`. **1 attempt only** (never retried, to avoid lockout) |
| 6.2 | Open that run in the UI | Red panel *Failed at acquisition · AuthenticationError*; **Failure screenshots** thumbnail of the Metabase login page with *"Did not match stored password"*; *Log in* step ✗; later steps "skipped" |
| 6.2a | Click the screenshot thumbnail | It opens **in the page** in a viewer (you stay on the run page): file name at the top, **← →** (or arrow keys) to step through attempts, **Open original**, **✕ Close**. Esc or a click on the dark backdrop also closes it |
| 6.3 | Its logs (UI or `ddg logs <run_id>`) | `Config overrides for this run: source.credentials.password=********`: the value is masked |
| 6.4 | Press **Retry** on that run | A new run with *Retry of `<id>`*, using the normal config → Success or Skipped |
| 6.5 | **Report not found**: `ddg run weekly_sales --set "source.location.dashboard=Sales Report (old)"` | `ReportNotFoundError: dashboard 'Sales Report (old)' not found in 'Sales'; it contains: Sales Report, Weekly Sales`, with 1 attempt and a screenshot of the collection page |
| 6.6 | **Mistyped filter**: `ddg run customer_usage --set source.filters.usage_dat=past7days` | `ConfigurationError: 'Customer Usage' has no filter(s) ['usage_dat']; check source.filters`. The run fails instead of silently downloading unfiltered data |
| 6.7 | **Dashboard down**: `docker compose stop metabase`, then `ddg run weekly_sales` | About 17 s: attempt 1 fails, waits 5 s, attempt 2 fails, waits 10 s, attempt 3 fails → `NavigationError … net::ERR_NAME_NOT_RESOLVED`. The run page shows *Acquire report (attempt 3 of 3)*, three screenshots and three HTML snapshots. Because the browser never rendered a page, each screenshot is a **"The page did not load"** card (report, step and attempt, dashboard URL, browser URL, error, time) instead of a blank white image |
| 6.8 | `docker compose start metabase` and wait about 1 minute until healthy | The next run succeeds again |
| 6.9 | **Warehouse down**: `docker compose stop clickhouse`, then `curl -i APP/api/health` | **503** `{"status":"degraded",…,"clickhouse":"down"}`; the UI shows *Metadata store unavailable*; `ddg run weekly_sales` fails immediately with `cannot connect to ClickHouse at clickhouse:8123` |
| 6.10 | `docker compose start clickhouse` | Within seconds `/api/health` is `ok` again and the UI works; no app restart is needed |
| 6.11 | **Data-quality failure**: `ddg run weekly_sales --set 'ingestion.quality.rules=[{column: revenue, min: 1000}]'` | `FAILED at quality - DataQualityError: 54.6% of rows invalid exceeds max_invalid_ratio 2.0%: 153/280 rows rejected (revenue: below minimum 1000 x153)`. Nothing is loaded |
| 6.12 | **Quarantine instead**: the same command plus `--set ingestion.quality.max_invalid_ratio=1` | `SUCCESS … rejected=153 inserted=127`. The run page's *Validate data* step links **Rejected rows (CSV)** (every rejected row with its reason). Then run `ddg run weekly_sales --force` to reload the full, clean week |
| 6.13 | **Interrupted by a restart**: click **Run now** on `customer_usage`, then within 2 s run `docker compose restart app` | After the app is healthy again, that run shows **Failed** with *Interrupted: the server restarted before this run completed*, not stuck as Running |
| 6.14 | **Concurrent run**: `curl -XPOST APP/api/reports/weekly_sales/runs` twice in quick succession | Second call → **409** `a run of 'weekly_sales' is already queued or running` |

---

## 7. Scheduling

| # | Action | Expected |
|---|---|---|
| 7.1 | Overview → *Schedule* column | `weekly_sales` **0 8 * * 1** (Monday 08:00 Asia/Kolkata), `customer_usage` **30 6 * * ***, each with "next in …"; `q4_budget_review` *On demand* |
| 7.2 | `curl -s APP/api/reports \| python3 -m json.tool \| grep next_run` | ISO timestamps with `+05:30`; weekly_sales' next run is a **Monday** |
| 7.3 | Live cron: pipeline `sales_copy` → **Edit config** → set `schedule: {enabled: true, cron: "* * * * *", timezone: UTC}` → Save | Pipeline page shows *next in <1m* |
| 7.4 | Wait until the next full minute | A new run with trigger **schedule** appears (Runs page / `ddg runs --limit 3`), usually **Skipped** since the data is unchanged |
| 7.5 | Set `enabled: false` again (or archive `sales_copy`) | No more scheduled runs |

---

## 8. Command line

| # | Command | Expected |
|---|---|---|
| 8.1 | `ddg --help` | Command list (run, runs, show, logs, retry, data, stats, list, validate, schema, config, serve, init-db, prune) plus examples |
| 8.2 | `ddg list` | Table of reports with state, format, destination, schedule |
| 8.3 | `ddg validate` | `✓` line per report; exit code 0 (`echo $?`) |
| 8.4 | `ddg schema customer_usage` | `CREATE TABLE … daily_usage … ORDER BY (usage_date, account_id)` |
| 8.5 | `ddg run customer_usage --set source.filters.usage_date=past30days` | `downloaded=1200 … inserted=300`; exit code 0 |
| 8.6 | `ddg runs --status failed` | Only failed runs (from section 6) with error summaries |
| 8.7 | `ddg show <run_id>` | Header, rows, full timeline with per-attempt browser steps, artifacts list |
| 8.8 | `ddg logs <run_id> --level WARNING` | Only warnings/errors. With `--follow` on a running run it streams until the run ends |
| 8.9 | `ddg retry <failed_run_id>` | Runs the report again, linked to the failed run |
| 8.10 | `ddg data weekly_sales --limit 3` | Aligned table, then `3 of 280 rows from analytics.sales_metrics (FINAL)`. With `--format csv` → CSV; with `--format json` → JSON |
| 8.11 | `ddg stats` | 7-day summary + per-report table (runs, loaded, skipped, failed, rows, avg duration, last run) |
| 8.12 | `ddg run weekly_sales --json` | The run record as JSON (for scripts) |
| 8.13 | `ddg run does_not_exist; echo $?` | `error: no report named 'does_not_exist'; available: …` and exit code **2** |
| 8.14 | `ddg config show weekly_sales` | Raw YAML with `${METABASE_PASSWORD}` (the reference, not the secret) |
| 8.15 | `ddg config new monthly_sales --from weekly_sales` → `ddg config history monthly_sales` → `ddg config archive monthly_sales -y` | Created → "No earlier versions." → Archived to `reports/.archive/…` |
| 8.16 | Plaintext secret via import: `ddg config show weekly_sales \| sed 's/\${METABASE_PASSWORD}/oops/;s/^name: .*/name: leak_test/' \| docker compose exec -T app dashdashgo config import - --name leak_test; echo $?` | Rejected: `source.credentials.password: secrets must reference an environment variable`; exit code 2; nothing created |
| 8.17 | Headed browser (local, not Docker): `make setup`, then `CLICKHOUSE_HOST=localhost METABASE_URL=http://localhost:3000 uv run dashdashgo run weekly_sales --headed --force` | A Chromium window opens and you watch it log in, open the dashboard and download |

---

## 9. API

| # | Request | Expected |
|---|---|---|
| 9.1 | `curl -s APP/api/stats?days=7` | JSON counts + `success_rate` |
| 9.2 | `curl -s APP/api/reports/weekly_sales` | Masked config (`"password":"**********"`), schedule with `next_run`, stats, `ddl` |
| 9.3 | `curl -s "APP/api/reports/weekly_sales/data?limit=2&since=2026-01-01"` | `{"table":"analytics.sales_metrics","total":…,"rows":[…]}`; `revenue` values are strings such as `"431.20"` (exact decimals) |
| 9.4 | `curl -s "APP/api/reports/weekly_sales/data?format=csv&limit=5"` | CSV download |
| 9.5 | `curl -s -XPOST APP/api/reports/q4_budget_review/runs` | **202** with the QUEUED run record; `GET /api/runs/<run_id>` a few seconds later shows its timeline |
| 9.6 | `curl -s APP/api/runs/<run_id>/logs` | JSON log lines |
| 9.7 | `curl -s -XPOST APP/api/runs/<finished_run_id>/retry` | **202**, `trigger: "retry"`, `parent_run_id` set |
| 9.8 | `curl -s APP/api/reports/weekly_sales/config` | Raw YAML + `version` |
| 9.9 | `curl -s -XPOST APP/api/reports/weekly_sales/config/validate -H 'content-type: application/json' -d '{"yaml":"name: weekly_sales\n"}'` | `{"valid":false,…,"problems":[{"location":"source","message":"Field required"},…]}` |
| 9.10 | Error codes: `curl -o /dev/null -w '%{http_code}\n'` on `APP/api/reports/nope`, `APP/api/runs/nope`, `--path-as-is APP/api/artifacts/../../etc/passwd`, `APP/api/reports/weekly_sales/data?limit=0` | **404, 404, 404, 422** |
| 9.11 | Save with a stale version: `PUT /api/reports/weekly_sales/config` with `{"yaml": <valid yaml>, "base_version": "old"}` | **409** *changed since it was opened* |
| 9.12 | `curl -sI APP/api/artifacts/failures/<report>/<date>/<run_id>/attempt1_login_failure.html` | `content-type: text/plain` and `x-content-type-options: nosniff`: captured pages are never rendered as HTML |

---

## 10. Storage, logs and security

| # | Action | Expected |
|---|---|---|
| 10.1 | `docker compose exec app find /data/storage -maxdepth 2 -type d` | `raw`, `processed`, `failures`, `screenshots`, `logs`, each partitioned `<report>/<date>/<run_id>/` |
| 10.2 | `docker compose exec app sh -c 'grep -rl "$METABASE_PASSWORD" /data/storage \| wc -l'` | **0**: the password is in no log, snapshot or artifact |
| 10.3 | `docker compose exec app cat /data/storage/logs/weekly_sales/<date>/<run_id>/run.log \| head -3` | JSON lines with `ts, level, logger, run_id, report, stage, message` |
| 10.4 | `docker compose logs app \| grep "\[run="` | Console lines tagged `[run=…] [report=…] [stage=…]` |
| 10.5 | `git grep -n "$METABASE_PASSWORD"` (in the repo, after `set -a; . ./.env`) | No matches; `.env` is git-ignored |
| 10.6 | `docker compose exec app id` | `uid=10001(dashdashgo)`: not root |
| 10.7 | `ddg prune --days 0` | `Retention disabled (0 days); nothing pruned` (the daily job uses `STORAGE_RETENTION_DAYS`) |
| 10.7a | Run something from a **host** terminal (step 8.17 or 2b.10), then open that run in the UI | *Result → Executed on* shows your machine and its storage path. Because the logs were written there, the Logs panel explains that instead of being empty. Runs started in the stack show the container's `/data/storage` |
| 10.8 | `make export-reports` | Configs edited in the UI (for example `sales_copy.yaml`) appear in `./reports/` |

---

## 11. Automated tests

| # | Command | Expected |
|---|---|---|
| 11.1 | `make test` | `179 passed, 26 deselected` in about 3 s (unit tests, no infrastructure) |
| 11.2 | `make test-integration` (stack running) | `8 passed` |
| 11.3 | `make test-e2e` | `18 passed` in about 60 s (real Metabase + Chromium + ClickHouse, isolated database) |
| 11.4 | `make test-all` | `205 passed` inside the app container |
| 11.5 | `make lint` | `All checks passed!`, `… files already formatted`, `Success: no issues found` |
| 11.6 | GitHub → **Actions** tab after a push | Workflow **CI** with jobs *Lint, types, unit tests*, *ClickHouse integration tests*, *End-to-end (Docker Compose)*, all green |

---

## 12. Clean up

```bash
ddg config archive sales_copy -y     # if still present
make down                            # stop, keep data
make clean                           # stop and delete all data/volumes
```
