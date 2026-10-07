"""The `atlas` command line.

Each module in atlas.commands that defines register(subparsers) adds one command and sets
handler=fn on its parser, where fn(args) -> int. Exit codes: 0 ok, 1 a check failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from collections.abc import Sequence
from typing import TYPE_CHECKING

import atlas.commands
from atlas import __version__

if TYPE_CHECKING:
    SubParsers = argparse._SubParsersAction[argparse.ArgumentParser]


class CommandRegistrationError(RuntimeError):
    """Two command modules registered the same command name."""


def _register_all(subparsers: SubParsers) -> None:
    owners: dict[str, str] = {}
    modules = sorted(pkgutil.iter_modules(atlas.commands.__path__), key=lambda m: m.name)
    for info in modules:
        if info.name.startswith("_"):
            continue
        module_name = f"{atlas.commands.__name__}.{info.name}"
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if not callable(register):
            continue
        before = set(subparsers.choices)
        try:
            register(subparsers)
        except argparse.ArgumentError as e:
            raise CommandRegistrationError(f"{module_name}: {e}") from e
        for name in set(subparsers.choices) - before:
            if name in owners:  # pragma: no cover (argparse raises first on 3.11+)
                raise CommandRegistrationError(
                    f"command {name!r} registered by both {owners[name]} and {module_name}"
                )
            owners[name] = module_name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="Gigawatt Atlas: validate, import and publish the data center dataset.",
    )
    parser.add_argument("--version", action="version", version=f"atlas {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command", required=True)
    _register_all(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # --help, --version and usage errors (exit 2)
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_usage()
        return 2
    return int(handler(args))
