"""Multi-org / multi-tier behaviour: config loading, aliases, per-tier credentials, fan-out."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import logging
import unittest
from unittest import mock

import httpx2 as httpx

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from apigee_hybrid_readonly_mcp import server
from apigee_hybrid_readonly_mcp.client import ApigeeClient
from apigee_hybrid_readonly_mcp.config import ConfigError, CredSpec, Registry, UnknownOrg

ROOT = pathlib.Path(__file__).resolve().parents[1]

CONFIG = {
    "default_org": "sbx-1",
    "tiers": {
        "sandbox": {"impersonate_service_account": "reader@sbx.iam.gserviceaccount.com", "orgs": ["sbx-1", "sbx-2"]},
        "nonprod": {"impersonate_service_account": "reader@np.iam.gserviceaccount.com", "orgs": ["dev-us", "qa-us"]},
        "preprod": {"impersonate_service_account": "reader@pp.iam.gserviceaccount.com", "orgs": ["pp-us"]},
        "prod": {"impersonate_service_account": "reader@prod.iam.gserviceaccount.com", "orgs": ["prod-us", "prod-eu"]},
    },
    "orgs": {
        "prod-us": {"aliases": ["usp"], "description": "US prod"},
        "prod-eu": {"api_base": "https://eu.apigee.test/v1"},
    },
}

# Which proxy revision is deployed in which org (prod-eu doesn't have it; qa-us is denied)
DEPLOYED = {"sbx-1": "12", "sbx-2": "12", "dev-us": "11", "pp-us": "10", "prod-us": "9"}


logging.getLogger("apigee_hybrid_readonly_mcp").setLevel(logging.ERROR)  # keep test output readable


class TierTokens:
    """One fake provider per CredSpec; the token names the SA so routing is checkable."""

    built: list[CredSpec] = []

    def __init__(self, cred: CredSpec) -> None:
        TierTokens.built.append(cred)
        self.description = cred.describe()
        self._tok = "tok-" + (cred.impersonate_service_account or "default").split("@")[1].split(".")[0]

    async def token(self) -> str:
        return self._tok


def handler(seen: list[httpx.Request]):
    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        parts = req.url.path.split("/")  # ['', 'v1', 'organizations', org, ...]
        org = parts[3]
        if org == "qa-us":
            return httpx.Response(403, json={"error": {"message": "Permission denied on qa-us"}})
        if parts[4:] == ["apis", "orders", "deployments"]:
            if org not in DEPLOYED:
                return httpx.Response(404, json={"error": {"message": "proxy not found"}})
            return httpx.Response(
                200, json={"deployments": [{"environment": "prod" if "prod" in org else "dev", "apiProxy": "orders", "revision": DEPLOYED[org]}]}
            )
        if parts[4:] == ["environments"]:
            return httpx.Response(200, json=["dev", "test"])
        if len(parts) == 4:
            return httpx.Response(200, json={"name": org, "runtimeType": "HYBRID"})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    return h


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


class MultiOrgTests(unittest.TestCase):
    def setUp(self) -> None:
        TierTokens.built = []
        self.seen: list[httpx.Request] = []
        self.reg = Registry.from_dict(CONFIG)
        server.set_client(ApigeeClient(self.reg, TierTokens, transport=httpx.MockTransport(handler(self.seen))))

    def tearDown(self) -> None:
        for r in self.seen:
            self.assertEqual(r.method, "GET")

    def test_config_inheritance(self):
        self.assertEqual(len(self.reg.orgs), 7)
        # promotion order = order of definition in the config, not alphabetical
        self.assertEqual(self.reg.tiers, ["sandbox", "nonprod", "preprod", "prod"])
        self.assertEqual(self.reg.resolve("pp-us").cred.impersonate_service_account, "reader@pp.iam.gserviceaccount.com")
        self.assertEqual(self.reg.resolve("usp").name, "prod-us")
        self.assertEqual(self.reg.resolve("prod-us").cred.impersonate_service_account, "reader@prod.iam.gserviceaccount.com")
        self.assertEqual(self.reg.resolve("prod-eu").api_base, "https://eu.apigee.test/v1")

    def test_each_tier_uses_its_own_credential_and_base(self):
        call("get_organization", {"org": "sbx-2"})
        call("get_organization", {"org": "usp"})
        call("get_organization", {"org": "prod-eu"})
        auth = [(r.url.host, r.headers["authorization"]) for r in self.seen]
        self.assertEqual(
            auth,
            [("apigee.googleapis.com", "Bearer tok-sbx"), ("apigee.googleapis.com", "Bearer tok-prod"), ("eu.apigee.test", "Bearer tok-prod")],
        )
        self.assertEqual(len(TierTokens.built), 2)  # providers are cached per credential, not per org

    def test_alias_in_generic_path_is_canonicalised(self):
        call("apigee_get", {"path": "organizations/usp/environments"})
        self.assertEqual(self.seen[0].url.path, "/v1/organizations/prod-us/environments")

    def test_unknown_org_rejected_without_request(self):
        out = call("list_environments", {"org": "prod-mars"})
        self.assertIn("is not configured", out)
        self.assertEqual(self.seen, [])

    def test_list_configured_orgs(self):
        data = json.loads(call("list_configured_orgs", {"tier": "prod"}))
        self.assertEqual([o["name"] for o in data["orgs"]], ["prod-eu", "prod-us"])
        self.assertEqual(data["orgs"][1]["aliases"], ["usp"])
        self.assertIn("impersonate reader@prod", data["orgs"][0]["credential"])

    def test_across_orgs_by_tier_reports_partial_failures(self):
        data = json.loads(call("across_orgs", {"resource": "environments", "tier": "nonprod"}))
        self.assertEqual(data["results"]["dev-us"], ["dev", "test"])
        self.assertIn("Permission denied", data["results"]["qa-us"]["error"])

    def test_across_orgs_explicit_list_with_alias(self):
        data = json.loads(call("across_orgs", {"resource": "environments", "orgs": ["usp", "sbx-1"]}))
        self.assertEqual(sorted(data["results"]), ["prod-us", "sbx-1"])

    def test_find_api_deployments_shows_drift(self):
        data = json.loads(call("find_api_deployments", {"api": "orders"}))
        # revisions reported in promotion order, showing each tier one revision behind the last
        self.assertEqual(
            list(data["revisionsByTier"].items()),
            [("sandbox", ["12"]), ("nonprod", ["11"]), ("preprod", ["10"]), ("prod", ["9"])],
        )
        self.assertEqual([r["tier"] for r in data["deployments"]], ["sandbox", "sandbox", "nonprod", "preprod", "prod"])
        self.assertEqual(data["notFound"], ["prod-eu"])
        self.assertIn("qa-us", data["errors"])

    def test_preprod_tier_selection(self):
        data = json.loads(call("find_api_deployments", {"api": "orders", "tier": "preprod"}))
        self.assertEqual(data["revisionsByTier"], {"preprod": ["10"]})
        listing = json.loads(call("list_configured_orgs"))
        self.assertEqual(listing["tiers"], ["sandbox", "nonprod", "preprod", "prod"])
        self.assertEqual([o["tier"] for o in listing["orgs"]][:5], ["sandbox", "sandbox", "nonprod", "nonprod", "preprod"])

    def test_example_config_file_loads(self):
        with mock.patch.dict(os.environ, {"APIGEE_CONFIG": str(ROOT / "apigee-orgs.example.toml")}, clear=False):
            reg = Registry.load()
        self.assertEqual(len(reg.orgs), 20)
        self.assertEqual(reg.tiers, ["sandbox", "nonprod", "preprod", "prod"])
        self.assertEqual(reg.resolve("stage-us").name, "acme-preprod-us")
        self.assertFalse(reg.resolve("acme-preprod-eu").allow_kvm_values)
        self.assertEqual(reg.resolve("partner").cred.impersonate_service_account,
                         "apigee-mcp-reader@acme-partner-prod.iam.gserviceaccount.com")
        self.assertTrue(reg.resolve("acme-sbx-02").allow_kvm_values)

    def test_apigee_tiers_restricts_instance(self):
        env = {"APIGEE_CONFIG": str(ROOT / "apigee-orgs.example.toml"), "APIGEE_TIERS": "sandbox,nonprod"}
        with mock.patch.dict(os.environ, env, clear=False):
            reg = Registry.load()
        self.assertEqual(reg.tiers, ["sandbox", "nonprod"])
        for hidden in ("acme-prod-us-west", "acme-preprod-us", "stage-us"):
            with self.assertRaises(UnknownOrg):
                reg.resolve(hidden)

        env["APIGEE_TIERS"] = "preprod,prod"
        with mock.patch.dict(os.environ, env, clear=False):
            reg = Registry.load()
        self.assertEqual(reg.tiers, ["preprod", "prod"])
        self.assertEqual(len(reg.orgs), 8)
        self.assertEqual(reg.default_org, None)  # default (sandbox) org is outside this instance
        self.assertEqual(reg.resolve("stage-us").name, "acme-preprod-us")
        with self.assertRaises(UnknownOrg):
            reg.resolve("acme-sbx-01")

    def test_config_errors(self):
        with self.assertRaises(ConfigError):
            Registry.from_dict({"orgs": {"a": {"tier": "missing"}}})
        with self.assertRaises(ConfigError):
            Registry.from_dict({"orgs": {"a": {"aliases": ["x"]}, "b": {"aliases": ["x"]}}})
        with self.assertRaises(ConfigError):
            Registry.from_dict({"tiers": {"prod": {"orgs": ["a"]}}, "orgs": {"a": {"tier": "dev"}}})


class EnvModeTests(unittest.TestCase):
    def test_open_mode_accepts_any_org(self):
        with mock.patch.dict(os.environ, {"APIGEE_ORG": "", "APIGEE_ALLOWED_ORGS": "", "APIGEE_CONFIG": ""}, clear=False):
            reg = Registry.load()
        self.assertTrue(reg.open_mode)
        self.assertEqual(reg.resolve("whatever-org").name, "whatever-org")

    def test_allowlist_mode(self):
        with mock.patch.dict(os.environ, {"APIGEE_ORG": "a", "APIGEE_ALLOWED_ORGS": "a,b", "APIGEE_CONFIG": ""}, clear=False):
            reg = Registry.load()
        self.assertFalse(reg.open_mode)
        self.assertEqual(sorted(reg.orgs), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
