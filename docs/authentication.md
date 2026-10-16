# Authentication

The server supports service accounts and user credentials, through google-auth (ADC) or the gcloud CLI. Each tier, or each org, can use a different identity.

## Supported identities

| Identity | How it's obtained | Config |
|---|---|---|
| **Your user login (gcloud CLI)** | `gcloud auth login` | `auth = "gcloud"` |
| A specific gcloud account, when several are logged in | `gcloud auth login you@acme.com` | `gcloud_account = "you@acme.com"` |
| **Your user login (ADC)** | `gcloud auth application-default login` | `auth = "adc"` (or `auto`) |
| **Service account, JSON key** | key file | `credentials_file = "~/keys/reader.json"` |
| Service account activated in gcloud | `gcloud auth activate-service-account --key-file=...` | `auth = "gcloud"`, `gcloud_account = "sa@..."` |
| **Service account via impersonation** (recommended) | your user or SA has `roles/iam.serviceAccountTokenCreator` on the target SA | `impersonate_service_account = "sa@..."` (combine with any of the above) |
| Workload identity, GKE/GCE metadata, GitHub OIDC | ambient ADC | `auth = "adc"` |
| Raw token for a quick test | `export APIGEE_ACCESS_TOKEN=$(gcloud auth print-access-token)` | default credential only, ~1 hour |

`auth = "auto"` is the default. It uses ADC when google-auth is installed and ADC is configured, and otherwise falls back to the gcloud CLI. Set `auth = "gcloud"` to always use your CLI login, even on a machine that also has ADC.

## Recommended setups

### Workstation: your login, one reader SA per tier

You log in once. The server impersonates a different read-only SA for each tier, so your personal IAM grants don't decide what the assistant can see.

```toml
[defaults]
auth = "gcloud"            # use `gcloud auth login`

[tiers.sandbox]
impersonate_service_account = "apigee-mcp-reader@acme-apigee-sandbox.iam.gserviceaccount.com"
orgs = ["acme-sbx-01", "acme-sbx-02"]

[tiers.prod]
impersonate_service_account = "apigee-mcp-reader@acme-apigee-prod.iam.gserviceaccount.com"
orgs = ["acme-prod-us-west", "acme-prod-eu-west"]
```

```bash
gcloud auth login
APIGEE_CONFIG=~/apigee-orgs.toml apigee-hybrid-readonly-mcp --check
```

### Workstation: just your own access

```bash
gcloud auth login            # or: gcloud auth application-default login
APIGEE_AUTH=gcloud APIGEE_ALLOWED_ORGS=org-a,org-b apigee-hybrid-readonly-mcp --check
```

The assistant gets exactly your Apigee permissions. The code still blocks writes, but IAM no longer backs that up. Prefer impersonating a reader SA.

### CI / Copilot coding agent / servers: service account

Use workload identity federation (no keys) with `auth = "adc"`. See [github-copilot.md](github-copilot.md). Fall back to `credentials_file` only if federation isn't possible.

## IAM setup

For each project (org) a tier covers:

```bash
SA=apigee-mcp-reader@acme-apigee-prod.iam.gserviceaccount.com
for ORG in acme-prod-us-west acme-prod-us-east acme-prod-eu-west; do
  gcloud projects add-iam-policy-binding "$ORG" \
    --member="serviceAccount:$SA" --role="roles/apigee.readOnlyAdmin" --condition=None
done

# who may impersonate it (you, your team group, or a CI identity)
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --member="group:apigee-platform@acme.com" --role="roles/iam.serviceAccountTokenCreator"
```

The loop assumes the Hybrid org name matches its GCP project ID, which is the default. If yours differ, use the project IDs.

## Env-var equivalents (no config file)

| Variable | Same as |
|---|---|
| `APIGEE_AUTH` | `auth` |
| `APIGEE_IMPERSONATE_SERVICE_ACCOUNT` | `impersonate_service_account` |
| `APIGEE_CREDENTIALS_FILE` | `credentials_file` |
| `APIGEE_GCLOUD_ACCOUNT` | `gcloud_account` |
| `APIGEE_ACCESS_TOKEN` | static token (default credential only) |

## Verify

```bash
apigee-hybrid-readonly-mcp --check
```

Expected output:

```text
config: /home/minh/apigee-orgs.toml  default org: acme-sbx-01  tiers: sandbox, nonprod, preprod, prod
  [nonprod   ] acme-dev-us        gcloud default credentials -> impersonate apigee-mcp-reader@...   OK (HYBRID)
  [preprod   ] acme-preprod-us    gcloud default credentials -> impersonate apigee-mcp-reader@...   OK (HYBRID)
  [prod      ] acme-prod-us-west  gcloud default credentials -> impersonate apigee-mcp-reader@...   OK (HYBRID)
  ...
20/20 orgs reachable
```

It exits non-zero if any org fails, so you can use it as a CI smoke test.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Reauthentication required` | Your gcloud login expired. Run `gcloud auth login`. |
| `403 ... iam.serviceAccounts.getAccessToken` | You lack `serviceAccountTokenCreator` on the tier SA. |
| `403 ... apigee.organizations.get` | The tier SA lacks `apigee.readOnlyAdmin` in that org's project. |
| `404` for one org only | The org name in the config is wrong, or the org lives on a data-residency endpoint (set `api_base` for it). |
| ADC used when you wanted your CLI login | Set `auth = "gcloud"` (`auto` prefers ADC). |
