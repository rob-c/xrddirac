"""DISET's two encodings, written from ``docs/diset-spec.md`` §4: JSON and DEncode.

A DISET message is one value in one of two encodings, and a peer may use either:
DIRAC 9 clients send JSON, older servers answer in DEncode, and a server reads
whichever arrives. This module is the pair of codecs and nothing else — it does
no I/O, keeps no state, and imports nothing.

The requirements it implements, by number, are in ``docs/diset-spec.md``:
a payload is one value (§4.1), a client sends JSON and a decoder takes both
(§4.2), decoding is *total* — malformed input raises :class:`CodecError` rather
than half a value (§4.3) — tuples survive a round trip (§4.5), NaN and infinity
are refused (§4.6), and a payload is one value with nothing after it (§4.7).

**Nothing a peer names is ever imported** (§4.5, §8.6): DIRAC's JSON can tag an
arbitrary class by module and name, and its own decoder imports it. Here an
unknown tag stays the plain object that arrived, which is both safer and the
only behaviour a library may have.

DEncode's grammar, as this project's fixtures show it::

    i42e I42e            integer, unsigned integer
    f1.5e f2e+20e        float, float with an exponent
    b1 b0 n              true, false, null
    s5:hello             bytes (length in bytes, then the bytes)
    u5:hello             text (length in bytes, then UTF-8)
    l…e t…e d…e          list, tuple, dict (keys and values interleaved)
    za… zd… zt…          a datetime, a date, a time: the letter then a tuple

JSON's, beyond what JSON itself says::

    {"__dCls": "b64",   "obj": "<base64>"}     bytes
    {"__dCls": "dt",    "obj": [y, m, d, …]}   datetime
    {"__dCls": "date",  "obj": [y, m, d]}      date
    {"__dCls": "time",  "obj": [h, m, s, us]}  time
    {"__dCls": "tuple", "obj": [ … ]}          a tuple, so it is not read back as a list
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from .errors import WireError

__all__ = ["CodecError", "Encoding", "decode", "dencode", "encode", "jencode", "sniff"]

#: Which encoding a message is in. A client sends one; a decoder accepts both.
Encoding = Literal["json", "dencode"]

#: The tag JSON uses for a value plain JSON cannot carry.
TAG = "__dCls"

#: The member that carries a tagged value.
OBJECT = "obj"


class CodecError(WireError):
    """A payload is not a value in either encoding, or not one this will accept.

    A :class:`~xrddirac.errors.WireError` because that is what a caller of
    :func:`decode` has to handle: the wire carried something no peer may send.
    """


# ---------------------------------------------------------------------------
# Choosing, and the one entry point a caller needs
# ---------------------------------------------------------------------------


def encode(value: Any, encoding: Encoding = "json") -> bytes:
    """``value`` in ``encoding`` — JSON unless DEncode was asked for (§4.1)."""
    return dencode(value) if encoding == "dencode" else jencode(value)


def decode(data: bytes) -> Any:
    """One value, in whichever encoding it arrived (§4.2, §4.3).

    DEncode is tried first, then JSON: DEncode's type letters make it decisive,
    where a JSON reader would accept only some of what DEncode writes. Anything
    neither reads is a :class:`CodecError`.
    """
    if isinstance(data, str):  # a caller that decoded already: be kind, not clever
        data = data.encode("utf-8")
    first = _failure = None
    for reader in (_dedecode, _jdecode):
        try:
            return reader(data)
        except CodecError as exc:
            first = first or exc
            _failure = exc
    raise CodecError(f"not a DISET value in DEncode or JSON: {_failure or first}")


def sniff(data: bytes) -> Encoding:
    """Which encoding ``data`` is in, for a report or a test (§4.2)."""
    try:
        _jdecode(data)
    except CodecError:
        return "dencode"
    return "json"


# ---------------------------------------------------------------------------
# JSON (the encoding a client sends)
# ---------------------------------------------------------------------------


def jencode(value: Any) -> bytes:
    """``value`` as JSON, tagging what JSON cannot carry (§4.1, §4.5, §4.6)."""
    return _json_bytes(_tag(value))


def _json_bytes(value: Any) -> bytes:
    """JSON bytes for a value already reduced to JSON-native ones."""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise CodecError(f"cannot be written as JSON: {exc}") from exc


def _tag(value: Any) -> Any:
    """``value`` with every non-JSON value replaced by its tagged form."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):  # §4.6
            raise CodecError(f"JSON cannot carry {value!r}")
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return {TAG: "b64", OBJECT: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, _dt.datetime):
        moment = [value.year, value.month, value.day, value.hour, value.minute, value.second]
        return {TAG: "dt", OBJECT: [*moment, value.microsecond]}
    if isinstance(value, _dt.date):
        return {TAG: "date", OBJECT: [value.year, value.month, value.day]}
    if isinstance(value, _dt.time):
        return {TAG: "time", OBJECT: [value.hour, value.minute, value.second, value.microsecond]}
    if isinstance(value, tuple):
        return {TAG: "tuple", OBJECT: [_tag(item) for item in value]}
    if isinstance(value, list):
        return [_tag(item) for item in value]
    if isinstance(value, Mapping):
        return {_key(key): _tag(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_tag(item) for item in value]
    # a value this cannot write is the caller's mistake, not the peer's
    raise TypeError(f"cannot be written as JSON: {type(value).__name__}")


def _key(key: Any) -> str:
    """A mapping key as JSON's object key: a string, as the protocol's keys are."""
    if isinstance(key, str):
        return key
    if isinstance(key, bytes):
        return key.decode("utf-8", "surrogateescape")
    raise CodecError(f"a mapping key must be a string, not {type(key).__name__}")


def _jdecode(data: bytes) -> Any:
    """One JSON value, with the tags this knows resolved and the rest left alone."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CodecError(f"not JSON: {exc}") from exc
    return _untag(value)


def _untag(value: Any) -> Any:
    """``value`` with every tagged object this knows turned back into its value."""
    if isinstance(value, list):
        return [_untag(item) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get(TAG)
    if isinstance(tag, str):
        resolved = _resolved(tag, value)
        if resolved is not _UNKNOWN:
            return resolved
    return {key: _untag(item) for key, item in value.items()}


class _Unknown:
    """A marker: this tag was understood by whoever wrote it, not by us."""


_UNKNOWN = _Unknown()


def _resolved(tag: str, value: Mapping[str, Any]) -> Any:
    """The value a tag carries, or :data:`_UNKNOWN` when it is not one this knows.

    A tag this does not know is *not* an error: an object that travels as itself
    is the only safe reading. Nothing is imported, ever (§4.5).
    """
    if tag == "b64":
        carried = value.get(OBJECT)
        if not isinstance(carried, str):
            raise CodecError(f"bad {tag!r}: it carries no string")
        try:
            return base64.b64decode(carried, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CodecError(f"a b64 tag carries something that is not base64: {exc}") from exc
    if OBJECT not in value:
        # a tag with nothing under it says nothing: pass the object on
        return _UNKNOWN
    if tag in {"dt", "date", "time"}:
        parts = value.get(OBJECT)
        if not isinstance(parts, list):
            # a *known* tag with the wrong contents is a wire error, not an
            # object to pass on: only an unknown tag travels as itself
            raise CodecError(f"bad {tag!r}: it carries {type(parts).__name__}, not a list")
        return _moment(tag, parts)
    if tag == "tuple":
        parts = value.get(OBJECT)
        return _UNKNOWN if not isinstance(parts, list) else tuple(_untag(item) for item in parts)
    return _UNKNOWN


def _moment(tag: str, parts: Sequence[Any]) -> Any:
    """A datetime, date or time from the numbers a tag carries."""
    numbers = [int(part) for part in parts if isinstance(part, int | float)]
    try:
        if tag == "dt":
            year, month, day, hour, minute, second, *rest = numbers
            return _dt.datetime(year, month, day, hour, minute, second, rest[0] if rest else 0)
        if tag == "date":
            year, month, day = numbers[:3]
            return _dt.date(year, month, day)
        hour, minute, second, *rest = numbers
        return _dt.time(hour, minute, second, rest[0] if rest else 0)
    except (TypeError, ValueError) as exc:
        raise CodecError(f"bad {tag!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# DEncode (the encoding an older server answers in)
# ---------------------------------------------------------------------------


def dencode(value: Any) -> bytes:
    """``value`` in DEncode (§4.1)."""
    out: list[bytes] = []
    _put(value, out)
    return b"".join(out)


def _put(value: Any, out: list[bytes]) -> None:
    """Append ``value``'s DEncode form to ``out``."""
    if value is None:
        out.append(b"n")
    elif value is True or value is False:
        out.append(b"b1" if value else b"b0")
    elif isinstance(value, int):
        # `i` is the protocol's integer; `I` is accepted on the way in because a
        # peer may use the unsigned spelling.
        out.append(b"i%de" % value)
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):  # §4.6
            raise CodecError(f"DEncode cannot carry {value!r}")
        out.append(_float(value))
    elif isinstance(value, str):
        raw = value.encode("utf-8")
        out.append(b"u%d:%s" % (len(raw), raw))
    elif isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        out.append(b"s%d:%s" % (len(raw), raw))
    elif isinstance(value, _dt.datetime):
        out.append(b"za")
        _put(
            (
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
                None,
            ),
            out,
        )
    elif isinstance(value, _dt.date):
        out.append(b"zd")
        _put((value.year, value.month, value.day), out)
    elif isinstance(value, _dt.time):
        out.append(b"zt")
        _put((value.hour, value.minute, value.second, value.microsecond, None), out)
    elif isinstance(value, list | tuple):
        # JSON has one sequence type, so a tuple decodes to a tuple only when
        # DEncode wrote it: `t` is the protocol's tuple (§4.5).
        out.append(b"t" if isinstance(value, tuple) else b"l")
        for item in value:
            _put(item, out)
        out.append(b"e")
    elif isinstance(value, Mapping):
        out.append(b"d")
        for key, item in value.items():
            _put(key, out)
            _put(item, out)
        out.append(b"e")
    else:
        # A value this cannot encode is the *caller's* mistake, not the peer's.
        raise TypeError(f"cannot be written as DEncode: {type(value).__name__}")


def _float(value: float) -> bytes:
    """A float the way DEncode writes one: ``f<mantissa>e<exponent>e``, or ``f<value>e``."""
    text = repr(value)
    if "e" in text or "E" in text:
        mantissa, _, exponent = text.lower().partition("e")
        return b"f%se%se" % (mantissa.encode(), exponent.encode())
    return b"f%se" % text.encode()


class _Reader:
    """A cursor over DEncode bytes; every read either yields a value or raises."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0

    def byte(self) -> bytes:
        """The next byte."""
        if self.at >= len(self.data):
            raise CodecError("DEncode ended in the middle of a value")
        byte = self.data[self.at : self.at + 1]
        self.at += 1
        return byte

    def until(self, stop: bytes) -> bytes:
        """The bytes up to ``stop``, which is consumed; ``stop`` must be there."""
        found = self.data.find(stop, self.at)
        if found < 0:
            raise CodecError(f"DEncode has no {stop!r} where one is required")
        text = self.data[self.at : found]
        self.at = found + 1
        return text

    def sized(self) -> bytes:
        """``<length>:<bytes>``, the length already consumed."""
        length = self.until(b":")
        try:
            count = int(length)
        except ValueError as exc:
            raise CodecError(f"DEncode has a length that is not a number: {length!r}") from exc
        found = self.data[self.at : self.at + count]
        if len(found) < count:
            raise CodecError(f"DEncode promised {count} bytes and has {len(found)}")
        self.at += count
        return found

    def value(self) -> Any:
        """One value."""
        kind = self.byte()
        if kind == b"n":
            return None
        if kind in (b"i", b"I"):
            text = self.until(b"e")
            try:
                return int(text)
            except ValueError as exc:
                raise CodecError(f"DEncode integer is not a number: {text!r}") from exc
        if kind == b"b":
            flag = self.byte()
            if flag == b"1":
                return True
            if flag == b"0":
                return False
            raise CodecError(f"DEncode boolean is neither 0 nor 1: {flag!r}")
        if kind == b"f":
            return self._float()
        if kind == b"s":
            return self.sized()
        if kind == b"u":
            raw = self.sized()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CodecError(f"DEncode text is not UTF-8: {exc}") from exc
        if kind in (b"l", b"t"):
            return self._sequence(tuple if kind == b"t" else list)
        if kind == b"d":
            return self._mapping()
        if kind == b"z":
            return self._moment()
        raise CodecError(f"DEncode has no type letter {kind!r}")

    def _float(self) -> float:
        """``f`` + mantissa + ``e``, or ``f`` + mantissa + ``e`` + exponent + ``e``.

        The first ``e`` ends the mantissa; if what follows can start an exponent
        (a digit or a sign) then an exponent follows and the value ends at the
        next ``e``. That is how ``f2e+20e`` and ``f1e-07e`` are both read.
        """
        mantissa = self.until(b"e")
        tail = b""
        sign = self.data[self.at : self.at + 1]
        if sign in (b"+", b"-") or sign.isdigit():
            tail = self.until(b"e")
        try:
            return float(mantissa) * (10 ** int(tail) if tail else 1)
        except ValueError as exc:
            raise CodecError(f"DEncode float is not a number: {mantissa!r}") from exc

    def _sequence(self, wanted: type) -> Any:
        """``l…e`` or ``t…e``."""
        found = []
        while True:
            if self.at < len(self.data) and self.data[self.at : self.at + 1] == b"e":
                self.at += 1
                return wanted(found)
            found.append(self.value())

    def _mapping(self) -> dict[Any, Any]:
        """``d<key><value>…e`` — with any hashable key, tuples included."""
        found: dict[Any, Any] = {}
        while True:
            if self.at < len(self.data) and self.data[self.at : self.at + 1] == b"e":
                self.at += 1
                return found
            key = self.value()
            item = self.value()
            if isinstance(key, list):
                # a list key is a tuple that was written by a peer which has one
                # sequence type: it can only be a key as a tuple (§4.5)
                key = tuple(key)
            try:
                found[key] = item
            except TypeError as exc:
                raise CodecError(f"DEncode dictionary has an unhashable key: {key!r}") from exc

    def _moment(self) -> Any:
        """``za…``, ``zd…`` or ``zt…``: a datetime, a date or a time."""
        which = self.byte()
        parts = self.value()
        if not isinstance(parts, tuple):
            raise CodecError(f"DEncode {which!r} is not followed by a tuple")
        numbers = [part for part in parts if isinstance(part, int) and not isinstance(part, bool)]
        if which == b"a":
            return _moment("dt", numbers)
        if which == b"d":
            return _moment("date", numbers)
        if which == b"t":
            return _moment("time", numbers)
        raise CodecError(f"DEncode has no date letter {which!r}")


def _dedecode(data: bytes) -> Any:
    """One DEncode value, and nothing after it (§4.7)."""
    reader = _Reader(data)
    value = reader.value()
    if reader.at != len(data):
        raise CodecError(f"DEncode has {len(data) - reader.at} bytes after the value")
    return value
