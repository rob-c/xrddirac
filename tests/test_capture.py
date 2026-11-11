"""`diset-capture` as a command: the two modes, and the refusals that keep it honest."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any

import pytest

from xrddirac import capture, codec
from xrddirac.testing import diset_trace


def _frame(value: object) -> bytes:
    payload = codec.encode(value)
    return str(len(payload)).encode() + b":" + payload


def test_from_stream_prints_the_transcript(tmp_path: pathlib.Path, capsys: Any) -> None:
    stream = tmp_path / "client.bin"
    stream.write_bytes(_frame({"OK": True, "Value": {"access_token": "abc", "n": 1}}))
    assert capture.main(["from-stream", str(stream)]) == 0
    said = capsys.readouterr().out
    assert "DISET transcript (client)" in said
    assert "<redacted: 3 chars>" in said


def test_from_stream_takes_a_side(tmp_path: pathlib.Path, capsys: Any) -> None:
    stream = tmp_path / "server.bin"
    stream.write_bytes(_frame({"OK": True, "Value": []}))
    assert capture.main(["from-stream", "--side", "server", str(stream)]) == 0
    assert "(server)" in capsys.readouterr().out


def test_it_says_so_when_tcpdump_is_missing(monkeypatch: Any, capsys: Any) -> None:
    """The capture is the evidence: without it there is nothing to record."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert capture.main(["run", "--", "true"]) == 1
    assert "tcpdump is not installed" in capsys.readouterr().err


def test_a_run_needs_a_command(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        capture.main(["run"])


def test_the_interface_is_the_default_route_on_macos(monkeypatch: Any) -> None:
    """`tcpdump -i any` is Linux-only: on macOS the route's interface is the one to capture."""
    monkeypatch.setattr(sys, "platform", "darwin")

    class Route:
        stdout = "   route to: default\ninterface: en7\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Route())
    assert capture.interface() == "en7"
    monkeypatch.setattr(sys, "platform", "linux")
    assert capture.interface() == "any"


def test_the_bundle_is_named_by_the_time_and_the_command(tmp_path: pathlib.Path) -> None:
    folder = capture._bundle(["dirac-proxy-init", "--group", "lhcb_user"], tmp_path)
    assert folder.parent == tmp_path
    assert folder.name.endswith("dirac-proxy-init")
    assert folder.is_dir()


def test_a_bundle_is_written_with_a_transcript_and_a_hash(tmp_path: pathlib.Path) -> None:
    """The JSON carries the sha256 of the bytes, so a transcript ties to a capture."""
    folder = tmp_path / "bundle"
    folder.mkdir()
    stream = _frame({"OK": True, "Value": []})
    (folder / "client.bin").write_bytes(stream)
    capture._write(folder, ["true"], 0)
    evidence = json.loads((folder / "evidence.json").read_text())
    assert evidence["streams"]["client"]["sha256"] == diset_trace.digest(stream)
    assert "DISET transcript (client)" in (folder / "transcript.md").read_text()


def test_the_capture_does_not_need_root_for_the_client(monkeypatch: Any, tmp_path: Any) -> None:
    """tcpdump may need root; the client must not: its proxy and config are the user's.

    The capture is written by *this* process from tcpdump's stdout, so no file is
    ever created by root and no ownership has to be repaired afterwards.
    """
    started: list[list[str]] = []

    class Capture:
        """What Popen gives us: a process we terminate once the client is done."""

        returncode = -15
        stdout = None
        stderr = None

        def communicate(self, timeout: int = 0) -> tuple[bytes, bytes]:
            return b"", b""

        def terminate(self) -> None:
            pass

        def wait(self, timeout: int = 0) -> int:
            return 0

    def popen(argv: Any, **kwargs: Any) -> Capture:
        started.append(list(argv))
        return Capture()

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(capture, "interface", lambda: "en0")
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "call", lambda command, env: 0)
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(capture, "_decrypt", lambda *args: None)
    assert capture.run(["true"], home=tmp_path) == 0
    argv = started[0]
    assert argv[:2] == ["sudo", "-n"] and "tcpdump" in argv
    assert argv[-1] == "8443" and "-i" in argv
    (bundle,) = list(tmp_path.iterdir())
    assert (bundle / "capture.pcap").exists()
