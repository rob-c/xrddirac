"""A Ganga-like layer, the dask way: datasets, applications, splitting, a job per subset.

Ganga's model is ``Job(application, inputdata, splitter, backend, merger)``,
run by Ganga's own monitoring loop and kept in Ganga's repository. Here each
of those is something that already exists, and this module only joins them:

==============  =========================================================
Ganga           here
==============  =========================================================
``BKQuery``     :meth:`Dataset.from_bookkeeping` (size-checked first)
``LHCbDataset`` :class:`Dataset`: LFNs, Bookkeeping metadata, replicas
splitter        :meth:`Dataset.split` - by files, keeping each subset's
                files at common sites so DIRAC can run it next to its data
application     :class:`GaudiApp` (``lb-run Project/version gaudirun.py``)
                or any :class:`xrdwms.JobSpec`
backend         xrdwms's ``dirac`` backend (:mod:`xrddirac.wms`), or ARC,
                or local processes, unchanged
monitoring      xrdwms's :class:`~xrdwms.JobManager`: bulk polling,
                retries by failure kind, the crash-safe journal
merger          a dask task over the subjobs' futures (:func:`merge_outputs`)
repository      xrdwms's journal (SQLite), shared by dask workers and
                ``xrdwms status``
==============  =========================================================

So a whole analysis is one dask graph::

    from dask.distributed import Client
    import xrdwms.dask as gw
    from xrddirac import DiracSession
    from xrddirac.lhcb import Dataset, GaudiApp, submit_dataset

    cluster = gw.gateway_cluster(backends={"lhcb": {"kind": "dirac", "profile": "lhcb"}},
                                 journal="~/work/b2cc.sqlite")
    client = Client(cluster)
    with DiracSession("lhcb") as dirac:
        data = Dataset.from_bookkeeping(dirac, "/LHCb/Collision24/.../B2CC.DST", runs=(299632,))
        data = data.with_replicas(dirac).online(dirac)
    app = GaudiApp("DaVinci", "v65r0", options=("ntuple.py",), platform="x86_64_v2-el9-gcc13-opt")
    futures = submit_dataset(client, app, data, backend="lhcb", files_per_job=5,
                             outputs=("tuple.root",))
    merged = client.submit(merge_outputs, futures, "tuple.root")

Status: a **prototype**. It shows the shape - every piece runs and is
tested against fakes - but what a production replacement still needs (a
prepared-application cache on CVMFS or EOS, output data registration,
re-splitting failed subsets, the GUI-less ``jobs`` table users know from
Ganga) is listed in ``docs/DESIGN.md``.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .catalog import normalise_lfn
from .config import cache_dir

if TYPE_CHECKING:
    from .bookkeeping import BKFile
    from .session import DiracSession

__all__ = ["Dataset", "GaudiApp", "merge_outputs", "submit_dataset"]


@dataclass(frozen=True)
class Dataset:
    """LFNs, with what is known of them: Bookkeeping metadata, replicas by SE."""

    lfns: tuple[str, ...]
    name: str = "dataset"
    metadata: Mapping[str, BKFile] = field(default_factory=dict)
    replicas: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    #: Where each file can be read now (SE names), once :meth:`online` has looked.
    online_at: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __len__(self) -> int:
        """How many files."""
        return len(self.lfns)

    @classmethod
    def from_lfns(cls, lfns: Iterable[str], name: str = "dataset") -> Dataset:
        """A dataset of these LFNs (duplicates dropped, order kept)."""
        return cls(tuple(dict.fromkeys(normalise_lfn(lfn) for lfn in lfns)), name)

    @classmethod
    def from_bookkeeping(
        cls, session: DiracSession, path: str, *, limit: int = 1000, **filters: Any
    ) -> Dataset:
        """A Bookkeeping dataset's files and metadata (refused over ``limit`` files)."""
        files = session.bookkeeping.files(path, limit=limit, **filters)
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return cls(tuple(sorted(files)), name, metadata=files)

    @property
    def size(self) -> int:
        """Bytes, as far as the metadata says."""
        return sum(meta.size or 0 for meta in self.metadata.values())

    @property
    def events(self) -> int:
        """Events, as far as the metadata says."""
        return sum(meta.events or 0 for meta in self.metadata.values())

    def with_replicas(self, session: DiracSession) -> Dataset:
        """The same dataset with each file's replicas from the catalog."""
        found = session.catalog.replicas(self.lfns)
        return replace(self, replicas={**self.replicas, **found.successful})

    def online(self, session: DiracSession, *, locality: bool = True) -> Dataset:
        """Only the files readable now (a disk replica, or one staged on tape), and where."""
        source = self if self.replicas else self.with_replicas(session)
        availability = session.replica_status.check(source.replicas, locality=locality)
        keep = {lfn: item.online_sites for lfn, item in availability.items() if item.online}
        return replace(
            source, lfns=tuple(lfn for lfn in source.lfns if lfn in keep), online_at=keep
        )

    def _sites_of(self, lfn: str) -> frozenset[str]:
        """The SEs a file can be read from (online ones when known, else every replica)."""
        if lfn in self.online_at:
            return frozenset(self.online_at[lfn])
        return frozenset(self.replicas.get(lfn, {}))

    def split(self, files_per_job: int = 10, *, by_site: bool = True) -> list[Dataset]:
        """Subsets of at most ``files_per_job`` files; with ``by_site``, files sharing SEs together.

        Ganga's ``SplitByFiles`` with its ``bulksubmit`` grouping: DIRAC
        sends a job where *all* its input files are, so a subset whose files
        share no SE could only run where it must download some. Files are
        grouped by their exact SE set first, then cut into chunks.
        """
        if files_per_job < 1:
            raise ValueError("files_per_job must be at least 1")
        groups: dict[frozenset[str], list[str]] = defaultdict(list)
        for lfn in self.lfns:
            groups[self._sites_of(lfn) if by_site else frozenset()].append(lfn)
        subsets: list[Dataset] = []
        for _sites, lfns in sorted(groups.items(), key=lambda item: sorted(item[0])):
            for start in range(0, len(lfns), files_per_job):
                part = tuple(lfns[start : start + files_per_job])
                subsets.append(self._subset(part, len(subsets)))
        return subsets

    def _subset(self, lfns: tuple[str, ...], index: int) -> Dataset:
        """A subset of this dataset, keeping what is known of its files."""
        return Dataset(
            lfns,
            f"{self.name}.{index}",
            metadata={lfn: self.metadata[lfn] for lfn in lfns if lfn in self.metadata},
            replicas={lfn: self.replicas[lfn] for lfn in lfns if lfn in self.replicas},
            online_at={lfn: self.online_at[lfn] for lfn in lfns if lfn in self.online_at},
        )

    def options_file(self) -> str:
        """Gaudi options naming this dataset's files (``IOHelper``), as Ganga writes ``data.py``."""
        listing = ",\n    ".join(repr(f"LFN:{lfn}") for lfn in self.lfns)
        return (
            "from GaudiConf import IOHelper\n"
            f"IOHelper('ROOT').inputFiles([\n    {listing},\n], clear=True)\n"
        )


@dataclass(frozen=True)
class GaudiApp:
    """An LHCb application run with ``lb-run``, as Ganga's ``GaudiExec`` runs a prepared one."""

    project: str
    version: str
    options: tuple[str, ...] = ()
    platform: str | None = None
    #: Extra arguments for ``gaudirun.py``.
    arguments: tuple[str, ...] = ()

    def spec(
        self,
        data: Dataset,
        *,
        outputs: Sequence[str] = (),
        name: str | None = None,
        cpu_time: int | None = 86400,
        max_retries: int = 2,
        workdir: str | os.PathLike[str] | None = None,
    ) -> Any:
        """One :class:`xrdwms.JobSpec` running this application over ``data``.

        The dataset's options file (``data.py``) is written beside the job
        (under ``workdir``, default xrddirac's cache) so it travels in the
        input sandbox; the LFNs also go in ``InputData``, so DIRAC runs the
        job where they are and the job wrapper resolves them.
        """
        from xrdwms import Input, JobSpec, Output, Resources, Runtime

        data_py = _write_options(data, workdir)
        platform = ["-c", self.platform] if self.platform else []
        arguments = (
            *platform,
            f"{self.project}/{self.version}",
            "gaudirun.py",
            *self.arguments,
            *(Path(option).name for option in self.options),
            data_py.name,
        )
        return JobSpec(
            name=name or f"{self.project}-{data.name}".replace("/", "_"),
            executable="lb-run",
            arguments=arguments,
            inputs=(*(Input(option) for option in self.options), Input(str(data_py))),
            outputs=tuple(Output(o) for o in outputs),
            resources=Resources(cputime_s=cpu_time),
            runtime=Runtime(platform=self.platform),
            max_retries=max_retries,
            labels={
                "group": data.name.split(".")[0],
                "application": f"{self.project}/{self.version}",
            },
            native={"dirac": {"InputData": [f"LFN:{lfn}" for lfn in data.lfns]}},
        )


def _write_options(data: Dataset, workdir: str | os.PathLike[str] | None) -> Path:
    """``data.py`` for ``data``, under a name unique to its contents (written once)."""
    text = data.options_file()
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    base = Path(workdir) if workdir is not None else cache_dir("jobs")
    path = base / digest / "data.py"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return path


def submit_dataset(
    client: Any,
    app: GaudiApp,
    data: Dataset,
    *,
    backend: str,
    files_per_job: int = 10,
    outputs: Sequence[str] = (),
    **spec_options: Any,
) -> list[Any]:
    """One grid job per subset of ``data``: their dask futures (of :class:`xrdwms.dask.JobResult`).

    The job keys are pure (the same subset of the same application is the
    same job), so running this again after a crash re-attaches to the jobs
    already submitted instead of sending them twice.
    """
    from xrdwms import dask as gw

    return [
        gw.submit_job(
            client, app.spec(part, outputs=outputs, **spec_options), backend=backend, pure=True
        )
        for part in data.split(files_per_job)
    ]


def merge_outputs(results: Sequence[Any], name: str, destination: str | None = None) -> list[str]:
    """The paths of ``name`` in each finished subjob's sandbox (a merger's input).

    A real merger (``hadd`` for ROOT files) runs on these paths; this one
    gathers them, so a dask graph can end in whatever merge fits the output.
    With ``destination``, the files are concatenated there (text outputs).
    """
    paths = [str(result.path(name)) for result in results]
    if destination is not None:
        with open(destination, "wb") as merged:
            for path in paths:
                merged.write(Path(path).read_bytes())
    return paths
