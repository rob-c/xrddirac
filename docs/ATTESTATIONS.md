# Attestations

Every file that speaks the protocol is either **attested** (its author states it
was written from `docs/diset-spec.md` and public documentation only) or listed as
**pending** re-derivation. `tests/test_cleanroom.py` checks this file against the
files that exist, so a new protocol file cannot appear without an entry and the
pending list cannot grow silently.

## Attested

_None yet._

## Pending re-derivation

The provenance of these is not recorded; the maintainer must attest to them or
they are re-derived from the specification (see
[`CLEANROOM-HANDOFF.md`](CLEANROOM-HANDOFF.md)).

```
src/xrddirac/diset.py
src/xrddirac/codec.py
```
