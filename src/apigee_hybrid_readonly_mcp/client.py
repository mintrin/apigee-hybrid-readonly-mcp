"""Read-only HTTP client for the Apigee management (control plane) API.

Apigee Hybrid uses the same management API as Apigee X:
    https://apigee.googleapis.com/v1/organizations/{org}/...

Read-only is enforced here, in code, independent of IAM:
  * Only GET is ever sent, plus a tiny allowlist of POST methods that are
    reads in practice (organizations/{org}:getSyncAuthorization).
  * Every path must sit under organizations/{org}/ for an org the registry
    knows (config file / allowlist), or any org in open mode.
  * Every path segment is validated against a strict charset, so a crafted
    argument can't escape the org, smuggle a query string or a custom verb.
  * Query-parameter names are validated too, so nothing like a method
    override can be passed through.

Each request is routed by its org: the org's tier decides which credential
(and API base URL) is used.

Resilience: transient failures (429/5xx/network) are retried with backoff,
a 401 triggers one token refresh, response size is capped, and every request
is logged (method, org, path, status, latency; never the token).

Pair this with service accounts holding roles/apigee.readOnlyAdmin so that
even a bug here can't write anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any, Awaitable, Callable

import httpx2 as httpx

from . import __version__
from .auth import AuthError, TokenProvider, build_token_provider
from .config import CredSpec, OrgConfig, Registry, UnknownOrg
from .redact import mask_text, redact

log = logging.getLogger("apigee_hybrid_readonly_mcp.http")

MAX_RESPONSE_CHARS = int(os.environ.get("APIGEE_MAX_RESPONSE_CHARS", "120000"))
MAX_RESPONSE_BYTES = int(os.environ.get("APIGEE_MAX_RESPONSE_BYTES", str(20 * 1024 * 1024)))
MAX_RETRIES = int(os.environ.get("APIGEE_MAX_RETRIES", "3"))

# Apigee resource names: letters, digits, dot, dash, underscore, and a few more
# characters seen in developer emails / app names. No '/', '?', '#', ':', '%' or '..'.
_SEGMENT = re.compile(r"^[A-Za-z0-9._@+\-][A-Za-z0-9._@+\- ]{0,254}$")
# Query parameters are an allowlist of the Apigee list/filter/analytics parameters.
# Google API *system* parameters must never pass: `callback`/`alt` change the response
# format (JSONP/proto) so JSON redaction would not run, `fields` can strip the keys
# redaction relies on, `format=bundle` returns proxy ZIPs, and `$httpMethod`,
# `access_token`, `oauth_token`, `key`, `quotaUser`... change method or identity.
_ALLOWED_PARAMS = frozenset({
    # paging / listing
    "pageSize", "pageToken", "count", "startKey", "rows", "expand", "ids",
    "includeRevisions", "includeMetaData", "includeCred", "includeCompany",
    "apiProduct", "apptype", "appType", "keyStatus", "status", "app",
    "filter", "sharedFlows",
    # analytics (stats API)
    "select", "timeRange", "timeUnit", "sortby", "sort", "limit", "offset",
    "topk", "tsAscending", "tzo", "accuracy", "realtime", "aggTable",
})
_MAX_PARAM_VALUE = 2000

# Paths whose payloads are inherently secret-bearing and have no safe redaction:
# debug-session transaction data contains live request headers, bodies and variables.
_DENIED_PATH = re.compile(r"(^|/)debugsessions/[^/]+/data(/|$)", re.IGNORECASE)
# The only endpoints allowed to return non-JSON bodies (raw resource file contents).
_TEXT_PATH = re.compile(r"/resourcefiles/[^/]+/[^/]+$")

# Custom methods that do not mutate anything. Keep these short and explicit.
_READ_ONLY_POST_VERBS = {":getSyncAuthorization"}
_READ_ONLY_GET_VERBS = {":getProjectMapping"}

_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_EXC = (httpx.TransportError,)  # connect/read timeouts, resets, protocol errors


class ApigeeError(RuntimeError):
    """An error response (or network failure, status 0) from the management API."""

    def __init__(self, status: int, message: str, path: str) -> None:
        label = f"HTTP {status}" if status else "Network error"
        super().__init__(f"{label} on {path}: {message}")
        self.status = status
        self.path = path


class ForbiddenOperation(ValueError):
    """Raised when a request would break the read-only / scoping rules."""


# Errors a tool should report to the model instead of crashing.
TOOL_ERRORS = (ApigeeError, ForbiddenOperation, UnknownOrg, AuthError)


def seg(value: Any, what: str = "name") -> str:
    """Validate one caller-supplied path segment; returns it unchanged.

    Encoding happens once, in ApigeeClient._check_path, which re-validates every
    segment of the final path (so tools can't bypass this by building paths by hand).
    """
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value) or set(value) <= {"."} or value != value.strip():
        shown = value[:80] if isinstance(value, str) else type(value).__name__
        raise ForbiddenOperation(f"Invalid {what}: {shown!r}")
    return value


def _check_params(params: dict[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ForbiddenOperation("params must be an object of name -> value")
    clean: dict[str, Any] = {}
    for k, v in params.items():
        if not isinstance(k, str) or k not in _ALLOWED_PARAMS:
            raise ForbiddenOperation(
                f"Query parameter not allowed: {str(k)[:40]!r}. Allowed: {', '.join(sorted(_ALLOWED_PARAMS))}"
            )
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        if not isinstance(v, (str, int, float)):
            raise ForbiddenOperation(f"Query parameter {k!r} must be a string or number")
        if len(str(v)) > _MAX_PARAM_VALUE:
            raise ForbiddenOperation(f"Query parameter {k!r} is too long")
        clean[k] = v
    return clean


ProviderFactory = Callable[[CredSpec], TokenProvider]


class ApigeeClient:
    def __init__(
        self,
        registry: Registry,
        provider_factory: ProviderFactory = build_token_provider,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = MAX_RETRIES,
        backoff_s: float = 0.5,
    ) -> None:
        self.registry = registry
        self._factory = provider_factory
        self._providers: dict[CredSpec, TokenProvider] = {}
        self._max_retries = max(0, max_retries)
        self._backoff_s = backoff_s
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(registry.timeout_s, connect=min(10.0, registry.timeout_s)),
            transport=transport,
            follow_redirects=False,  # never let a redirect carry the token elsewhere
            headers={"Accept": "application/json", "User-Agent": f"apigee-hybrid-readonly-mcp/{__version__} (read-only)"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- scoping -------------------------------------------------------

    def org(self, org: str | None) -> str:
        """Resolve a name or alias to the canonical org name (or raise)."""
        return self.org_config(org).name

    def org_config(self, org: str | None) -> OrgConfig:
        cfg = self.registry.resolve(org)
        seg(cfg.name, "organization")
        return cfg

    def provider_for(self, cred: CredSpec) -> TokenProvider:
        """One cached token provider per distinct credential (typically one per tier)."""
        if cred not in self._providers:
            self._providers[cred] = self._factory(cred)
        return self._providers[cred]

    def _check_path(self, path: Any) -> tuple[str, OrgConfig | None]:
        """Validate a resource path; returns (canonical path, org config)."""
        if not isinstance(path, str) or len(path) > 2048:
            raise ForbiddenOperation("Path must be a string of at most 2048 characters.")
        path = path.strip().lstrip("/")
        if path.startswith("v1/"):
            path = path[3:]
        parts = path.split("/")
        if parts == ["organizations"]:
            return path, None  # list orgs visible to the default credential
        if parts[0] != "organizations" or len(parts) < 2:
            raise ForbiddenOperation("Path must start with organizations/{org}.")
        org_part, sep, verb = parts[1].partition(":")
        cfg = self.org_config(org_part)  # raises if unknown; also maps aliases
        if sep:
            if len(parts) > 2 or not _SEGMENT.fullmatch(verb) or ":" in verb:
                raise ForbiddenOperation(f"Malformed path: {path[:120]!r}")
        out = ["organizations", cfg.name + sep + verb]
        for p in parts[2:]:
            if ":" in p:
                raise ForbiddenOperation(f"Custom methods are not allowed here: {p[:80]!r}")
            if out[-1] == "stats" and "," in p:
                # stats/{dim1,dim2}: the one place a list appears in a path; validate each part
                out.append(",".join(seg(d, "dimension") for d in p.split(",")))
                continue
            out.append(seg(p, "path segment").replace(" ", "%20"))
        canonical = "/".join(out)
        if _DENIED_PATH.search(canonical):
            raise ForbiddenOperation("Debug-session transaction data is not available through this server (it contains live credentials).")
        return canonical, cfg

    # ---- requests ------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        path, cfg = self._check_path(path)
        last = path.split("/")[-1]
        if ":" in last and last[last.index(":"):] not in _READ_ONLY_GET_VERBS:
            raise ForbiddenOperation(f"Only {sorted(_READ_ONLY_GET_VERBS)} are allowed as GET custom methods.")
        return await self._send("GET", path, cfg, params=_check_params(params))

    async def read_post(self, path: str) -> Any:
        """POST to an allowlisted read-only custom method."""
        path, cfg = self._check_path(path)
        verb = path[path.rfind(":"):] if ":" in path else ""
        if verb not in _READ_ONLY_POST_VERBS:
            raise ForbiddenOperation(f"POST is only permitted for {sorted(_READ_ONLY_POST_VERBS)}.")
        return await self._send("POST", path, cfg, json_body={})

    async def _send(
        self,
        method: str,
        path: str,
        cfg: OrgConfig | None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        # Final guard: nothing other than GET / allowlisted POST ever leaves this process.
        if method == "POST":
            if path[path.rfind(":"):] not in _READ_ONLY_POST_VERBS:
                raise ForbiddenOperation("POST is only permitted for allowlisted read-only methods.")
        elif method != "GET":
            raise ForbiddenOperation(f"Method {method} is not allowed.")
        if cfg is None:  # bare "organizations" listing
            base, cred, allow_kvm = self.registry.default_api_base, self.registry.default_cred, False
        else:
            base, cred, allow_kvm = cfg.api_base, cfg.cred, cfg.allow_kvm_values
        provider = self.provider_for(cred)
        org_name = cfg.name if cfg else "-"

        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            token = await provider.token()
            started = time.monotonic()
            try:
                status, headers, body = await self._request(method, f"{base}/{path}", params, json_body, token)
            except _RETRY_EXC as e:
                log.warning("%s org=%s /%s network error attempt=%d: %s", method, org_name, path, attempt, type(e).__name__)
                if attempt <= self._max_retries:
                    await self._sleep(attempt, None)
                    continue
                raise ApigeeError(0, f"{type(e).__name__} after {attempt} attempts", "/" + path) from None
            ms = int((time.monotonic() - started) * 1000)
            log.info("%s org=%s /%s status=%d ms=%d attempt=%d", method, org_name, path, status, ms, attempt)

            if status == 401 and not refreshed:
                refreshed = True
                provider.invalidate()
                continue
            if status in _RETRY_STATUS and attempt <= self._max_retries:
                await self._sleep(attempt, headers.get("retry-after"))
                continue
            break

        if status >= 400:
            raise ApigeeError(status, _error_message(body), "/" + path)
        if 300 <= status < 400:
            raise ApigeeError(status, "unexpected redirect (not followed)", "/" + path)
        if not body:
            return {}
        ctype = headers.get("content-type", "")
        if "json" in ctype:
            try:
                data = json.loads(body)
                if "keyvaluemaps" in path.lower().split("/") and not allow_kvm:
                    data = _mask_kvm_values(data)
                return redact(data) if self.registry.redact_secrets else data
            except RecursionError:
                raise ApigeeError(status, "response nesting too deep", "/" + path) from None
            except ValueError:
                raise ApigeeError(status, "response was not valid JSON", "/" + path) from None
        # Fail closed: only resource-file contents may be non-JSON. Anything else (JSONP,
        # proto, ZIP...) could carry data that JSON redaction never saw.
        if not _TEXT_PATH.search(path):
            raise ApigeeError(status, f"unexpected non-JSON response ({ctype or 'no content type'}); refused", "/" + path)
        text = body.decode("utf-8", errors="replace")[:MAX_RESPONSE_CHARS]
        return {"contentType": ctype, "body": mask_text(text) if self.registry.redact_secrets else text}

    async def _request(
        self, method: str, url: str, params: dict[str, Any] | None, json_body: Any, token: str
    ) -> tuple[int, httpx.Headers, bytes]:
        """One HTTP exchange with a bounded body size."""
        async with self._http.stream(
            method, url, params=params or None, json=json_body, headers={"Authorization": f"Bearer {token}"}
        ) as resp:
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ApigeeError(resp.status_code, f"response larger than {MAX_RESPONSE_BYTES} bytes; narrow the query", url.split("/v1", 1)[-1])
                chunks.append(chunk)
            return resp.status_code, resp.headers, b"".join(chunks)

    async def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = self._backoff_s * (2 ** (attempt - 1)) * (0.5 + random.random())
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        await asyncio.sleep(min(delay, 10.0))

    # ---- fan-out -------------------------------------------------------

    async def for_each_org(
        self, orgs: list[OrgConfig], fn: Callable[[OrgConfig], Awaitable[Any]]
    ) -> dict[str, Any]:
        """Run fn per org with bounded concurrency.

        Per-org failures become {"error": message, "status": http_status_or_null}
        so one bad org never fails the whole fan-out.
        """
        sem = asyncio.Semaphore(self.registry.max_parallel)

        async def one(cfg: OrgConfig) -> tuple[str, Any]:
            async with sem:
                try:
                    return cfg.name, await fn(cfg)
                except TOOL_ERRORS as e:
                    return cfg.name, {"error": str(e), "status": getattr(e, "status", None)}
                except Exception:  # a bug or odd response shape in one org must not sink the rest
                    log.exception("unexpected error for org %s", cfg.name)
                    return cfg.name, {"error": "internal error (see server log)", "status": None}

        results = await asyncio.gather(*(one(o) for o in orgs))
        return dict(results)


def _error_message(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            text = str(parsed["error"].get("message") or text)
    except ValueError:
        pass
    return mask_text(" ".join(text.split()))[:500]


def _mask_kvm_values(data: Any) -> Any:
    """KVM entries often hold credentials: keep names, hide every `value`, at any depth."""
    hint = "<hidden: set allow_kvm_values for this tier/org to show>"
    if isinstance(data, dict):
        return {k: (hint if k == "value" else _mask_kvm_values(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_mask_kvm_values(v) for v in data]
    return data


def to_text(data: Any) -> str:
    """Serialize a response for the model, truncating very large payloads."""
    text = json.dumps(data, indent=1, sort_keys=False, default=str)
    if len(text) > MAX_RESPONSE_CHARS:
        text = (
            text[:MAX_RESPONSE_CHARS]
            + f"\n... [truncated: {len(text) - MAX_RESPONSE_CHARS} more characters; narrow the query or use paging]"
        )
    return text
