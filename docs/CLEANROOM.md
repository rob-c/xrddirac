# Provenance, licensing, and how the clean room is enforced

xrddirac is **LGPL-3.0-or-later** (`LICENSE`, `COPYING`). That is not a
preference: it is the only licence that keeps the rest of the stack legal, and
it is why this project needs a written specification rather than a reading of
DIRAC's source.

## 1. Why LGPL, and what would break if it were GPL

DIRAC is **GPL-3.0-only**. This stack is **LGPL-3.0-or-later**.

| arrangement | result |
| --- | --- |
| xrddirac is **LGPL** and written clean-room | every tool that imports it stays LGPL. **This is the plan.** |
| xrddirac is **GPL** and a tool imports it | the combined work must be GPL-3.0: xrdwms, xrdsec, xrdclient, xgfalclient, xrdarc would all have to become GPL-3.0, and anything depending on them too. |
| xrddirac is **GPL** and only ever *run as a separate program* | the GPL stays with xrddirac, but that is a subprocess boundary around an in-process protocol: poor design, and every caller becomes responsible for an external executable. Not recommended. |
| xrddirac contains **code derived from DIRAC's source** | it is a GPL derivative whatever the `LICENSE` file says, so the first row is unavailable to it: it could not be used by this stack at all. |

So: **a clean-room xrddirac is LGPL; a derived one is unusable here.** That is
the whole reason for the process below.

A separate matter, recorded because it is the one place the stack touches a GPL
package: `xrdclient`'s `xrd-proxy token init --flow dirac` *imports a separately
installed* DIRAC at the user's request, so that DIRAC itself performs the token
exchange. That is use at arm's length, not incorporation; the GPL obligations
attach to the DIRAC the user installed. *(Engineering guidance, not legal
advice: a final opinion on distributability should come from whoever handles
licensing for the project.)*

## 2. What may and may not enter this repository

**May:**

* `docs/diset-spec.md` — the specification. It records *facts about a
  protocol*: framing, encodings, message shapes, behaviours. Protocol facts and
  interfaces are not the part of software copyright protects, and the document
  is written as behaviour plus requirements, not as a translation of any
  implementation's code.
* implementations written **from that document**, and tests written from it.
* facts observed from real servers (captures, error messages) — and the server's
  own wording, quoted as data, because a client has to report it verbatim.
* facts documented by xrddirac's **own** LGPL code (it is this project's).

**May not:**

* DIRAC source, in whole or in part, in any form: no copied functions, no
  renamed functions, no paraphrased functions, no identifiers invented by
  reading it, no comments translated from it.
* a dependency on DIRAC in `pyproject.toml`, at any point including tests.
* fixtures or expected-output files taken from DIRAC.

## 3. The state of `src/xrddirac/diset.py` today, stated plainly

`src/xrddirac/diset.py` (455 lines) and `src/xrddirac/codec.py` already exist,
and the specification cites them as **[impl]** facts. Their provenance is not
recorded. Two things follow:

1. **The maintainer must attest to them** (§4). If they were written with DIRAC's
   source at hand, this project has a licensing problem that a `LICENSE` file
   does not fix, and the honest remedy is a re-derivation from
   `docs/diset-spec.md`.
2. **The specification is written so that a re-derivation is possible** whatever
   the answer: every requirement is numbered, every fact says where it came from,
   and §10 says which tests must exist before the code they test.

## 4. How the clean room is enforced

The standard clean room has **two roles**: someone who studies the reference and
writes a *specification*, and someone else who writes the *implementation from
the specification only*. One person cannot play both parts and call the result
clean — their memory of the reference is contamination.

**Who wrote what here, stated plainly:** the author of `docs/diset-spec.md` had
read DIRAC's published source (to answer a question about DIRAC tokens) before
writing it. The specification is therefore the *contaminated half* — which is
the half that is allowed to be contaminated: a specification built from the
reference is how clean-room projects start (it is the "spec team" output), and it
contains facts, not code.

**The implementation must be the uncontaminated half.** Concretely:

1. **The implementer works only from `docs/diset-spec.md`** and public
   documentation, and has not read DIRAC's source. That includes AI-assisted
   work: the implementation must be produced in a context that has not read the
   source, and must not later be "corrected" by one that has.
2. **Implementer attestation**, appended here in the commit that adds or
   re-derives the implementation:

   ```
   ## Attestation

   I, <name>, wrote the implementation in <paths> from docs/diset-spec.md and
   public documentation only. I have not read, copied, or paraphrased DIRAC's
   source, and I have not had it in the same working context as that code.
   Signed: <name>, <date>
   ```

3. **Review checklist** for every implementation change:

   * every behaviour implemented cites a numbered requirement (`§n.m`) of the
     specification, and the change names the numbers;
   * no identifier in `src/` matches a DIRAC identifier that is not also a
     protocol term defined in the specification's terminology;
   * no comment reads like a translation of another project's comment;
   * `pyproject.toml` still names no DIRAC dependency, and no test imports it.

4. **The mechanical half is a test.** `tests/test_cleanroom.py` checks that the
   package imports nothing from `DIRAC`, that no DIRAC requirement is declared,
   that ruff's ban on importing `DIRAC`/`diracx` is still configured, and that
   the specification exists and is the one the code cites. Provenance of thought
   cannot be tested; what can be tested, is.

## 5. What the specification deliberately does not claim

`docs/diset-spec.md` marks every fact with its source and lists seven questions
(§11) that only a recorded conversation with a real server can settle. A guess at
a wire format is a bug that looks like a working client, so the document keeps
them open rather than inventing answers — and the acceptance test is a capture,
not an opinion.
