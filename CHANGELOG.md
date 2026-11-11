# Changelog

## 0.5.0 - unreleased

First prototype.

- DISET client (`dips://`): TLS with the X.509 proxy, DEncode and JSON, keep-alives,
  RPC and file transfer, failover over a service's URLs.
- DiracX client: tokens exchanged from the proxy through `ProxyManager`, kept in memory;
  job search, summary, status changes, sandboxes in S3.
- Profiles for LHCb's and GridPP's DIRAC; the configuration snapshot cached and refreshed
  only when newer; DIRAC 8 and 9 service layouts.
- Read-only data: the File Catalog (bulk, chunked, parallel), LHCb Bookkeeping (size
  checked before listing), storage element URLs built as DIRAC builds them, replica
  availability from the RSS and the WLCG Tape REST API.
- Jobs: submit (with sandboxes, parametric), find, status, kill, delete, reschedule,
  output, parameters, logging, JDL.
- The `dirac` backend for xrdwms, and `xrddirac.lhcb`, a Ganga-like dataset/application
  layer over dask.
- The `xrddirac` command line, with the shared `--json`/`--xml` report.
- `xrddirac.testing`: an in-process fake DIRAC (DISET, DiracX, S3, tape REST).
- `lfn:` as an xrdwms storage scheme (`xrddirac.lfnstore.LFNStore`, offered through the
  backend's `url_storage`): large inputs staged to `lfn:` are uploaded to `upload_se` and
  registered in the catalog (undone if registration fails); LFN outputs are checked in the
  catalog (`verify="replica"`: against a replica too), within `output_grace` (1800 s); staged
  LFNs are removed when a job is cleaned. Needs xrdwms with `url_storage` (after 0.4.0).
- The backend puts `lfn:` inputs in `InputSandbox` (DIRAC downloads them into the job's
  directory) rather than `InputData`; datasets go in `native["dirac"]["InputData"]`.

## Testing

- Exercised end to end on **macOS** (Apple Silicon) with X.509 authentication
  against production LHCb DIRAC: `whoami` and the configuration, catalogue
  queries (`ls`, `du`, `replicas`, `status`, `urls`, `meta`), job submission and
  tracking (`submit`, `jobs`, `job-status`, `logging`), and the write cycle on
  the LFC (`addFile` through `LFNStore`, then `removeFile`) - plus the proxy
  exchange DIRAC performs for its own tokens. The LHCb CVMFS client is
  Linux-only, so its configuration is read from CVMFS and its servers spoken to
  from macOS directly.
