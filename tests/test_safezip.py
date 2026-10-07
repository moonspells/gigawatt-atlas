from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from atlas.safezip import UnsafeZipError, open_zip


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_reads_members(tmp_path: Path) -> None:
    path = make_zip(tmp_path / "ok.zip", {"a.csv": b"x,y\n1,2\n", "dir/b.txt": b"hi"})
    with open_zip(path) as z:
        assert z.names() == ["a.csv", "dir/b.txt"]
        assert z.read("dir/b.txt") == b"hi"


@pytest.mark.parametrize(
    "name", ["../evil.txt", "a/../../evil.txt", "/etc/passwd", "C:/x.txt", "..\\x.txt"]
)
def test_refuses_unsafe_paths(tmp_path: Path, name: str) -> None:
    path = make_zip(tmp_path / "bad.zip", {name: b"x"})
    with pytest.raises(UnsafeZipError):
        open_zip(path)


def test_refuses_symlinks(tmp_path: Path) -> None:
    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(UnsafeZipError, match="symlink"):
        open_zip(path)


def test_member_count_limit(tmp_path: Path) -> None:
    path = make_zip(tmp_path / "many.zip", {f"f{i}.txt": b"x" for i in range(6)})
    with pytest.raises(UnsafeZipError, match="members"):
        open_zip(path, max_members=5)
    open_zip(path, max_members=6).close()


def test_member_size_limit(tmp_path: Path) -> None:
    path = make_zip(tmp_path / "big.zip", {"big.bin": b"\0" * 10_000})
    with pytest.raises(UnsafeZipError):
        open_zip(path, max_member_bytes=9_999)


def test_total_size_limit(tmp_path: Path) -> None:
    path = make_zip(tmp_path / "total.zip", {"a": b"\0" * 6_000, "b": b"\0" * 6_000})
    with pytest.raises(UnsafeZipError, match="uncompressed"):
        open_zip(path, max_total_bytes=10_000)


def test_read_enforces_limits_even_if_the_header_lies(tmp_path: Path) -> None:
    path = make_zip(tmp_path / "liar.zip", {"bomb.bin": b"\0" * 50_000})
    data = bytearray(path.read_bytes())
    # Patch the declared uncompressed size (central directory and local header) to 10 bytes.
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo("bomb.bin")
    declared = info.file_size.to_bytes(4, "little")
    data = data.replace(declared, (10).to_bytes(4, "little"))
    path.write_bytes(bytes(data))
    with (
        pytest.raises((UnsafeZipError, zipfile.BadZipFile)),
        open_zip(path, max_member_bytes=1_000) as z,
    ):
        z.read("bomb.bin")


def test_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "x.zip"
    path.write_bytes(b"not a zip")
    with pytest.raises(UnsafeZipError, match="not a ZIP"):
        open_zip(path)
