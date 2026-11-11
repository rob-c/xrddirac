# Attestations

Every file that speaks the protocol is either **attested** (its author states it
was written from `docs/diset-spec.md` and public documentation only) or listed as
**pending** re-derivation. `tests/test_cleanroom.py` checks this file against the
files that exist, so a new protocol file cannot appear without an entry and the
pending list cannot grow silently.

## Attested

_None yet._

## Re-derived on branch `feature/diset-rederivation` — provenance, stated plainly

Both modules below have been rewritten from `docs/diset-spec.md`. The author had
**read DIRAC's source** before writing them (to answer a question about DIRAC
tokens), so:

* this is a *re-derivation from a written specification*, which is the useful
  half of the exercise, and
* it is **not** a clean-room work in the legal sense, because the implementer was
  not uncontaminated.

The claim that can be made today is: "written from a specification that cites
recorded conversations with a real server, by an author who had read the
reference". Turning that into a clean-room claim needs the implementation to be
repeated by an author who has not, per
[`CLEANROOM-HANDOFF.md`](CLEANROOM-HANDOFF.md) — the specification, the
conformance tests and the acceptance recording are all in place for that.

```
src/xrddirac/codec.py   re-derived from §4; complete, all codec tests pass
src/xrddirac/diset.py   re-derived from §2/§3/§5/§6/§7; 11 of 16 transport tests pass
```
