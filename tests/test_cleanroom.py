"""The mechanical half of the clean room: no DIRAC, no dependency, a specification to cite.

Provenance of thought cannot be tested. What can be tested is that this package
imports nothing from DIRAC, that nothing declares DIRAC as a requirement, that
the import ban in ``pyproject.toml`` is still there, and that the specification
the implementation is supposed to be written from exists and is versioned with
the code.
"""

from __future__ import annotations

import ast
import pathlib
import tomllib
from collections.abc import Iterator

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "xrddirac"
BANNED = ("DIRAC", "diracx")


def _modules() -> list[pathlib.Path]:
    """Every module in the package."""
    return sorted(SOURCE.rglob("*.py"))


def _imported_names(tree: ast.AST) -> Iterator[str]:
    """Every module a file imports, absolute ones only: a relative import is this package."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield node.module or ""


def test_nothing_imports_dirac() -> None:
    """§CLEANROOM 2: DIRAC's source may not be reached, even at runtime."""
    for path in _modules():
        for name in _imported_names(ast.parse(path.read_text())):
            assert name.split(".")[0] not in BANNED, f"{path} imports {name}"


def test_no_dirac_requirement_is_declared() -> None:
    """§CLEANROOM 2: no dependency, in any extra, at any time."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = list(project.get("dependencies", []))
    declared += [
        one for extra in project.get("optional-dependencies", {}).values() for one in extra
    ]
    for requirement in declared:
        assert "dirac" not in requirement.lower(), requirement


def test_ruff_still_bans_the_import() -> None:
    """The ban is configuration, so configuration is what keeps it."""
    configured = tomllib.loads((ROOT / "pyproject.toml").read_text())
    banned = configured["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]
    for name in BANNED:
        assert name in banned, f"ruff no longer bans {name}"


def test_the_specification_is_versioned_with_the_code() -> None:
    """§CLEANROOM 3.3: every behaviour cites a requirement, so the document must be here."""
    spec = ROOT / "docs" / "diset-spec.md"
    assert spec.is_file(), "docs/diset-spec.md is the thing the implementation is written from"
    said = spec.read_text()
    assert "§3.1" in said and "§5.1" in said and "§11" in said
    assert (ROOT / "docs" / "CLEANROOM.md").is_file()


def test_the_transport_cites_the_specification() -> None:
    """The module that speaks the protocol points at the requirements it implements."""
    transport = (SOURCE / "diset.py").read_text()
    assert "diset-spec" in transport or "specification" in transport, (
        "src/xrddirac/diset.py must cite docs/diset-spec.md"
    )


#: The files that speak the protocol: each must be attested or listed as pending.
PROTOCOL = ("src/xrddirac/diset.py", "src/xrddirac/codec.py")


def test_every_protocol_file_is_accounted_for() -> None:
    """§CLEANROOM 4: a new protocol file cannot appear without an entry, and the
    pending list cannot grow silently."""
    said = (ROOT / "docs" / "ATTESTATIONS.md").read_text()
    for name in PROTOCOL:
        assert name in said, f"{name} is not in docs/ATTESTATIONS.md"
    pending = said.split("## Pending re-derivation", 1)[1]
    listed = {line.strip() for line in pending.splitlines() if line.strip().startswith("src/")}
    assert listed == set(PROTOCOL), f"pending list is {sorted(listed)}, expected {sorted(PROTOCOL)}"
