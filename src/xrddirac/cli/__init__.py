"""``xrddirac``: a light DIRAC client - files, datasets, replica status and jobs.

Reading (nothing in DIRAC changes)::

    xrddirac whoami                        proxy, VO, and who DIRAC says you are
    xrddirac ls DIR...                     catalog directories
    xrddirac replicas LFN...               which SEs hold each file, disk or tape
    xrddirac status LFN...                 ONLINE / NEARLINE / UNAVAILABLE per file
    xrddirac urls LFN...                   current root:// and https:// URLs
    xrddirac meta LFN... [--bk]            size, checksum, GUID; LHCb run, events, lumi
    xrddirac bk PATH [--summary]           a bookkeeping dataset (size first, then files)
    xrddirac se [SE...]                    storage elements: kind, status, endpoints
    xrddirac jobs | job-status | summary | logging | parameters | jdl | output

Changing (your jobs only)::

    xrddirac submit -- /bin/echo hello     or  xrddirac submit --jdl job.jdl
    xrddirac kill | delete | reschedule JOBID...

Every command takes ``--json``/``--xml`` for the versioned report shared
with xrdarc, xrdclient and xgfalclient, and ``-P gridpp`` (or a profile of
your own) to talk to another DIRAC. Exit status: 0 all worked, 1 some items
failed, 2 usage, 3 credentials, 4 configuration, 5 a DIRAC service, 6 a
local file, 7 a timeout, 8 a query too large for ``--limit``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from xrdcore import cli as _cli

from .._version import __version__
from . import _common, files, info, jobs
from ._common import EXIT_CODES, Command, ExitCode, Runner, output

__all__ = ["build_parser", "main"]

_COMMANDS: dict[str, tuple[Callable[[argparse.ArgumentParser], Runner], str]] = {
    "whoami": (info.setup_whoami, "your proxy, VO and DIRAC identity"),
    "profiles": (info.setup_profiles, "the DIRAC installations configured"),
    "config": (info.setup_config, "a value or section of the DIRAC configuration"),
    "ls": (files.setup_ls, "list catalog directories"),
    "du": (files.setup_du, "catalog directories' sizes"),
    "replicas": (files.setup_replicas, "files' replicas: SE, disk or tape, PFN"),
    "status": (files.setup_status, "whether files are online, on tape only, or unavailable"),
    "urls": (files.setup_urls, "files' current access URLs"),
    "meta": (files.setup_meta, "files' catalog (and Bookkeeping) metadata"),
    "bk": (files.setup_bk, "an LHCb Bookkeeping dataset: its size and files"),
    "se": (files.setup_se, "storage elements: kind, status, endpoints"),
    "submit": (jobs.setup_submit, "submit a job"),
    "jobs": (jobs.setup_jobs, "your jobs"),
    "job-status": (jobs.setup_status, "jobs' statuses"),
    "summary": (jobs.setup_summary, "your jobs counted by status"),
    "kill": (jobs.setup_kill, "kill jobs"),
    "delete": (jobs.setup_delete, "delete jobs"),
    "reschedule": (jobs.setup_reschedule, "reschedule jobs"),
    "output": (jobs.setup_output, "download jobs' output sandboxes"),
    "logging": (jobs.setup_logging, "a job's status history"),
    "parameters": (jobs.setup_parameters, "jobs' parameters"),
    "jdl": (jobs.setup_jdl, "a job's JDL"),
}


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, Runner]]:
    """The command line: one sub-parser per command, and each command's runner."""
    parser = output.Parser(prog="xrddirac", description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=output.Parser)
    runners: dict[str, Runner] = {}
    for name, (setup, help_text) in _COMMANDS.items():
        child = sub.add_parser(name, help=help_text, description=help_text)
        runners[name] = setup(child)
        _common.add_common(child)
    parser.add_argument("-V", "--version", action="version", version=f"xrddirac {__version__}")
    output.flags(parser)
    return parser, runners


def _run(argv: Sequence[str]) -> int:
    """Parse, run, and turn how it ended into an exit code (the session always closed)."""
    parser, runners = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return int(ExitCode.USAGE)
    _common.configure(args)
    command = Command(args.command, args)
    output.identify(args.command)
    fatal = _cli.Fatal(command.prog, EXIT_CODES, logger="xrddirac")
    try:
        return _cli.run(
            lambda: runners[args.command](command), interrupted=fatal.interrupted, fatal=fatal
        )
    finally:
        command.close()


def main(argv: Sequence[str] | None = None) -> int:
    """The ``xrddirac`` console script."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    return output.run_cli("xrddirac", arguments, lambda: _run(arguments))
