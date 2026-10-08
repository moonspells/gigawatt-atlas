"""ZIP files from upstream sources, opened with limits (10 §11.8).

open_zip checks the member count and the declared sizes before anything is extracted, and refuses
absolute paths, "..", symlinks, encrypted entries and compression other than stored or deflate.
SafeZip.read enforces the size limits again while decompressing, and zipfile stops at the declared
size, so a header that lies about its size gets an UnsafeZipError, not more bytes.

zipfile asks the deflate decoder for a bounded amount of output per read, but on some CPython
patch releases (3.13.14, for one) it expands a whole bzip2 or LZMA chunk at once: an 899-byte
bzip2 archive grew memory by 2 GB before any check ran. The upstream ZIPs are all deflate.
"""

from __future__ import annotations

import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

_CHUNK = 1 << 16
_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


class UnsafeZipError(ValueError):
    """The archive breaks one of the limits or contains an unsafe entry."""


def _check_name(name: str) -> None:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise UnsafeZipError(f"absolute path in archive: {name!r}")
    if ".." in PurePosixPath(normalized).parts:
        raise UnsafeZipError(f"path traversal in archive: {name!r}")


class SafeZip:
    def __init__(self, zf: zipfile.ZipFile, *, max_member_bytes: int, max_total_bytes: int) -> None:
        self._zf = zf
        self._max_member = max_member_bytes
        self._max_total = max_total_bytes
        self._read_total = 0

    def names(self) -> list[str]:
        """File members (directories excluded), in archive order."""
        return [i.filename for i in self._zf.infolist() if not i.is_dir()]

    def read(self, name: str) -> bytes:
        """Decompress one member, enforcing the member and total limits as bytes arrive."""
        info = self._zf.getinfo(name)
        if info.is_dir():
            raise UnsafeZipError(f"{name!r} is a directory")
        chunks: list[bytes] = []
        size = 0
        try:
            with self._zf.open(info) as f:
                while chunk := f.read(_CHUNK):
                    size += len(chunk)
                    self._read_total += len(chunk)
                    if size > self._max_member:
                        raise UnsafeZipError(f"{name!r} is larger than {self._max_member} bytes")
                    if self._read_total > self._max_total:
                        raise UnsafeZipError(f"archive is larger than {self._max_total} bytes")
                    chunks.append(chunk)
        except (zipfile.BadZipFile, zlib.error, EOFError) as e:
            # A bad CRC (what a lying size header gives), corrupt deflate data or a short member.
            raise UnsafeZipError(f"{name!r} is corrupt: {e}") from e
        return b"".join(chunks)

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def open_zip(
    path: Path,
    *,
    max_members: int = 50,
    max_member_bytes: int = 50_000_000,
    max_total_bytes: int = 100_000_000,
) -> SafeZip:
    """Open path after checking every entry against the limits."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UnsafeZipError(f"{path}: not a ZIP file ({e})") from e
    try:
        infos = zf.infolist()
        if len(infos) > max_members:
            raise UnsafeZipError(f"{path}: {len(infos)} members > {max_members}")
        total = 0
        for info in infos:
            _check_name(info.filename)
            if info.flag_bits & 0x1:
                raise UnsafeZipError(f"encrypted entry: {info.filename!r}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise UnsafeZipError(f"symlink entry: {info.filename!r}")
            if info.compress_type not in _METHODS:
                raise UnsafeZipError(
                    f"{info.filename!r}: compression method {info.compress_type} is not allowed "
                    "(stored or deflate only)"
                )
            if info.file_size > max_member_bytes:
                raise UnsafeZipError(
                    f"{info.filename!r}: {info.file_size} bytes > {max_member_bytes}"
                )
            total += info.file_size
        if total > max_total_bytes:
            raise UnsafeZipError(f"{path}: {total} bytes uncompressed > {max_total_bytes}")
    except BaseException:
        zf.close()
        raise
    return SafeZip(zf, max_member_bytes=max_member_bytes, max_total_bytes=max_total_bytes)
