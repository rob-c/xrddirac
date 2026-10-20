"""End to end on the fakes: the read path and a job's whole life, both DIRAC shapes."""

from __future__ import annotations

from pathlib import Path

from xrddirac.session import DiracSession
from xrddirac.testing import FakeDirac


def test_read_path(dirac: FakeDirac, session: DiracSession) -> None:
    dirac.add_file("/testvo/data/f1", replicas={"SITE-DISK": "", "SITE-TAPE": ""})
    dirac.tape["/tape/testvo/data/f1"] = "TAPE"
    reps = session.catalog.replicas(["/testvo/data/f1", "/testvo/missing"])
    assert reps.successful == {"/testvo/data/f1": {"SITE-DISK": "", "SITE-TAPE": ""}}
    assert reps.failed == {"/testvo/missing": "No such file or directory"}
    states = session.replica_status.check(reps.successful)
    item = states["/testvo/data/f1"]
    assert item.verdict == "ONLINE"
    assert {r.se: str(r.state) for r in item.replicas} == {
        "SITE-DISK": "ONLINE",
        "SITE-TAPE": "NEARLINE",
    }


def _job_life(fake: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("echo hi\n")
    result = session.jobs.submit('[Executable = "run.sh"; JobName = "smoke";]', sandbox=[script])
    (job,) = result.job_ids
    assert session.jobs.status([job])[job].status == "Received"
    fake.finish(job, files={"std.out": b"hi\n"})
    assert session.jobs.status([job])[job].status == "Done"
    files = session.jobs.output(job, tmp_path / "out")
    assert files == ["std.out"]
    assert (tmp_path / "out" / "std.out").read_text() == "hi\n"
    assert [r.ok for r in session.jobs.kill([job, 1])] == [True, False]


def test_job_life_dirac9(dirac: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    _job_life(dirac, session, tmp_path)
    assert any(path.startswith("/S3/") for path in dirac.sandboxes)


def test_job_life_dirac8(dirac8: FakeDirac, session8: DiracSession, tmp_path: Path) -> None:
    _job_life(dirac8, session8, tmp_path)
    assert any(path.startswith("/SandBox/") for path in dirac8.sandboxes)
