"""DISET, DIRAC's RPC protocol, written from ``docs/diset-spec.md`` §§2, 3, 5, 6, 7.

Every DIRAC 8/9 service still listens on ``dips://host:port/System/Component``.
One call is one TLS connection — the X.509 proxy is the client certificate, and
the server reads the user, VO and group from it — carrying a short dialogue of
framed values::

    client  S_OK([[service, setup, vo], [kind, method], extra_credentials, version])
    server  S_OK()                          the proposal is accepted (§5.2)
    client  S_OK([arg, ...])                the call's arguments (§6.1)
    server  S_OK(value) | S_ERROR(message)  the answer, then the connection closes

The requirements this implements are numbered in ``docs/diset-spec.md``: the
transport (§2), the framing and its keep-alives (§3), the proposal (§5), one call
(§6), failover and timeouts (§7), and the limits of §8 that stop a hostile peer
exhausting a client.

What must not break: nothing here logs a message body (it may carry a token, a
JDL or a delegated proxy — §8.5), a message's declared length is bounded before
it is read (§3.6), a failure travels as a value rather than an exception until it
is unwrapped (§4.4), and nothing a peer names is ever imported (§4.5, §8.6).
"""

from __future__ import annotations

import contextlib
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
    "CHUNK",
    "KEEPALIVE",
    "MAX_MESSAGE",
    "DisetClient",
    "Opener",
    "ServiceURL",
    "Wire",
    "s_error",
    "s_ok",
    "unwrap",
]

_log = get_logger(__name__)

#: The three bytes a keep-alive uses in the place of a length (§3.4).
KEEPALIVE = b"dka"

#: The largest message this will read, in bytes (§3.6). A peer that declares more
#: is refused before anything is allocated for it.
MAX_MESSAGE = 256 * 1024 * 1024

#: How much of a file one transfer message carries (§9, out of scope for the
#: clean-room fallback but still spoken by the code that predates it).
CHUNK = 64 * 1024

#: The default port a ``dips://`` URL may leave out (§2.3).
DEFAULT_PORT = 9135

#: Sent as the client's version in the proposal; the server logs it (§5.1).
CLIENT_VERSION = f"xrddirac/{__version__}"

#: How many keep-alives are tolerated before a reply (§7.3).
MAX_PINGS = 100


# ---------------------------------------------------------------------------
# Results: the two objects every answer is (§4.4)
# ---------------------------------------------------------------------------


def s_ok(value: Any = None) -> dict[str, Any]:
    """A success result, as a peer writes one (§4.4)."""
    return {"OK": True, "Value": value}


def s_error(message: str, errno: int = 0) -> dict[str, Any]:
    """A failure result, as a peer writes one (§4.4)."""
    return {"OK": False, "Errno": errno, "Message": message}


def unwrap(answer: Any, service: str = "", method: str = "") -> Any:
    """``answer``'s value, or the error it carries raised (§6.2, §6.3).

    Anything that is not a result object is a protocol error, not a value to hand
    to a caller: a bare value would be indistinguishable from "the reply was
    empty", and the caller would carry on with wrong data.
    """
    if not isinstance(answer, dict) or "OK" not in answer:
        raise WireError(f"{service}.{method}: the answer is not S_OK/S_ERROR")
    if answer.get("OK"):
        return answer.get("Value")
    message = str(answer.get("Message") or "no message")
    errno = answer.get("Errno")
    number = errno if isinstance(errno, int) and not isinstance(errno, bool) else 0
    kind = AuthorizationError if number == ENOAUTH or _unauthorised(message) else ServiceError
    raise kind(message, errno=number, service=service, method=method)


#: What a server says when it will not authorise the *user* rather than the call.
_UNAUTHORISED = ("unauthorized", "unauthorised", "not authorized", "not authorised", "denied")


def _unauthorised(message: str) -> bool:
    """Whether a refusal is about the identity rather than the call."""
    lowered = message.lower()
    return any(phrase in lowered for phrase in _UNAUTHORISED)


# ---------------------------------------------------------------------------
# Where a service is (§5.1, §7.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceURL:
    """One ``dips://host:port/System/Component`` a service answers on."""

    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, text: str) -> ServiceURL:
        """``text`` as a :class:`ServiceURL`; a bad one is a configuration error."""
        parts = urlsplit(text)
        if parts.scheme != "dips":
            raise ConfigError(f"{text}: a DIRAC service URL begins dips://")
        if not parts.hostname:
            raise ConfigError(f"{text}: no host")
        path = parts.path.strip("/")
        if path.count("/") != 1:
            raise ConfigError(f"{text}: the path is System/Component, not {parts.path!r}")
        return cls(parts.hostname, parts.port or DEFAULT_PORT, path)

    def __str__(self) -> str:
        """The URL as a configuration writes it."""
        return f"dips://{self.host}:{self.port}/{self.path}"

    @property
    def service(self) -> str:
        """The ``System/Component`` this URL names."""
        return self.path


# ---------------------------------------------------------------------------
# The wire: framing, keep-alives, one value at a time (§3)
# ---------------------------------------------------------------------------


#: How a client makes a stream to one URL: the seam a test replaces (§2.1).
Opener = Callable[["DisetClient", ServiceURL], "Stream"]


class Stream(Protocol):
    """The two calls a stream has to offer: ``sendall`` and ``recv`` (§3.3)."""

    def sendall(self, data: bytes, /) -> None:
        """Send every byte, or raise."""

    def recv(self, size: int, /) -> bytes:
        """Read at most ``size`` bytes; an empty answer means the peer closed."""


class Wire:
    """Framed values on one stream (§3)."""

    def __init__(self, stream: Stream, encoding: codec.Encoding = "json") -> None:
        """Frames on ``stream``, written in ``encoding``; either is read back."""
        self.stream = stream
        self.encoding: codec.Encoding = encoding
        self._buffer = bytearray()
        self._eof = False
        self._pings = 0

    # -- writing ---------------------------------------------------------------------

    def send(self, value: Any, prefix: bytes = b"") -> None:
        """Write ``value`` as one frame, with ``prefix`` in front of the length.

        The prefix is how a keep-alive is written: ``dka`` sits where a length
        would (§3.4), so it is sent with the frame rather than on its own.
        """
        payload = codec.encode(value, self.encoding)
        self.stream.sendall(prefix + str(len(payload)).encode() + b":" + payload)

    # -- reading ---------------------------------------------------------------------

    def receive(self, limit: int = MAX_MESSAGE) -> Any:
        """The next value, answering any keep-alive on the way to it (§3.4, §3.5)."""
        found, value = self.receive_any(limit=limit)
        while not found:  # a keep-alive: answer it, keep reading
            self._pings += 1
            if self._pings > MAX_PINGS:
                raise WireError(f"the peer has sent more than {MAX_PINGS} keep-alives")
            value = self.receive_any(limit=limit)[1]
            found = True
        return value

    def receive_any(self, limit: int = MAX_MESSAGE) -> tuple[bool, Any]:
        """``(True, value)`` for a message, or ``(False, ping)`` for a keep-alive (§3.5)."""
        prefix, payload = self._frame(limit)
        if prefix == KEEPALIVE:
            return False, self._ping(payload)
        try:
            return True, codec.decode(payload)
        except codec.CodecError as exc:
            raise WireError(f"the message is not a value in either encoding: {exc}") from exc

    def _ping(self, payload: bytes) -> Any:
        """A keep-alive's own value, answered as §3.5 requires, and returned for a log."""
        try:
            value = codec.decode(payload)
        except codec.CodecError:
            return None
        # The pong carries the id back, and `kaping: false` so the peer stops.
        if isinstance(value, dict) and value.get("kaping"):
            inner = value.get("id")
            self.send(s_ok({"id": inner, "kaping": False}), prefix=KEEPALIVE)
        return value

    def _frame(self, limit: int) -> tuple[bytes, bytes]:
        """One frame: its prefix (``b""`` or ``dka``) and its payload."""
        self._fill()
        if self._buffer.startswith(KEEPALIVE):
            del self._buffer[: len(KEEPALIVE)]
            return KEEPALIVE, self._payload(self._length(limit), limit)
        return b"", self._payload(self._length(limit), limit)

    def _length(self, limit: int) -> int:
        """The declared payload length, refusing one that is not a number or too big."""
        colon = self._buffer.find(b":")
        while colon < 0:
            if len(self._buffer) > 12:  # a length that long is not a length
                raise WireError("no message length in the frame")
            self._fill()
            colon = self._buffer.find(b":")
        text = bytes(self._buffer[:colon])
        del self._buffer[: colon + 1]
        if not text.isdigit():
            raise WireError(f"bad message length: {text!r}")
        length = int(text)
        if length > limit:
            raise WireError(f"a message of {length} bytes is over the {limit}-byte limit")
        return length

    def _payload(self, length: int, limit: int) -> bytes:
        """Exactly ``length`` bytes, read from the stream as needed."""
        _ = limit
        while len(self._buffer) < length:
            self._fill()
        payload = bytes(self._buffer[:length])
        del self._buffer[:length]
        return payload

    def _fill(self) -> None:
        """Read one more chunk into the buffer; a closed stream is an error (§3.7)."""
        if self._eof:
            raise XrdConnectionError("the peer closed the connection mid-message")
        chunk = self.stream.recv(65536)
        if not chunk:
            self._eof = True
            raise XrdConnectionError("the peer closed the connection mid-message")
        self._buffer.extend(chunk)


# ---------------------------------------------------------------------------
# One call (§5, §6, §7)
# ---------------------------------------------------------------------------


class DisetClient:
    """A client for one DIRAC service, over any number of its URLs (§7.1)."""

    def __init__(
        self,
        urls: Sequence[str | ServiceURL],
        *,
        tls: Callable[[], ssl.SSLContext],
        vo: str = "v",
        setup: str = "None",
        encoding: codec.Encoding = "json",
        timeout: float = 120.0,
        connect_timeout: float = 20.0,
        extra_credentials: Any = "",
        opener: Callable[[DisetClient, ServiceURL], Stream] | None = None,
    ) -> None:
        """``urls`` all name the *same* service: one client is one service.

        ``tls`` is called per connection and returns the context to present the
        proxy with, so a credential that is renewed between calls is used fresh.
        """
        parsed = [one if isinstance(one, ServiceURL) else ServiceURL.parse(one) for one in urls]
        if not parsed:
            raise ConfigError("a client needs at least one service URL")
        services = {one.service for one in parsed}
        if len(services) != 1:
            raise ConfigError(f"One client is one service, not {sorted(services)}")
        self.urls = parsed
        self.service = parsed[0].service
        self.tls = tls
        self.vo = vo
        self.setup = setup
        self.encoding: codec.Encoding = encoding
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.extra_credentials = extra_credentials
        self._open = opener or _open_tls
        self._turn = itertools.count()

    def __repr__(self) -> str:
        """The service and how many URLs it has."""
        return f"DisetClient({self.service!r}, {len(self.urls)} URL(s))"

    # -- calls -------------------------------------------------------------------------

    def call(self, method: str, *args: Any) -> Any:
        """``method(*args)`` on the service: its value, or the error it refused with."""
        return unwrap(self.request(method, *args), self.service, method)

    def request(self, method: str, *args: Any) -> dict[str, Any]:
        """``method(*args)``'s whole answer, success or failure, unwrapped.

        Some methods answer more than a value — ``killJob`` reports
        ``InvalidJobIDs`` beside it, ``submitJob`` a ``requireProxyUpload`` flag —
        so a caller that needs those asks for the whole result.
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
        if not isinstance(answer, dict) or "OK" not in answer:
            raise WireError(f"{self.service}.{method}: the answer is not S_OK/S_ERROR")
        _log.debug(
            "DISET %s.%s at %s: %.3f s", self.service, method, url, time.monotonic() - started
        )
        return answer

    # -- file transfer (§9: kept for the code that predates this module) ---------------

    def receive_file(self, file_id: Any, sink: BinaryIO, *, token: str = "", limit: int = 0) -> int:
        """Ask for ``file_id`` and write it into ``sink``; the bytes received."""
        wire, stream, url = self._connect(("FileTransfer", "ToClient"))
        digest = hashlib.sha256()
        written = 0
        try:
            wire.send(s_ok([file_id, token, limit]))
            unwrap(wire.receive(), self.service, "ToClient")
            while True:
                more, chunk = unwrap(wire.receive(), self.service, "ToClient")
                if not more:
                    break
                sink.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                wire.send(s_ok())
            unwrap(wire.receive(), self.service, "ToClient")
        except OSError as exc:
            raise _network(exc, f"{self.service} at {url}") from exc
        finally:
            _close(stream)
        return written

    def send_file(self, file_id: Any, source: BinaryIO, size: int = 0) -> None:
        """Send ``source`` as ``file_id``; ``size`` is advisory (§9)."""
        del size
        wire, stream, url = self._connect(("FileTransfer", "FromClient"))
        digest = hashlib.sha256()
        try:
            wire.send(s_ok([file_id]))
            unwrap(wire.receive(), self.service, "FromClient")
            while chunk := source.read(CHUNK):
                digest.update(chunk)
                wire.send(s_ok([True, chunk]))
                unwrap(wire.receive(), self.service, "FromClient")
            wire.send(s_ok([False, digest.hexdigest()]))
            unwrap(wire.receive(), self.service, "FromClient")
        except OSError as exc:
            raise _network(exc, f"{self.service} at {url}") from exc
        finally:
            _close(stream)

    # -- connecting (§2, §5, §7.1) ----------------------------------------------------

    def _connect(self, action: tuple[str, str]) -> tuple[Wire, Stream, ServiceURL]:
        """Connect somewhere the proposal is accepted, and propose ``action``.

        Failover moves to the next URL only while nothing has been sent that
        could have taken effect: connect and propose are repeatable, the call's
        arguments are not, so a failure after them is reported (§7.1).
        """
        refusals: list[str] = []
        for url in self._order():
            try:
                stream = self._open(self, url)
            except (OSError, ssl.SSLError) as exc:
                refusals.append(f"{url}: {_reason(exc)}")
                continue
            wire = Wire(stream, self.encoding)
            try:
                self._propose(wire, url, action)
            except (WireError, XrdConnectionError, OSError) as exc:
                _close(stream)
                refusals.append(f"{url}: {exc}")
                continue
            return wire, stream, url
        raise XrdConnectionError(
            f"No {self.service} server answered: " + "; ".join(refusals),
        )

    def _order(self) -> list[ServiceURL]:
        """The URLs, starting at a rotating position so callers spread out (§7.1)."""
        offset = next(self._turn) % len(self.urls)
        return [*self.urls[offset:], *self.urls[:offset]]

    def _propose(self, wire: Wire, url: ServiceURL, action: tuple[str, str]) -> None:
        """Send §5.1's proposal and read §5.2's answer."""
        proposal = [
            [url.service, self.setup, self.vo],
            [action[0], action[1]],
            self.extra_credentials,
            CLIENT_VERSION,
        ]
        wire.send(s_ok(proposal))
        answer = wire.receive()
        value = unwrap(answer, self.service, action[1])
        if isinstance(value, dict) and "delegate" in value:
            # §8.3: a server offering credentials is a confused deputy, refused.
            raise WireError(f"{self.service} asked for delegation, which this refuses")


def _open_tls(client: DisetClient, url: ServiceURL) -> Stream:
    """TCP, then TLS with the client's context and the host name checked (§2.1)."""
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
    """Close a stream, whatever closing it does."""
    closer = getattr(stream, "close", None)
    if closer is not None:
        with contextlib.suppress(OSError):
            closer()


def _network(exc: BaseException, where: str) -> Exception:
    """``exc`` as the chain's own error, for a call that failed on the wire (§7.2)."""
    if isinstance(exc, XrdConnectionError | XrdTimeoutError):
        return exc
    if isinstance(exc, TimeoutError | socket.timeout):
        return XrdTimeoutError(f"{where}: timed out")
    return XrdConnectionError(f"{where}: {_reason(exc)}")


def _reason(exc: BaseException) -> str:
    """A short reason for a transport failure, naming the TLS verdict when there is one."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"certificate not trusted ({exc.verify_message})"
    if isinstance(exc, TimeoutError | socket.timeout):
        return "timed out"
    return str(exc) or type(exc).__name__
