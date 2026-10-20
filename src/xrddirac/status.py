"""Is a replica readable now? Storage status, tape or disk, and where on tape it is.

Three things decide whether a job (or a user) can read a replica without
waiting:

1. **The storage element's status** in DIRAC's Resource Status System:
   ``ReadAccess`` ``Active``/``Degraded`` can be read, ``Banned``/``Probing``
   should not be. One ``ResourceStatus.select`` answers for every SE asked
   about; an installation without an RSS falls back to the SE's own
   ``ReadAccess`` option.
2. **The SE's kind** from the configuration (:mod:`.storage`): a disk SE's
   replica is online.
3. **A tape SE's replica's locality**, from the site's own WLCG Tape REST
   API (``POST archiveinfo``, the call FTS and Rucio use): ``DISK``,
   ``TAPE``, ``DISK_AND_TAPE``, or missing. It stages nothing. Replicas are
   grouped by endpoint, so a thousand files on one tape system are one or a
   few requests (:data:`ARCHIVE_CHUNK` paths each).

The answer per replica is one :class:`ReplicaState` word, gfal2's
``user.status`` vocabulary plus three of DIRAC's::

    ONLINE, ONLINE_AND_NEARLINE    readable now
    NEARLINE                       on tape only: needs staging first
    LOST                           the storage says it has no such file
    BANNED                         the SE is not readable (RSS)
    UNKNOWN                        could not be told (no HTTPS endpoint, endpoint down)

Asking a tape system about a file is a read, but it is still a request to a
site's storage: :meth:`ReplicaStatusService.check` asks only about tape
replicas, and only when ``locality=True``.
"""

from __future__ import annotations

import enum
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from xrdcore.errors import XrdError
from xrdcore.fanout import run_all
from xrdcore.log import get_logger
from xrdhttp import tape as _tape
from xrdhttp.session import Auth, Policy, Session

from .catalog import normalise_lfn
from .errors import ConfigError, DiracError, NoSuchServiceError

if TYPE_CHECKING:
    from .session import DiracSession

__all__ = [
    "ARCHIVE_CHUNK",
    "FileAvailability",
    "ReplicaState",
    "ReplicaStatus",
    "ReplicaStatusService",
]

_log = get_logger(__name__)

#: Paths per ``archiveinfo`` request.
ARCHIVE_CHUNK = 1000
#: RSS read statuses that mean "can be read".
_READABLE = frozenset({"Active", "Degraded"})


class ReplicaState(enum.Enum):
    """Whether a replica can be read now; see the module docstring."""

    ONLINE = "ONLINE"
    ONLINE_AND_NEARLINE = "ONLINE_AND_NEARLINE"
    NEARLINE = "NEARLINE"
    LOST = "LOST"
    BANNED = "BANNED"
    UNKNOWN = "UNKNOWN"

    @property
    def online(self) -> bool:
        """Readable without staging."""
        return self in (ReplicaState.ONLINE, ReplicaState.ONLINE_AND_NEARLINE)

    def __str__(self) -> str:
        """The word."""
        return self.value


@dataclass(frozen=True, slots=True)
class ReplicaStatus:
    """One replica: where, what kind of storage, its state and how to read it."""

    lfn: str
    se: str
    kind: str
    state: ReplicaState
    se_status: str = ""
    url: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class FileAvailability:
    """A file's replicas, and the verdict over them."""

    lfn: str
    replicas: tuple[ReplicaStatus, ...] = ()

    @property
    def online(self) -> bool:
        """Some replica is readable now."""
        return any(r.state.online for r in self.replicas)

    @property
    def verdict(self) -> str:
        """``ONLINE``, ``NEARLINE`` (only on tape), ``UNAVAILABLE`` or ``UNKNOWN``."""
        states = {r.state for r in self.replicas}
        if any(s.online for s in states):
            return "ONLINE"
        if ReplicaState.NEARLINE in states:
            return "NEARLINE"
        if ReplicaState.UNKNOWN in states:
            return "UNKNOWN"
        return "UNAVAILABLE"

    @property
    def online_sites(self) -> tuple[str, ...]:
        """The SEs it can be read from now."""
        return tuple(r.se for r in self.replicas if r.state.online)


@dataclass
class _Plan:
    """The tape replicas to ask one endpoint about."""

    base: str
    items: list[tuple[int, str]] = field(default_factory=list)


class ReplicaStatusService:
    """Replica availability for an installation; see the module docstring."""

    def __init__(
        self,
        session: DiracSession,
        *,
        workers: int = 8,
        connect_timeout: float = 10.0,
        http: Session | None = None,
    ) -> None:
        self.session = session
        self.workers = workers
        self.connect_timeout = connect_timeout
        self._http = http
        self._endpoints: dict[str, str | None] = {}

    @property
    def http(self) -> Session:
        """An HTTP session of its own: a tape system that does not answer is given up on quickly.

        Not the session's shared one, whose policy retries dropped
        connections: here an unreachable endpoint (a site firewall) should
        cost one short connect timeout, not three long ones.
        """
        if self._http is None:
            policy = Policy(connect_timeout=self.connect_timeout, timeout=120.0)
            self._http = Session(tls=self.session.public_tls, policy=policy)
        return self._http

    def close(self) -> None:
        """Close the tape probes' HTTP connections."""
        if self._http is not None:
            self._http.close()
            self._http = None

    # -- storage element status ------------------------------------------------------------

    def se_status(self, names: Iterable[str], status_type: str = "ReadAccess") -> dict[str, str]:
        """Each SE's RSS status (``Active``, ``Banned``...); the SE's own option without an RSS."""
        wanted = sorted(set(names))
        if not wanted:
            return {}
        try:
            rss = self.session.service("ResourceStatus/ResourceStatus")
        except NoSuchServiceError:
            return self._configured_status(wanted)
        answer = rss.call("select", "ResourceStatus", {"Name": wanted, "StatusType": status_type})
        found = {row[0]: row[2] for row in answer or [] if isinstance(row, list) and len(row) > 2}
        return {name: str(found.get(name, "Unknown")) for name in wanted}

    def _configured_status(self, names: Sequence[str]) -> dict[str, str]:
        """``ReadAccess`` from each SE's definition (``Active`` when it says nothing)."""
        result = {}
        for name in names:
            try:
                result[name] = self.session.storage(name).read_access or "Active"
            except ConfigError:
                result[name] = "Unknown"
        return result

    # -- replicas --------------------------------------------------------------------------

    def check(
        self,
        replicas: Mapping[str, Mapping[str, str]],
        *,
        locality: bool = True,
        scheme: str | None = None,
    ) -> dict[str, FileAvailability]:
        """The availability of every replica in ``replicas`` (``{lfn: {SE: pfn}}``, the catalog's).

        ``locality`` asks tape systems where their replicas are; without it a
        tape replica is reported ``UNKNOWN`` ("locality not asked"). ``scheme``
        picks the URL reported (``root``, ``https``; default: the SE's
        preferred protocol).
        """
        statuses = self.se_status({se for reps in replicas.values() for se in reps})
        flat: list[ReplicaStatus] = []
        for lfn, reps in replicas.items():
            for se in reps:
                flat.append(
                    self._first_guess(normalise_lfn(lfn), se, statuses.get(se, "Unknown"), scheme)
                )
        if locality:
            flat = self._tape_localities(flat)
        grouped: dict[str, list[ReplicaStatus]] = defaultdict(list)
        for status in flat:
            grouped[status.lfn].append(status)
        return {
            normalise_lfn(lfn): FileAvailability(
                normalise_lfn(lfn), tuple(grouped[normalise_lfn(lfn)])
            )
            for lfn in replicas
        }

    def _first_guess(self, lfn: str, se: str, se_status: str, scheme: str | None) -> ReplicaStatus:
        """What the configuration and the RSS alone say about one replica."""
        try:
            element = self.session.storage(se)
        except ConfigError as exc:
            return ReplicaStatus(
                lfn, se, "unknown", ReplicaState.UNKNOWN, se_status, reason=str(exc)
            )
        url = element.url(lfn, scheme) or ""
        if se_status not in _READABLE:
            return ReplicaStatus(
                lfn, se, element.kind, ReplicaState.BANNED, se_status, url, f"{se} is {se_status}"
            )
        if element.is_tape:
            return ReplicaStatus(
                lfn,
                se,
                element.kind,
                ReplicaState.UNKNOWN,
                se_status,
                url,
                "tape: locality not asked",
            )
        if element.kind == "disk":
            return ReplicaStatus(lfn, se, element.kind, ReplicaState.ONLINE, se_status, url)
        return ReplicaStatus(
            lfn, se, element.kind, ReplicaState.UNKNOWN, se_status, url, "SEType unknown"
        )

    def _tape_localities(self, flat: list[ReplicaStatus]) -> list[ReplicaStatus]:
        """Ask each tape endpoint, in bulk, about the tape replicas still ``UNKNOWN``."""
        plans: dict[str, _Plan] = {}
        result = list(flat)
        for index, status in enumerate(flat):
            if status.kind != "tape" or status.state is not ReplicaState.UNKNOWN:
                continue
            element = self.session.storage(status.se)
            endpoint = element.endpoint("https")
            if endpoint is None:
                result[index] = replace(status, reason="tape: no HTTPS endpoint to ask")
                continue
            plan = plans.setdefault(endpoint.base, _Plan(endpoint.base))
            plan.items.append((index, _path_of(endpoint.url(status.lfn))))
        calls = [lambda plan=plan: self._ask(plan) for plan in plans.values()]
        for answers in run_all(calls, workers=self.workers, name="xrddirac-tape") if calls else []:
            for index, state, reason in answers:
                result[index] = replace(result[index], state=state, reason=reason)
        return result

    def _ask(self, plan: _Plan) -> list[tuple[int, ReplicaState, str]]:
        """One endpoint's answers for its replicas; a failure makes them ``UNKNOWN``, not raised."""
        try:
            api = self._tape_api(plan.base)
            if api is None:
                return [
                    (i, ReplicaState.UNKNOWN, f"{plan.base} has no tape REST API")
                    for i, _ in plan.items
                ]
            answers: list[tuple[int, ReplicaState, str]] = []
            for start in range(0, len(plan.items), ARCHIVE_CHUNK):
                part = plan.items[start : start + ARCHIVE_CHUNK]
                found = self._archive_info(api, [path for _, path in part])
                answers += [(i, *_state_of(found.get(_tape.collapse(path)))) for i, path in part]
            return answers
        except (XrdError, OSError, DiracError) as exc:
            _log.info("Tape status from %s failed: %s", plan.base, exc)
            return [(i, ReplicaState.UNKNOWN, f"{plan.base}: {exc}") for i, _ in plan.items]

    def _tape_api(self, base: str) -> str | None:
        """The tape REST API's URI at ``base`` (``/.well-known/wlcg-tape-rest-api``), remembered."""
        if base not in self._endpoints:
            response = self.http.request(
                "GET", base + _tape.WELL_KNOWN, auth=Auth(tls=self.session.tls()), timeout=30
            )
            body = response.body()
            if response.status != 200:
                self._endpoints[base] = None
            else:
                endpoint = _tape.choose_endpoint(_tape.parse_document(body, base))
                self._endpoints[base] = endpoint.uri.rstrip("/")
        return self._endpoints[base]

    def _archive_info(self, api: str, paths: Sequence[str]) -> dict[str, _tape.FileStatus]:
        """``POST archiveinfo`` for ``paths``: each path's status, by its collapsed path."""
        response = self.http.request(
            "POST",
            api + "/archiveinfo",
            body=_tape.paths_body(paths),
            headers={"Content-Type": "application/json"},
            auth=Auth(tls=self.session.tls()),
            idempotent=True,
            expect=(200,),
            timeout=120,
        )
        statuses = _tape.archive_statuses(_tape.parse_document(response.body(), api), paths)
        return {_tape.collapse(s.path): s for s in statuses}


def _path_of(url: str) -> str:
    """The path part of an ``https://host:port/path`` URL."""
    rest = url.split("://", 1)[1]
    return "/" + rest.split("/", 1)[1] if "/" in rest else "/"


def _state_of(status: _tape.FileStatus | None) -> tuple[ReplicaState, str]:
    """A tape REST file status as a replica state and a reason."""
    if status is None:
        return ReplicaState.UNKNOWN, "the tape system did not answer for this file"
    if status.error and not status.exists:
        return ReplicaState.LOST, status.error
    if status.online and status.on_tape:
        return ReplicaState.ONLINE_AND_NEARLINE, ""
    if status.online:
        return ReplicaState.ONLINE, ""
    if status.on_tape:
        return ReplicaState.NEARLINE, ""
    return ReplicaState.UNKNOWN, status.error or f"locality {status.state or 'unknown'}"


def summarise(availability: Mapping[str, FileAvailability]) -> dict[str, Any]:
    """Counts over many files: how many are online, only on tape, unavailable."""
    counts: dict[str, int] = defaultdict(int)
    for item in availability.values():
        counts[item.verdict] += 1
    return {"files": len(availability), **dict(sorted(counts.items()))}
