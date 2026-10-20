"""DISET over TLS against the in-process server, and the configuration snapshot's cache."""

from __future__ import annotations

import io
import os
import socket
import ssl
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from xrdcore.errors import ConnectionError as XrdConnectionError
from xrdcore.errors import TimeoutError as XrdTimeoutError
from xrdsec import tls

from xrddirac import config as _config
from xrddirac.cfg import parse
from xrddirac.config import DiracConfig, cache_dir, require
from xrddirac.diset import DisetClient, ServiceURL, Wire, _network, _reason, s_error, s_ok, unwrap
from xrddirac.errors import (
    AuthorizationError,
    ConfigError,
    NoSuchServiceError,
    ServiceError,
    WireError,
)
from xrddirac.profiles import Profile
from xrddirac.testing import FakeDisetServer, Refuse

SVC = "Test/Service"


@pytest.fixture
def server(pki: Any) -> Iterator[FakeDisetServer]:
    with FakeDisetServer(pki.server_context()) as made:
        yield made


def _client(pki: Any, *urls: str, **options: Any) -> DisetClient:
    return DisetClient(
        list(urls),
        tls=lambda: tls.client_context(cert=pki.proxy_path, ca_path=pki.ca_dir),
        vo="v",
        timeout=10,
        **options,
    )


def _dead_url() -> str:
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
    return f"dips://localhost:{port}/{SVC}"


def test_call_and_answers(server: FakeDisetServer, pki: Any) -> None:
    server.rpc(SVC, "echo", lambda *args: list(args))
    server.rpc(SVC, "whole", lambda: {"OK": True, "Value": 1, "Extra": 2})
    server.rpc(SVC, "refuse", lambda: (_ for _ in ()).throw(Refuse("nope", 7, Extra=3)))
    server.rpc(SVC, "denied", lambda: s_error("Unauthorized query"))
    client = _client(pki, server.url(SVC))
    assert client.call("echo", 1, "a", [2]) == [1, "a", [2]]
    assert client.request("whole")["Extra"] == 2
    with pytest.raises(ServiceError) as caught:
        client.call("refuse")
    assert caught.value.errno == 7 and str(caught.value) == "Test/Service.refuse: nope"
    assert client.request("refuse")["Extra"] == 3
    with pytest.raises(AuthorizationError):
        client.call("denied")
    with pytest.raises(ServiceError, match="Unknown method"):
        client.call("missing")
    call = server.calls[0]
    assert call.proposal[0] == [SVC, "None", "v"] and call.proposal[3].startswith("xrddirac/")
    assert repr(client) == "DisetClient('Test/Service', 1 URL(s))"


def test_dencode_server_and_keepalive(server: FakeDisetServer, pki: Any) -> None:
    server.encoding = "dencode"
    server.faults.keepalive = True
    server.rpc(SVC, "echo", lambda value: value)
    assert _client(pki, server.url(SVC), encoding="dencode").call("echo", {"a": 1}) == {"a": 1}


def test_failover(server: FakeDisetServer, pki: Any) -> None:
    server.rpc(SVC, "ping", lambda: "pong")
    client = _client(pki, _dead_url(), server.url(SVC))
    assert [client.call("ping") for _ in range(3)] == ["pong"] * 3
    server.faults.hang_up.add("ping")
    with pytest.raises(XrdConnectionError, match="No Test/Service server answered"):
        client.call("ping")


def test_untrusted_server(pki: Any, tmp_path: Path) -> None:
    from xrdsec.testing.pki import create_pki

    other = create_pki(tmp_path / "other", key_slots=(4, 5, 6))
    stranger = FakeDisetServer(other.server_context(require_client=False))
    with stranger, pytest.raises(XrdConnectionError, match="certificate not trusted"):
        _client(pki, stranger.url(SVC)).call("x")


def test_file_transfer(server: FakeDisetServer, pki: Any) -> None:
    data = os.urandom(2 * 1024 * 1024 + 5)
    server.files["/f"] = data
    client = _client(pki, server.url(SVC))
    sink = io.BytesIO()
    assert client.receive_file("/f", sink) == len(data) and sink.getvalue() == data
    with pytest.raises(WireError, match="larger than"):
        client.receive_file("/f", io.BytesIO(), limit=10)
    with pytest.raises(ServiceError, match="No file"):
        client.receive_file("/missing", io.BytesIO())
    server.on_upload = lambda file_id, token, got: f"stored {len(got)}"
    assert client.send_file(["n.tar.bz2", {}], io.BytesIO(data), len(data)) == f"stored {len(data)}"
    assert server.uploads["n.tar.bz2"] == data


class _Scripted:
    """A stream that plays back bytes and records what was sent."""

    def __init__(self, *frames: bytes) -> None:
        self.data = b"".join(frames)
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


def _frame(value: Any, prefix: bytes = b"") -> bytes:
    from xrddirac import codec

    body = codec.jencode(value)
    return prefix + str(len(body)).encode() + b":" + body


def test_wire_limits_and_pings() -> None:
    wire = Wire(_Scripted(_frame(s_ok({"id": "x", "kaping": True}), b"dka"), _frame(s_ok(1))))
    assert wire.receive() == s_ok(1)
    assert b'"kaping":false' in wire.stream.sent[0]  # type: ignore[attr-defined]  # the scripted stream
    assert Wire(
        _Scripted(_frame(s_ok({"kaping": False}), b"dka"), _frame(s_ok(2)))
    ).receive() == s_ok(2)
    with pytest.raises(WireError, match="bad message length"):
        Wire(_Scripted(b"x1:1")).receive()
    with pytest.raises(WireError, match="no message length"):
        Wire(_Scripted(b"9" * 40)).receive()
    with pytest.raises(WireError, match="over the"):
        Wire(_Scripted(b"99:")).receive(limit=10)
    with pytest.raises(XrdConnectionError, match="closed"):
        Wire(_Scripted(b"5:ab")).receive()


def test_unwrap_and_parse() -> None:
    assert unwrap(s_ok(3)) == 3
    with pytest.raises(WireError):
        unwrap([1])
    with pytest.raises(ServiceError, match="no message") as caught:
        unwrap({"OK": False, "Errno": "x"})
    assert caught.value.errno == 0
    with pytest.raises(AuthorizationError):
        unwrap({"OK": False, "Message": "m", "Errno": 1111})
    assert ServiceURL.parse("dips://h/A/B") == ServiceURL("h", 9135, "A/B")
    assert str(ServiceURL("h", 1, "A/B")) == "dips://h:1/A/B"
    for bad in ("https://h:1/A/B", "dips://h:1/A", "dips:///A/B"):
        with pytest.raises(ConfigError):
            ServiceURL.parse(bad)
    with pytest.raises(ConfigError, match="at least one"):
        DisetClient([], tls=lambda: None)  # type: ignore[arg-type, return-value]  # never called
    with pytest.raises(ConfigError, match="One client"):
        DisetClient(["dips://h:1/A/B", "dips://h:1/A/C"], tls=lambda: None)  # type: ignore[arg-type, return-value]  # never called


def test_reasons() -> None:
    assert _reason(TimeoutError()) == "timed out"
    assert _reason(OSError()) == "OSError"
    verify = ssl.SSLCertVerificationError()
    verify.verify_message = "bad"
    assert _reason(verify) == "certificate not trusted (bad)"
    assert isinstance(_network(TimeoutError(), "w"), XrdTimeoutError)
    assert isinstance(_network(OSError("x"), "w"), XrdConnectionError)
    same = XrdConnectionError("already")
    assert _network(same, "w") is same


def test_timeouts_and_hang_ups_during_a_call(server: FakeDisetServer, pki: Any) -> None:
    def stall() -> int:
        time.sleep(1)
        return 1

    server.rpc(SVC, "slow", stall)
    client = _client(pki, server.url(SVC))
    client.timeout = 0.2
    with pytest.raises(XrdTimeoutError, match="timed out"):
        client.call("slow")

    def opener(_client: DisetClient, _url: ServiceURL) -> _Scripted:
        return _Scripted(_frame(s_ok()))  # the proposal is accepted, then the line goes dead

    scripted = _client(pki, server.url(SVC), opener=opener)
    with pytest.raises(XrdConnectionError, match="mid-message"):
        scripted.call("x")
    with pytest.raises(XrdConnectionError, match="mid-message"):
        scripted.receive_file("/f", io.BytesIO())
    with pytest.raises(XrdConnectionError, match="mid-message"):
        scripted.send_file("f", io.BytesIO(b"x"), 1)


def test_bad_transfer_messages(pki: Any, server: FakeDisetServer) -> None:
    def play(*frames: bytes) -> DisetClient:
        return _client(pki, server.url(SVC), opener=lambda _c, _u: _Scripted(*frames))

    accepted = (_frame(s_ok()), _frame(s_ok()))
    with pytest.raises(WireError, match="not \\[flag, data\\]"):
        play(*accepted, _frame(s_ok(1))).receive_file("/f", io.BytesIO())
    with pytest.raises(WireError, match="MD5"):
        play(*accepted, _frame(s_ok([True, b"a"])), _frame(s_ok([False, "0" * 32]))).receive_file(
            "/f", io.BytesIO()
        )
    sink = io.BytesIO()
    text_chunk = _frame(s_ok([True, {"__dCls": "b64", "obj": "YQ=="}]))
    assert (
        play(*accepted, text_chunk, _frame(s_ok([False, ""])), _frame(s_ok())).receive_file(
            "/f", sink
        )
        == 1
    )
    with pytest.raises(WireError, match="neither bytes nor text"):
        play(*accepted, _frame(s_ok([True, 5]))).receive_file("/f", io.BytesIO())
    from xrddirac import codec
    from xrddirac.diset import _chunk_bytes

    assert _chunk_bytes("\udcff") == b"\xff"
    assert codec.decode(_frame(s_ok())[3:]) == s_ok()
    with pytest.raises(WireError, match="not S_OK"):
        play(_frame(s_ok()), _frame([1])).request("x")


def test_closing_a_stream_that_fails() -> None:
    from xrddirac.diset import _close

    class Bad:
        def close(self) -> None:
            raise OSError("no")

    _close(Bad())  # type: ignore[arg-type]  # only close() is used
    _close(object())  # type: ignore[arg-type]  # nothing to close


def test_delegation_requests_are_refused(server: FakeDisetServer, pki: Any) -> None:
    client = _client(pki, server.url(SVC))
    wire = Wire(_Scripted(_frame(s_ok({"delegate": "csr"}))))
    with pytest.raises(ServiceError, match="delegation"):
        client._propose(wire, client.urls[0], ("RPC", "x"))


# -- the configuration ---------------------------------------------------------------------------

CS = """DIRAC
{
  Configuration
  {
    Version = 2
  }
  Setups
  {
    Prod
    {
      WorkloadManagement = Production
    }
  }
}
Systems
{
  DataManagement
  {
    URLs
    {
      FileCatalog = dips://a:1/DataManagement/FileCatalog, https://a:2/DataManagement/FileCatalog
    }
    Services
    {
      FileCatalog
      {
        Port = 1
      }
    }
  }
  WorkloadManagement
  {
    Production
    {
      URLs
      {
        JobManager = dips://b:1/WorkloadManagement/JobManager
      }
    }
  }
}
Operations
{
  Defaults
  {
    X = defaults
    Y = defaults
    Z = defaults
  }
  Prod
  {
    Y = setup
  }
  vo
  {
    Defaults
    {
      Z = vo
    }
  }
}
Registry
{
  DefaultVO = dv
  Groups
  {
    g
    {
      VO = gv
    }
  }
}
DiracX
{
  URL = https://dx/
}
"""


def test_config_lookups() -> None:
    config = DiracConfig.from_text(CS, setup="Prod")
    assert config.version == "2" and config.diracx_url == "https://dx"
    assert config.service_urls("DataManagement/FileCatalog") == [
        "dips://a:1/DataManagement/FileCatalog"
    ]
    assert len(config.service_urls("DataManagement/FileCatalog", schemes=("dips", "https"))) == 2
    assert config.service_urls("WorkloadManagement/JobManager") == [
        "dips://b:1/WorkloadManagement/JobManager"
    ]
    assert config.service_option("DataManagement/FileCatalog", "Port") == "1"
    assert config.service_option("Nope/Thing", "Port") is None
    with pytest.raises(NoSuchServiceError):
        config.service_urls("Nope/Thing")
    assert config.operations("X", "vo") == "defaults"
    assert config.operations("Y", "vo") == "setup"
    assert config.operations("Z", "vo") == "vo" and config.operations("Missing") is None
    assert config.vo_for_group("g") == "gv" and config.vo_for_group("other") == "dv"
    assert config.values("Systems/DataManagement/URLs/FileCatalog")[1].startswith("https")
    plain = DiracConfig.from_text(CS)
    assert plain.instance("WorkloadManagement") == "Production" and plain.instance("Nope") is None
    assert DiracConfig.from_text("A = 1\n").instance("X") is None
    named = DiracConfig.from_text(CS, setup="Other")
    assert named.instance("WorkloadManagement") == "Production"
    gone = DiracConfig.from_text(
        CS.replace("    Production\n    {\n      URLs", "    Elsewhere\n    {\n      URLs")
    )
    assert gone.system_section("WorkloadManagement") is not None
    assert require("x", "y") == "x"
    with pytest.raises(ConfigError, match="y is not configured"):
        require("", "y")


class _CS:
    """A Configuration Service that serves ``CS`` and counts calls."""

    def __init__(self, text: str = CS) -> None:
        self.text = text
        self.calls: list[str] = []
        self.down = False

    def __call__(self, method: str, *args: Any) -> Any:
        self.calls.append(method)
        if self.down:
            raise XrdConnectionError("down")
        data = zlib.compress(self.text.encode())
        if method == "getCompressedData":
            return data
        return {"newestVersion": "3"} if args[0] >= "2" else {"data": data}


def test_config_cache(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    profile = Profile("p", ("dips://cs:1/Configuration/Server",), config_max_age=100)
    cs = _CS()
    now = [1e9]
    first = _config.load(profile, cs, directory=tmp_path, now=lambda: now[0])
    assert first.version == "2" and cs.calls == ["getCompressedData"]
    assert (tmp_path / "configuration.cfg").stat().st_mode & 0o777 == 0o600
    os.utime(tmp_path / "configuration.cfg", (now[0], now[0]))
    _config.load(profile, cs, directory=tmp_path, now=lambda: now[0] + 10)
    assert cs.calls == ["getCompressedData"]
    _config.load(profile, cs, directory=tmp_path, now=lambda: now[0] + 1000)
    assert cs.calls[-1] == "getCompressedDataIfNewer"
    cs.text = CS.replace("Version = 2", "Version = 1")
    (tmp_path / "configuration.version").write_text("1")
    assert _config.load(profile, cs, directory=tmp_path, refresh=True).version == "1"
    cs.down = True
    assert _config.load(profile, cs, directory=tmp_path, refresh=True).version == "1"
    assert "Using the cached DIRAC configuration" in caplog.text
    with pytest.raises(XrdConnectionError):
        _config.load(profile, cs, directory=tmp_path / "empty")


def test_config_fetch_errors(tmp_path: Path) -> None:
    profile = Profile("p", ("dips://cs:1/Configuration/Server",))
    with pytest.raises(WireError, match="not compressed bytes"):
        _config.load(profile, lambda *_: 3, directory=tmp_path)
    with pytest.raises(WireError, match="does not decompress"):
        _config.load(profile, lambda *_: b"nope", directory=tmp_path)
    assert (
        _config.load(
            profile,
            lambda *_: zlib.compress(CS.encode()).decode("latin-1").encode("latin-1"),
            directory=tmp_path,
        ).version
        == "2"
    )
    (tmp_path / "configuration.version").write_text("1")
    with pytest.raises(WireError, match="other than a dict"):
        _config.load(profile, lambda *_: [], directory=tmp_path, refresh=True)
    assert _config._inflate(zlib.compress(b"A = 1").decode("utf-8", "surrogateescape")) == "A = 1"


def test_cache_write_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    profile = Profile("p", ("dips://cs:1/Configuration/Server",))
    assert _config.load(profile, _CS(), directory=blocker / "sub").version == "2"
    assert "Cannot keep the DIRAC configuration" in caplog.text
    assert cache_dir("p", {"XDG_CACHE_HOME": "/c"}) == Path("/c/xrddirac/p")
    assert parse(CS).get("DiracX/URL") == "https://dx/"
