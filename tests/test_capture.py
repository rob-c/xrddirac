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
    # the client exists, the capture tool does not: that is the failure to report
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/true" if name == "true" else None)
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


def test_a_client_that_is_not_installed_says_so_instead_of_a_traceback(capsys: Any) -> None:
    """The commonest failure is a missing client: say which, and that nothing was captured."""
    assert capture.run(["no-such-dirac-command", "--group", "lhcb_user"]) == 1
    said = capsys.readouterr().err
    assert "not found on PATH" in said and "Install the DIRAC client" in said


def test_an_empty_command_is_refused(capsys: Any) -> None:
    assert capture.run([]) == 1
    assert "give the command" in capsys.readouterr().err


def test_the_hook_logs_both_directions_of_a_tls_socket(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    """The capture that needs no tcpdump, no root and no tshark: the client's own view.

    The hook is imported the way a client's ``sitecustomize`` is imported, against a
    stand-in ``ssl.SSLSocket``, so what is tested is the hook itself: every byte sent
    is appended to ``client.bin``, every byte received to ``server.bin``.
    """
    import importlib.util
    import types

    class Standin:
        """The four methods of ``ssl.SSLSocket`` a DISET client actually uses."""

        received = b"server said this"

        def send(self, data: Any, flags: int = 0) -> int:
            return len(data)

        def sendall(self, data: Any, flags: int = 0) -> None:
            return None

        def recv(self, buflen: int, flags: int = 0) -> bytes:
            return self.received

        def recv_into(self, buffer: Any, nbytes: int = 0, flags: int = 0) -> int:
            return 0

    standin = types.ModuleType("ssl")
    standin.SSLSocket = Standin  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ssl", standin)
    monkeypatch.setenv("XRD_DISET_TRACE", str(tmp_path))
    hook = tmp_path / "sitecustomize.py"
    hook.write_text(capture._HOOK.lstrip("\n"))
    spec = importlib.util.spec_from_file_location("hook_under_test", hook)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    socket = Standin()
    socket.send(b"length:payload")
    socket.sendall(b"second")  # must be logged once, not twice
    socket.recv(10)
    for handle in module._files.values():
        handle.close()
    assert (tmp_path / "client.bin").read_bytes() == b"length:payloadsecond"
    assert (tmp_path / "server.bin").read_bytes() == b"server said this"


def test_the_hook_mode_sets_the_hook_up_for_the_client(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    """`hook` puts the sitecustomize on the child's PYTHONPATH and points it at the bundle."""
    seen: dict[str, Any] = {}

    def call(command: Any, env: Any) -> int:
        seen.update(command=command, env=env)
        return 0

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(subprocess, "call", call)
    assert capture.with_hook([sys.executable, "-c", "pass"], home=tmp_path) == 0
    (bundle,) = [one for one in tmp_path.iterdir() if one.is_dir()]
    assert (bundle / "sitecustomize.py").read_text().count("XRD_DISET_TRACE") == 1
    assert str(bundle) in seen["env"]["PYTHONPATH"].split(os.pathsep)
    assert seen["env"]["XRD_DISET_TRACE"] == str(bundle)
    assert seen["env"]["SSLKEYLOGFILE"].endswith("keys.log")
    assert (bundle / "transcript.md").exists()


def test_two_bundles_in_the_same_second_do_not_collide(tmp_path: pathlib.Path) -> None:
    first = capture._bundle(["one"], tmp_path)
    second = capture._bundle(["one"], tmp_path)
    assert first != second and second.is_dir()
