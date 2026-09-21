"""Config commands: validate, list, schema and `config ...` (the CLI side of the UI editor)."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from dashdashgo.cli.output import paint, table
from dashdashgo.config.loader import ReportRegistry, load_report_config
from dashdashgo.config.store import ConfigStore
from dashdashgo.errors import ConfigurationError
from dashdashgo.settings import get_settings
from dashdashgo.utils.formatting import fmt_bytes, fmt_time
from dashdashgo.warehouse.ddl import create_table_sql

EXIT_OK, EXIT_FAILED = 0, 1


def _registry() -> ReportRegistry:
    return ReportRegistry(get_settings().reports_dir)


def _store() -> ConfigStore:
    return ConfigStore(_registry())


def _print_problems(exc: ConfigurationError) -> None:
    print(paint(f"✗ {exc.message.splitlines()[0]}", "31"), file=sys.stderr)
    for location, message in exc.problems:
        print(f"    {location}: {message}", file=sys.stderr)


def cmd_validate(args: argparse.Namespace) -> int:
    status = EXIT_OK
    targets: list[tuple[str, Path | None]] = [(f, Path(f)) for f in args.file or []]
    registry = _registry()
    if not targets:
        targets = [(n, None) for n in (args.reports or registry.names())]
    for label, path in targets:
        try:
            config = load_report_config(path) if path else registry.load(label)
        except ConfigurationError as exc:
            _print_problems(exc)
            status = EXIT_FAILED
            continue
        schedule = (
            f"cron '{config.schedule.cron}' {config.schedule.timezone}"
            if config.schedule.enabled
            else "on demand"
        )
        print(
            f"{paint('✓', '32')} {config.name}: {config.source.export.format.value.upper()} -> "
            f"{config.destination.qualified_table} ({schedule})"
        )
    return status


def cmd_list(_: argparse.Namespace) -> int:
    valid, invalid = _registry().load_all()
    rows = [
        [
            name,
            "enabled" if cfg.enabled else "disabled",
            cfg.source.export.format.value,
            cfg.destination.qualified_table,
            f"{cfg.schedule.cron} ({cfg.schedule.timezone})"
            if cfg.schedule.enabled
            else "on demand",
        ]
        for name, cfg in valid.items()
    ]
    rows += [
        [name, paint("INVALID", "31"), "", err.message.splitlines()[0][:60], ""]
        for name, err in invalid.items()
    ]
    print(table(["REPORT", "STATE", "FORMAT", "DESTINATION", "SCHEDULE"], rows))
    return EXIT_OK if not invalid else EXIT_FAILED


def cmd_schema(args: argparse.Namespace) -> int:
    print(create_table_sql(_registry().load(args.report).destination) + ";")
    return EXIT_OK


# --- config management ---------------------------------------------------------------


def cmd_config_show(args: argparse.Namespace) -> int:
    sys.stdout.write(_store().read(args.report).text)
    return EXIT_OK


def _edit_loop(initial: str, validate: Callable[[str], object], suffix: str) -> str | None:
    """Open $EDITOR until the text validates (or the user gives up). Returns the text."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    text = initial
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / suffix
        while True:
            path.write_text(text, encoding="utf-8")
            subprocess.run([*shlex.split(editor), str(path)], check=False)
            text = path.read_text(encoding="utf-8")
            try:
                validate(text)
                return text
            except ConfigurationError as exc:
                _print_problems(exc)
                if not sys.stdin.isatty() or input("Edit again? [Y/n] ").strip().lower() == "n":
                    return None


def cmd_config_edit(args: argparse.Namespace) -> int:
    store = _store()
    document = store.read(args.report)
    text = _edit_loop(document.text, lambda t: store.check(args.report, t), f"{args.report}.yaml")
    if text is None:
        print("No changes saved.")
        return EXIT_FAILED
    if text == document.text:
        print("No changes.")
        return EXIT_OK
    saved = store.save(args.report, text, document.version)
    print(f"Saved {args.report} (version {saved.version}); previous version kept in history.")
    return EXIT_OK


def cmd_config_new(args: argparse.Namespace) -> int:
    store = _store()
    text = store.template(args.name, args.source)
    if args.edit:
        edited = _edit_loop(text, lambda t: store.check(args.name, t), f"{args.name}.yaml")
        if edited is None:
            print("Not created.")
            return EXIT_FAILED
        text = edited
    store.create(args.name, text)
    print(f"Created reports/{args.name}.yaml")
    print(f"Next: dashdashgo config edit {args.name}   then   dashdashgo run {args.name}")
    return EXIT_OK


def cmd_config_import(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    name = args.name or (Path(args.file).stem if args.file != "-" else None)
    if not name:
        raise ConfigurationError("--name is required when importing from stdin")
    store = _store()
    if name in _registry().names():
        document = store.read(name)
        store.save(name, text, document.version)
        print(f"Updated {name} (previous version kept in history).")
    else:
        store.create(name, text)
        print(f"Created {name}.")
    return EXIT_OK


def cmd_config_history(args: argparse.Namespace) -> int:
    versions = _store().history(args.report)
    if not versions:
        print("No earlier versions.")
        return EXIT_OK
    print(
        table(
            ["VERSION", "SAVED", "SIZE"],
            [[v.id, fmt_time(v.saved_at), fmt_bytes(v.size)] for v in versions],
        )
    )
    return EXIT_OK


def cmd_config_restore(args: argparse.Namespace) -> int:
    store = _store()
    text = store.read_version(args.report, args.version)
    saved = store.save(args.report, text, store.read(args.report).version)
    print(f"Restored {args.report} to {args.version} (now version {saved.version}).")
    return EXIT_OK


def cmd_config_archive(args: argparse.Namespace) -> int:
    if (
        not args.yes
        and sys.stdin.isatty()
        and input(f"Archive {args.report}? [y/N] ").strip().lower() != "y"
    ):
        print("Cancelled.")
        return EXIT_FAILED
    print(f"Archived to reports/{_store().archive(args.report)}")
    return EXIT_OK
