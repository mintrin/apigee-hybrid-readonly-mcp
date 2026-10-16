# Security and production-readiness audit: v0.3.0

**Date:** 2026-10-15
**Scope:** `src/apigee_mcp/*` (server, client, config, auth, redact), packaging, tests
**Method:** a line-by-line review, plus an independent adversarial review by a separate agent. That agent had not seen this review. It wrote and ran exploit attempts against the code, capturing real outgoing requests through a mock transport. Every finding below was fixed, then guarded by a regression test in `tests/test_hardening.py`. Each guard was mutation-checked: removing the fix makes a test fail.

Severity reflects impact on the core guarantees:
1. read-only
2. only configured orgs and hosts
3. no secrets reach the model
4. no crash from model-controlled input

## Findings

| # | Sev | Area | Finding | Fix |
|---|---|---|---|---|
| 1 | High | Config | Unknown keys in `[defaults]`, `[tiers.*]` and `[orgs.*]` were silently ignored. A typo such as `impersonate_serviceaccount` made that tier fall back to the default (often broader, personal) credential. | Every table has a strict key allowlist, and unknown keys are a startup error. |
| 2 | High | Client | Path segments after the org in `apigee_get` / `across_orgs` were only checked for `:`, `?`, `#`, `%`. Characters such as `;`, unicode look-alikes and control characters reached the URL. | Every segment is validated against a strict charset in `_check_path`, and encoded exactly once. |
| 3 | High | Client | Query-parameter names passed through unvalidated in `apigee_get` and `across_orgs`, including method-override-style and credential parameters. | Name allowlist (identifiers only), a denylist for override and credential parameters, and type and length limits on values. |
| 4 | Medium | Redaction | Apigee name/value attributes (`{"name": "client_secret", "value": "..."}`) were not masked, because only key names were checked. | Values of pairs whose name looks secret are masked. |
| 5 | Medium | Redaction | PEM private keys in free text, non-JSON bodies (resource files) and error messages were not masked. | `mask_pem()` runs on every JSON string, text body and error message. |
| 6 | Medium | Redaction | KVM masking only ran on paths containing `/entries`, and only at the top level. | `value` fields are masked at any depth, on any path with a `keyvaluemaps` segment. |
| 7 | Medium | Client | Redirects: a 3xx could have been followed, carrying the bearer token. | `follow_redirects=False`, and a 3xx is reported as an error. |
| 8 | Medium | Config | `api_base` accepted any scheme and host, so tokens could be sent over plain HTTP. | `https://` is required (`http://` only for loopback). Userinfo, query and fragment are rejected. Non-googleapis hosts are logged as a warning. |
| 9 | Medium | Reliability | Network errors (timeouts, resets), invalid JSON, gcloud timeouts and empty gcloud output raised uncaught exceptions. The model saw a generic crash, and one such error sank a whole cross-org fan-out. | Each case maps to a typed tool error. The fan-out also isolates unexpected exceptions per org. |
| 10 | Medium | Reliability | No retries, so transient 429 and 5xx responses failed the call. An expired or revoked token failed until the 45-minute cache TTL. | Bounded retries with backoff, jitter and `Retry-After`. A 401 triggers one token invalidation and retry. |
| 11 | Medium | MCP | Errors were returned as normal results starting with "Error:", so clients couldn't tell failure from success. | Errors are raised as `ToolError`, giving `isError: true`. |
| 12 | Low | MCP | Numeric arguments were unbounded (`count`, `rows`, `page_size`, `limit`, `warn_days`). | Pydantic bounds in the tool schema, so they are rejected before any request. |
| 13 | Low | Reliability | Response bodies were unbounded in memory. | Streamed with a byte cap (`APIGEE_MAX_RESPONSE_BYTES`). |
| 14 | Low | Config | `APIGEE_TIERS` in open mode silently disabled every org. `default_allow_kvm_values` was dropped by the tier filter. Missing or invalid files raised raw exceptions. | Clear `ConfigError`s. Settings are carried through. The server fails fast at startup (exit 2). |
| 15 | Low | Tools | `check_certificate_expiry` fetched keystores sequentially, one bad alias failed the whole scan, and a non-numeric expiry crashed it. | Concurrent and bounded. Per-alias errors are listed, and bad dates are marked `UNKNOWN`. |
| 16 | Low | Tools | `find_api_deployments` detected "not found" by string-matching the error text. | Uses the structured HTTP status. |
| 17 | Low | Ops | No request log, no version flag, logs sometimes without timestamps, and the HTTP client was never closed. | One log line per call on stderr (never the token), `--version`, a forced log format, and a lifespan close. |
| 18 | Low | Packaging | Direct imports (`httpx2`, `pydantic`, `mcp-types`) relied on transitive dependencies. The version was duplicated in three places. | Explicit dependencies with upper bounds. The version is single-sourced in `__init__.py`. |

Findings from the independent review that are not covered above are listed in the next section.

## Independent review

The second reviewer worked from the guarantees alone and wrote exploit scripts. It confirmed that guarantees 1, 2 and 4 held under every attack it tried:
- method tricks (`$httpMethod`, override parameters)
- path traversal and encoding tricks (`..`, `%2F`, unicode, `;`, verbs in any segment)
- unknown orgs and aliases
- redirects
- token leakage in errors and logs

It broke guarantee 3 (masking) in several ways. All of these are now fixed, and each reproduction is a test in `IndependentReviewTests` / `ToolBehaviourTests`:

| # | Sev | Finding | Fix |
|---|---|---|---|
| R1 | **High** | Google API system parameters bypassed redaction entirely. `callback=x` (JSONP), `alt=proto`/`sse` and `fields=` made the response non-JSON or stripped the `name` keys that redaction relies on. Raw consumer secrets and KVM values came back through `apigee_get` and `across_orgs`. | Query parameters are now an **allowlist** of Apigee list, filter and analytics parameters. Separately, **non-JSON responses fail closed** everywhere except resource-file endpoints. |
| R2 | **High** | Debug-session transaction data (`.../debugsessions/{s}/data/{t}`) contains live request headers, bodies, URIs and variables (bearer tokens, passwords, private variables). | That path is refused outright, case-insensitively. Listing sessions is still allowed. |
| R3 | Medium | Resource files and proxy bundles were returned raw: `db.password=...` in a properties file, and hard-coded `Authorization` in policy XML inside a `format=bundle` ZIP. | `format` is no longer an allowed parameter. Resource-file text goes through a free-text scan: PEM, `Basic`/`Bearer`, `secret_key=value` and `<SecretName>value</SecretName>`. |
| R4 | Medium | Secret-name heuristics were too narrow. The metadata allowlist overrode secret matches (`truststore_password`). Several names were missed (`hmacKey`, `access_key`, `basic_auth`, `cookie`, `client_id`). Non-string values were never masked. JWK private members and PGP keys passed through. | Secret patterns are checked first, and the allowlist is exact names only. Patterns were broadened, and the whole subtree under a secret key is masked. JWK `d/p/q/dp/dq/qi/k` are masked, and the PEM regex is generic. |
| R5 | Medium | `query_stats(dimensions="client_id")` returned consumer keys as dimension values. | Values of credential dimensions are masked by position, or every part is masked when positions can't be matched. |
| R6 | Low | KVM path detection was case-sensitive (`keyValueMaps`). | Case-insensitive. |
| R7 | Low | In open mode, `across_orgs(orgs=[2000 names])` sent 8,000 requests. | Explicit org lists are de-duplicated and capped at 50 before anything is resolved. |
| R8 | Low | A non-dict `attachments` response and very deeply nested JSON produced crashes rather than tool errors. | Type checks, and `RecursionError` becomes `ApigeeError`. |
| R9 | Low | API error messages were echoed unmasked. | They go through the free-text scan and are truncated to 500 characters. **Residual:** a message that quotes a bare value with no secret-looking key next to it can't be detected. |

My own fixes had one regression, which the new tests caught: the stricter segment check rejected multi-dimension stats paths (`stats/apiproxy,client_id`). Commas are now allowed only in the segment after `stats`, and each part is validated.

All findings were re-checked by running the reviewer's original exploit scripts against the fixed code. Every leak and bypass is closed except the residual noted under R9.

## Residual risks and recommendations

- **IAM is the real boundary.** Run each tier as an SA holding only `roles/apigee.readOnlyAdmin`. The code-level guards are defense in depth.
- **Business data.** Analytics (`query_stats`), debug sessions and developer/app listings can contain business or personal data. Leave these out of the Copilot coding agent's tool allowlist (as the docs do), and restrict who can use the prod instance.
- **HTTP transport** has no client authentication. Keep it on loopback, or front it with IAP or an authenticating proxy. The server warns if it is bound elsewhere.
- **Redaction is heuristic.** It catches:
  - secret-named keys and attributes
  - JWKs
  - PEM/PGP private keys
  - auth headers
  - `key=value` text
  - KVM values
  - credential analytics dimensions

  A secret stored under an innocuous name, or quoted in an error message with nothing secret-looking next to it, can still pass through.
- **Allowed query parameters:** `apigee_get` and `across_orgs` accept only the parameters listed in `client._ALLOWED_PARAMS`. If you need another Apigee parameter, add it there with a test, and never add a Google system parameter (`alt`, `callback`, `fields`, `$...`, `key`, `*_token`, `quotaUser`, `userProject`).
- **Not verified in this environment:** live calls against `apigee.googleapis.com`, google-auth refresh against real credentials, and `pip install` / wheel build (the build backend wasn't installable offline). CI (`.github/workflows/ci.yml`) covers install and tests, and `apigee-hybrid-mcp --check` covers live access.
