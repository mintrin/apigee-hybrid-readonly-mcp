# Changelog

## 0.3.0: production hardening

Security and the read-only guarantee:
- Every segment of every request path is validated against a strict charset, including paths passed to `apigee_get` and `across_orgs`. Percent-escapes, `;`, unicode look-alikes, control characters and leading or trailing spaces are rejected before any request is sent.
- Query-parameter names are validated. Method-override and credential parameters (`$httpMethod`, `method`, `access_token`, `key`, upload parameters) and dotted or `$`-prefixed names are refused. Values must be strings or numbers of bounded length.
- The POST allowlist is checked again at the lowest level (`_send`), not only in `read_post`.
- HTTP redirects are never followed, so a bearer token can't be forwarded to another host.
- `api_base` must be `https://` (or `http://` only for localhost). A non-`googleapis.com` host is logged as a warning.
- Redaction now also covers:
  - Apigee name/value attributes such as `{"name": "client_secret", "value": ...}`
  - PEM private-key blocks in any string, in non-JSON bodies, and in error messages
  - KVM `value` fields at any depth on any `keyvaluemaps` path

Reliability:
- Retries with exponential backoff and jitter on 429, 5xx and network errors, honoring `Retry-After`.
- A 401 triggers one token refresh (`TokenProvider.invalidate()`) and a retry.
- Response bodies are capped (`APIGEE_MAX_RESPONSE_BYTES`). Invalid JSON is reported as an error instead of crashing.
- Network failures, gcloud timeouts and empty gcloud output are reported as tool errors instead of crashes.
- Cross-org fan-out isolates unexpected per-org failures, and per-org errors include the HTTP status.
- `check_certificate_expiry` scans keystores concurrently, reports unreadable aliases under `errors`, and marks unparseable expiry dates `UNKNOWN`.

MCP behavior:
- Failures are returned as MCP tool errors (`isError: true`), not as success text that starts with "Error:".
- Numeric arguments are bounded: page sizes 1–1000, stats limit 1–14400, `warn_days` 0–3650.
- The HTTP client is closed on shutdown.

Configuration (fails closed):
- Unknown keys in `[defaults]`, `[tiers.*]` and `[orgs.*]` are errors, so a mistyped credential key can't fall back to a broader identity.
- Types and ranges are validated. Account emails are format-checked. A missing file or invalid TOML is a clear `ConfigError`.
- `APIGEE_TIERS` without a config file is an error; previously it silently disabled every org.
- The server validates its config at startup and exits with code 2 and a clear message on error.

Operations:
- One structured log line per API call (method, org, path, status, latency, attempt), on stderr with timestamps. Tokens are never logged.
- Startup summary, plus warnings for open mode, disabled redaction, and an HTTP transport bound to a non-loopback address.
- Added the `--version` flag. The version is single-sourced in `apigee_mcp.__version__`.
- Direct dependencies are declared explicitly, with upper bounds.

## 0.2.1
- Added a `preprod` tier. Tiers are reported in promotion order.

## 0.2.0
- Multi-org registry (TOML) with tiers, aliases and per-tier credentials. Cross-org tools.
- Auth options: gcloud CLI, ADC, SA key file, impersonation. Design docs, Copilot integration.

## 0.1.0
- Initial read-only server.
