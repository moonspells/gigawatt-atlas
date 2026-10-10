"""`atlas validate`: 07 §3.6 over data/records and data/orgs.json."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from atlas.geo.counties import COUNTIES_ZIP
from atlas.schema.export import SCHEMA_PATH


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from e


def _run(args: argparse.Namespace) -> int:
    from atlas.geo.counties import CountyIndex  # DuckDB is imported on use
    from atlas.validate import validate_dataset

    today: date = args.today or datetime.now(UTC).date()
    counties = CountyIndex.load(args.counties)
    try:
        report = validate_dataset(
            args.records, args.orgs, schema_path=args.schema, counties=counties, today=today
        )
    finally:
        counties.close()
    shown = report.issues[: args.max_issues]
    hidden = len(report.issues) - len(shown)
    if args.format == "json":
        payload = {
            "ok": report.ok,
            "records": report.records,
            "in_scope": report.in_scope,
            "out_of_scope": report.out_of_scope,
            "merged": report.merged,
            "issue_count": len(report.issues),
            "issues": [i.to_json() for i in shown],
        }
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        for issue in shown:
            print(issue.format())
        if hidden:
            print(f"... and {hidden} more issues (raise --max-issues to see them)")
        print(
            f"validate: {report.records} records (in scope {report.in_scope}, out of scope "
            f"{report.out_of_scope}, merged {report.merged}), {len(report.issues)} issues",
            file=sys.stdout,
        )
    return 0 if report.ok else 1


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("validate", help="check every record against 07 §3.6")
    parser.add_argument("--records", type=Path, default=Path("data/records"))
    parser.add_argument("--orgs", type=Path, default=Path("data/orgs.json"))
    parser.add_argument("--schema", type=Path, default=Path(SCHEMA_PATH))
    parser.add_argument("--counties", type=Path, default=COUNTIES_ZIP)
    parser.add_argument("--today", type=_date, default=None, help="YYYY-MM-DD (default: UTC today)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--max-issues", type=int, default=200)
    parser.set_defaults(handler=_run)
