"""``lfn:`` as a storage scheme: files in the DIRAC catalog, for xrdwms's staging and output checks.

xrdwms moves and checks a job's data through a small storage interface
(``exists``, ``size``, ``checksum``, ``copy``, ``remove``). :class:`LFNStore`
is that interface for ``lfn:/vo/...`` URLs, doing what ``dirac-dms-add-file``
and ``dirac-dms-remove-files`` do:

* **upload** (``copy(local, "lfn:/...")``): the file goes to a storage
  element's write endpoint (its URL built from the SE's definition, as for
  reads), then is registered in the File Catalog with its size, adler32 and a
  GUID. If the registration fails, the physical copy is removed again, so no
  dark data is left. Uploading the same bytes to the same LFN again is a
  no-op, which makes a retried stage-in safe;
* **exists** (an output check): the catalog knows the LFN, it has at least one
  replica, and its size and checksum are recorded. With ``verify="replica"`` a
  replica is also asked for its real size, and checksum when the catalog has
  one: the strict check, one storage call per file;
* **download** (``copy("lfn:/...", local)``): from the first replica with a URL;
* **remove**: every replica's physical file, then the catalog entry.

The physical transfers are not this package's: they go through the
``physical`` storage it is given - xrdwms's xgfalclient-backed
``GridStorage`` in practice - so xrddirac stays a client of the libraries
only. Every failure is an :class:`OSError` (what xrdwms catches): a DIRAC
refusal becomes ``OSError(EIO)``, a missing LFN ``FileNotFoundError``.

This module is the one place xrddirac writes to the File Catalog. It is used
only when a user configures ``lfn:`` staging or LFN outputs; nothing in the
read-only commands reaches it.
"""

from __future__ import annotations

import contextlib
import errno
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from xrdcore.checksum import checksum_file
from xrdcore.log import get_logger

from .catalog import SERVICE, Bulk, FileMetadata, normalise_lfn
from .errors import ConfigError, DiracError

if TYPE_CHECKING:
    from .session import DiracSession

__all__ = ["LFNStore", "PhysicalStorage", "is_lfn"]

_log = get_logger(__name__)


def is_lfn(location: str) -> bool:
    """``lfn:/...`` (any case)."""
    return location[:4].lower() == "lfn:"


class PhysicalStorage(Protocol):
    """What :class:`LFNStore` needs for the bytes themselves (xrdwms's ``GridStorage``)."""

    def size(self, url: str) -> int:
        """The size of the file at ``url``."""
        ...

    def checksum(self, url: str, algorithm: str = "adler32") -> str:
        """The server's checksum of the file at ``url``."""
        ...

    def copy(self, source: str, target: str) -> None:
        """Copy between a local path and a URL."""
        ...

    def remove(self, url: str) -> None:
        """Delete the file at ``url``."""
        ...


@contextmanager
def _as_oserror(what: str) -> Iterator[None]:
    """DIRAC's refusals inside the block as ``OSError(EIO)``, naming ``what``."""
    try:
        yield
    except DiracError as exc:
        raise OSError(errno.EIO, f"{what}: {exc}") from exc


def _same_adler(one: str, other: str) -> bool:
    """Two adler32 values, compared as DIRAC and storage print them (case, leading zeros)."""
    return one.lower().zfill(8) == other.lower().zfill(8)


def _adler32(path: Path) -> str:
    """A local file's adler32, as DIRAC records it (8 hex digits)."""
    with path.open("rb") as stream:
        return checksum_file("adler32", iter(lambda: stream.read(1 << 20), b""))


class LFNStore:
    """See the module docstring.

    ``se`` is the storage element uploads go to (``CERN-USER``...);
    without one, reads and checks work and uploads are refused.
    ``scheme`` forces the write protocol (default: the SE's
    ``WriteProtocols`` order, then its ``AccessProtocols``).
    """

    def __init__(
        self,
        session: DiracSession,
        physical: PhysicalStorage,
        *,
        se: str | None = None,
        scheme: str | None = None,
        verify: Literal["catalog", "replica"] = "catalog",
    ) -> None:
        self.session = session
        self.physical = physical
        self.se = se
        self.scheme = scheme
        self.verify = verify

    def __repr__(self) -> str:
        """The upload SE and the check, nothing more."""
        return f"LFNStore(se={self.se!r}, verify={self.verify!r})"

    # -- reading the catalog -------------------------------------------------------------------

    def _entry(self, url: str) -> tuple[str, dict[str, str], FileMetadata | None]:
        """``(lfn, replicas, metadata)``; :class:`FileNotFoundError` if the catalog lacks it."""
        lfn = normalise_lfn(url)
        with _as_oserror(f"looking up {lfn}"):
            replicas = self.session.catalog.replicas([lfn])
            if lfn not in replicas.successful:
                raise FileNotFoundError(errno.ENOENT, f"{lfn} is not in the catalog")
            meta = self.session.catalog.metadata([lfn]).successful.get(lfn)
        return lfn, replicas.successful[lfn], meta

    def exists(self, url: str) -> bool:
        """Registered, with a replica and a size (with ``verify="replica"``, a matching replica)."""
        try:
            lfn, replicas, meta = self._entry(url)
        except FileNotFoundError:
            return False
        if not replicas or meta is None or meta.size is None:
            return False
        if self.verify == "replica":
            return self._replica_matches(lfn, replicas, meta)
        return True

    def _replica_matches(self, lfn: str, replicas: dict[str, str], meta: FileMetadata) -> bool:
        """Whether the first replica with a URL holds what the catalog says: size, then adler32."""
        target = self._replica_url(lfn, replicas)
        if self.physical.size(target) != meta.size:
            return False
        if meta.checksum and meta.checksum_type.lower() in ("adler32", ""):
            return _same_adler(self.physical.checksum(target, "adler32"), meta.checksum)
        return True

    def _replica_url(self, lfn: str, replicas: dict[str, str]) -> str:
        """A replica's current URL (built from its SE), else an ``OSError``."""
        for se in replicas:
            try:
                url = self.session.storage(se).url(lfn, self.scheme)
            except ConfigError:
                continue
            if url:
                return url
        raise OSError(errno.ENOENT, f"{lfn}: no replica with a usable URL")

    def size(self, url: str) -> int:
        """The size the catalog records."""
        lfn, _replicas, meta = self._entry(url)
        if meta is None or meta.size is None:
            raise OSError(errno.ENODATA, f"{lfn}: the catalog records no size")
        return int(meta.size)

    def checksum(self, url: str, algorithm: str = "adler32") -> str:
        """The checksum the catalog records (DIRAC keeps adler32)."""
        lfn, _replicas, meta = self._entry(url)
        if algorithm.lower() != "adler32":
            raise OSError(errno.ENOTSUP, f"{lfn}: the catalog keeps adler32, not {algorithm}")
        if meta is None or not meta.checksum:
            raise OSError(errno.ENODATA, f"{lfn}: the catalog records no checksum")
        return str(meta.checksum).lower()

    # -- moving files --------------------------------------------------------------------------

    def copy(self, source: str, target: str) -> None:
        """Upload a local file to an LFN, or download an LFN to a local path."""
        if is_lfn(target) and not is_lfn(source) and "://" not in source:
            self._upload(Path(source), normalise_lfn(target))
        elif is_lfn(source) and not is_lfn(target) and "://" not in target:
            lfn, replicas, _meta = self._entry(source)
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            self.physical.copy(self._replica_url(lfn, replicas), target)
        else:
            raise OSError(
                errno.ENOTSUP, f"Cannot copy {source} to {target}: one side must be local"
            )

    def _upload(self, path: Path, lfn: str) -> None:
        """Put ``path`` on the upload SE and register it as ``lfn`` (undone if that fails)."""
        size = path.stat().st_size
        adler = _adler32(path)
        if self._already(lfn, size, adler):
            _log.info("%s is already registered with the same size and adler32", lfn)
            return
        url = self._write_url(lfn)
        self.physical.copy(str(path), url)
        entry = {
            "PFN": url,
            "SE": self.se,
            "Size": size,
            "Checksum": adler,
            "ChecksumType": "Adler32",
            "GUID": str(uuid.uuid4()).upper(),
        }
        try:
            with _as_oserror(f"registering {lfn}"):
                answer = Bulk.read(
                    self.session.service(SERVICE).call("addFile", {lfn: entry}), "addFile"
                )
            if lfn not in answer.successful:
                raise OSError(errno.EIO, f"registering {lfn}: {answer.failed.get(lfn, 'refused')}")
        except OSError:
            self._remove_quietly(url)
            raise

    def _already(self, lfn: str, size: int, adler: str) -> bool:
        """The LFN is registered already, with exactly these bytes."""
        try:
            _lfn, replicas, meta = self._entry(lfn)
        except FileNotFoundError:
            return False
        same = meta is not None and meta.size == size and _same_adler(meta.checksum, adler)
        if not same:
            raise FileExistsError(errno.EEXIST, f"{lfn} is registered already, with other contents")
        return bool(replicas)

    def _write_url(self, lfn: str) -> str:
        """Where an upload of ``lfn`` goes: the upload SE's write endpoint."""
        if not self.se:
            raise OSError(errno.EINVAL, "No upload SE: set upload_se for the DIRAC backend")
        try:
            element = self.session.storage(self.se)
        except ConfigError as exc:
            raise OSError(errno.EINVAL, str(exc)) from exc
        order = (
            [self.scheme]
            if self.scheme
            else [
                *element.options.get("WriteProtocols", "").replace(" ", "").split(","),
                *element.access_protocols,
            ]
        )
        for scheme in [s for s in order if s]:
            url = element.url(lfn, scheme)
            if url:
                return url
        raise OSError(errno.EINVAL, f"{self.se} has no remote endpoint to write to")

    def _remove_quietly(self, url: str) -> None:
        """Undo a physical copy; a failure is logged, the original error is what matters."""
        try:
            self._delete(url)
        except OSError as exc:
            _log.warning("Could not remove %s after a failed registration: %s", url, exc)

    def _delete(self, url: str) -> None:
        """The physical storage's ``remove``; ``ENOTSUP`` when it has none (xrdwms 0.4.0)."""
        method = getattr(self.physical, "remove", None)
        if method is None:
            raise OSError(errno.ENOTSUP, f"{type(self.physical).__name__} cannot remove {url}")
        method(url)

    def remove(self, url: str) -> None:
        """Every replica's file, then the catalog entry (an LFN not registered is nothing to do)."""
        try:
            lfn, replicas, _meta = self._entry(url)
        except FileNotFoundError:
            return
        for se in replicas:
            try:
                physical = self.session.storage(se).url(lfn, self.scheme)
            except ConfigError:
                continue
            if physical:
                with contextlib.suppress(FileNotFoundError):
                    self._delete(physical)
        with _as_oserror(f"unregistering {lfn}"):
            answer = Bulk.read(
                self.session.service(SERVICE).call("removeFile", [lfn]), "removeFile"
            )
        if lfn not in answer.successful:
            raise OSError(errno.EIO, f"unregistering {lfn}: {answer.failed.get(lfn, 'refused')}")
