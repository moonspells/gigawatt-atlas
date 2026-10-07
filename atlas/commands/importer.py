"""`atlas import <source>`: run a seed importer and merge its candidates into data/records.

Importers are discovered, not listed: every module in atlas.sources that defines a module-level
IMPORTER (an atlas.sources.base.Importer) becomes a subcommand named IMPORTER.name.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import atlas.sources

if TYPE_CHECKING:
    from atlas.sources.base import Importer

MAX_REVIEW_PRINTED = 20
_SKIP_MODULES = frozenset({"base"})


def discover_importers() -> dict[str, Importer]:
    """IMPORTER objects from atlas/sources/*.py, keyed by name. Duplicate names raise."""
    found: dict[str, Importer] = {}
    owners: dict[str, str] = {}
    for info in sorted(pkgutil.iter_modules(atlas.sources.__path__), key=lambda m: m.name):
        if info.name.startswith("_") or info.name in _SKIP_MODULES:
            continue
        module_name = f"{atlas.sources.__name__}.{info.name}"
        module = importlib.import_module(module_name)
        importer = getattr(module, "IMPORTER", None)
        if importer is None:
            continue
        name = importer.name
        if name in found:
            raise RuntimeError(
                f"importer name {name!r} defined by both {owners[name]} and {module_name}"
            )
        found[name] = importer
        owners[name] = module_name
    return found


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 date-time: {value!r}") from e
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"--now needs a time zone (for example ...Z): {value!r}")
    return parsed


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", type=Path, default=None, help="read this file, do not fetch")
    parser.add_argument("--records", type=Path, default=Path("data/records"))
    parser.add_argument("--review-dir", type=Path, default=Path("review/queue"))
    parser.add_argument("--receipts-dir", type=Path, default=Path("data/imports"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/atlas"))
    parser.add_argument(
        "--counties", type=Path, default=Path("reference/census/cb_2025_us_county_5m.zip")
    )
    parser.add_argument("--now", type=_aware, default=None, help="ISO 8601 with a time zone")
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument(
        "--offline", action="store_true", help="refuse every network request (inputs from files)"
    )


def _run(args: argparse.Namespace) -> int:
    from atlas.ids import new_record_id
    from atlas.net import FetchError, OfflineTransport, make_client, user_agent_from_env
    from atlas.sources.base import ImportContext, merge_import
    from atlas.store import RecordStore, StoreError

    importer: Importer = args.importer
    now: datetime = (args.now or datetime.now(UTC)).astimezone(UTC)
    store = RecordStore(args.records)
    try:
        records = store.load()
    except StoreError as e:
        for problem in e.problems:
            print(problem, file=sys.stderr)
        return 1
    user_agent = user_agent_from_env()
    transport = OfflineTransport() if args.offline else None
    with make_client(user_agent=user_agent, transport=transport) as http:
        ctx = ImportContext(
            now=now,
            today=now.date(),
            records=records,
            http=http,
            cache_dir=args.cache_dir,
            input_path=args.input,
            user_agent=user_agent,
            new_id=new_record_id,
            counties_path=args.counties,
        )
        try:
            result = importer.run(ctx, args)
        except FetchError as e:
            print(f"import {importer.name}: {e}", file=sys.stderr)
            return 1
        receipt, review = merge_import(
            importer,
            result,
            ctx,
            store=store,
            review_dir=args.review_dir,
            receipts_dir=args.receipts_dir,
            dry_run=args.dry_run,
        )
    counts = " ".join(f"{k}={v}" for k, v in receipt.counts.items())
    print(f"import {importer.name}{' (dry run)' if args.dry_run else ''}: {counts}")
    if receipt.metrics:
        print("metrics: " + " ".join(f"{k}={v}" for k, v in sorted(receipt.metrics.items())))
    for i in review[:MAX_REVIEW_PRINTED]:
        print(f"  [{i.kind}] {i.external_id or '-'} {i.record_id or '-'}: {i.reason}")
    if len(review) > MAX_REVIEW_PRINTED:
        print(f"  ... and {len(review) - MAX_REVIEW_PRINTED} more in {args.review_dir}")
    return 1 if receipt.counts.get("invalid", 0) > 0 else 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("import", help="run a seed importer (atlas/sources/*.py)")
    sources = parser.add_subparsers(dest="source", metavar="source", required=True)
    for name, importer in discover_importers().items():
        sp = sources.add_parser(name, help=importer.help)
        _add_common(sp)
        importer.add_arguments(sp)
        sp.set_defaults(handler=_run, importer=importer)
