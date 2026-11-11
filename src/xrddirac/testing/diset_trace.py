"""Turn a recorded DISET conversation into evidence: frames, values, and a hash.

What this is for: the clean-room claim in ``docs/CLEANROOM.md`` rests on facts
about the protocol, and the strongest fact is a recorded conversation with a real
server. This module reads such a conversation — the plaintext byte stream of one
connection, as a key log and `tshark`/`scapy` can recover from a capture — and
writes it out as a transcript a reviewer can check against
``docs/diset-spec.md``:

    from xrddirac.testing import diset_trace
    trace = diset_trace.read(bytes.fromhex(path.read_text()))
    path.write_text(diset_trace.as_markdown(trace))

**Redaction is not optional.** A DISET conversation may carry a DiracX token, a
JDL, a delegated proxy or a job's sandbox contents; none of that belongs in a
repository, and a transcript that leaks it is worse than no evidence at all. So
every value is redacted before it is written: strings that look like a token,
a PEM, a proxy or a password become ``<redacted: n bytes>``, and the redaction
is *recorded* (a reviewer can see that something was there, and how big it was)
rather than silently dropped.

Stability: test and evidence API, versioned with the package.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .. import codec
from ..diset import KEEPALIVE, MAX_MESSAGE

__all__ = [
    "Message",
    "Trace",
    "as_markdown",
    "digest",
    "read",
    "redact",
]

#: A value longer than this is truncated in the transcript: a message body is
#: not the evidence, its shape is.
_VALUE_LIMIT = 400

#: What a secret looks like, by the text around it. Deliberately broad: a false
#: positive costs a redacted field, a false negative costs a leaked token.
_SECRET = re.compile(
    r"(?i)(bearer\s|-----BEGIN [A-Z0-9 ]+-----|access_token|refresh_token|"
    r"client_secret|password|passwd|private key|proxy\.pem|token=)"
)

#: A JWT, wherever it appears: three base64url segments with a JSON header.
_JWT = re.compile(r"ey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

#: Mapping keys whose *value* is a credential whatever it looks like. A token
#: can be short, opaque and unremarkable - `access_token: "abc"` - so the key
#: decides, not the shape of the value.
_SECRET_KEY = re.compile(
    r"(?i)^(access_token|refresh_token|id_token|token|client_secret|secret|"
    r"password|passwd|proxy|credential|private_key)$"
)


@dataclass(frozen=True, slots=True)
class Message:
    """One frame of the conversation, as the transcript needs it."""

    #: Which way it went: ``client`` or ``server``.
    side: str
    #: The frame's declared payload length, in bytes.
    length: int
    #: ``keepalive``, ``value`` or ``undecodable``.
    kind: str
    #: The decoded value, redacted; ``None`` for a keep-alive or a bad frame.
    value: Any = None
    #: What went wrong, for a frame that could not be decoded.
    problem: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Plain values, for the JSON evidence file."""
        return {
            "side": self.side,
            "kind": self.kind,
            "length": self.length,
            "value": self.value,
            "problem": self.problem,
        }


@dataclass(frozen=True, slots=True)
class Trace:
    """A whole conversation: the frames, the encoding and the digest of the bytes."""

    messages: tuple[Message, ...]
    #: ``sha256`` of the exact bytes read, so a transcript can be tied to a capture.
    sha256: str
    #: How many bytes were read, which is how a truncated capture is spotted.
    size: int
    #: Which encodings were seen: ``json``, ``dencode`` or both.
    encodings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Plain values, for the JSON evidence file."""
        return {
            "sha256": self.sha256,
            "bytes": self.size,
            "encodings": list(self.encodings),
            "messages": [one.as_dict() for one in self.messages],
        }


def digest(data: bytes) -> str:
    """``sha256`` of ``data``, as the evidence files record it."""
    return hashlib.sha256(data).hexdigest()


def read(stream: bytes, *, side: str = "client") -> Trace:
    """The conversation in one direction of a connection, as a :class:`Trace`.

    ``side`` is whose direction the bytes are. Framing follows the specification:
    a message is ``length ":" payload``, and the three bytes ``dka`` where a
    length would start are a keep-alive. A frame that cannot be decoded is
    recorded as such and skipping continues from the next length, because one bad
    frame should not throw away the evidence around it: a client must not guess,
    but a transcript's job is to show what happened.
    """
    messages: list[Message] = []
    seen: set[str] = set()
    at = 0
    while at < len(stream):
        message, at, encoding = _step(stream, at, side)
        messages.append(message)
        if encoding:
            seen.add(encoding)
    encodings = tuple(name for name in ("json", "dencode") if name in seen)
    return Trace(tuple(messages), digest(stream), len(stream), encodings)


def _step(stream: bytes, at: int, side: str) -> tuple[Message, int, str]:
    """One frame from ``at``: the message, where the next one starts, its encoding."""
    if stream.startswith(KEEPALIVE, at):
        return Message(side=side, length=0, kind="keepalive"), at + len(KEEPALIVE), ""
    colon = stream.find(b":", at, at + 12)
    if colon < 0:
        return _broken(side, "no length before ':'"), len(stream), ""
    return _message(stream, at, colon, side)


def _message(stream: bytes, at: int, colon: int, side: str) -> tuple[Message, int, str]:
    """The message whose length ends at ``colon``, and where the next one starts."""
    try:
        length = int(stream[at:colon])
    except ValueError:
        return _broken(side, f"not a length: {stream[at:colon]!r}"), colon + 1, ""
    if length > MAX_MESSAGE:
        return _broken(side, "over the limit", length=length), len(stream), ""
    start = colon + 1
    payload = stream[start : start + length]
    if len(payload) < length:
        problem = f"truncated: {len(payload)} of {length} bytes"
        return _broken(side, problem, length=length), len(stream), ""
    encoding, value = _decoded(payload)
    if not encoding:
        return _broken(side, "neither JSON nor DEncode", length=length), start + length, ""
    message = Message(side=side, length=length, kind="value", value=redact(value))
    return message, start + length, encoding


def _broken(side: str, problem: str, *, length: int = 0) -> Message:
    """A frame that is not a message, saying why."""
    return Message(side=side, length=length, kind="undecodable", problem=problem)


def _decoded(payload: bytes) -> tuple[str, Any]:
    """The payload as a value, and which encoding read it: ``("", None)`` if neither.

    ``codec.decode`` is the mixed reader the protocol's own decoder is (DEncode
    first, then JSON); which one answered is decided by asking JSON separately,
    so the transcript can say what the peer actually sent.
    """
    import json

    try:
        value = codec.decode(payload)
    except Exception:
        return "", None
    try:
        json.loads(payload.decode("utf-8"))
    except ValueError:
        return "dencode", value
    return "json", value


def redact(value: Any, *, limit: int = _VALUE_LIMIT) -> Any:
    """``value`` with anything that could be a credential replaced, and long strings cut.

    The replacement says what it replaced and how big it was — a reviewer must be
    able to see that a token was there, and that nothing else was hidden.
    """
    if isinstance(value, dict):
        return _redacted_mapping(value, limit=limit)
    if isinstance(value, list | tuple):
        return [redact(item, limit=limit) for item in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return _redacted_text(value, limit=limit)
    return value


def _redacted_text(value: str, *, limit: int) -> str:
    """A string: a credential if it looks like one, else itself, cut if it is long."""
    if _SECRET.search(value) or _JWT.search(value):
        return f"<redacted: {len(value)} chars>"
    return value if len(value) <= limit else f"{value[:limit]}… <{len(value)} chars>"


def _redacted_mapping(value: dict[Any, Any], *, limit: int) -> dict[Any, Any]:
    """A mapping: a credential-named key's value goes whatever it looks like."""
    out: dict[Any, Any] = {}
    for key, item in value.items():
        if isinstance(key, str) and _SECRET_KEY.match(key):
            out[key] = f"<redacted: {len(str(item))} chars>"
        else:
            out[key] = redact(item, limit=limit)
    return out


def as_markdown(trace: Trace, *, side: str = "client") -> str:
    """The transcript a reviewer reads, with the hash that ties it to the bytes."""
    lines = [
        f"# DISET transcript ({side})",
        "",
        f"- bytes: {trace.size}",
        f"- sha256: `{trace.sha256}`",
        f"- encodings seen: {', '.join(trace.encodings) or 'none'}",
        f"- frames: {len(trace.messages)}",
        "",
        "| # | side | kind | length | value |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, message in enumerate(trace.messages):
        value = message.problem or _shown(message.value)
        lines.append(f"| {index} | {message.side} | {message.kind} | {message.length} | {value} |")
    return "\n".join(lines) + "\n"


def _shown(value: Any) -> str:
    """A value as one table cell: JSON where it is structure, plain where it is not."""
    import json

    text = json.dumps(value, sort_keys=True) if isinstance(value, dict | list) else str(value)
    return text.replace("|", "\\|")
