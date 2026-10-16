# Copilot instructions for apigee-hybrid-mcp

This repo is a **read-only** MCP server for the Apigee Hybrid management API. The read-only guarantee is the core requirement. Never weaken it.

## Layout
- `src/apigee_mcp/server.py`: MCP tools (one async function per tool, registered with `@tool`)
- `src/apigee_mcp/client.py`: HTTP client. Path validation, GET-only enforcement, per-org routing, fan-out.
- `src/apigee_mcp/config.py`: org/tier registry (TOML or env vars)
- `src/apigee_mcp/auth.py`: token providers (ADC, SA key, gcloud CLI, impersonation)
- `src/apigee_mcp/redact.py`: secret masking
- `docs/design.md`: architecture and security model. Update it when behaviour changes.

## Rules for new or changed tools
1. Use `@tool` (it sets `readOnlyHint=True` and converts errors). Never call `mcp.tool` directly.
2. Call `client().get(...)` only. Do not add HTTP methods. `read_post` is limited to the `_READ_ONLY_POST_VERBS` allowlist, so don't extend that list without a design-doc change and review.
3. Pass every caller-supplied path piece through `seg(value, "what")`. Resolve orgs with `_o(org)` or `client().org(...)`.
4. Accept `org: str | None = None` and let the registry resolve names and aliases.
5. For cross-org tools, use `client().registry.select(tier=..., orgs=...)` with `client().for_each_org(...)`, so per-org failures stay inline.
6. Return `to_text(...)`, which handles truncation. Redaction happens in the client.
7. Never add Google API system parameters (`alt`, `callback`, `fields`, `format`, `$...`, `key`, `*_token`, `quotaUser`) to `_ALLOWED_PARAMS`. They can change the response format and slip past redaction. Never relax the non-JSON fail-closed rule or the debug-session data block.
8. Add tests in `tests/` against `httpx2.MockTransport`. The `tearDown` in `test_server.py` asserts that no non-GET request was sent, so keep that pattern.

## Conventions
- Python 3.11+, MCP SDK 2.x: `from mcp.server.mcpserver import MCPServer`. `FastMCP` no longer exists.
- HTTP library: `httpx2` (a dependency of `mcp`). Don't add `httpx` or `requests` to the core install.
- Run tests with `python -m unittest discover -s tests -v`.
