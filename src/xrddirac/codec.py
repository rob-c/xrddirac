"""DISET's two encodings: DIRAC's ``DEncode`` and its JSON (``JEncode``), read safely.

Every DISET message is one value in one of two encodings, and a peer may use
either: DIRAC 9 clients send JSON by default (``DIRAC_USE_JSON_ENCODE``),
older servers - GridPP's DIRAC 8 - answer in DEncode, and a server reads
whichever arrives. :func:`decode` does what DIRAC's ``MixedEncode`` does -
DEncode first, JSON when that fails - and :func:`encode` writes either.

**DEncode** is a bencode relative: a one-letter type, then the value::

    i42e  I42e  f1.5e  b1  b0  n  s5:hello  u5:hello
    l...e (list)  t...e (tuple)  d<key><value>...e (dict)
    za<tuple> (datetime)  zd<tuple> (date)  zt<tuple> (time)

**JSON** is plain JSON plus three tagged objects, ``{"__dCls": "dt" | "date"
| "b64", "obj": ...}``. DIRAC also tags *any* ``JSerializable`` class by
module and name and imports it to decode; this reader never imports
anything a peer names - such an object stays the plain dict it arrived as.

What must not break: decoding is total. Any malformed input - truncated,
trailing bytes, an unknown type letter, nesting deep enough to exhaust the
stack - raises :class:`~xrddirac.errors.WireError`, never ``IndexError`` or
``RecursionError``, because the bytes come from the network.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
from collections.abc import Callable
from typing import Any, Literal

from .errors import WireError

__all__ = ["Encoding", "decode", "dencode", "encode", "jencode"]

#: The encodings :func:`encode` writes.
Encoding = Literal["json", "dencode"]

_DATETIME = "%Y-%m-%d %H:%M:%S"
_DATE = "%Y-%m-%d"

#: Nesting deeper than this is refused rather than recursed into.
MAX_DEPTH = 200


# -- DEncode -----------------------------------------------------------------------------


def dencode(value: Any) -> bytes:
    """``value`` in DEncode; ``TypeError`` for a type DEncode has no letter for."""
    out: list[bytes] = []
    _put(value, out)
    return b"".join(out)


def _put(value: Any, out: list[bytes]) -> None:
    """Append ``value``'s DEncode form to ``out``."""
    if value is None:
        out.append(b"n")
    elif isinstance(value, bool):
        out.append(b"b1" if value else b"b0")
    elif isinstance(value, int):
        out += (b"i", str(value).encode(), b"e")
    elif isinstance(value, float):
        out += (b"f", repr(value).encode(), b"e")
    elif isinstance(value, str | bytes):
        raw = value.encode("utf-8", "surrogateescape") if isinstance(value, str) else value
        out += (b"s", str(len(raw)).encode(), b":", raw)
    elif isinstance(value, _dt.datetime | _dt.date | _dt.time):
        _put_moment(value, out)
    else:
        _put_container(value, out)


def _put_moment(value: _dt.datetime | _dt.date | _dt.time, out: list[bytes]) -> None:
    """A datetime, date or time as DIRAC writes them: ``z`` + kind + a tuple of fields."""
    if isinstance(value, _dt.datetime):
        fields: tuple[Any, ...] = (
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            None,
        )
        out.append(b"za")
    elif isinstance(value, _dt.date):
        fields = (value.year, value.month, value.day)
        out.append(b"zd")
    else:
        fields = (value.hour, value.minute, value.second, value.microsecond, None)
        out.append(b"zt")
    _put(fields, out)


def _put_container(value: Any, out: list[bytes]) -> None:
    """Lists, tuples and dicts; anything else has no DEncode form."""
    if isinstance(value, dict):
        out.append(b"d")
        for key, item in value.items():
            _put(key, out)
            _put(item, out)
    elif isinstance(value, list | tuple):
        out.append(b"t" if isinstance(value, tuple) else b"l")
        for item in value:
            _put(item, out)
    else:
        raise TypeError(f"DEncode cannot encode {type(value).__name__}")
    out.append(b"e")


class _Reader:
    """One DEncode document, read left to right; see :func:`decode`."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.depth = 0

    def value(self, i: int) -> tuple[Any, int]:
        """The value starting at ``i`` and the index after it."""
        kind = self.data[i : i + 1]
        reader = _READERS.get(kind)
        if reader is None:
            raise WireError(f"DEncode: unknown type {kind!r} at byte {i}")
        return reader(self, i)

    def _until(self, i: int, end: bytes) -> tuple[bytes, int]:
        """The bytes from ``i`` to the next ``end``, and the index after ``end``."""
        stop = self.data.find(end, i)
        if stop < 0:
            raise WireError(f"DEncode: unterminated value at byte {i}")
        return self.data[i:stop], stop + 1

    def integer(self, i: int) -> tuple[Any, int]:
        """``i<digits>e`` or ``I<digits>e``."""
        text, after = self._until(i + 1, b"e")
        return int(text), after

    def number(self, i: int) -> tuple[Any, int]:
        """``f<repr>e``; a repr with an exponent (``1e+20``) holds an ``e`` of its own."""
        text, after = self._until(i + 1, b"e")
        if self.data[after : after + 1] in (b"+", b"-"):
            exponent, after = self._until(after, b"e")
            return float(text) * 10 ** int(exponent), after
        return float(text), after

    def boolean(self, i: int) -> tuple[Any, int]:
        """``b0`` or ``b1``."""
        return self.data[i + 1 : i + 2] != b"0", i + 2

    def none(self, i: int) -> tuple[Any, int]:
        """``n``."""
        return None, i + 1

    def string(self, i: int) -> tuple[Any, int]:
        """``s<len>:<bytes>`` or ``u<len>:<bytes>``, as text (bytes kept with surrogateescape)."""
        length, start = self._until(i + 1, b":")
        end = start + int(length)
        if end > len(self.data):
            raise WireError(f"DEncode: string at byte {i} runs past the end")
        return self.data[start:end].decode("utf-8", "surrogateescape"), end

    def listing(self, i: int) -> tuple[Any, int]:
        """``l...e``."""
        return self._items(i + 1)

    def tupled(self, i: int) -> tuple[Any, int]:
        """``t...e``."""
        items, after = self._items(i + 1)
        return tuple(items), after

    def _items(self, i: int) -> tuple[list[Any], int]:
        """Values up to the closing ``e``."""
        self._enter()
        items: list[Any] = []
        while self.data[i : i + 1] != b"e":
            item, i = self.value(i)
            items.append(item)
        self.depth -= 1
        return items, i + 1

    def mapping(self, i: int) -> tuple[Any, int]:
        """``d<key><value>...e``."""
        self._enter()
        result: dict[Any, Any] = {}
        i += 1
        while self.data[i : i + 1] != b"e":
            key, i = self.value(i)
            result[_hashable(key)], i = self.value(i)
        self.depth -= 1
        return result, i + 1

    def moment(self, i: int) -> tuple[Any, int]:
        """``za``/``zd``/``zt`` and a tuple of fields."""
        kind = self.data[i + 1 : i + 2]
        fields, after = self.value(i + 2)
        if not isinstance(fields, tuple | list):
            raise WireError(f"DEncode: bad date fields at byte {i}")
        try:
            if kind == b"a":
                return _dt.datetime(*fields[:7]), after
            if kind == b"d":
                return _dt.date(*fields[:3]), after
            if kind == b"t":
                return _dt.time(*fields[:4]), after
        except (TypeError, ValueError) as exc:
            raise WireError(f"DEncode: bad date at byte {i}: {exc}") from exc
        raise WireError(f"DEncode: unknown date kind {kind!r} at byte {i}")

    def _enter(self) -> None:
        """Count one more level of nesting; refuse past :data:`MAX_DEPTH`."""
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise WireError(f"DEncode: nested deeper than {MAX_DEPTH}")


def _hashable(key: Any) -> Any:
    """A dict key as Python allows one: a list key (never sent by DIRAC) becomes a tuple."""
    return tuple(key) if isinstance(key, list) else key


_READERS: dict[bytes, Callable[[_Reader, int], tuple[Any, int]]] = {
    b"i": _Reader.integer,
    b"I": _Reader.integer,
    b"f": _Reader.number,
    b"b": _Reader.boolean,
    b"n": _Reader.none,
    b"s": _Reader.string,
    b"u": _Reader.string,
    b"l": _Reader.listing,
    b"t": _Reader.tupled,
    b"d": _Reader.mapping,
    b"z": _Reader.moment,
}


def _dedecode(data: bytes) -> Any:
    """``data`` as exactly one DEncode value; :class:`WireError` otherwise."""
    reader = _Reader(data)
    try:
        value, end = reader.value(0)
    except (ValueError, IndexError) as exc:
        raise WireError(f"DEncode: {exc}") from exc
    if end != len(data):
        raise WireError(f"DEncode: {len(data) - end} byte(s) after the value")
    return value


# -- JSON --------------------------------------------------------------------------------


def jencode(value: Any) -> bytes:
    """``value`` as DIRAC's JSON: tuples as lists, dates and bytes as tagged objects."""
    return json.dumps(value, default=_tag, separators=(",", ":")).encode()


def _tag(value: Any) -> Any:
    """``json.dumps``'s fallback: DIRAC's tagged forms."""
    if isinstance(value, _dt.datetime):
        return {"__dCls": "dt", "obj": value.strftime(_DATETIME)}
    if isinstance(value, _dt.date):
        return {"__dCls": "date", "obj": value.strftime(_DATE)}
    if isinstance(value, bytes | bytearray):
        return {"__dCls": "b64", "obj": base64.b64encode(value).decode()}
    raise TypeError(f"DIRAC's JSON cannot encode {type(value).__name__}")


def _untag(obj: dict[str, Any]) -> Any:
    """``json.loads``'s object hook: DIRAC's tagged forms back; any other class left a dict."""
    tag = obj.get("__dCls")
    if tag is None or "obj" not in obj:
        return obj
    raw = obj["obj"]
    try:
        if tag == "dt":
            return _dt.datetime.strptime(raw, _DATETIME)
        if tag == "date":
            return _dt.datetime.strptime(raw, _DATE).date()
        if tag == "b64":
            return base64.b64decode(raw, validate=True)
    except (TypeError, ValueError) as exc:
        raise WireError(f"JSON: bad {tag!r} value: {exc}") from exc
    return obj


def _jdecode(data: bytes) -> Any:
    """``data`` as DIRAC JSON; :class:`WireError` otherwise."""
    try:
        return json.loads(data, object_hook=_untag)
    except (ValueError, RecursionError) as exc:
        raise WireError(f"Neither DEncode nor JSON: {exc}") from exc


# -- both --------------------------------------------------------------------------------


def encode(value: Any, encoding: Encoding = "json") -> bytes:
    """``value`` in ``encoding`` (JSON unless asked for DEncode)."""
    return dencode(value) if encoding == "dencode" else jencode(value)


def decode(data: bytes) -> Any:
    """One value, in whichever encoding it came: DEncode first, then JSON (``MixedEncode``).

    JSON's ``null`` would read as DEncode's ``n`` plus three stray bytes, and
    DEncode refuses trailing bytes, so it falls through to JSON correctly.
    """
    if not data:
        raise WireError("Empty DISET message")
    try:
        return _dedecode(data)
    except WireError:
        return _jdecode(data)


def sniff(data: bytes) -> Encoding:
    """Which encoding ``data`` is in, for answering a peer in its own."""
    try:
        _dedecode(data)
    except WireError:
        return "json"
    return "dencode"
