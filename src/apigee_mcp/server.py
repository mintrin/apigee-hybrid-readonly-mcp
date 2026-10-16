"""MCP server exposing read-only tools over the Apigee Hybrid control plane."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import functools
import logging
import os
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from .client import TOOL_ERRORS, ApigeeClient, seg, to_text
from .config import Registry

log = logging.getLogger("apigee_mcp")

INSTRUCTIONS = """\
Read-only access to the Apigee Hybrid management plane (apigee.googleapis.com).
Nothing here can create, change, deploy or delete anything.

Multiple orgs: call list_configured_orgs first to see org names, aliases and tiers
(e.g. sandbox / nonprod / prod). Every tool's `org` accepts a name or alias; omit it
to use the default org. For questions spanning orgs, use find_api_deployments
(where is proxy X deployed, at which revision, across a tier) or across_orgs
(the same GET against every org in a tier).

Typical starting points: list_environments -> list_deployments -> get_deployment_status
for "what is running where"; get_organization / get_sync_authorization for hybrid
runtime wiring; check_certificate_expiry for TLS housekeeping; query_stats for traffic.
Credential-like fields (consumer secrets, passwords, KVM values) are masked.
"""

mcp = MCPServer(name="apigee-hybrid", instructions=INSTRUCTIONS, version="0.2.0")

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

_client: ApigeeClient | None = None


def client() -> ApigeeClient:
    global _client
    if _client is None:
        _client = ApigeeClient(Registry.load())
    return _client


def set_client(c: ApigeeClient) -> None:
    """Inject a client (used by tests)."""
    global _client
    _client = c


async def _get(path: str, params: dict[str, Any] | None = None) -> str:
    try:
        return to_text(await client().get(path, params))
    except TOOL_ERRORS as e:
        return f"Error: {e}"


def _o(org: str | None) -> str:
    return "organizations/" + client().org(org)


def _guard(fn):
    """Turn validation errors raised while building a path into tool errors."""

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except TOOL_ERRORS as e:
            return f"Error: {e}"

    return wrapper


def tool(fn):
    return mcp.tool(annotations=RO)(_guard(fn))


# ---------------------------------------------------------------- organization


@tool
async def list_organizations() -> str:
    """List the Apigee organizations the configured credentials can see."""
    return await _get("organizations")


@tool
async def get_organization(org: str | None = None) -> str:
    """Org details: runtime type (HYBRID), analytics region, properties, environments, billing type."""
    return await _get(_o(org))


@tool
async def get_project_mapping(org: str | None = None) -> str:
    """GCP project / location mapping for the org."""
    return await _get(_o(org) + ":getProjectMapping")


@tool
async def get_sync_authorization(org: str | None = None) -> str:
    """Hybrid: service-account identities allowed to run the synchronizer (pull runtime contracts)."""
    try:
        return to_text(await client().read_post(_o(org) + ":getSyncAuthorization"))
    except TOOL_ERRORS as e:
        return f"Error: {e}"


@tool
async def list_operations(org: str | None = None, filter: str | None = None, page_size: int = 50) -> str:
    """Long-running operations in the org (env creation, attachments, etc.), most useful to see stuck/failed ones."""
    return await _get(_o(org) + "/operations", {"filter": filter, "pageSize": page_size})


# ---------------------------------------------------------------- environments


@tool
async def list_environments(org: str | None = None) -> str:
    """Names of all environments in the org."""
    return await _get(_o(org) + "/environments")


@tool
async def get_environment(env: str, org: str | None = None) -> str:
    """Environment config: type, deployment type, properties, node config, forward proxy."""
    return await _get(f"{_o(org)}/environments/{seg(env, 'env')}")


@tool
async def get_deployed_config(env: str, org: str | None = None) -> str:
    """Hybrid: the runtime config revision currently computed for the environment (what synchronizer pulls)."""
    return await _get(f"{_o(org)}/environments/{seg(env, 'env')}/deployedConfig")


@tool
async def list_envgroups(org: str | None = None) -> str:
    """Environment groups with their hostnames."""
    return await _get(_o(org) + "/envgroups")


@tool
async def get_envgroup(envgroup: str, org: str | None = None) -> str:
    """One environment group, its hostnames and which environments are attached to it."""
    base = f"{_o(org)}/envgroups/{seg(envgroup, 'envgroup')}"
    group, attachments = await asyncio.gather(client().get(base), client().get(base + "/attachments"))
    return to_text({"envgroup": group, "attachments": attachments.get("environmentGroupAttachments", [])})


# ---------------------------------------------------------------- proxies & shared flows


@tool
async def list_api_proxies(org: str | None = None, include_revisions: bool = False) -> str:
    """All API proxies in the org (optionally with their revision numbers)."""
    return await _get(_o(org) + "/apis", {"includeRevisions": str(include_revisions).lower()})


@tool
async def get_api_proxy(api: str, org: str | None = None) -> str:
    """Proxy metadata: revisions, latest revision, labels, read-only flag."""
    return await _get(f"{_o(org)}/apis/{seg(api, 'api')}")


@tool
async def get_api_proxy_revision(api: str, revision: str, org: str | None = None) -> str:
    """One proxy revision: policies, proxy endpoints, target endpoints, resource files, created/modified."""
    return await _get(f"{_o(org)}/apis/{seg(api, 'api')}/revisions/{seg(revision, 'revision')}")


@tool
async def list_sharedflows(org: str | None = None, include_revisions: bool = False) -> str:
    """All shared flows in the org."""
    return await _get(_o(org) + "/sharedflows", {"includeRevisions": str(include_revisions).lower()})


@tool
async def get_sharedflow(sharedflow: str, org: str | None = None) -> str:
    """Shared flow metadata and revisions."""
    return await _get(f"{_o(org)}/sharedflows/{seg(sharedflow, 'sharedflow')}")


@tool
async def list_deployments(
    org: str | None = None,
    env: str | None = None,
    api: str | None = None,
    sharedflows: bool = False,
) -> str:
    """What is deployed where.

    - no env/api: every deployment in the org
    - env only: everything deployed to that environment
    - api only: every environment that proxy (or shared flow, if sharedflows=true) is deployed to
    - env + api: that proxy in that environment
    """
    o = _o(org)
    kind = "sharedflows" if sharedflows else "apis"
    if env and api:
        path = f"{o}/environments/{seg(env, 'env')}/{kind}/{seg(api, 'api')}/deployments"
    elif env:
        path = f"{o}/environments/{seg(env, 'env')}/deployments"
    elif api:
        path = f"{o}/{kind}/{seg(api, 'api')}/deployments"
    else:
        path = f"{o}/deployments"
    params = {"sharedFlows": "true"} if sharedflows and not api else None
    return await _get(path, params)


@tool
async def get_deployment_status(env: str, api: str, revision: str, org: str | None = None, sharedflow: bool = False) -> str:
    """Deployment state of one revision in one env, incl. per-pod status, route conflicts and errors (hybrid rollout debugging)."""
    kind = "sharedflows" if sharedflow else "apis"
    return await _get(
        f"{_o(org)}/environments/{seg(env, 'env')}/{kind}/{seg(api, 'api')}"
        f"/revisions/{seg(revision, 'revision')}/deployments"
    )


# ---------------------------------------------------------------- environment config


EnvResource = Literal["targetservers", "keystores", "references", "resourcefiles", "flowhooks", "caches", "keyvaluemaps"]


@tool
async def list_env_resources(env: str, resource: EnvResource, org: str | None = None) -> str:
    """List an environment-scoped config collection: targetservers, keystores, references,
    resourcefiles, flowhooks, caches or keyvaluemaps."""
    return await _get(f"{_o(org)}/environments/{seg(env, 'env')}/{resource}")


@tool
async def get_target_server(env: str, name: str, org: str | None = None) -> str:
    """Target server host, port, protocol, enabled flag and TLS (sslInfo) settings."""
    return await _get(f"{_o(org)}/environments/{seg(env, 'env')}/targetservers/{seg(name)}")


@tool
async def get_keystore(env: str, keystore: str, org: str | None = None, alias: str | None = None) -> str:
    """A keystore/truststore and its aliases; with alias, the alias's certificate details (subject, issuer, validity)."""
    base = f"{_o(org)}/environments/{seg(env, 'env')}/keystores/{seg(keystore, 'keystore')}"
    if alias:
        return await _get(f"{base}/aliases/{seg(alias, 'alias')}")
    return await _get(base)


@tool
async def get_flowhook(env: str, flowhook: Literal["PreProxyFlowHook", "PostProxyFlowHook", "PreTargetFlowHook", "PostTargetFlowHook"], org: str | None = None) -> str:
    """Which shared flow is attached to a flow hook in an environment."""
    return await _get(f"{_o(org)}/environments/{seg(env, 'env')}/flowhooks/{flowhook}")


@tool
async def check_certificate_expiry(env: str, org: str | None = None, warn_days: int = 30) -> str:
    """Scan every keystore/truststore alias in an environment and report certificates that are expired
    or expire within `warn_days`. Returns all certs sorted by expiry."""
    c = client()
    base = f"{_o(org)}/environments/{seg(env, 'env')}/keystores"
    keystores = await c.get(base)
    names = keystores if isinstance(keystores, list) else keystores.get("keystores", [])
    sem = asyncio.Semaphore(8)
    now = dt.datetime.now(dt.timezone.utc)
    rows: list[dict[str, Any]] = []

    async def one(ks: str, alias: str) -> None:
        async with sem:
            info = await c.get(f"{base}/{seg(ks)}/aliases/{seg(alias)}")
        for cert in (info.get("certsInfo") or {}).get("certInfo", []):
            exp_ms = int(cert.get("expiryDate", 0))
            exp = dt.datetime.fromtimestamp(exp_ms / 1000, dt.timezone.utc) if exp_ms else None
            days = (exp - now).days if exp else None
            rows.append(
                {
                    "keystore": ks,
                    "alias": alias,
                    "subject": cert.get("subject"),
                    "issuer": cert.get("issuer"),
                    "expires": exp.isoformat() if exp else None,
                    "daysLeft": days,
                    "status": "EXPIRED" if days is not None and days < 0
                    else "EXPIRING" if days is not None and days <= warn_days
                    else "OK",
                }
            )

    tasks = []
    for ks in names:
        ks_detail = await c.get(f"{base}/{seg(ks)}")
        tasks += [one(ks, a) for a in ks_detail.get("aliases", [])]
    await asyncio.gather(*tasks)
    rows.sort(key=lambda r: (r["daysLeft"] is None, r["daysLeft"] if r["daysLeft"] is not None else 0))
    flagged = [r for r in rows if r["status"] != "OK"]
    return to_text({"environment": env, "warnDays": warn_days, "flagged": len(flagged), "certificates": rows})


# ---------------------------------------------------------------- key value maps


@tool
async def list_kvms(scope: Literal["org", "env", "proxy"] = "env", env: str | None = None, api: str | None = None, org: str | None = None) -> str:
    """List KVM names at org, environment (needs env) or proxy (needs api) scope."""
    o = _o(org)
    if scope == "env":
        if not env:
            return "Error: env is required for scope=env"
        return await _get(f"{o}/environments/{seg(env, 'env')}/keyvaluemaps")
    if scope == "proxy":
        if not api:
            return "Error: api is required for scope=proxy"
        return await _get(f"{o}/apis/{seg(api, 'api')}/keyvaluemaps")
    return await _get(f"{o}/keyvaluemaps")


@tool
async def list_kvm_entries(
    kvm: str,
    scope: Literal["org", "env", "proxy"] = "env",
    env: str | None = None,
    api: str | None = None,
    org: str | None = None,
    page_size: int = 100,
    page_token: str | None = None,
) -> str:
    """Entry keys in a KVM. Values are hidden unless the server runs with APIGEE_ALLOW_KVM_VALUES=true."""
    o = _o(org)
    if scope == "env":
        if not env:
            return "Error: env is required for scope=env"
        base = f"{o}/environments/{seg(env, 'env')}"
    elif scope == "proxy":
        if not api:
            return "Error: api is required for scope=proxy"
        base = f"{o}/apis/{seg(api, 'api')}"
    else:
        base = o
    return await _get(f"{base}/keyvaluemaps/{seg(kvm, 'kvm')}/entries", {"pageSize": page_size, "pageToken": page_token})


# ---------------------------------------------------------------- products, developers, apps


@tool
async def list_api_products(org: str | None = None, expand: bool = False, count: int = 100, start_key: str | None = None) -> str:
    """API products (names only unless expand=true). Page with count/start_key."""
    return await _get(_o(org) + "/apiproducts", {"expand": str(expand).lower(), "count": count, "startKey": start_key})


@tool
async def get_api_product(product: str, org: str | None = None) -> str:
    """API product: proxies, environments, operation groups, quota, scopes, approval type."""
    return await _get(f"{_o(org)}/apiproducts/{seg(product, 'product')}")


@tool
async def list_developers(org: str | None = None, expand: bool = False, count: int = 100, start_key: str | None = None) -> str:
    """Developers (emails, or full records with expand=true). Page with count/start_key."""
    return await _get(_o(org) + "/developers", {"expand": str(expand).lower(), "count": count, "startKey": start_key})


@tool
async def get_developer(email: str, org: str | None = None) -> str:
    """A developer and the names of their apps."""
    return await _get(f"{_o(org)}/developers/{seg(email, 'developer email')}")


@tool
async def list_apps(org: str | None = None, developer: str | None = None, expand: bool = False, rows: int = 100, start_key: str | None = None) -> str:
    """Developer apps across the org, or for one developer email. Credentials are masked."""
    if developer:
        return await _get(f"{_o(org)}/developers/{seg(developer, 'developer email')}/apps", {"expand": str(expand).lower()})
    return await _get(_o(org) + "/apps", {"expand": str(expand).lower(), "rows": rows, "startKey": start_key})


@tool
async def get_app(app_id: str, org: str | None = None) -> str:
    """A developer app by app ID: status, products per credential, callback URL, attributes (keys/secrets masked)."""
    return await _get(f"{_o(org)}/apps/{seg(app_id, 'app id')}")


# ---------------------------------------------------------------- analytics & debug


@tool
async def query_stats(
    env: str,
    dimensions: str,
    select: str = "sum(message_count)",
    time_range: str | None = None,
    time_unit: Literal["minute", "hour", "day", "week", "month"] | None = None,
    filter: str | None = None,
    sort_by: str | None = None,
    limit: int = 50,
    org: str | None = None,
) -> str:
    """Analytics stats for an environment (Apigee stats API).

    dimensions: e.g. "apiproxy", "apiproxy,response_status_code", "developer_app".
    select: metrics, e.g. "sum(message_count),avg(total_response_time),sum(is_error)".
    time_range: "MM/DD/YYYY HH:MM~MM/DD/YYYY HH:MM" in UTC; defaults to the last 24 hours.
    filter: e.g. "(response_status_code ge 500)".
    """
    if not time_range:
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(hours=24)
        time_range = f"{start:%m/%d/%Y %H:%M}~{end:%m/%d/%Y %H:%M}"
    dims = ",".join(seg(d.strip(), "dimension") for d in dimensions.split(",") if d.strip())
    return await _get(
        f"{_o(org)}/environments/{seg(env, 'env')}/stats/{dims}",
        {
            "select": select,
            "timeRange": time_range,
            "timeUnit": time_unit,
            "filter": filter,
            "sortby": sort_by,
            "sort": "DESC" if sort_by else None,
            "limit": limit,
        },
    )


@tool
async def list_debug_sessions(env: str, api: str, revision: str, org: str | None = None) -> str:
    """Existing debug (trace) sessions for a proxy revision. Read-only: this does not start a session."""
    return await _get(
        f"{_o(org)}/environments/{seg(env, 'env')}/apis/{seg(api, 'api')}/revisions/{seg(revision, 'revision')}/debugsessions"
    )


# ---------------------------------------------------------------- multi-org


@tool
async def list_configured_orgs(tier: str | None = None) -> str:
    """Orgs this server can query: name, tier (e.g. sandbox/nonprod/prod), aliases, description,
    and which credential each uses. Call this first when the user refers to an org by nickname or tier."""
    reg = client().registry
    orgs = reg.select(tier=tier) if (tier or reg.orgs) else []
    return to_text(
        {
            "source": reg.source,
            "defaultOrg": reg.default_org,
            "openMode": reg.open_mode,
            "tiers": reg.tiers,
            "orgs": [
                {
                    "name": o.name,
                    "tier": o.tier,
                    "aliases": list(o.aliases),
                    "description": o.description,
                    "credential": o.cred.describe(),
                }
                for o in sorted(orgs, key=lambda o: (o.tier, o.name))
            ],
        }
    )


@tool
async def across_orgs(
    resource: str,
    tier: str | None = None,
    orgs: list[str] | None = None,
    params: dict[str, str] | None = None,
) -> str:
    """Run the same read against many orgs at once and return {org: result}.

    `resource` is a path relative to the org, e.g. "environments", "envgroups",
    "apis", "sharedflows", "environments/prod/targetservers", "deployments".
    Pick orgs by `tier` (e.g. "prod"), by an explicit list of names/aliases, or both.
    Per-org failures (404, permission denied) are reported inline and do not stop the others.
    """
    c = client()
    rel = resource.strip().strip("/")
    if rel.startswith("organizations/"):
        return "Error: `resource` is relative to the org, e.g. 'environments' (not organizations/...)."
    selected = c.registry.select(tier=tier, orgs=orgs)
    results = await c.for_each_org(selected, lambda o: c.get(f"organizations/{o.name}/{rel}", params))
    return to_text({"resource": rel, "tier": tier, "orgCount": len(selected), "results": results})


@tool
async def find_api_deployments(
    api: str,
    tier: str | None = None,
    orgs: list[str] | None = None,
    sharedflow: bool = False,
) -> str:
    """Where is a proxy (or shared flow) deployed across orgs, and at which revision?

    Returns one row per org/environment plus a per-tier summary of revisions, which makes
    promotion drift (sandbox -> nonprod -> prod) easy to spot. Orgs where the proxy does not
    exist are listed under notFound.
    """
    c = client()
    kind = "sharedflows" if sharedflow else "apis"
    name = seg(api, "api")
    selected = c.registry.select(tier=tier, orgs=orgs)
    tiers = {o.name: o.tier for o in selected}
    raw = await c.for_each_org(selected, lambda o: c.get(f"organizations/{o.name}/{kind}/{name}/deployments"))

    rows: list[dict[str, Any]] = []
    not_found: list[str] = []
    errors: dict[str, str] = {}
    by_tier: dict[str, set[str]] = {}
    for org_name, data in raw.items():
        if isinstance(data, dict) and "error" in data:
            if "HTTP 404" in data["error"]:
                not_found.append(org_name)
            else:
                errors[org_name] = data["error"]
            continue
        deps = data.get("deployments", []) if isinstance(data, dict) else []
        if not deps:
            rows.append({"org": org_name, "tier": tiers[org_name], "environment": None, "revision": None, "note": "exists, not deployed"})
        for d in deps:
            rev = str(d.get("revision"))
            rows.append(
                {
                    "org": org_name,
                    "tier": tiers[org_name],
                    "environment": d.get("environment"),
                    "revision": rev,
                    "deployStartTime": d.get("deployStartTime"),
                }
            )
            by_tier.setdefault(tiers[org_name], set()).add(rev)
    rows.sort(key=lambda r: (r["tier"], r["org"], r["environment"] or ""))
    return to_text(
        {
            "api": api,
            "kind": kind,
            "revisionsByTier": {t: sorted(v, key=lambda x: int(x) if x.isdigit() else 0) for t, v in sorted(by_tier.items())},
            "deployments": rows,
            "notFound": sorted(not_found),
            "errors": errors,
        }
    )


# ---------------------------------------------------------------- escape hatch


@tool
async def apigee_get(path: str, params: dict[str, str] | None = None) -> str:
    """GET any Apigee management API resource not covered by the other tools.

    `path` is relative to https://apigee.googleapis.com/v1 and must start with
    organizations/{org}, e.g. "organizations/my-org/environments/prod/apis/orders/revisions/3/deployments".
    Query parameters go in `params`. Only GET is ever issued.
    """
    return await _get(path, params)


# ---------------------------------------------------------------- entry point


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only MCP server for the Apigee Hybrid control plane")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--check", action="store_true", help="verify credentials and org access, then exit")
    args = p.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr)
    for noisy in ("httpx2", "httpx", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.check:
        async def check() -> None:
            c = client()
            reg = c.registry
            print(f"config: {reg.source}  default org: {reg.default_org}  tiers: {', '.join(reg.tiers) or '-'}")
            if not reg.orgs:
                data = await c.get(f"organizations/{reg.default_org}") if reg.default_org else await c.get("organizations")
                print(to_text(data)[:2000])
                return

            async def probe(o):
                d = await c.get(f"organizations/{o.name}")
                return d.get("runtimeType", "?")

            results = await c.for_each_org(list(reg.orgs.values()), probe)
            failed = 0
            for o in sorted(reg.orgs.values(), key=lambda o: (o.tier, o.name)):
                r = results[o.name]
                ok = not (isinstance(r, dict) and "error" in r)
                failed += not ok
                status = f"OK ({r})" if ok else f"FAIL: {r['error'][:160]}"
                print(f"  [{o.tier:<10}] {o.name:<32} {o.cred.describe():<55} {status}")
            print(f"{len(reg.orgs) - failed}/{len(reg.orgs)} orgs reachable")
            if failed:
                sys.exit(1)

        asyncio.run(check())
        return

    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
