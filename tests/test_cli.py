"""The ``xrddirac`` command line against the fake DIRAC: every command, text and ``--json``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xrddirac.cli import main
from xrddirac.cli._common import ExitCode, human_size, read_items
from xrddirac.testing import FakeDirac

PATH = "/LHCb/Collision24/Beam6800GeV/Real Data/Sprucing24c2a/94000000/B2CC.DST"


@pytest.fixture
def cli(dirac: FakeDirac, tmp_path: Path, pki: Any, monkeypatch: pytest.MonkeyPatch) -> FakeDirac:
    """The fake as the ``fake`` profile, with the PKI's proxy and CAs in the environment."""
    config = tmp_path / "xrddirac.toml"
    server = dirac.diset.url("Configuration/Server")
    config.write_text(
        f'default = "fake"\n[profiles.fake]\nservers = ["{server}"]\n'
        'vo = "testvo"\nconfig_max_age = 0\n'
        f'[profiles.fake8]\nservers = ["{server}"]\nvo = "testvo"\n'
        "use_diracx = false\n"
    )
    monkeypatch.setenv("XRDDIRAC_CONFIG", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name, value in pki.environment().items():
        monkeypatch.setenv(name, value)
    for i in range(3):
        dirac.add_file(
            f"/testvo/d/f{i}", size=2_500_000, replicas={"SITE-DISK": "pfn", "SITE-TAPE": ""}
        )
    dirac.add_file("/testvo/d/sub/g", replicas={"OTHER-DISK": "", "NOT-AN-SE": ""})
    dirac.tape["/tape/testvo/d/f0"] = "TAPE"
    dirac.add_dataset(PATH, [f"/testvo/d/f{i}" for i in range(3)], run=9)
    return dirac


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def report(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, list[dict[str, Any]]]:
    code, out, _ = run(capsys, *argv, "--json")
    document = json.loads(out)
    assert document["summary"]["exit_code"] == code
    return code, [r for r in document["records"] if r["kind"] == "result"]


def test_module_entry(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import runpy

    monkeypatch.setattr("sys.argv", ["xrddirac", "profiles"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("xrddirac.cli", run_name="__main__")
    assert caught.value.code == 0 and "lhcb" in capsys.readouterr().out


def test_help_and_usage(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys)
    assert code == ExitCode.USAGE and "replicas" in out
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "xrddirac" in capsys.readouterr().out


def test_files(cli: FakeDirac, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "ls", "/testvo/d")
    assert code == 0 and out.splitlines() == [
        "/testvo/d/sub/",
        "/testvo/d/f0",
        "/testvo/d/f1",
        "/testvo/d/f2",
    ]
    code, out, _ = run(capsys, "ls", "-l", "/testvo/d", "/nope")
    assert code == 1 and "2.50 MB" in out and "2 rep" in out
    code, out, _ = run(capsys, "replicas", "/testvo/d/f0", "/testvo/d/sub/g")
    assert code == 0 and "SITE-TAPE              tape" in out and "OTHER-DISK" in out
    code, records = report(capsys, "status", "/testvo/d/f0", "/testvo/d/sub/g")
    assert code == 1 and records[0]["verdict"] == "ONLINE" and records[1]["verdict"] == "UNKNOWN"
    assert records[2] == {**records[2], "files": 2, "ONLINE": 1, "UNKNOWN": 1}
    code, out, _ = run(
        capsys, "status", "-q", "--no-locality", "--protocol", "https", "/testvo/d/f1"
    )
    assert code == 0 and out.startswith("ONLINE") and "SITE-DISK" not in out
    code, out, _ = run(capsys, "urls", "--protocol", "root", "/testvo/d/f1")
    assert out.strip() == "root://disk.example.org//eos/testvo/grid/testvo/d/f1"
    code, out, _ = run(capsys, "urls", "/testvo/d/sub/g")
    assert "/eos/other/testvo/d/sub/g" in out
    code, records = report(capsys, "meta", "--bk", "/testvo/d/f0")
    assert records[0]["checksum"] == "adler32:deadbeef" and records[0]["run"] == 9
    code, out, _ = run(capsys, "meta", "/testvo/d/sub/g")
    assert code == 0 and "run" not in out
    code, out, _ = run(capsys, "du", "/testvo/d")
    assert "4 files" in out


def test_lfn_input(
    cli: FakeDirac,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = tmp_path / "lfns.txt"
    listing.write_text("# a comment\n\nLFN:/testvo/d/f0\n")
    code, out, _ = run(capsys, "replicas", "-F", str(listing))
    assert code == 0 and out.startswith("/testvo/d/f0")
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("/testvo/d/f1\n"))
    assert read_items(["-"]) == ["/testvo/d/f1"]
    code, _, err = run(capsys, "replicas")
    assert code == ExitCode.CONFIG and "at least one LFN" in err
    assert (
        human_size(None) == "-"
        and human_size(999) == "999 B"
        and human_size(10**18) == "1000.00 PB"
    )


def test_bk_and_se(cli: FakeDirac, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "bk", PATH)
    assert code == 0 and out.splitlines()[0].startswith("# 3 files") and len(out.splitlines()) == 4
    code, out, _ = run(
        capsys, "bk", "-s", "--runs", "1:20", "-r", "9", "--dq", "OK", "--production", "1", PATH
    )
    assert len(out.splitlines()) == 1
    code, _, err = run(capsys, "bk", "-n", "2", PATH)
    assert code == ExitCode.TOO_LARGE and "holds 3 files" in err
    code, _, err = run(capsys, "bk", "/not/a/path")
    assert code == ExitCode.CONFIG
    code, out, _ = run(capsys, "se")
    assert "SITE-DISK" in out.split()
    code, records = report(capsys, "se", "SITE-TAPE", "OTHER-DISK")
    assert records[0]["storage"] == "tape" and records[1]["status"] == "Banned"


def test_info(cli: FakeDirac, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "whoami")
    assert code == 0 and "DIRAC      tuser in testvo_user" in out
    code, out, _ = run(capsys, "whoami", "--no-diracx", "-v")
    assert "DIRAC " not in out
    code, out, _ = run(capsys, "config", "DiracX")
    assert out.startswith("URL = https://")
    code, out, _ = run(capsys, "config", "DiracX/URL")
    assert out.startswith("https://")
    code, out, _ = run(capsys, "config")
    assert "Resources/" in out
    code, out, _ = run(capsys, "config", "--service", "DataManagement/FileCatalog")
    assert out.strip() == cli.diset.url("DataManagement/FileCatalog")
    code, _, err = run(capsys, "config", "No/Such")
    assert code == ExitCode.CONFIG and "Nothing at No/Such" in err
    code, out, _ = run(capsys, "profiles")
    assert {line.split()[0] for line in out.splitlines()} == {"fake", "fake8", "gridpp", "lhcb"}


@pytest.mark.parametrize("profile", ["fake", "fake8"])
def test_jobs(
    cli: FakeDirac, capsys: pytest.CaptureFixture[str], tmp_path: Path, profile: str
) -> None:
    script = tmp_path / "run.sh"
    script.write_text("echo hi\n")
    code, out, _ = run(
        capsys,
        "submit",
        "-P",
        profile,
        "-n",
        "j",
        "-i",
        str(script),
        "-o",
        "o.txt",
        "--input-data",
        "/testvo/d/f0",
        "--site",
        "S",
        "--cpu-time",
        "60",
        "--platform",
        "p",
        "--",
        "/bin/sh",
        "run.sh",
        "a b",
    )
    assert code == 0
    job = int(out.strip())
    jdl = cli.jobs[job].jdl
    assert jdl["Arguments"] == "run.sh 'a b'" and jdl["InputData"] == ["LFN:/testvo/d/f0"]
    assert jdl["OutputSandbox"] == ["std.out", "std.err", "o.txt"] and jdl["Site"] == ["S"]
    jdl_file = tmp_path / "job.jdl"
    jdl_file.write_text('[Executable = "x"; JobName = "fromfile";]')
    cli.require_proxy_upload = True
    code, out, err = run(capsys, "submit", "-P", profile, "--jdl", str(jdl_file))
    assert code == 0 and "lhcb-proxy-init" in err
    second = int(out.strip())
    code, out, _ = run(capsys, "jobs", "-P", profile, "--days", "100000", "-s", "Received")
    assert [int(line.split()[0]) for line in out.splitlines()] == [second, job]
    code, records = report(capsys, "job-status", "-P", profile, str(job), "5")
    assert code == 1 and records[0]["status"] == "Received" and records[1]["ok"] is False
    code, out, _ = run(capsys, "summary", "-P", profile, "-g", "Status")
    assert "count=2" in out
    cli.jobs[job].parameters = {"CPU": 1}
    code, out, _ = run(capsys, "parameters", "-P", profile, str(job))
    assert "CPU" in out
    code, out, _ = run(capsys, "logging", "-P", profile, str(job))
    assert "Received" in out
    code, out, _ = run(capsys, "jdl", "-P", profile, str(job), "--original")
    assert "Executable" in out
    cli.use_diracx = profile == "fake"  # where the fake keeps output sandboxes
    cli.finish(job, files={"o.txt": b"o"})
    code, out, _ = run(capsys, "output", "-P", profile, str(job), "-D", str(tmp_path / "res"))
    assert code == 0 and (tmp_path / "res" / str(job) / "o.txt").read_text() == "o"
    code, out, _ = run(
        capsys, "output", "-P", profile, "--input", str(job), "-D", str(tmp_path / "in")
    )
    assert (tmp_path / "in" / str(job) / "run.sh").exists()
    code, out, err = run(capsys, "kill", "-P", profile, str(second), "5")
    assert code == 1 and "killed" in out and "no such job" in err
    assert run(capsys, "delete", "-P", profile, str(second))[0] == 0
    assert run(capsys, "reschedule", "-P", profile, str(second))[0] == 0


def test_submit_usage(cli: FakeDirac, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _, err = run(capsys, "submit")
    assert code == ExitCode.CONFIG and "--jdl FILE" in err
    jdl = tmp_path / "j.jdl"
    jdl.write_text('[Executable = "x";]')
    code, _, err = run(capsys, "submit", "--jdl", str(jdl), "--", "/bin/true")
    assert code == ExitCode.CONFIG and "not both" in err
    code, out, _ = run(capsys, "submit", "--", "/bin/true")
    assert code == 0 and "Arguments" not in str(cli.jobs[int(out)].jdl)


def test_fatal_errors(
    cli: FakeDirac, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _, err = run(capsys, "ls", "-P", "nope", "/x")
    assert code == ExitCode.CONFIG and "No DIRAC profile" in err
    code, _, err = run(capsys, "whoami", "--proxy", "/no/such/proxy")
    assert code == ExitCode.CREDENTIAL
    cli.stop()
    code, _, err = run(capsys, "ls", "/testvo/d", "-t", "2")
    assert code in (ExitCode.SERVICE, ExitCode.TIMEOUT)
