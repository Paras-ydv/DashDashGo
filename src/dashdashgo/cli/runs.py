"""Commands that execute or inspect runs: run, runs, show, logs, retry, data, stats."""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

from dashdashgo.cli.output import paint, print_json, status_label, step_symbol, table
from dashdashgo.config.loader import load_report_config
from dashdashgo.container import Container, build_container
from dashdashgo.distribution.views import LogLine, build_timeline, list_artifacts, read_run_log
from dashdashgo.errors import ConfigurationError, ReportNotConfiguredError
from dashdashgo.metadata.models import RunRecord, RunStatus, Trigger
from dashdashgo.settings import get_settings
from dashdashgo.utils.formatting import fmt_ago, fmt_bytes, fmt_duration, fmt_time

EXIT_OK, EXIT_FAILED = 0, 1
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _container() -> Container:
    container = build_container(get_settings())
    container.runs.ensure_schema()
    return container


def _get_run(container: Container, run_id: str) -> RunRecord:
    run = container.runs.get_run(run_id)
    if run is None:
        raise ReportNotConfiguredError(f"no run with id {run_id!r}")
    return run


def _run_overrides(args: argparse.Namespace) -> list[str]:
    overrides = list(args.set or [])
    if args.headed:
        overrides.append("browser.headless=false")
    if args.no_retry:
        overrides.append("retry.max_attempts=1")
    if args.timeout:
        overrides.append(f"browser.timeout_ms={args.timeout * 1000}")
    return overrides


def print_run_summary(run: RunRecord) -> None:
    print(
        f"\n{status_label(run.status)}  run={run.run_id}  report={run.report}  "
        f"duration={fmt_duration(run.duration_ms)}  downloaded={run.records_downloaded}  "
        f"rejected={run.records_rejected}  inserted={run.records_inserted}"
    )
    if run.duplicate_of:
        print(
            f"Identical data was already loaded by run {run.duplicate_of} (use --force to reload)."
        )
    if run.error_message:
        print(f"Error at {run.error_stage}: {run.error_type}: {run.error_message}")
    print(f"Details: dashdashgo show {run.run_id}")


def cmd_run(args: argparse.Namespace) -> int:
    overrides = _run_overrides(args)
    container = _container()
    try:
        if args.config:
            config = load_report_config(Path(args.config), overrides=overrides)
            if args.report and args.report != config.name:
                raise ConfigurationError(
                    f"--config file defines {config.name!r}, not {args.report!r}"
                )
            result = container.run_service.run_now(config.name, config=config, force=args.force)
        elif args.report:
            result = container.run_service.run_now(
                args.report, force=args.force, overrides=overrides
            )
        else:
            raise ConfigurationError("give a report name or --config FILE")
    finally:
        container.run_service.shutdown()
    if args.json:
        print_json(result.run.model_dump(mode="json"))
    else:
        print_run_summary(result.run)
    return EXIT_OK if result.ok else EXIT_FAILED


def cmd_runs(args: argparse.Namespace) -> int:
    container = _container()
    status = RunStatus(args.status.upper()) if args.status else None
    runs = container.runs.list_runs(report=args.report, status=status, limit=args.limit)
    if args.json:
        print_json([r.model_dump(mode="json") for r in runs])
        return EXIT_OK
    if not runs:
        print("No runs found.")
        return EXIT_OK
    rows = [
        [
            status_label(r.status),
            r.run_id,
            r.report,
            r.trigger.value,
            fmt_ago(r.started_at),
            fmt_duration(r.duration_ms),
            r.records_downloaded,
            r.records_rejected,
            r.records_inserted,
            (
                f"{r.error_type}: {r.error_message}"[:60]
                if r.error_message
                else r.current_stage
                if not r.status.is_terminal
                else ""
            ),
        ]
        for r in runs
    ]
    print(
        table(
            [
                "STATUS",
                "RUN",
                "REPORT",
                "TRIGGER",
                "STARTED",
                "DURATION",
                "IN",
                "REJECTED",
                "LOADED",
                "NOTE",
            ],
            rows,
        )
    )
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    container = _container()
    run = _get_run(container, args.run_id)
    stages = container.runs.stages(run.run_id)
    timeline = build_timeline(run, stages)
    artifacts = list_artifacts(container.storage, run)
    if args.json:
        print_json(
            {
                "run": run.model_dump(mode="json"),
                "timeline": [asdict(s) for s in timeline],
                "artifacts": {a: [asdict(x) for x in items] for a, items in artifacts.items()},
            }
        )
        return EXIT_OK

    print(f"{paint(run.run_id, '1')}  {status_label(run.status)}")
    print(
        f"report={run.report}  trigger={run.trigger.value}  "
        f"started={fmt_time(run.started_at)}  duration={fmt_duration(run.duration_ms)}"
    )
    if run.parent_run_id:
        print(f"retry of {run.parent_run_id}")
    print(
        f"rows: downloaded={run.records_downloaded} rejected={run.records_rejected} "
        f"loaded={run.records_inserted} -> {run.destination_table or '-'}"
    )
    if run.error_message:
        print(paint(f"\nFailed at {run.error_stage}: {run.error_type}: {run.error_message}", "31"))
    print("\nTimeline")
    for step in timeline:
        duration = fmt_duration(step.duration_ms) if step.duration_ms is not None else ""
        print(f"  {step_symbol(step.state)} {step.label:<32} {duration:>8}  {step.message}")
        for attempt in step.attempts:
            if len(step.attempts) > 1:
                print(f"      attempt {attempt.number}: {attempt.state}")
            for sub in attempt.steps:
                sub_duration = fmt_duration(sub.duration_ms) if sub.duration_ms is not None else ""
                symbol = step_symbol(sub.state)
                print(f"      {symbol} {sub.label:<26} {sub_duration:>8}  {sub.message}")
    if artifacts:
        print("\nArtifacts")
        for area, items in artifacts.items():
            for item in items:
                print(f"  {area:<12} {item.key}  ({fmt_bytes(item.size)})")
    return EXIT_OK


def _print_log_lines(lines: list[LogLine], minimum: int, start: int = 0) -> int:
    for line in lines[start:]:
        if _LEVELS.get(line.level, 20) < minimum:
            continue
        level = paint(f"{line.level:<7}", {"ERROR": "31", "WARNING": "33"}.get(line.level, "36"))
        stage = f"[{line.stage}] " if line.stage else ""
        print(f"{line.ts[11:23]} {level} {stage}{line.message}")
        if line.exception:
            print(paint(line.exception, "31"))
    return len(lines)


def cmd_logs(args: argparse.Namespace) -> int:
    container = _container()
    run = _get_run(container, args.run_id)
    minimum = _LEVELS[args.level.upper()]
    seen = _print_log_lines(read_run_log(container.storage, run), minimum)
    while args.follow and not run.status.is_terminal:
        time.sleep(1)
        run = _get_run(container, args.run_id)
        seen = _print_log_lines(read_run_log(container.storage, run), minimum, seen)
    return EXIT_OK


def cmd_retry(args: argparse.Namespace) -> int:
    container = _container()
    previous = _get_run(container, args.run_id)
    if not previous.status.is_terminal:
        print(f"run {previous.run_id} is still {previous.status.value.lower()}", file=sys.stderr)
        return EXIT_FAILED
    try:
        result = container.run_service.run_now(
            previous.report, trigger=Trigger.CLI, parent_run_id=previous.run_id, force=args.force
        )
    finally:
        container.run_service.shutdown()
    print_run_summary(result.run)
    return EXIT_OK if result.ok else EXIT_FAILED


def cmd_data(args: argparse.Namespace) -> int:
    container = _container()
    config = container.registry.load(args.report)
    page = container.data_reader.fetch(
        config.destination,
        limit=args.limit,
        offset=args.offset,
        since=date.fromisoformat(args.since) if args.since else None,
        until=date.fromisoformat(args.until) if args.until else None,
    )
    if args.format == "json":
        print_json(
            {"table": config.destination.qualified_table, "total": page.total, "rows": page.rows}
        )
    elif args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=page.columns)
        writer.writeheader()
        writer.writerows(page.rows)
    else:
        columns = [c for c in page.columns if args.lineage or not c.startswith("_")]
        print(table(columns, [[row[c] for c in columns] for row in page.rows]))
        source = config.destination.qualified_table
        print(f"\n{len(page.rows)} of {page.total} rows from {source} (FINAL)")
    return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    container = _container()
    overview = container.runs.overview(args.days)
    rate = f"{overview.success_rate:.0%}" if overview.success_rate is not None else "-"
    print(
        f"Last {args.days} days: {overview.total_runs} runs · success rate {rate} · "
        f"{overview.failed_runs} failed · {overview.rows_inserted:,} rows loaded · "
        f"median {fmt_duration(overview.median_duration_ms)}\n"
    )
    rows = []
    for name in container.registry.names():
        stats = container.runs.report_stats(name)
        last = stats.last_run
        rows.append(
            [
                name,
                stats.total_runs,
                stats.successful_runs,
                stats.skipped_runs,
                stats.failed_runs,
                stats.rows_inserted,
                fmt_duration(stats.avg_duration_ms),
                status_label(last.status) + f" {fmt_ago(last.started_at)}" if last else "never",
            ]
        )
    print(table(["REPORT", "RUNS", "LOADED", "SKIPPED", "FAILED", "ROWS", "AVG", "LAST RUN"], rows))
    return EXIT_OK
