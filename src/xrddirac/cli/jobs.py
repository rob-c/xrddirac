"""Job commands: ``submit``, ``jobs``, ``job-status``, ``kill``, ``delete``, ``reschedule``,
``output``, ``logging``, ``parameters``, ``jdl``, ``summary``.

``submit``, ``kill``, ``delete`` and ``reschedule`` change things in DIRAC;
everything else only reads.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path
from typing import Any

from ..jdl import JDL, shell_join
from ..jobs import DiracJob, OpResult
from ._common import Command, Runner, UsageError, output

__all__ = [
    "setup_delete",
    "setup_jdl",
    "setup_jobs",
    "setup_kill",
    "setup_logging",
    "setup_output",
    "setup_parameters",
    "setup_reschedule",
    "setup_status",
    "setup_submit",
    "setup_summary",
]


def _ids(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job_ids", nargs="+", type=int, metavar="JOBID", help="DIRAC job IDs")


def _job_fields(job: DiracJob) -> dict[str, Any]:
    """A job as a report record's fields (add, never rename: scripts read them)."""
    return {
        "job_id": job.job_id,
        "status": job.status,
        "minor_status": job.minor_status,
        "application_status": job.application_status,
        "site": job.site,
        "name": job.name,
        "group": job.group,
        "submitted": job.submitted.isoformat() if job.submitted else None,
        "updated": job.updated.isoformat() if job.updated else None,
    }


def _show(command: Command, job: DiracJob) -> None:
    output.record(**_job_fields(job))
    command.say(
        f"{job.job_id:>11}  {job.status:<11} {job.minor_status[:38]:<38} {job.site:<20} {job.name}"
    )


# -- submit -----------------------------------------------------------------------------------


def setup_submit(parser: argparse.ArgumentParser) -> Runner:
    """``submit JDL`` or ``submit -- EXECUTABLE ARGS...``."""
    parser.add_argument(
        "--jdl", help="a JDL file (otherwise one is made from the command after --)"
    )
    parser.add_argument("-n", "--name", default="xrddirac", help="job name (without --jdl)")
    parser.add_argument(
        "-i", "--input", action="append", default=[], help="input sandbox file (repeatable)"
    )
    parser.add_argument(
        "-o", "--output", action="append", default=[], help="output sandbox file (repeatable)"
    )
    parser.add_argument(
        "--input-data", action="append", default=[], metavar="LFN", help="input data LFN"
    )
    parser.add_argument(
        "--site", action="append", default=[], help="run only at this site (repeatable)"
    )
    parser.add_argument("--cpu-time", type=int, help="CPU time in seconds")
    parser.add_argument("--platform", help="platform (x86_64-el9...)")
    parser.add_argument(
        "command_line",
        nargs=argparse.REMAINDER,
        metavar="-- EXECUTABLE ARGS",
        help="what to run, when not given a JDL",
    )
    return run_submit


def run_submit(command: Command) -> int:
    """Submit one job (or one parametric set); its job IDs."""
    jdl = _jdl_from(command.args)
    result = command.session.jobs.submit(jdl, sandbox=command.args.input)
    for job in result.job_ids:
        output.record(
            job_id=job, sandbox=result.sandbox, require_proxy_upload=result.require_proxy_upload
        )
        command.say(str(job))
    if result.require_proxy_upload:
        command.say(
            f"{command.prog}: DIRAC holds no proxy of yours to run these jobs: run lhcb-proxy-init",
            stderr=True,
        )
    return 0


def _jdl_from(args: argparse.Namespace) -> JDL:
    """The JDL file given, or one made from the command line, with the options applied."""
    jdl = _base_jdl(args)
    for name in args.output:
        jdl.append("OutputSandbox", name)
    if args.input_data:
        jdl.append(
            "InputData", *[d if d[:4].upper() == "LFN:" else f"LFN:{d}" for d in args.input_data]
        )
    for attribute, value in (
        ("Site", list(args.site)),
        ("CPUTime", args.cpu_time),
        ("Platform", args.platform),
    ):
        if value:
            jdl[attribute] = value
    return jdl


def _base_jdl(args: argparse.Namespace) -> JDL:
    """``--jdl FILE``'s description, or one running the words after ``--``."""
    words = [w for w in args.command_line if w != "--"] if args.command_line else []
    if args.jdl:
        if words:
            raise UsageError("Give a JDL file or a command line, not both")
        return JDL.parse(Path(args.jdl).read_text())
    if not words:
        raise UsageError("Give --jdl FILE or the command to run after --")
    jdl = JDL(
        {
            "JobName": args.name,
            "Executable": words[0],
            "StdOutput": "std.out",
            "StdError": "std.err",
        }
    )
    if len(words) > 1:
        jdl["Arguments"] = shell_join(words[1:])
    jdl["OutputSandbox"] = ["std.out", "std.err"]
    return jdl


# -- find and monitor ---------------------------------------------------------------------------


def setup_jobs(parser: argparse.ArgumentParser) -> Runner:
    """``jobs``: your jobs."""
    parser.add_argument(
        "-s", "--status", action="append", default=[], help="only this status (repeatable)"
    )
    parser.add_argument("--name", help="job name (% is a wildcard)")
    parser.add_argument("--group", help="job group")
    parser.add_argument("--site", help="site")
    parser.add_argument("--days", type=float, help="submitted in the last DAYS days")
    parser.add_argument(
        "-n", "--limit", type=int, default=100, help="at most this many (newest first)"
    )
    return run_jobs


def run_jobs(command: Command) -> int:
    """Your jobs matching the options, newest first."""
    args = command.args
    since = (
        _dt.datetime.now(_dt.UTC).replace(tzinfo=None) - _dt.timedelta(days=args.days)
        if args.days
        else None
    )
    jobs = command.session.jobs.find(
        status=args.status,
        name=args.name,
        group=args.group,
        site=args.site,
        since=since,
        limit=args.limit,
    )
    for job in jobs:
        _show(command, job)
    return 0


def setup_status(parser: argparse.ArgumentParser) -> Runner:
    """``job-status JOBID...``."""
    _ids(parser)
    return run_status


def run_status(command: Command) -> int:
    """Each job's status; 1 if any is unknown (or not yours)."""
    found = command.session.jobs.status(command.args.job_ids)
    for job_id in command.args.job_ids:
        job = found.get(job_id)
        if job is None:
            output.record(job_id=job_id, ok=False, reason="not found")
            command.say(f"{command.prog}: {job_id}: no such job (or not yours)", stderr=True)
        else:
            _show(command, job)
    return 0 if all(j in found for j in command.args.job_ids) else 1


def setup_summary(parser: argparse.ArgumentParser) -> Runner:
    """``summary``."""
    parser.add_argument(
        "-g", "--group-by", action="append", default=[], help="attribute (Status, Site...)"
    )
    return run_summary


def run_summary(command: Command) -> int:
    """Your jobs counted by status (or the attributes asked for)."""
    rows = command.session.jobs.summary(tuple(command.args.group_by) or ("Status",))
    for row in rows:
        output.record(**row)
        command.say("  ".join(f"{k}={v}" for k, v in row.items()))
    return 0


# -- manage ------------------------------------------------------------------------------------


def _manage(command: Command, results: list[OpResult], verb: str) -> int:
    for result in results:
        output.record(job_id=result.job_id, ok=result.ok, reason=result.reason)
        if result.ok:
            command.say(f"{result.job_id}: {verb}")
        else:
            command.say(f"{command.prog}: {result.job_id}: {result.reason}", stderr=True)
    return 0 if all(r.ok for r in results) else 1


def setup_kill(parser: argparse.ArgumentParser) -> Runner:
    """``kill JOBID...``."""
    _ids(parser)
    return lambda command: _manage(
        command, command.session.jobs.kill(command.args.job_ids), "killed"
    )


def setup_delete(parser: argparse.ArgumentParser) -> Runner:
    """``delete JOBID...``."""
    _ids(parser)
    return lambda command: _manage(
        command, command.session.jobs.delete(command.args.job_ids), "deleted"
    )


def setup_reschedule(parser: argparse.ArgumentParser) -> Runner:
    """``reschedule JOBID...``."""
    _ids(parser)
    return lambda command: _manage(
        command, command.session.jobs.reschedule(command.args.job_ids), "rescheduled"
    )


# -- details and output -------------------------------------------------------------------


def setup_output(parser: argparse.ArgumentParser) -> Runner:
    """``output JOBID... [-D DIR]``."""
    _ids(parser)
    parser.add_argument(
        "-D", "--directory", default=".", help="where to put each job's files (DIR/<id>)"
    )
    parser.add_argument("--input", action="store_true", help="the input sandbox instead")
    return run_output


def run_output(command: Command) -> int:
    """Download and unpack each job's output (or input) sandbox into ``DIR/<job id>``."""
    kind = "Input" if command.args.input else "Output"
    for job_id in command.args.job_ids:
        target = Path(command.args.directory, str(job_id))
        files = command.session.jobs.output(job_id, target, kind=kind)
        output.record(job_id=job_id, directory=str(target), files=files)
        command.say(f"{job_id}: {len(files)} file(s) in {target}")
    return 0


def setup_logging(parser: argparse.ArgumentParser) -> Runner:
    """``logging JOBID``."""
    parser.add_argument("job_id", type=int, metavar="JOBID")
    return run_logging


def run_logging(command: Command) -> int:
    """The job's status history."""
    for status, minor, application, when, source in command.session.jobs.logging(
        command.args.job_id
    ):
        output.record(
            status=status,
            minor_status=minor,
            application_status=application,
            time=when,
            source=source,
        )
        command.say(f"{when:<20} {status:<11} {minor:<35} {application:<25} {source}")
    return 0


def setup_parameters(parser: argparse.ArgumentParser) -> Runner:
    """``parameters JOBID...``."""
    _ids(parser)
    return run_parameters


def run_parameters(command: Command) -> int:
    """What each job's wrapper reported (CPU, node, wall time...)."""
    for job_id, values in command.session.jobs.parameters(command.args.job_ids).items():
        output.record(job_id=job_id, parameters=values)
        command.say(str(job_id))
        for name, value in sorted(values.items()):
            command.say(f"    {name:<28} {value}")
    return 0


def setup_jdl(parser: argparse.ArgumentParser) -> Runner:
    """``jdl JOBID``."""
    parser.add_argument("job_id", type=int, metavar="JOBID")
    parser.add_argument(
        "--original", action="store_true", help="as submitted, before DIRAC's additions"
    )
    return run_jdl


def run_jdl(command: Command) -> int:
    """The JDL DIRAC holds for the job."""
    text = command.session.jobs.jdl(command.args.job_id, original=command.args.original)
    output.record(job_id=command.args.job_id, jdl=text)
    command.say(text)
    return 0
