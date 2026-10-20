"""The xrdwms backend for DIRAC: grid jobs as dask tasks, on LHCb's or GridPP's DIRAC.

Registered as the ``dirac`` kind through xrdwms's ``xrdwms.backends`` entry
point, so xrdwms finds it without knowing about it::

    # ~/.config/xrdwms/config.toml
    [backends.lhcb]
    kind = "dirac"
    profile = "lhcb"               # an xrddirac profile
    sites = ["LCG.CERN.cern"]      # optional default placement

    $ xrdwms submit -b lhcb -t 1h -o out.txt -- /bin/sh -c 'hostname > out.txt'

or from Python, ``xrdwms.backends.create("dirac", "lhcb", profile="lhcb")``.

:func:`to_jdl` writes a :class:`xrdwms.JobSpec` as DIRAC's JDL and raises
:class:`xrdwms.errors.UnsupportedError` for what DIRAC cannot express:

* executable, arguments, environment (``ExecutionEnvironment``);
* local inputs go in the input sandbox; ``lfn:`` / ``LFN:`` inputs become
  ``InputData`` (DIRAC places the job where they are and downloads them);
  any other URL is refused - DIRAC jobs read data by LFN;
* sandbox outputs go in ``OutputSandbox``; an output whose destination is
  ``lfn:/path`` is ``OutputData`` (uploaded and registered by the job, to
  ``native["dirac"]["OutputSE"]`` or the VO's default);
* cores (``NumberOfProcessors``), CPU time (``CPUTime``), wall time (as
  ``CPUTime`` when no CPU time is given), platform (``Platform``), sites,
  banned sites and tags; memory as a ``<N>GB`` tag, DIRAC's convention;
* ``native["dirac"]`` attributes last, replacing any of the same name.

The idempotency key travels in the job name (``<spec name>~<key>``) as with
ARC, and :meth:`DiracBackend.find` searches your jobs by it after a crash.
Jobs are watched in bulk (one DiracX search, or one ``getJobsSummary``, per
round), cancelled with ``killJob``, cleaned with ``deleteJob`` and their
output sandboxes downloaded by :meth:`DiracBackend.retrieve`.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from xrdcore.errors import CredentialError, XrdError
from xrdwms.backends import SyncBackend
from xrdwms.errors import UnsupportedError
from xrdwms.model import (
    Capabilities,
    Failure,
    FailureKind,
    JobHandle,
    JobInfo,
    JobSpec,
    JobState,
    KeyedSpec,
    OpResult,
    Retrieved,
    SubmitResult,
)

from .errors import AuthorizationError, DiracError
from .jdl import JDL, lfn_list, shell_join
from .jobs import DiracJob
from .jobs import OpResult as DiracOp
from .lfnstore import LFNStore, PhysicalStorage, is_lfn
from .session import DiracSession

__all__ = ["STDERR", "STDOUT", "DiracBackend", "classify", "job_name", "key_of", "to_jdl"]

STDOUT, STDERR = "std.out", "std.err"
_KEY_MARK = "~"

#: DIRAC's statuses, as xrdwms states (Done, Failed, Killed and Deleted are decided per job).
_STATES = {
    "Submitting": JobState.SUBMITTING,
    "Received": JobState.QUEUED,
    "Checking": JobState.QUEUED,
    "Staging": JobState.STAGING_IN,
    "Waiting": JobState.QUEUED,
    "Matched": JobState.QUEUED,
    "Rescheduled": JobState.QUEUED,
    "Running": JobState.RUNNING,
    # A job DIRAC has not heard from: it may yet recover (or be failed by DIRAC).
    "Stalled": JobState.HELD,
    "Completing": JobState.FINISHING,
}

#: Words in DIRAC's minor/application status, and the failure they mean; first match wins.
_CLASSES: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (FailureKind.AUTH, ("proxy", "credential", "authoriz", "authentic")),
    (FailureKind.LIMIT, ("cpu time", "cputime", "wall clock", "memory", "watchdog", "disk space")),
    (
        FailureKind.DATA,
        ("input data", "inputdata", "download", "upload", "output data", "staging", "resolution"),
    ),
    (FailureKind.INFRA, ("stalled", "lost", "pilot", "matcher", "node", "heartbeat")),
)
_EXIT = re.compile(r"(?:exit(?:ed)?(?: with)?(?: status| code)?)\s*[:=]?\s*(-?\d+)", re.IGNORECASE)


# -- the translator --------------------------------------------------------------------------


def job_name(spec: JobSpec, key: str) -> str:
    """The DIRAC job name for ``spec`` submitted under ``key``."""
    return f"{spec.name}{_KEY_MARK}{key}"


def key_of(name: str | None) -> str | None:
    """The idempotency key in a DIRAC job name, or ``None`` for one xrdwms did not name."""
    if not name or _KEY_MARK not in name:
        return None
    return name.rsplit(_KEY_MARK, 1)[1] or None


def _is_lfn(text: str) -> bool:
    """``lfn:/...`` or ``LFN:/...``."""
    return text[:4].lower() == "lfn:"


def to_jdl(
    spec: JobSpec, key: str, *, defaults: Mapping[str, Any] | None = None
) -> tuple[JDL, list[str]]:
    """``spec`` as a DIRAC JDL, and the local files for its input sandbox.

    ``defaults`` are the backend's own JDL attributes (its default sites),
    which the spec's placement and ``native["dirac"]`` override.
    """
    jdl = JDL({"JobName": job_name(spec, key), "Executable": spec.executable})
    if spec.arguments:
        jdl["Arguments"] = shell_join(spec.arguments)
    jdl["StdOutput"], jdl["StdError"] = STDOUT, STDERR
    jdl["JobGroup"] = spec.labels.get("group", "xrdwms")
    for name, value in (defaults or {}).items():
        jdl[name] = value
    local = _inputs(spec, jdl)
    _outputs(spec, jdl)
    if spec.environment:
        jdl["ExecutionEnvironment"] = [f"{k}={v}" for k, v in spec.environment.items()]
    _resources(spec, jdl)
    _runtime_and_placement(spec, jdl)
    for name, value in spec.native.get("dirac", {}).items():
        jdl[name] = list(value) if isinstance(value, list | tuple) else value
    return jdl, local


def _inputs(spec: JobSpec, jdl: JDL) -> list[str]:
    """LFN and local inputs to the input sandbox (DIRAC fetches ``LFN:`` ones); other URLs refused.

    The local files are returned for packing; the LFNs go in the JDL now.
    Either kind lands in the job's directory under its own name, so a
    different ``Input.name`` cannot be honoured.
    """
    local: list[str] = []
    lfns: list[str] = []
    for item in spec.inputs:
        if not _is_lfn(item.source) and item.is_url:
            raise UnsupportedError(
                "dirac", "inputs", f"{item.source}: DIRAC jobs read data by LFN (lfn:/...)"
            )
        path = item.source[4:] if _is_lfn(item.source) else item.source
        if item.name != Path(path).name:
            raise UnsupportedError(
                "dirac", "inputs", f"{item.source}: a sandbox file keeps its own name"
            )
        (lfns if _is_lfn(item.source) else local).append(path)
    if lfns:
        jdl["InputSandbox"] = lfn_list(lfns)
    return local


def _outputs(spec: JobSpec, jdl: JDL) -> None:
    """Sandbox outputs (and the streams) to ``OutputSandbox``; LFN outputs to ``OutputData``."""
    sandbox = [STDOUT, STDERR]
    data: list[str] = []
    for output in spec.outputs:
        if output.in_sandbox:
            sandbox.append(output.name)
        elif _is_lfn(output.destination):
            if Path(output.destination[4:]).name != output.name:
                raise UnsupportedError(
                    "dirac", "outputs", f"{output.destination}: an LFN output keeps the file's name"
                )
            data.append(output.destination)
        else:
            raise UnsupportedError(
                "dirac", "outputs", f"{output.destination}: DIRAC uploads outputs to LFNs only"
            )
    jdl["OutputSandbox"] = list(dict.fromkeys(sandbox))
    if data:
        jdl["OutputData"] = lfn_list([d[4:] for d in data])


def _resources(spec: JobSpec, jdl: JDL) -> None:
    """Cores, CPU or wall time, memory (as a tag); GPUs and disk are not JDL."""
    res = spec.resources
    if res.gpus:
        raise UnsupportedError("dirac", "resources.gpus", "use a GPU tag in placement.tags")
    if res.disk_mb is not None:
        raise UnsupportedError("dirac", "resources.disk_mb", "DIRAC's JDL has no disk request")
    if res.cores > 1:
        jdl["NumberOfProcessors"] = res.cores
    seconds = res.cputime_s if res.cputime_s is not None else res.walltime_s
    if seconds is not None:
        jdl["CPUTime"] = seconds
    if res.memory_mb is not None:
        jdl.append("Tags", f"{math.ceil(res.memory_mb / 1024)}GB")


def _runtime_and_placement(spec: JobSpec, jdl: JDL) -> None:
    """Platform, sites, banned sites, tags; containers and runtime environments are not JDL."""
    if spec.runtime.container:
        raise UnsupportedError("dirac", "runtime.container", "run it with apptainer in the job")
    if spec.runtime.environments:
        raise UnsupportedError("dirac", "runtime.environments", "use placement.tags")
    if spec.runtime.platform:
        jdl["Platform"] = spec.runtime.platform
    placement = spec.placement
    if placement.queue:
        raise UnsupportedError("dirac", "placement.queue", "DIRAC matches jobs to queues itself")
    if placement.sites:
        jdl["Site"] = list(placement.sites)
    if placement.banned_sites:
        jdl["BannedSites"] = list(placement.banned_sites)
    if placement.tags:
        jdl.append("Tags", *placement.tags)


# -- states --------------------------------------------------------------------------------


def classify(*texts: str) -> FailureKind:
    """The kind of a DIRAC failure, from its minor and application statuses."""
    text = " ".join(texts).lower()
    for kind, words in _CLASSES:
        if any(word in text for word in words):
            return kind
    return FailureKind.PAYLOAD if "application" in text or _exit_code(text) else FailureKind.INFRA


def _exit_code(text: str) -> int | None:
    """An exit code stated in a status text (``... exited with status 2``)."""
    match = _EXIT.search(text)
    return int(match.group(1)) if match else None


def _state(job: DiracJob) -> tuple[JobState, Failure | None, int | None]:
    """The xrdwms state of a DIRAC job, its failure, and its exit code when one is stated."""
    code = _exit_code(job.application_status)
    if job.status in ("Done", "Completed"):
        return JobState.DONE, None, code if code is not None else 0
    if job.status == "Failed":
        why = (
            "; ".join(t for t in (job.minor_status, job.application_status) if t)
            or "no reason given"
        )
        return (
            JobState.FAILED,
            Failure(classify(job.minor_status, job.application_status), why),
            code,
        )
    if job.status in ("Killed", "Deleted"):
        return JobState.CANCELLED, Failure(FailureKind.INFRA, job.minor_status or job.status), code
    return _STATES.get(job.status, JobState.UNKNOWN), None, None


def _stamp(job: DiracJob, name: str) -> float | None:
    """A job time as epoch seconds (DIRAC's times are UTC)."""
    moment = getattr(job, name)
    if moment is None:
        return None
    return float(moment.replace(tzinfo=_dt.UTC).timestamp())


def _grid_storage() -> PhysicalStorage:
    """xrdwms's xgfalclient storage (every scheme the sites run), for LFN files' bytes."""
    from xrdwms.staging import GridStorage

    storage: PhysicalStorage = GridStorage()
    return storage


# -- the backend -------------------------------------------------------------------------------


class DiracBackend(SyncBackend):
    """Jobs on one DIRAC installation; see the module docstring.

    ``profile`` is an xrddirac profile (``lhcb``, ``gridpp``...). ``sites``,
    ``banned_sites``, ``platform`` and ``jdl`` are defaults for every job
    (``jdl``: any JDL attributes). ``workdir`` is where relative sandbox
    paths are read from. ``session`` replaces the :class:`DiracSession`
    built from all of these (tests, scripts that have one).
    """

    kind = "dirac"
    capabilities = Capabilities(
        max_bulk=100,
        # DIRAC's default input sandbox limit is 10 MB; bigger inputs belong on storage.
        sandbox_limit=10 * 1024 * 1024,
        data_staging=True,
        proxy_renewal=False,
        x509_auth=True,
        token_auth=False,
        find=True,
    )

    def __init__(
        self,
        name: str = "dirac",
        *,
        profile: str | None = None,
        sites: Sequence[str] = (),
        banned_sites: Sequence[str] = (),
        platform: str | None = None,
        jdl: Mapping[str, Any] | None = None,
        workdir: str | None = None,
        upload_se: str | None = None,
        verify: Literal["catalog", "replica"] = "catalog",
        output_grace: float = 1800.0,
        sandbox_limit: int = 10 * 1024 * 1024,
        session: DiracSession | None = None,
        physical: PhysicalStorage | None = None,
    ) -> None:
        super().__init__(name)
        self.profile = profile
        self.workdir = workdir
        defaults: dict[str, Any] = dict(jdl or {})
        if sites:
            defaults["Site"] = list(sites)
        if banned_sites:
            defaults["BannedSites"] = list(banned_sites)
        if platform:
            defaults["Platform"] = platform
        self.defaults = defaults
        self.upload_se = upload_se
        self.verify: Literal["catalog", "replica"] = verify
        self.capabilities = replace(
            DiracBackend.capabilities, output_grace=output_grace, sandbox_limit=sandbox_limit
        )
        self._session = session
        self._physical = physical
        self._lfns: LFNStore | None = None

    @property
    def session(self) -> DiracSession:
        """The DIRAC session, made on first use (on the backend's thread)."""
        if self._session is None:
            self._session = DiracSession(self.profile)
        return self._session

    def url_storage(self, url: str) -> LFNStore | None:
        """xrdwms's hook: the catalog-backed storage for ``lfn:`` URLs, else ``None``.

        Large inputs staged to an ``lfn:`` staging area are uploaded to
        ``upload_se`` (or the profile's ``upload_se``) and registered; LFN
        outputs are checked in the catalog (``verify="replica"``: against a
        replica too).
        """
        if not is_lfn(url):
            return None
        if self._lfns is None:
            se = self.upload_se or self.session.profile.extra.get("upload_se")
            self._lfns = LFNStore(
                self.session, self._physical or _grid_storage(), se=se, verify=self.verify
            )
        return self._lfns

    def _handle(self, job_id: int) -> JobHandle:
        return JobHandle(self.name, str(job_id))

    # -- submit --------------------------------------------------------------------------------

    def _submit(self, specs: list[KeyedSpec]) -> list[SubmitResult]:
        """Each spec as its own DIRAC job (one sandbox and one ``submitJob`` each)."""
        base = Path(self.workdir) if self.workdir else Path.cwd()
        return [self._one(keyed, base) for keyed in specs]

    def _one(self, keyed: KeyedSpec, base: Path) -> SubmitResult:
        """Submit one spec; its failure becomes its result, not an exception."""
        try:
            jdl, local = to_jdl(keyed.spec, keyed.key, defaults=self.defaults)
            files = [str(path if (path := Path(f)).is_absolute() else base / path) for f in local]
            result = self.session.jobs.submit(jdl, sandbox=files)
        except (UnsupportedError, FileNotFoundError) as exc:
            return SubmitResult(keyed.key, failure=Failure(FailureKind.PAYLOAD, str(exc)))
        except (AuthorizationError, CredentialError) as exc:
            return SubmitResult(keyed.key, failure=Failure(FailureKind.AUTH, str(exc)))
        except (DiracError, XrdError, OSError) as exc:
            return SubmitResult(keyed.key, failure=Failure(FailureKind.INFRA, str(exc)))
        return SubmitResult(keyed.key, handle=self._handle(result.job_ids[0]))

    # -- monitor -------------------------------------------------------------------------------

    def _status(self, handles: list[JobHandle]) -> list[JobInfo]:
        """One bulk query for every handle; a job DIRAC does not answer for is ``UNKNOWN``."""
        try:
            found = self.session.jobs.status(int(h.id) for h in handles)
        except (DiracError, XrdError, OSError) as exc:
            return [
                JobInfo(h, JobState.UNKNOWN, native_state=str(exc), transient=True) for h in handles
            ]
        return [self._info(h, found.get(int(h.id))) for h in handles]

    def _info(self, handle: JobHandle, job: DiracJob | None) -> JobInfo:
        """One DIRAC job as a ``JobInfo``."""
        if job is None:
            return JobInfo(handle, JobState.UNKNOWN, native_state="not found")
        state, failure, code = _state(job)
        native = "/".join(part for part in (job.status, job.minor_status) if part)
        return JobInfo(
            handle,
            state,
            native_state=native or None,
            exit_code=code,
            failure=failure,
            site=job.site or None,
            submitted=_stamp(job, "submitted"),
            started=_stamp(job, "started"),
            ended=_stamp(job, "ended"),
        )

    # -- manage --------------------------------------------------------------------------------

    def _results(self, handles: list[JobHandle], done: list[DiracOp]) -> list[OpResult]:
        by_id = {op.job_id: op for op in done}
        return [
            OpResult(h, by_id[int(h.id)].ok, by_id[int(h.id)].reason)
            if int(h.id) in by_id
            else OpResult(h, False, "no answer")
            for h in handles
        ]

    def _cancel(self, handles: list[JobHandle]) -> list[OpResult]:
        """``killJob`` for every handle at once."""
        return self._results(handles, self.session.jobs.kill(int(h.id) for h in handles))

    def _clean(self, handles: list[JobHandle]) -> list[OpResult]:
        """``deleteJob`` for every handle at once (their sandboxes go with them)."""
        return self._results(handles, self.session.jobs.delete(int(h.id) for h in handles))

    def _retrieve(self, handle: JobHandle, directory: str) -> Retrieved:
        """Download the output sandbox into ``directory/<job id>``."""
        target = Path(directory, handle.id)
        files = self.session.jobs.output(int(handle.id), target)
        return Retrieved(handle, str(target), tuple(files))

    def _find(self, keys: list[str]) -> dict[str, JobHandle]:
        """Your jobs named ``*~<key>``, for the keys asked about."""
        wanted = set(keys)
        found: dict[str, JobHandle] = {}
        for job in self.session.jobs.find(name=f"%{_KEY_MARK}%", limit=10000):
            key = key_of(job.name)
            if key is not None and key in wanted:
                found[key] = self._handle(job.job_id)
        return found

    def _close(self) -> None:
        """Close the session's connections."""
        if self._session is not None:
            self._session.close()
