"""DiracX, DIRAC 9's REST API, through xrdhttp - with a token made from the user's proxy.

LHCb's DIRAC runs DiracX (``/DiracX/URL`` in the configuration) for job
search, job status changes and the sandbox store, which lives in S3. DiracX
takes a bearer token, not a certificate; a DIRAC client gets one by asking
the ``Framework/ProxyManager`` DISET service to *exchange* its proxy
(``exchangeProxyForToken``: the service vouches for the proxy's owner to
DiracX and hands back an access and a refresh token for the same user and
group). :class:`DiracX` does exactly that on first use and again shortly
before the access token expires.

**The token is kept in memory only**: never written to disk, never logged,
not shown by ``repr``. A process that wants one makes it from the proxy, so
the proxy stays the single credential a user manages.

Calls used here, all from DiracX's published OpenAPI document::

    GET   /api/auth/userinfo                    who the token is for
    POST  /api/jobs/search?page=&per_page=      jobs by condition (206 + Content-Range when paged)
    POST  /api/jobs/summary                     counts grouped by attributes
    POST  /api/jobs/jdl                         submit JDLs
    PATCH /api/jobs/status                      set statuses (kill, delete)
    POST  /api/jobs/reschedule                  reschedule jobs
    POST  /api/jobs/sandbox                     start a sandbox upload (a presigned S3 POST)
    GET   /api/jobs/{id}/sandbox[/{type}]       a job's sandboxes
    GET   /api/jobs/sandbox?pfn=                a presigned URL to download one
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from xrdhttp.session import Auth

from .errors import AuthorizationError, ServiceError, WireError
from .sandbox import Packed

if TYPE_CHECKING:
    from xrdhttp.session import Response

    from .session import DiracSession

__all__ = ["DiracX", "Token"]

#: Make a new token when the current one has less than this many seconds left.
REFRESH_MARGIN = 120.0
_RANGE = re.compile(r"(\d+)-(\d+)/(\d+|\*)")


@dataclass
class Token:
    """A DiracX access token and when it stops working; its text never shown."""

    access: str = field(repr=False)
    expires: float

    def fresh(self, now: float) -> bool:
        """Good for at least :data:`REFRESH_MARGIN` more seconds."""
        return now < self.expires - REFRESH_MARGIN


class DiracX:
    """DiracX for one installation; see the module docstring."""

    def __init__(self, session: DiracSession, url: str, *, clock: Any = time.time) -> None:
        self.session = session
        self.url = url.rstrip("/")
        self._clock = clock
        self._token: Token | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        """The URL, and nothing secret."""
        return f"DiracX({self.url!r})"

    # -- the token ---------------------------------------------------------------------------

    def token(self, *, renew: bool = False) -> str:
        """A fresh access token, made from the proxy when there is none (or ``renew``)."""
        with self._lock:
            now = self._clock()
            if renew or self._token is None or not self._token.fresh(now):
                self._token = self._exchange(now)
            return self._token.access

    def _exchange(self, now: float) -> Token:
        """``ProxyManager.exchangeProxyForToken``: the proxy's owner's token, from DIRAC."""
        answer = self.session.service("Framework/ProxyManager").call("exchangeProxyForToken")
        if not isinstance(answer, dict) or not isinstance(answer.get("access_token"), str):
            raise WireError("exchangeProxyForToken did not answer an access token")
        lifetime = answer.get("expires_in")
        lifetime = float(lifetime) if isinstance(lifetime, int | float) else 600.0
        return Token(answer["access_token"], now + lifetime)

    # -- requests ----------------------------------------------------------------------------

    def _auth(self, renew: bool = False) -> Auth:
        """The bearer header, over a TLS context presenting nothing."""
        return Auth(headers={"Authorization": f"Bearer {self.token(renew=renew)}"})

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: Mapping[str, Any] | None = None,
        ok: Sequence[int] = (200,),
    ) -> tuple[int, Any, Response]:
        """One call: ``(status, decoded JSON, response)``; a 401 makes a new token once.

        Statuses outside ``ok`` raise: 401/403 :class:`AuthorizationError`,
        anything else :class:`ServiceError` with DiracX's ``detail``.
        """
        target = self.url + path + (f"?{urlencode(query, doseq=True)}" if query else "")
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        response = self.session.http.request(
            method,
            target,
            body=data,
            headers=headers,
            auth=self._auth(),
            reauth=lambda _url, _resp: self._auth(renew=True),
            idempotent=method in ("GET", "HEAD") or path.endswith("/search"),
        )
        payload = response.body()
        decoded = _json(payload, path) if payload else None
        if response.status not in ok:
            raise _failure(response.status, decoded, method, path)
        return response.status, decoded, response

    # -- who ---------------------------------------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        """``/api/auth/userinfo``: the user, VO, DIRAC group and properties the token carries."""
        return dict(self.request("GET", "/api/auth/userinfo")[1] or {})

    # -- jobs ----------------------------------------------------------------------------------

    def search(
        self,
        search: Sequence[Mapping[str, Any]] = (),
        *,
        parameters: Sequence[str] | None = None,
        sort: Sequence[Mapping[str, str]] = (),
        per_page: int = 1000,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Jobs matching ``search`` (DiracX's ``{parameter, operator, value(s)}`` specs), all pages.

        ``limit`` stops after that many jobs. A normal user only ever sees
        their own jobs: DiracX's policy, not a filter added here.
        """
        body: dict[str, Any] = {"search": list(search), "sort": list(sort)}
        if parameters:
            body["parameters"] = list(parameters)
        jobs: list[dict[str, Any]] = []
        page = 1
        while True:
            status, found, response = self.request(
                "POST",
                "/api/jobs/search",
                body=body,
                query={"page": page, "per_page": per_page},
                ok=(200, 206),
            )
            jobs += [dict(job) for job in found or []]
            if limit is not None and len(jobs) >= limit:
                return jobs[:limit]
            if status != 206 or not _more(response.header("Content-Range"), len(jobs)):
                return jobs
            page += 1

    def summary(
        self, grouping: Sequence[str], search: Sequence[Mapping[str, Any]] = ()
    ) -> list[dict[str, Any]]:
        """Job counts grouped by ``grouping`` (``Status``, ``Site``...)."""
        body = {"grouping": list(grouping), "search": list(search)}
        return [dict(row) for row in self.request("POST", "/api/jobs/summary", body=body)[1] or []]

    def submit_jdl(self, jdls: Sequence[str]) -> list[dict[str, Any]]:
        """Submit each JDL; one ``{JobID, Status, MinorStatus, TimeStamp}`` per job made."""
        return [
            dict(job) for job in self.request("POST", "/api/jobs/jdl", body=list(jdls))[1] or []
        ]

    def set_status(
        self, job_ids: Sequence[int], status: str, minor: str, *, force: bool = False
    ) -> dict[str, Any]:
        """Set ``status`` on each job (``Killed``, ``Deleted``); DiracX's success/failed answer."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self._clock()))
        update = {
            str(job): {stamp: {"Status": status, "MinorStatus": minor, "Source": "xrddirac"}}
            for job in job_ids
        }
        query = {"force": "true"} if force else None
        return dict(self.request("PATCH", "/api/jobs/status", body=update, query=query)[1] or {})

    def reschedule(self, job_ids: Sequence[int]) -> dict[str, Any]:
        """Reschedule the jobs (back to the queue)."""
        return dict(
            self.request("POST", "/api/jobs/reschedule", body={"job_ids": list(job_ids)})[1] or {}
        )

    # -- sandboxes -----------------------------------------------------------------------------

    def upload_sandbox(self, packed: Packed) -> str:
        """Store ``packed`` in the sandbox store; its PFN (``SB:SandboxSE|/S3/...``) for the JDL.

        DiracX answers with a presigned S3 POST (a URL and form fields), or
        with no URL when the same bytes are already stored.
        """
        info = {
            "checksum_algorithm": "sha256",
            "checksum": packed.sha256,
            "size": packed.size,
            "format": packed.format,
        }
        answer = self.request("POST", "/api/jobs/sandbox", body=info)[1] or {}
        pfn = answer.get("pfn")
        if not isinstance(pfn, str):
            raise WireError("DiracX's sandbox upload answer has no pfn")
        url = answer.get("url")
        if url:
            body, content_type = _form(dict(answer.get("fields") or {}), packed.data)
            self.session.http.request(
                "POST",
                url,
                body=body,
                headers={"Content-Type": content_type},
                expect=(200, 201, 204),
            ).body()
        return pfn if pfn.startswith("SB:") else f"SB:SandboxSE|{pfn}"

    def job_sandboxes(self, job_id: int) -> dict[str, list[str]]:
        """The job's sandbox PFNs by type (``Input``, ``Output``)."""
        found = self.request("GET", f"/api/jobs/{int(job_id)}/sandbox")[1] or {}
        return {str(kind): [str(p) for p in pfns] for kind, pfns in dict(found).items()}

    def download_sandbox(self, pfn: str, *, limit: int = 1 << 30) -> bytes:
        """A sandbox's bytes, through the presigned URL DiracX hands out for it."""
        plain = pfn.split("|", 1)[1] if pfn.startswith("SB:") else pfn
        answer = self.request("GET", "/api/jobs/sandbox", query={"pfn": plain})[1] or {}
        url = answer.get("url")
        if not isinstance(url, str):
            raise WireError("DiracX did not answer a download URL for the sandbox")
        return self.session.http.request("GET", url, expect=(200,)).body(limit)


def _json(payload: bytes, path: str) -> Any:
    """A JSON body; :class:`WireError` for anything else."""
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise WireError(f"DiracX {path}: the answer is not JSON") from exc


def _failure(status: int, decoded: Any, method: str, path: str) -> ServiceError:
    """An unwanted status as an error, with DiracX's ``detail`` when it gave one."""
    detail = decoded.get("detail") if isinstance(decoded, dict) else None
    text = detail if isinstance(detail, str) else json.dumps(detail) if detail else f"HTTP {status}"
    kind = AuthorizationError if status in (401, 403) else ServiceError
    return kind(f"HTTP {status}: {text}", errno=status, service="DiracX", method=f"{method} {path}")


def _more(content_range: str, seen: int) -> bool:
    """Whether a ``Content-Range: jobs 0-99/1234`` says there are jobs past the ``seen`` ones."""
    match = _RANGE.search(content_range or "")
    if match is None:
        return False
    total = match.group(3)
    return total == "*" or seen < int(total)


def _form(fields: Mapping[str, str], data: bytes) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body: the presigned fields, then the file (S3 wants it last)."""
    boundary = secrets.token_hex(16)
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="file"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts += [data, f"\r\n--{boundary}--\r\n".encode()]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
