from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from atlas.safezip import SafeZip, UnsafeZipError, open_zip


def make_zip(
    path: Path, members: dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED
) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            # A fixed timestamp keeps the bytes the same from run to run.
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zf.writestr(info, data, compress_type=compression)
    return path


def lie_about_size(path: Path, name: str, declared: int) -> None:
    """Patch name's uncompressed size in the local header and the central directory."""
    with zipfile.ZipFile(path) as zf:
        real = zf.getinfo(name).file_size.to_bytes(4, "little")
    data = path.read_bytes()
    assert data.count(real) == 2
    path.write_bytes(data.replace(real, declared.to_bytes(4, "little")))


@pytest.mark.parametrize("compression", [zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED])
def test_reads_members(tmp_path: Path, compression: int) -> None:
    members = {"a.csv": b"x,y\n1,2\n", "dir/b.txt": b"hi"}
    path = make_zip(tmp_path / "ok.zip", members, compression)
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


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_refuses_bzip2_and_lzma_even_if_the_header_is_small(
    tmp_path: Path, compression: int
) -> None:
    # On CPython 3.13.14 zipfile expands a whole bzip2 or LZMA chunk at once, so a member whose
    # header says 10 bytes grew memory by gigabytes before SafeZip.read could count them.
    path = make_zip(tmp_path / "bomb.zip", {"data.csv": b"\0" * 4_000_000}, compression)
    lie_about_size(path, "data.csv", 10)
    with pytest.raises(UnsafeZipError, match="compression method"):
        open_zip(path, max_member_bytes=1_000)


def test_a_deflate_header_that_lies_gets_no_more_bytes(tmp_path: Path) -> None:
    # zipfile stops at the declared size, and the CRC then fails: a corrupt member, not data.
    path = make_zip(tmp_path / "liar.zip", {"bomb.bin": b"\0" * 50_000})
    lie_about_size(path, "bomb.bin", 10)
    with open_zip(path, max_member_bytes=1_000) as z, pytest.raises(UnsafeZipError, match="CRC"):
        z.read("bomb.bin")


def test_read_enforces_the_limits_as_bytes_arrive(tmp_path: Path) -> None:
    # open_zip has already checked the declared sizes; read() counts the bytes themselves.
    path = make_zip(tmp_path / "big.zip", {"a": b"\0" * 6_000, "b": b"\0" * 6_000})
    member = SafeZip(zipfile.ZipFile(path), max_member_bytes=5_000, max_total_bytes=100_000)
    with member, pytest.raises(UnsafeZipError, match="larger than 5000 bytes"):
        member.read("a")
    total = SafeZip(zipfile.ZipFile(path), max_member_bytes=10_000, max_total_bytes=10_000)
    with total:
        assert total.read("a") == b"\0" * 6_000
        with pytest.raises(UnsafeZipError, match="archive is larger than 10000 bytes"):
            total.read("b")


def test_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "x.zip"
    path.write_bytes(b"not a zip")
    with pytest.raises(UnsafeZipError, match="not a ZIP"):
        open_zip(path)
