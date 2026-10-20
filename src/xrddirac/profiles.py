"""Profiles: which DIRAC to talk to - its configuration servers, VO and setup.

A DIRAC installation is found the way ``dirac-configure`` finds it: a list of
Configuration Service URLs, from which everything else (every service URL,
every storage element, DiracX) is read. Two are built in::

    lhcb    LHCbDIRAC at CERN (DIRAC 9, DiracX at lhcbdiracx.app.cern.ch)
    gridpp  GridPP's multi-VO DIRAC at Imperial (DIRAC 8, no DiracX)

and more can be added, or the built-in ones changed, in
``$XDG_CONFIG_HOME/xrddirac/config.toml`` (``~/.config``)::

    default = "lhcb"

    [profiles.gridpp]
    vo = "lz"                       # GridPP is multi-VO: the VO you work in

    [profiles.mydirac]
    servers = ["dips://cs.example.org:9135/Configuration/Server"]
    vo = "myvo"
    setup = "MyVO-Production"       # DIRAC 8 and older; DIRAC 9 has no setups

``vo = ""`` (the default for a profile with no VO) takes the VO from the
proxy's VOMS attribute.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

__all__ = ["BUILTIN", "Profile", "config_path", "load_profile", "profile_names"]


@dataclass(frozen=True, slots=True)
class Profile:
    """One DIRAC installation, as a client needs it."""

    name: str
    #: The Configuration Service's ``dips://`` URLs.
    servers: tuple[str, ...]
    #: The VO sent in every proposal; ``""`` means the proxy's VOMS VO.
    vo: str = ""
    #: DIRAC 8's setup (``/DIRAC/Setup``); ``None`` for DIRAC 9.
    setup: str = "None"
    #: DiracX's URL, overriding ``/DiracX/URL`` from the configuration.
    diracx: str = ""
    #: Whether DiracX is used where the configuration offers it.
    use_diracx: bool = True
    #: ``json`` (DIRAC 7.3 and later) or ``dencode``.
    encoding: str = "json"
    #: Seconds a cached configuration is used before asking for a newer one.
    config_max_age: float = 3600.0
    #: Anything else the profile's table held, for tools built on xrddirac.
    extra: Mapping[str, Any] = field(default_factory=dict)


_LHCB_CS = (
    *(
        f"dips://lbvobox{n}.cern.ch:9135/Configuration/Server"
        for n in (300, 301, 302, 303, 304, 305, 306, 307, 308, 309, 310, 311, 312, 313, 315, 316)
    ),
    "dips://lhcb-conf-dirac.cern.ch:9135/Configuration/Server",
)

_GRIDPP_CS = (
    "dips://dirac01.grid.hep.ph.ic.ac.uk:9135/Configuration/Server",
    "dips://dirac02.grid.hep.ph.ic.ac.uk:9135/Configuration/Server",
    "dips://diracweb.grid.hep.ph.ic.ac.uk:9135/Configuration/Server",
)

#: The profiles that need no configuration file.
BUILTIN: dict[str, Profile] = {
    "lhcb": Profile("lhcb", _LHCB_CS, vo="lhcb"),
    "gridpp": Profile("gridpp", _GRIDPP_CS, setup="GridPP"),
}


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    """``$XRDDIRAC_CONFIG``, else ``$XDG_CONFIG_HOME/xrddirac/config.toml``."""
    env = os.environ if environ is None else environ
    if env.get("XRDDIRAC_CONFIG"):
        return Path(env["XRDDIRAC_CONFIG"])
    base = env.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base, "xrddirac", "config.toml")


def _read(path: Path) -> dict[str, Any]:
    """The configuration file's data; a missing file is empty."""
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def profile_names(environ: Mapping[str, str] | None = None) -> list[str]:
    """Every profile there is: the built-in ones and the configured ones."""
    tables = _read(config_path(environ)).get("profiles", {})
    return sorted(set(BUILTIN) | set(tables if isinstance(tables, dict) else {}))


def load_profile(name: str | None = None, environ: Mapping[str, str] | None = None) -> Profile:
    """The profile ``name`` (default: ``$XRDDIRAC_PROFILE``, the file's ``default``, ``lhcb``)."""
    env = os.environ if environ is None else environ
    path = config_path(env)
    data = _read(path)
    chosen = name or env.get("XRDDIRAC_PROFILE") or data.get("default") or "lhcb"
    tables = data.get("profiles", {})
    if not isinstance(tables, dict):
        raise ConfigError(f"{path}: [profiles.<name>] must be tables")
    table = tables.get(chosen)
    base = BUILTIN.get(chosen)
    if table is None:
        if base is None:
            known = ", ".join(profile_names(env))
            raise ConfigError(f"No DIRAC profile {chosen!r} (known: {known})")
        return base
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [profiles.{chosen}] must be a table")
    return _from_table(chosen, table, base, path)


def _from_table(name: str, table: dict[str, Any], base: Profile | None, path: Path) -> Profile:
    """A profile from its table, laid over the built-in one of that name if there is one."""
    known = {f.name for f in fields(Profile)} - {"name", "extra"}
    values = {key: value for key, value in table.items() if key in known}
    extra = {key: value for key, value in table.items() if key not in known}
    if "servers" in values:
        servers = values["servers"]
        if isinstance(servers, str):
            servers = [servers]
        if not isinstance(servers, list) or not all(isinstance(s, str) for s in servers):
            raise ConfigError(f"{path}: [profiles.{name}] servers must be a list of URLs")
        values["servers"] = tuple(servers)
    if values.get("encoding", "json") not in ("json", "dencode"):
        raise ConfigError(f"{path}: [profiles.{name}] encoding must be 'json' or 'dencode'")
    if base is not None:
        return replace(base, **values, extra=extra)
    if "servers" not in values:
        raise ConfigError(f"{path}: [profiles.{name}] needs servers = [...]")
    return Profile(name, **values, extra=extra)
