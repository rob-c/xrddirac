"""Against the real LHCb DIRAC, read-only: run with ``XRDDIRAC_INTEROP=1`` and an lhcb VOMS proxy.

Nothing here writes to DIRAC: configuration, catalog, Bookkeeping, RSS and
tape-status reads, and a DiracX ``whoami`` and job search. Datasets change, so
assertions are about shape, not exact contents.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from xrddirac.errors import TooLargeError
from xrddirac.session import DiracSession

pytestmark = [pytest.mark.interop, pytest.mark.ambient]

DST = "/lhcb/LHCb/Collision24/B2CC.DST/00278103/0023/00278103_00230015_1.b2cc.dst"
RAW = "/lhcb/data/2018/RAW/FULL/LHCb/COLLISION18/211932/211932_0000000020.raw"
DATASET = (
    "/LHCb/Collision24/Beam6800GeV-VeloClosed-MagDown/Real Data/Sprucing24c2a/94000000/B2CC.DST"
)


@pytest.fixture(scope="module")
def lhcb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DiracSession]:
    with DiracSession("lhcb", cache_dir=tmp_path_factory.mktemp("cs")) as session:
        yield session


def test_configuration(lhcb: DiracSession) -> None:
    assert lhcb.config.version and lhcb.diracx_url.startswith("https://")
    assert lhcb.config.service_urls("DataManagement/FileCatalog")


def test_catalog_and_urls(lhcb: DiracSession) -> None:
    replicas = lhcb.catalog.replicas([DST, RAW, "/lhcb/no/such/file"])
    assert set(replicas.successful) == {DST, RAW} and "/lhcb/no/such/file" in replicas.failed
    meta = lhcb.catalog.metadata([DST]).successful[DST]
    assert meta.size and meta.checksum_type == "Adler32"
    for se in replicas.successful[DST]:
        urls = lhcb.storage(se).urls(DST).values()
        assert all(url.startswith(("root://", "https://")) for url in urls)


def test_urls_still_match_dirac(lhcb: DiracSession) -> None:
    oracle = json.loads((Path(__file__).parent / "data" / "lhcb_urls.json").read_text())
    for key, want in oracle["urls"].items():
        se, scheme = key.split("|")
        assert lhcb.storage(se).url(oracle["lfn"], scheme) == want, key


def test_bookkeeping(lhcb: DiracSession) -> None:
    assert lhcb.bookkeeping.summary(DATASET).files > 1000
    with pytest.raises(TooLargeError):
        lhcb.bookkeeping.files(DATASET, limit=100)
    files = lhcb.bookkeeping.files(DATASET, runs=(299632,))
    assert files and all(f.run == 299632 for f in files.values())
    assert lhcb.bookkeeping.file_metadata([DST]).successful[DST].file_type == "B2CC.DST"


def test_replica_status(lhcb: DiracSession) -> None:
    replicas = lhcb.catalog.replicas([DST, RAW]).successful
    found = lhcb.replica_status.check(replicas)
    assert found[DST].verdict == "ONLINE"
    assert found[RAW].verdict in ("NEARLINE", "ONLINE", "UNKNOWN")
    status = lhcb.replica_status.se_status(["CERN-USER"])
    assert set(status.values()) <= {"Active", "Degraded", "Banned", "Probing"}


def test_diracx_reads(lhcb: DiracSession) -> None:
    who = lhcb.diracx.whoami()
    assert who["vo"] == "lhcb" and who["preferred_username"]
    assert isinstance(lhcb.jobs.find(limit=5), list)
