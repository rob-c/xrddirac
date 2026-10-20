"""The DIRAC configuration: one snapshot of the Configuration Service's tree, cached on disk.

Everything a client needs to know about an installation - every service URL,
every storage element and its protocols, the registry, DiracX's URL - is in
the Configuration Service's tree, which it hands out whole
(``getCompressedData``: zlib-compressed ``.cfg`` text, ~0.3 MB for LHCb).
:func:`load` fetches it once, keeps it in ``$XDG_CACHE_HOME/xrddirac/<profile>/``
(``~/.cache``) and, once the copy is older than the profile's
``config_max_age``, asks the server for a newer one
(``getCompressedDataIfNewer``), so a command run every few seconds costs no
download. A server that cannot be reached leaves the stale copy in use, with a
warning, rather than failing a read-only query.

The cached copy holds the registry (users' DNs), so it is written ``0600``
like a credential, though nothing in it is secret.

Two layouts are read:

* DIRAC 9 (LHCb): ``/Systems/<System>/URLs/<Service>``;
* DIRAC 8 and older (GridPP): ``/DIRAC/Setups/<setup>/<System>`` names an
  *instance*, then ``/Systems/<System>/<instance>/URLs/<Service>``.

:meth:`DiracConfig.operations` reads the ``/Operations`` tree with DIRAC's
precedence: the VO's own section first, the setup's before ``Defaults``.
"""

from __future__ import annotations

import os
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xrdcore.log import get_logger
from xrdsec import credentials as _credentials

from .cfg import Section, parse, split_list
from .errors import ConfigError, NoSuchServiceError, WireError
from .profiles import Profile

__all__ = ["DiracConfig", "cache_dir", "load"]

_log = get_logger(__name__)

_CACHE_NAME = "configuration.cfg"
_VERSION_NAME = "configuration.version"


@dataclass(frozen=True)
class DiracConfig:
    """One configuration snapshot, read; see the module docstring."""

    root: Section
    version: str
    setup: str = "None"

    @classmethod
    def from_text(cls, text: str, *, setup: str = "None") -> DiracConfig:
        """A snapshot from ``.cfg`` text (its version from ``/DIRAC/Configuration/Version``)."""
        root = parse(text)
        return cls(root, root.get("DIRAC/Configuration/Version") or "", setup)

    # -- values ----------------------------------------------------------------------------

    def get(self, path: str, default: str | None = None) -> str | None:
        """The option at ``path``."""
        return self.root.get(path, default)

    def values(self, path: str) -> list[str]:
        """The list option at ``path``."""
        return self.root.values(path)

    @property
    def diracx_url(self) -> str:
        """DiracX's base URL (``/DiracX/URL``), or ``""`` where there is none."""
        return (self.get("DiracX/URL") or "").rstrip("/")

    def instance(self, system: str) -> str | None:
        """DIRAC 8's instance of ``system`` for this setup, or ``None`` on DIRAC 9."""
        if self.setup and self.setup != "None":
            named = self.get(f"DIRAC/Setups/{self.setup}/{system}")
            if named:
                return named
        setups = self.root.names("DIRAC/Setups")
        if len(setups) == 1:
            return self.get(f"DIRAC/Setups/{setups[0]}/{system}")
        return None

    def system_section(self, system: str) -> Section | None:
        """``/Systems/<system>`` - or its instance's section on DIRAC 8."""
        instance = self.instance(system)
        if instance is not None:
            found = self.root.section(f"Systems/{system}/{instance}")
            if found is not None:
                return found
        return self.root.section(f"Systems/{system}")

    def service_urls(self, service: str, *, schemes: tuple[str, ...] = ("dips",)) -> list[str]:
        """``System/Component``'s URLs with one of ``schemes``; ``NoSuchServiceError`` if none."""
        system, _, component = service.partition("/")
        section = self.system_section(system)
        urls = split_list(section.get(f"URLs/{component}")) if section is not None else []
        wanted = [url for url in urls if url.partition("://")[0] in schemes]
        if not wanted:
            raise NoSuchServiceError(
                f"The configuration names no {'/'.join(schemes)} URL for {service}"
            )
        return wanted

    def service_option(self, service: str, option: str) -> str | None:
        """An option of a service's own section (``Services/<Component>/<option>``)."""
        system, _, component = service.partition("/")
        section = self.system_section(system)
        return section.get(f"Services/{component}/{option}") if section is not None else None

    def operations(self, path: str, vo: str = "") -> str | None:
        """``/Operations/...`` with DIRAC's precedence: the VO, the setup, then ``Defaults``."""
        places = []
        setup = self.setup if self.setup and self.setup != "None" else ""
        if vo:
            places += [f"Operations/{vo}/{setup}" if setup else "", f"Operations/{vo}/Defaults"]
        places += [f"Operations/{setup}" if setup else "", "Operations/Defaults"]
        for place in places:
            if place:
                value = self.get(f"{place}/{path}")
                if value is not None:
                    return value
        return None

    def vo_for_group(self, group: str) -> str | None:
        """The VO a DIRAC group belongs to (``/Registry/Groups/<group>/VO``)."""
        return self.get(f"Registry/Groups/{group}/VO") or self.get("Registry/DefaultVO")


# -- loading ---------------------------------------------------------------------------------


def cache_dir(profile: str, environ: Mapping[str, str] | None = None) -> Path:
    """Where ``profile``'s configuration snapshot is kept."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base, "xrddirac", profile)


#: ``(method, *args) -> value``: one call to the Configuration Service.
Caller = Callable[..., Any]


def load(
    profile: Profile,
    call: Caller,
    *,
    directory: Path | None = None,
    refresh: bool = False,
    now: Callable[[], float] = time.time,
) -> DiracConfig:
    """The configuration for ``profile``: the cached copy, refreshed as needed; see above."""
    where = directory or cache_dir(profile.name)
    cached, version = _read_cache(where)
    age = now() - (where / _CACHE_NAME).stat().st_mtime if cached is not None else None
    if cached is not None and age is not None and age < profile.config_max_age and not refresh:
        return DiracConfig.from_text(cached, setup=profile.setup)
    try:
        fresh = _fetch(call, version) if cached is not None else _inflate(call("getCompressedData"))
    except OSError as exc:
        if cached is None:
            raise
        _log.warning("Using the cached DIRAC configuration: the server did not answer (%s)", exc)
        return DiracConfig.from_text(cached, setup=profile.setup)
    if fresh is None:
        os.utime(where / _CACHE_NAME)
        return DiracConfig.from_text(str(cached), setup=profile.setup)
    config = DiracConfig.from_text(fresh, setup=profile.setup)
    _write_cache(where, fresh, config.version)
    return config


def _fetch(call: Caller, version: str) -> str | None:
    """The configuration text if the server's is newer than ``version``, else ``None``."""
    answer = call("getCompressedDataIfNewer", version)
    if not isinstance(answer, dict):
        raise WireError("getCompressedDataIfNewer answered something other than a dict")
    data = answer.get("data")
    return None if data is None else _inflate(data)


def _inflate(data: Any) -> str:
    """The configuration text from its zlib-compressed bytes (JSON) or text (DEncode)."""
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogateescape")
    if not isinstance(data, bytes):
        raise WireError("The configuration is not compressed bytes")
    try:
        return zlib.decompress(data).decode("utf-8", "replace")
    except zlib.error as exc:
        raise WireError(f"The configuration does not decompress: {exc}") from exc


def _read_cache(where: Path) -> tuple[str | None, str]:
    """The cached text and its version, or ``(None, "")``: a cache that cannot be read is none."""
    try:
        text = (where / _CACHE_NAME).read_text()
        version = (where / _VERSION_NAME).read_text().strip()
    except OSError:
        return None, ""
    return text, version


def _write_cache(where: Path, text: str, version: str) -> None:
    """Keep the snapshot (``0600``); a cache that cannot be written is only a warning."""
    try:
        where.mkdir(parents=True, exist_ok=True, mode=0o700)
        _credentials.write_private_file(where / _CACHE_NAME, text.encode(), kind="configuration")
        _credentials.write_private_file(
            where / _VERSION_NAME, version.encode(), kind="configuration"
        )
    except OSError as exc:
        _log.warning("Cannot keep the DIRAC configuration in %s: %s", where, exc)


def require(value: str | None, what: str) -> str:
    """``value``, or :class:`ConfigError` saying ``what`` is not configured."""
    if not value:
        raise ConfigError(f"{what} is not configured")
    return value
