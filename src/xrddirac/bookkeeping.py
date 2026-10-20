"""LHCb's Bookkeeping: datasets by path, their size before their files, file metadata.

A dataset is named by its bookkeeping path, as ``dirac-bookkeeping-get-files``
and Ganga's ``BKQuery`` take it::

    /<ConfigName>/<ConfigVersion>/<Conditions>/<Processing/Pass/...>/<EventType>/<FileType>
    /LHCb/Collision24/Beam6800GeV-VeloClosed-MagDown/Real Data/Sprucing24c2a/94000000/B2CC.DST

The processing pass is every part between the conditions and the 8-digit
event type. Runs, a production and data-quality flags narrow it further
(:class:`Query`).

Datasets can be enormous - that one is 24,000 files and 100 TB - so
:meth:`Bookkeeping.files` asks for the dataset's *summary* first (one cheap
call: files, events, bytes, luminosity) and refuses, with
:class:`~xrddirac.errors.TooLargeError`, a dataset over the caller's
``limit`` before a single file is listed. Narrow it (``runs``) or raise the
limit knowingly.

Read-only: nothing here adds, changes or hides anything in the Bookkeeping.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .catalog import Bulk, normalise_lfn
from .errors import ConfigError, TooLargeError, WireError

if TYPE_CHECKING:
    from .session import DiracSession

__all__ = ["BKFile", "Bookkeeping", "DatasetSummary", "Query"]

SERVICE = "Bookkeeping/BookkeepingManager"


@dataclass(frozen=True, slots=True)
class Query:
    """A bookkeeping query: a dataset path's parts and the filters on it."""

    config_name: str
    config_version: str
    conditions: str
    processing_pass: str
    event_type: str
    file_type: str
    runs: tuple[int, ...] = ()
    start_run: int | None = None
    end_run: int | None = None
    production: int | None = None
    data_quality: tuple[str, ...] = ()
    visible: bool = True

    @classmethod
    def from_path(cls, path: str, **filters: Any) -> Query:
        """A query from a bookkeeping path; :class:`ConfigError` for one that is not."""
        parts = [part for part in path.strip().split("/") if part]
        if len(parts) < 6:
            raise ConfigError(
                "Not a bookkeeping path (/Config/Version/Conditions/Pass.../EventType/FileType): "
                f"{path!r}"
            )
        event_type, file_type = parts[-2], parts[-1]
        if not (event_type.isdigit() and len(event_type) == 8):
            raise ConfigError(
                f"Bookkeeping path {path!r}: {event_type!r} is not an 8-digit event type"
            )
        return cls(
            config_name=parts[0],
            config_version=parts[1],
            conditions=parts[2],
            processing_pass="/" + "/".join(parts[3:-2]),
            event_type=event_type,
            file_type=file_type,
            **filters,
        )

    @property
    def path(self) -> str:
        """The bookkeeping path again."""
        return "/".join(
            (
                "",
                self.config_name,
                self.config_version,
                self.conditions,
                self.processing_pass.strip("/"),
                self.event_type,
                self.file_type,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        """The query as the BookkeepingManager reads it."""
        query: dict[str, Any] = {
            "ConfigName": self.config_name,
            "ConfigVersion": self.config_version,
            "ConditionDescription": self.conditions,
            "ProcessingPass": self.processing_pass,
            "EventType": self.event_type,
            "FileType": self.file_type,
            "Visible": "Y" if self.visible else "N",
        }
        if self.runs:
            query["RunNumber"] = list(self.runs)
        if self.start_run is not None:
            query["StartRun"] = self.start_run
        if self.end_run is not None:
            query["EndRun"] = self.end_run
        if self.production is not None:
            query["Production"] = self.production
        if self.data_quality:
            query["DataQuality"] = list(self.data_quality)
        return query


@dataclass(frozen=True, slots=True)
class DatasetSummary:
    """How big a dataset is."""

    files: int
    events: int
    size: int
    luminosity: float

    @classmethod
    def read(cls, value: Any) -> DatasetSummary:
        """From ``getFilesSummary``'s ``ParameterNames``/``Records`` table."""
        if not isinstance(value, dict) or "ParameterNames" not in value:
            raise WireError("getFilesSummary: not a ParameterNames/Records table")
        records = value.get("Records") or [[]]
        row = dict(zip(value["ParameterNames"], records[0] if records else [], strict=False))
        return cls(
            files=int(row.get("NbofFiles") or 0),
            events=int(row.get("NumberOfEvents") or 0),
            size=int(row.get("FileSize") or 0),
            luminosity=float(row.get("Luminosity") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class BKFile:
    """A file as the Bookkeeping knows it."""

    lfn: str
    size: int | None = None
    events: int | None = None
    run: int | None = None
    luminosity: float | None = None
    file_type: str = ""
    event_type: str = ""
    data_quality: str = ""
    got_replica: bool | None = None
    guid: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def read(cls, lfn: str, raw: Mapping[str, Any]) -> BKFile:
        """From either of the Bookkeeping's spellings (``FileSize``/``Runnumber``/``RunNumber``)."""
        replica = raw.get("GotReplica")
        return cls(
            lfn=lfn,
            size=_int(raw.get("FileSize")),
            events=_int(raw.get("EventStat")),
            run=_int(raw.get("RunNumber", raw.get("Runnumber"))),
            luminosity=_float(raw.get("Luminosity")),
            file_type=str(raw.get("FileType") or ""),
            event_type=str(raw.get("EventType") or ""),
            data_quality=str(raw.get("DataqualityFlag") or raw.get("DataQuality") or ""),
            got_replica=None if replica is None else str(replica).lower() == "yes",
            guid=str(raw.get("GUID") or ""),
            extra=dict(raw),
        )


def _int(value: Any) -> int | None:
    """An integer field, ``None`` when absent or not a number."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    """A float field, ``None`` when absent or not a number."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class Bookkeeping:
    """Read-only LHCb Bookkeeping queries; see the module docstring."""

    def __init__(self, session: DiracSession) -> None:
        self.session = session

    def _call(self, method: str, *args: Any) -> Any:
        """One BookkeepingManager call."""
        return self.session.service(SERVICE).call(method, *args)

    @staticmethod
    def _query(dataset: str | Query, filters: Mapping[str, Any]) -> Query:
        """A :class:`Query` from a path (with ``filters``) or as given."""
        return dataset if isinstance(dataset, Query) else Query.from_path(dataset, **filters)

    def summary(self, dataset: str | Query, **filters: Any) -> DatasetSummary:
        """How many files, events and bytes the dataset holds (one call, nothing listed)."""
        return DatasetSummary.read(
            self._call("getFilesSummary", self._query(dataset, filters).as_dict())
        )

    def files(
        self, dataset: str | Query, *, limit: int = 1000, **filters: Any
    ) -> dict[str, BKFile]:
        """The dataset's files with their metadata; :class:`TooLargeError` over ``limit`` files.

        ``limit=0`` lists whatever size the dataset is.
        """
        query = self._query(dataset, filters)
        if limit > 0:
            size = self.summary(query)
            if size.files > limit:
                raise TooLargeError(
                    f"{query.path} holds {size.files} files ({size.size / 1e12:.2f} TB), over the "
                    f"limit of {limit}: narrow it (runs, a production) or raise the limit"
                )
        answer = self._call("getVisibleFilesWithMetadata", query.as_dict())
        lfns = answer.get("LFNs") if isinstance(answer, dict) else None
        if not isinstance(lfns, dict):
            raise WireError("getVisibleFilesWithMetadata: no LFNs in the answer")
        return {
            lfn: BKFile.read(lfn, raw if isinstance(raw, dict) else {}) for lfn, raw in lfns.items()
        }

    def file_metadata(self, lfns: Iterable[str]) -> Bulk[BKFile]:
        """The Bookkeeping's metadata for each file (run, events, luminosity, data quality)."""
        wanted = list(dict.fromkeys(normalise_lfn(lfn) for lfn in lfns))
        if not wanted:
            return Bulk()
        answer = Bulk.read(self._call("getFileMetadata", wanted), f"{SERVICE}.getFileMetadata")
        return answer.map(BKFile.read)

    def processing_passes(
        self, config_name: str, config_version: str, conditions: str
    ) -> list[str]:
        """The processing passes under a configuration and conditions, for browsing."""
        query = {
            "ConfigName": config_name,
            "ConfigVersion": config_version,
            "ConditionDescription": conditions,
        }
        return _names(self._call("getProcessingPass", query, "/"))

    def config_versions(self, config_name: str) -> list[str]:
        """The configuration versions (``Collision24``...) under a configuration name."""
        return _names(self._call("getConfigVersions", {"ConfigName": config_name}))


def _names(value: Any) -> list[str]:
    """The first column of the Bookkeeping's ``ParameterNames``/``Records`` table(s)."""
    tables: Sequence[Any] = value if isinstance(value, list) else [value]
    names: list[str] = []
    for table in tables:
        if isinstance(table, dict):
            names += [str(row[0]) for row in table.get("Records") or [] if row]
    return names
