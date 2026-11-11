"""DISET, DIRAC's RPC protocol: TLS with the proxy as client certificate, then framed values.

The requirements this implements are numbered in ``docs/diset-spec.md`` - a
clean-room specification, written because DIRAC is GPL-3.0-only and this package
is LGPL-3.0-or-later (``docs/CLEANROOM.md``). A change here should name the
sections it implements.

Every DIRAC 8/9 service still listens on ``dips://host:port/System/Component``.
A call is one TLS connection (the X.509 proxy is the client certificate:
the server reads the user's DN, VO and group from it, nothing else is sent
to authenticate) carrying a short dialogue of *messages*, each one value in
DEncode or JSON (:mod:`xrddirac.codec`) framed as ``<length>:<bytes>``::

    client  S_OK([[path, setup, vo], [kind, name], extra_credentials, version])
    server  S_OK()                          the action is accepted (or S_ERROR)
    client  S_OK([arg, ...])                RPC: the arguments
    server  S_OK(value) | S_ERROR(message)  the answer, then the server closes

``kind`` is ``RPC`` or ``FileTransfer``; a transfer streams chunks instead
of arguments (:meth:`DisetClient.receive_file`, :meth:`DisetClient.send_file`).
Either side may interleave a keep-alive (``dka`` before the length, a
``{"id", "kaping"}`` value), which :class:`Wire` answers and skips.

``S_OK``/``S_ERROR`` are dicts - ``{"OK": True, "Value": v}`` and
``{"OK": False, "Message": m, "Errno": n}`` - and :meth:`DisetClient.call`
unwraps them: the value, or :class:`~xrddirac.errors.ServiceError`.

Failover: a service usually has several URLs (the configuration lists a
dozen ``FileCatalog`` hosts). A call starts at a rotating position, so
parallel callers spread out, and moves to the next URL only when it could not
*connect or propose* there - once arguments are sent, a failure is reported
rather than repeated, because the call may have taken effect.

What must not break: nothing here logs a message body (it may carry a DiracX
token, a JDL or a delegated proxy), and a message's declared length is
bounded before it is read, so a hostile peer cannot make the client buffer
gigabytes.
"""

from __future__ import annotations

import hashlib
import itertools
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol
from urllib.parse import urlsplit

from xrdcore.errors import ConnectionError as XrdConnectionError
from xrdcore.errors import TimeoutError as XrdTimeoutError
from xrdcore.log import get_logger

from . import codec
from ._version import __version__
from .errors import ENOAUTH, AuthorizationError, ConfigError, ServiceError, WireError

__all__ = [
    "MAX_MESSAGE",
    "DisetClient",
    "ServiceURL",
    "Wire",
    "s_error",
    "s_ok",
    "unwrap",
]

_log = get_logger(__name__)

#: The keep-alive marker that may precede a message's length.
KEEPALIVE = b"dka"
#: The longest message accepted (the LHCb configuration is ~0.3 MB compressed).
MAX_MESSAGE = 256 * 1024 * 1024
#: The length prefix is at most this many digits (and the colon).
_MAX_PREFIX = 16
#: Bytes per file-transfer chunk, as DIRAC's ``FileHelper`` sends them.
CHUNK = 1024 * 1024
#: Sent as the client's version; DIRAC only logs it.
CLIENT_VERSION = f"xrddirac/{__version__}"


def s_ok(value: Any = None) -> dict[str, Any]:
    """DIRAC's success structure."""
    return {"OK": True, "Value": value}


def s_error(message: str, errno: int = 0) -> dict[str, Any]:
    """DIRAC's failure structure."""
    return {"OK": False, "Message": message, "Errno": errno}


def unwrap(answer: Any, service: str = "", method: str = "") -> Any:
    """``S_OK``'s value; ``S_ERROR`` raised as :class:`ServiceError` (or a subclass)."""
    if not isinstance(answer, dict) or "OK" not in answer:
        raise WireError(f"{service}.{method}: the answer is not S_OK/S_ERROR")
    if answer["OK"]:
        return answer.get("Value")
    message = str(answer.get("Message", "no message"))
    errno = answer.get("Errno")
    errno = errno if isinstance(errno, int) else 0
    kind = AuthorizationError if errno == ENOAUTH or _unauthorised(message) else ServiceError
    raise kind(message, errno=errno, service=service, method=method)


def _unauthorised(message: str) -> bool:
    """DIRAC 8 refuses some calls without an errno; its words for it."""
    text = message.lower()
    return "unauthorized" in text or "not authorized" in text


@dataclass(frozen=True, slots=True)
class ServiceURL:
    """``dips://host:port/System/Component``, parsed."""

    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, text: str) -> ServiceURL:
        """A ``dips://`` URL; :class:`ConfigError` for anything else."""
        parts = urlsplit(text.strip())
        path = parts.path.strip("/")
        if parts.scheme != "dips" or not parts.hostname or path.count("/") != 1:
            raise ConfigError(
                f"Not a DISET service URL (dips://host:port/System/Component): {text!r}"
            )
        return cls(parts.hostname, parts.port or 9135, path)

    def __str__(self) -> str:
        """The URL again."""
        return f"dips://{self.host}:{self.port}/{self.path}"


class Stream(Protocol):
    """What :class:`Wire` needs of a socket."""

    def sendall(self, data: bytes, /) -> None:
        """Send every byte."""
        ...

    def recv(self, size: int, /) -> bytes:
        """Up to ``size`` bytes; empty at the end of the stream."""
        ...


class Wire:
    """Messages over one stream: ``<len>:<bytes>``, keep-alives answered and skipped.

    ``encoding`` is what :meth:`send` writes; :meth:`receive` reads either.
    """

    def __init__(self, stream: Stream, encoding: codec.Encoding = "json") -> None:
        self.stream = stream
        self.encoding: codec.Encoding = encoding
        self._buffer = bytearray()

    def send(self, value: Any, prefix: bytes = b"") -> None:
        """One message."""
        body = codec.encode(value, self.encoding)
        self.stream.sendall(b"".join((prefix, str(len(body)).encode(), b":", body)))

    def receive(self, limit: int = MAX_MESSAGE) -> Any:
        """The next message that is not a keep-alive (pings are answered)."""
        while True:
            keepalive, value = self.receive_any(limit)
            if not keepalive:
                return value
            self._keepalive(value)

    def receive_any(self, limit: int = MAX_MESSAGE) -> tuple[bool, Any]:
        """The next message, keep-alive or not: ``(is_keepalive, value)``, nothing answered."""
        keepalive, body = self._frame(limit)
        return keepalive, codec.decode(body)

    def _keepalive(self, value: Any) -> None:
        """Answer a ping with a pong; a pong needs nothing."""
        inner = value.get("Value") if isinstance(value, dict) else None
        if isinstance(inner, dict) and inner.get("kaping"):
            self.send(s_ok({"id": inner.get("id"), "kaping": False}), prefix=KEEPALIVE)

    def _frame(self, limit: int) -> tuple[bool, bytes]:
        """``(is_keepalive, body)`` of the next frame."""
        keepalive = False
        while True:
            if self._buffer[:3] == KEEPALIVE:
                del self._buffer[:3]
                keepalive = True
            colon = self._buffer.find(b":")
            if colon >= 0:
                break
            if len(self._buffer) > _MAX_PREFIX:
                raise WireError("DISET: no message length where one was expected")
            self._fill()
        length = self._length(bytes(self._buffer[:colon]), limit)
        del self._buffer[: colon + 1]
        while len(self._buffer) < length:
            self._fill()
        body = bytes(self._buffer[:length])
        del self._buffer[:length]
        return keepalive, body

    @staticmethod
    def _length(text: bytes, limit: int) -> int:
        """The declared length, checked against ``limit``."""
        if not text.isdigit():
            raise WireError(f"DISET: bad message length {text[:_MAX_PREFIX]!r}")
        length = int(text)
        if length > limit:
            raise WireError(f"DISET: a {length}-byte message is over the {limit}-byte limit")
        return length

    def _fill(self) -> None:
        """Read more from the stream; the end of it here is an error."""
        data = self.stream.recv(65536)
        if not data:
            raise XrdConnectionError("DISET: the server closed the connection mid-message")
        self._buffer += data


#: ``() -> SSLContext``: the client's TLS context (its proxy), built when first needed.
ContextFactory = Callable[[], ssl.SSLContext]

#: ``(url) -> Stream``: how a connection is opened (tests replace it).
Opener = Callable[["DisetClient", ServiceURL], Stream]


class DisetClient:
    """Calls to one DIRAC service (all its URLs), each over a connection of its own.

    ``urls`` are the service's ``dips://`` URLs (same ``System/Component``);
    ``vo`` and ``setup`` go into every proposal (DIRAC 9 ignores the setup,
    DIRAC 8 reads it); ``tls`` builds the TLS context - the proxy and the
    grid CAs - on first use. ``timeout`` bounds every read, ``connect_timeout``
    the TCP and TLS handshake. ``encoding`` is what is sent: ``json`` as
    DIRAC 9 clients send, or ``dencode`` for a server older than DIRAC 7.3.

    Safe to share between threads: each call has its own connection, and
    the URL rotation is an atomic counter.
    """

    def __init__(
        self,
        urls: Sequence[str],
        *,
        tls: ContextFactory,
        vo: str = "unknown",
        setup: str = "None",
        timeout: float = 600.0,
        connect_timeout: float = 30.0,
        encoding: codec.Encoding = "json",
        extra_credentials: str = "",
        opener: Opener | None = None,
    ) -> None:
        parsed = [ServiceURL.parse(url) for url in urls]
        if not parsed:
            raise ConfigError("A DISET client needs at least one service URL")
        services = {url.path for url in parsed}
        if len(services) != 1:
            raise ConfigError(f"One client is one service, not {sorted(services)}")
        self.urls = parsed
        self.service = parsed[0].path
        self.tls = tls
        self.vo = vo
        self.setup = setup
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.encoding: codec.Encoding = encoding
        self.extra_credentials = extra_credentials
        self._open = opener or _open_tls
        self._turn = itertools.count()

    def __repr__(self) -> str:
        """The service and how many URLs it has."""
        return f"DisetClient({self.service!r}, {len(self.urls)} URL(s))"

    # -- calls ---------------------------------------------------------------------------

    def call(self, method: str, *args: Any) -> Any:
        """``method(*args)`` on the service: its value, or :class:`ServiceError`."""
        return unwrap(self.request(method, *args), self.service, method)

    def request(self, method: str, *args: Any) -> dict[str, Any]:
        """``method(*args)``'s whole answer, ``S_OK`` or ``S_ERROR``, unread.

        For the calls that answer more than a value: ``JobManager.killJob``
        reports ``InvalidJobIDs`` beside it, ``submitJob`` a
        ``requireProxyUpload`` flag, ``ResourceStatus.select`` its
        ``Columns``. Anything but an ``S_OK``/``S_ERROR`` dict is a
        :class:`WireError`.
        """
        started = time.monotonic()
        wire, stream, url = self._connect(("RPC", method))
        try:
            wire.send(s_ok(list(args)))
            answer = wire.receive()
        except OSError as exc:
            raise _network(exc, f"{self.service}.{method} at {url}") from exc
        finally:
            _close(stream)
        _log.debug(
            "DISET %s.%s at %s: %.3f s", self.service, method, url, time.monotonic() - started
        )
        if not isinstance(answer, dict) or "OK" not in answer:
            raise WireError(f"{self.service}.{method}: the answer is not S_OK/S_ERROR")
        return answer

    def receive_file(self, file_id: Any, sink: BinaryIO, *, token: str = "", limit: int = 0) -> int:
        """Download ``file_id`` into ``sink``; the byte count. The MD5 trailer is checked.

        ``limit`` (when positive) refuses a file larger than that many bytes.
        """
        wire, stream, url = self._connect(("FileTransfer", "ToClient"))
        try:
            wire.send(s_ok([file_id, token]))
            unwrap(wire.receive(), self.service, "ToClient")
            size = _receive_chunks(wire, sink, limit, f"{self.service} at {url}")
            unwrap(wire.receive(), self.service, "ToClient")
        except OSError as exc:
            raise _network(exc, f"{self.service} download at {url}") from exc
        finally:
            _close(stream)
        return size

    def send_file(self, file_id: Any, source: BinaryIO, size: int, *, token: str = "") -> Any:
        """Upload ``size`` bytes of ``source`` as ``file_id``; the service's answer's value."""
        wire, stream, url = self._connect(("FileTransfer", "FromClient"))
        try:
            wire.send(s_ok([file_id, token, size]))
            unwrap(wire.receive(), self.service, "FromClient")
            digest = hashlib.md5(usedforsecurity=False)
            while chunk := source.read(CHUNK):
                digest.update(chunk)
                wire.send(s_ok([True, chunk]))
                unwrap(wire.receive(), self.service, "FromClient")
            wire.send(s_ok([False, digest.hexdigest()]))
            answer = wire.receive()
        except OSError as exc:
            raise _network(exc, f"{self.service} upload at {url}") from exc
        finally:
            _close(stream)
        return unwrap(answer, self.service, "FromClient")

    # -- connections -----------------------------------------------------------------------

    def _connect(self, action: tuple[str, str]) -> tuple[Wire, Stream, ServiceURL]:
        """A connection on which ``action`` was accepted, failing over URL by URL."""
        start = next(self._turn) % len(self.urls)
        order = self.urls[start:] + self.urls[:start]
        failures: list[str] = []
        for url in order:
            try:
                stream = self._open(self, url)
            except OSError as exc:
                failures.append(f"{url}: {_reason(exc)}")
                continue
            wire = Wire(stream, self.encoding)
            try:
                self._propose(wire, url, action)
            except OSError as exc:
                _close(stream)
                failures.append(f"{url}: {_reason(exc)}")
                continue
            except BaseException:
                _close(stream)
                raise
            return wire, stream, url
        raise XrdConnectionError(f"No {self.service} server answered: " + "; ".join(failures))

    def _propose(self, wire: Wire, url: ServiceURL, action: tuple[str, str]) -> None:
        """Send the connection info and the action; the server's ``S_ERROR`` is raised."""
        info = [
            [url.path, self.setup, self.vo],
            list(action),
            self.extra_credentials,
            CLIENT_VERSION,
        ]
        wire.send(s_ok(info))
        answer = wire.receive()
        unwrap(answer, self.service, action[1])
        if isinstance(answer.get("Value"), dict) and "delegate" in answer["Value"]:
            raise ServiceError(
                "The server asked for a delegation, which xrddirac does not do on a call",
                service=self.service,
                method=action[1],
            )


def _receive_chunks(wire: Wire, sink: BinaryIO, limit: int, where: str) -> int:
    """Read ``[True, chunk]`` messages (acknowledging each) up to ``[False, md5]``."""
    digest = hashlib.md5(usedforsecurity=False)
    size = 0
    while True:
        value = unwrap(wire.receive(), where, "ToClient")
        if not isinstance(value, list) or len(value) != 2:
            raise WireError(f"{where}: a file-transfer message is not [flag, data]")
        more, data = value
        if not more:
            if isinstance(data, str) and data and data != digest.hexdigest():
                raise WireError(f"{where}: the file's MD5 does not match")
            return size
        chunk = _chunk_bytes(data)
        size += len(chunk)
        if 0 < limit < size:
            raise WireError(f"{where}: the file is larger than {limit} bytes")
        digest.update(chunk)
        sink.write(chunk)
        wire.send(s_ok())


def _chunk_bytes(data: Any) -> bytes:
    """A chunk as bytes: JSON brings bytes (decoded from ``b64``), DEncode text with surrogates."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8", "surrogateescape")
    raise WireError("A file-transfer chunk is neither bytes nor text")


def _open_tls(client: DisetClient, url: ServiceURL) -> Stream:
    """TCP, then TLS with the client's context and the host name checked."""
    context = client.tls()
    raw = socket.create_connection((url.host, url.port), timeout=client.connect_timeout)
    try:
        stream = context.wrap_socket(raw, server_hostname=url.host)
    except BaseException:
        raw.close()
        raise
    stream.settimeout(client.timeout)
    return stream


def _close(stream: Stream) -> None:
    """Close the stream if it can be closed; a failure to close is not news."""
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except OSError:
            _log.debug("DISET: closing a connection failed", exc_info=True)


def _reason(exc: BaseException) -> str:
    """A failure in a few words, for the "no server answered" message."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"certificate not trusted ({exc.verify_message})"
    if isinstance(exc, socket.timeout | TimeoutError):
        return "timed out"
    return str(exc) or type(exc).__name__


def _network(exc: OSError, where: str) -> OSError:
    """A network failure during a call, as xrdcore's connection or timeout error."""
    if isinstance(exc, XrdConnectionError):
        return exc
    if isinstance(exc, socket.timeout | TimeoutError):
        return XrdTimeoutError(f"{where}: timed out")
    return XrdConnectionError(f"{where}: {_reason(exc)}")
