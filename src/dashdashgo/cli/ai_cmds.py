"""``dashdashgo ai ...``: the AI assistant from a terminal."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dashdashgo.ai.assistant import AIAssistant, Diagnosis
from dashdashgo.cli.output import paint, print_json
from dashdashgo.cli.runs import _container
from dashdashgo.errors import AINotConfiguredError

EXIT_OK, EXIT_FAILED = 0, 1


def _assistant() -> AIAssistant:
    assistant = _container().assistant
    if assistant is None or not assistant.enabled:
        raise AINotConfiguredError(
            "the AI assistant is off: set AI_API_KEY (free keys: see README, 'AI assistant')"
        )
    return assistant


def print_diagnosis(diagnosis: Diagnosis) -> None:
    print(paint(diagnosis.summary, "1"))
    print(
        f"\n{paint('Category', '2')}    {diagnosis.category}"
        f"{' (transient: retrying later should work)' if diagnosis.transient else ''}"
    )
    print(f"{paint('Confidence', '2')}  {diagnosis.confidence:.0%}")
    print(f"\n{paint('Likely cause', '1')}\n{diagnosis.likely_cause}")
    print(f"\n{paint('Suggested fix', '1')}\n{diagnosis.suggested_fix}")
    if diagnosis.config_overrides:
        sets = " ".join(f"--set {o!r}" for o in diagnosis.config_overrides)
        print(f"\n{paint('Retry with the suggested change', '1')}")
        print(f"  dashdashgo retry {diagnosis.run_id} {sets}")
    elif diagnosis.overrides_note:
        print(f"\n{paint('Note', '2')}: {diagnosis.overrides_note}")
    print(paint(f"\n({diagnosis.model})", "2"))


def cmd_diagnose(args: argparse.Namespace) -> int:
    diagnosis = _assistant().diagnose(args.run_id)
    if args.json:
        print_json(diagnosis.model_dump(mode="json"))
    else:
        print_diagnosis(diagnosis)
    return EXIT_OK


def cmd_draft(args: argparse.Namespace) -> int:
    sample = Path(args.sample)
    draft = _assistant().draft(args.name, sample.name, sample.read_bytes(), from_report=args.source)
    if args.output:
        Path(args.output).write_text(draft.yaml, encoding="utf-8")
        print(f"Draft written to {args.output}", file=sys.stderr)
    else:
        print(draft.yaml, end="")
    for note in draft.notes:
        print(f"note: {note}", file=sys.stderr)
    for location, message in draft.problems:
        print(f"problem: {location}: {message}", file=sys.stderr)
    if draft.problems:
        print("The draft does not validate yet; fix the problems above.", file=sys.stderr)
        return EXIT_FAILED
    print(
        f"Valid. Create it with: dashdashgo config import {args.output or '<file>'} "
        f"--name {args.name}",
        file=sys.stderr,
    )
    return EXIT_OK
