"""Mask credential-like fields before responses reach the model.

Apigee happily returns developer-app consumer keys/secrets, target-server
passwords inside configs, etc. to anyone with read access. The model rarely
needs the raw value, and anything it sees can end up in a chat transcript or
log, so by default those values are masked down to their last 4 characters.
Set APIGEE_REDACT_SECRETS=false to disable (not recommended).
"""

from __future__ import annotations

import re
from typing import Any

# Matched against the JSON key name, case-insensitively.
_SECRET_KEY = re.compile(
    r"(secret|password|passwd|private[_-]?key|consumer[_-]?key|client[_-]?key|"
    r"api[_-]?key|token|credential[_-]?value|^key$|keystorepassword)",
    re.IGNORECASE,
)
# Keys that look secret-ish but are just metadata.
_ALLOW_KEY = re.compile(
    r"(^keystore$|^keys$|keyalias|keyaliasreference|^name$|tokentype|expiresat|expiresin|"
    r"issuedat|apiproducts?|^status$|truststore|^keyname$|keystores?|"
    r"secretmanager|tokenendpoint|^keyid$|^keys?count$)",
    re.IGNORECASE,
)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"****{value[-4:]}"


def redact(obj: Any) -> Any:
    """Return a deep copy of obj with secret-looking string values masked."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and _SECRET_KEY.search(k) and not _ALLOW_KEY.search(k):
                out[k] = _mask(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj
