"""xrddirac: a light, pure Python 3 DIRAC client, built on the xrd* grid libraries.

It speaks DIRAC's own protocols - DISET (``dips://``, :mod:`xrddirac.diset`)
and DiracX's REST API (:mod:`xrddirac.diracx`) - with nothing of DIRAC
installed: the X.509 proxy is the only credential, :mod:`xrdsec` does the
TLS, :mod:`xrdhttp` the HTTP. One :class:`DiracSession` is one installation
(LHCb's or GridPP's DIRAC, or another profile)::

    from xrddirac import DiracSession

    with DiracSession("lhcb") as dirac:
        replicas = dirac.catalog.replicas(lfns)
        online = dirac.replica_status.check(replicas.successful)
        files = dirac.bookkeeping.files("/LHCb/Collision24/.../B2CC.DST", runs=(299632,))
        job = dirac.jobs.submit("[Executable = \\"/bin/hostname\\";]")

Jobs as dask tasks come from xrdwms, for which :mod:`xrddirac.wms` is the
``dirac`` backend; :mod:`xrddirac.lhcb` sketches the Ganga-like layer on top.
"""

from ._version import __version__
from .catalog import Bulk, FileCatalog
from .errors import (
    AuthorizationError,
    ConfigError,
    DiracError,
    ServiceError,
    TooLargeError,
    WireError,
)
from .jdl import JDL
from .profiles import Profile, load_profile
from .session import DiracSession

__all__ = [
    "JDL",
    "AuthorizationError",
    "Bulk",
    "ConfigError",
    "DiracError",
    "DiracSession",
    "FileCatalog",
    "Profile",
    "ServiceError",
    "TooLargeError",
    "WireError",
    "__version__",
    "load_profile",
]
