"""One DIRAC installation, as a client sees it: profile, credentials, configuration, services.

:class:`DiracSession` is the object every part of xrddirac is built on::

    from xrddirac import DiracSession

    with DiracSession("lhcb") as dirac:
        replicas = dirac.catalog.replicas(["/lhcb/LHCb/Collision24/..."])
        jobs = dirac.jobs.status([123456789])

Nothing is fetched or opened until it is needed - the proxy is found on the
first call, the configuration read on the first lookup, each service's client
made on its first call - so ``xrddirac --help`` and a command that never
talks to DIRAC cost nothing. Everything built is kept for the session's life
and is safe to use from several threads; :meth:`DiracSession.close` releases
the HTTP connections.

Credentials are the user's X.509 proxy (``$X509_USER_PROXY``, else
``/tmp/x509up_u<uid>``), found by :mod:`xrdsec`; a DiracX token is *made*
from it on demand (:mod:`xrddirac.diracx`) and kept in memory only.
"""

from __future__ import annotations

import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from xrdsec import credentials as _credentials
from xrdsec import tls as _tls
from xrdsec.proxytool import read_proxy_info

from . import config as _config
from .diset import DisetClient, Opener
from .errors import ConfigError
from .profiles import Profile, load_profile
from .storage import StorageElement, resolve

if TYPE_CHECKING:
    from xrdhttp.session import Session as HTTPSession
    from xrdsec.proxies import ProxyInfo

    from .bookkeeping import Bookkeeping
    from .catalog import FileCatalog
    from .diracx import DiracX
    from .jobs import JobClient
    from .status import ReplicaStatusService

__all__ = ["DiracSession"]

T = TypeVar("T")


class DiracSession:
    """One DIRAC installation; see the module docstring.

    ``profile`` is a profile name (``lhcb``, ``gridpp``, one from the
    configuration file) or a :class:`Profile`. ``proxy`` and ``ca_path``
    override where the proxy and the grid CAs are found. ``cache_dir`` is
    where the configuration snapshot is kept, ``refresh_config`` asks for a
    newer one now. ``opener`` and ``http`` replace the network for tests.
    """

    def __init__(
        self,
        profile: str | Profile | None = None,
        *,
        proxy: str | None = None,
        ca_path: str | None = None,
        cache_dir: Path | None = None,
        refresh_config: bool = False,
        timeout: float = 600.0,
        opener: Opener | None = None,
        http: HTTPSession | None = None,
    ) -> None:
        self.profile = profile if isinstance(profile, Profile) else load_profile(profile)
        self._proxy = proxy
        self._ca_path = ca_path
        self._cache_dir = cache_dir
        self._refresh = refresh_config
        self.timeout = timeout
        self._opener = opener
        self._http = http
        self._owns_http = http is None
        self._lock = threading.RLock()
        self._built: dict[str, Any] = {}
        self._clients: dict[str, DisetClient] = {}
        self._storage: dict[str, StorageElement] = {}

    def __enter__(self) -> DiracSession:
        """The session; leaving the block closes it."""
        return self

    def __exit__(self, *exc: object) -> None:
        """:meth:`close`."""
        self.close()

    def __repr__(self) -> str:
        """The profile, and nothing that could be a secret."""
        return f"DiracSession({self.profile.name!r})"

    def close(self) -> None:
        """Release HTTP connections; the session can be used again afterwards."""
        with self._lock:
            http = self._built.pop("http", None)
            status = self._built.get("status")
        if status is not None:
            status.close()
        if http is not None and self._owns_http:
            http.close()

    def _once(self, key: str, build: Callable[[], T]) -> T:
        """``build()`` the first time ``key`` is asked for, the same object after that."""
        with self._lock:
            if key not in self._built:
                self._built[key] = build()
            value: T = self._built[key]
            return value

    # -- credentials -----------------------------------------------------------------------

    @property
    def proxy_path(self) -> str:
        """The proxy file: the one given, ``$X509_USER_PROXY`` or ``/tmp/x509up_u<uid>``."""
        return self._proxy or _credentials.default_proxy_path()

    @property
    def ca_path(self) -> str | None:
        """The grid CA directory (``$X509_CERT_DIR``, ``/etc/grid-security/certificates``)."""
        return self._ca_path or _credentials.grid_ca_path()

    def proxy_info(self) -> ProxyInfo:
        """The proxy, described (identity, time left, VOMS attributes)."""
        return read_proxy_info(self.proxy_path)

    def tls(self) -> ssl.SSLContext:
        """The TLS context presenting the proxy, trusting the grid CAs (DISET, storage)."""
        return _tls.client_context(cert=self.proxy_path, ca_path=self.ca_path)

    def public_tls(self, url: str = "") -> ssl.SSLContext:
        """A context presenting nothing (DiracX, S3 sandboxes): grid and system CAs."""
        return _tls.client_context(ca_path=self.ca_path, system_ca=True)

    @property
    def vo(self) -> str:
        """The VO: the profile's, else the proxy's VOMS VO."""
        if self.profile.vo:
            return self.profile.vo
        return self._once("vo", self._proxy_vo)

    def _proxy_vo(self) -> str:
        """The first VOMS VO in the proxy; :class:`ConfigError` when it carries none."""
        for entry in self.proxy_info().voms:
            if entry.vo:
                return str(entry.vo)
        raise ConfigError(
            f"Profile {self.profile.name!r} names no VO and the proxy has no VOMS attribute: "
            'set vo = "..." in the profile or make a VOMS proxy'
        )

    # -- configuration and services --------------------------------------------------------

    @property
    def config(self) -> _config.DiracConfig:
        """The configuration snapshot (read, or fetched, on first use)."""
        return self._once("config", self._load_config)

    def _load_config(self) -> _config.DiracConfig:
        """Fetch or reuse the snapshot through the Configuration Service."""
        server = self._client("Configuration/Server", list(self.profile.servers))
        return _config.load(
            self.profile, server.call, directory=self._cache_dir, refresh=self._refresh
        )

    def service(self, name: str) -> DisetClient:
        """The DISET client for ``System/Component`` (its URLs from the configuration)."""
        with self._lock:
            client = self._clients.get(name)
        if client is None:
            client = self._client(name, self.config.service_urls(name))
        return client

    def _client(self, name: str, urls: list[str]) -> DisetClient:
        """A client for ``name`` over ``urls``, kept for the session (the first one made wins)."""
        made = DisetClient(
            urls,
            tls=self.tls,
            vo=self.vo,
            setup=self.profile.setup,
            timeout=self.timeout,
            encoding="dencode" if self.profile.encoding == "dencode" else "json",
            opener=self._opener,
        )
        with self._lock:
            return self._clients.setdefault(name, made)

    def storage(self, name: str) -> StorageElement:
        """The storage element ``name``, resolved from the configuration (and kept)."""
        with self._lock:
            found = self._storage.get(name)
        if found is None:
            found = resolve(self.config, name, vo=self.vo)
            with self._lock:
                self._storage[name] = found
        return found

    @property
    def http(self) -> HTTPSession:
        """The pooled HTTP session (DiracX, S3 sandboxes, the tape REST API)."""
        if self._http is not None:
            return self._http
        return self._once("http", self._make_http)

    def _make_http(self) -> HTTPSession:
        """An xrdhttp session presenting nothing by default (a request may bring a proxy)."""
        from xrdhttp.session import Policy, RetryPolicy, Session

        policy = Policy(
            headers={"User-Agent": f"xrddirac/{_version()}"},
            timeout=self.timeout,
            retry=RetryPolicy(retries=2, transport=True),
        )
        return Session(tls=self.public_tls, policy=policy)

    @property
    def diracx_url(self) -> str:
        """DiracX's URL for this installation, or ``""`` when it has none or it is turned off."""
        if not self.profile.use_diracx:
            return ""
        return (self.profile.diracx or self.config.diracx_url).rstrip("/")

    # -- the clients -----------------------------------------------------------------------

    @property
    def catalog(self) -> FileCatalog:
        """The DIRAC File Catalog (read-only use)."""
        from .catalog import FileCatalog

        return self._once("catalog", lambda: FileCatalog(self))

    @property
    def bookkeeping(self) -> Bookkeeping:
        """LHCb's Bookkeeping (datasets by path, file metadata)."""
        from .bookkeeping import Bookkeeping

        return self._once("bookkeeping", lambda: Bookkeeping(self))

    @property
    def replica_status(self) -> ReplicaStatusService:
        """Replica availability: storage status (RSS) and tape locality."""
        from .status import ReplicaStatusService

        return self._once("status", lambda: ReplicaStatusService(self))

    @property
    def diracx(self) -> DiracX:
        """The DiracX client; :class:`ConfigError` where the installation has no DiracX."""
        from .diracx import DiracX

        url = self.diracx_url
        if not url:
            raise ConfigError(f"The {self.profile.name} DIRAC has no DiracX (or it is turned off)")
        return self._once("diracx", lambda: DiracX(self, url))

    @property
    def jobs(self) -> JobClient:
        """Jobs: submit, monitor, kill, delete, reschedule, output."""
        from .jobs import JobClient

        return self._once("jobs", lambda: JobClient(self))


def _version() -> str:
    """The package version (imported late: this module is imported by the CLI's help)."""
    from ._version import __version__

    return __version__
