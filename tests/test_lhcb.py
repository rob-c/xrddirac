"""The Ganga-like layer: datasets, splitting, a Gaudi application, a dataset as dask grid jobs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from distributed import Client
from xrdwms import PollPolicy
from xrdwms import dask as gw
from xrdwms.model import JobState

from xrddirac.lhcb import Dataset, GaudiApp, merge_outputs, submit_dataset
from xrddirac.session import DiracSession
from xrddirac.testing import FakeDirac, FakeJob

PATH = "/LHCb/Collision24/Beam6800GeV/Real Data/Sprucing24c2a/94000000/B2CC.DST"


def _data(fake: FakeDirac) -> list[str]:
    lfns = [f"/testvo/bk/f{i}" for i in range(7)]
    sites = [
        {"SITE-DISK": ""},
        {"SITE-DISK": ""},
        {"SITE-DISK": "", "SITE-TAPE": ""},
        {"SITE-TAPE": ""},
        {"SITE-TAPE": ""},
        {"OTHER-DISK": ""},
        {"SITE-DISK": ""},
    ]
    for lfn, reps in zip(lfns, sites, strict=True):
        fake.add_file(lfn, size=10, replicas=reps)
    fake.tape.update({f"/tape{lfns[3]}": "DISK_AND_TAPE", f"/tape{lfns[4]}": "TAPE"})
    fake.add_dataset(PATH, lfns, run=7, events=5)
    return lfns


def test_datasets(dirac: FakeDirac, session: DiracSession) -> None:
    lfns = _data(dirac)
    data = Dataset.from_bookkeeping(session, PATH)
    assert data.name == "B2CC.DST" and len(data) == 7 and data.size == 70 and data.events == 35
    online = data.online(session)
    assert online.lfns == (lfns[0], lfns[1], lfns[2], lfns[3], lfns[6])
    assert online.online_at[lfns[3]] == ("SITE-TAPE",)
    parts = online.split(2)
    assert [len(p) for p in parts] == [2, 2, 1] and parts[0].name == "B2CC.DST.0"
    assert {lfns[0], lfns[1]} == set(parts[0].lfns) and parts[0].metadata[lfns[0]].run == 7
    assert [len(p) for p in data.with_replicas(session).split(10, by_site=False)] == [7]
    assert [len(p) for p in Dataset.from_lfns(lfns + lfns[:1]).split(3)] == [3, 3, 1]
    with pytest.raises(ValueError, match="files_per_job"):
        data.split(0)
    assert "LFN:/testvo/bk/f0" in parts[0].options_file() and "IOHelper" in parts[0].options_file()


def test_gaudi_app(tmp_path: Path) -> None:
    data = Dataset.from_lfns(["/v/a", "/v/b"], "set")
    app = GaudiApp(
        "DaVinci", "v65r0", options=("opts/ntuple.py",), platform="x86_64-el9", arguments=("-T",)
    )
    spec = app.spec(data, outputs=("t.root",), workdir=tmp_path)
    assert spec.executable == "lb-run" and spec.name == "DaVinci-set"
    assert spec.arguments == (
        "-c",
        "x86_64-el9",
        "DaVinci/v65r0",
        "gaudirun.py",
        "-T",
        "ntuple.py",
        "data.py",
    )
    data_py = Path(spec.inputs[1].source)
    assert data_py.read_text() == data.options_file() and data_py.is_relative_to(tmp_path)
    assert app.spec(data, workdir=tmp_path).inputs[1].source == str(data_py)
    assert spec.native["dirac"]["InputData"] == ["LFN:/v/a", "LFN:/v/b"]
    plain = GaudiApp("Moore", "v1").spec(data, name="n")
    assert plain.arguments == ("Moore/v1", "gaudirun.py", "data.py") and plain.name == "n"


@pytest.fixture
def cluster_client(
    dirac: FakeDirac, tmp_path: Path, pki: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Client]:
    """An in-process gateway cluster whose ``lhcb`` backend is the fake DIRAC, by profile."""
    config = tmp_path / "xrddirac.toml"
    config.write_text(
        f'[profiles.fake]\nservers = ["{dirac.diset.url("Configuration/Server")}"]\nvo = "testvo"\n'
        "config_max_age = 0\n"
    )
    monkeypatch.setenv("XRDDIRAC_CONFIG", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name, value in pki.environment().items():
        monkeypatch.setenv(name, value)
    policy = PollPolicy(
        interval=0.01, max_interval=0.05, batch_delay=0.0, backoff_cap=0.05, lease=5.0
    )
    cluster = gw.gateway_cluster(
        backends={"lhcb": {"kind": "dirac", "profile": "fake", "workdir": str(tmp_path)}},
        journal=tmp_path / "journal.sqlite",
        policy=policy,
        workdir=str(tmp_path / "work"),
        dashboard_address=":0",
        threads=2,
        grid_slots=50,
    )
    with Client(cluster) as client:
        yield client
    cluster.close()


def test_a_dataset_as_dask_grid_jobs(
    dirac: FakeDirac, cluster_client: Client, tmp_path: Path
) -> None:
    lfns = [f"/testvo/d/f{i}" for i in range(5)]
    for lfn in lfns:
        dirac.add_file(lfn, replicas={"SITE-DISK": ""})

    def run(job: FakeJob) -> dict[str, bytes]:
        inputs = ",".join(job.jdl.strings("InputData"))
        return {"tuple.txt": f"{inputs}\n".encode(), "std.out": b"ok\n"}

    dirac.autorun = run
    app = GaudiApp("DaVinci", "v65r0")
    futures = submit_dataset(
        cluster_client,
        app,
        Dataset.from_lfns(lfns, "d"),
        backend="lhcb",
        files_per_job=2,
        outputs=("tuple.txt",),
        workdir=tmp_path / "opts",
    )
    results = cluster_client.gather(futures)
    assert [r.record.state for r in results] == [JobState.DONE] * 3
    merged = tmp_path / "merged.txt"
    paths = cluster_client.submit(merge_outputs, futures, "tuple.txt", str(merged)).result(
        timeout=60
    )
    assert len(paths) == 3 and merged.read_text().count("LFN:") == 5
    assert len(dirac.jobs) == 3
    again = submit_dataset(
        cluster_client,
        app,
        Dataset.from_lfns(lfns, "d"),
        backend="lhcb",
        files_per_job=2,
        outputs=("tuple.txt",),
        workdir=tmp_path / "opts",
    )
    cluster_client.gather(again)
    assert len(dirac.jobs) == 3, "pure keys: the same subsets are the same jobs"
    assert merge_outputs(results, "tuple.txt") == paths
