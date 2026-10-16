# Using with GitHub Copilot

You can connect three Copilot surfaces to this server:

| Surface | Where it runs | Identity | Config |
|---|---|---|---|
| VS Code Copilot Chat (agent mode) | your machine | your gcloud login / ADC | `.vscode/mcp.json` |
| Copilot CLI | your machine | your gcloud login / ADC | `~/.copilot/mcp-config.json` |
| Copilot coding agent (cloud) | GitHub Actions runner | GCP workload identity federation | repo **Settings → Copilot → MCP servers**, plus `.github/workflows/copilot-setup-steps.yml` |

Every tool sets `readOnlyHint: true`. Copilot code review only uses MCP tools that do.

## 1. VS Code (agent mode)

Install the server once. Run the install from the repo checkout:

```bash
python3 -m venv ~/.venvs/apigee-mcp
~/.venvs/apigee-mcp/bin/pip install -e '.[google]'
gcloud auth login
```

Then add `.vscode/mcp.json` to whatever workspace you want it in. This repo includes one:

```json
{
  "servers": {
    "apigee-hybrid": {
      "type": "stdio",
      "command": "${userHome}/.venvs/apigee-mcp/bin/apigee-hybrid-mcp",
      "env": {
        "APIGEE_CONFIG": "${userHome}/.config/apigee-mcp/apigee-orgs.toml",
        "APIGEE_TIERS": "sandbox,nonprod"
      }
    }
  }
}
```

1. Open Copilot Chat, switch to **Agent** mode, and start the server. You can start it from the CodeLens in `mcp.json` or with **MCP: List Servers**.
2. Use **Configure Tools** in the chat input to enable the `apigee-hybrid` tools.
3. Ask, for example: *"Using apigee-hybrid, which revision of `orders` is deployed in each nonprod org?"*

To also reach preprod and prod, add a second entry with its own tier filter. The prod-like tiers then stay a deliberate choice in the tool picker:

```json
    "apigee-hybrid-prod": {
      "type": "stdio",
      "command": "${userHome}/.venvs/apigee-mcp/bin/apigee-hybrid-mcp",
      "env": {
        "APIGEE_CONFIG": "${userHome}/.config/apigee-mcp/apigee-orgs.toml",
        "APIGEE_TIERS": "preprod,prod"
      }
    }
```

To add it to your user profile instead of a workspace:

```bash
code --add-mcp '{"name":"apigee-hybrid","command":"'"$HOME"'/.venvs/apigee-mcp/bin/apigee-hybrid-mcp","env":{"APIGEE_CONFIG":"'"$HOME"'/.config/apigee-mcp/apigee-orgs.toml"}}'
```

**Notes**
- Keep the real `apigee-orgs.toml` outside the repo, or gitignored. It holds SA emails and org names, not secrets, but there's no reason to publish your topology.
- `.mcp.json` at the repo root, with an `mcpServers` key, is the portable variant that other clients also read.

## 2. Copilot CLI

`~/.copilot/mcp-config.json`:

```json
{
  "mcpServers": {
    "apigee-hybrid": {
      "type": "local",
      "command": "/home/you/.venvs/apigee-mcp/bin/apigee-hybrid-mcp",
      "args": [],
      "env": { "APIGEE_CONFIG": "/home/you/.config/apigee-mcp/apigee-orgs.toml" },
      "tools": ["*"]
    }
  }
}
```

## 3. Copilot coding agent (cloud)

The coding agent runs in an Actions runner and uses MCP tools without asking for approval. So:
- give it its own GCP identity
- limit it to sandbox and nonprod (no preprod or prod)
- allowlist the tools it may use

### a. GCP: workload identity federation (no keys)

```bash
PROJECT=acme-apigee-ci            # project that hosts the pool
POOL=github ; PROVIDER=github
REPO=acme/apigee-proxies          # the repo Copilot works in
SA=apigee-mcp-copilot@$PROJECT.iam.gserviceaccount.com

gcloud iam workload-identity-pools create $POOL --project=$PROJECT --location=global
gcloud iam workload-identity-pools providers create-oidc $PROVIDER --project=$PROJECT \
  --location=global --workload-identity-pool=$POOL \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='$REPO'"

gcloud iam service-accounts create apigee-mcp-copilot --project=$PROJECT
POOL_ID=$(gcloud iam workload-identity-pools describe $POOL --project=$PROJECT --location=global --format='value(name)')
gcloud iam service-accounts add-iam-policy-binding $SA --project=$PROJECT \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/$POOL_ID/attribute.repository/$REPO"

# read-only on the non-prod orgs only
for ORG in acme-sbx-01 acme-dev-us acme-qa-us; do
  gcloud projects add-iam-policy-binding $ORG --member="serviceAccount:$SA" --role=roles/apigee.readOnlyAdmin --condition=None
done
```

### b. Setup steps workflow

Copy [`.github/workflows/copilot-setup-steps.yml`](../.github/workflows/copilot-setup-steps.yml) into the repo Copilot works in. It runs before the agent starts and does three things:
- installs the server
- authenticates to GCP with OIDC, writing an ADC credentials file
- writes the org config

Create these variables in that repo's `copilot` environment (Settings → Environments → copilot):
- `GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_SERVICE_ACCOUNT`
- `APIGEE_ORGS_TOML`, holding the TOML text for the non-prod orgs
- `APIGEE_MCP_SOURCE`, the pip source for this server, for example `git+https://github.com/<owner>/apigee-hybrid-mcp@v0.2.0`

All four are variables, not secrets.

### c. MCP configuration

Repo **Settings → Copilot → MCP servers → MCP configuration**:

```json
{
  "mcpServers": {
    "apigee-hybrid": {
      "type": "local",
      "command": "/home/runner/apigee-mcp/bin/apigee-hybrid-mcp",
      "args": [],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/home/runner/gcp-adc.json",
        "APIGEE_CONFIG": "/home/runner/apigee-orgs.toml",
        "APIGEE_TIERS": "sandbox,nonprod",
        "APIGEE_AUTH": "adc"
      },
      "tools": [
        "list_configured_orgs",
        "list_environments",
        "list_api_proxies",
        "get_api_proxy",
        "get_api_proxy_revision",
        "list_deployments",
        "get_deployment_status",
        "find_api_deployments",
        "across_orgs",
        "list_env_resources",
        "get_target_server",
        "list_sharedflows",
        "get_sharedflow",
        "get_flowhook",
        "list_api_products",
        "get_api_product"
      ]
    }
  }
}
```

The `tools` list deliberately leaves out these, because the agent's logs and PRs are visible to everyone who can see the repo:
- `query_stats`, `list_debug_sessions`, `list_apps`, `get_app`, `list_developers`, `get_developer` (business and consumer data)
- `apigee_get` (unbounded)
- `list_kvm_entries`

How the agent gets credentials:
- The setup workflow runs `google-github-actions/auth@v3` with workload identity federation.
- It moves the generated ADC file to `/home/runner/gcp-adc.json`. This keeps the file out of the checkout, so the agent can't commit it, and gives it a fixed path.
- The MCP `env` above points `GOOGLE_APPLICATION_CREDENTIALS` at that path.
- The setup job ends with `apigee-hybrid-mcp --check`, so broken credentials fail before the agent starts rather than mid-task.

### d. Try it

Assign an issue to Copilot, for example:

> Our `orders` proxy in nonprod is one revision behind sandbox. Use the apigee-hybrid MCP tools to confirm the deployed revisions, diff the proxy bundles in this repo between those revisions, and summarise what changed.

## Repo instructions for Copilot

[`.github/copilot-instructions.md`](../.github/copilot-instructions.md) tells Copilot how this repository is laid out, and the rules any new tool must follow (read-only, `seg()` on every argument, tests).
