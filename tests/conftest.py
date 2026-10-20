"""Fixtures shared by every test module.

Every test is hermetic - xrdsec's plugin clears the credential environment,
gives each test an empty ``$HOME`` and an empty ``/tmp`` for proxies, and
mints one session-wide PKI - and :func:`pytest_xrdsec_hermetic` adds
xrddirac's own variables. ``dirac`` is a running :class:`FakeDirac` (DIRAC 9
shape, with DiracX) and ``session`` a :class:`DiracSession` on it; the
``dirac8`` variant has no DiracX and a setup, as GridPP's DIRAC.

Interop tests (``tests/test_interop.py``) are marked ``ambient`` and
``interop``, and run only with ``XRDDIRAC_INTEROP=1``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from xrddirac.session import DiracSession
from xrddirac.testing import FakeDirac

if TYPE_CHECKING:
    from xrdsec.testing.fixtures import Hermetic
    from xrdsec.testing.pki import PKI

pytest_plugins = ["xrdsec.testing.fixtures"]

_OWN = ("XRDDIRAC_CONFIG", "XRDDIRAC_PROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XRDWMS_CONFIG")


def pytest_xrdsec_hermetic(hermetic: Hermetic) -> None:
    """xrddirac's own inputs, on top of xrdsec's."""
    hermetic.unset(*_OWN)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Interop tests talk to the real LHCb DIRAC: skipped unless XRDDIRAC_INTEROP=1."""
    if os.environ.get("XRDDIRAC_INTEROP") == "1":
        return
    skip = pytest.mark.skip(reason="talks to the real LHCb DIRAC; set XRDDIRAC_INTEROP=1")
    for item in items:
        if item.get_closest_marker("interop"):
            item.add_marker(skip)


def _running(pki: PKI, **options: Any) -> Iterator[FakeDirac]:
    fake = FakeDirac(pki, **options).start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def dirac(pki: PKI) -> Iterator[FakeDirac]:
    """A fake DIRAC 9 installation with DiracX."""
    yield from _running(pki)


@pytest.fixture
def dirac8(pki: PKI) -> Iterator[FakeDirac]:
    """A fake DIRAC 8 installation: DISET only, a setup, no DiracX."""
    yield from _running(pki, diracx=False, setup="TestSetup")


@pytest.fixture
def session(dirac: FakeDirac, tmp_path: Path) -> Iterator[DiracSession]:
    """A session on ``dirac``."""
    with dirac.session(tmp_path / "cache") as made:
        yield made


@pytest.fixture
def session8(dirac8: FakeDirac, tmp_path: Path) -> Iterator[DiracSession]:
    """A session on ``dirac8``."""
    with dirac8.session(tmp_path / "cache") as made:
        yield made
