"""Information commands: ``whoami``, ``config``, ``profiles``."""

from __future__ import annotations

import argparse

from ..errors import ConfigError
from ..profiles import load_profile, profile_names
from ._common import Command, Runner, output

__all__ = ["setup_config", "setup_profiles", "setup_whoami"]


def setup_whoami(parser: argparse.ArgumentParser) -> Runner:
    """``whoami``."""
    parser.add_argument("--no-diracx", action="store_true", help="only the proxy, no DiracX check")
    return run_whoami


def run_whoami(command: Command) -> int:
    """The proxy (identity, VO, time left) and, where there is DiracX, who DIRAC says you are."""
    session = command.session
    info = session.proxy_info()
    fqans = [fqan for entry in info.voms for fqan in entry.fqans]
    output.record(
        about="proxy",
        identity=info.identity,
        time_left=int(info.time_left),
        fqans=fqans,
        profile=session.profile.name,
        vo=session.vo,
    )
    hours, rest = divmod(int(info.time_left), 3600)
    command.say(f"identity   {info.identity}")
    command.say(f"time left  {hours}h{rest // 60:02d}m")
    command.say(f"VOMS       {', '.join(fqans) or '(none)'}")
    command.say(f"profile    {session.profile.name} (VO {session.vo})")
    if command.args.no_diracx or not session.diracx_url:
        return 0
    who = session.diracx.whoami()
    output.record(
        about="diracx",
        user=who.get("preferred_username"),
        group=who.get("dirac_group"),
        vo=who.get("vo"),
        properties=who.get("properties", []),
    )
    command.say(
        f"DIRAC      {who.get('preferred_username')} in {who.get('dirac_group')} "
        f"({', '.join(who.get('properties', []))})"
    )
    return 0


def setup_config(parser: argparse.ArgumentParser) -> Runner:
    """``config [PATH]``."""
    parser.add_argument(
        "path", nargs="?", default="", help="an option or section (DIRAC/Configuration)"
    )
    parser.add_argument("--service", metavar="System/Component", help="a service's URLs instead")
    return run_config


def run_config(command: Command) -> int:
    """A value or a section of the DIRAC configuration, or a service's URLs."""
    config = command.session.config
    if command.args.service:
        urls = config.service_urls(command.args.service, schemes=("dips", "https"))
        for url in urls:
            output.record(service=command.args.service, url=url)
            command.say(url)
        return 0
    path = command.args.path.strip("/")
    section = config.root.section(path) if path else config.root
    if section is None:
        value = config.get(path)
        if value is None:
            raise ConfigError(
                f"Nothing at {path} in the {command.session.profile.name} configuration"
            )
        output.record(path=path, value=value)
        command.say(value)
        return 0
    output.record(
        path=path or "/",
        version=config.version,
        sections=list(section.sections),
        options=dict(section.options),
    )
    for name, value in section.options.items():
        command.say(f"{name} = {value}")
    for name in section.sections:
        command.say(f"{name}/")
    return 0


def setup_profiles(parser: argparse.ArgumentParser) -> Runner:
    """``profiles``."""
    return run_profiles


def run_profiles(command: Command) -> int:
    """The DIRAC profiles there are, and what each points at."""
    for name in profile_names():
        profile = load_profile(name)
        output.record(
            profile=name, vo=profile.vo, setup=profile.setup, servers=list(profile.servers)
        )
        command.say(f"{name:<10} VO {profile.vo or '(from the proxy)':<18} {profile.servers[0]}")
    return 0
