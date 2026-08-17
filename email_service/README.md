# AppPilot Email Relay

Minimal, server-side email API. It accepts an already-generated AppPilot test
report and sends it from ONE fixed, service-owned sender via **Azure
Communication Services (ACS) Email**. AppPilot end-users provide nothing related
to email and never authenticate interactively.

```
AppPilot CLI  ──HTTPS + X-API-Key──▶  this relay (Container App)
                                          │  fixed sender; recipient from request
                                          │  (validated) or configured default
                                          │  ACS connection string (server secret)
                                          ▼
                                    ACS Email  ──▶  recipient
```

## Why ACS (not Microsoft Graph)

The subscription/tenant is a personal Azure directory with **no Exchange Online
mailboxes and no M365 licenses**. Graph app-only mail would require a licensed
M365 mailbox, an Entra app registration, admin consent, and an Exchange
`ApplicationAccessPolicy` to scope the mailbox. ACS Email needs none of that: a
pure Azure PaaS resource with a **free, auto-verified Azure-managed domain**
(no DNS, no mailbox, no user, no admin consent). Simpler and safer here.

## Provisioned resources (resource group `rg-apppilot-email`)

| Resource | Name | Purpose |
| --- | --- | --- |
| Email Communication Service | `apppilot-email` | Email sending service |
| Azure-managed domain | `AzureManagedDomain` | Verified fixed sender (no DNS) |
| Communication Service | `apppilot-acs` | ACS resource (linked to domain) |
| Container Registry (Basic) | `apppilotrelay<sub8>` | Hosts the relay image |
| Log Analytics workspace | `workspace-...` | Container Apps logs |
| Container Apps environment | `apppilot-env` | Serverless host env |
| Container App | `apppilot-email-api` | The relay API (scale-to-zero) |
| Storage Account (Standard_LRS) | `apppilottelemetry` | Table Storage for telemetry |
| Table | `SuiteRuns` | One row per suite run (telemetry) |

Fixed sender: `DoNotReply@05294377-b44f-48d0-a7a6-9e78ae5ad6a0.azurecomm.net`

## Security model

* Caller can NEVER set the From address — it is fixed server-side
  (`ACS_SENDER_ADDRESS`). The request body accepts `subject`, `body`, and an
  optional `to` recipient (syntactically validated); when `to` is omitted the
  configured default (`APPPILOT_EMAIL_TO`) is used. **Note:** because `to` is
  caller-chosen, anyone holding the API key can send from the fixed sender to
  any address — treat `APPPILOT_API_KEY` as a sensitive credential. Tighten with
  a recipient-domain allowlist if a narrower blast radius is required.
* The privileged ACS connection string lives only as a Container App secret
  (`acs-conn`); it is never in the CLI, never logged, never returned.
* The privileged Storage connection string lives only as a Container App secret
  (`telemetry-conn`); it is never in the CLI, never logged, never returned.
* The relay pulls its image using its **system-assigned managed identity** with
  only the `AcrPull` role — no broad access, no registry admin creds.
* Callers authenticate with a scoped API key (`api-key` secret), constant-time
  compared. HTTPS is enforced (`allowInsecure=false`).

## Server-side environment (set on the Container App, not the CLI)

| Variable | Value |
| --- | --- |
| `ACS_SENDER_ADDRESS` | fixed managed-domain sender |
| `APPPILOT_EMAIL_TO` | default recipient(s) when request omits `to`, comma/semicolon separated |
| `ACS_CONNECTION_STRING` | `secretref:acs-conn` |
| `APPPILOT_API_KEY` | `secretref:api-key` |
| `TELEMETRY_TABLE_CONNECTION_STRING` | `secretref:telemetry-conn` |

Retrieve the API key (owner only):

```bash
az containerapp secret show -n apppilot-email-api -g rg-apppilot-email \
  --secret-name api-key --query value -o tsv
```

## AppPilot CLI integration

The relay is the send transport, already wired in `src/apppilot/email_report.py`
(stdlib `urllib`, no extra deps). The CLI holds no `AZURE_*` credentials — those
live only server-side. It needs two owner-baked settings, no per-user input:

| Variable | Value |
| --- | --- |
| `APPPILOT_EMAIL_API_URL` | `https://<app-fqdn>/send-report` |
| `APPPILOT_EMAIL_API_KEY` | the `api-key` secret value |

The client posts `{subject, body[, to]}` with an `X-API-Key` header over https
only (cleartext and redirects are refused so the key can't leak) and treats any
2xx as success. See `_post_report` / `send_suite_report` for the authoritative
implementation; do not fork the snippet here.

## Telemetry endpoint (`POST /telemetry`)

The same relay records minimal per-suite telemetry. Authenticated (`X-API-Key`)
callers post **exactly four** fields — `suite`, `test_cases`, `host_os`
(`macOS`/`Windows`), `target_platform` (`Android`/`iOS`); extra or invalid fields
are rejected (`422`). Each accepted request writes one row to the `SuiteRuns`
table (via the server-only `telemetry-conn` secret) with `PartitionKey` = suite
and a random `uuid` `RowKey` (Azure mechanics, not data). The client
(`src/apppilot/telemetry.py`, stdlib `urllib`) is best-effort. Wire it with
`APPPILOT_TELEMETRY_URL` = `https://<app-fqdn>/telemetry` and the shared
`APPPILOT_EMAIL_API_KEY`.

## Redeploy the image

```bash
az acr build -r apppilotrelay<sub8> -t apppilot-email-relay:v1 ./email_service
az containerapp update -n apppilot-email-api -g rg-apppilot-email \
  --image apppilotrelay<sub8>.azurecr.io/apppilot-email-relay:v1
```
