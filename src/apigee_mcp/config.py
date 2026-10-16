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
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE_URL = "https://apigee.googleapis.com/v1"
_NAME = re.compile(r"^[A-Za-z0-9._\-]{1,255}$")
_INHERITED = ("api_base", "auth", "impersonate_service_account", "credentials_file", "gcloud_account", "allow_kvm_values")
AUTH_METHODS = ("auto", "adc", "gcloud")


class ConfigError(ValueError):
    pass


class UnknownOrg(ValueError):
    pass


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


def _truthy(v: Any, default: bool) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


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
    ) -> None:
        self.orgs: dict[str, OrgConfig] = {}
        self._alias: dict[str, str] = {}
        for o in orgs:
            if not _NAME.fullmatch(o.name):
                raise ConfigError(f"Invalid org name {o.name!r}")
            if o.name in self.orgs:
                raise ConfigError(f"Org {o.name!r} is defined twice")
            self.orgs[o.name] = o
        for o in orgs:
            for a in o.aliases:
                if a in self.orgs and a != o.name or a in self._alias and self._alias[a] != o.name:
                    raise ConfigError(f"Alias {a!r} is ambiguous")
                self._alias[a] = o.name
        self.open_mode = open_mode
        self.default_cred = default_cred
        self.default_api_base = default_api_base.rstrip("/")
        self.default_allow_kvm_values = default_allow_kvm_values
        self.redact_secrets = redact_secrets
        self.timeout_s = timeout_s
        self.max_parallel = max_parallel
        self.source = source
        self.default_org = self.resolve(default_org).name if default_org else None

    # ---- lookup --------------------------------------------------------

    def resolve(self, name: str | None) -> OrgConfig:
        if not name:
            if not self.default_org:
                raise UnknownOrg(
                    "No org given and no default org configured. Use list_configured_orgs to see choices."
                )
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
        raise UnknownOrg(f"Org {name!r} is not configured. Known: {', '.join(sorted(self.orgs)) or '(none)'}")

    @property
    def tiers(self) -> list[str]:
        return sorted({o.tier for o in self.orgs.values()})

    def select(self, tier: str | None = None, orgs: list[str] | None = None) -> list[OrgConfig]:
        """Orgs matching a tier and/or an explicit list (names or aliases). Neither = all."""
        if orgs:
            picked = [self.resolve(o) for o in orgs]
        else:
            picked = list(self.orgs.values())
        if tier:
            if tier not in self.tiers:
                raise UnknownOrg(f"Unknown tier {tier!r}. Tiers: {', '.join(self.tiers)}")
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
        if enabled:
            unknown = set(enabled) - set(reg.tiers)
            if unknown and not reg.open_mode:
                raise ConfigError(f"APIGEE_TIERS has unknown tiers: {sorted(unknown)}")
            kept = [o for o in reg.orgs.values() if o.tier in enabled]
            default = reg.default_org if reg.default_org in {o.name for o in kept} else None
            reg = cls(
                kept,
                default_org=default,
                open_mode=False,
                default_cred=reg.default_cred,
                default_api_base=reg.default_api_base,
                redact_secrets=reg.redact_secrets,
                timeout_s=reg.timeout_s,
                max_parallel=reg.max_parallel,
                source=reg.source + f" (tiers: {','.join(enabled)})",
            )
        return reg

    @classmethod
    def from_env(cls) -> "Registry":
        api_base = os.environ.get("APIGEE_API_BASE", DEFAULT_BASE_URL).rstrip("/")
        kvm = _truthy(os.environ.get("APIGEE_ALLOW_KVM_VALUES"), False)
        cred = CredSpec(
            auth=os.environ.get("APIGEE_AUTH", "auto") or "auto",
            gcloud_account=os.environ.get("APIGEE_GCLOUD_ACCOUNT") or None,
            impersonate_service_account=os.environ.get("APIGEE_IMPERSONATE_SERVICE_ACCOUNT") or None,
            credentials_file=os.environ.get("APIGEE_CREDENTIALS_FILE") or None,
        )
        allowed = _csv(os.environ.get("APIGEE_ALLOWED_ORGS"))
        default = os.environ.get("APIGEE_ORG") or None
        if default and allowed and default not in allowed:
            raise ConfigError(f"APIGEE_ORG={default} is not in APIGEE_ALLOWED_ORGS")
        orgs = [OrgConfig(name=o, api_base=api_base, cred=cred, allow_kvm_values=kvm) for o in allowed]
        return cls(
            orgs,
            default_org=default,
            open_mode=not allowed,
            default_cred=cred,
            default_api_base=api_base,
            default_allow_kvm_values=kvm,
            redact_secrets=_truthy(os.environ.get("APIGEE_REDACT_SECRETS"), True),
            timeout_s=float(os.environ.get("APIGEE_TIMEOUT_S", "30")),
            source="env",
        )

    @classmethod
    def from_toml(cls, path: str) -> "Registry":
        import tomllib

        with open(os.path.expanduser(path), "rb") as f:
            doc = tomllib.load(f)
        return cls.from_dict(doc, source=path)

    @classmethod
    def from_dict(cls, doc: dict[str, Any], source: str = "dict") -> "Registry":
        unknown_top = set(doc) - {"default_org", "defaults", "tiers", "orgs", "redact_secrets", "timeout_s", "max_parallel"}
        if unknown_top:
            raise ConfigError(f"Unknown top-level keys in config: {sorted(unknown_top)}")
        defaults: dict[str, Any] = {"api_base": DEFAULT_BASE_URL, "allow_kvm_values": False, **doc.get("defaults", {})}
        tiers: dict[str, dict[str, Any]] = doc.get("tiers", {})
        org_tables: dict[str, dict[str, Any]] = dict(doc.get("orgs", {}))

        # Tiers may list their orgs compactly: [tiers.prod] orgs = ["a", "b"]
        for tier_name, t in tiers.items():
            for org_name in t.get("orgs", []):
                existing = org_tables.setdefault(org_name, {})
                if existing.get("tier", tier_name) != tier_name:
                    raise ConfigError(f"Org {org_name!r} is listed under tier {tier_name!r} but says tier={existing['tier']!r}")
                existing.setdefault("tier", tier_name)

        orgs: list[OrgConfig] = []
        for name, t in org_tables.items():
            tier = t.get("tier", "default")
            if tier != "default" and tier not in tiers:
                raise ConfigError(f"Org {name!r} uses undefined tier {tier!r}")
            merged = {k: v for k, v in defaults.items() if k in _INHERITED}
            merged.update({k: v for k, v in tiers.get(tier, {}).items() if k in _INHERITED})
            merged.update({k: v for k, v in t.items() if k in _INHERITED})
            orgs.append(
                OrgConfig(
                    name=name,
                    tier=tier,
                    aliases=tuple(t.get("aliases", [])),
                    description=t.get("description", ""),
                    api_base=str(merged.get("api_base", DEFAULT_BASE_URL)).rstrip("/"),
                    cred=CredSpec(
                        auth=str(merged.get("auth", "auto")),
                        impersonate_service_account=merged.get("impersonate_service_account"),
                        credentials_file=os.path.expanduser(merged["credentials_file"]) if merged.get("credentials_file") else None,
                        gcloud_account=merged.get("gcloud_account"),
                    ),
                    allow_kvm_values=_truthy(merged.get("allow_kvm_values"), False),
                )
            )
        default_cred = CredSpec(
            auth=str(defaults.get("auth", "auto")),
            impersonate_service_account=defaults.get("impersonate_service_account"),
            credentials_file=defaults.get("credentials_file"),
            gcloud_account=defaults.get("gcloud_account"),
        )
        return cls(
            orgs,
            default_org=doc.get("default_org"),
            open_mode=False,
            default_cred=default_cred,
            default_api_base=defaults["api_base"],
            redact_secrets=_truthy(doc.get("redact_secrets"), True),
            timeout_s=float(doc.get("timeout_s", 30)),
            max_parallel=int(doc.get("max_parallel", 6)),
            source=source,
        )
