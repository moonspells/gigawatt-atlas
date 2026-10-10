"""DuckDB with the spatial extension.

The extension comes from extensions.duckdb.org, and DuckDB checks its signature on LOAD. CI runs
`python -m atlas.geo.duck --install` once per job; connect() installs it on first use otherwise.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb


def connect() -> duckdb.DuckDBPyConnection:
    """An in-memory connection with spatial loaded (installed first if missing)."""
    import duckdb  # heavy, so imported on use

    con = duckdb.connect(":memory:")
    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
    return con


def install() -> str:
    """Install and load spatial; return the DuckDB version."""
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    con.close()
    return str(duckdb.__version__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m atlas.geo.duck", description="DuckDB spatial extension helper"
    )
    parser.add_argument("--install", action="store_true", help="install the spatial extension")
    args = parser.parse_args(argv)
    if args.install:
        print(f"duckdb {install()}: spatial installed")
        return 0
    con = connect()
    con.close()
    print("spatial loads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
