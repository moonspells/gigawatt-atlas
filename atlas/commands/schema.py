"""`atlas schema export [--out schema/facility.v1.json] [--check]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas.jsonio import write_text
from atlas.schema.export import SCHEMA_PATH, render


def _export(args: argparse.Namespace) -> int:
    out: Path = args.out
    text = render()
    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else None
        if current != text:
            print(f"{out} is out of date; run uv run atlas schema export", file=sys.stderr)
            return 1
        print(f"{out} is up to date")
        return 0
    write_text(out, text)
    print(f"wrote {out}")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("schema", help="JSON Schema of the facility record")
    actions = parser.add_subparsers(dest="schema_command", metavar="action", required=True)
    export = actions.add_parser("export", help=f"write {SCHEMA_PATH} from atlas/schema/record.py")
    export.add_argument("--out", type=Path, default=Path(SCHEMA_PATH), help="output file")
    export.add_argument("--check", action="store_true", help="exit 1 if the file is out of date")
    export.set_defaults(handler=_export)
