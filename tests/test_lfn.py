"""``lfn:`` storage: the catalog-backed store, and xrdwms staging and output checks through it."""

from __future__ import annotations

import errno
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from xrdwms import Input, JobManager, JobSpec, Output, PollPolicy
from xrdwms.model import FailureKind, JobState

from xrddirac.lfnstore import LFNStore, is_lfn
from xrddirac.session import DiracSession
from xrddirac.testing import FakeDirac, Refuse
from xrddirac.wms import DiracBackend

FAST = PollPolicy(interval=0.01, max_interval=0.05, batch_delay=0.0, backoff_cap=0.05, lease=5.0)
STAGE = "lfn:/testvo/user/t/tuser/xrdwms"
URL = "root://disk.example.org//eos/testvo/grid"


@pytest.fixture
def store(dirac: FakeDirac, session: DiracSession) -> LFNStore:
    return LFNStore(session, dirac.physical, se="SITE-DISK")


def _file(tmp_path: Path, data: bytes = b"hello world\n", name: str = "f.dat") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_upload_registers_and_is_idempotent(
    dirac: FakeDirac, store: LFNStore, tmp_path: Path
) -> None:
    store.copy(str(_file(tmp_path)), "lfn:/testvo/u/f.dat")
    entry = dirac.files["/testvo/u/f.dat"]
    assert entry["Replicas"] == {"SITE-DISK": f"{URL}/testvo/u/f.dat"}
    assert entry["MetaData"]["Size"] == 12 and entry["MetaData"]["Checksum"] == "1e720467"
    assert dirac.physical.files[f"{URL}/testvo/u/f.dat"] == b"hello world\n"
    assert store.exists("lfn:/testvo/u/f.dat") and store.size("LFN:/testvo/u/f.dat") == 12
    assert store.checksum("lfn:/testvo/u/f.dat") == "1e720467"
    dirac.physical.fail_copy = True
    store.copy(str(_file(tmp_path)), "lfn:/testvo/u/f.dat")  # the same bytes: nothing to do
    with pytest.raises(FileExistsError, match="other contents"):
        store.copy(str(_file(tmp_path, b"different")), "lfn:/testvo/u/f.dat")
    assert "LFNStore(se='SITE-DISK'" in repr(store) and is_lfn("LFN:/x") and not is_lfn("/x")


def test_failed_registration_leaves_no_dark_data(
    dirac: FakeDirac, store: LFNStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dirac.refuse_register["/testvo/u/g.dat"] = "quota exceeded"
    with pytest.raises(OSError, match="quota exceeded"):
        store.copy(str(_file(tmp_path)), "lfn:/testvo/u/g.dat")
    assert dirac.physical.files == {} and "/testvo/u/g.dat" not in dirac.files
    dirac.physical.fail_remove = True
    with caplog.at_level(logging.WARNING), pytest.raises(OSError, match="quota"):
        store.copy(str(_file(tmp_path)), "lfn:/testvo/u/g.dat")
    assert "after a failed registration" in caplog.text
    dirac.diset.rpc(
        "DataManagement/FileCatalog", "addFile", lambda _e: (_ for _ in ()).throw(Refuse("down"))
    )
    with pytest.raises(OSError, match=r"registering /testvo/u/h\.dat") as raised:
        store.copy(str(_file(tmp_path)), "lfn:/testvo/u/h.dat")
    assert raised.value.errno == errno.EIO


def test_where_uploads_go(dirac: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    source = str(_file(tmp_path))
    for se, words in (
        (None, "No upload SE"),
        ("NOPE", "No storage element"),
        ("SandboxSE", "no remote"),
    ):
        with pytest.raises(OSError, match=words) as raised:
            LFNStore(session, dirac.physical, se=se).copy(source, "lfn:/testvo/u/x")
        assert raised.value.errno == errno.EINVAL
    https = LFNStore(session, dirac.physical, se="SITE-DISK", scheme="https")
    https.copy(source, "lfn:/testvo/u/https.dat")
    assert dirac.files["/testvo/u/https.dat"]["Replicas"]["SITE-DISK"].startswith(
        "https://disk.example.org:8443/"
    )
    with pytest.raises(OSError, match="one side must be local") as raised:
        https.copy("root://a//b", "lfn:/testvo/u/z")
    assert raised.value.errno == errno.ENOTSUP


def test_output_checks(dirac: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    job = session.jobs.submit('[Executable = "x";]').job_ids[0]
    dirac.finish(job, output_data={"/testvo/out/a.root": b"abc"})
    catalog = LFNStore(session, dirac.physical)
    strict = LFNStore(session, dirac.physical, verify="replica")
    assert catalog.exists("lfn:/testvo/out/a.root") and strict.exists("lfn:/testvo/out/a.root")
    assert not catalog.exists("lfn:/testvo/out/none.root")
    dirac.physical.files[f"{URL}/testvo/out/a.root"] = b"abd"  # same size, other bytes
    assert catalog.exists("lfn:/testvo/out/a.root") and not strict.exists("lfn:/testvo/out/a.root")
    dirac.physical.files[f"{URL}/testvo/out/a.root"] = b"abcd"
    assert not strict.exists("lfn:/testvo/out/a.root")
    dirac.files["/testvo/out/a.root"]["MetaData"]["Checksum"] = ""
    dirac.physical.files[f"{URL}/testvo/out/a.root"] = b"abd"
    assert strict.exists("lfn:/testvo/out/a.root")
    with pytest.raises(OSError, match="records no checksum"):
        catalog.checksum("lfn:/testvo/out/a.root")
    with pytest.raises(OSError, match="not md5") as raised:
        catalog.checksum("lfn:/testvo/out/a.root", "md5")
    assert raised.value.errno == errno.ENOTSUP
    dirac.add_file("/testvo/out/empty", replicas={})
    assert not catalog.exists("lfn:/testvo/out/empty")
    dirac.add_file("/testvo/out/odd", replicas={"NOT-AN-SE": ""})
    with pytest.raises(OSError, match="no replica with a usable URL"):
        strict.exists("lfn:/testvo/out/odd")
    del dirac.files["/testvo/out/odd"]["MetaData"]["Size"]
    assert not catalog.exists("lfn:/testvo/out/odd")
    with pytest.raises(OSError, match="records no size"):
        catalog.size("lfn:/testvo/out/odd")
    with pytest.raises(FileNotFoundError):
        catalog.size("lfn:/testvo/out/none")
    dirac.diset.rpc(
        "DataManagement/FileCatalog",
        "getReplicas",
        lambda *_a: (_ for _ in ()).throw(Refuse("down")),
    )
    with pytest.raises(OSError, match="looking up"):
        catalog.exists("lfn:/testvo/out/a.root")


def test_download_and_remove(dirac: FakeDirac, store: LFNStore, tmp_path: Path) -> None:
    url = dirac.register("/testvo/out/b.root", b"bytes")
    store.copy("lfn:/testvo/out/b.root", str(tmp_path / "deep" / "b.root"))
    assert (tmp_path / "deep" / "b.root").read_bytes() == b"bytes"
    dirac.files["/testvo/out/b.root"]["Replicas"]["NOT-AN-SE"] = ""
    dirac.files["/testvo/out/b.root"]["Replicas"]["OTHER-DISK"] = ""  # its file is already gone
    store.remove("lfn:/testvo/out/b.root")
    assert "/testvo/out/b.root" not in dirac.files and url not in dirac.physical.files
    store.remove("lfn:/testvo/out/b.root")  # not registered: nothing to do
    dirac.register("/testvo/out/c.root", b"c")
    dirac.diset.rpc(
        "DataManagement/FileCatalog", "removeFile", lambda _l: {"Successful": {}, "Failed": {}}
    )
    with pytest.raises(OSError, match=r"unregistering /testvo/out/c\.root: refused"):
        store.remove("lfn:/testvo/out/c.root")


def test_physical_storage_without_remove(dirac: FakeDirac, session: DiracSession) -> None:
    class ReadOnly:
        def size(self, url: str) -> int:
            return 0

        def checksum(self, url: str, algorithm: str = "adler32") -> str:
            return ""

        def copy(self, source: str, target: str) -> None:
            return None

    dirac.register("/testvo/out/d.root", b"d")
    with pytest.raises(OSError, match="cannot remove") as raised:
        LFNStore(session, ReadOnly()).remove("lfn:/testvo/out/d.root")  # type: ignore[arg-type]  # no remove(), on purpose
    assert raised.value.errno == errno.ENOTSUP


def _store(backend: DiracBackend, url: str) -> LFNStore:
    found = backend.url_storage(url)
    assert found is not None, url
    return found


def _why(record: Any) -> str:
    assert record.info is not None and record.info.failure is not None
    return str(record.info.failure.message)


# -- through xrdwms --------------------------------------------------------------------------


def _backend(
    fake: FakeDirac, session: DiracSession, tmp_path: Path, **options: Any
) -> DiracBackend:
    options.setdefault("output_grace", 30.0)
    return DiracBackend(
        "d",
        session=session,
        physical=fake.physical,
        upload_se="SITE-DISK",
        workdir=str(tmp_path),
        sandbox_limit=10,
        **options,
    )


@pytest.mark.parametrize("shape", ["dirac9", "dirac8"])
async def test_lfn_staging_and_outputs_through_xrdwms(
    shape: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    fake: FakeDirac = request.getfixturevalue("dirac" if shape == "dirac9" else "dirac8")
    session: DiracSession = request.getfixturevalue("session" if shape == "dirac9" else "session8")
    (tmp_path / "big.dat").write_bytes(b"B" * 100)
    (tmp_path / "small.dat").write_bytes(b"s")
    backend = _backend(fake, session, tmp_path)
    out = "lfn:/testvo/user/t/tuser/out/h.root"
    spec = JobSpec(
        "ana",
        "/bin/sh",
        inputs=(Input("big.dat"), Input("small.dat")),
        outputs=(Output("h.root", out),),
    )
    async with JobManager([backend], policy=FAST, staging=STAGE) as manager:
        [key] = await manager.submit([spec], backend="d")
        [record] = await manager.submitted([key])
        staged = f"{STAGE}/{key}/big.dat"
        assert record.spec.inputs[0].source == staged
        assert fake.files[staged[4:]]["MetaData"]["Size"] == 100
        job = fake.jobs[int(record.handle.id)]  # type: ignore[union-attr]  # submitted
        assert job.jdl.strings("InputSandbox")[0] == "LFN" + staged[3:]
        assert job.jdl["OutputData"] == ["LFN:/testvo/user/t/tuser/out/h.root"]
        fake.finish(job.job_id, files={"std.out": b"ok"})
        for _ in range(500):
            if manager.journal.get(key).state is JobState.STAGING_OUT:
                break
            await _tick()
        assert manager.journal.get(key).state is JobState.STAGING_OUT, "done, waiting for its LFN"
        fake.register(out[4:], b"histograms")  # DIRAC's failover registers it late
        assert (await manager.wait(key)).state is JobState.DONE
        [cleaned] = await manager.clean([key])
        assert cleaned.ok and staged[4:] not in fake.files
        assert not any("/xrdwms/" in url for url in fake.physical.files)


async def test_an_lfn_output_never_registered_fails(
    dirac: FakeDirac, session: DiracSession, tmp_path: Path
) -> None:
    backend = _backend(dirac, session, tmp_path, output_grace=0.05, verify="replica")
    spec = JobSpec("ana", "/bin/sh", outputs=(Output("never.root", "lfn:/testvo/out/never.root"),))
    async with JobManager([backend], policy=replace(FAST, max_submit_tries=1)) as manager:
        [key] = await manager.submit([spec], backend="d")
        [record] = await manager.submitted([key])
        dirac.finish(int(record.handle.id))  # type: ignore[union-attr]  # submitted
        record = await manager.wait(key)
    assert record.state is JobState.FAILED and record.info.failure.kind is FailureKind.DATA  # type: ignore[union-attr]  # failed
    assert "nothing at lfn:/testvo/out/never.root" in record.info.failure.message  # type: ignore[union-attr]  # failed


async def test_a_failed_upload_is_a_failed_submission(
    dirac: FakeDirac, session: DiracSession, tmp_path: Path
) -> None:
    (tmp_path / "big.dat").write_bytes(b"B" * 100)
    dirac.physical.fail_copy = True
    backend = _backend(dirac, session, tmp_path)
    async with JobManager(
        [backend], policy=replace(FAST, max_submit_tries=1), staging=STAGE
    ) as manager:
        [key] = await manager.submit([JobSpec("a", "x", inputs=(Input("big.dat"),))], backend="d")
        record = await manager.wait(key)
    assert record.state is JobState.FAILED and "Could not stage big.dat" in _why(record)
    assert not dirac.jobs


def test_url_storage_hook(dirac: FakeDirac, session: DiracSession, tmp_path: Path) -> None:
    backend = _backend(dirac, session, tmp_path)
    assert backend.url_storage("davs://se/x") is None
    first = backend.url_storage("lfn:/x")
    assert first is backend.url_storage("LFN:/y") and first is not None and first.se == "SITE-DISK"
    assert backend.capabilities.sandbox_limit == 10 and backend.capabilities.output_grace == 30.0
    profiled = DiracBackend(
        "d", session=dirac.session(tmp_path / "p", extra={"upload_se": "ALIAS-DISK"})
    )
    assert _store(profiled, "lfn:/x").se == "ALIAS-DISK"
    plain = DiracBackend("d", session=session)
    assert type(_store(plain, "lfn:/x").physical).__name__ == "GridStorage"
    dirac.add_file("/testvo/out/sandboxed", replicas={"SandboxSE": ""})
    with pytest.raises(OSError, match="no replica with a usable URL"):
        LFNStore(session, dirac.physical, scheme="root", verify="replica").exists(
            "lfn:/testvo/out/sandboxed"
        )
    LFNStore(session, dirac.physical, scheme="root").remove("lfn:/testvo/out/sandboxed")
    assert "/testvo/out/sandboxed" not in dirac.files
    answer = session.service("DataManagement/FileCatalog").call(
        "addFile",
        {"/testvo/out/twice": {"PFN": "p", "SE": "SITE-DISK", "Size": 1, "Checksum": "1"}},
    )
    assert answer["Successful"] and session.service("DataManagement/FileCatalog").call(
        "addFile",
        {"/testvo/out/twice": {"PFN": "p", "SE": "SITE-DISK", "Size": 1, "Checksum": "1"}},
    )["Failed"] == {"/testvo/out/twice": "File already exists"}


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.01)
