"""Command line interface: ``dashdashgo <command>`` / ``python -m dashdashgo <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta

from dashdashgo.errors import DashDashGoError
from dashdashgo.observability.logging import configure_logging
from dashdashgo.settings import get_settings

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _cmd_validate(args: argparse.Namespace) -> int:
    from dashdashgo.config.loader import ReportRegistry

    registry = ReportRegistry(get_settings().reports_dir)
    names = args.reports or registry.names()
    status = EXIT_OK
    for name in names:
        try:
            config = registry.load(name)
        except DashDashGoError as exc:
            print(f"✗ {exc.message}")
            status = EXIT_FAILED
            continue
        schedule = (
            f"cron '{config.schedule.cron}' {config.schedule.timezone}"
            if config.schedule.enabled
            else "on demand"
        )
        print(
            f"✓ {name}: {config.source.export.format.value.upper()} -> "
            f"{config.destination.qualified_table} ({schedule})"
        )
    return status


def _cmd_list(_: argparse.Namespace) -> int:
    from dashdashgo.config.loader import ReportRegistry

    valid, invalid = ReportRegistry(get_settings().reports_dir).load_all()
    for name, config in valid.items():
        state = "enabled " if config.enabled else "disabled"
        cron = config.schedule.cron if config.schedule.enabled else "-"
        print(
            f"{name:<24} {state}  {config.source.export.format.value:<5} "
            f"{config.destination.qualified_table:<28} {cron}"
        )
    for name, error in invalid.items():
        print(f"{name:<24} INVALID   {error.message.splitlines()[0]}")
    return EXIT_OK if not invalid else EXIT_FAILED


def _cmd_schema(args: argparse.Namespace) -> int:
    from dashdashgo.config.loader import ReportRegistry
    from dashdashgo.warehouse.ddl import create_table_sql

    print(
        create_table_sql(ReportRegistry(get_settings().reports_dir).load(args.report).destination)
        + ";"
    )
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    from dashdashgo.container import build_container

    container = build_container(get_settings())
    container.runs.ensure_schema()
    result = container.run_service.run_now(args.report, force=args.force)
    run = result.run
    if args.json:
        print(json.dumps(run.model_dump(mode="json"), indent=2))
    else:
        print(
            f"\n{run.status.value}  run={run.run_id}  report={run.report}  "
            f"duration={run.duration_seconds or 0:.1f}s  downloaded={run.records_downloaded}  "
            f"rejected={run.records_rejected}  inserted={run.records_inserted}"
        )
        if run.duplicate_of:
            print(
                f"Identical data was already loaded by run {run.duplicate_of} "
                "(use --force to reload)."
            )
        if run.error_message:
            print(f"Error at {run.error_stage}: {run.error_type}: {run.error_message}")
    container.run_service.shutdown()
    return EXIT_OK if result.ok else EXIT_FAILED


def _cmd_init_db(_: argparse.Namespace) -> int:
    from dashdashgo.container import build_container

    container = build_container(get_settings())
    container.runs.ensure_schema()
    print(f"Metadata tables ready in database '{container.settings.clickhouse_metadata_database}'")
    return EXIT_OK


def _cmd_prune(args: argparse.Namespace) -> int:
    from dashdashgo.container import build_container
    from dashdashgo.metadata.models import utcnow

    settings = get_settings()
    days = args.days if args.days is not None else settings.storage_retention_days
    if days <= 0:
        print("Retention disabled (0 days); nothing pruned")
        return EXIT_OK
    removed = build_container(settings).storage.prune((utcnow() - timedelta(days=days)).date())
    print(f"Removed {removed} artifact(s) older than {days} days")
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from dashdashgo.distribution.app import create_app

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        log_config=None,  # keep our structured logging
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashdashgo", description="Configuration-driven dashboard report ETL."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one report pipeline now")
    run.add_argument("report", help="report name (file name in reports/ without .yaml)")
    run.add_argument(
        "--force", action="store_true", help="load even if identical data was already loaded"
    )
    run.add_argument("--json", action="store_true", help="print the run record as JSON")
    run.set_defaults(func=_cmd_run)

    validate = sub.add_parser("validate", help="validate report configs without running them")
    validate.add_argument("reports", nargs="*", help="report names (default: all)")
    validate.set_defaults(func=_cmd_validate)

    sub.add_parser("list", help="list configured reports").set_defaults(func=_cmd_list)

    schema = sub.add_parser("schema", help="print the ClickHouse DDL for a report's table")
    schema.add_argument("report")
    schema.set_defaults(func=_cmd_schema)

    sub.add_parser("init-db", help="create the pipeline metadata tables").set_defaults(
        func=_cmd_init_db
    )

    prune = sub.add_parser("prune", help="delete stored artifacts older than the retention period")
    prune.add_argument("--days", type=int, help="override STORAGE_RETENTION_DAYS")
    prune.set_defaults(func=_cmd_prune)

    serve = sub.add_parser("serve", help="start the API, UI and scheduler")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        code: int = args.func(args)
    except DashDashGoError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_USAGE if exc.stage == "config" else EXIT_FAILED
    return code


if __name__ == "__main__":
    sys.exit(main())
