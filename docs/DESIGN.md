# xrddirac: design

A DIRAC client with no DIRAC in it, for three jobs:

1. **Data, read-only**: find files (catalog, LHCb Bookkeeping), their replicas, the URLs to
   read them by, and whether each is readable now or only from tape. Small and medium
   datasets, quickly. Nothing is ever written to the catalog or the Bookkeeping.
2. **Jobs**: submit, monitor, kill, delete, reschedule and fetch the output of jobs on
   LHCb's and GridPP's DIRAC, directly and as xrdwms's `dirac` backend.
3. **A Ganga replacement, sketched**: the same work Ganga does, as a dask graph over
   xrdwms, with xrddirac providing the LHCb-specific pieces.

Non-goals: managing data (replication, removal, registration), production
(transformations), administration, or a DIRAC server of any kind.

## The pieces

```
           xrddirac CLI  (xrdcore.report: text, --json, --xml)
               |
        DiracSession -- profile (lhcb, gridpp, ~/.config/xrddirac/config.toml)
          |    |    |\
          |    |    | `-- config: CS snapshot, cached 0600, refreshed "if newer"
          |    |    `---- storage: SE definitions -> URLs, disk/tape         (cfg)
          |    |
          |    +-- catalog (DFC)     bookkeeping (LHCb)    replica_status (RSS + tape REST)
          |    +-- jobs: JobManager / JobMonitoring / SandboxStore (DISET), DiracX
          |
          +-- DisetClient (dips://: TLS + proxy, DEncode/JSON, failover)   <- xrdsec TLS
          +-- DiracX (REST, token exchanged from the proxy, memory only)    <- xrdhttp
          +-- tape REST (archiveinfo, per endpoint, bulk)                   <- xrdhttp.tape

    xrdwms (dask, journal, retries)  --entry point-->  xrddirac.wms.DiracBackend
    xrddirac.lhcb (Dataset, GaudiApp, split, submit_dataset, merge)  -- the Ganga sketch
```

Dependencies are the chain's libraries only (xrdcore, xrdsec, xrdhttp); xrdwms is the
optional `wms` extra. Ruff bans importing `DIRAC`, `diracx`, xrdclient, xgfalclient and
xrdarc, so nothing creeps in.

## Talking to DIRAC

### DISET (`diset.py`, `codec.py`)

Every DIRAC 8/9 service still answers `dips://host:port/System/Component`. Read from
DIRAC 9.1.21's source and checked against LHCb's and GridPP's live servers:

* TCP, then TLS with the user's proxy as the client certificate (plain `ssl`; the server
  allows proxy chains). Nothing else authenticates: DN, VO and group come from the chain.
* Messages are `<decimal length>:<body>`, the body one value in **DEncode** (a bencode
  relative: `i42e`, `s5:hello`, `l...e`, `t...e`, `d...e`, `za<tuple>` for datetimes) or
  **JSON** with three tagged objects (`dt`, `date`, `b64`). DIRAC 9 clients send JSON;
  DIRAC 8 servers (GridPP) answer DEncode; servers read either. xrddirac reads both
  (DEncode first, as DIRAC's `MixedEncode` does) and sends JSON unless a profile says
  `encoding = "dencode"`. DIRAC's JSON can also name *any* `JSerializable` class to be
  imported on decode; xrddirac never imports anything a peer names.
* A call: `S_OK([[System/Component, setup, vo], ["RPC", method], extraCredentials,
  clientVersion])`, the server's `S_OK()`, `S_OK([args...])`, the answer. A file
  transfer swaps the arguments for `[True, chunk]` messages, each acknowledged, ending
  `[False, md5]`. `dka`-prefixed keep-alive pings are answered and skipped.
* One connection per call (the server closes after answering, as with DIRAC's own client).
  Failover moves to a service's next URL only when connecting or proposing failed - never
  after the arguments were sent, because the call may have happened.

### DiracX (`diracx.py`)

LHCb runs DiracX (`/DiracX/URL = https://lhcbdiracx.app.cern.ch`). It wants a bearer token.
DIRAC 9 gives a proxy holder one through the DISET call
`Framework/ProxyManager.exchangeProxyForToken` (the server vouches for the proxy's owner,
via DiracX's legacy-exchange). xrddirac does exactly that, **keeps the token in memory
only** (never on disk, never logged, hidden from `repr`), and makes a new one shortly before
it expires or on a 401. So the proxy stays the one credential a user manages, and no
OAuth device flow is needed. Calls used are all in DiracX's published OpenAPI document:
job search (paged: 206 + `Content-Range`), summary, status changes, sandboxes (S3
presigned POST and GET), `userinfo`.

### Finding an installation (`profiles.py`, `config.py`, `cfg.py`)

A profile is a list of Configuration Service URLs, a VO and (DIRAC 8) a setup; `lhcb` and
`gridpp` are built in. The CS hands out its whole tree (`getCompressedData`, ~0.3 MB
compressed, 2.4 MB of `.cfg` for LHCb, parsed in ~50 ms). It is cached and, after
`config_max_age`, refreshed with `getCompressedDataIfNewer`, so most commands make no CS
call at all. A CS that does not answer leaves the cached copy in use, with a warning. The
cache holds the registry (users' DNs), so it is written `0600`. Service URLs are read
from both layouts: DIRAC 9 `/Systems/<S>/URLs/<C>`, DIRAC 8
`/Systems/<S>/<instance>/URLs/<C>` with the instance from `/DIRAC/Setups/<setup>/<S>`.

## Data

* **Catalog** (`catalog.py`): DFC calls answer DIRAC's bulk `{Successful, Failed}` shape,
  kept as `Bulk`, so a list with three bad LFNs still answers the rest. Lists are chunked
  (500) and the chunks run in parallel (4 workers) over the catalog's servers. There is no
  write method.
* **URLs** (`storage.py`): the catalog's PFNs are often stale (LHCb's still name CASTOR
  `srm://` paths), so URLs are built from each SE's definition exactly as DIRAC's
  `StorageElement.getURL` builds them: `Alias`, `BaseSE` inheritance with per-section
  overrides, `AccessProtocols` order, the xroot `//`, Echo's `lhcb:prod` pool paths, SRM's
  `WSUrl`. **Checked against DIRAC itself**: 30 URLs (15 LHCb SEs from CERN EOS and CTA
  to RAL Echo, dCache, StoRM and Glasgow, root and https) are identical
  (`tests/data/lhcb_urls.json`, re-checked live by the interop suite). One deliberate
  difference: DIRAC's xroot plugin prefixes `x509up_u<uid>@` (an XRootD login name that
  keeps sessions per proxy); xrddirac does not.
* **Availability** (`status.py`): a replica is readable now if its SE is readable in the
  Resource Status System (`Active`/`Degraded`; one `select` for every SE asked about) and
  either the SE is disk (`SEType = T0D1`) or the site's tape system says the file has a
  disk copy. Tape systems are asked with the WLCG Tape REST API's `archiveinfo` (what FTS
  and Rucio use; it stages nothing), grouped by endpoint, 1000 paths per request, in
  parallel, with a 10 s connect timeout. A file's verdict is `ONLINE`, `NEARLINE` (only on
  tape), `UNAVAILABLE` or `UNKNOWN`.
* **Bookkeeping** (`bookkeeping.py`): datasets by path
  (`/Config/Version/Conditions/Pass.../EventType/FileType`), with runs, run ranges,
  production and data-quality filters. `files()` asks for the dataset's summary first and
  refuses (`TooLargeError`) one over `limit` (1000) before listing a single file. The
  B2CC dataset used in testing is 24,127 files and 100 TB; one run of it is 10.

## Jobs (`jobs.py`, `jdl.py`, `sandbox.py`)

Each operation goes where DIRAC 9's own client sends it:

| operation | LHCb (DIRAC 9 + DiracX) | GridPP (DIRAC 8) |
|---|---|---|
| input sandbox | DiracX S3 store (tar.bz2, SHA-256) | DISET `SandboxStore` file transfer (MD5) |
| submit | `JobManager.submitJob(JDL)` (+ `confirmBulkSubmission` for parametric jobs) | same |
| status, find, summary | DiracX `jobs/search` (bulk, paged) | `JobMonitoring.getJobsSummary` / `getJobs` |
| kill, delete, reschedule | `JobManager` (it also clears task queues and pilots) | same |
| output | DiracX presigned download | `SandboxStore` file transfer from the sandbox SE |
| parameters, logging, JDL | `JobMonitoring` | same |

DIRAC runs a job with the owner's proxy, which pilots fetch from the `ProxyManager`. A user
who has never uploaded one gets `requireProxyUpload` on submission; xrddirac reports it
and does not upload (see *Open questions*). Output sandboxes may be `tar.zst` on DIRAC 9;
they are read with Python 3.14's `compression.zstd` or the `zstandard` package. Archives
are always extracted with the `data` filter, which blocks path traversal.

## The xrdwms backend (`wms.py`)

`DiracBackend` is a `SyncBackend` registered on xrdwms's `xrdwms.backends` entry point as
kind `dirac`. `JobSpec` becomes JDL: local and `lfn:` inputs to the input sandbox (DIRAC
downloads `LFN:` entries into the job's directory; datasets a job streams go in
`native["dirac"]["InputData"]`, as `GaudiApp` does), sandbox outputs to `OutputSandbox`,
`lfn:` outputs to `OutputData`, cores, CPU or wall time, memory as a `<N>GB` tag, platform,
sites, banned sites and tags. Anything else is refused with `UnsupportedError`, never
dropped. The idempotency key travels in the job name (`<name>~<key>`) as for ARC, so `find`
recovers jobs after a crash. DIRAC states map to xrdwms's, and failures are classified from
DIRAC's minor and application statuses (`AUTH`, `LIMIT`, `DATA`, `INFRA`, `PAYLOAD`), which
drives xrdwms's retries.

**LFNs as job data** (`lfnstore.py`, and two small xrdwms additions). xrdwms moves and
checks job data through one storage interface. A backend may now own a scheme
(`url_storage(url)`), and `Capabilities.output_grace` lets outputs registered after the job
ends still count. The DIRAC backend owns `lfn:`:

* *staging*: with `staging = "lfn:/..."`, an input over the sandbox limit is uploaded to
  `upload_se`'s write endpoint (its `WriteProtocols` first) through xrdwms's xgfalclient
  storage, then registered with `addFile` (size, adler32, GUID). If registration fails, the
  copy is removed. The same bytes at the same LFN are a no-op, so a retried stage-in is safe.
  Cleaning the job removes the replicas and the entry;
* *outputs*: DIRAC's job wrapper uploads and registers `OutputData`. xrdwms asks the store
  whether each LFN is registered with a replica and a size; `verify="replica"` also compares
  a replica's size and adler32. DIRAC can register late, through a failover request, so a
  job reported Done waits in `staging-out` for up to `output_grace` (1800 s) before missing
  outputs fail it as `DATA`, which xrdwms retries.

**Where it should live**: in the chain, xrdwms imports the clients (its ARC backend lives in
xrdwms and imports xrdarc). For a self-contained prototype the backend sits here, behind
the entry point. Adopted, it should move to `xrdwms/backends/dirac.py` with a `dirac`
extra, and `xrddirac` would then ban `xrdwms` as xrdarc does. xrdwms's inline
`kind:target` shorthand (`resolve_backend`) only knows `arc`, `local` and `fake`; letting a
backend class offer `from_target()` would make `-b dirac:lhcb` work without a config table.

## Replacing Ganga

Ganga's model is `Job(application, inputdata, splitter, backend, postprocessors)`, run by
Ganga's monitoring loop and stored in Ganga's repository. Every one of those already has
a modern equivalent in this stack; `xrddirac.lhcb` joins them:

| Ganga | here |
|---|---|
| `BKQuery`, `LHCbDataset` | `Dataset.from_bookkeeping` (size-checked), `.with_replicas`, `.online` |
| `SplitByFiles` (+ bulk submission by site) | `Dataset.split(files_per_job)`: files grouped by the SEs they can be read from, so DIRAC can run each subjob next to all its data |
| `GaudiExec` / prepared app | `GaudiApp` (`lb-run Project/version gaudirun.py options data.py`); the dataset's `data.py` (IOHelper) travels in the sandbox |
| `Dirac()` backend | xrdwms `dirac` backend - or ARC, or local, unchanged |
| monitoring loop | xrdwms `JobManager`: one bulk query per round, back-off, retries by failure kind |
| repository (`~/gangadir`) | xrdwms's SQLite journal, crash-safe, shared by dask workers and `xrdwms status` |
| `jobs(N).subjobs` | dask futures; `xrdwms status --json` |
| mergers | a dask task over the subjobs' results (`merge_outputs`; `hadd` for ROOT) |
| resubmit | `JobSpec.max_retries` for INFRA/DATA failures; pure keys make re-running a graph re-attach, not resubmit |

A whole analysis is one Python script or notebook: dataset to futures to merged output,
and it survives the laptop going to sleep, because the journal holds every job. The
prototype runs this whole path end to end against the fake DIRAC (`tests/test_lhcb.py`).

What a production replacement still needs:

* **Prepared applications**: Ganga's `GaudiExec` builds and ships a user's own project.
  Here only released `lb-run` projects are covered; user builds need a tarball (sandbox or
  EOS) or CVMFS/`lb-dev` support, and LHCb's AnalysisProductions route.
* **LHCb workflow JDL**: Ganga's DIRAC backend submits LHCb `Job()` workflows (step
  modules, `ProductionOutputData`, `bkLFNs`). A plain-executable JDL runs, but outputs
  registered in the catalog need `OutputData`/`OutputSE` handling and testing on LHCb.
* **Splitters** by events, by runs, and by dataset size; re-splitting failed subjobs.
* **A jobs table** (`xrdwms status` plus a small TUI or a notebook widget) and per-user
  defaults (platform, sites, output SE).
* **Output data**: listing, downloading and cleaning up a job's output LFNs (reads plus
  `xgfalclient` for the copies). This is the only point where managing user files comes in.

## Security

* The proxy is the only credential. DISET and the tape REST API present it; DiracX and S3
  get no certificate, only the bearer token, which exists only in process memory.
* Nothing is delegated: a server asking for delegation in a proposal is refused.
* All decoding of network data is total and bounded: message length limits, nesting
  depth, no class instantiation from JSON, a `data` tar filter, a sandbox download limit.
* The configuration cache is `0600`. Logs are xrdcore's, which redact URLs and tokens.

## What was tested where

* **Hermetic suite**: 132 tests, 100 % line and branch coverage, against `FakeDirac` (DISET,
  DiracX, S3, tape REST, both DIRAC shapes, xrdsec's test PKI), including whole jobs
  through xrdwms's `JobManager` and a dataset run as dask grid jobs on a gateway cluster.
* **Live, read-only, LHCb production** (2026-10-20, `tests/test_interop.py`, 6 tests): CS,
  catalog (listings, replicas, metadata), URLs against DIRAC's, Bookkeeping (summary, size
  guard, run-filtered listing, file metadata), RSS, tape locality at GridKa and SARA,
  DiracX `whoami` and job search, and the DISET-only job read path.
* **Live, GridPP**: the CS answers the lhcb proxy (DIRAC 8.0.84, DEncode). Other services
  need a GridPP VO membership, so the DIRAC 8 paths are tested against the fake only.
* **Not run live**: job submission, kill, delete and reschedule, sandbox upload and
  download, and LFN staging, registration and removal. These create real jobs or catalog
  entries, so they wait for an explicit go-ahead. The first live test should be a trivial
  job, then one small staged file under the user's own directory.

Timings on this host (warm, single LFN): `xrddirac replicas` 0.75 s and 55 MB RSS against
4.1 s and 120 MB for `lb-dirac dirac-dms-lfn-replicas`; a run-sized Bookkeeping listing
2.9 s; `status` with tape locality 1-10 s, bounded by the slowest tape endpoint's connect
timeout (CERN CTA's HTTPS port is not reachable from this host).

## Open questions and next steps

1. **Proxy upload**: implement `ProxyManager` delegation upload (`requestDelegationUpload`
   / `completeDelegationUpload`, signing DIRAC's CSR with xrdsec) so a user never needs
   `lhcb-proxy-init`? It writes the user's credential to DIRAC, so it should be opt-in.
2. **Tape locality over xroot** for SEs with only `root://` endpoints (or HTTPS that is
   firewalled, like CERN CTA from here). xrdclient has it, but the dependency direction
   needs it in a library first.
3. **Tornado/HTTPS DIRAC services** (`https://.../TornadoFTS3Manager`): none are needed
   for this scope yet; the client filters URLs to `dips://`.
4. **DiracX-native submission** (`POST /api/jobs/jdl`) once LHCb moves submission there;
   the client already has it.
5. **GridPP live testing** with a GridPP-VO proxy; DIRAC 8's `getJobsSummary` reply shape
   is handled in both forms, but has only been seen in DIRAC 9.
6. **Reuse a DiracX login**: a user who ran `dirac login lhcb` (device flow) has
   `~/.cache/diracx/credentials.json` (access ~20 min, refresh ~1 h). Reading it as a
   second token source, refreshed with DiracX's `refresh_token` grant, would let the
   DiracX-only operations work without a proxy. Not done: it is untested, and it acts
   with a credential the user did not hand to this prototype.
7. Move the backend into xrdwms, add `from_target()` shorthand, and bring the repository
   under xrd-devtools' conformance (`xrddevtools sync`), CI and the release gates.
