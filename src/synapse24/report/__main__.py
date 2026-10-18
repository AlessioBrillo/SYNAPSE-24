"""CLI for SessionReport v1: ``python -m synapse24.report <catalog> <session_id>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo src is importable when run as `python -m synapse24.report`.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from synapse24.report import build_session_report  # noqa: E402
from synapse24.sessions import load_catalog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="synapse-report",
        description="Build a SessionReport v1 from a session catalog.",
    )
    parser.add_argument("catalog", help="Session catalog JSON (from synapse-sessions index)")
    parser.add_argument("session_id", help="Session id to report on")
    parser.add_argument(
        "--jobs",
        help="Optional JSON dict of worker job results keyed by computation type",
    )
    parser.add_argument(
        "--out",
        help="Write report JSON to this file (default: print to stdout)",
    )
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    record = catalog.sessions.get(args.session_id)
    if record is None:
        print(f"session {args.session_id} not found in catalog", file=sys.stderr)
        return 1

    job_results: dict[str, dict] | None = None
    if args.jobs:
        job_results = json.loads(Path(args.jobs).read_text(encoding="utf-8"))

    report = build_session_report(record, job_results)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
