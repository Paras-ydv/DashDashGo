"""Command line interface: ``dashdashgo <command>`` / ``python -m dashdashgo <command>``.

Everything the web UI can do is available here, so pipelines can be operated
entirely from a terminal, a cron job or CI:

    run, runs, show, logs, retry, data, stats      execute and inspect runs
    list, validate, schema, config ...             manage report configs
    serve, init-db, prune                          operate the service
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from dashdashgo import __version__
from dashdashgo.cli import config_cmds, runs
from dashdashgo.errors import DashDashGoError
from dashdashgo.observability.logging import configure_logging
from dashdashgo.settings import get_settings

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

EPILOG = """\
examples:
  dashdashgo run weekly_sales
  dashdashgo run customer_usage --set source.filters.usage_date=past30days --force
  dashdashgo run weekly_sales --headed --no-retry        # watch the browser, fail fast
  dashdashgo run --config ./my_report.yaml               # a config outside reports/
  dashdashgo runs --status failed
  dashdashgo show <run_id>   |   dashdashgo logs <run_id> --follow
  dashdashgo data weekly_sales --since 2026-09-14 --format csv > sales.csv
  dashdashgo config new monthly_sales --from weekly_sales --edit
"""


def _cmd_init_db(_: argparse.Namespace) -> int:
    from dashdashgo.container import build_container

    container = build_container(get_settings())
    container.runs.ensure_schema()
    print(f"Metadata tables ready in database '{container.settings.clickhouse_metadata_database}'")
    return EXIT_OK


def _cmd_prune(args: argparse.Namespace) -> int:
    from dashdashgo.metadata.models import utcnow
    from dashdashgo.storage import LocalStorage

    settings = get_settings()
    days = args.days if args.days is not None else settings.storage_retention_days
    if days <= 0:
        print("Retention disabled (0 days); nothing pruned")
        return EXIT_OK
    removed = LocalStorage(settings.storage_root).prune((utcnow() - timedelta(days=days)).date())
    print(f"Removed {removed} artifact(s) older than {days} days")
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from dashdashgo.distribution.app import create_app

    settings = get_settings()
    uvicorn.run(
        create_app(settings, scheduler=False if args.no_scheduler else None),
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        log_config=None,  # keep our structured logging
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashdashgo",
        description="Configuration-driven dashboard report ETL.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"dashdashgo {__version__}")
    parser.add_argument("--log-level", help="override LOG_LEVEL (DEBUG, INFO, WARNING, ERROR)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # --- runs ---------------------------------------------------------------------
    run = sub.add_parser("run", help="run a pipeline now and wait for the result")
    run.add_argument("report", nargs="?", help="report name (reports/<name>.yaml)")
    run.add_argument("--config", metavar="FILE", help="run a config file instead of a named report")
    run.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="override a config value for this run only (repeatable), "
        "e.g. --set browser.timeout_ms=60000",
    )
    run.add_argument("--headed", action="store_true", help="show the browser window")
    run.add_argument("--no-retry", action="store_true", help="a single attempt, no retries")
    run.add_argument("--timeout", type=int, metavar="SECONDS", help="browser step timeout")
    run.add_argument(
        "--force", action="store_true", help="load even if identical data was already loaded"
    )
    run.add_argument("--json", action="store_true", help="print the run record as JSON")
    run.set_defaults(func=runs.cmd_run)

    runs_p = sub.add_parser("runs", help="list recent runs")
    runs_p.add_argument("--report")
    runs_p.add_argument("--status", choices=["queued", "running", "success", "skipped", "failed"])
    runs_p.add_argument("--limit", type=int, default=20)
    runs_p.add_argument("--json", action="store_true")
    runs_p.set_defaults(func=runs.cmd_runs)

    show = sub.add_parser("show", help="show a run: status, timeline, artifacts")
    show.add_argument("run_id")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=runs.cmd_show)

    logs = sub.add_parser("logs", help="print a run's structured log")
    logs.add_argument("run_id")
    logs.add_argument(
        "--level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], type=str.upper
    )
    logs.add_argument(
        "--follow", "-f", action="store_true", help="keep printing until the run finishes"
    )
    logs.set_defaults(func=runs.cmd_logs)

    retry = sub.add_parser("retry", help="re-run the report of a finished run")
    retry.add_argument("run_id")
    retry.add_argument("--force", action="store_true")
    retry.set_defaults(func=runs.cmd_retry)

    data = sub.add_parser("data", help="query a report's loaded data")
    data.add_argument("report")
    data.add_argument("--limit", type=int, default=20)
    data.add_argument("--offset", type=int, default=0)
    data.add_argument("--since", metavar="YYYY-MM-DD")
    data.add_argument("--until", metavar="YYYY-MM-DD")
    data.add_argument("--format", choices=["table", "csv", "json"], default="table")
    data.add_argument("--lineage", action="store_true", help="include _run_id/_ingested_at")
    data.set_defaults(func=runs.cmd_data)

    stats = sub.add_parser("stats", help="run statistics overall and per report")
    stats.add_argument("--days", type=int, default=7)
    stats.set_defaults(func=runs.cmd_stats)

    # --- configs ------------------------------------------------------------------
    sub.add_parser("list", help="list configured reports").set_defaults(func=config_cmds.cmd_list)

    validate = sub.add_parser("validate", help="validate report configs without running them")
    validate.add_argument("reports", nargs="*", help="report names (default: all)")
    validate.add_argument("--file", action="append", metavar="FILE", help="validate a YAML file")
    validate.set_defaults(func=config_cmds.cmd_validate)

    schema = sub.add_parser("schema", help="print the ClickHouse DDL for a report's table")
    schema.add_argument("report")
    schema.set_defaults(func=config_cmds.cmd_schema)

    config = sub.add_parser("config", help="create, edit and version report configs")
    config_sub = config.add_subparsers(dest="config_command", required=True, metavar="<action>")
    c_show = config_sub.add_parser("show", help="print a report's YAML")
    c_show.add_argument("report")
    c_show.set_defaults(func=config_cmds.cmd_config_show)
    c_edit = config_sub.add_parser("edit", help="edit in $EDITOR; validated before saving")
    c_edit.add_argument("report")
    c_edit.set_defaults(func=config_cmds.cmd_config_edit)
    c_new = config_sub.add_parser("new", help="create a report from a template or a copy")
    c_new.add_argument("name")
    c_new.add_argument("--from", dest="source", metavar="REPORT", help="copy an existing report")
    c_new.add_argument("--edit", action="store_true", help="open $EDITOR before creating")
    c_new.set_defaults(func=config_cmds.cmd_config_new)
    c_import = config_sub.add_parser(
        "import", help="create/update a report from a file or stdin (-)"
    )
    c_import.add_argument("file")
    c_import.add_argument("--name", help="report name (default: file name)")
    c_import.set_defaults(func=config_cmds.cmd_config_import)
    c_history = config_sub.add_parser("history", help="list saved versions")
    c_history.add_argument("report")
    c_history.set_defaults(func=config_cmds.cmd_config_history)
    c_restore = config_sub.add_parser("restore", help="restore a saved version")
    c_restore.add_argument("report")
    c_restore.add_argument("version")
    c_restore.set_defaults(func=config_cmds.cmd_config_restore)
    c_archive = config_sub.add_parser("archive", help="stop and hide a report (file is kept)")
    c_archive.add_argument("report")
    c_archive.add_argument("--yes", "-y", action="store_true")
    c_archive.set_defaults(func=config_cmds.cmd_config_archive)

    # --- service ------------------------------------------------------------------
    serve = sub.add_parser("serve", help="start the API, UI and scheduler")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-scheduler", action="store_true", help="UI/API only, no cron jobs")
    serve.set_defaults(func=_cmd_serve)

    sub.add_parser("init-db", help="create the pipeline metadata tables").set_defaults(
        func=_cmd_init_db
    )

    prune = sub.add_parser("prune", help="delete stored artifacts older than the retention period")
    prune.add_argument("--days", type=int, help="override STORAGE_RETENTION_DAYS")
    prune.set_defaults(func=_cmd_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(args.log_level or settings.log_level, settings.log_format)
    try:
        code: int = args.func(args)
    except DashDashGoError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_USAGE if exc.stage == "config" else EXIT_FAILED
    except KeyboardInterrupt:
        return 130
    return code


__all__ = ["build_parser", "main"]
