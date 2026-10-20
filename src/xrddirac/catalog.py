"""The DIRAC File Catalog, for reading: replicas, metadata, listings, sizes.

``DataManagement/FileCatalog`` answers DIRAC's bulk shape for every call -
``{"Successful": {lfn: value}, "Failed": {lfn: reason}}`` - and this client
keeps it (:class:`Bulk`), so a query for a thousand LFNs where three are
missing still answers the other 997.

Large lists are cut into chunks (``chunk``, 500 LFNs by default: a few
hundred kB per answer) and the chunks run in parallel (``workers``), each
call on its own connection; the client's URL rotation spreads them over the
catalog's servers. A failure of a whole call (a server down) fails over to
the next server; a call that fails on every server fails the query.

This client is read-only on purpose: it has no method that adds, removes or
changes anything in the catalog.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from xrdcore.fanout import run_all

from .errors import WireError

if TYPE_CHECKING:
    from .session import DiracSession

__all__ = ["Bulk", "DirectoryListing", "FileCatalog", "FileMetadata", "normalise_lfn"]

V = TypeVar("V")
W = TypeVar("W")

SERVICE = "DataManagement/FileCatalog"


def normalise_lfn(text: str) -> str:
    """An LFN as the catalog keys it: no ``LFN:`` prefix, one leading ``/``, no trailing one."""
    lfn = text.strip()
    if lfn[:4].upper() == "LFN:":
        lfn = lfn[4:]
    lfn = "/" + lfn.strip("/")
    while "//" in lfn:
        lfn = lfn.replace("//", "/")
    return lfn


@dataclass
class Bulk(Generic[V]):
    """A bulk answer: what worked, per LFN, and why the rest did not."""

    successful: dict[str, V] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """Every LFN answered."""
        return not self.failed

    def merge(self, other: Bulk[V]) -> Bulk[V]:
        """This answer with ``other``'s added (for chunks of one query)."""
        self.successful.update(other.successful)
        self.failed.update(other.failed)
        return self

    def map(self, convert: Callable[[str, V], W]) -> Bulk[W]:
        """The same answer with every successful value converted."""
        return Bulk(
            {lfn: convert(lfn, value) for lfn, value in self.successful.items()}, dict(self.failed)
        )

    @classmethod
    def read(cls, value: Any, where: str) -> Bulk[Any]:
        """DIRAC's ``{"Successful": ..., "Failed": ...}`` (``Failed`` may be a list of LFNs)."""
        if not isinstance(value, dict) or "Successful" not in value:
            raise WireError(f"{where}: not a Successful/Failed answer")
        failed = value.get("Failed") or {}
        if isinstance(failed, list):
            failed = {str(lfn): "failed" for lfn in failed}
        return Bulk(dict(value["Successful"]), {str(k): str(v) for k, v in failed.items()})


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """A catalog entry's metadata."""

    lfn: str
    size: int | None = None
    checksum: str = ""
    checksum_type: str = ""
    guid: str = ""
    status: str = ""
    created: _dt.datetime | None = None
    owner: str = ""
    group: str = ""
    mode: int | None = None

    @classmethod
    def read(cls, lfn: str, raw: Mapping[str, Any]) -> FileMetadata:
        """From the catalog's dict (``Size``, ``Checksum``, ``GUID``...)."""
        created = raw.get("CreationDate")
        size = raw.get("Size")
        mode = raw.get("Mode")
        return cls(
            lfn=lfn,
            size=int(size) if isinstance(size, int | str) and str(size).isdigit() else None,
            checksum=str(raw.get("Checksum") or ""),
            checksum_type=str(raw.get("ChecksumType") or ""),
            guid=str(raw.get("GUID") or ""),
            status=str(raw.get("Status") or ""),
            created=created if isinstance(created, _dt.datetime) else None,
            owner=str(raw.get("Owner") or ""),
            group=str(raw.get("OwnerGroup") or ""),
            mode=mode if isinstance(mode, int) else None,
        )


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """One directory: its files (metadata and replicas when ``verbose``) and subdirectories."""

    path: str
    files: dict[str, FileMetadata]
    replicas: dict[str, dict[str, str]]
    subdirs: tuple[str, ...]

    @classmethod
    def read(cls, path: str, raw: Mapping[str, Any]) -> DirectoryListing:
        """From ``listDirectory``'s per-directory dict."""
        files: dict[str, FileMetadata] = {}
        replicas: dict[str, dict[str, str]] = {}
        for name, entry in (raw.get("Files") or {}).items():
            lfn = name if name.startswith("/") else f"{path.rstrip('/')}/{name}"
            details = entry if isinstance(entry, dict) else {}
            files[lfn] = FileMetadata.read(lfn, details.get("MetaData") or {})
            reps = details.get("Replicas") or {}
            replicas[lfn] = {
                se: str(r.get("PFN", "")) for se, r in reps.items() if isinstance(r, dict)
            }
        return cls(path, files, replicas, tuple(sorted(raw.get("SubDirs") or {})))


class FileCatalog:
    """Read-only DIRAC File Catalog queries; see the module docstring."""

    def __init__(self, session: DiracSession, *, chunk: int = 500, workers: int = 4) -> None:
        self.session = session
        self.chunk = chunk
        self.workers = workers

    def _bulk(self, method: str, lfns: Iterable[str], *extra: Any) -> Bulk[Any]:
        """``method(chunk, *extra)`` over every chunk of ``lfns``, merged."""
        unique = list(dict.fromkeys(normalise_lfn(lfn) for lfn in lfns))
        if not unique:
            return Bulk()
        client = self.session.service(SERVICE)
        chunks = [unique[i : i + self.chunk] for i in range(0, len(unique), self.chunk)]

        def one(part: Sequence[str]) -> Callable[[], Bulk[Any]]:
            return lambda: Bulk.read(client.call(method, list(part), *extra), f"{SERVICE}.{method}")

        merged: Bulk[Any] = Bulk()
        for answer in run_all(
            [one(part) for part in chunks], workers=self.workers, name="xrddirac-fc"
        ):
            merged.merge(answer)
        return merged

    def exists(self, lfns: Iterable[str]) -> Bulk[bool]:
        """Whether each path exists (a file or a directory)."""
        return self._bulk("exists", lfns).map(lambda _lfn, value: bool(value))

    def is_file(self, lfns: Iterable[str]) -> Bulk[bool]:
        """Whether each path is a file."""
        return self._bulk("isFile", lfns).map(lambda _lfn, value: bool(value))

    def replicas(self, lfns: Iterable[str], *, all_status: bool = False) -> Bulk[dict[str, str]]:
        """Each file's replicas, ``{SE: PFN}`` (the PFN may be stale; see :mod:`.storage`).

        ``all_status`` includes replicas the catalog marks unusable.
        """
        return self._bulk("getReplicas", lfns, all_status).map(
            lambda _lfn, value: {str(se): str(pfn or "") for se, pfn in dict(value).items()}
        )

    def metadata(self, lfns: Iterable[str]) -> Bulk[FileMetadata]:
        """Each file's size, checksum, GUID, status and ownership."""
        return self._bulk("getFileMetadata", lfns).map(FileMetadata.read)

    def list_directory(
        self, paths: Iterable[str], *, verbose: bool = False
    ) -> Bulk[DirectoryListing]:
        """Each directory's files and subdirectories; ``verbose`` adds metadata and replicas."""
        return self._bulk("listDirectory", paths, verbose).map(DirectoryListing.read)

    def directory_size(self, paths: Iterable[str]) -> Bulk[dict[str, Any]]:
        """Each directory's logical size and file count (the catalog's own accounting)."""
        return self._bulk("getDirectorySize", paths).map(lambda _lfn, value: dict(value))
