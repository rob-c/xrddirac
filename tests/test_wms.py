"""The xrdwms backend: JobSpec to JDL, DIRAC states, and whole jobs through xrdwms's JobManager."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from xrdwms import Input, JobManager, JobSpec, Journal, Output, PollPolicy, Resources
from xrdwms.backends import create, kinds
from xrdwms.errors import UnsupportedError
from xrdwms.model import FailureKind, JobHandle, JobState, KeyedSpec, Placement, Runtime

from xrddirac.jobs import DiracJob
from xrddirac.session import DiracSession
from xrddirac.testing import FakeDirac
from xrddirac.wms import DiracBackend, _state, classify, job_name, key_of, to_jdl

FAST = PollPolicy(interval=0.01, max_interval=0.05, batch_delay=0.0, backoff_cap=0.05, lease=5.0)


def test_translation(tmp_path: Path) -> None:
    spec = JobSpec(
        "ana",
        "lb-run",
        ("DaVinci/v1", "gaudirun.py", "opts py"),
        environment={"A": "1"},
        inputs=(Input("opts.py"), Input("lfn:/v/data/f1")),
        outputs=(Output("tuple.root"), Output("hist.root", "lfn:/v/user/hist.root")),
        resources=Resources(cores=4, memory_mb=3000, cputime_s=3600),
        runtime=Runtime(platform="x86_64-el9"),
        placement=Placement(sites=("LCG.CERN.cern",), banned_sites=("LCG.Bad.org",), tags=("GPU",)),
        labels={"group": "mygroup"},
        native={"dirac": {"Priority": 5, "OutputSE": ["CERN-USER"]}},
    )
    jdl, local = to_jdl(spec, "k1-0", defaults={"Site": ["X"], "Platform": "p"})
    assert local == ["opts.py"]
    assert jdl["JobName"] == "ana~k1-0" and jdl["JobGroup"] == "mygroup"
    assert jdl["Arguments"] == "DaVinci/v1 gaudirun.py 'opts py'"
    assert jdl["InputSandbox"] == ["LFN:/v/data/f1"] and jdl["OutputData"] == [
        "LFN:/v/user/hist.root"
    ]
    assert jdl["OutputSandbox"] == ["std.out", "std.err", "tuple.root"]
    assert (
        jdl["NumberOfProcessors"] == 4 and jdl["CPUTime"] == 3600 and jdl["Tags"] == ["3GB", "GPU"]
    )
    assert jdl["Site"] == ["LCG.CERN.cern"] and jdl["BannedSites"] == ["LCG.Bad.org"]
    assert jdl["Platform"] == "x86_64-el9" and jdl["ExecutionEnvironment"] == ["A=1"]
    assert jdl["Priority"] == 5 and jdl["OutputSE"] == ["CERN-USER"]
    walltime, _ = to_jdl(JobSpec("w", "x", resources=Resources(walltime_s=60)), "k")
    assert (
        walltime["CPUTime"] == 60
        and walltime["JobGroup"] == "xrdwms"
        and "Arguments" not in walltime
    )


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (JobSpec("a", "x", inputs=(Input("root://h//f"),)), "inputs"),
        (JobSpec("a", "x", inputs=(Input("dir/f.txt", "g.txt"),)), "inputs"),
        (JobSpec("a", "x", inputs=(Input("lfn:/v/f.txt", "g.txt"),)), "inputs"),
        (JobSpec("a", "x", outputs=(Output("f", "root://h//f"),)), "outputs"),
        (JobSpec("a", "x", outputs=(Output("f", "lfn:/v/g"),)), "outputs"),
        (JobSpec("a", "x", resources=Resources(gpus=1)), "resources.gpus"),
        (JobSpec("a", "x", resources=Resources(disk_mb=1)), "resources.disk_mb"),
        (JobSpec("a", "x", runtime=Runtime(container="img")), "runtime.container"),
        (JobSpec("a", "x", runtime=Runtime(environments=("E",))), "runtime.environments"),
        (JobSpec("a", "x", placement=Placement(queue="q")), "placement.queue"),
    ],
)
def test_unsupported(spec: JobSpec, field: str) -> None:
    with pytest.raises(UnsupportedError) as caught:
        to_jdl(spec, "k")
    assert caught.value.field == field


def test_states_and_names() -> None:
    assert key_of(job_name(JobSpec("a~b", "x"), "k-1")) == "k-1"
    assert key_of(None) is None and key_of("plain") is None and key_of("a~") is None
    assert classify("Proxy expired") is FailureKind.AUTH
    assert classify("Watchdog identified this job as stalled") is FailureKind.LIMIT
    assert classify("Input Data Resolution") is FailureKind.DATA
    assert classify("Job stalled: pilot not running") is FailureKind.INFRA
    assert classify("Application Finished With Errors") is FailureKind.PAYLOAD
    assert classify("Something else") is FailureKind.INFRA

    def state(status: str, minor: str = "", app: str = "") -> tuple[JobState, object, object]:
        return _state(DiracJob(1, status, minor, app))

    assert state("Done", app="DaVinci exited with status 0")[0] is JobState.DONE
    assert state("Done")[2] == 0
    failed = state("Failed", "Application Finished With Errors", "gaudirun exited with status 3")
    assert failed[0] is JobState.FAILED and failed[2] == 3
    assert state("Failed")[1].message == "no reason given"  # type: ignore[attr-defined]  # a Failure
    assert state("Killed", "Marked for termination")[0] is JobState.CANCELLED
    assert state("Stalled")[0] is JobState.HELD and state("Staging")[0] is JobState.STAGING_IN
    assert state("Brand new")[0] is JobState.UNKNOWN


def test_entry_point() -> None:
    assert kinds()["dirac"] == "xrddirac.wms:DiracBackend"
    backend = create("dirac", "lhcb", profile="lhcb", sites=["S"], banned_sites=["B"], platform="p")
    assert isinstance(backend, DiracBackend) and backend.defaults == {
        "Site": ["S"],
        "BannedSites": ["B"],
        "Platform": "p",
    }
    assert backend.capabilities.find and not backend.capabilities.proxy_renewal


async def _life(fake: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    (tmp_path / "in.txt").write_text("input")
    backend = DiracBackend("d", session=session, workdir=str(tmp_path))
    spec = JobSpec(
        "hello", "/bin/echo", ("hi",), inputs=(Input("in.txt"),), outputs=(Output("out.txt"),)
    )
    journal = Journal()
    async with JobManager([backend], journal=journal, policy=FAST) as manager:
        [key] = await manager.submit([spec], backend="d")
        [record] = await manager.submitted([key])
        job_id = int(record.handle.id)  # type: ignore[union-attr]  # submitted, so it has a handle
        assert fake.jobs[job_id].jdl["JobName"] == f"hello~{key}-0"
        fake.jobs[job_id].status = "Running"
        while manager.journal.get(key).state is not JobState.RUNNING:
            await manager.refresh([key])
        fake.finish(job_id, files={"out.txt": b"result", "std.out": b"hi\n"})
        record = await manager.wait(key)
        assert record.state is JobState.DONE and record.info.site == "LCG.Test.org"  # type: ignore[union-attr]  # polled
        got = await manager.retrieve(key, str(tmp_path / "results"))
        assert Path(got.path, "out.txt").read_text() == "result" and "std.out" in got.files
        [cleaned] = await manager.clean([key], forget=True)
        assert cleaned.ok and fake.jobs[job_id].status == "Deleted"
    journal.close()


async def test_job_life_dirac9(dirac: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    await _life(dirac, session, tmp_path)


async def test_job_life_dirac8(dirac8: FakeDirac, session8: DiracSession, tmp_path: Path) -> None:
    await _life(dirac8, session8, tmp_path)


async def test_backend_corners(dirac: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    backend = DiracBackend("d", session=session, workdir=str(tmp_path))
    results = await backend.submit(
        [
            KeyedSpec("a-0", JobSpec("ok", "/bin/true")),
            KeyedSpec("other-0", JobSpec("ok", "/bin/true")),
            KeyedSpec("b-0", JobSpec("gpu", "/bin/true", resources=Resources(gpus=1))),
            KeyedSpec("c-0", JobSpec("missing", "/bin/true", inputs=(Input("nope.txt"),))),
        ]
    )
    assert results[0].ok and [r.failure.kind for r in results[2:]] == [FailureKind.PAYLOAD] * 2  # type: ignore[union-attr]  # failed ones
    handle = results[0].handle
    assert handle is not None
    session.jobs.submit('[Executable = "x"; JobName = "plain";]')
    found = await backend.find(["a-0", "zz-0"])
    assert found == {"a-0": handle}
    [info] = await backend.status([handle])
    assert info.state is JobState.QUEUED and info.native_state == "Received/Job accepted"
    [gone] = await backend.status([JobHandle("d", "7")])
    assert gone.state is JobState.UNKNOWN and not gone.transient
    [killed] = await backend.cancel([handle])
    assert killed.ok and dirac.jobs[int(handle.id)].status == "Killed"
    [missing] = await backend.cancel([JobHandle("d", "7")])
    assert not missing.ok and missing.reason == "no such job"
    dirac.jobs[int(handle.id)].submitted = dt.datetime(2026, 1, 1)
    [renew] = await backend.renew([handle])
    assert not renew.ok
    dirac.stop()
    [down] = await backend.status([handle])
    assert down.state is JobState.UNKNOWN and down.transient
    [infra] = await backend.submit([KeyedSpec("d-0", JobSpec("x", "/bin/true"))])
    assert infra.failure.kind is FailureKind.INFRA  # type: ignore[union-attr]  # refused
    await backend.close()


async def test_auth_failure_and_lazy_session(dirac: FakeDirac, session: DiracSession) -> None:
    from xrddirac.testing import Refuse

    def refuse(_text: str) -> None:
        raise Refuse("Unauthorized query", 1111)

    dirac.diset.rpc("WorkloadManagement/JobManager", "submitJob", refuse)
    backend = DiracBackend("d", session=session)
    [denied] = await backend.submit([KeyedSpec("a-0", JobSpec("x", "/bin/true"))])
    assert denied.failure.kind is FailureKind.AUTH  # type: ignore[union-attr]  # refused
    await backend.close()
    lazy = DiracBackend("d", profile="gridpp")
    assert lazy.session.profile.name == "gridpp"
    await lazy.close()
    unused = DiracBackend("d")
    await unused.close()
