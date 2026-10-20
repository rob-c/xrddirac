"""Job sandboxes: packing files into one, and unpacking one safely.

A DIRAC sandbox is a compressed tar file. Input sandboxes are packed here as
``tar.bz2`` - the format every DIRAC since v6 accepts, through DISET's
``SandboxStore`` or DiracX's S3 store alike, and one the standard library
writes. Output sandboxes come back as whatever the job's wrapper wrote:
``tar.bz2`` from DIRAC 8, often ``tar.zst`` (Zstandard) from DIRAC 9; the
latter needs Python 3.14's ``compression.zstd`` or the ``zstandard`` package.

Unpacking never writes outside the destination: the archive is read with
the ``data`` extraction filter (no absolute paths, no ``..``, no links that
leave the directory, no device files), whatever the archive claims.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import os
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError, DiracError

__all__ = ["Packed", "pack", "unpack"]

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


@dataclass(frozen=True, slots=True)
class Packed:
    """A packed sandbox: its bytes, their SHA-256 (DiracX) and MD5 (DISET), the format."""

    data: bytes
    sha256: str
    md5: str
    format: str = "tar.bz2"

    @property
    def size(self) -> int:
        """Bytes."""
        return len(self.data)


def pack(
    paths: Iterable[str | os.PathLike[str]], *, extra: dict[str, bytes] | None = None
) -> Packed:
    """``paths`` (directories recursively) as a ``tar.bz2``, each under its own name.

    ``extra`` adds in-memory files (``{"data.py": b"..."}``): a generated
    options file need not be written to disk first. A missing path is a
    :class:`FileNotFoundError` naming it, before anything is packed.
    """
    files = [Path(p) for p in paths]
    for path in files:
        if not path.exists():
            raise FileNotFoundError(f"No such input sandbox file: {path}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as archive:
        for path in files:
            archive.add(str(path.resolve()), arcname=path.name, recursive=True)
        for name, content in (extra or {}).items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    data = buffer.getvalue()
    return Packed(
        data,
        hashlib.sha256(data).hexdigest(),
        hashlib.md5(data, usedforsecurity=False).hexdigest(),
    )


def unpack(data: bytes, destination: str | os.PathLike[str]) -> list[str]:
    """Extract a sandbox into ``destination`` (made if missing); the files in it, relative paths."""
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    stream = io.BytesIO(_decompress_zstd(data) if data[:4] == _ZSTD_MAGIC else data)
    try:
        with tarfile.open(fileobj=stream, mode="r:*") as archive:
            members = archive.getmembers()
            archive.extractall(target, members=members, filter="data")
    except tarfile.TarError as exc:
        raise DiracError(f"The sandbox is not a readable tar archive: {exc}") from exc
    return sorted(m.name for m in members if m.isfile())


def _decompress_zstd(data: bytes) -> bytes:
    """Zstandard-compressed bytes, decompressed with whichever library this Python has."""
    module = _zstd_module()
    if hasattr(module, "decompress") and module.__name__ == "compression.zstd":
        result: bytes = module.decompress(data)
        return result
    reader: Any = module.ZstdDecompressor().stream_reader(io.BytesIO(data))
    with reader:
        return bytes(reader.read())


def _zstd_module() -> Any:
    """``compression.zstd`` (Python 3.14) or ``zstandard``; :class:`ConfigError` with neither."""
    for name in ("compression.zstd", "zstandard"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ConfigError(
        "This sandbox is Zstandard-compressed (tar.zst): use Python 3.14 or pip install zstandard"
    )
