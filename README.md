# xrddirac

A light, pure Python 3 DIRAC client: the file catalog, LHCb's Bookkeeping, replica
availability (online / nearline) and jobs, on LHCb's and GridPP's DIRAC - **without any
DIRAC code installed**. It speaks DIRAC's own protocols itself: DISET (`dips://`, TLS with
your X.509 proxy) for the classic services, and DiracX's REST API (with a token it makes
from your proxy, kept in memory only) where the installation has DiracX.

Status: **pre-alpha prototype** (0.5.0). Read paths are tested live against LHCb's
production DIRAC; job submission is tested against an in-process fake DIRAC only (see
[docs/DESIGN.md](docs/DESIGN.md#what-was-tested-where)).

Built on the same libraries as xrdarc - [xrdcore](https://github.com/rob-c/xrdcore),
[xrdsec](https://github.com/rob-c/xrdsec) (credentials, TLS, VOMS),
[xrdhttp](https://github.com/rob-c/xrdhttp) (HTTP, the WLCG tape REST API) - and is the
`dirac` backend of [xrdwms](https://github.com/rob-c/xrdwms), which makes DIRAC jobs dask
tasks.

## From the shell

```console
$ xrddirac whoami
identity   /DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jdoe/CN=123456/CN=Jane Doe
time left  154h07m
VOMS       /lhcb/Role=NULL/Capability=NULL
profile    lhcb (VO lhcb)
DIRAC      jdoe in lhcb_user (NormalUser, PrivateLimitedDelegation)

$ xrddirac status /lhcb/LHCb/Collision24/B2CC.DST/00278103/0023/00278103_00230015_1.b2cc.dst
ONLINE      /lhcb/LHCb/Collision24/B2CC.DST/00278103/0023/00278103_00230015_1.b2cc.dst
    GRIDKA-ARCHIVE         tape    NEARLINE             https://lhcbdcache-kit-tape.gridka.de:2880/...
    SARA-DST               disk    ONLINE               root://xrootd.grid.surfsara.nl//pnfs/...
    Glasgow-DST            disk    ONLINE               root://cephc02.gla.scotgrid.ac.uk:1094//cephfs/...
files=1 ONLINE=1

$ xrddirac bk -s "/LHCb/Collision24/Beam6800GeV-VeloClosed-MagDown/Real Data/Sprucing24c2a/94000000/B2CC.DST"
# 24127 files, 1778879264 events, 100.61 TB, lumi 1.099e+09
$ xrddirac bk -r 299632 ".../B2CC.DST"            # one run: 10 files
$ xrddirac bk -r 299632 ".../B2CC.DST" | xrddirac status -q -     # is it all online?
```

| Command | What it does (none of these change anything in DIRAC) |
|---|---|
| `whoami`, `profiles`, `config [PATH]` | your proxy and DIRAC identity; the installations; the configuration |
| `ls [-l] DIR`, `du DIR` | catalog directories and their sizes |
| `replicas LFN...` | which storage elements hold each file, disk or tape |
| `status LFN...` | `ONLINE` / `NEARLINE` / `UNAVAILABLE` / `UNKNOWN` per file, and per replica |
| `urls [--protocol root\|https] LFN...` | each replica's current URL, built as DIRAC builds it |
| `meta [--bk] LFN...` | size, checksum, GUID; LHCb run, events, luminosity, data quality |
| `bk PATH [-s] [-r RUN] [--runs A:B] [-n LIMIT]` | a Bookkeeping dataset: its size first, then its files |
| `se [SE...]` | storage elements: kind, RSS status, endpoints |
| `jobs`, `job-status`, `summary`, `logging`, `parameters`, `jdl`, `output` | your jobs |

Changing (your jobs only): `submit --jdl job.jdl` or `submit -i run.sh -- /bin/sh run.sh`,
`kill`, `delete`, `reschedule`.

LFNs come as arguments, from `-F FILE`, or from standard input (`-`). Every command takes
`--json`/`--xml` for the versioned report shared with xrdarc, xrdclient and xgfalclient,
and `-P gridpp` (or a profile of your own) for another DIRAC. Exit status: 0 all worked,
1 some items failed (or some files are not online), 2 usage, 3 credentials, 4
configuration, 5 a DIRAC service, 6 a local file, 7 a timeout, 8 a query over `--limit`.

`status` is the only command that talks to storage: it asks each tape system's WLCG Tape
REST API (`archiveinfo`, which stages nothing) where its replicas are, one request per
endpoint for the whole list. `--no-locality` skips that.

## From Python

```python
from xrddirac import DiracSession

with DiracSession("lhcb") as dirac:
    files = dirac.bookkeeping.files(path, runs=(299632,))          # TooLargeError past limit=1000
    replicas = dirac.catalog.replicas(files)                        # Bulk: .successful, .failed
    online = dirac.replica_status.check(replicas.successful)        # {lfn: FileAvailability}
    url = dirac.storage("Glasgow-DST").url(lfn, "root")
    job = dirac.jobs.submit('[Executable = "/bin/hostname";]').job_ids[0]
    dirac.jobs.status([job])
```

## Jobs as dask tasks (xrdwms)

```toml
# ~/.config/xrdwms/config.toml
[backends.lhcb]
kind = "dirac"
profile = "lhcb"
```

```console
$ xrdwms submit -b lhcb -t 1h -o out.txt -- /bin/sh -c 'hostname > out.txt'
```

Files can live in the catalog on both sides of a job. With `staging` an `lfn:` directory,
inputs over the backend's sandbox limit are uploaded to `upload_se` and registered, and the
job gets them through its `InputSandbox`; outputs with `lfn:` destinations are uploaded and
registered by DIRAC's job wrapper, and xrdwms counts the job done only once the catalog has
them (waiting up to `output_grace`, as DIRAC may register them late):

```toml
# ~/.config/xrdwms/config.toml
staging = "lfn:/lhcb/user/j/jdoe/xrdwms"

[backends.lhcb]
kind = "dirac"
profile = "lhcb"
upload_se = "CERN-USER"
verify = "catalog"        # or "replica": also compare a replica's size and adler32
output_grace = 1800
```

This needs an xrdwms with the `url_storage` hook (newer than 0.4.0). It is the one feature
that writes to the DIRAC catalog, and it has been tested against the fake DIRAC only.

and the Ganga-like layer on top ([`xrddirac.lhcb`](src/xrddirac/lhcb.py)):

```python
from xrddirac.lhcb import Dataset, GaudiApp, submit_dataset, merge_outputs

data = Dataset.from_bookkeeping(dirac, path, runs=(299632,)).online(dirac)
futures = submit_dataset(client, GaudiApp("DaVinci", "v65r0", options=("ntuple.py",)),
                         data, backend="lhcb", files_per_job=5, outputs=("tuple.root",))
paths = client.submit(merge_outputs, futures, "tuple.root").result()
```

## Profiles

`lhcb` (the default) and `gridpp` are built in. Others, and changes to those, go in
`~/.config/xrddirac/config.toml`:

```toml
default = "lhcb"

[profiles.gridpp]
vo = "lz"                      # GridPP's DIRAC is multi-VO: the VO you work in

[profiles.mydirac]
servers = ["dips://cs.example.org:9135/Configuration/Server"]
vo = "myvo"
setup = "MyVO-Production"      # DIRAC 8 and older only
```

The configuration snapshot is cached in `~/.cache/xrddirac/<profile>/` (`0600`) and
refreshed, only if newer, after an hour (`config_max_age`).

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q -n 4 --cov        # 100 % line and branch; hermetic (fake DIRAC)
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy
.venv/bin/complexipy --quiet
XRDDIRAC_INTEROP=1 .venv/bin/python -m pytest -q tests/test_interop.py -o addopts=   # live, read-only
```

The suite runs against `xrddirac.testing.FakeDirac`: an in-process DISET server, DiracX,
S3 sandbox store and tape REST API over one model, in both DIRAC 9 (LHCb) and DIRAC 8
(GridPP) shapes, with xrdsec's throwaway PKI.
