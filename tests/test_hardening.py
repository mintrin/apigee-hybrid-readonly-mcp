"""Regression tests for the production-hardening audit (v0.3.0).

Each test names the failure it guards against.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

import httpx2 as httpx
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from apigee_hybrid_readonly_mcp import __version__, auth, client as client_mod, server
from apigee_hybrid_readonly_mcp.auth import AuthError, GcloudCliProvider
from apigee_hybrid_readonly_mcp.client import ApigeeClient, ApigeeError, ForbiddenOperation, _check_params, seg
from apigee_hybrid_readonly_mcp.config import ConfigError, OrgConfig, Registry
from apigee_hybrid_readonly_mcp.redact import redact

logging.getLogger("apigee_hybrid_readonly_mcp").setLevel(logging.CRITICAL)

ORG = "acme"
BASE = "https://apigee.googleapis.com/v1"


class Tokens:
    def __init__(self) -> None:
        self.n = 0
        self.invalidated = 0
        self.description = "fake"

    async def token(self) -> str:
        self.n += 1
        return f"tok-{self.n}"

    def invalidate(self) -> None:
        self.invalidated += 1


def make(handler, **kw) -> tuple[ApigeeClient, Tokens, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    tokens = Tokens()

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req, len(seen))

    reg = Registry([OrgConfig(ORG)], default_org=ORG)
    c = ApigeeClient(reg, lambda cred: tokens, transport=httpx.MockTransport(h), backoff_s=0, **kw)
    return c, tokens, seen


def run(coro):
    return asyncio.run(coro)


def call(name: str, args: dict | None = None):
    """Returns (text, is_error); raises on an unexpected crash."""
    try:
        res = run(server.mcp.call_tool(name, args or {}))
    except UnexpectedToolError:
        raise
    except ToolError as e:
        return str(e), True
    return "".join(getattr(c, "text", "") for c in res.content), False


# ------------------------------------------------------------------ resilience


class ResilienceTests(unittest.TestCase):
    def test_transient_5xx_is_retried(self):
        c, _, seen = make(lambda r, n: httpx.Response(503) if n < 3 else httpx.Response(200, json={"ok": 1}))
        self.assertEqual(run(c.get(f"organizations/{ORG}")), {"ok": 1})
        self.assertEqual(len(seen), 3)

    def test_retries_are_bounded(self):
        c, _, seen = make(lambda r, n: httpx.Response(429, json={"error": {"message": "slow down"}}), max_retries=2)
        with self.assertRaises(ApigeeError) as cm:
            run(c.get(f"organizations/{ORG}"))
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(len(seen), 3)

    def test_network_errors_become_tool_errors_not_crashes(self):
        def boom(r, n):
            raise httpx.ConnectError("connection refused")

        c, _, seen = make(boom, max_retries=1)
        with self.assertRaises(ApigeeError) as cm:
            run(c.get(f"organizations/{ORG}"))
        self.assertEqual(cm.exception.status, 0)
        self.assertIn("Network error", str(cm.exception))
        self.assertEqual(len(seen), 2)

    def test_401_refreshes_token_once(self):
        c, tokens, seen = make(lambda r, n: httpx.Response(401) if n == 1 else httpx.Response(200, json={}))
        run(c.get(f"organizations/{ORG}"))
        self.assertEqual(tokens.invalidated, 1)
        self.assertEqual([r.headers["authorization"] for r in seen], ["Bearer tok-1", "Bearer tok-2"])

    def test_persistent_401_is_reported(self):
        c, tokens, seen = make(lambda r, n: httpx.Response(401, json={"error": {"message": "bad creds"}}))
        with self.assertRaises(ApigeeError) as cm:
            run(c.get(f"organizations/{ORG}"))
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(len(seen), 2)

    def test_redirects_are_not_followed(self):
        c, _, seen = make(lambda r, n: httpx.Response(302, headers={"location": "https://evil.example/steal"}))
        with self.assertRaises(ApigeeError):
            run(c.get(f"organizations/{ORG}"))
        self.assertEqual(len(seen), 1)

    def test_oversized_response_is_rejected(self):
        c, _, _ = make(lambda r, n: httpx.Response(200, content=b"x" * 2048, headers={"content-type": "text/plain"}))
        with mock.patch.object(client_mod, "MAX_RESPONSE_BYTES", 1024):
            with self.assertRaisesRegex(ApigeeError, "larger than"):
                run(c.get(f"organizations/{ORG}"))

    def test_invalid_json_is_an_error(self):
        c, _, _ = make(lambda r, n: httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"}))
        with self.assertRaisesRegex(ApigeeError, "not valid JSON"):
            run(c.get(f"organizations/{ORG}"))

    def test_unexpected_error_in_one_org_does_not_sink_fanout(self):
        reg = Registry([OrgConfig("a"), OrgConfig("b")], default_org="a")
        c = ApigeeClient(reg, lambda cred: Tokens(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

        async def fn(o):
            if o.name == "a":
                raise KeyError("bug")
            return "fine"

        out = run(c.for_each_org(list(reg.orgs.values()), fn))
        self.assertEqual(out["b"], "fine")
        self.assertIn("internal error", out["a"]["error"])


# ------------------------------------------------------------------ input validation


class ValidationTests(unittest.TestCase):
    def test_method_override_and_credential_params_rejected(self):
        for bad in ["$httpMethod", "httpMethod", "method", "_method", "X-HTTP-Method-Override", "X_HTTP_Method_Override",
                    "access_token", "oauth_token", "key", "fields.x", "a b", "", "upload_protocol", "uploadType",
                    "callback", "alt", "$alt", "fields", "prettyPrint", "quotaUser", "userProject", "format"]:
            with self.assertRaises(ForbiddenOperation, msg=bad):
                _check_params({bad: "x"})

    def test_param_values_validated(self):
        self.assertEqual(_check_params({"pageSize": 10, "expand": True, "filter": None}), {"pageSize": 10, "expand": "true"})
        with self.assertRaises(ForbiddenOperation):
            _check_params({"filter": {"nested": 1}})
        with self.assertRaises(ForbiddenOperation):
            _check_params({"filter": "x" * 5000})

    def test_segment_charset(self):
        for bad in ["a;b", "a%2fb", "ｐrod", "a\nb", " lead", "trail ", "..", "...", "", "a?b", "a#b", "a/b", "a:b", "x" * 300]:
            with self.assertRaises(ForbiddenOperation, msg=repr(bad)):
                seg(bad)
        self.assertEqual(seg("dev@acme.com"), "dev@acme.com")

    def test_every_generic_path_segment_is_validated(self):
        c, _, seen = make(lambda r, n: httpx.Response(200, json={}))
        for bad in [f"organizations/{ORG}/apis/a;b", f"organizations/{ORG}/apis/%2e%2e", f"organizations/{ORG}/apis/ｘ",
                    f"organizations/{ORG}:getSyncAuthorization:x", f"organizations/{ORG}/apis\n/x"]:
            with self.assertRaises(ForbiddenOperation, msg=bad):
                run(c.get(bad))
        self.assertEqual(seen, [])

    def test_commas_only_in_stats_dimension_lists(self):
        c, _, seen = make(lambda r, n: httpx.Response(200, json={}))
        run(c.get(f"organizations/{ORG}/environments/prod/stats/apiproxy,client_id"))
        for bad in [f"organizations/{ORG}/apis/a,b", f"organizations/{ORG}/environments/prod/stats/apiproxy,a;b",
                    f"organizations/{ORG}/environments/prod/stats/apiproxy,,x"]:
            with self.assertRaises(ForbiddenOperation, msg=bad):
                run(c.get(bad))
        self.assertEqual(len(seen), 1)

    def test_spaces_encoded_once(self):
        c, _, seen = make(lambda r, n: httpx.Response(200, json={}))
        run(c.get(f"organizations/{ORG}/developers/dev@acme.com/apps/My App"))
        self.assertEqual(seen[0].url.raw_path.decode(), f"/v1/organizations/{ORG}/developers/dev@acme.com/apps/My%20App")

    def test_post_guard_holds_even_when_called_directly(self):
        c, _, seen = make(lambda r, n: httpx.Response(200, json={}))
        for verb in (":setSyncAuthorization", ":getSyncAuthorization/../x", ""):
            with self.assertRaises(ForbiddenOperation):
                run(c._send("POST", f"organizations/{ORG}{verb}", c.registry.resolve(ORG)))
        with self.assertRaises(ForbiddenOperation):
            run(c._send("PATCH", f"organizations/{ORG}", c.registry.resolve(ORG)))
        self.assertEqual(seen, [])


# ------------------------------------------------------------------ redaction


PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"


class RedactionTests(unittest.TestCase):
    def test_attribute_name_value_pairs(self):
        out = redact({"attributes": [{"name": "client_secret", "value": "abcdefghij1234"},
                                     {"name": "DisplayName", "value": "Mobile app"}]})
        self.assertEqual(out["attributes"][0]["value"], "****1234")
        self.assertEqual(out["attributes"][1]["value"], "Mobile app")

    def test_pem_private_keys_anywhere(self):
        out = redact({"description": f"key: {PEM} end", "list": [PEM]})
        self.assertNotIn("MIIEvQ", json.dumps(out))
        self.assertIn("<redacted PRIVATE KEY>", out["list"][0])

    def test_pem_in_text_body_and_error(self):
        c, _, _ = make(lambda r, n: httpx.Response(200, content=PEM.encode(), headers={"content-type": "text/plain"}))
        self.assertNotIn("MIIEvQ", json.dumps(run(c.get(f"organizations/{ORG}/environments/e/resourcefiles/properties/k"))))
        c, _, _ = make(lambda r, n: httpx.Response(400, json={"error": {"message": f"bad {PEM}"}}))
        with self.assertRaises(ApigeeError) as cm:
            run(c.get(f"organizations/{ORG}"))
        self.assertNotIn("MIIEvQ", str(cm.exception))

    def test_kvm_values_hidden_at_any_depth_and_path(self):
        body = {"keyValueEntries": [{"name": "a", "value": "s3cr3t"}], "nested": {"entry": {"name": "b", "value": "x"}}}
        c, _, _ = make(lambda r, n: httpx.Response(200, json=body))
        for path in [f"organizations/{ORG}/keyvaluemaps/m/entries", f"organizations/{ORG}/keyvaluemaps/m/entries/a",
                     f"organizations/{ORG}/environments/e/keyvaluemaps/m", f"organizations/{ORG}/apis/p/keyvaluemaps/m/entries"]:
            out = json.dumps(run(c.get(path)))
            self.assertNotIn("s3cr3t", out, path)
            self.assertNotIn('"x"', out, path)


# ------------------------------------------------------------------ independent review findings


def _apps_jsonp(r, n):
    body = '// API callback\nx({"credentials": [{"consumerSecret": "CS_SUPERSECRET_9876"}]});'
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/javascript; charset=UTF-8"})


class IndependentReviewTests(unittest.TestCase):
    """Reproductions from the adversarial review, now blocked."""

    def test_r1_system_params_cannot_switch_off_redaction(self):
        c, _, seen = make(_apps_jsonp)
        for params in ({"callback": "x"}, {"alt": "proto"}, {"alt": "sse"}, {"fields": "attributes(value)"}, {"format": "bundle"}):
            with self.assertRaises(ForbiddenOperation, msg=params):
                run(c.get(f"organizations/{ORG}/apps/a1", params))
        self.assertEqual(seen, [])

    def test_r1_non_json_responses_fail_closed(self):
        c, _, _ = make(_apps_jsonp)  # even if the server sent JSONP unprompted
        with self.assertRaisesRegex(ApigeeError, "non-JSON"):
            run(c.get(f"organizations/{ORG}/apps/a1"))

    def test_r2_debug_session_data_blocked(self):
        c, _, seen = make(lambda r, n: httpx.Response(200, json={}))
        for path in [f"organizations/{ORG}/environments/prod/apis/p/revisions/1/debugsessions/s1/data",
                     f"organizations/{ORG}/environments/prod/apis/p/revisions/1/debugsessions/s1/data/t1",
                     f"organizations/{ORG}/environments/prod/apis/p/revisions/1/DebugSessions/s1/Data"]:
            with self.assertRaises(ForbiddenOperation, msg=path):
                run(c.get(path))
        run(c.get(f"organizations/{ORG}/environments/prod/apis/p/revisions/1/debugsessions"))  # listing still allowed
        self.assertEqual(len(seen), 1)

    def test_r3_resource_file_text_is_scanned(self):
        body = "db.password=RESOURCEFILE_SECRET\nauth: Basic dXNlcjpwYXNzd29yZA==\n<ClientSecret>XMLSECRET123</ClientSecret>\nhost=db.local"
        c, _, _ = make(lambda r, n: httpx.Response(200, content=body.encode(), headers={"content-type": "text/plain"}))
        out = json.dumps(run(c.get(f"organizations/{ORG}/environments/prod/resourcefiles/properties/cfg")))
        for leaked in ("RESOURCEFILE_SECRET", "dXNlcjpwYXNzd29yZA", "XMLSECRET123"):
            self.assertNotIn(leaked, out)
        self.assertIn("host=db.local", out)

    def test_r4_secret_name_heuristics(self):
        doc = {
            "truststore_password": "TS_PASSWORD_VALUE", "tokenEndpointClientSecret": "TECS_VALUE_1234",
            "secretManagerToken": "SMT_VALUE_12345", "keyAliasPassword": "KAP_VALUE_1234", "basic_auth": "BASIC_AUTH_VAL",
            "access_key": "ACCESS_KEY_VALUE", "hmacKey": "HMAC_KEY_VALUE1", "sharedKey": "SHARED_KEY_VAL1",
            "encryptionKey": "ENC_KEY_VALUE12", "cookie": "COOKIE_VALUE_123",
            "password": {"value": "NESTED_PW_VALUE"}, "apiKeys": ["APIKEY_IN_LIST1"], "pin": 123456,
            "jwk": {"kty": "RSA", "n": "public-modulus", "d": "JWK_PRIVATE_D", "p": "JWK_PRIVATE_P"},
            "note": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nPGPSECRETDATA\n-----END PGP PRIVATE KEY BLOCK-----",
            "credentials": [{"consumerKey": "CK_VALUE_123456", "status": "approved", "apiProducts": [{"apiproduct": "gold"}]}],
            "keyAliasReference": "my-alias", "name": "kept",
            "attributes": [{"name": "client_id", "value": "LEAK_CLIENT_ID"}],
        }
        out = json.dumps(redact(doc))
        for leaked in ("TS_PASSWORD", "TECS_VALUE", "SMT_VALUE", "KAP_VALUE", "BASIC_AUTH_VAL", "ACCESS_KEY_VALUE",
                       "HMAC_KEY", "SHARED_KEY", "ENC_KEY", "COOKIE_VALUE", "NESTED_PW", "APIKEY_IN_LIST",
                       "123456", "JWK_PRIVATE", "PGPSECRETDATA", "CK_VALUE", "LEAK_CLIENT_ID"):
            self.assertNotIn(leaked, out, leaked)
        r = redact(doc)
        self.assertEqual(r["jwk"]["n"], "public-modulus")
        self.assertEqual(r["credentials"][0]["status"], "approved")
        self.assertEqual(r["credentials"][0]["apiProducts"], [{"apiproduct": "gold"}])
        self.assertEqual(r["keyAliasReference"], "my-alias")

    def test_r6_kvm_masking_is_case_insensitive(self):
        c, _, _ = make(lambda r, n: httpx.Response(200, json={"keyValueEntries": [{"name": "a", "value": "KVM_RAW_VALUE"}]}))
        out = json.dumps(run(c.get(f"organizations/{ORG}/environments/prod/keyValueMaps/cfg/entries")))
        self.assertNotIn("KVM_RAW_VALUE", out)

    def test_r8_deeply_nested_json_is_an_error_not_a_crash(self):
        c, _, _ = make(lambda r, n: httpx.Response(200, content=b"[" * 100_000, headers={"content-type": "application/json"}))
        with self.assertRaises(ApigeeError):
            run(c.get(f"organizations/{ORG}"))

    def test_r9_error_messages_are_scanned(self):
        c, _, _ = make(lambda r, n: httpx.Response(400, json={"error": {"message": "bad value client_secret=LEAKY_SECRET_1"}}))
        with self.assertRaises(ApigeeError) as cm:
            run(c.get(f"organizations/{ORG}"))
        self.assertNotIn("LEAKY_SECRET_1", str(cm.exception))


# ------------------------------------------------------------------ tools


class ToolBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: list[httpx.Request] = []

        def h(req: httpx.Request) -> httpx.Response:
            self.seen.append(req)
            p = req.url.path
            if p.endswith("/keystores"):
                return httpx.Response(200, json=["ks1"])
            if p.endswith("/keystores/ks1"):
                return httpx.Response(200, json={"aliases": ["good", "denied", "weird"]})
            if p.endswith("/aliases/good"):
                return httpx.Response(200, json={"certsInfo": {"certInfo": [{"subject": "CN=a", "expiryDate": "4102444800000"}]}})
            if p.endswith("/aliases/denied"):
                return httpx.Response(403, json={"error": {"message": "denied"}})
            if p.endswith("/aliases/weird"):
                return httpx.Response(200, json={"certsInfo": {"certInfo": [{"subject": "CN=w", "expiryDate": "soon"}]}})
            if p.endswith("/apis/orders/deployments"):
                return httpx.Response(200, json={"deployments": "not-a-list"})
            return httpx.Response(404, json={"error": {"message": "nope"}})

        reg = Registry([OrgConfig(ORG)], default_org=ORG)
        server.set_client(ApigeeClient(reg, lambda cred: Tokens(), transport=httpx.MockTransport(h), backoff_s=0))

    def test_failures_are_mcp_tool_errors(self):
        text, is_error = call("list_environments", {"org": "nope"})
        self.assertTrue(is_error)
        self.assertIn("not configured", text)
        text, is_error = call("list_kvms", {"scope": "env"})
        self.assertTrue(is_error)
        self.assertIn("env is required", text)

    def test_numeric_arguments_are_bounded(self):
        for name, args in [("list_api_products", {"count": 100000}), ("query_stats", {"env": "e", "dimensions": "apiproxy", "limit": 0}),
                           ("check_certificate_expiry", {"env": "e", "warn_days": -1})]:
            text, is_error = call(name, args)
            self.assertTrue(is_error, name)
        self.assertEqual(self.seen, [])

    def test_cert_scan_survives_partial_failures(self):
        text, is_error = call("check_certificate_expiry", {"env": "prod"})
        self.assertFalse(is_error)
        data = json.loads(text)
        self.assertEqual({r["alias"]: r["status"] for r in data["certificates"]}, {"good": "OK", "weird": "UNKNOWN"})
        self.assertEqual(data["errors"][0]["alias"], "denied")

    def test_find_deployments_tolerates_odd_shapes(self):
        text, is_error = call("find_api_deployments", {"api": "orders"})
        self.assertFalse(is_error)
        self.assertEqual(json.loads(text)["deployments"][0]["note"], "exists, not deployed")

    def test_r5_credential_dimensions_are_masked(self):
        stats = {"environments": [{"name": "prod", "dimensions": [
            {"name": "CONSUMERKEY_FROM_STATS_5678", "metrics": []}]}]}
        reg = Registry([OrgConfig(ORG)], default_org=ORG)
        server.set_client(ApigeeClient(reg, lambda cred: Tokens(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=stats))))
        text, is_error = call("query_stats", {"env": "prod", "dimensions": "client_id"})
        self.assertFalse(is_error)
        self.assertNotIn("CONSUMERKEY_FROM_STATS", text)
        self.assertIn("****5678", text)
        stats["environments"][0]["dimensions"][0]["name"] = "orders,CONSUMERKEY_FROM_STATS_5678"
        text, _ = call("query_stats", {"env": "prod", "dimensions": "apiproxy,client_id"})
        self.assertIn("orders,****5678", text)

    def test_r7_fanout_is_capped(self):
        reg = Registry([], open_mode=True)
        seen = []
        server.set_client(ApigeeClient(reg, lambda cred: Tokens(),
                                       transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=[]))))
        text, is_error = call("across_orgs", {"resource": "environments", "orgs": [f"org{i}" for i in range(500)]})
        self.assertTrue(is_error)
        self.assertIn("at most", text)
        self.assertEqual(seen, [])
        text, is_error = call("across_orgs", {"resource": "environments", "orgs": ["a", "a", "a"]})
        self.assertFalse(is_error)
        self.assertEqual(len(seen), 1)  # duplicates collapsed

    def test_empty_dimensions_and_resource_rejected(self):
        self.assertTrue(call("query_stats", {"env": "e", "dimensions": " , "})[1])
        self.assertTrue(call("across_orgs", {"resource": "/"})[1])
        self.assertTrue(call("across_orgs", {"resource": "organizations"})[1])


# ------------------------------------------------------------------ config strictness


class ConfigStrictnessTests(unittest.TestCase):
    def test_typo_in_credential_key_fails_closed(self):
        with self.assertRaisesRegex(ConfigError, "unknown keys"):
            Registry.from_dict({"tiers": {"prod": {"impersonate_serviceaccount": "x@y.iam.gserviceaccount.com", "orgs": ["p"]}}})
        with self.assertRaisesRegex(ConfigError, "unknown keys"):
            Registry.from_dict({"orgs": {"p": {"impersonate": "x@y"}}})
        with self.assertRaisesRegex(ConfigError, "unknown keys"):
            Registry.from_dict({"defaults": {"api-base": "https://x.googleapis.com/v1"}})

    def test_api_base_must_be_https(self):
        for bad in ["http://apigee.googleapis.com/v1", "ftp://x", "https://user:pw@apigee.googleapis.com/v1",
                    "https://apigee.googleapis.com/v1?x=1", "apigee.googleapis.com/v1"]:
            with self.assertRaises(ConfigError, msg=bad):
                Registry.from_dict({"defaults": {"api_base": bad}, "orgs": {"a": {}}})
        Registry.from_dict({"defaults": {"api_base": "http://127.0.0.1:9/v1"}, "orgs": {"a": {}}})  # local test double ok

    def test_types_and_ranges(self):
        bad_docs = [
            {"orgs": {"a": {"aliases": "x"}}},
            {"orgs": {"a": {"allow_kvm_values": "maybe"}}},
            {"max_parallel": 0, "orgs": {"a": {}}},
            {"timeout_s": "fast", "orgs": {"a": {}}},
            {"tiers": {"prod": {"orgs": "p"}}},
            {"tiers": {"prod": "nope"}},
            {"orgs": {"bad name!": {}}},
            {"orgs": {"a": {"impersonate_service_account": "not-an-email"}}},
            {"default_org": "missing", "orgs": {"a": {}}},
        ]
        for doc in bad_docs:
            with self.assertRaises(ConfigError, msg=doc):
                Registry.from_dict(doc)

    def test_file_errors_are_config_errors(self):
        with self.assertRaisesRegex(ConfigError, "not found"):
            Registry.from_toml("/nonexistent/apigee.toml")
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("this is = = not toml")
        try:
            with self.assertRaisesRegex(ConfigError, "not valid TOML"):
                Registry.from_toml(f.name)
        finally:
            os.unlink(f.name)

    def test_tier_filter_requires_config(self):
        env = {"APIGEE_CONFIG": "", "APIGEE_ALLOWED_ORGS": "", "APIGEE_ORG": "", "APIGEE_TIERS": "prod"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaisesRegex(ConfigError, "APIGEE_TIERS"):
                Registry.load()


# ------------------------------------------------------------------ auth + CLI


class AuthAndCliTests(unittest.TestCase):
    def test_gcloud_timeout_and_empty_output(self):
        p = GcloudCliProvider("/usr/bin/gcloud")
        with mock.patch.object(auth.subprocess, "run", side_effect=subprocess.TimeoutExpired("gcloud", 60)):
            with self.assertRaisesRegex(AuthError, "timed out"):
                run(p.token())
        with mock.patch.object(auth.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="\n", stderr="")):
            with self.assertRaisesRegex(AuthError, "no access token"):
                run(p.token())
        with mock.patch.object(auth.subprocess, "run", side_effect=FileNotFoundError("gcloud")):
            with self.assertRaisesRegex(AuthError, "could not run"):
                run(p.token())

    def test_gcloud_invalidate_refetches(self):
        p = GcloudCliProvider("/usr/bin/gcloud")
        outs = iter(["tok-a\n", "tok-b\n"])
        with mock.patch.object(auth.subprocess, "run", side_effect=lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=next(outs), stderr="")):
            self.assertEqual(run(p.token()), "tok-a")
            p.invalidate()
            self.assertEqual(run(p.token()), "tok-b")

    def test_main_exits_2_on_config_error(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"APIGEE_CONFIG": "/nonexistent.toml"}), redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                server.main([])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("configuration error", err.getvalue())

    def test_version_flag(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            server.main(["--version"])
        self.assertIn(f"apigee-hybrid-readonly-mcp {__version__}", out.getvalue())

    def test_legacy_command_still_works_with_warning(self):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), redirect_stderr(err), self.assertRaises(SystemExit):
            with mock.patch("sys.argv", ["apigee-hybrid-mcp", "--version"]):
                server.main_legacy()
        self.assertIn("deprecated", err.getvalue())
        self.assertIn(__version__, out.getvalue())

    def test_server_identity(self):
        self.assertEqual(server.mcp.name, "apigee-hybrid-readonly")
        self.assertEqual(server.mcp.title, "Apigee Hybrid Read-Only MCP")


if __name__ == "__main__":
    unittest.main()


class EveryToolSmokeTest(unittest.TestCase):
    """Every registered tool runs end to end with minimal arguments: catches a tool whose own
    query parameters fall outside the allowlist, or a crash on an empty-but-valid response."""

    MINIMAL = {
        "get_environment": {"env": "e"}, "get_deployed_config": {"env": "e"}, "get_envgroup": {"envgroup": "g"},
        "get_api_proxy": {"api": "a"}, "get_api_proxy_revision": {"api": "a", "revision": "1"},
        "get_sharedflow": {"sharedflow": "s"}, "get_deployment_status": {"env": "e", "api": "a", "revision": "1"},
        "list_env_resources": {"env": "e", "resource": "targetservers"}, "get_target_server": {"env": "e", "name": "t"},
        "get_keystore": {"env": "e", "keystore": "k"}, "get_flowhook": {"env": "e", "flowhook": "PreProxyFlowHook"},
        "check_certificate_expiry": {"env": "e"}, "list_kvms": {"env": "e"}, "list_kvm_entries": {"kvm": "m", "env": "e"},
        "get_api_product": {"product": "p"}, "get_developer": {"email": "d@x.com"}, "get_app": {"app_id": "a"},
        "query_stats": {"env": "e", "dimensions": "apiproxy,response_status_code"},
        "list_debug_sessions": {"env": "e", "api": "a", "revision": "1"}, "across_orgs": {"resource": "environments"},
        "find_api_deployments": {"api": "a"}, "apigee_get": {"path": "organizations/acme"},
        "list_deployments": {"sharedflows": True}, "list_apps": {"expand": True}, "list_api_products": {"expand": True},
        "list_developers": {"expand": True}, "list_operations": {"filter": "done=false"},
        "list_api_proxies": {"include_revisions": True}, "list_sharedflows": {"include_revisions": True},
    }

    def test_every_tool(self):
        reg = Registry([OrgConfig(ORG, tier="prod")], default_org=ORG, tier_order=["prod"])
        server.set_client(ApigeeClient(reg, lambda cred: Tokens(),
                                       transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
        tools = run(server.mcp.list_tools())
        self.assertEqual(len(tools), 36)
        for t in tools:
            text, is_error = call(t.name, self.MINIMAL.get(t.name, {}))
            self.assertFalse(is_error, f"{t.name}: {text[:200]}")
