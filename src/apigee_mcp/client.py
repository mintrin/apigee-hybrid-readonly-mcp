"""Read-only HTTP client for the Apigee management (control plane) API.

Apigee Hybrid uses the same management API as Apigee X:
    https://apigee.googleapis.com/v1/organizations/{org}/...

Read-only is enforced here, in code, independent of IAM:
  * Only GET is ever sent, plus a tiny allowlist of POST methods that are
    reads in practice (organizations/{org}:getSyncAuthorization).
  * Every path must sit under organizations/{org}/ for an org the registry
    knows (config file / allowlist), or any org in open mode.
  * Path segments are validated so a crafted argument can't escape the org
    or smuggle a query string / custom verb.

Each request is routed by its org: the org's tier decides which credential
(and API base URL) is used.

Pair this with service accounts holding roles/apigee.readOnlyAdmin so that
even a bug here can't write anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Awaitable, Callable

import httpx2 as httpx

from .auth import AuthError, TokenProvider, build_token_provider
from .config import CredSpec, OrgConfig, Registry, UnknownOrg
from .redact import redact

MAX_RESPONSE_CHARS = int(os.environ.get("APIGEE_MAX_RESPONSE_CHARS", "120000"))

# Apigee resource names: letters, digits, dot, dash, underscore, and a few more
# characters seen in developer emails / app names. No '/', '?', '#', ':' or '..'.
_SEGMENT = re.compile(r"^[A-Za-z0-9._@+\- ]{1,255}$")

# POST methods that do not mutate anything. Keep this list short and explicit.
_READ_ONLY_POST_VERBS = {":getSyncAuthorization"}
_READ_ONLY_GET_VERBS = {":getProjectMapping"}


class ApigeeError(RuntimeError):
    def __init__(self, status: int, message: str, path: str) -> None:
        super().__init__(f"HTTP {status} on {path}: {message}")
        self.status = status
        self.path = path


class ForbiddenOperation(ValueError):
    """Raised when a request would break the read-only / scoping rules."""


# Errors a tool should report to the model instead of crashing.
TOOL_ERRORS = (ApigeeError, ForbiddenOperation, UnknownOrg, AuthError)


def seg(value: str, what: str = "name") -> str:
    """Validate one path segment supplied by a caller."""
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value) or value in {".", ".."}:
        raise ForbiddenOperation(f"Invalid {what}: {value!r}")
    return httpx.URL(f"/{value}").raw_path.decode()[1:]  # percent-encode spaces etc.


ProviderFactory = Callable[[CredSpec], TokenProvider]


class ApigeeClient:
    def __init__(
        self,
        registry: Registry,
        provider_factory: ProviderFactory = build_token_provider,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.registry = registry
        self._factory = provider_factory
        self._providers: dict[CredSpec, TokenProvider] = {}
        self._http = httpx.AsyncClient(
            timeout=registry.timeout_s,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "apigee-hybrid-mcp/0.2 (read-only)"},
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

    def _check_path(self, path: str) -> tuple[str, OrgConfig | None]:
        """Validate a resource path; returns (canonical path, org config)."""
        path = path.strip().lstrip("/")
        if path.startswith("v1/"):
            path = path[3:]
        if "://" in path or "?" in path or "#" in path or "\\" in path or "%" in path:
            raise ForbiddenOperation("Path must be a bare resource path; pass query params separately.")
        parts = path.split("/")
        if any(p in {"", ".", ".."} for p in parts):
            raise ForbiddenOperation(f"Malformed path: {path!r}")
        if parts == ["organizations"]:
            return path, None  # list orgs visible to the default credential
        if parts[0] != "organizations" or len(parts) < 2:
            raise ForbiddenOperation("Path must start with organizations/{org}.")
        org_part, sep, verb = parts[1].partition(":")
        cfg = self.org_config(org_part)  # raises if unknown; also maps aliases
        parts[1] = cfg.name + sep + verb
        if verb and len(parts) > 2:
            raise ForbiddenOperation(f"Malformed path: {path!r}")
        for p in parts[2:]:
            if ":" in p:
                raise ForbiddenOperation(f"Custom methods are not allowed here: {p!r}")
        return "/".join(parts), cfg

    # ---- requests ------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        path, cfg = self._check_path(path)
        last = path.split("/")[-1]
        if ":" in last and last[last.index(":"):] not in _READ_ONLY_GET_VERBS:
            raise ForbiddenOperation(f"Only {sorted(_READ_ONLY_GET_VERBS)} are allowed as GET custom methods.")
        return await self._send("GET", path, cfg, params=params)

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
        if method not in {"GET", "POST"}:
            raise ForbiddenOperation(f"Method {method} is not allowed.")
        if cfg is None:  # bare "organizations" listing
            base, cred, allow_kvm = self.registry.default_api_base, self.registry.default_cred, False
        else:
            base, cred, allow_kvm = cfg.api_base, cfg.cred, cfg.allow_kvm_values
        token = await self.provider_for(cred).token()
        clean_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        resp = await self._http.request(
            method,
            f"{base}/{path}",
            params=clean_params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code >= 400:
            msg = resp.text
            try:
                msg = resp.json().get("error", {}).get("message", msg)
            except Exception:
                pass
            raise ApigeeError(resp.status_code, msg[:2000], "/" + path)
        if not resp.content:
            return {}
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            data = resp.json()
            if "/keyvaluemaps/" in path and "/entries" in path and not allow_kvm:
                data = _mask_kvm_values(data)
            return redact(data) if self.registry.redact_secrets else data
        # Non-JSON (e.g. a resource file body). Return as text, truncated.
        return {"contentType": ctype, "body": resp.text[:MAX_RESPONSE_CHARS]}

    # ---- fan-out -------------------------------------------------------

    async def for_each_org(
        self, orgs: list[OrgConfig], fn: Callable[[OrgConfig], Awaitable[Any]]
    ) -> dict[str, Any]:
        """Run fn per org with bounded concurrency; per-org failures become {"error": ...}."""
        sem = asyncio.Semaphore(self.registry.max_parallel)

        async def one(cfg: OrgConfig) -> tuple[str, Any]:
            async with sem:
                try:
                    return cfg.name, await fn(cfg)
                except TOOL_ERRORS as e:
                    return cfg.name, {"error": str(e)}

        results = await asyncio.gather(*(one(o) for o in orgs))
        return dict(results)


def _mask_kvm_values(data: Any) -> Any:
    """KVM entries often hold credentials; keep names, hide values."""
    hint = "<hidden: set allow_kvm_values for this tier/org to show>"
    if isinstance(data, dict):
        if "keyValueEntries" in data:
            return {**data, "keyValueEntries": [{**e, "value": hint} for e in data["keyValueEntries"]]}
        if "value" in data and "name" in data:
            return {**data, "value": hint}
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
