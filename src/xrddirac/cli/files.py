"""Read-only file commands: ``ls``, ``replicas``, ``status``, ``urls``, ``meta``, ``bk``, ``se``...

None of these change anything in DIRAC. ``status`` is the one that talks to
storage: it asks tape systems (their WLCG Tape REST API) where tape replicas
are, unless ``--no-locality``.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from ..bookkeeping import Query
from ..catalog import normalise_lfn
from ..errors import ConfigError
from ._common import Command, Runner, human_size, output, read_items

if TYPE_CHECKING:
    from ..status import FileAvailability

__all__ = [
    "setup_bk",
    "setup_du",
    "setup_ls",
    "setup_meta",
    "setup_replicas",
    "setup_se",
    "setup_status",
    "setup_urls",
]


def _lfn_arguments(parser: argparse.ArgumentParser) -> None:
    """``LFN ...`` and ``-F FILE`` (one LFN per line; ``-`` reads standard input)."""
    parser.add_argument(
        "lfns", nargs="*", metavar="LFN", help="LFNs (- reads them from standard input)"
    )
    parser.add_argument(
        "-F", "--from-file", action="append", default=[], metavar="FILE", help="read LFNs from FILE"
    )


def _lfns(command: Command) -> list[str]:
    return [normalise_lfn(item) for item in read_items(command.args.lfns, command.args.from_file)]


def _failed(command: Command, failed: dict[str, str]) -> int:
    """Report the LFNs a bulk call could not answer; 1 when there were any."""
    for lfn, reason in failed.items():
        output.record(lfn=lfn, ok=False, reason=reason)
        command.say(f"{command.prog}: {lfn}: {reason}", stderr=True)
    return 1 if failed else 0


# -- ls -------------------------------------------------------------------------------------


def setup_ls(parser: argparse.ArgumentParser) -> Runner:
    """``ls DIR...``."""
    parser.add_argument("paths", nargs="+", metavar="DIR", help="catalog directories")
    parser.add_argument("-l", "--long", action="store_true", help="sizes, dates and replica counts")
    return run_ls


def run_ls(command: Command) -> int:
    """List each directory: subdirectories (ending ``/``), then files."""
    answer = command.session.catalog.list_directory(command.args.paths, verbose=command.args.long)
    for listing in answer.successful.values():
        for sub in listing.subdirs:
            output.record(path=sub, type="directory")
            command.say(f"{sub}/")
        for lfn, meta in sorted(listing.files.items()):
            replicas = sorted(listing.replicas.get(lfn, {}))
            output.record(
                path=lfn, type="file", size=meta.size, replicas=replicas, created=_iso(meta.created)
            )
            if command.args.long:
                stamp = meta.created.strftime("%Y-%m-%d %H:%M") if meta.created else "-"
                command.say(f"{human_size(meta.size):>10}  {stamp}  {len(replicas)} rep  {lfn}")
            else:
                command.say(lfn)
    return _failed(command, answer.failed)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# -- replicas, status, urls --------------------------------------------------------------------


def setup_replicas(parser: argparse.ArgumentParser) -> Runner:
    """``replicas LFN...``."""
    _lfn_arguments(parser)
    parser.add_argument("-a", "--all", action="store_true", help="include replicas marked unusable")
    return run_replicas


def run_replicas(command: Command) -> int:
    """Each file's replicas: the SE, its kind (disk or tape), the catalog's PFN."""
    session = command.session
    answer = session.catalog.replicas(_lfns(command), all_status=command.args.all)
    for lfn, replicas in answer.successful.items():
        command.say(lfn)
        for se, pfn in sorted(replicas.items()):
            kind = _kind(session, se)
            output.record(lfn=lfn, se=se, storage=kind, pfn=pfn)
            command.say(f"    {se:<22} {kind:<7} {pfn}")
    return _failed(command, answer.failed)


def _kind(session: Any, se: str) -> str:
    """``disk``, ``tape`` or ``unknown`` for a storage element (an unknown SE is ``unknown``)."""
    try:
        return str(session.storage(se).kind)
    except ConfigError:
        return "unknown"


def setup_status(parser: argparse.ArgumentParser) -> Runner:
    """``status LFN...``: is each file online?"""
    _lfn_arguments(parser)
    parser.add_argument(
        "--no-locality",
        action="store_true",
        help="do not ask tape systems (tape replicas: UNKNOWN)",
    )
    parser.add_argument(
        "--protocol", choices=("root", "https"), help="the URL to show for each replica"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="only the per-file verdict and the totals"
    )
    return run_status


def run_status(command: Command) -> int:
    """Each file's verdict (ONLINE, NEARLINE, UNAVAILABLE, UNKNOWN) and each replica's state."""
    from ..status import summarise

    session = command.session
    answer = session.catalog.replicas(_lfns(command))
    availability = session.replica_status.check(
        answer.successful, locality=not command.args.no_locality, scheme=command.args.protocol
    )
    for lfn, item in availability.items():
        _show_availability(command, lfn, item)
    totals = summarise(availability)
    output.record(about="summary", **totals)
    command.say(" ".join(f"{k}={v}" for k, v in totals.items()))
    code = _failed(command, answer.failed)
    return code or (0 if all(item.online for item in availability.values()) else 1)


def _show_availability(command: Command, lfn: str, item: FileAvailability) -> None:
    output.record(
        lfn=lfn,
        verdict=item.verdict,
        online_at=list(item.online_sites),
        replicas=[
            {
                "se": r.se,
                "storage": r.kind,
                "state": str(r.state),
                "se_status": r.se_status,
                "url": r.url,
                "reason": r.reason,
            }
            for r in item.replicas
        ],
    )
    command.say(f"{item.verdict:<11} {lfn}")
    if not command.args.quiet:
        for r in item.replicas:
            why = f"  ({r.reason})" if r.reason else ""
            command.say(f"    {r.se:<22} {r.kind:<7} {r.state!s:<20} {r.url}{why}")


def setup_urls(parser: argparse.ArgumentParser) -> Runner:
    """``urls LFN...``: where to read each replica from."""
    _lfn_arguments(parser)
    parser.add_argument("--protocol", choices=("root", "https"), help="only URLs of this scheme")
    return run_urls


def run_urls(command: Command) -> int:
    """Each replica's current access URL(s), built from its SE's definition."""
    session = command.session
    answer = session.catalog.replicas(_lfns(command))
    for lfn, replicas in answer.successful.items():
        for se in sorted(replicas):
            urls = _urls(session, se, lfn)
            for scheme, url in urls.items():
                if command.args.protocol in (None, scheme):
                    output.record(lfn=lfn, se=se, scheme=scheme, url=url)
                    command.say(url)
    return _failed(command, answer.failed)


def _urls(session: Any, se: str, lfn: str) -> dict[str, str]:
    """Every remote URL of ``lfn`` at ``se`` (none for an SE the configuration lacks)."""
    try:
        return dict(session.storage(se).urls(lfn))
    except ConfigError:
        return {}


# -- meta, du ---------------------------------------------------------------------------------


def setup_meta(parser: argparse.ArgumentParser) -> Runner:
    """``meta LFN...``."""
    _lfn_arguments(parser)
    parser.add_argument(
        "--bk", action="store_true", help="the Bookkeeping's metadata (LHCb) as well"
    )
    return run_meta


def run_meta(command: Command) -> int:
    """Each file's catalog metadata, and with ``--bk`` its Bookkeeping metadata."""
    session = command.session
    lfns = _lfns(command)
    answer = session.catalog.metadata(lfns)
    bk = session.bookkeeping.file_metadata(lfns).successful if command.args.bk else {}
    for lfn, meta in answer.successful.items():
        fields: dict[str, Any] = {
            "size": meta.size,
            "checksum": f"{meta.checksum_type.lower()}:{meta.checksum}" if meta.checksum else "",
            "guid": meta.guid,
            "status": meta.status,
            "created": _iso(meta.created),
            "owner": f"{meta.owner}/{meta.group}",
        }
        found = bk.get(lfn)
        if found is not None:
            fields.update(
                run=found.run,
                events=found.events,
                luminosity=found.luminosity,
                data_quality=found.data_quality,
                got_replica=found.got_replica,
            )
        output.record(lfn=lfn, **fields)
        command.say(lfn)
        for key, value in fields.items():
            command.say(f"    {key:<13} {value}")
    return _failed(command, answer.failed)


def setup_du(parser: argparse.ArgumentParser) -> Runner:
    """``du DIR...``."""
    parser.add_argument("paths", nargs="+", metavar="DIR", help="catalog directories")
    return run_du


def run_du(command: Command) -> int:
    """Each directory's logical size and file count, as the catalog accounts them."""
    answer = command.session.catalog.directory_size(command.args.paths)
    for path, info in answer.successful.items():
        size = info.get("LogicalSize")
        files = info.get("LogicalFiles")
        output.record(path=path, size=size, files=files)
        command.say(f"{human_size(size):>10}  {files} files  {path}")
    return _failed(command, answer.failed)


# -- bk ---------------------------------------------------------------------------------------


def setup_bk(parser: argparse.ArgumentParser) -> Runner:
    """``bk PATH``."""
    parser.add_argument(
        "path", help="bookkeeping path: /Config/Version/Conditions/Pass.../EventType/FileType"
    )
    parser.add_argument(
        "-r", "--run", type=int, action="append", default=[], help="only this run (repeatable)"
    )
    parser.add_argument("--runs", metavar="START:END", help="only runs START to END")
    parser.add_argument("--production", type=int, help="only this production")
    parser.add_argument(
        "--dq", action="append", default=[], help="data-quality flag (OK, UNCHECKED...)"
    )
    parser.add_argument("-s", "--summary", action="store_true", help="only the dataset's size")
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=1000,
        help="refuse datasets of more files than this (0: no limit)",
    )
    return run_bk


def run_bk(command: Command) -> int:
    """The dataset's size, and (unless ``--summary``) its files."""
    args = command.args
    filters: dict[str, Any] = {
        "runs": tuple(args.run),
        "data_quality": tuple(args.dq),
        "production": args.production,
    }
    if args.runs:
        start, _, end = args.runs.partition(":")
        filters["start_run"], filters["end_run"] = int(start), int(end or start)
    query = Query.from_path(args.path, **filters)
    bk = command.session.bookkeeping
    size = bk.summary(query)
    output.record(
        about="summary",
        path=query.path,
        files=size.files,
        events=size.events,
        size=size.size,
        luminosity=size.luminosity,
    )
    command.say(
        f"# {size.files} files, {size.events} events, {human_size(size.size)}, "
        f"lumi {size.luminosity:.4g}"
    )
    if args.summary:
        return 0
    for lfn, meta in sorted(bk.files(query, limit=args.limit).items()):
        output.record(
            lfn=lfn, size=meta.size, events=meta.events, run=meta.run, luminosity=meta.luminosity
        )
        command.say(lfn)
    return 0


# -- se ---------------------------------------------------------------------------------------


def setup_se(parser: argparse.ArgumentParser) -> Runner:
    """``se NAME...``."""
    parser.add_argument(
        "names", nargs="*", metavar="SE", help="storage elements (none: list them all)"
    )
    return run_se


def run_se(command: Command) -> int:
    """Each storage element's kind, status and endpoints (or every SE's name)."""
    session = command.session
    names = command.args.names or session.config.root.names("Resources/StorageElements")
    if not command.args.names:
        for name in names:
            output.record(se=name)
            command.say(name)
        return 0
    status = session.replica_status.se_status(names)
    for name in names:
        element = session.storage(name)
        endpoints = [
            {"scheme": e.scheme, "base": e.base, "path": e.path, "access": e.access}
            for e in element.endpoints
        ]
        output.record(
            se=name,
            storage=element.kind,
            se_type=element.se_type,
            status=status.get(name),
            base=element.base,
            protocols=list(element.access_protocols),
            endpoints=endpoints,
        )
        command.say(
            f"{name}: {element.kind} ({element.se_type or '?'}), {status.get(name)}, "
            f"base {element.base or '-'}"
        )
        for e in element.endpoints:
            command.say(f"    {e.scheme:<6} {e.base}{e.path}  [{e.access}]")
    return 0
