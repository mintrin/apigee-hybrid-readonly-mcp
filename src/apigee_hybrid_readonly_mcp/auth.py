"""Access-token providers for the Apigee management API.

Each org resolves to a CredSpec (see config.py); one provider is built and
cached per distinct CredSpec, so 24 orgs across 3 tiers typically means 3
token providers.

Supported identities
--------------------
Service accounts
  * JSON key file ............ credentials_file = "/path/key.json"         (google-auth)
  * workload identity / GCE .. auth = "adc"                                (google-auth ADC)
  * gcloud-activated SA ...... auth = "gcloud", gcloud_account = "sa@..."  (gcloud auth activate-service-account)
  * impersonation ............ impersonate_service_account = "sa@..."      (on top of any of the above,
                                                                            or of a user login)
User accounts
  * gcloud CLI login ......... auth = "gcloud"   (gcloud auth login; gcloud_account picks one of several)
  * ADC user login ........... auth = "adc"      (gcloud auth application-default login)
  * raw token ................ APIGEE_ACCESS_TOKEN=$(gcloud auth print-access-token)  (default cred only, ~1h)

auth = "auto" (default) tries ADC first, then the gcloud CLI.
Impersonating needs roles/iam.serviceAccountTokenCreator on the target SA.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from typing import Any, Protocol

from .config import CredSpec

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_GCLOUD_TOKEN_TTL_S = 45 * 60  # gcloud does not report expiry; tokens last 60 min
_GCLOUD_TIMEOUT_S = 60


def _short(msg: object, limit: int = 500) -> str:
    """One-line, bounded error text (gcloud / google-auth errors can be long)."""
    text = " ".join(str(msg or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


class AuthError(RuntimeError):
    pass


class TokenProvider(Protocol):
    description: str

    async def token(self) -> str: ...

    def invalidate(self) -> None:
        """Drop any cached token so the next token() call fetches a fresh one (after a 401)."""
        ...


class StaticTokenProvider:
    def __init__(self, token: str) -> None:
        self._token = token.strip()
        self.description = "static token from APIGEE_ACCESS_TOKEN"

    async def token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        pass  # nothing to refresh; a 401 surfaces to the caller


class GoogleAuthProvider:
    """Wraps any google-auth Credentials object and refreshes it when needed."""

    def __init__(self, creds: Any, description: str) -> None:
        self._creds = creds
        self._lock = asyncio.Lock()
        self._force_refresh = False
        self.description = description

    def invalidate(self) -> None:
        self._force_refresh = True

    def _refresh_sync(self) -> None:
        import google.auth.transport.requests

        self._creds.refresh(google.auth.transport.requests.Request())

    async def token(self) -> str:
        async with self._lock:
            # `valid` is False with no token or within google-auth's refresh threshold.
            if self._force_refresh or not self._creds.valid:
                try:
                    await asyncio.to_thread(self._refresh_sync)
                except Exception as e:
                    raise AuthError(f"{self.description}: token refresh failed: {_short(e)}") from e
                self._force_refresh = False
            if not self._creds.token:
                raise AuthError(f"{self.description}: no access token after refresh")
            return self._creds.token


class GcloudCliProvider:
    def __init__(self, gcloud: str, account: str | None = None, impersonate: str | None = None) -> None:
        self._cmd = [gcloud, "auth", "print-access-token"]
        if account:
            self._cmd.append(account)
        if impersonate:
            self._cmd.append(f"--impersonate-service-account={impersonate}")
        self._token: str | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()
        self.description = "gcloud CLI" + (f" as {account}" if account else "") + (
            f" impersonating {impersonate}" if impersonate else ""
        )

    def invalidate(self) -> None:
        self._token = None

    def _fetch_sync(self) -> str:
        try:
            out = subprocess.run(
                self._cmd, capture_output=True, text=True, timeout=_GCLOUD_TIMEOUT_S, check=True, stdin=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError as e:
            raise AuthError(f"{self.description} failed: {_short(e.stderr)}") from e
        except subprocess.TimeoutExpired:
            raise AuthError(f"{self.description} timed out after {_GCLOUD_TIMEOUT_S}s (is gcloud waiting for a login?)") from None
        except OSError as e:
            raise AuthError(f"{self.description} could not run: {e}") from e
        # gcloud may print a warning line before the token when impersonating
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        if not lines or " " in lines[-1]:
            raise AuthError(f"{self.description} returned no access token")
        return lines[-1]

    async def token(self) -> str:
        async with self._lock:
            if not self._token or time.time() - self._fetched_at > _GCLOUD_TOKEN_TTL_S:
                self._token = await asyncio.to_thread(self._fetch_sync)
                self._fetched_at = time.time()
            return self._token


def _adc() -> tuple[Any, str | None]:
    import google.auth

    return google.auth.default(scopes=SCOPES)


def _google_creds(spec: CredSpec) -> tuple[Any, str]:
    """google-auth credentials for a spec: SA key file or ADC, optionally impersonating an SA."""
    if spec.credentials_file:
        from google.oauth2 import service_account

        source = service_account.Credentials.from_service_account_file(spec.credentials_file, scopes=SCOPES)
        desc = f"key file for {source.service_account_email}"
    else:
        source, project = _adc()
        who = getattr(source, "service_account_email", None) or "user credentials"
        desc = f"ADC ({who}, project={project})"
    if spec.impersonate_service_account:
        from google.auth import impersonated_credentials

        source = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=spec.impersonate_service_account,
            target_scopes=SCOPES,
            lifetime=3600,
        )
        desc += f" impersonating {spec.impersonate_service_account}"
    return source, desc


def build_token_provider(spec: CredSpec | None = None) -> TokenProvider:
    """Build the token provider for one credential spec (see CredSpec.auth)."""
    spec = spec or CredSpec()

    # Quick-test escape hatch; only for the plain default credential.
    static = os.environ.get("APIGEE_ACCESS_TOKEN")
    if static and spec.is_default:
        return StaticTokenProvider(static)

    gcloud = shutil.which("gcloud")
    use_gcloud = spec.auth == "gcloud" or (spec.auth == "auto" and bool(spec.gcloud_account))
    if use_gcloud:
        if not gcloud:
            raise AuthError("auth = 'gcloud' (or gcloud_account) is set but the gcloud CLI is not on PATH")
        return GcloudCliProvider(gcloud, account=spec.gcloud_account, impersonate=spec.impersonate_service_account)

    errors: list[str] = []
    try:
        creds, desc = _google_creds(spec)
        return GoogleAuthProvider(creds, desc)
    except ImportError:
        errors.append("google-auth not installed (pip install 'apigee-hybrid-readonly-mcp[google]')")
    except Exception as e:  # DefaultCredentialsError, bad key file, ...
        errors.append(f"google-auth: {_short(e)}")

    if spec.auth == "adc" or spec.credentials_file:
        raise AuthError(f"{spec.describe()}: " + "; ".join(errors))
    if gcloud:  # auto: fall back to whatever gcloud is logged in as
        return GcloudCliProvider(gcloud, impersonate=spec.impersonate_service_account)
    errors.append("gcloud CLI not found on PATH")
    raise AuthError("No Google credentials found. Tried:\n  - " + "\n  - ".join(errors))
