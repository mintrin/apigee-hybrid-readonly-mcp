"""Line of business (LOB): a second dimension across tiers, one org per (lob, tier)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import unittest
from contextlib import redirect_stdout

import httpx2 as httpx
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from apigee_hybrid_readonly_mcp import server
from apigee_hybrid_readonly_mcp.client import ApigeeClient
from apigee_hybrid_readonly_mcp.config import ConfigError, Registry, UnknownOrg

logging.getLogger("apigee_hybrid_readonly_mcp").setLevel(logging.CRITICAL)

CONFIG = {
    "tiers": {
        "preprod": {"impersonate_service_account": "reader@pp.iam.gserviceaccount.com",
                    "orgs": ["pay-pp", "retail-pp", "lend-pp"]},
        "prod": {"impersonate_service_account": "reader@prod.iam.gserviceaccount.com",
                 "orgs": ["pay-prod", "retail-prod", "lend-prod", "partner-prod"]},
    },
    "lobs": {
        "payments": {"description": "Card and payment APIs", "orgs": ["pay-pp", "pay-prod"]},
        "retail": {"orgs": ["retail-pp", "retail-prod"]},
        "lending": {"orgs": ["lend-pp"]},  # lend-prod sets its lob on the org instead
    },
    "orgs": {"lend-prod": {"lob": "lending"}, "pay-prod": {"aliases": ["pay"]}},
}

# revision of `checkout` deployed per org; lend-prod lacks the proxy
DEPLOYED = {"pay-pp": "14", "pay-prod": "12", "retail-pp": "7", "retail-prod": "7", "lend-pp": "3", "partner-prod": "5"}


class Tokens:
    def __init__(self, cred) -> None:
        self.description = "fake"
        self._t = "tok-" + cred.impersonate_service_account.split("@")[1].split(".")[0]

    async def token(self) -> str:
        return self._t

    def invalidate(self) -> None:
        pass


def call(name: str, args: dict | None = None):
    try:
        res = asyncio.run(server.mcp.call_tool(name, args or {}))
    except UnexpectedToolError:
        raise
    except ToolError as e:
        return str(e), True
    return "".join(getattr(c, "text", "") for c in res.content), False


class LobConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = Registry.from_dict(CONFIG)

    def test_lob_assignment_both_ways(self):
        self.assertEqual(self.reg.orgs["pay-pp"].lob, "payments")
        self.assertEqual(self.reg.orgs["lend-prod"].lob, "lending")
        self.assertIsNone(self.reg.orgs["partner-prod"].lob)  # LOB is optional
        self.assertEqual(self.reg.lobs, ["payments", "retail", "lending"])  # [lobs] table order

    def test_lob_at_tier_resolution(self):
        self.assertEqual(self.reg.resolve("payments@prod").name, "pay-prod")
        self.assertEqual(self.reg.resolve("retail@preprod").name, "retail-pp")
        with self.assertRaisesRegex(UnknownOrg, "Unknown lob"):
            self.reg.resolve("cards@prod")
        with self.assertRaisesRegex(UnknownOrg, "no org in tier 'sandbox'.*preprod, prod"):
            self.reg.resolve("payments@sandbox")

    def test_credentials_still_come_from_tier(self):
        self.assertEqual(self.reg.resolve("payments@prod").cred.impersonate_service_account, "reader@prod.iam.gserviceaccount.com")
        self.assertEqual(self.reg.resolve("payments@preprod").cred.impersonate_service_account, "reader@pp.iam.gserviceaccount.com")

    def test_select_by_lob_and_tier(self):
        self.assertEqual({o.name for o in self.reg.select(lob="payments")}, {"pay-pp", "pay-prod"})
        self.assertEqual([o.name for o in self.reg.select(lob="retail", tier="prod")], ["retail-prod"])
        self.assertEqual({o.name for o in self.reg.select(tier="prod")}, {"pay-prod", "retail-prod", "lend-prod", "partner-prod"})
        with self.assertRaisesRegex(UnknownOrg, "Unknown lob"):
            self.reg.select(lob="cards")

    def test_grid(self):
        self.assertEqual(self.reg.grid(), {
            "payments": {"preprod": "pay-pp", "prod": "pay-prod"},
            "retail": {"preprod": "retail-pp", "prod": "retail-prod"},
            "lending": {"preprod": "lend-pp", "prod": "lend-prod"},
        })

    def test_config_rules(self):
        bad = [
            # two orgs for one (lob, tier)
            {"tiers": {"prod": {"orgs": ["a", "b"]}}, "lobs": {"pay": {"orgs": ["a", "b"]}}},
            # org says one lob, lob table lists it under another
            {"tiers": {"prod": {"orgs": ["a"]}}, "lobs": {"x": {"orgs": ["a"]}, "y": {}}, "orgs": {"a": {"lob": "y"}}},
            # undefined lob on an org (typo guard)
            {"tiers": {"prod": {"orgs": ["a"]}}, "orgs": {"a": {"lob": "paymnets"}}},
            # lob table lists an org nothing defines
            {"tiers": {"prod": {"orgs": ["a"]}}, "lobs": {"x": {"orgs": ["ghost"]}}},
            # unknown key in a lob table (LOBs carry no credentials)
            {"tiers": {"prod": {"orgs": ["a"]}}, "lobs": {"x": {"orgs": ["a"], "impersonate_service_account": "s@p.iam"}}},
            # bad lob name
            {"tiers": {"prod": {"orgs": ["a"]}}, "lobs": {"bad name": {"orgs": ["a"]}}},
        ]
        for doc in bad:
            with self.assertRaises(ConfigError, msg=doc):
                Registry.from_dict(doc)


class LobToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: list[httpx.Request] = []

        def h(req: httpx.Request) -> httpx.Response:
            self.seen.append(req)
            parts = req.url.path.split("/")
            org = parts[3]
            if parts[4:] == ["apis", "checkout", "deployments"]:
                if org not in DEPLOYED:
                    return httpx.Response(404, json={"error": {"message": "not found"}})
                return httpx.Response(200, json={"deployments": [{"environment": "prod", "revision": DEPLOYED[org]}]})
            if parts[4:] == ["environments"]:
                return httpx.Response(200, json=["prod"])
            return httpx.Response(200, json={"name": org})

        server.set_client(ApigeeClient(Registry.from_dict(CONFIG), Tokens, transport=httpx.MockTransport(h)))

    def tearDown(self) -> None:
        for r in self.seen:
            self.assertEqual(r.method, "GET")

    def test_org_argument_accepts_lob_at_tier(self):
        text, err = call("list_environments", {"org": "payments@prod"})
        self.assertFalse(err, text)
        self.assertEqual(self.seen[0].url.path, "/v1/organizations/pay-prod/environments")
        self.assertEqual(self.seen[0].headers["authorization"], "Bearer tok-prod")
        call("apigee_get", {"path": "organizations/retail@preprod/environments"})
        self.assertEqual(self.seen[1].url.path, "/v1/organizations/retail-pp/environments")

    def test_across_orgs_by_lob(self):
        data = json.loads(call("across_orgs", {"resource": "environments", "lob": "payments"})[0])
        self.assertEqual(sorted(data["results"]), ["pay-pp", "pay-prod"])
        data = json.loads(call("across_orgs", {"resource": "environments", "lob": "retail", "tier": "prod"})[0])
        self.assertEqual(list(data["results"]), ["retail-prod"])

    def test_find_api_deployments_per_lob_drift(self):
        data = json.loads(call("find_api_deployments", {"api": "checkout"})[0])
        self.assertEqual(data["revisionsByLob"], {
            "payments": {"preprod": ["14"], "prod": ["12"]},
            "retail": {"preprod": ["7"], "prod": ["7"]},
            "lending": {"preprod": ["3"]},
        })
        self.assertEqual(data["notFound"], ["lend-prod"])
        self.assertIn({"org": "partner-prod", "tier": "prod", "lob": None, "environment": "prod",
                       "revision": "5", "deployStartTime": None}, data["deployments"])
        # rows grouped by LOB, then tier in promotion order
        self.assertEqual([r["org"] for r in data["deployments"]][:4], ["pay-pp", "pay-prod", "retail-pp", "retail-prod"])

    def test_find_api_deployments_one_lob(self):
        data = json.loads(call("find_api_deployments", {"api": "checkout", "lob": "payments"})[0])
        self.assertEqual(list(data["revisionsByLob"]), ["payments"])
        self.assertEqual(len(self.seen), 2)

    def test_list_configured_orgs_shows_lobs(self):
        data = json.loads(call("list_configured_orgs", {"lob": "payments"})[0])
        self.assertEqual([o["name"] for o in data["orgs"]], ["pay-pp", "pay-prod"])
        self.assertEqual(data["lobs"][0], {"name": "payments", "description": "Card and payment APIs"})
        self.assertEqual(data["grid"]["lending"], {"preprod": "lend-pp", "prod": "lend-prod"})
        self.assertEqual(self.seen, [])  # no API calls

    def test_unknown_lob_is_a_tool_error_without_requests(self):
        text, err = call("across_orgs", {"resource": "environments", "lob": "cards"})
        self.assertTrue(err)
        self.assertIn("Unknown lob", text)
        text, err = call("list_environments", {"org": "payments@sandbox"})
        self.assertTrue(err)
        self.assertEqual(self.seen, [])

    def test_check_prints_lob_column(self):
        reg = Registry.from_dict(CONFIG)
        c = ApigeeClient(reg, Tokens, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"runtimeType": "HYBRID"})))
        out = io.StringIO()
        with redirect_stdout(out):
            code = asyncio.run(server._check(c))
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("lobs: payments, retail, lending", text)
        self.assertRegex(text, r"\[preprod\s*\] payments\s+pay-pp")
        self.assertRegex(text, r"\[prod\s*\] -\s+partner-prod")


if __name__ == "__main__":
    unittest.main()
