"""The pure parts: the codec, the CFG reader, JDL, sandboxes, errors, profiles."""

from __future__ import annotations

import datetime as dt
import io
import pickle
import sys
import tarfile
import types
from pathlib import Path
from typing import Any

import pytest

from xrddirac import codec, sandbox
from xrddirac.cfg import Section, parse, split_list
from xrddirac.errors import AuthorizationError, ConfigError, DiracError, ServiceError, WireError
from xrddirac.jdl import JDL, lfn_list, quote, shell_join
from xrddirac.profiles import BUILTIN, config_path, load_profile, profile_names

# -- codec -------------------------------------------------------------------------------------

VALUE: dict[str, Any] = {
    "OK": True,
    "Value": [1, -3, 2.5, 2e20, 1e-7, None, True, False, "x", "ünï", ("a", "b"), {"k": [1]}],
    "When": dt.datetime(2026, 10, 20, 8, 45, 4),
    "Day": dt.date(2026, 1, 2),
}


@pytest.mark.parametrize("encoding", ["dencode", "json"])
def test_round_trip(encoding: codec.Encoding) -> None:
    decoded = codec.decode(codec.encode(VALUE, encoding))
    assert decoded["When"] == VALUE["When"] and decoded["Day"] == VALUE["Day"]
    assert decoded["Value"][:10] == VALUE["Value"][:10]
    assert codec.sniff(codec.encode(VALUE, encoding)) == encoding


def test_dencode_specifics() -> None:
    assert codec.dencode((1,)) == b"ti1ee"
    assert codec.decode(b"I12e") == 12
    assert codec.decode(b"u3:abc") == "abc"
    assert codec.decode(b"f2e+20e") == 2e20
    assert codec.decode(b"ztti1ei2ei3ei4ene") == dt.time(1, 2, 3, 4)
    assert codec.decode(b"zdti2026ei1ei2ee") == dt.date(2026, 1, 2)
    assert codec.decode(b"dli1eei2ee") == {(1,): 2}
    assert codec.dencode(b"\xff") == b"s1:\xff"
    assert codec.decode(codec.dencode(dt.time(1, 2, 3))) == dt.time(1, 2, 3)


def test_json_specifics() -> None:
    assert codec.jencode(b"\x00") == b'{"__dCls":"b64","obj":"AA=="}'
    assert codec.decode(b'{"__dCls":"Foo","__dMod":"os","x":1}') == {
        "__dCls": "Foo",
        "__dMod": "os",
        "x": 1,
    }
    assert codec.decode(b'{"__dCls":"dt"}') == {"__dCls": "dt"}
    assert codec.decode(b'{"__dCls":"Foo","obj":1}') == {"__dCls": "Foo", "obj": 1}
    assert codec.decode(b"null") is None
    with pytest.raises(WireError, match="bad 'dt'"):
        codec.decode(b'{"__dCls":"dt","obj":"never"}')
    with pytest.raises(TypeError):
        codec.jencode(object())


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"x",
        b"s99:ab",
        b"ds2:OK",
        b"i1ei2e",
        b"l" * 300,
        b"zxi1e",
        b"zas1:xe",
        b"zati1ee",
        b"i12",
        b"iabce",
        b"zxti1ee",
    ],
)
def test_malformed_is_a_wire_error(data: bytes) -> None:
    with pytest.raises(WireError):
        codec.decode(data)


def test_dencode_refuses_other_types() -> None:
    with pytest.raises(TypeError):
        codec.dencode({1, 2})


# -- cfg ---------------------------------------------------------------------------------------

CFG = """
# a comment
DIRAC
{
  Setup = Test
  Servers = a
  Servers += b
  Inner {
    Key = v = w
  }
  Named
  {
  }
}
Top = 1
"""


def test_cfg() -> None:
    root = parse(CFG)
    assert root.values("DIRAC/Servers") == ["a", "b"]
    assert root.get("DIRAC/Inner/Key") == "v = w"
    assert root.get("Top") == "1"
    assert root.get("Missing/Thing", "d") == "d"
    assert root.names("DIRAC") == ["Inner", "Named"]
    assert root.names("Nope") == [] and root.names() == ["DIRAC"]
    assert root.section("DIRAC/Nope/Deeper") is None
    assert dict(root.walk())["/DIRAC/Inner/Key"] == "v = w"
    assert split_list(None) == [] and split_list(" a, ,b ") == ["a", "b"]


def test_cfg_merged() -> None:
    base = parse("P\n{\n  Host = h\n  Path = /a\n}\nX = 1\nY = 2\n")
    child = parse("P\n{\n  Path = /b\n}\nQ\n{\n}\nY = 3\n")
    merged = base.merged(child)
    assert merged.get("P/Host") == "h" and merged.get("P/Path") == "/b"
    assert merged.get("Y") == "3" and merged.section("Q") == Section()


@pytest.mark.parametrize(
    ("text", "words"), [("}\n", "closes no"), ("A\n{\n", "left open"), ("{\n", "no section")]
)
def test_cfg_errors(text: str, words: str) -> None:
    with pytest.raises(ConfigError, match=words):
        parse(text)


# -- JDL ---------------------------------------------------------------------------------------


def test_jdl_round_trip() -> None:
    text = (
        '# c\n// c\n[\n  JobName = "a \\"b\\"";\n'
        '  N = 3; F = 1.5; E = 2e3; T = TRUE; B = false\n  L = {"x", 1, 2.5}\n]'
    )
    jdl = JDL.parse(text)
    assert jdl["jobname"] == 'a "b"' and jdl["N"] == 3 and jdl["F"] == 1.5 and jdl["E"] == 2000.0
    assert jdl["T"] is True and jdl["B"] is False and jdl["L"] == ["x", "1", "2.5"]
    assert JDL.parse(str(jdl)).items() == jdl.items()
    assert "n" in jdl and 3 not in jdl and len(jdl) == 7 and next(iter(jdl)) == "JobName"
    jdl["JOBNAME"] = "c"
    assert next(iter(jdl)) == "JobName"
    del jdl["n"]
    assert jdl.get("N") is None and jdl.get("N", 1) == 1
    assert jdl.strings("Missing") == [] and jdl.strings("F") == ["1.5"]
    jdl.append("L", "x", "y")
    assert jdl["L"] == ["x", "1", "2.5", "y"]
    jdl["Tup"] = ("a",)  # type: ignore[assignment]  # a tuple is accepted and kept as a list
    assert jdl["Tup"] == ["a"] and "Tup" in repr(jdl)
    assert JDL.parse('A = "x"').items() == [("A", "x")]


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("[ = 1 ]", "attribute name"),
        ("[A 1]", "expected '='"),
        ('[A = "x', "unterminated"),
        ("[A = {{}}]", "list inside"),
        ("[A = {1 2}]", "',' or '}'"),
        ("[A = @]", "expected a value"),
        ("[A = 1] x", "unexpected"),
        ("[A = 1", "expected ']'"),
    ],
)
def test_jdl_errors(text: str, words: str) -> None:
    with pytest.raises(ConfigError, match=words):
        JDL.parse(text)


def test_jdl_helpers() -> None:
    with pytest.raises(ConfigError):
        JDL({"bad name": 1})
    assert quote('a"\\') == '"a\\"\\\\"'
    assert lfn_list(["/a", "LFN:/b"]) == ["LFN:/a", "LFN:/b"]
    assert shell_join(["-c", "echo 'x'", "a=b", ""]) == "-c 'echo '\"'\"'x'\"'\"'' a=b ''"


# -- sandboxes ---------------------------------------------------------------------------------


def test_pack_and_unpack(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    packed = sandbox.pack([tmp_path / "dir", tmp_path / "b.txt"], extra={"data.py": b"x = 1\n"})
    assert (
        packed.size == len(packed.data) and packed.format == "tar.bz2" and len(packed.sha256) == 64
    )
    files = sandbox.unpack(packed.data, tmp_path / "out")
    assert files == ["b.txt", "data.py", "dir/a.txt"]
    with pytest.raises(FileNotFoundError, match="nope"):
        sandbox.pack([tmp_path / "nope"])


def test_unpack_refuses_escapes(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(DiracError, match="outside the destination"):
        sandbox.unpack(buffer.getvalue(), tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()
    with pytest.raises(DiracError, match="not a readable tar"):
        sandbox.unpack(b"not a tar file at all", tmp_path / "out")


def _zstd_payload() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("z.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"z"))
    return buffer.getvalue()


def test_unpack_zstd_with_either_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _zstd_payload()
    stdlib = types.ModuleType("compression.zstd")
    stdlib.decompress = lambda data: raw  # type: ignore[attr-defined]  # a stand-in module
    monkeypatch.setattr(sandbox, "_zstd_module", lambda: stdlib)
    assert sandbox.unpack(b"\x28\xb5\x2f\xfd" + b"x", tmp_path / "a") == ["z.txt"]

    class Reader(io.BytesIO):
        pass

    third = types.ModuleType("zstandard")
    third.ZstdDecompressor = lambda: types.SimpleNamespace(stream_reader=lambda _s: Reader(raw))  # type: ignore[attr-defined]  # a stand-in module
    monkeypatch.setattr(sandbox, "_zstd_module", lambda: third)
    assert sandbox.unpack(b"\x28\xb5\x2f\xfd" + b"x", tmp_path / "b") == ["z.txt"]


def test_zstd_module_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("zstandard")
    monkeypatch.setitem(sys.modules, "compression.zstd", None)
    monkeypatch.setitem(sys.modules, "zstandard", fake)
    assert sandbox._zstd_module() is fake
    monkeypatch.setitem(sys.modules, "zstandard", None)
    with pytest.raises(ConfigError, match="zstandard"):
        sandbox._zstd_module()


# -- errors ------------------------------------------------------------------------------------


def test_errors_pickle() -> None:
    error = AuthorizationError("no", errno=1111, service="S/C", method="m")
    again = pickle.loads(pickle.dumps(error))
    assert type(again) is AuthorizationError and again.errno == 1111 and str(again) == "S/C.m: no"
    assert str(ServiceError("plain")) == "plain"


# -- profiles ----------------------------------------------------------------------------------


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("XRDDIRAC_CONFIG", str(path))
    return path


def test_builtin_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert load_profile().name == "lhcb"
    assert load_profile("gridpp").setup == "GridPP"
    monkeypatch.setenv("XRDDIRAC_PROFILE", "gridpp")
    assert load_profile().name == "gridpp"
    assert profile_names() == ["gridpp", "lhcb"]
    assert config_path({"XDG_CONFIG_HOME": "/x"}) == Path("/x/xrddirac/config.toml")
    assert BUILTIN["lhcb"].servers[-1].startswith("dips://lhcb-conf-dirac")
    with pytest.raises(ConfigError, match="No DIRAC profile"):
        load_profile("nope")


def test_configured_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _config(
        tmp_path,
        monkeypatch,
        'default = "mine"\n[profiles.mine]\nservers = "dips://h:1/Configuration/Server"\n'
        'vo = "v"\ncolour = "x"\n'
        '[profiles.gridpp]\nvo = "lz"\n',
    )
    mine = load_profile()
    assert mine.servers == ("dips://h:1/Configuration/Server",) and mine.extra == {"colour": "x"}
    assert load_profile("gridpp").vo == "lz" and load_profile("gridpp").setup == "GridPP"
    assert profile_names() == ["gridpp", "lhcb", "mine"]


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("[profiles.x]\nvo = 1\n", "needs servers"),
        ("[profiles.x]\nservers = [1]\n", "list of URLs"),
        ("[profiles.x]\nservers = ['dips://h:1/C/S']\nencoding = 'xml'\n", "encoding"),
        ("profiles = 1\n", "must be tables"),
        ("[profiles]\nx = 1\n", "must be a table"),
        ("[profiles.x\n", "config.toml"),
    ],
)
def test_bad_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, words: str
) -> None:
    _config(tmp_path, monkeypatch, text)
    with pytest.raises(ConfigError, match=words):
        load_profile("x")
