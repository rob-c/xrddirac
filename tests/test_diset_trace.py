"""The evidence reader: framing, redaction, and the hash that ties a transcript to bytes."""

from __future__ import annotations

import json

import pytest

from xrddirac import codec
from xrddirac.testing import diset_trace


def _frame(value: object, encoding: codec.Encoding = "json") -> bytes:
    """One framed message, built the way the specification says: length, colon, payload."""
    payload = codec.encode(value, encoding=encoding)
    return str(len(payload)).encode() + b":" + payload


def test_a_conversation_is_read_back_frame_by_frame() -> None:
    stream = _frame(
        {
            "OK": True,
            "Value": [
                [
                    ["Framework/ProxyManager", "None", "lhcb"],
                    ["RPC", "exchangeProxyForToken"],
                    {},
                    "xrddirac/0.5",
                ]
            ],
        }
    )
    stream += _frame({"OK": True, "Value": []})
    trace = diset_trace.read(stream)
    assert [one.kind for one in trace.messages] == ["value", "value"]
    assert trace.encodings == ("json",)
    assert trace.sha256 == diset_trace.digest(stream)
    assert trace.size == len(stream)


def test_a_keep_alive_is_recorded_where_a_length_would_be() -> None:
    trace = diset_trace.read(b"dka" + _frame({"OK": True}) + b"dka")
    assert [one.kind for one in trace.messages] == ["keepalive", "value", "keepalive"]
    assert trace.messages[0].length == 0


def test_dencode_is_read_as_dencode() -> None:
    trace = diset_trace.read(_frame({"OK": True, "Value": {"n": 1}}, encoding="dencode"))
    assert trace.encodings == ("dencode",)
    assert trace.messages[0].value == {"OK": True, "Value": {"n": 1}}


@pytest.mark.parametrize(
    ("stream", "problem"),
    [
        (b"notaframe", "no length"),
        (b"12:short", "truncated"),
        (b"999999999:", "over the limit"),
        (b"x:payload", "not a length"),
        (b"3:\x01\x02\x03", "neither"),
    ],
    ids=["no-colon", "truncated", "oversized", "bad-length", "undecodable"],
)
def test_a_frame_that_is_not_a_message_is_shown_as_such(stream: bytes, problem: str) -> None:
    trace = diset_trace.read(stream)
    assert any(one.kind == "undecodable" and problem in one.problem for one in trace.messages)


def test_a_credential_is_redacted_by_its_key_not_only_its_shape() -> None:
    """`access_token: "abc"` is a token: a short opaque value must not slip through."""
    trace = diset_trace.read(_frame({"OK": True, "Value": {"access_token": "abc", "n": 1}}))
    shown = json.dumps(trace.as_dict())
    assert "abc" not in shown
    assert "<redacted: 3 chars>" in shown
    assert '"n": 1' in shown  # everything that is not a credential is kept


def test_a_token_is_redacted_and_says_so() -> None:
    """Evidence must not carry a credential, and must not pretend nothing was there."""
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJyY3VycmllIn0.c2lnbmF0dXJl"
    trace = diset_trace.read(_frame({"OK": True, "Value": f"Bearer {jwt}"}))
    shown = json.dumps(trace.as_dict())
    assert jwt not in shown
    assert "redacted" in shown


@pytest.mark.parametrize(
    "secret",
    ["access_token=abc", "-----BEGIN PRIVATE KEY-----", "password=hunter2", "client_secret: s"],
    ids=["query", "pem", "password", "client-secret"],
)
def test_the_things_that_look_like_secrets_are_redacted(secret: str) -> None:
    assert str(diset_trace.redact(secret)).startswith("<redacted")


def test_a_long_body_is_cut_rather_than_committed() -> None:
    assert "…" in str(diset_trace.redact("x" * 5000))
    assert diset_trace.redact(b"1234567890") == "<10 bytes>"
    assert diset_trace.redact(7) == 7


def test_the_transcript_is_markdown_a_reviewer_can_read() -> None:
    trace = diset_trace.read(
        _frame({"OK": True, "Value": [[["S", "None", ""], ["RPC", "m"], {}, "v"]]})
    )
    said = diset_trace.as_markdown(trace)
    assert "| # | side | kind | length | value |" in said
    assert trace.sha256 in said
    assert "| 0 | client | value |" in said
