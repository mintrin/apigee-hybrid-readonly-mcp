"""Mask credential-like values before responses reach the model.

Apigee returns developer-app consumer keys/secrets, attribute values, and
other credentials to anyone with read access. The model rarely needs the raw
value, and anything it sees can end up in a chat transcript or log, so by
default those values are masked (strings down to their last 4 characters).
Set redact_secrets = false (APIGEE_REDACT_SECRETS=false) to disable (not recommended).

Rules, applied to every JSON response:
  1. A key that looks secret ("consumerSecret", "truststore_password", "hmacKey", ...)
     has its whole value masked: strings, numbers, lists and objects alike.
  2. Apigee name/value pairs ({"name": "client_secret", "value": ...}, used for
     app/product/developer attributes) whose *name* looks secret.
  3. Private JWK members (d, p, q, dp, dq, qi, k) in any object that has "kty".
  4. Every string is scanned as free text (see mask_text): PEM private-key blocks,
     `Basic`/`Bearer` credentials, and `secret_name=value` / `<SecretName>value</SecretName>`.

Redaction is a heuristic. A secret stored under an innocuous name in an arbitrary
field can still pass; IAM and the tool allowlist remain the primary controls.
"""

from __future__ import annotations

import re
from typing import Any

# Matched against key and attribute names, case-insensitively. Checked BEFORE the
# metadata allowlist, which only contains exact names.
_SECRET_KEY = re.compile(
    r"(secret|password|passwd|passphrase|pass[_-]?word|private[_-]?key|consumer[_-]?key|client[_-]?(key|id)$|"
    r"api[_-]?keys?$|^apikey|token|credential|basic[_-]?auth|auth[_-]?(header|token|key|code)|^authorization$|"
    r"^key$|^keys?value$|access[_-]?key|(hmac|shared|encryption|signing|master|session)[_-]?key|"
    r"cookie|^pin$|passcode|^otp$)",
    re.IGNORECASE,
)
# Exact names that match the pattern above but are metadata, not secrets.
_ALLOW_KEY = re.compile(
    r"^(credentials|tokentype|tokenendpoint|expiresat|expiresin|issuedat|"
    r"accesstokenexpiry|tokenexpiry|credentialscount)$",
    re.IGNORECASE,
)
_JWK_PRIVATE = {"d", "p", "q", "dp", "dq", "qi", "k"}

_PEM_PRIVATE = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY[A-Z ]*)-----.*?(-----END [A-Z0-9 ]+-----|\Z)",
    re.DOTALL,
)
_AUTH_SCHEME = re.compile(r"\b(Basic|Bearer|Digest)\s+[A-Za-z0-9._~+/=\-]{8,}", re.IGNORECASE)
_SECRET_WORD = (
    r"[A-Za-z0-9_.\-]*(?:secret|password|passwd|passphrase|private[_.-]?key|api[_.-]?key|token|"
    r"credential|access[_.-]?key|client[_.-]?id|consumer[_.-]?key)[A-Za-z0-9_.\-]*"
)
# key=value, key: value, "key": "value" in free text (properties files, JS, YAML, logs)
_KV_TEXT = re.compile(
    r"(?P<k>[\"']?" + _SECRET_WORD + r"[\"']?\s*[:=]\s*)(?P<q>[\"']?)(?P<v>[^\s\"'&,;<]{1,})",
    re.IGNORECASE,
)
# <ClientSecret>value</ClientSecret> in XML (policies, config)
_XML_TEXT = re.compile(r"(<(" + _SECRET_WORD + r")\b[^>]*>)([^<]{1,})(</\2>)", re.IGNORECASE)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"****{value[-4:]}"


def _mask_any(value: Any) -> Any:
    """Mask a value of any type under a secret key."""
    if isinstance(value, str):
        return _mask(value)
    if isinstance(value, list):
        return [_mask_any(v) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {k: _mask_any(v) for k, v in value.items()}
    return "****"  # numbers


def _secret_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_SECRET_KEY.search(name)) and not _ALLOW_KEY.match(name)


def mask_pem(text: str) -> str:
    """Replace PEM / PGP private-key blocks in free text."""
    if "PRIVATE KEY" not in text:
        return text
    return _PEM_PRIVATE.sub(lambda m: f"<redacted {m.group(1).strip()}>", text)


def mask_text(text: str) -> str:
    """Free-text scan: private keys, auth headers, and secret-named key=value / XML values."""
    text = mask_pem(text)
    text = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} ****", text)
    text = _KV_TEXT.sub(lambda m: f"{m.group('k')}{m.group('q')}{_mask(m.group('v'))}", text)
    text = _XML_TEXT.sub(lambda m: f"{m.group(1)}{_mask(m.group(3))}{m.group(4)}", text)
    return text


def redact(obj: Any) -> Any:
    """Return a deep copy of obj with secret-looking values masked."""
    if isinstance(obj, dict):
        pair_secret = "value" in obj and _secret_name(obj.get("name"))
        is_jwk = "kty" in obj
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if _secret_name(k) or (pair_secret and k == "value") or (is_jwk and k in _JWK_PRIVATE):
                out[k] = _mask_any(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj
