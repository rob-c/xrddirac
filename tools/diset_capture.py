#!/usr/bin/env python3
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xrddirac.testing import diset_trace  # noqa: E402 - after the path fix


def _bundle(command: list[str]) -> pathlib.Path:
    """Where this run's evidence goes."""
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    name = shlex.split(shlex.join(command))[0].replace("/", "_")
    folder = ROOT / "docs" / "evidence" / f"{stamp}-{name}"
    folder.mkdir(parents=True)
    return folder


def run(command: list[str]) -> int:
    """Run ``command`` under a capture, then write the bundle."""
    if not shutil.which("tcpdump"):
        print(
            "tcpdump is not installed: the capture is the evidence, so it is required",
            file=sys.stderr,
        )
        return 1
    folder = _bundle(command)
    keys = folder / "keys.log"
    pcap = folder / "capture.pcap"
    environment = dict(os.environ, SSLKEYLOGFILE=str(keys))
    capture = subprocess.Popen(
        ["tcpdump", "-i", "any", "-s", "0", "-w", str(pcap), "tcp", "port", "8443"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        code = subprocess.call(command, env=environment)
    finally:
        capture.terminate()
        capture.wait(timeout=10)
    _decrypt(pcap, keys, folder)
    _write(folder, command, code)
    print(f"evidence in {folder.relative_to(ROOT)}")
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
    runner.add_argument("command", nargs=argparse.REMAINDER, help="the command, after --")
    streamer = commands.add_parser("from-stream", help="decode a plaintext stream")
    streamer.add_argument("path", type=pathlib.Path)
    streamer.add_argument("--side", choices=("client", "server"), default="client")
    args = parser.parse_args(argv)
    if args.mode == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("give the command to capture, after --")
        return run(command)
    return from_stream(args.path, args.side)


if __name__ == "__main__":
    raise SystemExit(main())
