"""DIRAC jobs: submit, find, monitor, kill, delete, reschedule, fetch their output.

The same calls work on DIRAC 8 (GridPP) and DIRAC 9 (LHCb), choosing per
operation what the installation offers, as DIRAC 9's own client does:

==================  ====================================  ==================================
operation           DIRAC 9 with DiracX (LHCb)            DIRAC 8, DISET only (GridPP)
==================  ====================================  ==================================
submit              sandbox to DiracX's S3 store;         sandbox to ``SandboxStore``;
                    JDL to ``JobManager.submitJob``       JDL to ``JobManager.submitJob``
status, list        DiracX ``jobs/search`` (bulk, paged)  ``JobMonitoring.getJobsSummary``
kill/delete/resched ``JobManager`` (it also clears task   the same
                    queues and pilots, which a status
                    change alone would not)
output              DiracX presigned S3 download          ``SandboxStore`` file transfer
parameters, log     ``JobMonitoring``                     the same
==================  ====================================  ==================================

Job IDs are DIRAC's integers. A parametric JDL (``Parameters = 10``) makes
several jobs from one submission; :meth:`JobClient.submit` confirms them in
bulk, as ``dirac-wms-job-submit`` does, and answers every ID.

DIRAC runs a job with the owner's proxy, which its pilots fetch from the
``ProxyManager``: a user who has never uploaded one (``lhcb-proxy-init`` or
``dirac-proxy-init`` does) gets jobs that wait. :meth:`JobClient.submit`
reports the service's ``requireProxyUpload`` flag as a warning rather than
uploading anything itself.
"""

from __future__ import annotations

import ast
import datetime as _dt
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xrdcore.log import get_logger

from .diset import DisetClient, unwrap
from .errors import ConfigError, DiracError, ServiceError, WireError
from .jdl import JDL
from .sandbox import Packed, pack, unpack

if TYPE_CHECKING:
    from .session import DiracSession

__all__ = ["DiracJob", "JobClient", "OpResult", "SubmitResult"]

_log = get_logger(__name__)

JOB_MANAGER = "WorkloadManagement/JobManager"
JOB_MONITORING = "WorkloadManagement/JobMonitoring"
SANDBOX_STORE = "WorkloadManagement/SandboxStore"

#: The job attributes asked of DiracX's search, and read from either path.
ATTRIBUTES = (
    "JobID",
    "JobName",
    "JobGroup",
    "Owner",
    "OwnerGroup",
    "Status",
    "MinorStatus",
    "ApplicationStatus",
    "Site",
    "SubmissionTime",
    "StartExecTime",
    "EndExecTime",
    "LastUpdateTime",
)

#: DIRAC's final states.
FINAL = frozenset({"Done", "Completed", "Failed", "Killed", "Deleted"})


@dataclass(frozen=True, slots=True)
class DiracJob:
    """A job's attributes, the same whichever path they came by."""

    job_id: int
    status: str = ""
    minor_status: str = ""
    application_status: str = ""
    site: str = ""
    name: str = ""
    group: str = ""
    owner: str = ""
    submitted: _dt.datetime | None = None
    started: _dt.datetime | None = None
    ended: _dt.datetime | None = None
    updated: _dt.datetime | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def final(self) -> bool:
        """The job will not change again."""
        return self.status in FINAL

    @classmethod
    def read(cls, raw: Mapping[str, Any], job_id: int | None = None) -> DiracJob:
        """From DiracX's or JobMonitoring's attribute dict (times as text or datetimes)."""
        return cls(
            job_id=int(job_id if job_id is not None else raw.get("JobID", 0)),
            status=str(raw.get("Status") or ""),
            minor_status=str(raw.get("MinorStatus") or ""),
            application_status=str(raw.get("ApplicationStatus") or ""),
            site=str(raw.get("Site") or ""),
            name=str(raw.get("JobName") or ""),
            group=str(raw.get("JobGroup") or ""),
            owner=str(raw.get("Owner") or ""),
            submitted=_when(raw.get("SubmissionTime")),
            started=_when(raw.get("StartExecTime")),
            ended=_when(raw.get("EndExecTime")),
            updated=_when(raw.get("LastUpdateTime")),
            attributes=dict(raw),
        )


def _when(value: Any) -> _dt.datetime | None:
    """A DIRAC time: a datetime already (DISET), ISO text (DiracX), ``None``/"None"."""
    if isinstance(value, _dt.datetime):
        return value
    if not isinstance(value, str) or value in ("", "None"):
        return None
    try:
        moment = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=None) if moment.tzinfo else moment


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """A submission: the job IDs made, and whether DIRAC wants the user's proxy uploaded."""

    job_ids: tuple[int, ...]
    require_proxy_upload: bool = False
    sandbox: str = ""


@dataclass(frozen=True, slots=True)
class OpResult:
    """What a kill, delete or reschedule did to one job."""

    job_id: int
    ok: bool
    reason: str = ""


class JobClient:
    """Jobs on one installation; see the module docstring."""

    def __init__(self, session: DiracSession, *, chunk: int = 1000) -> None:
        self.session = session
        self.chunk = chunk

    @property
    def _use_diracx(self) -> bool:
        """Whether this installation's DiracX is used."""
        return bool(self.session.diracx_url)

    def _service(self, name: str) -> DisetClient:
        return self.session.service(name)

    # -- submit ------------------------------------------------------------------------------

    def submit(
        self,
        jdl: str | JDL,
        *,
        sandbox: Iterable[str | Path] = (),
        extra: Mapping[str, bytes] | None = None,
    ) -> SubmitResult:
        """Submit one job (or one parametric set): upload its sandbox, send its JDL.

        ``sandbox`` are local files and directories for the input sandbox,
        ``extra`` in-memory ones (``{"data.py": b"..."}``); both are packed
        into one ``tar.bz2`` added to the JDL's ``InputSandbox``. Local
        names already in the JDL's ``InputSandbox`` are packed too, as DIRAC's
        client does; ``LFN:`` and ``SB:`` entries are left for DIRAC.
        """
        description = jdl if isinstance(jdl, JDL) else JDL.parse(jdl)
        local = [entry for entry in description.strings("InputSandbox") if not _remote(entry)]
        files = [*map(str, sandbox), *local]
        location = ""
        if files or extra:
            location = self.upload_sandbox(pack(files, extra=dict(extra or {})))
            kept = [entry for entry in description.strings("InputSandbox") if _remote(entry)]
            description["InputSandbox"] = [*kept, location]
        answer = self._service(JOB_MANAGER).request("submitJob", str(description))
        value = unwrap(answer, JOB_MANAGER, "submitJob")
        ids = tuple(int(job) for job in (value if isinstance(value, list) else [value]))
        if len(ids) > 1 or "Parameters" in description:
            self._service(JOB_MANAGER).call("confirmBulkSubmission", list(ids))
        upload = bool(answer.get("requireProxyUpload"))
        if upload:
            _log.warning(
                "DIRAC has no proxy of yours to run these jobs with: run lhcb-proxy-init "
                "(or dirac-proxy-init) once, or they will wait"
            )
        return SubmitResult(ids, upload, location)

    def upload_sandbox(self, packed: Packed) -> str:
        """Store an input sandbox; its ``SB:<SE>|<path>`` for the JDL.

        Through DiracX where the installation's sandbox store is DiracX's,
        else through the DISET ``SandboxStore``'s file transfer.
        """
        if self._use_diracx:
            return self.session.diracx.upload_sandbox(packed)
        store = self._service(SANDBOX_STORE)
        location = store.send_file(
            [f"{packed.md5}.{packed.format}", {}], BytesIO(packed.data), packed.size
        )
        if not isinstance(location, str):
            raise WireError("The SandboxStore did not answer the sandbox's location")
        return location

    # -- find and monitor ----------------------------------------------------------------------

    def status(self, job_ids: Iterable[int]) -> dict[int, DiracJob]:
        """Each job's attributes, by ID; IDs DIRAC does not know (or not for you) are left out."""
        ids = sorted({int(job) for job in job_ids})
        found: dict[int, DiracJob] = {}
        for start in range(0, len(ids), self.chunk):
            part = ids[start : start + self.chunk]
            found.update(
                self._diracx_status(part) if self._use_diracx else self._diset_status(part)
            )
        return found

    def _diracx_status(self, ids: Sequence[int]) -> dict[int, DiracJob]:
        """One DiracX search for ``ids``."""
        spec = [{"parameter": "JobID", "operator": "in", "values": list(ids)}]
        jobs = self.session.diracx.search(spec, parameters=ATTRIBUTES)
        return {int(job["JobID"]): DiracJob.read(job) for job in jobs if "JobID" in job}

    def _diset_status(self, ids: Sequence[int]) -> dict[int, DiracJob]:
        """``JobMonitoring.getJobsSummary`` for ``ids`` (keys are text under JSON)."""
        answer = self._service(JOB_MONITORING).call("getJobsSummary", list(ids))
        table = _summary_table(answer)
        return {
            int(job): DiracJob.read(raw, int(job))
            for job, raw in table.items()
            if isinstance(raw, dict)
        }

    def find(
        self,
        *,
        status: Sequence[str] = (),
        name: str | None = None,
        group: str | None = None,
        site: str | None = None,
        since: _dt.datetime | None = None,
        limit: int = 1000,
    ) -> list[DiracJob]:
        """Your jobs matching every condition given (``name`` may hold ``%`` wildcards)."""
        if self._use_diracx:
            return self._diracx_find(status, name, group, site, since, limit)
        return self._diset_find(status, name, group, site, since, limit)

    def _diset_find(
        self,
        status: Sequence[str],
        name: str | None,
        group: str | None,
        site: str | None,
        since: _dt.datetime | None,
        limit: int,
    ) -> list[DiracJob]:
        """``JobMonitoring.getJobs``, which matches names exactly: a wildcard is matched here."""
        pattern = _like(name) if name and "%" in name else None
        conditions: dict[str, Any] = {"Owner": self.username()}
        wanted = (
            ("Status", list(status)),
            ("JobName", None if pattern else name),
            ("JobGroup", group),
        )
        conditions.update({key: value for key, value in (*wanted, ("Site", site)) if value})
        cut = since.strftime("%Y-%m-%d %H:%M:%S") if since else None
        ids = self._service(JOB_MONITORING).call("getJobs", conditions, cut)
        newest = sorted((int(job) for job in ids or []), reverse=True)
        chosen = newest if pattern is not None else newest[:limit]
        known = self.status(chosen)
        found = [known[job] for job in chosen if job in known]
        if pattern is not None:
            found = [job for job in found if pattern.fullmatch(job.name)]
        return found[:limit]

    def _diracx_find(
        self,
        status: Sequence[str],
        name: str | None,
        group: str | None,
        site: str | None,
        since: _dt.datetime | None,
        limit: int,
    ) -> list[DiracJob]:
        """The same conditions as DiracX search specs."""
        specs: list[dict[str, Any]] = []
        if status:
            specs.append({"parameter": "Status", "operator": "in", "values": list(status)})
        for key, value in (("JobName", name), ("JobGroup", group), ("Site", site)):
            if value:
                operator = "like" if "%" in value else "eq"
                specs.append({"parameter": key, "operator": operator, "value": value})
        if since is not None:
            specs.append(
                {"parameter": "SubmissionTime", "operator": "gt", "value": since.isoformat()}
            )
        sort = [{"parameter": "JobID", "direction": "desc"}]
        jobs = self.session.diracx.search(specs, parameters=ATTRIBUTES, sort=sort, limit=limit)
        return [DiracJob.read(job) for job in jobs]

    def summary(self, grouping: Sequence[str] = ("Status",)) -> list[dict[str, Any]]:
        """Your jobs counted by ``grouping`` (DiracX), or by status from your job list (DISET)."""
        if self._use_diracx:
            return self.session.diracx.summary(grouping)
        counts: dict[tuple[Any, ...], int] = {}
        for job in self.find(limit=100000):
            key = tuple(job.attributes.get(g, "") for g in grouping)
            counts[key] = counts.get(key, 0) + 1
        return [
            {**dict(zip(grouping, key, strict=True)), "count": n}
            for key, n in sorted(counts.items())
        ]

    def username(self) -> str:
        """Your DIRAC user name: DiracX's word for it, else the registry's for your DN."""
        if self._use_diracx:
            return str(self.session.diracx.whoami().get("preferred_username", ""))
        identity = self.session.proxy_info().identity
        registry = self.session.config.root.section("Registry/Users")
        for name, user in registry.sections.items() if registry is not None else []:
            if identity in [dn.strip() for dn in user.options.get("DN", "").split(",")]:
                return name
        raise ConfigError(f"The DIRAC registry has no user with the DN {identity}")

    def parameters(self, job_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """Each job's parameters (what its wrapper reported: CPU, node, wall time...)."""
        answer = self._service(JOB_MONITORING).call("getJobParameters", [int(j) for j in job_ids])
        return {int(job): dict(values) for job, values in dict(answer or {}).items()}

    def logging(self, job_id: int) -> list[tuple[str, str, str, str, str]]:
        """The job's history: ``(status, minor, application, time, source)`` per change."""
        rows = self._service(JOB_MONITORING).call("getJobLoggingInfo", int(job_id))
        history = []
        for row in rows or []:
            cells = [str(cell) for cell in row] + [""] * 5
            history.append((cells[0], cells[1], cells[2], cells[3], cells[4]))
        return history

    def jdl(self, job_id: int, *, original: bool = False) -> str:
        """The JDL DIRAC holds for the job (``original``: as submitted)."""
        return str(self._service(JOB_MONITORING).call("getJobJDL", int(job_id), original))

    # -- manage --------------------------------------------------------------------------------

    def kill(self, job_ids: Iterable[int], *, force: bool = False) -> list[OpResult]:
        """Kill the jobs (running ones are stopped; waiting ones never start)."""
        return self._manage("killJob", job_ids, force)

    def delete(self, job_ids: Iterable[int], *, force: bool = False) -> list[OpResult]:
        """Delete the jobs (they are killed first if still alive, then hidden)."""
        return self._manage("deleteJob", job_ids, force)

    def reschedule(self, job_ids: Iterable[int]) -> list[OpResult]:
        """Send failed or stalled jobs back to the queue."""
        return self._manage("rescheduleJob", job_ids)

    def _manage(self, method: str, job_ids: Iterable[int], *flags: bool) -> list[OpResult]:
        """One JobManager call; per job, from its valid/invalid/unauthorised/failed lists."""
        ids = [int(job) for job in job_ids]
        if not ids:
            return []
        answer = self._service(JOB_MANAGER).request(method, ids, *flags)
        problems: dict[int, str] = {}
        for key, reason in (
            ("InvalidJobIDs", "no such job"),
            ("NonauthorizedJobIDs", "not allowed"),
            ("FailedJobIDs", "failed"),
        ):
            for job in answer.get(key) or []:
                problems[int(job)] = reason
        if not answer.get("OK") and not problems:
            message = str(answer.get("Message", "failed"))
            return [OpResult(job, False, message) for job in ids]
        return [OpResult(job, job not in problems, problems.get(job, "")) for job in ids]

    # -- output --------------------------------------------------------------------------------

    def sandboxes(self, job_id: int) -> dict[str, list[str]]:
        """The job's sandbox locations by kind (``Input``, ``Output``)."""
        if self._use_diracx:
            found = self.session.diracx.job_sandboxes(job_id)
        else:
            found = self._service(SANDBOX_STORE).call(
                "getSandboxesAssignedToEntity", f"Job:{int(job_id)}"
            )
        return {
            str(kind).capitalize(): [str(p) for p in pfns]
            for kind, pfns in dict(found or {}).items()
        }

    def output(self, job_id: int, directory: str | Path, *, kind: str = "Output") -> list[str]:
        """Download and unpack the job's ``kind`` sandbox(es) into ``directory``; the files.

        :class:`~xrddirac.errors.DiracError` when the job has none (not
        finished, or its output sandbox expired).
        """
        locations = self.sandboxes(job_id).get(kind.capitalize(), [])
        if not locations:
            raise DiracError(
                f"Job {job_id} has no {kind.lower()} sandbox (not finished, or expired)"
            )
        files: list[str] = []
        for location in locations:
            files += unpack(self._download(location), directory)
        return sorted(set(files))

    def _download(self, location: str) -> bytes:
        """One sandbox's bytes: DiracX's S3 store (``/S3/`` paths) or the DISET store."""
        se_name, _, path = location.removeprefix("SB:").partition("|")
        if path.startswith("/S3/"):
            return self.session.diracx.download_sandbox(location)
        endpoint = self.session.storage(se_name).endpoint("dips")
        if endpoint is None:
            raise ConfigError(f"Sandbox SE {se_name} has no dips:// endpoint")
        store = DisetClient(
            [f"{endpoint.base}{endpoint.path}"],
            tls=self.session.tls,
            vo=self.session.vo,
            setup=self.session.profile.setup,
        )
        sink = BytesIO()
        store.receive_file(endpoint.url(path), sink, limit=1 << 30)
        return sink.getvalue()


def _like(pattern: str) -> re.Pattern[str]:
    """An SQL ``LIKE`` pattern (``%``: any run of characters) as a regular expression."""
    return re.compile(".*".join(re.escape(part) for part in pattern.split("%")))


def _remote(entry: str) -> bool:
    """An InputSandbox entry DIRAC resolves itself (an LFN, a stored sandbox, a template)."""
    return entry[:4].upper() == "LFN:" or entry.startswith(("SB:", "%s", "%("))


def _summary_table(answer: Any) -> dict[Any, Any]:
    """``getJobsSummary``'s table: a dict, or (very old DIRAC) its ``repr`` text."""
    if isinstance(answer, dict):
        return answer
    if isinstance(answer, str):
        try:
            value = ast.literal_eval(answer)
        except (ValueError, SyntaxError) as exc:
            raise WireError("getJobsSummary answered text that is not a plain table") from exc
        if isinstance(value, dict):
            return value
    raise ServiceError(
        "getJobsSummary answered neither a table nor its text", service=JOB_MONITORING
    )
