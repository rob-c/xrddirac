"""DIRAC's configuration text (the ``.cfg`` format), read into a tree.

The Configuration Service hands out its whole tree as one zlib-compressed
``.cfg`` document; a site's ``dirac.cfg`` is the same format::

    DIRAC
    {
      Configuration
      {
        Servers = dips://cs1.example.org:9135/Configuration/Server
        Servers += dips://cs2.example.org:9135/Configuration/Server
      }
    }

A line is a section name (its ``{`` on the same line or the next), a ``}``,
an option ``name = value``, or ``name += value``, which appends to the
option as ``,value`` - DIRAC's lists are comma-separated text. ``#`` starts
a comment line. This reader is a small, total, linear one: an unbalanced
brace or a ``{`` with no name is a :class:`~xrddirac.errors.ConfigError`
with the line number, never a silently half-read tree.

What must not break: :meth:`Section.get` answers ``None`` for anything
missing, so a lookup in a tree some other DIRAC version laid out differently
degrades to "not configured" rather than raising.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from .errors import ConfigError

__all__ = ["Section", "parse", "split_list"]


def split_list(value: str | None) -> list[str]:
    """A DIRAC list option's items: comma-separated, trimmed, blanks dropped."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Section:
    """One section: its options (text) and its subsections, in file order."""

    options: dict[str, str] = field(default_factory=dict)
    sections: dict[str, Section] = field(default_factory=dict)

    def section(self, path: str) -> Section | None:
        """The subsection at ``path`` (``Resources/StorageElements``), or ``None``."""
        node: Section | None = self
        for part in _parts(path):
            node = node.sections.get(part) if node is not None else None
        return node

    def get(self, path: str, default: str | None = None) -> str | None:
        """The option at ``path`` (``DIRAC/Configuration/Servers``), or ``default``."""
        parent, _, name = path.strip("/").rpartition("/")
        node = self.section(parent) if parent else self
        if node is None or name not in node.options:
            return default
        return node.options[name]

    def values(self, path: str) -> list[str]:
        """The option at ``path`` as a list (empty when it is not there)."""
        return split_list(self.get(path))

    def names(self, path: str = "") -> list[str]:
        """The subsection names under ``path``."""
        node = self.section(path) if path else self
        return list(node.sections) if node is not None else []

    def walk(self, prefix: str = "") -> Iterator[tuple[str, str]]:
        """Every ``(path, value)`` below this section, depth first, in file order."""
        for name, value in self.options.items():
            yield f"{prefix}/{name}", value
        for name, child in self.sections.items():
            yield from child.walk(f"{prefix}/{name}")

    def merged(self, other: Section) -> Section:
        """A copy of this section with ``other``'s options and subsections laid over it.

        What DIRAC's ``BaseSE`` inheritance needs: the child's option wins, a
        subsection of the same name is merged rather than replaced.
        """
        result = Section(dict(self.options), dict(self.sections))
        result.options.update(other.options)
        for name, child in other.sections.items():
            base = result.sections.get(name)
            result.sections[name] = base.merged(child) if base is not None else child
        return result


def _parts(path: str) -> list[str]:
    """``A/B/C`` as its non-empty parts."""
    return [part for part in path.split("/") if part]


def parse(text: str) -> Section:
    """``text`` as a tree; :class:`ConfigError` for unbalanced braces or a nameless section."""
    stack = [Section()]
    pending: str | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            pending = _line(stack, line, pending, number)
    if len(stack) != 1:
        raise ConfigError(f"{len(stack) - 1} section(s) left open at the end")
    return stack[0]


def _line(stack: list[Section], line: str, pending: str | None, number: int) -> str | None:
    """Apply one significant line to the open sections; the section name it leaves pending."""
    if line == "{" or line.endswith("{"):
        name = line[:-1].strip() or pending
        if not name:
            raise ConfigError(f"line {number}: '{{' with no section name")
        stack.append(stack[-1].sections.setdefault(name, Section()))
        return None
    if line == "}":
        if len(stack) == 1:
            raise ConfigError(f"line {number}: '}}' closes no section")
        stack.pop()
        return None
    if "=" in line:
        _option(stack[-1], line)
        return None
    return line


def _option(section: Section, line: str) -> None:
    """``name = value`` sets, ``name += value`` appends ``,value``."""
    head, _, value = line.partition("=")
    value = value.strip()
    if head.endswith("+"):
        name = head[:-1].strip()
        before = section.options.get(name)
        section.options[name] = f"{before},{value}" if before else value
    else:
        section.options[head.strip()] = value
