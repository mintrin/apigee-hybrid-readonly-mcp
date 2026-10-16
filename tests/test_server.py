"""Tests against a fake Apigee management API (no network, no credentials).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import unittest

import httpx2 as httpx

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from apigee_mcp import server
from apigee_mcp.client import ApigeeClient, ForbiddenOperation
from apigee_mcp.config import OrgConfig, Registry
from apigee_mcp.redact import redact

ORG = "acme-hybrid"
NOW_MS = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
DAY_MS = 86_400_000

FAKE: dict[tuple[str, str], object] = {
    ("GET", f"/v1/organizations/{ORG}"): {"name": ORG, "runtimeType": "HYBRID", "environments": ["dev", "prod"]},
    ("GET", f"/v1/organizations/{ORG}/environments"): ["dev", "prod"],
    ("POST", f"/v1/organizations/{ORG}:getSyncAuthorization"): {
        "identities": ["serviceAccount:apigee-synchronizer@acme.iam.gserviceaccount.com"]
    },
    ("GET", f"/v1/organizations/{ORG}/environments/prod/deployments"): {
        "deployments": [{"environment": "prod", "apiProxy": "orders", "revision": "7", "state": "READY"}]
    },
    ("GET", f"/v1/organizations/{ORG}/environments/prod/apis/orders/revisions/7/deployments"): {
        "state": "PROGRESSING",
        "instances": [{"instance": "apigee-runtime-prod-abc", "deployedRevisions": [{"revision": "6", "percentage": 50}]}],
    },
    ("GET", f"/v1/organizations/{ORG}/apps/app-123"): {
        "appId": "app-123",
        "name": "mobile",
        "credentials": [{"consumerKey": "AbCdEfGhIjKlMnOp1234", "consumerSecret": "SuperSecretValue9876", "status": "approved"}],
    },
    ("GET", f"/v1/organizations/{ORG}/environments/prod/keyvaluemaps/cfg/entries"): {
        "keyValueEntries": [{"name": "db_password", "value": "hunter2"}]
    },
    ("GET", f"/v1/organizations/{ORG}/environments/prod/keystores"): ["ks-tls"],
    ("GET", f"/v1/organizations/{ORG}/environments/prod/keystores/ks-tls"): {"name": "ks-tls", "aliases": ["old", "new"]},
    ("GET", f"/v1/organizations/{ORG}/environments/prod/keystores/ks-tls/aliases/old"): {
        "certsInfo": {"certInfo": [{"subject": "CN=api.acme.com", "issuer": "CN=CA", "expiryDate": str(NOW_MS + 10 * DAY_MS)}]}
    },
    ("GET", f"/v1/organizations/{ORG}/environments/prod/keystores/ks-tls/aliases/new"): {
        "certsInfo": {"certInfo": [{"subject": "CN=api.acme.com", "issuer": "CN=CA", "expiryDate": str(NOW_MS + 300 * DAY_MS)}]}
    },
    ("GET", f"/v1/organizations/{ORG}/environments/prod/stats/apiproxy"): {"environments": [{"name": "prod", "dimensions": []}]},
}


logging.getLogger("apigee_mcp").setLevel(logging.ERROR)  # keep test output readable


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = FAKE.get((request.method, request.url.path))
        if body is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"not found: {request.url.path}"}})
        return httpx.Response(200, json=body)


class FakeTokens:
    description = "fake"

    async def token(self) -> str:
        return "test-token"


def call(name: str, args: dict | None = None) -> str:
    """Invoke a tool like an MCP client would. Expected failures (ToolError -> isError=true)
    come back as 'Error: ...'; a crash (UnexpectedToolError) fails the test."""
    try:
        result = asyncio.run(server.mcp.call_tool(name, args or {}))
    except UnexpectedToolError:
        raise
    except ToolError as e:
        return f"Error: {e}"
    return "".join(getattr(c, "text", "") for c in result.content)


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        reg = Registry([OrgConfig(ORG, api_base="https://apigee.test/v1")], default_org=ORG)
        server.set_client(ApigeeClient(reg, lambda cred: FakeTokens(), transport=httpx.MockTransport(self.rec)))

    def tearDown(self) -> None:
        # Every request this suite made must be GET, except the allowlisted read-only POST.
        for r in self.rec.requests:
            ok = r.method == "GET" or (r.method == "POST" and r.url.path.endswith(":getSyncAuthorization"))
            self.assertTrue(ok, f"non-read request sent: {r.method} {r.url}")

    def test_lists_environments_with_bearer_token(self):
        out = call("list_environments")
        self.assertEqual(json.loads(out), ["dev", "prod"])
        self.assertEqual(self.rec.requests[0].headers["authorization"], "Bearer test-token")

    def test_sync_authorization_is_the_only_post(self):
        out = call("get_sync_authorization")
        self.assertIn("apigee-synchronizer", out)
        self.assertEqual(self.rec.requests[0].method, "POST")

    def test_deployment_routing(self):
        self.assertIn('"orders"', call("list_deployments", {"env": "prod"}))
        out = call("get_deployment_status", {"env": "prod", "api": "orders", "revision": "7"})
        self.assertIn("PROGRESSING", out)

    def test_app_credentials_are_masked(self):
        out = call("get_app", {"app_id": "app-123"})
        self.assertNotIn("SuperSecretValue9876", out)
        self.assertNotIn("AbCdEfGhIjKlMnOp1234", out)
        self.assertIn("****9876", out)
        self.assertIn('"approved"', out)

    def test_kvm_values_hidden(self):
        out = call("list_kvm_entries", {"kvm": "cfg", "env": "prod"})
        self.assertIn("db_password", out)
        self.assertNotIn("hunter2", out)

    def test_certificate_expiry(self):
        data = json.loads(call("check_certificate_expiry", {"env": "prod", "warn_days": 30}))
        self.assertEqual(data["flagged"], 1)
        self.assertEqual(data["certificates"][0]["alias"], "old")
        self.assertEqual(data["certificates"][0]["status"], "EXPIRING")

    def test_stats_defaults_time_range(self):
        call("query_stats", {"env": "prod", "dimensions": "apiproxy"})
        q = self.rec.requests[0].url.params
        self.assertIn("~", q["timeRange"])
        self.assertEqual(q["select"], "sum(message_count)")

    def test_path_traversal_rejected(self):
        out = call("get_environment", {"env": "../../projects"})
        self.assertTrue(out.startswith("Error"), out)
        self.assertEqual(self.rec.requests, [])

    def test_other_org_rejected(self):
        out = call("list_environments", {"org": "someone-else"})
        self.assertIn("is not configured", out)
        out = call("apigee_get", {"path": "organizations/someone-else/environments"})
        self.assertIn("is not configured", out)
        self.assertEqual(self.rec.requests, [])

    def test_generic_get_cannot_reach_custom_methods_or_inject_query(self):
        for bad in [
            f"organizations/{ORG}/environments/prod/apis/orders/revisions/7/deployments:generateDeployChangeReport",
            f"organizations/{ORG}:setSyncAuthorization",
            f"organizations/{ORG}/apis?action=delete",
            "projects/acme/locations",
            f"https://evil.example/organizations/{ORG}",
            f"organizations/{ORG}/../../x",
        ]:
            out = call("apigee_get", {"path": bad})
            self.assertTrue(out.startswith("Error"), f"{bad} -> {out}")
        self.assertEqual(self.rec.requests, [])

    def test_generic_get_masks_kvm_values_too(self):
        out = call("apigee_get", {"path": f"organizations/{ORG}/environments/prod/keyvaluemaps/cfg/entries"})
        self.assertNotIn("hunter2", out)

    def test_api_errors_surface_cleanly(self):
        out = call("get_api_proxy", {"api": "missing"})
        self.assertTrue(out.startswith("Error"), out)
        self.assertIn("HTTP 404 on /organizations/acme-hybrid/apis/missing", out)

    def test_all_tools_are_annotated_read_only(self):
        tools = asyncio.run(server.mcp.list_tools())
        self.assertGreater(len(tools), 25)
        for t in tools:
            self.assertTrue(t.annotations.read_only_hint, t.name)
            self.assertFalse(t.annotations.destructive_hint, t.name)


class ClientGuardTests(unittest.TestCase):
    def test_client_refuses_non_allowlisted_post(self):
        reg = Registry([OrgConfig(ORG)], default_org=ORG)
        c = ApigeeClient(reg, lambda cred: FakeTokens(), transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        with self.assertRaises(ForbiddenOperation):
            asyncio.run(c.read_post(f"organizations/{ORG}:setSyncAuthorization"))
        with self.assertRaises(ForbiddenOperation):
            asyncio.run(c._send("DELETE", f"organizations/{ORG}/apis/x", reg.resolve(ORG)))

    def test_redaction_keeps_metadata(self):
        out = redact({"name": "ks", "keyAliasReference": "x", "password": "p4ssw0rd!!", "nested": [{"clientSecret": "abc"}]})
        self.assertEqual(out["name"], "ks")
        self.assertEqual(out["keyAliasReference"], "x")
        self.assertEqual(out["password"], "****rd!!")
        self.assertEqual(out["nested"][0]["clientSecret"], "****")


if __name__ == "__main__":
    unittest.main()
