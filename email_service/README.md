# AppPilot Email Relay

Server-side email API for AppPilot reports. It accepts an already-generated
report and sends it from one fixed service-owned Azure Communication Services
sender.

```
AppPilot CLI  ──HTTPS + X-API-Key──▶  this relay (Container App)
                                          │  fixed sender; recipient from request
                                          │  (validated) or configured default
                                          │  ACS connection string (server secret)
                                          ▼
                                    ACS Email  ──▶  recipient
```

## Azure resources

Required resources:

| Resource | Purpose |
| --- | --- |
| Email Communication Service | Email sending service |
| Azure-managed domain | Fixed sender domain |
| Communication Service | ACS Email API |
| Container Registry | Relay image registry |
| Log Analytics workspace | Container Apps logs |
| Container Apps environment | Relay host environment |
| Container App | HTTPS relay |
| Storage Account | Table Storage for telemetry |
| Table | One row per suite run |

## Security model

- Fixed sender: `ACS_SENDER_ADDRESS`
- Email payload: `{subject, body[, html][, to]}`
- Default recipient: `APPPILOT_EMAIL_TO`
- ACS secret: `ACS_CONNECTION_STRING`
- Email API key: `APPPILOT_API_KEY`
- Telemetry API key: `APPPILOT_TELEMETRY_API_KEY`
- Telemetry identity: `AZURE_CLIENT_ID`
- Telemetry table endpoint: `TELEMETRY_TABLE_ENDPOINT`
- HTTPS-only ingress: `allowInsecure=false`

## Local development and validation

The relay dependencies are isolated in `email_service/requirements.txt`; the
main AppPilot runner does not import them.

```bash
python -m pip install -r requirements-dev.txt
python -m unittest tests.test_email_service
python -m pip_audit -r email_service/requirements.txt --progress-spinner off
docker build --tag apppilot-email-relay:local ./email_service
```

Tests cover email/telemetry keys, fixed sender, recipient handling, telemetry
schema, and sanitized provider failures.

`containerapp.bicep` owns the `SuiteRuns` table and its table-scoped role
assignment. Deploy it before enabling telemetry. The relay cannot create tables
at runtime.

## Server-side environment (set on the Container App, not the CLI)

| Variable | Value |
| --- | --- |
| `ACS_SENDER_ADDRESS` | fixed managed-domain sender |
| `APPPILOT_EMAIL_TO` | default recipient(s) when request omits `to`, comma/semicolon separated |
| `ACS_CONNECTION_STRING` | `secretref:acs-conn` |
| `APPPILOT_API_KEY` | `secretref:api-key` |
| `APPPILOT_TELEMETRY_API_KEY` | `secretref:telemetry-api-key` |
| `TELEMETRY_TABLE_ENDPOINT` | Table service endpoint |
| `AZURE_CLIENT_ID` | relay managed-identity client ID |

## AppPilot CLI integration

The relay is the send transport in `src/apppilot/email_delivery.py`. The CLI holds
no `AZURE_*` credentials. It needs two settings:

| Variable | Value |
| --- | --- |
| `APPPILOT_EMAIL_API_URL` | `https://<app-fqdn>/send-report` |
| `APPPILOT_EMAIL_API_KEY` | the `api-key` secret value |

The client posts `{subject, body[, html][, to]}` with an `X-API-Key` header over
HTTPS only and treats any 2xx as success.

## Telemetry endpoint (`POST /telemetry`)

Authenticated callers post `suite`, `test_cases`, `host_os`, and
`target_platform` with `X-API-Key`. Accepted requests write one row to
`SuiteRuns`.

## Redeploy the image

```bash
az acr build -r acrapppilot -t apppilot-relay:<version> ./email_service
```

Build and deploy an immutable image with `containerapp.bicep`. Required
parameters: `image`, `senderAddress`, `acsConnectionString`, `emailApiKey`, and
`telemetryApiKey`. `defaultRecipient` is optional.
