"""DiracX, the job client on both DIRAC shapes, and the session."""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from pathlib import Path
from typing import Any

import pytest
from xrdsec.testing.https import Fault

from xrddirac.config import DiracConfig
from xrddirac.diracx import DiracX, Token, _failure, _form, _more
from xrddirac.errors import AuthorizationError, ConfigError, DiracError, ServiceError, WireError
from xrddirac.jdl import JDL
from xrddirac.jobs import DiracJob, _summary_table, _when
from xrddirac.sandbox import pack
from xrddirac.session import DiracSession
from xrddirac.testing import FakeDirac, Refuse

# -- DiracX ------------------------------------------------------------------------------------


def test_token_life(dirac: FakeDirac, session: DiracSession) -> None:
    clock = [1000.0]
    diracx = DiracX(session, dirac.https_url + "/", clock=lambda: clock[0])
    assert diracx.token() == "fake-token-1" and diracx.token() == "fake-token-1"
    clock[0] += 3600 - 60
    assert diracx.token() == "fake-token-2"
    assert diracx.token(renew=True) == "fake-token-3"
    assert "fake-token" not in repr(diracx) and "fake-token" not in repr(Token("fake-token-x", 1.0))
    dirac.token_lifetime = "never"  # type: ignore[assignment]  # a server that sends no lifetime
    assert diracx._exchange(0.0).expires == 600.0
    dirac.diset.rpc("Framework/ProxyManager", "exchangeProxyForToken", lambda: {"no": "token"})
    with pytest.raises(WireError, match="access token"):
        diracx.token(renew=True)


def test_reauth_on_401(dirac: FakeDirac, session: DiracSession) -> None:
    diracx = session.diracx
    assert diracx.whoami()["preferred_username"] == "tuser"
    dirac.https.add_fault(Fault("GET", "userinfo", status=401))
    assert diracx.whoami()["vo"] == "testvo" and dirac.tokens_made == 2


def test_search_pages(dirac: FakeDirac, session: DiracSession) -> None:
    for _ in range(5):
        session.jobs.submit('[Executable = "x";]')
    diracx = session.diracx
    assert len(diracx.search(per_page=2)) == 5
    assert len(diracx.search(per_page=2, limit=3)) == 3
    assert (
        next(j["JobID"] for j in diracx.search(sort=[{"parameter": "JobID", "direction": "desc"}]))
        == 1004
    )
    assert diracx.summary(["Status"]) == [{"Status": "Received", "count": 5}]
    made = diracx.submit_jdl(['[Executable = "y";]'])
    assert made[0]["JobID"] == 1005
    assert diracx.set_status([1005, 9], "Killed", "by me", force=True)["failed"] == {
        "9": {"detail": "not found"}
    }
    assert dirac.jobs[1005].status == "Killed"
    assert diracx.reschedule([1005])["Value"] == [1005]


def test_sandboxes(dirac: FakeDirac, session: DiracSession) -> None:
    diracx = session.diracx
    packed = pack([], extra={"a.txt": b"a"})
    pfn = diracx.upload_sandbox(packed)
    assert pfn.startswith("SB:SandboxSE|/S3/") and diracx.upload_sandbox(packed) == pfn
    assert diracx.download_sandbox(pfn) == packed.data
    with pytest.raises(ServiceError, match="no such sandbox"):
        diracx.download_sandbox("/S3/nope")
    dirac.https.add_fault(Fault("POST", "sandbox", status=200, body=b'{"pfn": "/S3/x/y"}'))
    assert diracx.upload_sandbox(packed) == "SB:SandboxSE|/S3/x/y"


def test_diracx_failures(dirac: FakeDirac, session: DiracSession) -> None:
    diracx = session.diracx
    dirac.https.add_fault(Fault("GET", "userinfo", status=403, body=b'{"detail": "no"}'))
    with pytest.raises(AuthorizationError, match="HTTP 403: no"):
        diracx.whoami()
    dirac.https.add_fault(Fault("GET", "userinfo", status=500, body=b"not json"))
    with pytest.raises(WireError, match="not JSON"):
        diracx.whoami()
    assert "HTTP 418" in str(_failure(418, None, "GET", "/x"))
    assert '{"a": 1}' in str(_failure(422, {"detail": {"a": 1}}, "GET", "/x"))
    assert _more("jobs 0-1/*", 2) and not _more("", 2) and not _more("jobs 0-1/2", 2)
    body, kind = _form({"k": "v"}, b"data")
    assert b'name="k"\r\n\r\nv' in body and b"\r\n\r\ndata\r\n--" in body and "boundary=" in kind
    dirac.https.add_fault(Fault("POST", "sandbox", status=200, body=b'{"url": "x"}', times=1))
    with pytest.raises(WireError, match="no pfn"):
        diracx.upload_sandbox(pack([], extra={"b": b"b"}))
    dirac.https.add_fault(Fault("GET", "pfn=", status=200, body=b'{"expires_in": 1}'))
    with pytest.raises(WireError, match="download URL"):
        diracx.download_sandbox("/S3/whatever")


# -- jobs --------------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["dirac9", "dirac8"])
def test_jobs(
    shape: str, request: pytest.FixtureRequest, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake: FakeDirac = request.getfixturevalue("dirac" if shape == "dirac9" else "dirac8")
    session: DiracSession = request.getfixturevalue("session" if shape == "dirac9" else "session8")
    jobs = session.jobs
    local = tmp_path / "in.txt"
    local.write_text("x")
    jdl = JDL(
        {
            "Executable": "x",
            "JobName": "a~k1",
            "JobGroup": "g",
            "InputSandbox": [str(local), "LFN:/v/f"],
        }
    )
    one = jobs.submit(jdl, extra={"data.py": b"d"})
    assert len(one.job_ids) == 1 and one.sandbox.startswith("SB:SandboxSE|")
    assert fake.jobs[one.job_ids[0]].jdl.strings("InputSandbox") == ["LFN:/v/f", one.sandbox]
    many = jobs.submit('[Executable = "x"; Parameters = 3; JobName = "b";]')
    assert len(many.job_ids) == 3 and "confirmBulkSubmission" in fake.diset.methods()
    fake.require_proxy_upload = True
    with caplog.at_level(logging.WARNING):
        assert jobs.submit('[Executable = "x";]').require_proxy_upload
    assert "lhcb-proxy-init" in caplog.text
    ids = [one.job_ids[0], *many.job_ids]
    known = jobs.status([*ids, 99])
    assert sorted(known) == sorted(ids) and known[ids[0]].group == "g" and not known[ids[0]].final
    assert [j.job_id for j in jobs.find(name="a~%")] == [ids[0]]
    assert [j.job_id for j in jobs.find(name="b")] == list(reversed(many.job_ids))
    assert (
        len(jobs.find(status=["Received"], group="g", site="ANY", since=dt.datetime(2020, 1, 1)))
        == 1
    )
    assert jobs.summary() == [{"Status": "Received", "count": 5}]
    assert jobs.username() == "tuser"
    fake.jobs[ids[0]].parameters = {"CPU": 1}
    assert jobs.parameters([ids[0]]) == {ids[0]: {"CPU": 1}}
    assert jobs.logging(ids[0])[0][0] == "Received" and "Executable" in jobs.jdl(ids[0])
    assert [(r.job_id, r.ok) for r in jobs.kill([ids[1], 5])] == [(ids[1], True), (5, False)]
    assert all(r.ok for r in jobs.delete([ids[2]])) and all(r.ok for r in jobs.reschedule([ids[3]]))
    assert jobs.kill([]) == []
    with pytest.raises(DiracError, match="no output sandbox"):
        jobs.output(ids[0], tmp_path / "o")
    fake.finish(ids[0], files={"out.txt": b"o"})
    assert jobs.sandboxes(ids[0])["Output"] and jobs.output(ids[0], tmp_path / "o") == ["out.txt"]
    assert jobs.output(ids[0], tmp_path / "i", kind="input") == ["data.py", "in.txt"]


def test_job_failures(dirac8: FakeDirac, session8: DiracSession) -> None:
    jobs = session8.jobs
    dirac8.diset.rpc(
        "WorkloadManagement/JobManager", "killJob", lambda ids, _f: {"OK": False, "Message": "down"}
    )
    assert [r.reason for r in jobs.kill([1, 2])] == ["down", "down"]
    dirac8.diset.rpc(
        "WorkloadManagement/SandboxStore",
        "getSandboxesAssignedToEntity",
        lambda _e: {"Output": ["SB:SITE-DISK|/x"]},
    )
    with pytest.raises(ConfigError, match="no dips://"):
        jobs.output(1, "/tmp/unused")
    dirac8.diset.on_upload = lambda *_: 5
    with pytest.raises(WireError, match="location"):
        jobs.submit('[Executable = "x"; InputSandbox = {"LFN:/a"};]', extra={"a": b"a"})
    dirac8.diset.rpc(
        "WorkloadManagement/JobManager",
        "submitJob",
        lambda _t: (_ for _ in ()).throw(Refuse("bad JDL")),
    )
    with pytest.raises(ServiceError, match="bad JDL"):
        jobs.submit('[Executable = "x";]')
    text = dirac8.configuration().replace("DN = /", "DN = /nobody")
    session8._built["config"] = DiracConfig.from_text(text, setup="TestSetup")
    with pytest.raises(ConfigError, match="no user with the DN"):
        jobs.username()


def test_fake_refusals(dirac: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    with pytest.raises(ServiceError, match="Invalid job JDL"):
        session.jobs.submit('[JobName = "no executable";]')
    with pytest.raises(ServiceError, match="not found"):
        session.jobs.logging(99)
    with pytest.raises(ServiceError, match="HTTP 404"):
        session.diracx.job_sandboxes(99)
    assert session.bookkeeping.summary("/A/B/C/D/12345678/F").files == 0
    response = session.http.request("GET", dirac.https_url + "/api/auth/userinfo")
    assert response.status == 401 and b"Not authenticated" in response.body()
    job = session.jobs.submit('[Executable = "x";]').job_ids[0]
    dirac.finish(job)
    assert session.jobs.status([job])[job].status == "Done"


def test_newer_configuration(dirac: FakeDirac, tmp_path: Path) -> None:
    with dirac.session(tmp_path) as first:
        assert first.config.version == dirac.config_version
    dirac.config_version = "2027-01-01 00:00:00.000001"
    with dirac.session(tmp_path) as second:
        assert second.config.version == "2027-01-01 00:00:00.000001"
    with FakeDirac(dirac.pki) as other:
        assert other.diset.start() is other.diset


def test_vo_from_voms(session: DiracSession, monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    entries = (types.SimpleNamespace(vo=""), types.SimpleNamespace(vo="lhcb"))
    monkeypatch.setattr(session, "proxy_info", lambda: types.SimpleNamespace(voms=entries))
    assert session._proxy_vo() == "lhcb"


def test_job_shapes() -> None:
    assert _when(None) is None and _when("None") is None and _when("garbage") is None
    assert _when("2026-01-01T12:00:00Z") == dt.datetime(2026, 1, 1, 12)
    assert _when(dt.datetime(2026, 1, 1)) == dt.datetime(2026, 1, 1)
    assert DiracJob.read({"JobID": "5", "Status": "Done"}).final
    assert _summary_table({"1": {}}) == {"1": {}} and _summary_table("{1: {'Status': 'Done'}}") == {
        1: {"Status": "Done"}
    }
    with pytest.raises(WireError):
        _summary_table("{1: datetime.datetime(2020, 1, 1)}")
    with pytest.raises(ServiceError):
        _summary_table("[1]")
    with pytest.raises(ServiceError):
        _summary_table(3)


# -- the session -------------------------------------------------------------------------------


def test_session(dirac: FakeDirac, tmp_path: Path, pki: Any) -> None:
    with dirac.session(tmp_path) as session:
        assert session.vo == "testvo" and "fake" in repr(session)
        assert session.proxy_info().identity
        assert session.storage("SITE-DISK") is session.storage("SITE-DISK")
        assert session.service("DataManagement/FileCatalog") is session.service(
            "DataManagement/FileCatalog"
        )
        assert session.http is session.http and session.ca_path == str(pki.ca_dir)
        assert session.catalog is session.catalog and session.jobs is session.jobs
        assert session.bookkeeping is session.bookkeeping
    with dirac.session(tmp_path, use_diracx=False) as off:
        assert off.diracx_url == ""
        with pytest.raises(ConfigError, match="no DiracX"):
            _ = off.diracx
    with dirac.session(tmp_path, diracx="https://elsewhere/") as other:
        assert other.diracx_url == "https://elsewhere"


def test_session_vo_from_a_proxy_without_voms(dirac: FakeDirac, tmp_path: Path) -> None:
    with dirac.session(tmp_path, vo="") as session, pytest.raises(ConfigError, match="no VOMS"):
        _ = session.vo


def test_session_defaults(monkeypatch: pytest.MonkeyPatch, grid_env: Any) -> None:
    session = DiracSession("lhcb")
    assert session.proxy_path == str(grid_env.proxy_path) and session.ca_path == str(
        grid_env.ca_dir
    )
    session.close()
    given = DiracSession(dataclasses.replace(session.profile, encoding="dencode"), http=object())  # type: ignore[arg-type]  # never used to send
    assert given.http is not None
    given.close()
