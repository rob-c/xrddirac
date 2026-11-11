# Recording a DISET exchange as evidence

The clean-room claim in [`../CLEANROOM.md`](../CLEANROOM.md) rests on facts about
a protocol, and the strongest fact is a conversation with a real server. This
directory holds such conversations, recorded reproducibly, so that a reader can
check the specification's claims against bytes rather than trust them.

## How to record one

Two commands, both in this repository:

```bash
# one call of DIRAC's own client, captured (tcpdump needs root; -E keeps SSLKEYLOGFILE)
sudo -E python3 tools/diset_capture.py run -- \
    dirac-proxy-init --group lhcb_user --rfc

# or any client, so long as the call reaches a DIRAC service:
sudo -E python3 tools/diset_capture.py run -- \
    python -c 'from DIRAC.Core.Security.DiracX import addTokenToPEM; addTokenToPEM("/tmp/x509up_u501", "lhcb_user")'
```

`run` sets `SSLKEYLOGFILE` (CPython's `ssl` honours it), captures the traffic on
port 8443, runs the command, decrypts with `tshark` when it is installed, and
writes a bundle:

```
<UTC date>-<command>/
  capture.pcap      the raw TLS capture
  keys.log          the TLS key log, which is what makes the pcap readable
  client.bin        the client-to-server plaintext, when it could be recovered
  server.bin        the server-to-client plaintext
  transcript.md     the decoded conversation, redacted, with each stream's sha256
  evidence.json     the same as data, for a checker
```

Then commit `transcript.md` and `evidence.json`, and the two `*.bin` streams if
a reviewer needs them. **`capture.pcap` and `keys.log` are not committed** by
default (`.gitignore` here): a key log decrypts the session, and a session may
carry a token. Keep them somewhere durable outside the repository and record
their `sha256` in the bundle's `evidence.json` so the link is preserved:

```bash
shasum -a 256 capture.pcap keys.log >> SHA256SUMS
```

## What is redacted, and why that is not hiding evidence

`xrddirac.testing.diset_trace` replaces anything that could be a credential
before it writes a transcript: strings matching a bearer token, a JWT, a PEM
block, `access_token=`, `client_secret`, `password`; and any other string longer
than 400 characters is cut with its length kept. The replacement is visible —
`<redacted: 512 chars>` — so a reviewer sees that something was there and how
big it was. A transcript that leaked a token would be worse than no evidence at
all, and one that silently dropped a message would be a lie.

Everything else — every frame, every message shape, every `S_OK`/`S_ERROR`, every
keep-alive — is recorded verbatim, because that is the evidence.

## What a recorded exchange must settle

`../diset-spec.md` §11 lists the seven open items (C1–C7). A recording is
accepted as evidence for an item when the transcript shows the bytes that answer
it; the specification is then amended to cite the bundle, and the item moves
from **[impl]** to **[capture]**. The specification is the only thing an
implementation is written from, so the amendment is part of the evidence commit.
