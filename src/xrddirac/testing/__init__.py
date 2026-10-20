"""Test helpers: an in-process DISET server and a whole fake DIRAC (DISET, DiracX, S3, tape REST).

Stability: test API, versioned with the package. Nothing in the library imports it.
"""

from .dirac import FakeDirac, FakeJob, MemoryStorage, sandbox_bytes
from .diset import Call, FakeDisetServer, Refuse

__all__ = [
    "Call",
    "FakeDirac",
    "FakeDisetServer",
    "FakeJob",
    "MemoryStorage",
    "Refuse",
    "sandbox_bytes",
]
