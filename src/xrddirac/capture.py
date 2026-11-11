"""Record a DISET conversation as evidence: run a client, capture it, write a transcript.

The clean-room claim in `docs/CLEANROOM.md` rests on facts, and the strongest
fact is a conversation with a real server. This tool produces that evidence in a
form a reviewer can check, and refuses to write anything that could carry a
credential.

Two modes:

    # 1. capture one call of DIRAC's own client (needs tcpdump; root for it)
    sudo -E python3 tools/diset_capture.py run -- \\
        dirac-proxy-init --group lhcb_user --rfc

    # 2. turn an already-decrypted stream into a transcript (no privileges)
    python3 tools/diset_capture.py from-stream --side client stream.bin

`run` sets `SSLKEYLOGFILE` (CPython's ssl honours it), starts a capture of the
traffic, runs the command, then writes an evidence bundle under
`docs/evidence/<date>-<command>/`:

    capture.pcap      the raw capture (TLS records; ciphertext without the key log)
    keys.log          the TLS key log, so the pcap can be decrypted
    client.bin        the client-to-server plaintext stream, when it could be
    server.bin        the server-to-client plaintext stream, when it could be
    transcript.md     the decoded conversation, redacted, with the sha256 of each stream
    evidence.json     the same as data, for a checker

Decrypting the pcap needs `tshark` (Wireshark's CLI) with the key log; the tool
does that when it is installed and says so when it is not, because *the capture
and the key log are the evidence* - the transcript is a convenience on top.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

from .testing import diset_trace

#: The hook `--with-hook` injects: everything a Python client sends and receives
#: over TLS, written as it happens. Standalone on purpose - it is dropped into a
#: `PYTHONPATH` and imported by a process that knows nothing about this package -
#: so it imports nothing of it, and it never raises into the client it observes.
_HOOK = '''
"""Written by `diset-capture --with-hook`: log the plaintext of a TLS client."""
import atexit
import os
import ssl

_out = os.environ.get("XRD_DISET_TRACE")
if _out:
    _files = {}

    def _close_all():
        for handle in _files.values():
            try:
                handle.close()
            except Exception:
                pass

    atexit.register(_close_all)

    def _write(name, data):
        try:
            handle = _files.get(name)
            if handle is None:
                handle = _files[name] = open(os.path.join(_out, name), "ab", buffering=0)
            handle.write(bytes(data))
        except Exception:
            pass  # an observer must never break the thing it observes

    _send, _recv, _recv_into = (
        ssl.SSLSocket.send,
        ssl.SSLSocket.recv,
        ssl.SSLSocket.recv_into,
    )

    def send(self, data, flags=0):
        sent = _send(self, data, flags)
        _write("client.bin", memoryview(data)[:sent])
        return sent

    def sendall(self, data, flags=0):
        # Loop the *original* send rather than the C sendall: the C one calls back
        # into the patched send, which would log every frame twice.
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            written = _send(self, view[sent:], flags)
            _write("client.bin", view[sent : sent + written])
            sent += written

    def recv(self, buflen, flags=0):
        data = _recv(self, buflen, flags)
        _write("server.bin", data)
        return data

    def recv_into(self, buffer, nbytes=0, flags=0):
        read = _recv_into(self, buffer, nbytes, flags)
        _write("server.bin", memoryview(buffer)[:read])
        return read

    ssl.SSLSocket.send, ssl.SSLSocket.sendall = send, sendall
    ssl.SSLSocket.recv, ssl.SSLSocket.recv_into = recv, recv_into
'''


def with_hook(command: list[str], *, home: pathlib.Path | None = None, keys: bool = True) -> int:
    """Run ``command`` with the plaintext hook installed, then write the bundle.

    This is the capture that works everywhere: no `tcpdump`, no root, no TLS
    decryption and no `tshark`. The client is a Python program, so a
    ``sitecustomize`` on its ``PYTHONPATH`` sees every byte it sends and receives
    on a TLS socket, which is exactly the conversation - and it is the client's
    own view, not a reconstruction from packets.
    """
    if not command:
        print("give the command to capture, after --", file=sys.stderr)
        return 1
    if shutil.which(command[0]) is None:
        print(f"{command[0]}: not found on PATH, so there is nothing to capture.", file=sys.stderr)
        print(
            "Install the client, or give the full path to the script you mean to run.",
            file=sys.stderr,
        )
        return 1
    folder = _bundle(command, home)
    (folder / "sitecustomize.py").write_text(_HOOK.lstrip("\n"))
    environment = dict(
        os.environ,
        XRD_DISET_TRACE=str(folder),
        PYTHONPATH=os.pathsep.join(
            part for part in (str(folder), os.environ.get("PYTHONPATH", "")) if part
        ),
    )
    if keys:
        environment["SSLKEYLOGFILE"] = str(folder / "keys.log")
    code = subprocess.call(command, env=environment)
    _write(folder, command, code)
    print(f"evidence in {folder}")
    return code


#: Where a bundle goes when the caller names no other place: the working
#: directory's ``docs/evidence``, which is where the repository keeps them.
DEFAULT_HOME = pathlib.Path("docs/evidence")


def _bundle(command: list[str], home: pathlib.Path | None = None) -> pathlib.Path:
    """Where this run's evidence goes: ``HOME``/docs/evidence/<stamp>-<command>."""
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    name = shlex.split(shlex.join(command))[0].replace("/", "_")
    base = (home or DEFAULT_HOME) / f"{stamp}-{name}"
    folder, attempt = base, 1
    while folder.exists():  # two captures in the same second must not collide
        attempt += 1
        folder = base.with_name(f"{base.name}-{attempt}")
    folder.mkdir(parents=True)
    return folder


def interface() -> str:
    """The interface to capture on: ``any`` on Linux, the default route's on macOS.

    ``tcpdump -i any`` is a Linux-only spelling; macOS wants a real interface, so
    the default route's is found. A caller may always name one with ``--interface``.
    """
    if sys.platform != "darwin":
        return "any"
    route = subprocess.run(
        ["route", "-n", "get", "default"], capture_output=True, text=True, check=False
    )
    for line in route.stdout.splitlines():
        if line.strip().startswith("interface:"):
            return line.split(":", 1)[1].strip()
    return "en0"


def run(command: list[str], *, device: str = "", home: pathlib.Path | None = None) -> int:
    """Run ``command`` under a capture, then write the bundle."""
    if not command:
        print("give the command to capture, after --", file=sys.stderr)
        return 1
    missing = shutil.which(command[0])
    if missing is None:
        # A client that is not installed is the commonest failure here, and a
        # traceback from deep inside subprocess helps nobody: say what is
        # missing, and that nothing was captured.
        print(
            f"{command[0]}: not found on PATH, so there is nothing to capture.",
            file=sys.stderr,
        )
        print(
            "Install the DIRAC client where this command can see it, or give the full"
            " path to the script you mean to run.",
            file=sys.stderr,
        )
        return 1
    if not shutil.which("tcpdump"):
        print(
            "tcpdump is not installed: the capture is the evidence, so it is required",
            file=sys.stderr,
        )
        return 1
    folder = _bundle(command, home)
    keys = folder / "keys.log"
    pcap = folder / "capture.pcap"
    environment = dict(os.environ, SSLKEYLOGFILE=str(keys))
    # tcpdump needs root; the client must stay the user (its proxy and its
    # configuration live there). So tcpdump writes the capture to *stdout* and
    # this process writes the file: `sudo tcpdump -w -` is the one shape that
    # needs no file ownership dance and no root-owned artefact.
    with pcap.open("wb") as handle:
        capture = subprocess.Popen(
            [
                *([] if os.geteuid() == 0 else ["sudo", "-n"]),
                "tcpdump",
                "-i",
                device or interface(),
                "-s",
                "0",
                "-U",
                "-w",
                "-",
                "tcp",
                "port",
                "8443",
            ],
            stdout=handle,
            stderr=subprocess.PIPE,
        )
        try:
            code = subprocess.call(command, env=environment)
        finally:
            capture.terminate()
            _stderr = capture.communicate(timeout=10)[1]
    if capture.returncode not in (0, -15, 143):  # terminated by us is fine
        print(
            "the capture did not start: " + (_stderr or b"").decode("utf-8", "replace").strip(),
            file=sys.stderr,
        )
        print(
            "(tcpdump needs root: run `sudo -v` once first, then this command again)",
            file=sys.stderr,
        )
        return 1
    _decrypt(pcap, keys, folder)
    _write(folder, command, code)
    print(f"evidence in {folder}")
    return code


def _decrypt(pcap: pathlib.Path, keys: pathlib.Path, folder: pathlib.Path) -> None:
    """Split the capture into the two plaintext streams, when tshark can do it."""
    if not shutil.which("tshark"):
        print(
            "tshark is not installed: capture.pcap and keys.log are the evidence; decode later",
            file=sys.stderr,
        )
        return
    for side in ("client", "server"):
        out = subprocess.run(
            [
                "tshark",
                "-r",
                str(pcap),
                "-o",
                f"tls.keylog_file:{keys}",
                "-Y",
                f"tcp.srcport eq {_port(side)}",
                "-T",
                "fields",
                "-e",
                "tcp.payload",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        payload = b"".join(
            bytes.fromhex(line.strip()) for line in out.stdout.split() if line.strip()
        )
        if payload:
            (folder / f"{side}.bin").write_bytes(payload)


def _port(side: str) -> str:
    """The port filter for one direction: the server's is the well-known one."""
    return "not 8443" if side == "client" else "8443"


def _write(folder: pathlib.Path, command: list[str], code: int) -> None:
    """The transcript and the JSON, from whatever streams were recovered."""
    pieces: list[str] = []
    streams: dict[str, object] = {}
    evidence: dict[str, object] = {"command": command, "exit": code, "streams": streams}
    for side in ("client", "server"):
        stream = folder / f"{side}.bin"
        if not stream.exists():
            continue
        trace = diset_trace.read(stream.read_bytes(), side=side)
        pieces.append(diset_trace.as_markdown(trace, side=side))
        streams[side] = trace.as_dict()
    (folder / "transcript.md").write_text("\n".join(pieces) or "# no streams recovered\n")
    (folder / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def from_stream(path: pathlib.Path, side: str) -> int:
    """Turn one plaintext stream into a transcript on stdout."""
    trace = diset_trace.read(path.read_bytes(), side=side)
    sys.stdout.write(diset_trace.as_markdown(trace, side=side))
    return 0


def main(argv: list[str] | None = None) -> int:
    """``run -- CMD...`` or ``from-stream --side client FILE``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="mode", required=True)
    runner = commands.add_parser("run", help="capture one command")
    runner.add_argument(
        "--interface", default="", metavar="DEV", help="capture device (default: the route's)"
    )
    runner.add_argument(
        "--into", type=pathlib.Path, default=None, metavar="DIR", help="where to write the bundle"
    )
    runner.add_argument("command", nargs=argparse.REMAINDER, help="the command, after --")
    hooker = commands.add_parser(
        "hook",
        help="capture a Python client's plaintext (no root, no tcpdump, no tshark)",
    )
    hooker.add_argument("--into", type=pathlib.Path, default=None, metavar="DIR")
    hooker.add_argument("--no-keys", action="store_true", help="do not also write a TLS key log")
    hooker.add_argument("command", nargs=argparse.REMAINDER, help="the command, after --")
    streamer = commands.add_parser("from-stream", help="decode a plaintext stream")
    streamer.add_argument("path", type=pathlib.Path)
    streamer.add_argument("--side", choices=("client", "server"), default="client")
    args = parser.parse_args(argv)
    if args.mode == "hook":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        return with_hook(command, home=args.into, keys=not args.no_keys)
    if args.mode == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("give the command to capture, after --")
        return run(command, device=args.interface, home=args.into)
    return from_stream(args.path, args.side)


if __name__ == "__main__":
    raise SystemExit(main())
