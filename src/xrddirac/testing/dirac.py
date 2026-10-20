"""A fake DIRAC installation, in process: DISET services, DiracX, S3 sandboxes, a tape REST API.

:class:`FakeDirac` is one small DIRAC with one model behind every service
xrddirac uses, so a job submitted through ``JobManager`` (DISET) is found by
DiracX's search, and its output sandbox downloads by either path::

    fake = FakeDirac(pki).start()
    fake.add_file("/vo/data/f1", size=10, replicas={"SITE-DISK": "", "SITE-TAPE": ""})
    fake.tape["/tape/vo/data/f1"] = "TAPE"
    with fake.session(tmp_path) as dirac:                # a DiracSession on the fake
        dirac.catalog.replicas(["/vo/data/f1"])
    fake.finish(job_id, files={"out.txt": b"hello"})     # the job ends, with an output sandbox

The configuration it serves names its own services, three storage elements
(a disk SE, a tape SE whose HTTPS endpoint is the fake's tape REST API, an
alias of the disk SE) and the sandbox SE, the test PKI's user in the
registry, and DiracX at the fake HTTPS server - unless built with
``diracx=False``, which makes it a DIRAC 8-style, DISET-only installation
(GridPP's shape), with ``setup`` naming its setup.

Stability: test API, versioned with the package.
"""

from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import io
import itertools
import json
import re
import tarfile
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from xrdsec.proxytool import read_proxy_info
from xrdsec.testing.https import FakeHTTPSServer, Request, Response

from ..errors import ENOAUTH
from ..jdl import JDL
from ..profiles import Profile
from .diset import FakeDisetServer, Refuse

if TYPE_CHECKING:
    from xrdsec.testing.pki import BasePKI

    from ..session import DiracSession

__all__ = ["FakeDirac", "FakeJob", "MemoryStorage", "sandbox_bytes"]

VO = "testvo"
USER = "tuser"
GROUP = "testvo_user"
VERSION = "2026-01-01 00:00:00.000001"


def sandbox_bytes(files: dict[str, bytes]) -> bytes:
    """A ``tar.bz2`` sandbox of ``files``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class MemoryStorage:
    """Physical storage in memory, by URL: what :class:`~xrddirac.lfnstore.LFNStore` copies through.

    ``fail_copy``/``fail_remove`` make those operations raise ``OSError``.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.fail_copy = False
        self.fail_remove = False

    def size(self, url: str) -> int:
        return len(self._get(url))

    def checksum(self, url: str, algorithm: str = "adler32") -> str:
        from xrdcore.checksum import checksum_bytes

        return checksum_bytes(algorithm, self._get(url))

    def copy(self, source: str, target: str) -> None:
        if self.fail_copy:
            raise OSError(errno.EIO, f"copy to {target} failed")
        if "://" in target:
            self.files[target] = Path(source).read_bytes()
        else:
            Path(target).write_bytes(self._get(source))

    def remove(self, url: str) -> None:
        if self.fail_remove:
            raise OSError(errno.EACCES, f"cannot remove {url}")
        self._get(url)
        del self.files[url]

    def _get(self, url: str) -> bytes:
        if url not in self.files:
            raise FileNotFoundError(errno.ENOENT, f"No such file {url}")
        return self.files[url]


@dataclass
class FakeJob:
    """One job in the fake's job database."""

    job_id: int
    jdl: JDL
    status: str = "Received"
    minor: str = "Job accepted"
    application: str = "Unknown"
    site: str = "ANY"
    owner: str = USER
    submitted: _dt.datetime = field(default_factory=lambda: _dt.datetime(2026, 1, 1, 12, 0, 0))
    sandboxes: dict[str, list[str]] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    history: list[tuple[str, str, str, str, str]] = field(default_factory=list)

    def attributes(self) -> dict[str, Any]:
        """The job as JobMonitoring and DiracX report it."""
        return {
            "JobID": self.job_id,
            "JobName": str(self.jdl.get("JobName", "")),
            "JobGroup": str(self.jdl.get("JobGroup", "")),
            "Owner": self.owner,
            "OwnerGroup": GROUP,
            "Status": self.status,
            "MinorStatus": self.minor,
            "ApplicationStatus": self.application,
            "Site": self.site,
            "SubmissionTime": self.submitted,
            "LastUpdateTime": self.submitted,
            "StartExecTime": None,
            "EndExecTime": None,
        }


class FakeDirac:
    """See the module docstring."""

    def __init__(
        self,
        pki: BasePKI[Any, Any, Any],
        *,
        diracx: bool = True,
        setup: str = "None",
        rss: bool = True,
    ) -> None:
        self.pki = pki
        self.use_diracx = diracx
        self.setup = setup
        self.rss = rss
        self.diset = FakeDisetServer(pki.server_context())
        self.https = FakeHTTPSServer(tls=pki.server_context(require_client=False), host="localhost")
        #: The tape system's REST API: a storage endpoint, so it wants the proxy.
        self.tape_https = FakeHTTPSServer(tls=pki.server_context(), host="localhost")
        self.files: dict[str, dict[str, Any]] = {}
        self.datasets: dict[str, list[str]] = {}
        self.bk_metadata: dict[str, dict[str, Any]] = {}
        self.jobs: dict[int, FakeJob] = {}
        self.se_status: dict[str, str] = {
            "SITE-DISK": "Active",
            "SITE-TAPE": "Active",
            "OTHER-DISK": "Banned",
        }
        #: Tape locality by the path the tape REST API sees.
        self.tape: dict[str, str] = {}
        #: Stored sandboxes by their store path (``/SandBox/...`` and ``/S3/...``).
        self.sandboxes: dict[str, bytes] = {}
        #: The bytes behind the catalog's replicas (``url -> bytes``), for LFN staging tests.
        self.physical = MemoryStorage()
        #: ``addFile`` refusals by LFN.
        self.refuse_register: dict[str, str] = {}
        self.tokens_made = 0
        self.token_lifetime = 3600
        self.require_proxy_upload = False
        self.config_version = VERSION
        #: ``job -> files``: when set, every job ends ``Done`` as soon as it is
        #: submitted, with these files in its output sandbox (a grid that runs instantly).
        self.autorun: Callable[[FakeJob], dict[str, bytes]] | None = None
        self._ids = itertools.count(1000)
        self._lock = threading.Lock()
        self._register()

    # -- life -----------------------------------------------------------------------------------

    def start(self) -> FakeDirac:
        """Start the servers."""
        self.diset.start()
        self.https.start()
        self.tape_https.start()
        return self

    def stop(self) -> None:
        """Stop the servers (again is nothing)."""
        self.diset.stop()
        for server in (self.https, self.tape_https):
            if server._thread is not None:
                server.stop()
                server._thread = None

    def __enter__(self) -> FakeDirac:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def https_url(self) -> str:
        """The fake DiracX/S3 server's URL."""
        return self.https.url

    @property
    def tape_url(self) -> str:
        """The fake tape REST API's server URL."""
        return self.tape_https.url

    def profile(self, **changes: Any) -> Profile:
        """An xrddirac profile for this installation."""
        values: dict[str, Any] = {
            "name": "fake",
            "servers": (self.diset.url("Configuration/Server"),),
            "vo": VO,
            "setup": self.setup,
            "config_max_age": 0.0,
        }
        values.update(changes)
        return Profile(**values)

    def session(self, cache: Path, **profile: Any) -> DiracSession:
        """A :class:`DiracSession` on this installation, with the PKI's proxy and CAs."""
        from ..session import DiracSession

        return DiracSession(
            self.profile(**profile),
            proxy=str(self.pki.proxy_path),
            ca_path=str(self.pki.ca_dir),
            cache_dir=cache,
            timeout=30,
        )

    # -- the model --------------------------------------------------------------------------------

    def add_file(
        self,
        lfn: str,
        *,
        size: int = 100,
        replicas: dict[str, str] | None = None,
        checksum: str = "deadbeef",
    ) -> None:
        """A catalog file and its replicas (``{SE: pfn}``)."""
        self.files[lfn] = {
            "MetaData": {
                "Size": size,
                "Checksum": checksum,
                "ChecksumType": "Adler32",
                "GUID": hashlib.md5(lfn.encode(), usedforsecurity=False).hexdigest().upper(),
                "Status": "AprioriGood",
                "CreationDate": _dt.datetime(2025, 4, 17, 15, 10, 45),
                "Owner": USER,
                "OwnerGroup": GROUP,
                "Mode": 509,
            },
            "Replicas": dict(replicas or {}),
        }

    def add_dataset(self, path: str, lfns: list[str], *, run: int = 1, events: int = 10) -> None:
        """A Bookkeeping dataset (its LFNs must be catalog files too, to have replicas)."""
        self.datasets[path] = list(lfns)
        for lfn in lfns:
            self.bk_metadata[lfn] = {
                "FileSize": self.files.get(lfn, {}).get("MetaData", {}).get("Size", 100),
                "EventStat": events,
                "Runnumber": run,
                "Luminosity": 1.5,
                "FileType": path.rsplit("/", 1)[-1],
                "EventType": 90000000,
                "DataqualityFlag": "OK",
                "GotReplica": "Yes",
                "GUID": "G",
            }

    def finish(
        self,
        job_id: int,
        *,
        status: str = "Done",
        minor: str = "Execution Complete",
        application: str = "",
        files: dict[str, bytes] | None = None,
        output_data: dict[str, bytes] | None = None,
    ) -> None:
        """End a job, with an output sandbox of ``files`` and ``output_data`` registered by LFN."""
        for lfn, data in (output_data or {}).items():
            self.register(lfn, data)
        job = self.jobs[job_id]
        job.status, job.minor, job.application = status, minor, application or job.application
        job.site = "LCG.Test.org"
        if files is not None:
            data = sandbox_bytes(files)
            digest = hashlib.sha256(data).hexdigest()
            path = (
                f"/S3/sandboxes/{VO}/{GROUP}/{USER}/sha256:{digest}.tar.bz2"
                if self.use_diracx
                else f"/SandBox/t/{digest}.tar.bz2"
            )
            self.sandboxes[path] = data
            job.sandboxes.setdefault("Output", []).append(f"SB:SandboxSE|{path}")
            self.diset.files[f"{self.diset.url('WorkloadManagement/SandboxStore')}{path}"] = data

    # -- the configuration ------------------------------------------------------------------------

    def configuration(self) -> str:
        """The ``.cfg`` text the fake Configuration Service hands out."""
        url = self.diset.url
        services = {
            "Configuration": ["Server"],
            "DataManagement": ["FileCatalog"],
            "Framework": ["ProxyManager"],
            "WorkloadManagement": ["JobManager", "JobMonitoring", "SandboxStore"],
            "Bookkeeping": ["BookkeepingManager"],
            **({"ResourceStatus": ["ResourceStatus"]} if self.rss else {}),
        }
        dirac8 = self.setup != "None"
        systems = []
        for system, components in services.items():
            lines = "\n".join(f"        {c} = {url(f'{system}/{c}')}" for c in components)
            other = f"        Other = https://localhost:1/{system}/Other"
            body = f"      URLs\n      {{\n{lines}\n{other}\n      }}"
            if dirac8:
                body = f"    Production\n    {{\n{body}\n    }}"
            systems.append(f"  {system}\n  {{\n{body}\n  }}")
        setups = (
            f"  Setups\n  {{\n    {self.setup}\n    {{\n"
            + "".join(f"      {s} = Production\n" for s in services)
            + "    }\n  }\n"
            if dirac8
            else ""
        )
        https = urlsplit(self.tape_url)
        dips = urlsplit(url("WorkloadManagement/SandboxStore"))
        diracx = f"DiracX\n{{\n  URL = {self.https_url}\n}}\n" if self.use_diracx else ""
        identity = read_proxy_info(self.pki.proxy_path).identity
        return f"""DIRAC
{{
  Configuration
  {{
    Version = {self.config_version}
    Servers = {url("Configuration/Server")}
  }}
{setups}}}
Systems
{{
{chr(10).join(systems)}
}}
Operations
{{
  Defaults
  {{
    DataManagement
    {{
      AccessProtocols = root, https
    }}
  }}
}}
Registry
{{
  Users
  {{
    {USER}
    {{
      DN = {identity}
    }}
  }}
  Groups
  {{
    {GROUP}
    {{
      VO = {VO}
    }}
  }}
}}
Resources
{{
  StorageElementBases
  {{
    DISK-BASE
    {{
      SEType = T0D1
      BackendType = Eos
      AccessProtocols = root, https
      XRootConfig
      {{
        Host = disk.example.org
        Protocol = root
        Path = /eos/{VO}/grid
        Access = remote
      }}
      HTTPSConfig
      {{
        Host = disk.example.org
        Port = 8443
        Protocol = https
        Path = /eos/{VO}/grid
        Access = remote
      }}
      Occupancy
      {{
        Plugin = WLCGAccountingJson
      }}
      LocalConfig
      {{
        Host = localhost
        Protocol = file
        Path = /local
        Access = local
      }}
    }}
    TAPE-BASE
    {{
      SEType = T1D0
      BackendType = Cta
      AccessProtocols = https
      HTTPSConfig
      {{
        Host = {https.hostname}
        Port = {https.port}
        Protocol = https
        Path = /tape
        Access = remote
      }}
    }}
  }}
  StorageElements
  {{
    SITE-DISK
    {{
      BaseSE = DISK-BASE
    }}
    SITE-TAPE
    {{
      BaseSE = TAPE-BASE
    }}
    OTHER-DISK
    {{
      BaseSE = DISK-BASE
      ReadAccess = Banned
      XRootConfig
      {{
        Path = /eos/other
      }}
    }}
    ALIAS-DISK
    {{
      Alias = SITE-DISK
    }}
    SandboxSE
    {{
      DIP
      {{
        Host = {dips.hostname}
        Port = {dips.port}
        Protocol = dips
        Path = /WorkloadManagement/SandboxStore
        Access = remote
      }}
    }}
  }}
}}
{diracx}"""

    # -- DISET services ---------------------------------------------------------------------------

    def _register(self) -> None:
        """Every DISET method the fake answers."""
        rpc = self.diset.rpc
        rpc("Configuration/Server", "getVersion", lambda: self.config_version)
        rpc("Configuration/Server", "getCompressedData", self._compressed)
        rpc("Configuration/Server", "getCompressedDataIfNewer", self._if_newer)
        for method in (
            "exists",
            "isFile",
            "getReplicas",
            "getFileMetadata",
            "listDirectory",
            "getDirectorySize",
            "addFile",
            "removeFile",
        ):
            rpc("DataManagement/FileCatalog", method, getattr(self, f"_fc_{method}"))
        rpc("ResourceStatus/ResourceStatus", "select", self._rss_select)
        rpc("Framework/ProxyManager", "exchangeProxyForToken", self._exchange)
        for method in (
            "submitJob",
            "confirmBulkSubmission",
            "killJob",
            "deleteJob",
            "rescheduleJob",
        ):
            rpc("WorkloadManagement/JobManager", method, getattr(self, f"_jm_{method}"))
        for method in (
            "getJobs",
            "getJobsSummary",
            "getJobParameters",
            "getJobLoggingInfo",
            "getJobJDL",
        ):
            rpc("WorkloadManagement/JobMonitoring", method, getattr(self, f"_mon_{method}"))
        rpc("WorkloadManagement/SandboxStore", "getSandboxesAssignedToEntity", self._assigned)
        for method in (
            "getFilesSummary",
            "getVisibleFilesWithMetadata",
            "getFileMetadata",
            "getProcessingPass",
            "getConfigVersions",
        ):
            rpc("Bookkeeping/BookkeepingManager", method, getattr(self, f"_bk_{method}"))
        self.diset.on_upload = self._uploaded
        self._routes()

    def _compressed(self) -> bytes:
        return zlib.compress(self.configuration().encode())

    def _if_newer(self, version: str) -> dict[str, Any]:
        answer: dict[str, Any] = {"newestVersion": self.config_version}
        if version < self.config_version:
            answer["data"] = self._compressed()
        return answer

    def _bulk(self, lfns: list[str], value: Any) -> dict[str, Any]:
        good = {lfn: value(lfn) for lfn in lfns if lfn in self.files or self._is_dir(lfn)}
        return {
            "Successful": good,
            "Failed": {lfn: "No such file or directory" for lfn in lfns if lfn not in good},
        }

    def _is_dir(self, path: str) -> bool:
        return any(lfn.startswith(path.rstrip("/") + "/") for lfn in self.files)

    def _fc_exists(self, lfns: list[str]) -> dict[str, Any]:
        return self._bulk(lfns, lambda lfn: lfn)

    def _fc_isFile(self, lfns: list[str]) -> dict[str, Any]:
        return self._bulk(lfns, lambda lfn: lfn in self.files)

    def _fc_getReplicas(self, lfns: list[str], _all: bool = False) -> dict[str, Any]:
        found = self._bulk(
            [lfn for lfn in lfns if lfn in self.files], lambda lfn: self.files[lfn]["Replicas"]
        )
        found["Failed"].update(
            {lfn: "No such file or directory" for lfn in lfns if lfn not in self.files}
        )
        return found

    def _fc_getFileMetadata(self, lfns: list[str]) -> dict[str, Any]:
        found = self._bulk(
            [lfn for lfn in lfns if lfn in self.files], lambda lfn: self.files[lfn]["MetaData"]
        )
        found["Failed"].update(
            {lfn: "No such file or directory" for lfn in lfns if lfn not in self.files}
        )
        return found

    def _fc_listDirectory(self, paths: list[str], verbose: bool) -> dict[str, Any]:
        return self._bulk(paths, lambda path: self._listing(path, verbose))

    def _listing(self, path: str, verbose: bool) -> dict[str, Any]:
        """One directory as ``listDirectory`` answers it."""
        prefix = path.rstrip("/") + "/"
        files: dict[str, Any] = {}
        subdirs: dict[str, bool] = {}
        for lfn, entry in self.files.items():
            if not lfn.startswith(prefix):
                continue
            rest = lfn[len(prefix) :]
            if "/" in rest:
                subdirs[prefix + rest.split("/", 1)[0]] = True
            else:
                files[rest] = self._entry(entry) if verbose else True
        return {"Files": files, "SubDirs": subdirs, "Links": {}, "Datasets": {}}

    @staticmethod
    def _entry(entry: dict[str, Any]) -> dict[str, Any]:
        """A file's verbose listing entry: metadata and replicas."""
        replicas = {
            se: {"PFN": pfn, "Status": "AprioriGood"} for se, pfn in entry["Replicas"].items()
        }
        return {"MetaData": entry["MetaData"], "Replicas": replicas}

    def register(self, lfn: str, data: bytes, se: str = "SITE-DISK") -> str:
        """Put ``data`` on ``se`` and register it (a job wrapper's output upload); its URL."""
        from xrdcore.checksum import checksum_bytes

        from ..config import DiracConfig
        from ..storage import resolve

        url = resolve(DiracConfig.from_text(self.configuration()), se).url(lfn) or ""
        self.physical.files[url] = data
        self.add_file(
            lfn, size=len(data), replicas={se: url}, checksum=checksum_bytes("adler32", data)
        )
        return url

    def _fc_addFile(self, entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
        good: dict[str, bool] = {}
        failed: dict[str, str] = {}
        for lfn, entry in entries.items():
            if lfn in self.refuse_register:
                failed[lfn] = self.refuse_register[lfn]
            elif lfn in self.files:
                failed[lfn] = "File already exists"
            else:
                self.add_file(
                    lfn,
                    size=entry["Size"],
                    replicas={entry["SE"]: entry["PFN"]},
                    checksum=entry["Checksum"],
                )
                good[lfn] = True
        return {"Successful": good, "Failed": failed}

    def _fc_removeFile(self, lfns: list[str]) -> dict[str, Any]:
        gone = {lfn: True for lfn in lfns if self.files.pop(lfn, None) is not None}
        return {
            "Successful": gone,
            "Failed": {lfn: "No such file" for lfn in lfns if lfn not in gone},
        }

    def _fc_getDirectorySize(self, paths: list[str]) -> dict[str, Any]:
        def size(path: str) -> dict[str, Any]:
            inside = [e for lfn, e in self.files.items() if lfn.startswith(path.rstrip("/") + "/")]
            return {
                "LogicalSize": sum(e["MetaData"]["Size"] for e in inside),
                "LogicalFiles": len(inside),
            }

        return self._bulk(paths, size)

    def _rss_select(self, table: str, query: dict[str, Any]) -> dict[str, Any]:
        names = query.get("Name") or []
        rows = [
            [
                n,
                query.get("StatusType"),
                self.se_status[n],
                "",
                None,
                None,
                "StorageElement",
                None,
                "rs",
                "all",
            ]
            for n in names
            if n in self.se_status
        ]
        return {"OK": True, "Value": rows, "Columns": ["Name", "StatusType", "Status"]}

    def _exchange(self) -> dict[str, Any]:
        self.tokens_made += 1
        return {
            "access_token": f"fake-token-{self.tokens_made}",
            "expires_in": self.token_lifetime,
            "token_type": "Bearer",
            "refresh_token": "fake-refresh",
        }

    # jobs ---

    def _jm_submitJob(self, text: str) -> dict[str, Any]:
        jdl = JDL.parse(text)
        if "Executable" not in jdl:
            raise Refuse("Invalid job JDL", 1501)
        count = jdl.get("Parameters")
        made = []
        for _ in range(int(count) if isinstance(count, int) else 1):
            job = FakeJob(next(self._ids), jdl)
            job.history.append(
                ("Received", "Job accepted", "Unknown", "2026-01-01 12:00:00", "JobManager")
            )
            job.sandboxes["Input"] = [s for s in jdl.strings("InputSandbox") if s.startswith("SB:")]
            self.jobs[job.job_id] = job
            made.append(job.job_id)
            if self.autorun is not None:
                self.finish(job.job_id, files=self.autorun(job))
        value: Any = made if isinstance(count, int) else made[0]
        answer: dict[str, Any] = {"OK": True, "Value": value}
        if self.require_proxy_upload:
            answer["requireProxyUpload"] = True
        return answer

    def _jm_confirmBulkSubmission(self, ids: list[int]) -> list[int]:
        return ids

    def _manage(self, ids: list[int], change: Any) -> dict[str, Any]:
        invalid = [job for job in ids if job not in self.jobs]
        done = [job for job in ids if job in self.jobs]
        for job in done:
            change(self.jobs[job])
        answer: dict[str, Any] = {"OK": not invalid, "Value": done}
        if invalid:
            answer.update(Message="Some jobs failed", InvalidJobIDs=invalid)
        return answer

    def _jm_killJob(self, ids: list[int], _force: bool = False) -> dict[str, Any]:
        return self._manage(ids, lambda job: setattr(job, "status", "Killed"))

    def _jm_deleteJob(self, ids: list[int], _force: bool = False) -> dict[str, Any]:
        return self._manage(ids, lambda job: setattr(job, "status", "Deleted"))

    def _jm_rescheduleJob(self, ids: list[int]) -> dict[str, Any]:
        return self._manage(ids, lambda job: setattr(job, "status", "Rescheduled"))

    def _mine(self, job: int | str) -> FakeJob:
        found = self.jobs.get(int(job))
        if found is None:
            raise Refuse(f"Job {job} not found")
        return found

    def _mon_getJobs(
        self, conditions: dict[str, Any] | None = None, _cut: str | None = None
    ) -> list[int]:
        def keep(job: FakeJob) -> bool:
            attrs = job.attributes()
            for key, wanted in (conditions or {}).items():
                values = wanted if isinstance(wanted, list) else [wanted]
                if str(attrs.get(key)) not in [str(v) for v in values]:
                    return False
            return True

        return [job.job_id for job in self.jobs.values() if keep(job)]

    def _mon_getJobsSummary(self, ids: list[int]) -> dict[str, Any]:
        return {str(job): self.jobs[int(job)].attributes() for job in ids if int(job) in self.jobs}

    def _mon_getJobParameters(self, ids: list[int]) -> dict[str, Any]:
        return {str(job): self.jobs[int(job)].parameters for job in ids if int(job) in self.jobs}

    def _mon_getJobLoggingInfo(self, job: int) -> list[list[str]]:
        return [list(row) for row in self._mine(job).history]

    def _mon_getJobJDL(self, job: int, _original: bool) -> str:
        return str(self._mine(job).jdl)

    def _assigned(self, entity: str) -> dict[str, list[str]]:
        return self._mine(entity.split(":", 1)[1]).sandboxes

    def _uploaded(self, file_id: Any, _token: str, data: bytes) -> str:
        name = file_id[0] if isinstance(file_id, list) else str(file_id)
        path = f"/SandBox/t/{name}"
        self.sandboxes[path] = data
        self.diset.files[f"{self.diset.url('WorkloadManagement/SandboxStore')}{path}"] = data
        return f"SB:SandboxSE|{path}"

    # bookkeeping ---

    def _dataset(self, query: dict[str, Any]) -> list[str]:
        path = "/".join(
            (
                "",
                query["ConfigName"],
                query["ConfigVersion"],
                query["ConditionDescription"],
                str(query["ProcessingPass"]).strip("/"),
                str(query["EventType"]),
                query["FileType"],
            )
        )
        lfns = self.datasets.get(path)
        if lfns is None:
            return []
        runs = query.get("RunNumber")
        return [lfn for lfn in lfns if not runs or self.bk_metadata[lfn]["Runnumber"] in runs]

    def _bk_getFilesSummary(self, query: dict[str, Any]) -> dict[str, Any]:
        lfns = self._dataset(query)
        meta = [self.bk_metadata[lfn] for lfn in lfns]
        row = [
            len(lfns),
            sum(m["EventStat"] for m in meta),
            sum(m["FileSize"] for m in meta),
            sum(m["Luminosity"] for m in meta),
            0,
        ]
        return {
            "ParameterNames": [
                "NbofFiles",
                "NumberOfEvents",
                "FileSize",
                "Luminosity",
                "InstLuminosity",
            ],
            "Records": [row],
            "TotalRecords": 1,
        }

    def _bk_getVisibleFilesWithMetadata(self, query: dict[str, Any]) -> dict[str, Any]:
        return {"LFNs": {lfn: self.bk_metadata[lfn] for lfn in self._dataset(query)}}

    def _bk_getFileMetadata(self, lfns: list[str]) -> dict[str, Any]:
        return {
            "Successful": {lfn: self.bk_metadata[lfn] for lfn in lfns if lfn in self.bk_metadata},
            "Failed": [lfn for lfn in lfns if lfn not in self.bk_metadata],
        }

    def _bk_getProcessingPass(self, _query: dict[str, Any], _path: str) -> list[dict[str, Any]]:
        return [{"ParameterNames": ["Name"], "Records": [["Real Data"]], "TotalRecords": 1}]

    def _bk_getConfigVersions(self, _query: dict[str, Any]) -> dict[str, Any]:
        return {
            "ParameterNames": ["Name"],
            "Records": [["Collision24"], ["Collision25"]],
            "TotalRecords": 2,
        }

    # -- HTTPS: DiracX, S3, tape REST ----------------------------------------------------------

    def _routes(self) -> None:
        def api(method: str, pattern: str, handler: Callable[[Request], Response]) -> None:
            self.https.route(
                method, pattern, lambda request: self._guard(request) or handler(request)
            )

        api("GET", r"/api/auth/userinfo", self._userinfo)
        api("POST", r"/api/jobs/search", self._search)
        api("POST", r"/api/jobs/summary", self._summary)
        api("POST", r"/api/jobs/jdl", self._submit_jdl)
        api("PATCH", r"/api/jobs/status", self._set_status)
        api("POST", r"/api/jobs/reschedule", self._reschedule)
        api("POST", r"/api/jobs/sandbox", self._sandbox_init)
        api("GET", r"/api/jobs/sandbox", self._sandbox_url)
        api("GET", r"/api/jobs/(?P<job>\d+)/sandbox", self._job_sandboxes)
        self.https.route("POST", r"/s3/upload", self._s3_upload)
        self.https.route("GET", r"/s3/(?P<path>.+)", self._s3_download)
        self.tape_https.route("GET", r"/.well-known/wlcg-tape-rest-api", self._tape_discovery)
        self.tape_https.route("POST", r"/api/v1/archiveinfo", self._archiveinfo)

    def _authorised(self, request: Request) -> bool:
        return (request.header("Authorization") or "").startswith("Bearer fake-token-")

    @staticmethod
    def _json(value: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
        return Response(
            status,
            json.dumps(value, default=str).encode(),
            {"Content-Type": "application/json", **(headers or {})},
        )

    def _guard(self, request: Request) -> Response | None:
        if not self._authorised(request):
            return self._json({"detail": "Not authenticated"}, 401)
        return None

    def _userinfo(self, request: Request) -> Response:
        return self._json(
            {
                "sub": f"{VO}:x",
                "vo": VO,
                "dirac_group": GROUP,
                "properties": ["NormalUser"],
                "preferred_username": USER,
            }
        )

    def _matches(self, job: dict[str, Any], spec: dict[str, Any]) -> bool:
        value = str(job.get(spec["parameter"]))
        operator = spec["operator"]
        if operator in ("in", "not in"):
            inside = value in [str(v) for v in spec["values"]]
            return inside if operator == "in" else not inside
        wanted = str(spec["value"])
        if operator == "like":
            pattern = ".*".join(re.escape(part) for part in wanted.split("%"))
            return re.fullmatch(pattern, value) is not None
        if operator == "gt":
            return value > wanted
        return value == wanted if operator == "eq" else value != wanted

    def _search(self, request: Request) -> Response:
        body = json.loads(request.body or b"{}")
        jobs = [j.attributes() for j in self.jobs.values()]
        jobs = [j for j in jobs if all(self._matches(j, spec) for spec in body.get("search", []))]
        for spec in body.get("sort", []):
            jobs.sort(key=lambda j: j[spec["parameter"]], reverse=spec["direction"] == "desc")
        params = parse_qs(urlsplit(request.target).query)
        page, per_page = int(params.get("page", ["1"])[0]), int(params.get("per_page", ["100"])[0])
        chunk = jobs[(page - 1) * per_page : page * per_page]
        wanted = body.get("parameters")
        chunk = [{k: v for k, v in j.items() if not wanted or k in wanted} for j in chunk]
        if len(jobs) > per_page:
            first = (page - 1) * per_page
            headers = {"Content-Range": f"jobs {first}-{first + len(chunk) - 1}/{len(jobs)}"}
            return self._json(chunk, 206, headers)
        return self._json(chunk)

    def _summary(self, request: Request) -> Response:
        grouping = json.loads(request.body)["grouping"]
        counts: dict[tuple[Any, ...], int] = {}
        for job in self.jobs.values():
            key = tuple(job.attributes()[g] for g in grouping)
            counts[key] = counts.get(key, 0) + 1
        return self._json(
            [{**dict(zip(grouping, k, strict=True)), "count": n} for k, n in counts.items()]
        )

    def _submit_jdl(self, request: Request) -> Response:
        made = []
        for text in json.loads(request.body):
            job_id = self._jm_submitJob(text)["Value"]
            made.append(
                {
                    "JobID": job_id,
                    "Status": "Received",
                    "MinorStatus": "Job accepted",
                    "TimeStamp": "2026-01-01T12:00:00",
                }
            )
        return self._json(made)

    def _set_status(self, request: Request) -> Response:
        success, failed = {}, {}
        for job, changes in json.loads(request.body).items():
            if int(job) not in self.jobs:
                failed[job] = {"detail": "not found"}
                continue
            for change in changes.values():
                self.jobs[int(job)].status = change["Status"]
            success[job] = {"Status": self.jobs[int(job)].status}
        return self._json({"success": success, "failed": failed})

    def _reschedule(self, request: Request) -> Response:
        ids = json.loads(request.body)["job_ids"]
        return self._json(self._jm_rescheduleJob([i for i in ids if i in self.jobs]))

    def _sandbox_init(self, request: Request) -> Response:
        info = json.loads(request.body)
        path = f"/S3/sandboxes/{VO}/{GROUP}/{USER}/sha256:{info['checksum']}.{info['format']}"
        if path in self.sandboxes:
            return self._json({"pfn": path, "url": None, "fields": {}})
        return self._json(
            {
                "pfn": path,
                "url": f"{self.https_url}/s3/upload",
                "fields": {"key": path, "policy": "p"},
            }
        )

    def _s3_upload(self, request: Request) -> Response:
        content_type = request.header("Content-Type") or ""
        boundary = content_type.partition("boundary=")[2].encode()
        parts = request.body.split(b"--" + boundary)
        fields: dict[str, bytes] = {}
        for part in parts:
            head, _, data = part.partition(b"\r\n\r\n")
            name = head.partition(b'name="')[2].partition(b'"')[0].decode()
            if name:
                fields[name] = data[:-2] if data.endswith(b"\r\n") else data
        self.sandboxes[fields["key"].decode()] = fields["file"]
        return Response(204)

    def _sandbox_url(self, request: Request) -> Response:
        pfn = request.param("pfn")
        if pfn not in self.sandboxes:
            return self._json({"detail": "no such sandbox"}, 404)
        return self._json({"url": f"{self.https_url}/s3{pfn}", "expires_in": 300})

    def _s3_download(self, request: Request) -> Response:
        data = self.sandboxes.get("/" + request.params["path"])
        return Response(200, data) if data is not None else Response(404)

    def _job_sandboxes(self, request: Request) -> Response:
        job = self.jobs.get(int(request.params["job"]))
        if job is None:
            return self._json({"detail": "not found"}, 404)
        return self._json({kind.lower(): pfns for kind, pfns in job.sandboxes.items()})

    def _tape_discovery(self, _request: Request) -> Response:
        return self._json(
            {"endpoints": [{"uri": f"{self.tape_url}/api/v1", "version": "v1"}], "sitename": "FAKE"}
        )

    def _archiveinfo(self, request: Request) -> Response:
        answers = []
        for path in json.loads(request.body)["paths"]:
            locality = self.tape.get(path)
            if locality is None:
                answers.append({"path": path, "error": "USER ERROR: file does not exist"})
            else:
                answers.append({"path": path, "locality": locality})
        return self._json(answers)


#: For handlers that refuse the caller.
NOT_ALLOWED = Refuse("Unauthorized query", ENOAUTH)
