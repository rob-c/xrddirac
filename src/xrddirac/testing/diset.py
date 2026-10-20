"""A threaded, in-process DISET server: TLS, framing, proposals, RPC and file transfer.

What a test drives :class:`~xrddirac.diset.DisetClient` against. One server
listens on one port and hosts any number of services (the proposal names
the ``System/Component``); a test registers what each method answers::

    with FakeDisetServer(pki.server_context()) as server:
        server.rpc("DataManagement/FileCatalog", "exists", lambda lfns: {"Successful": {}, ...})
        server.files["/SandBox/a.tar.bz2"] = b"..."            # served to FileTransfer ToClient
        client = DisetClient([server.url("DataManagement/FileCatalog")], tls=...)

A handler returns a value (sent as ``S_OK(value)``), a whole ``S_OK``/
``S_ERROR`` dict (sent as it is), or raises :class:`Refuse` (``S_ERROR``).
Faults for the transport's tests: ``hang_up`` (close straight after the
proposal), ``keepalive`` (a ping before each answer, whose pong is checked),
``encoding`` (answer in DEncode, as DIRAC 8 does).

Every call is logged in :attr:`FakeDisetServer.calls` - service, method,
arguments, the proposal - for a test to assert what was asked.

Stability: test API, versioned with the package.
"""

from __future__ import annotations

import hashlib
import socket
import socketserver
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import codec
from ..diset import CHUNK, KEEPALIVE, Wire, s_error, s_ok

__all__ = ["Call", "FakeDisetServer", "Refuse"]


class Refuse(Exception):
    """Raised by a handler: answered as ``S_ERROR(message, errno)``."""

    def __init__(self, message: str, errno: int = 0, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.errno = errno
        self.extra = extra


@dataclass
class Call:
    """One call the server took."""

    service: str
    method: str
    args: list[Any]
    proposal: list[Any]


@dataclass
class _Faults:
    hang_up: set[str] = field(default_factory=set)
    keepalive: bool = False


class FakeDisetServer:
    """See the module docstring."""

    def __init__(
        self, tls: ssl.SSLContext, *, host: str = "localhost", encoding: codec.Encoding = "json"
    ) -> None:
        self.tls = tls
        self.host = host
        self.encoding: codec.Encoding = encoding
        self.handlers: dict[tuple[str, str], Callable[..., Any]] = {}
        self.files: dict[str, bytes] = {}
        self.uploads: dict[str, bytes] = {}
        #: ``(file_id, token, data) -> location``: what an upload answers.
        self.on_upload: Callable[[Any, str, bytes], Any] = lambda file_id, _token, data: "stored"
        self.calls: list[Call] = []
        self.faults = _Faults()
        self._lock = threading.Lock()
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                outer._serve(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server((host, 0), Handler)
        self._thread: threading.Thread | None = None

    # -- set up ----------------------------------------------------------------------------

    @property
    def port(self) -> int:
        """The port listened on."""
        return int(self._server.server_address[1])

    def url(self, service: str) -> str:
        """``dips://host:port/<service>``."""
        return f"dips://{self.host}:{self.port}/{service}"

    def rpc(self, service: str, method: str, handler: Callable[..., Any]) -> None:
        """Answer ``service.method(*args)`` with ``handler(*args)``."""
        self.handlers[(service, method)] = handler

    def start(self) -> FakeDisetServer:
        """Serve in a background thread."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever, args=(0.01,), daemon=True
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving (again is nothing)."""
        if self._thread is not None:
            self._server.shutdown()
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> FakeDisetServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def methods(self, service: str | None = None) -> list[str]:
        """The methods called so far (of ``service``), in order."""
        with self._lock:
            return [c.method for c in self.calls if service is None or c.service == service]

    # -- serving -------------------------------------------------------------------------------

    def _serve(self, raw: socket.socket) -> None:
        """One connection: TLS, the proposal, then the action."""
        try:
            stream = self.tls.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        try:
            wire = Wire(stream, self.encoding)
            proposal = wire.receive()["Value"]
            (service, _setup, _vo), (kind, name) = proposal[0], proposal[1]
            if name in self.faults.hang_up:
                return
            if kind == "RPC" and (service, name) not in self.handlers:
                wire.send(s_error(f"Unknown method {name} of {service}"))
                return
            self._answer(wire, s_ok())
            if kind == "RPC":
                self._rpc(wire, service, name, proposal)
            elif name == "ToClient":
                self._to_client(wire, service, proposal)
            else:
                self._from_client(wire, service, proposal)
        except (OSError, ssl.SSLError, KeyError, IndexError, TypeError, ValueError):
            return
        finally:
            stream.close()

    def _answer(self, wire: Wire, value: dict[str, Any]) -> None:
        """Send ``value``, after a keep-alive ping (and its pong) when that fault is on."""
        if self.faults.keepalive:
            wire.send(s_ok({"id": "ka1", "kaping": True}), prefix=KEEPALIVE)
            wire.receive_any()  # the client's pong: it must send one, or this waits
        wire.send(value)

    def _rpc(self, wire: Wire, service: str, method: str, proposal: list[Any]) -> None:
        args = list(wire.receive()["Value"])
        with self._lock:
            self.calls.append(Call(service, method, args, proposal))
        try:
            value = self.handlers[(service, method)](*args)
        except Refuse as refusal:
            self._answer(wire, {**s_error(refusal.message, refusal.errno), **refusal.extra})
            return
        self._answer(wire, value if isinstance(value, dict) and "OK" in value else s_ok(value))

    def _to_client(self, wire: Wire, service: str, proposal: list[Any]) -> None:
        file_id, _token = wire.receive()["Value"][:2]
        with self._lock:
            self.calls.append(Call(service, "ToClient", [file_id], proposal))
        data = self.files.get(str(file_id))
        if data is None:
            wire.send(s_error(f"No file {file_id}"))
            return
        wire.send(s_ok())
        digest = hashlib.md5(usedforsecurity=False)
        for start in range(0, len(data), CHUNK):
            chunk = data[start : start + CHUNK]
            digest.update(chunk)
            wire.send(s_ok([True, chunk]))
            wire.receive()
        wire.send(s_ok([False, digest.hexdigest()]))
        wire.send(s_ok())

    def _from_client(self, wire: Wire, service: str, proposal: list[Any]) -> None:
        file_id, token, _size = wire.receive()["Value"][:3]
        with self._lock:
            self.calls.append(Call(service, "FromClient", [file_id], proposal))
        wire.send(s_ok())
        received = bytearray()
        while True:
            more, data = wire.receive()["Value"]
            if not more:
                break
            received += (
                data if isinstance(data, bytes) else str(data).encode("utf-8", "surrogateescape")
            )
            wire.send(s_ok())
        key = str(file_id[0] if isinstance(file_id, list) else file_id)
        self.uploads[key] = bytes(received)
        wire.send(s_ok(self.on_upload(file_id, token, bytes(received))))
