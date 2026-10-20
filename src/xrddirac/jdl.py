"""JDL, DIRAC's job description language (a ClassAd subset): read, change, write.

A DIRAC job is described as::

    [
      JobName = "hello";
      Executable = "/bin/sh";
      Arguments = "-c 'echo hello'";
      StdOutput = "std.out";
      StdError = "std.err";
      InputSandbox = {"run.sh", "LFN:/lhcb/user/r/rcurrie/options.py"};
      OutputSandbox = {"std.out", "std.err"};
      InputData = {"LFN:/lhcb/LHCb/Collision24/.../00278103_00230015_1.b2cc.dst"};
      CPUTime = 86400;
      Site = {"LCG.CERN.cern", "LCG.Glasgow.uk"};
    ]

Values are strings (``"..."``, with ``\\"`` inside), lists of strings
(``{...}``), integers, floats and ``true``/``false``. :class:`JDL` keeps the
attributes in order with their case, looks them up without it (DIRAC is
case-insensitive), and writes them back in the same layout; anything else
in a description is a :class:`~xrddirac.errors.ConfigError` naming where.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import TypeAlias

from .errors import ConfigError

__all__ = ["JDL", "Value", "lfn_list", "quote", "shell_join"]

#: A JDL attribute's value.
Value: TypeAlias = str | int | float | bool | list[str]

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?")


def quote(text: str) -> str:
    """``text`` as a JDL string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class JDL:
    """An ordered, case-insensitive set of JDL attributes; see the module docstring."""

    def __init__(self, attributes: Mapping[str, Value] | None = None) -> None:
        self._items: dict[str, tuple[str, Value]] = {}
        for name, value in (attributes or {}).items():
            self[name] = value

    # -- mapping -----------------------------------------------------------------------------

    def __setitem__(self, name: str, value: Value) -> None:
        """Set ``name`` (keeping its first spelling and position)."""
        if not _NAME.fullmatch(name):
            raise ConfigError(f"Not a JDL attribute name: {name!r}")
        if isinstance(value, tuple):
            value = list(value)
        spelling = self._items.get(name.lower(), (name, value))[0]
        self._items[name.lower()] = (spelling, value)

    def __getitem__(self, name: str) -> Value:
        """The value of ``name``, any case; ``KeyError`` when absent."""
        return self._items[name.lower()][1]

    def __contains__(self, name: object) -> bool:
        """Whether ``name`` is set, any case."""
        return isinstance(name, str) and name.lower() in self._items

    def __delitem__(self, name: str) -> None:
        """Remove ``name``."""
        del self._items[name.lower()]

    def __iter__(self) -> Iterator[str]:
        """The attribute names, as first spelt, in order."""
        return iter(spelling for spelling, _ in self._items.values())

    def __len__(self) -> int:
        """How many attributes."""
        return len(self._items)

    def get(self, name: str, default: Value | None = None) -> Value | None:
        """The value of ``name``, or ``default``."""
        found = self._items.get(name.lower())
        return found[1] if found is not None else default

    def items(self) -> list[tuple[str, Value]]:
        """``(name, value)`` pairs, in order."""
        return list(self._items.values())

    def strings(self, name: str) -> list[str]:
        """``name`` as a list of strings (a single string becomes a one-item list)."""
        value = self.get(name)
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        return [str(value)]

    def append(self, name: str, *items: str) -> None:
        """Add ``items`` to the list attribute ``name`` (made if missing), skipping repeats."""
        current = self.strings(name)
        current += [item for item in items if item not in current]
        self[name] = current

    # -- text --------------------------------------------------------------------------------

    def __str__(self) -> str:
        """The description, one attribute per line, as DIRAC writes it."""
        lines = [f"    {name} = {_write(value)};" for name, value in self._items.values()]
        return "[\n" + "\n".join(lines) + "\n]"

    def __repr__(self) -> str:
        """The attributes."""
        return f"JDL({dict(self.items())!r})"

    @classmethod
    def parse(cls, text: str) -> JDL:
        """A description from its text (with or without the outer brackets)."""
        return cls(dict(_Parser(text).attributes()))


def _write(value: Value) -> str:
    """One value as JDL text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return "{" + ", ".join(quote(str(item)) for item in value) + "}"
    return quote(value)


class _Parser:
    """A small recursive-descent reader for the subset in the module docstring."""

    def __init__(self, text: str) -> None:
        self.text = _strip_comments(text)
        self.i = 0

    def error(self, what: str) -> ConfigError:
        """``what`` went wrong here, with the line number."""
        line = self.text.count("\n", 0, self.i) + 1
        return ConfigError(f"JDL line {line}: {what}")

    def skip(self) -> None:
        """Past whitespace."""
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def peek(self) -> str:
        """The next significant character ("" at the end)."""
        self.skip()
        return self.text[self.i : self.i + 1]

    def expect(self, char: str) -> None:
        """Consume ``char`` or fail."""
        if self.peek() != char:
            raise self.error(f"expected {char!r}, found {self.peek() or 'the end'!r}")
        self.i += 1

    def attributes(self) -> list[tuple[str, Value]]:
        """Every ``name = value;`` in the description."""
        bracketed = self.peek() == "["
        if bracketed:
            self.i += 1
        found: list[tuple[str, Value]] = []
        while self.peek() not in ("", "]"):
            name = _NAME.match(self.text, self.i)
            if name is None:
                raise self.error(f"expected an attribute name, found {self.peek()!r}")
            self.i = name.end()
            self.expect("=")
            found.append((name.group(), self.value()))
            if self.peek() == ";":
                self.i += 1
        if bracketed:
            self.expect("]")
        if self.peek():
            raise self.error(f"unexpected {self.peek()!r} after the description")
        return found

    def value(self) -> Value:
        """One value."""
        char = self.peek()
        if char == '"':
            return self.string()
        if char == "{":
            return self.listing()
        word = re.compile(r"[A-Za-z]+").match(self.text, self.i)
        if word is not None and word.group().lower() in ("true", "false"):
            self.i = word.end()
            return word.group().lower() == "true"
        number = _NUMBER.match(self.text, self.i)
        if number is None:
            raise self.error(f"expected a value, found {char or 'the end'!r}")
        self.i = number.end()
        text = number.group()
        return int(text) if number.group(1) is None and number.group(2) is None else float(text)

    def string(self) -> str:
        """``"..."`` with backslash escapes."""
        self.i += 1
        out: list[str] = []
        while self.i < len(self.text):
            char = self.text[self.i]
            if char == "\\" and self.i + 1 < len(self.text):
                out.append(self.text[self.i + 1])
                self.i += 2
            elif char == '"':
                self.i += 1
                return "".join(out)
            else:
                out.append(char)
                self.i += 1
        raise self.error("unterminated string")

    def listing(self) -> list[str]:
        """``{"a", "b"}`` (numbers in a list are kept as their text)."""
        self.i += 1
        items: list[str] = []
        while self.peek() != "}":
            item = self.value()
            if isinstance(item, list):
                raise self.error("a list inside a list")
            items.append(item if isinstance(item, str) else _write(item))
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "}":
                raise self.error("expected ',' or '}' in a list")
        self.i += 1
        return items


def _strip_comments(text: str) -> str:
    """The text without ``#`` and ``//`` comment lines (DIRAC strips ``#`` lines too)."""
    kept = [line for line in text.splitlines() if not line.lstrip().startswith(("#", "//"))]
    return "\n".join(kept)


def lfn_list(lfns: Sequence[str]) -> list[str]:
    """LFNs as JDL wants them in ``InputData``/``InputSandbox``: each with its ``LFN:`` prefix."""
    return [lfn if lfn[:4].upper() == "LFN:" else f"LFN:{lfn}" for lfn in lfns]


def shell_join(arguments: Sequence[str]) -> str:
    """Arguments as one ``Arguments`` string, which DIRAC's job wrapper splits as a shell does."""
    return " ".join(_shell_quote(arg) for arg in arguments)


def _shell_quote(arg: str) -> str:
    """One argument, single-quoted unless it is plainly safe."""
    if arg and all(c.isalnum() or c in "-_./=:,+@%" for c in arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"
