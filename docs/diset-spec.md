# DISET: a clean-room specification

**Status:** draft for re-derivation. **Version:** 0.1 (2026-11-11).

This document specifies the part of DIRAC's DISET protocol that xrddirac needs:
the TLS transport, message framing, the two encodings, the connection
handshake, one RPC, keep-alives, error results, and the limits that keep a
hostile peer from exhausting a client. It exists so that the code under
`src/xrddirac/` can be *derived from a written specification* rather than from
DIRAC's GPL-3.0-only source; see [`CLEANROOM.md`](CLEANROOM.md) for why that
matters and how it is enforced.

The words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119. Each
requirement is numbered (`§n.m`) so that code and tests can cite it, and each
fact carries where it came from:

* **[impl]** — behaviour documented by xrddirac 0.5's own implementation
  (`src/xrddirac/diset.py`, `codec.py`, `testing/diset.py`), which is this
  project's own LGPL code and therefore usable as a source of facts;
* **[doc]** — public documentation of DIRAC;
* **[capture]** — verified against a recorded conversation with a real server.
  Nothing in this document is marked **[capture]** yet: that is the one gap, and
  §11 says exactly what to record and which requirements it settles.

A requirement whose only source is **[impl]** is still a requirement — the
implementation has been used against real services — but a re-derivation must
confirm it (§11) before it is relied on.

---

## 1. Model and terminology

* **client** — the process that opens one TLS connection and asks a **service**
  to perform one **method**.
* **server** — a DIRAC service host. It identifies the client by the TLS
  handshake alone: the client presents an X.509 **proxy**, and the server reads
  the user, VO and group from that certificate.
* **service** — a `System/Component` name, e.g. `WorkloadManagement/JobManager`,
  carried in the connection URL: `dips://host:port/System/Component` **[impl]**.
* **method** — an exported function of that service.
* **message** — one encoded value (§4) in one **frame** (§3).
* **result** — the distinguished object a peer answers with (§4.4).

§1.1 There is no session token, cookie or nonce. Identity is the handshake and
everything after it is a request/response exchange on that connection **[impl]**.

## 2. Transport

§2.1 TLS 1.2 or later over TCP. The client MUST verify the server's certificate
and hostname and MUST present its proxy as its client certificate **[impl]**.
In this stack that is `xrdsec.tls.client_context`: the CA directory, CRL policy
and trust decisions are the ones every other connection makes, and a proxy's key
is handed to OpenSSL in memory rather than written out.

§2.2 The client certificate MUST be the proxy chain, not a bare end-entity
certificate: the server resolves user, VO and group from the proxy's identity
and has nothing else to go on **[impl]**.

§2.3 The port comes from the connection URL and MUST NOT be assumed **[impl]**.
Deployments are commonly served on 8443, and a configuration lists every URL of
a service.

§2.4 One connection carries **one** call: the client connects, proposes, calls,
and the server answers and closes **[impl]**.

§2.5 No ALPN protocol is negotiated **[impl]**.

## 3. Framing

§3.1 A message is carried as:

```
frame   = length ":" payload
length  = 1*DIGIT          ; byte length of payload, ASCII decimal
payload = OCTET            ; exactly `length` bytes: one encoded value (§4)
```

§3.2 `length` counts **bytes** of the payload, never characters, and carries no
padding or spaces. A zero-length payload carries no value and MUST be treated
as a protocol error where a value is required **[impl]**.

§3.3 A receiver MUST read until it has a length and then exactly that many
bytes. It MUST NOT assume one frame per TCP segment, and MUST NOT discard bytes
that follow a complete frame: they belong to the next one **[impl]**.

§3.4 **Keep-alive.** Either peer may send the three ASCII bytes `dka` *before*
a length, to keep an idle connection alive. The receiver MUST recognise `dka`
where a length would start, discard it, and continue with the next length
**[impl]**.

§3.5 A keep-alive may also arrive as a *value* — an object carrying an `id` and
a truthy `kaping` — which the receiver MUST recognise and answer with a message
whose payload is `S_OK({"id": <same id>, "kaping": false})`, also prefixed by
`dka` **[impl]**.

§3.6 **Security:** a receiver MUST bound the frame it will read
(`MAX_MESSAGE`, 256 MiB in xrddirac) and MUST fail with a protocol error rather
than allocate without limit or block forever **[impl]**.

§3.7 **Security:** a peer that closes in the middle of a frame is a protocol
error, never an empty reply: an empty reply would be mistaken for "the method
returned nothing" and the caller would carry on with wrong data **[impl]**.

§3.8 Bytes that are neither `dka` nor a decimal length are a protocol error,
named as such: never a length of zero, and never a value to skip past **[impl]**.

## 4. Value encoding

§4.1 A payload is one value in one of two encodings **[impl]**:

* **JSON** — plain JSON (RFC 8259), UTF-8, plus three tagged objects for values
  JSON lacks: `{"__dCls": "dt" | "date" | "b64", "obj": …}` for a datetime, a
  date and base64 bytes.
* **DEncode** — DIRAC's own textual encoding, a bencode relative:
  `i42e`/`I42e` integers, `f1.5e` float, `b1`/`b0` booleans, `n` null,
  `s5:hello` bytes, `u5:hello` text, `l…e` list, `t…e` tuple, `d…e` dict,
  `za<tuple>` datetime, `zd<tuple>` date, `zt<tuple>` time **[impl]**.

§4.2 The client SHOULD send JSON; a server accepts either **[impl]**. A decoder
MUST accept both, because a DIRAC 8 server answers in DEncode **[impl]**.

§4.3 **Decoding MUST be total.** Malformed input — truncated, over-long,
unknown type letters, unbalanced containers — MUST raise a protocol error, not
propagate an exception from the decoder's internals and not return a
half-decoded value **[impl]**.

§4.4 **Results.** A peer answers with a result object:

```
success = { "OK": true,  "Value": <any> }              ; Value MAY be absent
failure = { "OK": false, "Message": <string>, "Errno": <integer> }
```

A real server's failure, recorded in
[`evidence/20261111T131637Z-python`](evidence/20261111T131637Z-python/transcript.md)
against LHCb DIRAC 12.0.23, also carries a `CallStack` — an array of the server's
own traceback lines — so a receiver MUST tolerate members it does not know and
MUST pass them on to a caller that asks for the whole answer **[capture]**.

A success MAY carry additional members beside `Value` — `JobManager.killJob`
reports `InvalidJobIDs`, `submitJob` a `requireProxyUpload` flag — and a
receiver MUST pass those on rather than discard them **[impl]**. A failure's
`Errno` is `0` when the server did not say; a receiver MUST NOT treat its
absence as a protocol error **[impl]**.

§4.5 **No class loading.** DIRAC's JSON may tag *any* serialisable class by
module and class name, and its decoder imports it. A clean-room decoder MUST
NOT import anything a peer names: such an object stays the plain object it
arrived as. This is a **security** requirement, not a compatibility note
**[impl]**.

§4.6 Numbers JSON cannot represent (NaN, ±Infinity) MUST NOT be sent, and a
receiver MUST reject them rather than pass them on **[impl]**.

§4.7 A payload MUST decode as exactly one value. Trailing data is a protocol
error.

## 5. Connection handshake

§5.1 After TLS the **client** sends exactly one message: the *proposal*, a
four-element array **[impl]**:

```
[ [service, setup, vo], [kind, method], extra_credentials, version ]
```

| element | meaning |
| --- | --- |
| `service` | the `System/Component` of the URL, e.g. `WorkloadManagement/JobManager` |
| `setup` | the deployment's setup name; the literal `"None"` for the default |
| `vo` | the VO to act for, or empty/absent when the call is not VO-scoped |
| `kind` | `"RPC"` for a method call; `"FileTransfer"` for a transfer (§9) |
| `method` | **the method's name**, for `kind` `"RPC"` |
| `extra_credentials` | an object of additional credentials; empty unless a mechanism needs one |
| `version` | the client's version string; the server logs it and does not gate on it |

The two inner arrays are arrays, not objects: the protocol is positional.

§5.2 The **server** answers with a result (§4.4) **[impl]**:

* success — the action is accepted; proceed to §6. The acceptance is a message
  of its own (`{"OK": true, "Value": null}` in the recording), and the answer to
  the call (§6.2) is a *second* message **[capture]**;
* failure — the service refused; the `Message` MUST be reported verbatim, since
  it is the only place a server explains itself;
* a success whose value is an object asking for **delegation** MUST be refused
  by a clean-room client: delegation is the gateway's feature, and a client
  that accepts credentials a server offers is a confused deputy (§8.3).

§5.3 A client MUST NOT send anything else before it has read that answer.

## 6. One RPC

§6.1 The client sends the arguments as one message: `S_OK([arg, …])` — a
success result whose value is the argument list, positional and possibly empty
**[impl]**. The method is *not* in this message: it was named in the proposal
(§5.1).

§6.2 The server answers with one result (§4.4) and closes the connection
**[impl]**. A success's `Value` is the method's value; a failure MUST be raised
as an error carrying the server's `Message` and `Errno` **[impl]**.

§6.3 A reply that is not a result object — a bare value, or a result whose
members are of the wrong type — MUST be a protocol error, not a value handed to
the caller **[impl]**.

## 7. Failover, timeouts, cancellation

§7.1 A service has several URLs. A call MUST start at a rotating position and
move to the next URL only when it could not *connect or propose* there. Once
arguments have been sent, a failure MUST be reported rather than retried: the
call may have taken effect **[impl]**.

§7.2 Every blocking operation — connect, TLS handshake, frame read, frame write
— MUST have a finite timeout, and a timeout MUST be distinguishable from a
refusal in the error the caller sees **[impl]**.

§7.3 The frame limit (§3.6) and a frame-count limit while waiting for a reply
(a peer that only sends keep-alives must not loop forever) MUST both be
enforced.

## 8. Security requirements

§8.1 **Authentication is the handshake.** No message carries an identity, and
the client MUST NOT send a credential over the protocol: TLS already has it.

§8.2 **Nothing but the call.** The client sends one proposal, one argument
list, and nothing else. A server cannot ask it for a key, a password or a file,
and an implementation MUST NOT grow a path that could be driven that way.

§8.3 **Refuse delegation** (§5.2).

§8.4 **Bound everything** (§3.6, §7.3).

§8.5 **Never log a message body**: it may carry a token, a JDL or a delegated
proxy **[impl]**.

§8.6 **No class loading** (§4.5).

## 9. File transfer (out of scope for the fallback)

`kind` `"FileTransfer"` streams chunks instead of arguments. xrddirac supports
it today **[impl]**, but it is explicitly out of scope for the clean-room
re-derivation: the fallback needs the token exchange
(`Framework/ProxyManager.exchangeProxyForToken`) and read-only catalogue
queries, and a transfer protocol multiplies the surface without being needed
by them. A re-derivation MUST refuse `FileTransfer` with a clear error rather
than implement it half-way.

## 10. Conformance

§10.1 **Offline tests**, one group per section, all of which MUST exist before
the code they test:

| tests | requirements |
| --- | --- |
| frame round trip, split reads, two frames in one read | §3.1–§3.3 |
| keep-alive skipped, keep-alive answered | §3.4, §3.5 |
| oversize, truncated, non-numeric and empty frames | §3.6–§3.8 |
| DEncode and JSON decode, malformed decode is total | §4.1–§4.3 |
| result parsing: extra members kept, absent `Errno`, absent `Value` | §4.4 |
| no class loading from a peer's tag | §4.5 |
| proposal message asserted as data | §5.1 |
| refusal reported verbatim; delegation refused | §5.2, §8.3 |
| call message asserted as data; reply unwrapped or raised | §6.1–§6.3 |
| failover only before arguments are sent | §7.1 |

§10.2 **Black-box acceptance**: one recorded conversation with the real server
the fallback is for, replayed at the framing layer — the capture's bytes fed to
the parser, and the client's own messages compared with the capture's. This is
what turns every **[impl]** fact above into a verified one.

## 11. What a capture must settle

Record one working exchange — a DIRAC client performing one RPC (the proxy
exchange or a catalogue query) against the server to be supported — as bytes
(`tcpdump -s0 -w diset.pcap port <port>`, or the deployment's own client
logging), then answer:

Recorded 2026-11-11 against **LHCb DIRAC 12.0.23** (`lbvobox300.cern.ch:9135`)
with `diset-capture hook`, two calls: `Configuration/Server.getVersion` (answered)
and `Framework/ProxyManager.exchangeProxyForToken` (refused, service not up).
Bundles: [`evidence/`](evidence/), transcripts with the sha256 of each stream.

| # | question | requirements it settles | settled? |
| --- | --- | --- | --- |
| C1 | the proposal's exact values: `setup`, `vo`, `kind`, `version` | §5.1 | **yes**: `["System/Component", "None", "unknown"]`, `["RPC", method]`, `""` for extra credentials, a version string the server only logs **[capture]** |
| C2 | the keep-alive: sent unprompted? interval? `dka` form only, or the `kaping` value too? | §3.4, §3.5 |
| C3 | the result members on success and on failure (`Value` absent? `Errno` present?) | §4.4 | **yes**: success `{"OK": true, "Value": null\|value}`, failure `{"OK": false, "Message", "Errno": 0, "CallStack": [...]}` **[capture]** |
| C4 | the encoding the server answers in (JSON or DEncode) | §4.1–§4.2 | **yes, for this server**: JSON both ways (**[capture]**); DEncode stays `[impl]` for DIRAC 8 |
| C5 | whether a connection ever carries more than one call | §2.4 | **partly**: one proposal, one call, two server messages in this recording; nothing shows reuse **[capture]** |
| C6 | the largest message a server sends, for `MAX_MESSAGE` | §3.6 |
| C7 | whether the server ever asks for delegation | §5.2 |

Nothing in §3–§6 may be *changed* on the strength of a single capture without
updating this document: the capture settles what the document asserts, and the
document stays the only thing the implementation is written from.
