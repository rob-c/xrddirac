"""Storage elements as the DIRAC configuration describes them, and their URLs for an LFN.

The file catalog stores *which* storage elements hold a replica, and often a
stale PFN (LHCb's still name CASTOR ``srm://`` paths). The URL to read a
replica *now* is built from the storage element's definition, exactly as
DIRAC's ``StorageElement.getURL`` builds it::

    Resources/StorageElements/<SE>         BaseSE = <base>, Alias = <other SE>, overrides
    Resources/StorageElementBases/<base>   SEType, AccessProtocols, protocol sections

A protocol section is a subsection with a ``Protocol`` option (``GFAL2_XROOT``,
``GFAL2_HTTPS``, ``CTA``, ``XRootConfig``...); an SE's section of the same
name overrides the base's options one by one (``CERN-USER`` changes only the
``Path`` of ``CERN-EOS``'s ``GFAL2_XROOT``). The URL is::

    <Protocol>://<Host>[:<Port>]/<Path>/<lfn without its leading slash>

with DIRAC's plugin rules on top: an ``xroot`` URL has ``//`` after the
host (GFAL2_XROOT and Echo add it), an ``srm`` URL carries the ``WSUrl``
(``/srm/managerv2?SFN=``) before the path, and Echo's ``Path = lhcb:prod``
(a Ceph pool, not a directory) is joined without a leading ``/``.

The *kind* of an SE - tape or disk - is its ``SEType``: ``T1D0`` is tape
only (a replica there may need staging), ``T0D1`` disk only, ``T1D1`` both.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from .cfg import Section, split_list
from .config import DiracConfig
from .errors import ConfigError

__all__ = ["DISK", "TAPE", "UNKNOWN_KIND", "Endpoint", "StorageElement", "resolve"]

TAPE, DISK, UNKNOWN_KIND = "tape", "disk", "unknown"

_SE = "Resources/StorageElements"
_BASES = "Resources/StorageElementBases"
#: Endpoints a client off-site can use (``local`` is the site's own network only).
_REMOTE = ("remote", "remoteonly")
#: DIRAC's SEType words, as a kind.
_KINDS = {"T1D0": TAPE, "T0D1": DISK, "T1D1": TAPE}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One protocol section of a storage element."""

    section: str
    plugin: str
    scheme: str
    host: str
    port: str = ""
    path: str = ""
    ws_url: str = ""
    access: str = "remote"
    space_token: str = ""

    @property
    def remote(self) -> bool:
        """Usable from outside the site."""
        return self.access in _REMOTE

    @property
    def base(self) -> str:
        """``scheme://host[:port]``."""
        return f"{self.scheme}://{self.host}{':' + self.port if self.port else ''}"

    def url(self, lfn: str) -> str:
        """The URL of ``lfn`` here, by DIRAC's rules (see the module docstring)."""
        name = lfn.lstrip("/")
        if self.scheme == "srm":
            path = posixpath.normpath(f"/{self.path}/{name}").replace("//", "/")
            return f"{self.base}{self.ws_url}{path}"
        if self.scheme == "file":
            return f"file://{posixpath.join(self.path, name)}"
        joined = posixpath.join(self.path, name) if self.path else name
        if not joined.startswith("/"):
            joined = "/" + joined
        if self.scheme == "root":
            joined = "/" + joined if not joined.startswith("//") else joined
        return f"{self.base}{joined}"


@dataclass(frozen=True, slots=True)
class StorageElement:
    """A storage element, resolved: its kind, endpoints and preferred protocols."""

    name: str
    #: The SE whose definition this is (differs from ``name`` for an ``Alias``).
    real_name: str
    base: str = ""
    se_type: str = ""
    backend: str = ""
    endpoints: tuple[Endpoint, ...] = ()
    #: ``AccessProtocols`` in order: the schemes a read should try first.
    access_protocols: tuple[str, ...] = ()
    #: The SE's own ``ReadAccess`` option (used where the RSS is not).
    read_access: str = ""
    options: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """``tape``, ``disk`` or ``unknown``."""
        return _KINDS.get(self.se_type.upper(), UNKNOWN_KIND)

    @property
    def is_tape(self) -> bool:
        """A replica here may be nearline (on tape only)."""
        return self.kind == TAPE

    def endpoint(self, scheme: str | None = None) -> Endpoint | None:
        """The remote endpoint for ``scheme``, or the first by ``AccessProtocols`` order."""
        remote = [e for e in self.endpoints if e.remote]
        if scheme is not None:
            return next((e for e in remote if e.scheme == scheme), None)
        for wanted in self.access_protocols:
            found = next((e for e in remote if e.scheme == wanted), None)
            if found is not None:
                return found
        return remote[0] if remote else None

    def url(self, lfn: str, scheme: str | None = None) -> str | None:
        """``lfn``'s URL here (by ``scheme``, or the preferred protocol); ``None`` if none."""
        endpoint = self.endpoint(scheme)
        return endpoint.url(lfn) if endpoint is not None else None

    def urls(self, lfn: str) -> dict[str, str]:
        """``lfn``'s URL for every remote scheme here, preferred first."""
        order = list(self.access_protocols) + [e.scheme for e in self.endpoints]
        result: dict[str, str] = {}
        for scheme in order:
            if scheme not in result:
                url = self.url(lfn, scheme)
                if url is not None:
                    result[scheme] = url
        return result


def resolve(config: DiracConfig, name: str, *, vo: str = "") -> StorageElement:
    """The storage element ``name`` from ``config``; :class:`ConfigError` if it is not there."""
    real, section = _definition(config, name)
    base_name = section.options.get("BaseSE", "")
    merged = section
    if base_name:
        base = config.root.section(f"{_BASES}/{base_name}") or config.root.section(
            f"{_SE}/{base_name}"
        )
        if base is None:
            raise ConfigError(f"Storage element {name}: its BaseSE {base_name} is not configured")
        merged = base.merged(section)
    options = merged.options
    protocols = split_list(options.get("AccessProtocols")) or split_list(
        config.operations("DataManagement/AccessProtocols", vo)
    )
    return StorageElement(
        name=name,
        real_name=real,
        base=base_name,
        se_type=options.get("SEType", ""),
        backend=options.get("BackendType", ""),
        endpoints=tuple(_endpoints(merged)),
        access_protocols=tuple(protocols),
        read_access=options.get("ReadAccess", ""),
        options=dict(options),
    )


def _definition(config: DiracConfig, name: str) -> tuple[str, Section]:
    """The SE's own section, following one ``Alias`` (DIRAC allows no chains of them)."""
    section = config.root.section(f"{_SE}/{name}")
    if section is None:
        raise ConfigError(f"No storage element {name} in the DIRAC configuration")
    alias = section.options.get("Alias")
    if alias:
        target = config.root.section(f"{_SE}/{alias}")
        if target is None:
            raise ConfigError(
                f"Storage element {name} is an alias of {alias}, which is not configured"
            )
        return alias, target
    return name, section


def _endpoints(section: Section) -> list[Endpoint]:
    """Every protocol section (a subsection with a ``Protocol``), in order."""
    found = []
    for name, child in section.sections.items():
        options = child.options
        scheme = options.get("Protocol", "")
        if not scheme:
            continue
        found.append(
            Endpoint(
                section=name,
                plugin=options.get("PluginName", name),
                scheme=scheme,
                host=options.get("Host", ""),
                port=options.get("Port", ""),
                path=options.get("Path", ""),
                ws_url=options.get("WSUrl", ""),
                access=options.get("Access", "remote").lower(),
                space_token=options.get("SpaceToken", ""),
            )
        )
    return found
