"""What every ``xrddirac`` subcommand shares: options, the session, LFN input, exit codes.

:class:`Command` is the one object a runner gets: the parsed options and the
:class:`~xrddirac.session.DiracSession`, built on first use - so a command
that fails on its arguments never looks for a proxy. Output goes through
:mod:`xrdcore.report` (``output``), the report contract shared with xrdarc,
xrdclient and xgfalclient: text by default, ``--json``/``--xml`` records
from the same calls.

Exit codes (:class:`ExitCode`): xrdcore's 0, 1, 2, 70, 130 and 141, and
xrddirac's own 3-8 for fatal failures, so a script can tell a missing proxy
from a DIRAC that refused it.
"""

from __future__ import annotations

import argparse
import enum
import logging
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from xrdcore import cli as _cli
from xrdcore import log as _xrdlog
from xrdcore import report as output
from xrdcore.errors import AuthenticationError, CredentialError
from xrdcore.errors import TimeoutError as XrdTimeoutError

from ..errors import AuthorizationError, ConfigError, DiracError, ServiceError, TooLargeError

__all__ = [
    "EXIT_CODES",
    "Command",
    "ExitCode",
    "Runner",
    "UsageError",
    "add_common",
    "configure",
    "output",
    "read_items",
]


@_cli.extends(_cli.ExitCode)
class ExitCode(enum.IntEnum):
    """What ``xrddirac`` exits with: xrdcore's shared codes (restated, checked) and these."""

    OK = 0
    FAILED = 1
    USAGE = 2
    INTERNAL = 70
    INTERRUPTED = 130
    BROKEN_PIPE = 141
    #: No usable proxy, or DIRAC refused it.
    CREDENTIAL = 3
    #: A profile, the configuration or an argument's value cannot be used.
    CONFIG = 4
    #: A DIRAC service answered an error for the whole request.
    SERVICE = 5
    #: A local file could not be read or written.
    LOCAL = 6
    #: A DIRAC service did not answer in time.
    TIMEOUT = 7
    #: A query would return more than ``--limit``.
    TOO_LARGE = 8


#: Exceptions to exit codes, most specific first.
EXIT_CODES: tuple[_cli.ExitRule, ...] = (
    (AuthorizationError, ExitCode.CREDENTIAL),
    (CredentialError, ExitCode.CREDENTIAL),
    (AuthenticationError, ExitCode.CREDENTIAL),
    (TooLargeError, ExitCode.TOO_LARGE),
    (ConfigError, ExitCode.CONFIG),
    (ServiceError, ExitCode.SERVICE),
    (XrdTimeoutError, ExitCode.TIMEOUT),
    (DiracError, ExitCode.SERVICE),
    (ConnectionError, ExitCode.SERVICE),
    (OSError, ExitCode.LOCAL),
)


class UsageError(ConfigError):
    """The command line asks for something that makes no sense (beyond argparse's checks)."""


class Command:
    """One run of one subcommand: its options and its session."""

    def __init__(self, name: str, args: argparse.Namespace) -> None:
        self.name = name
        self.args = args
        self.prog = f"xrddirac {name}"
        self._session: Any = None

    @property
    def session(self) -> Any:
        """The DIRAC session (made on first use, from ``--profile`` and ``--proxy``)."""
        if self._session is None:
            from ..session import DiracSession

            self._session = DiracSession(
                getattr(self.args, "profile", None),
                proxy=getattr(self.args, "proxy", None),
                refresh_config=bool(getattr(self.args, "refresh_config", False)),
                timeout=getattr(self.args, "timeout", None) or 600.0,
            )
        return self._session

    def close(self) -> None:
        """Close the session, if one was made."""
        if self._session is not None:
            self._session.close()

    def say(self, text: str = "", *, stderr: bool = False) -> None:
        """One line of human output (a ``message`` record under ``--json``)."""
        output.message(text + "\n", stderr=stderr)


#: A subcommand's body: the command in, an exit code out.
Runner = Callable[[Command], int]


def add_common(parser: argparse.ArgumentParser) -> None:
    """Options every subcommand takes."""
    group = parser.add_argument_group("common options")
    group.add_argument(
        "-P", "--profile", help="DIRAC profile: lhcb (default), gridpp, or one you configured"
    )
    group.add_argument(
        "--proxy", help="X.509 proxy file (default: $X509_USER_PROXY, /tmp/x509up_u<uid>)"
    )
    group.add_argument(
        "--refresh-config", action="store_true", help="ask for a newer DIRAC configuration now"
    )
    group.add_argument("-t", "--timeout", type=float, help="seconds to wait for a DIRAC service")
    group.add_argument(
        "-v", "--verbose", action="count", default=0, help="more logging (-v info, -vv debug)"
    )
    output.flags(parser)


def configure(args: argparse.Namespace) -> None:
    """Logging for ``-v``: warnings by default, info, then debug."""
    level = _xrdlog.verbosity_level(getattr(args, "verbose", 0) or 0)
    _xrdlog.configure(
        level if level else logging.WARNING, loggers=("xrddirac", "xrdhttp", "xrdsec")
    )


def read_items(values: Sequence[str], files: Sequence[str] = ()) -> list[str]:
    """Arguments and the lines of ``files`` (``-``: stdin), less blank and ``#`` lines."""
    items = [v for v in values if v != "-"]
    sources: list[Iterable[str]] = []
    if "-" in values or "-" in files:
        sources.append(sys.stdin)
    sources += [Path(name).read_text().splitlines() for name in files if name != "-"]
    for lines in sources:
        items += [
            line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")
        ]
    if not items:
        raise UsageError("Give at least one LFN (or --from-file FILE, or - for standard input)")
    return list(dict.fromkeys(items))


def human_size(size: int | None) -> str:
    """Bytes as a short human size (``1.30 GB``); ``-`` for unknown."""
    if size is None:
        return "-"
    units = ("B", "kB", "MB", "GB", "TB", "PB")
    value, index = float(size), 0
    while value >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    return f"{int(value)} B" if index == 0 else f"{value:.2f} {units[index]}"
