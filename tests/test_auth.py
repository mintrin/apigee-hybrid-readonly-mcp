"""Credential selection: service accounts and user logins via ADC or the gcloud CLI."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import types
import logging
import unittest
from unittest import mock

from apigee_hybrid_readonly_mcp import auth
from apigee_hybrid_readonly_mcp.auth import AuthError, GcloudCliProvider, GoogleAuthProvider, StaticTokenProvider, build_token_provider
from apigee_hybrid_readonly_mcp.config import ConfigError, CredSpec, Registry

GCLOUD = "/usr/bin/gcloud"


def fake_run(stdout: str):
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return run, calls


logging.getLogger("apigee_hybrid_readonly_mcp").setLevel(logging.ERROR)  # keep test output readable


class FakeCreds:
    def __init__(self, email: str | None = None) -> None:
        self.service_account_email = email
        self.token = None
        self.valid = False

    def refresh(self, request) -> None:
        self.token, self.valid = "adc-token", True


def no_google():
    """Make `import google...` fail regardless of what is installed."""
    return mock.patch.dict(sys.modules, {"google": None, "google.auth": None, "google.oauth2": None})


def with_google(creds: FakeCreds):
    """Install a minimal fake google-auth whose ADC returns `creds`."""
    google = types.ModuleType("google")
    ga = types.ModuleType("google.auth")
    ga.default = lambda scopes=None: (creds, "proj-x")
    imp = types.ModuleType("google.auth.impersonated_credentials")

    class Impersonated(FakeCreds):
        def __init__(self, source_credentials, target_principal, target_scopes, lifetime):
            super().__init__(target_principal)
            self.source = source_credentials

    imp.Credentials = Impersonated
    tr = types.ModuleType("google.auth.transport")
    trr = types.ModuleType("google.auth.transport.requests")
    trr.Request = lambda: None
    google.auth = ga
    ga.impersonated_credentials = imp
    ga.transport = tr
    tr.requests = trr
    return mock.patch.dict(
        sys.modules,
        {
            "google": google,
            "google.auth": ga,
            "google.auth.impersonated_credentials": imp,
            "google.auth.transport": tr,
            "google.auth.transport.requests": trr,
        },
    )


class AuthSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {"APIGEE_ACCESS_TOKEN": ""}, clear=False)
        self.env.start()
        self.which = mock.patch.object(auth.shutil, "which", return_value=GCLOUD)
        self.which.start()

    def tearDown(self) -> None:
        self.which.stop()
        self.env.stop()

    # ---- user credentials via gcloud ----------------------------------

    def test_gcloud_user_login(self):
        p = build_token_provider(CredSpec(auth="gcloud"))
        self.assertIsInstance(p, GcloudCliProvider)
        run, calls = fake_run("ya29.user-token\n")
        with mock.patch.object(auth.subprocess, "run", run):
            self.assertEqual(asyncio.run(p.token()), "ya29.user-token")
            asyncio.run(p.token())  # cached
        self.assertEqual(calls, [[GCLOUD, "auth", "print-access-token"]])

    def test_gcloud_specific_account(self):
        p = build_token_provider(CredSpec(gcloud_account="minh@acme.com"))
        run, calls = fake_run("tok\n")
        with mock.patch.object(auth.subprocess, "run", run):
            asyncio.run(p.token())
        self.assertEqual(calls[0], [GCLOUD, "auth", "print-access-token", "minh@acme.com"])

    def test_gcloud_user_impersonating_tier_sa(self):
        spec = CredSpec(auth="gcloud", impersonate_service_account="reader@prod.iam.gserviceaccount.com")
        p = build_token_provider(spec)
        # gcloud prints a warning line before the token when impersonating
        run, calls = fake_run("WARNING: This command is using service account impersonation.\nya29.sa-token\n")
        with mock.patch.object(auth.subprocess, "run", run):
            self.assertEqual(asyncio.run(p.token()), "ya29.sa-token")
        self.assertIn("--impersonate-service-account=reader@prod.iam.gserviceaccount.com", calls[0])

    def test_gcloud_failure_is_reported(self):
        p = build_token_provider(CredSpec(auth="gcloud"))

        def boom(cmd, **kw):
            raise subprocess.CalledProcessError(1, cmd, stderr="Reauthentication required.")

        with mock.patch.object(auth.subprocess, "run", boom):
            with self.assertRaisesRegex(AuthError, "Reauthentication required"):
                asyncio.run(p.token())

    def test_gcloud_missing(self):
        self.which.stop()
        with mock.patch.object(auth.shutil, "which", return_value=None):
            with self.assertRaisesRegex(AuthError, "not on PATH"):
                build_token_provider(CredSpec(auth="gcloud"))
        self.which.start()

    # ---- ADC: service accounts and user ADC ---------------------------

    def test_adc_service_account(self):
        with with_google(FakeCreds("svc@proj.iam.gserviceaccount.com")):
            p = build_token_provider(CredSpec(auth="adc"))
            self.assertIsInstance(p, GoogleAuthProvider)
            self.assertIn("svc@proj", p.description)
            self.assertEqual(asyncio.run(p.token()), "adc-token")

    def test_adc_user_impersonating_sa(self):
        with with_google(FakeCreds(None)):
            p = build_token_provider(CredSpec(impersonate_service_account="reader@np.iam.gserviceaccount.com"))
            self.assertIsInstance(p, GoogleAuthProvider)
            self.assertIn("user credentials", p.description)
            self.assertIn("impersonating reader@np", p.description)

    def test_auto_prefers_adc_then_falls_back_to_gcloud(self):
        with with_google(FakeCreds("svc@p.iam.gserviceaccount.com")):
            self.assertIsInstance(build_token_provider(CredSpec()), GoogleAuthProvider)
        with no_google():
            self.assertIsInstance(build_token_provider(CredSpec()), GcloudCliProvider)

    def test_adc_required_but_missing(self):
        with no_google():
            with self.assertRaisesRegex(AuthError, "google-auth"):
                build_token_provider(CredSpec(auth="adc"))
            with self.assertRaisesRegex(AuthError, "google-auth"):
                build_token_provider(CredSpec(credentials_file="/k.json"))

    # ---- static token -------------------------------------------------

    def test_static_token_only_for_default_credential(self):
        with mock.patch.dict(os.environ, {"APIGEE_ACCESS_TOKEN": "static"}):
            self.assertIsInstance(build_token_provider(CredSpec()), StaticTokenProvider)
            # A tier that impersonates must not silently use the static token
            self.assertIsInstance(
                build_token_provider(CredSpec(auth="gcloud", impersonate_service_account="x@y.iam.gserviceaccount.com")),
                GcloudCliProvider,
            )

    # ---- config plumbing ----------------------------------------------

    def test_config_auth_inherits_and_validates(self):
        reg = Registry.from_dict(
            {
                "defaults": {"auth": "gcloud"},
                "tiers": {
                    "sandbox": {"orgs": ["s1"]},
                    "prod": {"auth": "adc", "impersonate_service_account": "r@p.iam.gserviceaccount.com", "orgs": ["p1"]},
                },
                "orgs": {"s2": {"tier": "sandbox", "gcloud_account": "minh@acme.com"}},
            }
        )
        self.assertEqual(reg.resolve("s1").cred.auth, "gcloud")
        self.assertEqual(reg.resolve("s2").cred.describe(), "gcloud account minh@acme.com")
        self.assertEqual(reg.resolve("p1").cred.describe(), "ADC default credentials -> impersonate r@p.iam.gserviceaccount.com")
        with self.assertRaises(ConfigError):
            Registry.from_dict({"defaults": {"auth": "magic"}, "orgs": {"a": {}}})

    def test_env_mode_auth(self):
        env = {"APIGEE_CONFIG": "", "APIGEE_ALLOWED_ORGS": "", "APIGEE_ORG": "", "APIGEE_AUTH": "gcloud",
               "APIGEE_IMPERSONATE_SERVICE_ACCOUNT": "r@p.iam.gserviceaccount.com"}
        with mock.patch.dict(os.environ, env):
            reg = Registry.load()
        self.assertEqual(reg.resolve("any-org").cred.auth, "gcloud")
        self.assertEqual(reg.resolve("any-org").cred.impersonate_service_account, "r@p.iam.gserviceaccount.com")


if __name__ == "__main__":
    unittest.main()
