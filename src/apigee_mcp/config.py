"""Org / tier registry.

Two ways to configure:

1. A TOML file (APIGEE_CONFIG=/path/to/apigee-orgs.toml) for many orgs across
   tiers, with per-tier credentials. See apigee-orgs.example.toml.
2. Plain env vars (APIGEE_ORG, APIGEE_ALLOWED_ORGS, APIGEE_API_BASE) for a
   single credential. With neither a config file nor an allowlist, any org
   name is accepted ("open mode") and IAM alone decides what is visible.

Settings inherit defaults -> tier -> org, so most orgs only need a tier.
APIGEE_TIERS=sandbox,nonprod limits one server instance to some tiers
(e.g. run a separate instance for prod).

Validation is strict and fails closed: unknown keys, wrong types, non-HTTPS
API bases and malformed names are all startup errors. In particular a typo in
a credential key must never silently fall back to a broader identity.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("apigee_mcp.config")

DEFAULT_BASE_URL = "https://apigee.googleapis.com/v1"
AUTH_METHODS = ("auto", "adc", "gcloud")
MAX_FANOUT = 50  # explicit org list per cross-org call

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,254}$")
_SA_EMAIL = re.compile(r"^[A-Za-z0-9._\-]+@[A-Za-z0-9._\-]+$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

_INHERITED = ("api_base", "auth", "impersonate_service_account", "credentials_file", "gcloud_account", "allow_kvm_values")
_TOP_KEYS = {"default_org", "defaults", "tiers", "orgs", "redact_secrets", "timeout_s", "max_parallel"}
_DEFAULT_KEYS = set(_INHERITED)
_TIER_KEYS = set(_INHERITED) | {"orgs", "description"}
_ORG_KEYS = set(_INHERITED) | {"tier", "aliases", "description"}


class ConfigError(ValueError):
    pass


class UnknownOrg(ValueError):
    pass


# ---------------------------------------------------------------------------
# validators


def _check_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ConfigError(f"Invalid {what} {value!r}: use letters, digits, '.', '-' or '_'")
    return value


def _check_api_base(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: api_base must be a string")
    url = value.rstrip("/")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"{where}: api_base must not contain credentials, a query or a fragment: {value!r}")
    if parts.scheme == "https" and host:
        if not (host == "googleapis.com" or host.endswith(".googleapis.com")):
            # Allowed (private endpoints, test doubles) but bearer tokens go to this host.
            log.warning("%s: api_base %s is not a googleapis.com host; access tokens will be sent there", where, url)
        return url
    if parts.scheme == "http" and host in _LOOPBACK:
        return url  # local test doubles only
    raise ConfigError(f"{where}: api_base must be an https:// URL (http:// only for localhost): {value!r}")


def _opt_str(value: Any, key: str, where: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be a string")
    return value


def _bool(value: Any, key: str, where: str, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    raise ConfigError(f"{where}: {key} must be true or false, got {value!r}")


def _number(value: Any, key: str, where: str, lo: float, hi: float, cast: type) -> Any:
    try:
        n = cast(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: {key} must be a number, got {value!r}") from None
    if isinstance(value, bool) or not lo <= n <= hi:
        raise ConfigError(f"{where}: {key} must be between {lo} and {hi}, got {value!r}")
    return n


def _table(value: Any, where: str, allowed: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)} (allowed: {sorted(allowed)})")
    return value


def _str_list(value: Any, key: str, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: {key} must be a list of strings")
    return value


# ---------------------------------------------------------------------------
# model


@dataclass(frozen=True)
class CredSpec:
    """How to get a token. All None = the process's default credentials.

    auth: "auto" (ADC if available, else gcloud CLI), "adc" (google-auth only:
    SA key / workload identity / `gcloud auth application-default login`), or
    "gcloud" (the gcloud CLI's own login: `gcloud auth login` user accounts or
    `gcloud auth activate-service-account`).
    """

    auth: str = "auto"
    impersonate_service_account: str | None = None
    credentials_file: str | None = None
    gcloud_account: str | None = None

    def __post_init__(self) -> None:
        if self.auth not in AUTH_METHODS:
            raise ConfigError(f"auth must be one of {AUTH_METHODS}, got {self.auth!r}")
        if self.credentials_file and self.auth == "gcloud":
            raise ConfigError("credentials_file cannot be combined with auth = 'gcloud'")
        for key in ("impersonate_service_account", "gcloud_account"):
            v = getattr(self, key)
            if v is not None and not _SA_EMAIL.fullmatch(v):
                raise ConfigError(f"{key} must be an account email, got {v!r}")

    @property
    def is_default(self) -> bool:
        return self.auth == "auto" and not (self.impersonate_service_account or self.credentials_file or self.gcloud_account)

    def describe(self) -> str:
        via = {"auto": "", "adc": "ADC ", "gcloud": "gcloud "}[self.auth]
        if self.credentials_file:
            out = f"key file {os.path.basename(self.credentials_file)}"
        elif self.gcloud_account:
            out = f"{via or 'gcloud '}account {self.gcloud_account}"
        else:
            out = f"{via}default credentials" if via else "default credentials"
        if self.impersonate_service_account:
            out += f" -> impersonate {self.impersonate_service_account}"
        return out


@dataclass(frozen=True)
class OrgConfig:
    name: str
    tier: str = "default"
    aliases: tuple[str, ...] = ()
    description: str = ""
    api_base: str = DEFAULT_BASE_URL
    cred: CredSpec = field(default_factory=CredSpec)
    allow_kvm_values: bool = False


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _cred_from(merged: dict[str, Any], where: str) -> CredSpec:
    cred_file = _opt_str(merged.get("credentials_file"), "credentials_file", where)
    try:
        return CredSpec(
            auth=_opt_str(merged.get("auth"), "auth", where) or "auto",
            impersonate_service_account=_opt_str(merged.get("impersonate_service_account"), "impersonate_service_account", where),
            credentials_file=os.path.expanduser(cred_file) if cred_file else None,
            gcloud_account=_opt_str(merged.get("gcloud_account"), "gcloud_account", where),
        )
    except ConfigError as e:
        raise ConfigError(f"{where}: {e}") from None


class Registry:
    def __init__(
        self,
        orgs: list[OrgConfig],
        default_org: str | None = None,
        open_mode: bool = False,
        default_cred: CredSpec = CredSpec(),
        default_api_base: str = DEFAULT_BASE_URL,
        default_allow_kvm_values: bool = False,
        redact_secrets: bool = True,
        timeout_s: float = 30.0,
        max_parallel: int = 6,
        source: str = "env",
        tier_order: list[str] | None = None,
    ) -> None:
        self.orgs: dict[str, OrgConfig] = {}
        self._alias: dict[str, str] = {}
        for o in orgs:
            _check_name(o.name, "org name")
            _check_name(o.tier, f"tier for {o.name}")
            if o.name in self.orgs:
                raise ConfigError(f"Org {o.name!r} is defined twice")
            self.orgs[o.name] = o
        for o in orgs:
            for a in o.aliases:
                _check_name(a, f"alias of {o.name}")
                if (a in self.orgs and a != o.name) or (a in self._alias and self._alias[a] != o.name):
                    raise ConfigError(f"Alias {a!r} is ambiguous")
                self._alias[a] = o.name
        self.open_mode = open_mode
        self.default_cred = default_cred
        self.default_api_base = _check_api_base(default_api_base, "default api_base")
        self.default_allow_kvm_values = default_allow_kvm_values
        self.redact_secrets = redact_secrets
        self.timeout_s = _number(timeout_s, "timeout_s", "config", 1, 300, float)
        self.max_parallel = _number(max_parallel, "max_parallel", "config", 1, 32, int)
        self.source = source
        self._tier_order = list(tier_order or [])
        if default_org:
            try:
                self.default_org: str | None = self.resolve(default_org).name
            except UnknownOrg as e:
                raise ConfigError(f"default_org: {e}") from None
        else:
            self.default_org = None

    # ---- lookup --------------------------------------------------------

    def resolve(self, name: str | None) -> OrgConfig:
        if name is not None and not isinstance(name, str):
            raise UnknownOrg(f"Org must be a string, got {type(name).__name__}")
        if not name:
            if not self.default_org:
                raise UnknownOrg("No org given and no default org configured. Use list_configured_orgs to see choices.")
            name = self.default_org
        if name in self.orgs:
            return self.orgs[name]
        if name in self._alias:
            return self.orgs[self._alias[name]]
        if self.open_mode and _NAME.fullmatch(name):
            return OrgConfig(
                name=name,
                api_base=self.default_api_base,
                cred=self.default_cred,
                allow_kvm_values=self.default_allow_kvm_values,
            )
        known = ", ".join(sorted(self.orgs)) or "(none)"
        raise UnknownOrg(f"Org {name[:80]!r} is not configured. Known: {known}")

    @property
    def tiers(self) -> list[str]:
        """Tiers in promotion order: as defined in the config file, then any others alphabetically."""
        present = {o.tier for o in self.orgs.values()}
        ordered = [t for t in self._tier_order if t in present]
        return ordered + sorted(present - set(ordered))

    def tier_rank(self, tier: str) -> int:
        tiers = self.tiers
        return tiers.index(tier) if tier in tiers else len(tiers)

    def select(self, tier: str | None = None, orgs: list[str] | None = None) -> list[OrgConfig]:
        """Orgs matching a tier and/or an explicit list (names or aliases). Neither = all.

        An explicit list is de-duplicated and capped (MAX_FANOUT) before anything is resolved,
        so a caller can't turn one tool call into thousands of requests (open mode)."""
        if orgs:
            unique = list(dict.fromkeys(orgs))
            if len(unique) > MAX_FANOUT:
                raise UnknownOrg(f"Too many orgs in one call ({len(unique)}); at most {MAX_FANOUT}.")
            picked = [self.resolve(o) for o in unique]
        else:
            picked = list(self.orgs.values())
        if tier:
            if tier not in self.tiers:
                raise UnknownOrg(f"Unknown tier {tier[:40]!r}. Tiers: {', '.join(self.tiers) or '(none)'}")
            picked = [o for o in picked if o.tier == tier]
        if not picked:
            raise UnknownOrg("No orgs matched (in open mode, pass orgs explicitly).")
        seen: dict[str, OrgConfig] = {}
        for o in picked:
            seen.setdefault(o.name, o)
        return list(seen.values())

    # ---- construction --------------------------------------------------

    @classmethod
    def load(cls) -> "Registry":
        path = os.environ.get("APIGEE_CONFIG")
        reg = cls.from_toml(path) if path else cls.from_env()
        enabled = _csv(os.environ.get("APIGEE_TIERS"))
        if not enabled:
            return reg
        if reg.open_mode:
            raise ConfigError("APIGEE_TIERS needs a config file (APIGEE_CONFIG) or an org allowlist with tiers")
        unknown = set(enabled) - set(reg.tiers)
        if unknown:
            raise ConfigError(f"APIGEE_TIERS has unknown tiers: {sorted(unknown)} (known: {reg.tiers})")
        kept = [o for o in reg.orgs.values() if o.tier in enabled]
        default = reg.default_org if reg.default_org in {o.name for o in kept} else None
        return cls(
            kept,
            default_org=default,
            open_mode=False,
            default_cred=reg.default_cred,
            default_api_base=reg.default_api_base,
            default_allow_kvm_values=reg.default_allow_kvm_values,
            redact_secrets=reg.redact_secrets,
            timeout_s=reg.timeout_s,
            max_parallel=reg.max_parallel,
            source=reg.source + f" (tiers: {','.join(enabled)})",
            tier_order=reg._tier_order,
        )

    @classmethod
    def from_env(cls) -> "Registry":
        where = "environment"
        api_base = _check_api_base(os.environ.get("APIGEE_API_BASE") or DEFAULT_BASE_URL, "APIGEE_API_BASE")
        kvm = _bool(os.environ.get("APIGEE_ALLOW_KVM_VALUES"), "APIGEE_ALLOW_KVM_VALUES", where, False)
        cred = _cred_from(
            {
                "auth": os.environ.get("APIGEE_AUTH"),
                "gcloud_account": os.environ.get("APIGEE_GCLOUD_ACCOUNT"),
                "impersonate_service_account": os.environ.get("APIGEE_IMPERSONATE_SERVICE_ACCOUNT"),
                "credentials_file": os.environ.get("APIGEE_CREDENTIALS_FILE"),
            },
            where,
        )
        allowed = _csv(os.environ.get("APIGEE_ALLOWED_ORGS"))
        default = os.environ.get("APIGEE_ORG") or None
        if default and allowed and default not in allowed:
            raise ConfigError(f"APIGEE_ORG={default} is not in APIGEE_ALLOWED_ORGS")
        if default:
            _check_name(default, "APIGEE_ORG")
        orgs = [OrgConfig(name=_check_name(o, "org in APIGEE_ALLOWED_ORGS"), api_base=api_base, cred=cred, allow_kvm_values=kvm) for o in allowed]
        return cls(
            orgs,
            default_org=default,
            open_mode=not allowed,
            default_cred=cred,
            default_api_base=api_base,
            default_allow_kvm_values=kvm,
            redact_secrets=_bool(os.environ.get("APIGEE_REDACT_SECRETS"), "APIGEE_REDACT_SECRETS", where, True),
            timeout_s=_number(os.environ.get("APIGEE_TIMEOUT_S") or 30, "APIGEE_TIMEOUT_S", where, 1, 300, float),
            source="env",
        )

    @classmethod
    def from_toml(cls, path: str) -> "Registry":
        import tomllib

        full = os.path.expanduser(path)
        try:
            with open(full, "rb") as f:
                doc = tomllib.load(f)
        except FileNotFoundError:
            raise ConfigError(f"Config file not found: {full}") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Config file {full} is not valid TOML: {e}") from None
        except OSError as e:
            raise ConfigError(f"Cannot read config file {full}: {e}") from None
        return cls.from_dict(doc, source=full)

    @classmethod
    def from_dict(cls, doc: dict[str, Any], source: str = "dict") -> "Registry":
        _table(doc, "config", _TOP_KEYS)
        defaults = _table(doc.get("defaults"), "[defaults]", _DEFAULT_KEYS)
        tiers_raw = _table(doc.get("tiers"), "[tiers]", set(doc.get("tiers") or {}))
        orgs_raw = _table(doc.get("orgs"), "[orgs]", set(doc.get("orgs") or {}))

        tiers: dict[str, dict[str, Any]] = {}
        for tier_name, t in tiers_raw.items():
            _check_name(tier_name, "tier name")
            tiers[tier_name] = _table(t, f"[tiers.{tier_name}]", _TIER_KEYS)

        org_tables: dict[str, dict[str, Any]] = {}
        for org_name, t in orgs_raw.items():
            _check_name(org_name, "org name")
            org_tables[org_name] = dict(_table(t, f"[orgs.{org_name}]", _ORG_KEYS))

        # Tiers may list their orgs compactly: [tiers.prod] orgs = ["a", "b"]
        for tier_name, t in tiers.items():
            for org_name in _str_list(t.get("orgs"), "orgs", f"[tiers.{tier_name}]"):
                _check_name(org_name, f"org name in [tiers.{tier_name}]")
                existing = org_tables.setdefault(org_name, {})
                if existing.get("tier", tier_name) != tier_name:
                    raise ConfigError(f"Org {org_name!r} is listed under tier {tier_name!r} but says tier={existing['tier']!r}")
                existing["tier"] = tier_name

        default_api_base = _check_api_base(defaults.get("api_base", DEFAULT_BASE_URL), "[defaults]")
        orgs: list[OrgConfig] = []
        for name, t in org_tables.items():
            where = f"[orgs.{name}]"
            tier = t.get("tier", "default")
            if not isinstance(tier, str):
                raise ConfigError(f"{where}: tier must be a string")
            if tier != "default" and tier not in tiers:
                raise ConfigError(f"Org {name!r} uses undefined tier {tier!r}")
            merged: dict[str, Any] = {k: v for k, v in defaults.items() if k in _INHERITED}
            merged.update({k: v for k, v in tiers.get(tier, {}).items() if k in _INHERITED})
            merged.update({k: v for k, v in t.items() if k in _INHERITED})
            orgs.append(
                OrgConfig(
                    name=name,
                    tier=tier,
                    aliases=tuple(_str_list(t.get("aliases"), "aliases", where)),
                    description=_opt_str(t.get("description"), "description", where) or "",
                    api_base=_check_api_base(merged.get("api_base", default_api_base), where),
                    cred=_cred_from(merged, where),
                    allow_kvm_values=_bool(merged.get("allow_kvm_values"), "allow_kvm_values", where, False),
                )
            )
        return cls(
            orgs,
            default_org=_opt_str(doc.get("default_org"), "default_org", "config"),
            open_mode=False,
            default_cred=_cred_from(defaults, "[defaults]"),
            default_api_base=default_api_base,
            default_allow_kvm_values=_bool(defaults.get("allow_kvm_values"), "allow_kvm_values", "[defaults]", False),
            redact_secrets=_bool(doc.get("redact_secrets"), "redact_secrets", "config", True),
            timeout_s=_number(doc.get("timeout_s", 30), "timeout_s", "config", 1, 300, float),
            max_parallel=_number(doc.get("max_parallel", 6), "max_parallel", "config", 1, 32, int),
            source=source,
            tier_order=list(tiers),  # TOML table order = promotion order
        )
