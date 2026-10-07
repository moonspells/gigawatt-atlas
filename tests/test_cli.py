from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import atlas.commands
from atlas import __version__
from atlas.cli import CommandRegistrationError, build_parser, main


def test_discovery_registers_the_scaffold_commands() -> None:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert {"validate", "schema", "import"} <= set(sub.choices)


def test_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out
    assert main([]) == 2
    assert main(["no-such-command"]) == 2
    assert main(["schema"]) == 2
    assert main(["--help"]) == 0


def write_module(directory: Path, name: str, command: str) -> None:
    (directory / f"{name}.py").write_text(
        "from __future__ import annotations\n"
        "import argparse\n"
        "def _run(args: argparse.Namespace) -> int:\n"
        "    return 7\n"
        "def register(sub):\n"
        f"    p = sub.add_parser({command!r}, help='test command')\n"
        "    p.set_defaults(handler=_run)\n",
        encoding="utf-8",
    )


@pytest.fixture
def extra_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A directory added to atlas.commands' search path; its modules are unloaded afterwards."""
    monkeypatch.setattr(atlas.commands, "__path__", [*atlas.commands.__path__, str(tmp_path)])
    yield tmp_path
    for name in [m for m in sys.modules if m.startswith("atlas.commands.zz_")]:
        del sys.modules[name]


def test_new_command_module_is_discovered(extra_commands: Path) -> None:
    write_module(extra_commands, "zz_hello", "hello")
    assert main(["hello"]) == 7


def test_duplicate_command_name_fails_at_startup(extra_commands: Path) -> None:
    write_module(extra_commands, "zz_dupe", "validate")
    with pytest.raises(CommandRegistrationError, match="zz_dupe"):
        build_parser()


def test_python_dash_m_atlas(repo_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "atlas", "--version"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"atlas {__version__}"
