"""Storage elements and their URLs, the catalog, replica status and the Bookkeeping."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from xrdsec.testing.https import Fault

from xrddirac.bookkeeping import BKFile, DatasetSummary, Query, _names
from xrddirac.catalog import Bulk, FileCatalog, normalise_lfn
from xrddirac.config import DiracConfig
from xrddirac.errors import ConfigError, TooLargeError, WireError
from xrddirac.session import DiracSession
from xrddirac.status import FileAvailability, ReplicaState, ReplicaStatus, _state_of, summarise
from xrddirac.storage import Endpoint, resolve
from xrddirac.testing import FakeDirac

DATA = Path(__file__).parent / "data"

# -- storage -------------------------------------------------------------------------------------


def test_urls_match_dirac() -> None:
    """Every URL DIRAC 9's own StorageElement.getURL built for LHCb, rebuilt the same."""
    config = DiracConfig.from_text((DATA / "lhcb_storage.cfg").read_text())
    oracle = json.loads((DATA / "lhcb_urls.json").read_text())
    for key, want in oracle["urls"].items():
        se, scheme = key.split("|")
        assert resolve(config, se, vo="lhcb").url(oracle["lfn"], scheme) == want, key


def test_storage_kinds_and_choices() -> None:
    config = DiracConfig.from_text((DATA / "lhcb_storage.cfg").read_text())
    raw = resolve(config, "CERN-RAW")
    assert raw.kind == "tape" and raw.is_tape and raw.base == "CERN-CTA" and raw.backend == "Cta"
    assert raw.endpoint().scheme == "https"  # type: ignore[union-attr]  # AccessProtocols = https, root
    glasgow = resolve(config, "Glasgow-DST")
    assert glasgow.kind == "disk" and list(glasgow.urls("/lhcb/x")) == ["root", "https"]
    assert resolve(config, "SARA-RAW").url("/lhcb/x", "root") is None


def test_storage_edge_cases(dirac: FakeDirac) -> None:
    config = DiracConfig.from_text(dirac.configuration())
    alias = resolve(config, "ALIAS-DISK")
    assert alias.name == "ALIAS-DISK" and alias.real_name == "SITE-DISK" and alias.kind == "disk"
    assert alias.url("/v/f") == "root://disk.example.org//eos/testvo/grid/v/f"
    assert resolve(config, "SandboxSE").kind == "unknown"
    assert resolve(config, "SandboxSE").endpoint("dips") is not None
    assert resolve(config, "OTHER-DISK").read_access == "Banned"
    bare = DiracConfig.from_text(
        "Resources\n{\n  StorageElements\n  {\n    X\n    {\n    }\n  }\n}\n"
    )
    assert resolve(bare, "X").url("/f") is None and resolve(bare, "X").endpoint() is None
    for text, words in (
        ("", "No storage element"),
        ("Resources\n{\nStorageElements\n{\nX\n{\nBaseSE = B\n}\n}\n}\n", "BaseSE B"),
        ("Resources\n{\nStorageElements\n{\nX\n{\nAlias = Y\n}\n}\n}\n", "alias of Y"),
    ):
        with pytest.raises(ConfigError, match=words):
            resolve(DiracConfig.from_text(text), "X")
    legacy = "Resources\n{\nStorageElements\n{\nB\n{\nSEType = T0D1\n}\nX\n{\nBaseSE = B\n}\n}\n}\n"
    assert resolve(DiracConfig.from_text(legacy), "X").kind == "disk"


def test_endpoint_url_rules() -> None:
    srm = Endpoint("S", "SRM2", "srm", "h", "8443", "/castor", "/srm/managerv2?SFN=")
    assert srm.url("/lhcb/f") == "srm://h:8443/srm/managerv2?SFN=/castor/lhcb/f"
    assert Endpoint("F", "File", "file", "", path="/data").url("/v/f") == "file:///data/v/f"
    assert Endpoint("H", "H", "https", "h").url("/v/f") == "https://h/v/f"
    assert Endpoint("R", "R", "root", "h", path="//a").url("/v/f") == "root://h//a/v/f"
    assert not Endpoint("L", "L", "file", "", access="local").remote


# -- catalog ---------------------------------------------------------------------------------------


def _files(dirac: FakeDirac) -> list[str]:
    lfns = [f"/testvo/data/run1/f{i}" for i in range(5)]
    for lfn in lfns:
        dirac.add_file(lfn, size=1000, replicas={"SITE-DISK": "pfn", "SITE-TAPE": ""})
    dirac.add_file("/testvo/data/run2/g", replicas={"OTHER-DISK": ""})
    return lfns


def test_catalog(dirac: FakeDirac, session: DiracSession) -> None:
    lfns = _files(dirac)
    catalog = FileCatalog(session, chunk=2)
    assert (
        catalog.replicas(["LFN:" + lfns[0], lfns[1] + "/"]).successful[lfns[0]]["SITE-DISK"]
        == "pfn"
    )
    assert len(catalog.replicas(lfns).successful) == 5
    assert dirac.diset.methods("DataManagement/FileCatalog").count("getReplicas") == 4
    assert catalog.replicas([]).successful == {}
    meta = catalog.metadata([lfns[0], "/nope"])
    assert meta.successful[lfns[0]].size == 1000 and meta.successful[lfns[0]].created is not None
    assert not meta and meta.failed == {"/nope": "No such file or directory"}
    assert catalog.exists(["/testvo/data", "/nope"]).successful == {"/testvo/data": True}
    assert catalog.is_file([lfns[0], "/testvo/data"]).successful == {
        lfns[0]: True,
        "/testvo/data": False,
    }
    listing = catalog.list_directory(["/testvo/data"]).successful["/testvo/data"]
    assert listing.subdirs == ("/testvo/data/run1", "/testvo/data/run2") and not listing.files
    deep = catalog.list_directory(["/testvo/data/run1"], verbose=True).successful[
        "/testvo/data/run1"
    ]
    assert sorted(deep.files) == lfns and deep.replicas[lfns[0]] == {
        "SITE-DISK": "pfn",
        "SITE-TAPE": "",
    }
    assert catalog.directory_size(["/testvo/data"]).successful["/testvo/data"]["LogicalFiles"] == 6


def test_catalog_shapes() -> None:
    assert normalise_lfn(" lfn://a//b/ ") == "/a/b"
    assert Bulk.read({"Successful": {}, "Failed": ["/a"]}, "w").failed == {"/a": "failed"}
    with pytest.raises(WireError):
        Bulk.read([], "w")
    from xrddirac.catalog import DirectoryListing, FileMetadata

    assert FileMetadata.read("/a", {"Size": "x", "Mode": "1"}).size is None
    odd = DirectoryListing.read("/d", {"Files": {"/d/a": True, "b": {"Replicas": {"S": "bad"}}}})
    assert sorted(odd.files) == ["/d/a", "/d/b"] and odd.replicas["/d/b"] == {}


# -- replica status --------------------------------------------------------------------------------


def test_replica_status(dirac: FakeDirac, session: DiracSession) -> None:
    lfns = _files(dirac)
    dirac.tape.update(
        {
            f"/tape{lfns[0]}": "DISK_AND_TAPE",
            f"/tape{lfns[1]}": "DISK",
            f"/tape{lfns[2]}": "TAPE",
            f"/tape{lfns[3]}": "WEIRD",
        }
    )
    replicas = {lfn: {"SITE-TAPE": ""} for lfn in lfns}
    replicas["/testvo/data/run2/g"] = {"OTHER-DISK": "", "NOT-AN-SE": ""}
    found = session.replica_status.check(replicas, scheme="https")
    states = {lfn: str(found[lfn].replicas[0].state) for lfn in lfns}
    assert states == {
        lfns[0]: "ONLINE_AND_NEARLINE",
        lfns[1]: "ONLINE",
        lfns[2]: "NEARLINE",
        lfns[3]: "LOST",
        lfns[4]: "LOST",
    }
    assert found[lfns[0]].verdict == "ONLINE" and found[lfns[2]].verdict == "NEARLINE"
    other = found["/testvo/data/run2/g"]
    assert [str(r.state) for r in other.replicas] == [
        "BANNED",
        "UNKNOWN",
    ] and other.verdict == "UNKNOWN"
    assert summarise(found) == {
        "NEARLINE": 1,
        "ONLINE": 2,
        "UNAVAILABLE": 2,
        "UNKNOWN": 1,
        "files": 6,
    }
    assert (
        session.replica_status.check({lfns[0]: {"SITE-TAPE": ""}}, locality=False)[lfns[0]].verdict
        == "UNKNOWN"
    )
    again = session.replica_status.check({lfns[2]: {"SITE-TAPE": ""}})
    assert again[lfns[2]].verdict == "NEARLINE" and len(session.replica_status._endpoints) == 1
    dirac.se_status["SandboxSE"] = "Active"
    odd = session.replica_status.check({lfns[0]: {"SandboxSE": ""}})[lfns[0]].replicas[0]
    assert odd.state is ReplicaState.UNKNOWN and odd.reason == "SEType unknown"
    assert session.replica_status.se_status([]) == {}
    assert session.replica_status.se_status(["SITE-DISK", "ELSEWHERE"]) == {
        "SITE-DISK": "Active",
        "ELSEWHERE": "Unknown",
    }


def test_tape_endpoint_failures(dirac: FakeDirac, session: DiracSession) -> None:
    dirac.add_file("/testvo/t", replicas={"SITE-TAPE": ""})
    service = session.replica_status
    dirac.tape_https.add_fault(Fault("GET", "wlcg-tape-rest-api", status=404))
    item = service.check({"/testvo/t": {"SITE-TAPE": ""}})["/testvo/t"]
    assert (
        item.replicas[0].state is ReplicaState.UNKNOWN
        and "no tape REST API" in item.replicas[0].reason
    )
    service._endpoints.clear()
    dirac.tape_https.add_fault(Fault("POST", "archiveinfo", drop=True, times=None))
    item = service.check({"/testvo/t": {"SITE-TAPE": ""}})["/testvo/t"]
    assert (
        item.replicas[0].state is ReplicaState.UNKNOWN and dirac.tape_url in item.replicas[0].reason
    )
    service.close()
    service.close()


def test_no_rss_and_no_https(pki: Any, tmp_path: Path) -> None:
    fake = FakeDirac(pki, rss=False).start()
    try:
        with fake.session(tmp_path) as session:
            assert session.replica_status.se_status(["OTHER-DISK", "SITE-DISK", "NOPE"]) == {
                "OTHER-DISK": "Banned",
                "SITE-DISK": "Active",
                "NOPE": "Unknown",
            }
            text = fake.configuration().replace(
                "Protocol = https\n        Path = /tape", "Protocol = root\n        Path = /tape"
            )
            object.__setattr__(session, "_built", {"config": DiracConfig.from_text(text)})
            session._storage.clear()
            item = session.replica_status.check({"/f": {"SITE-TAPE": ""}})["/f"]
            assert item.replicas[0].reason == "tape: no HTTPS endpoint to ask"
    finally:
        fake.stop()


def test_availability_words() -> None:
    def item(*states: ReplicaState) -> FileAvailability:
        return FileAvailability(
            "/f", tuple(ReplicaStatus("/f", f"S{i}", "disk", s) for i, s in enumerate(states))
        )

    assert item().verdict == "UNAVAILABLE" and item(ReplicaState.LOST).verdict == "UNAVAILABLE"
    assert item(ReplicaState.ONLINE).online_sites == ("S0",)
    assert str(ReplicaState.LOST) == "LOST"
    from xrdhttp.tape import FileStatus

    assert _state_of(None)[0] is ReplicaState.UNKNOWN
    unknown = FileStatus("/p", True, False, False, False, False, "", "", "WEIRD")
    assert _state_of(unknown) == (ReplicaState.UNKNOWN, "locality WEIRD")
    assert summarise({"/f": item(ReplicaState.NEARLINE)}) == {"files": 1, "NEARLINE": 1}


# -- bookkeeping -----------------------------------------------------------------------------------

PATH = "/LHCb/Collision24/Beam6800GeV/Real Data/Sprucing24c2a/94000000/B2CC.DST"


def test_bookkeeping(dirac: FakeDirac, session: DiracSession) -> None:
    lfns = [f"/testvo/bk/f{i}" for i in range(4)]
    for lfn in lfns:
        dirac.add_file(lfn, size=10)
    dirac.add_dataset(PATH, lfns[:3], run=7)
    dirac.add_dataset(PATH.replace("B2CC", "BNOC"), lfns[3:], run=8)
    bk = session.bookkeeping
    assert bk.summary(PATH) == DatasetSummary(3, 30, 30, 4.5)
    assert sorted(bk.files(PATH, runs=(7,))) == lfns[:3]
    with pytest.raises(TooLargeError, match="holds 3 files"):
        bk.files(PATH, limit=2)
    assert len(bk.files(Query.from_path(PATH), limit=0)) == 3
    meta = bk.file_metadata([lfns[0], "/nope"])
    assert meta.successful[lfns[0]].run == 7 and meta.failed == {"/nope": "failed"}
    assert bk.file_metadata([]).successful == {}
    assert bk.processing_passes("LHCb", "Collision24", "c") == ["Real Data"]
    assert bk.config_versions("LHCb") == ["Collision24", "Collision25"]


def test_bookkeeping_shapes(session: DiracSession, dirac: FakeDirac) -> None:
    query = Query.from_path(
        PATH, runs=(1, 2), start_run=1, end_run=3, production=5, data_quality=("OK",), visible=False
    )
    assert query.path == PATH and query.processing_pass == "/Real Data/Sprucing24c2a"
    assert query.as_dict()["Visible"] == "N" and query.as_dict()["StartRun"] == 1
    assert {"EndRun", "Production", "DataQuality", "RunNumber"} <= set(query.as_dict())
    for bad in ("/A/B/C/D/E", "/A/B/C/D/1234/F"):
        with pytest.raises(ConfigError):
            Query.from_path(bad)
    with pytest.raises(WireError):
        DatasetSummary.read([])
    assert DatasetSummary.read({"ParameterNames": ["NbofFiles"], "Records": []}).files == 0
    weird = BKFile.read(
        "/f", {"FileSize": "x", "Luminosity": "y", "GotReplica": "No", "DataQuality": "BAD"}
    )
    assert weird.size is None and weird.luminosity is None and weird.got_replica is False
    assert weird.data_quality == "BAD" and BKFile.read("/f", {}).got_replica is None
    assert _names([{"Records": [["a"], []]}, "junk"]) == ["a"]
    dirac.diset.rpc(
        "Bookkeeping/BookkeepingManager", "getVisibleFilesWithMetadata", lambda _q: {"No": 1}
    )
    with pytest.raises(WireError, match="no LFNs"):
        session.bookkeeping.files(PATH, limit=0)
