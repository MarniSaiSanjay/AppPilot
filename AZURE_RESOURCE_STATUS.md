# AppPilot Azure Resource Status

**Verification date:** September 15, 2026  
**Subscription:** PLG Hackathon  
**Resource group:** `rg-apppilot`  
**Verification mode:** Direct Azure inspection plus RG-scoped relay preparation

**Follow-up action:** Two resource-scoped attempts were made to enable local
authentication on `aoai-apppilot`. Azure retained `disableLocalAuth=true`.

## Local environment files

| File | Status | Purpose |
|---|---|---|
| `.env` | Unchanged | Current active personal configuration |
| `.env.personal` | Created; exact copy; permissions `600`; gitignored | Protected snapshot of all existing personal values |
| `.env.work` | Created; permissions `600`; gitignored | Work-subscription cloud settings and explicit placeholders |

The work file currently contains:

```dotenv
APPPILOT_MODEL=gpt-4.1-mini
APPPILOT_MODEL_AUTH=entra
APPPILOT_MODEL_BASE_URL=https://aoai-apppilot.openai.azure.com/openai/v1
APPPILOT_MODEL_API_KEY=
APPPILOT_EMAIL_API_URL=
APPPILOT_EMAIL_API_KEY=
APPPILOT_TELEMETRY_URL=
APPPILOT_TELEMETRY_API_KEY=
```

The model key is intentionally blank because the work configuration now uses
Microsoft Entra authentication. Email and telemetry endpoints/keys are blank
because the Communication Services resources and relay Container App do not
exist yet. `stapppilot` and `SuiteRuns` already exist, but the client must send
telemetry through the relay rather than connecting to Storage directly.

### Personal versus work authentication

`APPPILOT_MODEL_AUTH` selects the authentication method:

| Configuration | Authentication |
|---|---|
| `APPPILOT_MODEL_AUTH=api_key`, or the variable is absent | Uses `APPPILOT_MODEL_API_KEY`; this preserves the existing personal configuration |
| `APPPILOT_MODEL_AUTH=entra` | Uses the signed-in Microsoft work identity and requires `APPPILOT_MODEL_API_KEY` to remain blank |

AppPilot automatically loads `.env`, so an ordinary run continues to use the
personal configuration. `.env.personal` is only the protected backup copy.
`.env.work` is selected explicitly for a work run:

```bash
set -a
source .env.work
set +a
```

Already-exported shell variables take precedence over `.env`, so this selects
the work endpoint and Entra authentication without modifying either `.env` or
`.env.personal`. The selection lasts only in that shell.

## Diagnostic settings

All new diagnostic settings send data to `law-apppilot`.

| Source | Setting | Enabled data |
|---|---|---|
| `aoai-apppilot` | `diag-aoai-apppilot` | Audit, Azure OpenAI request usage, trace, managed-network events, and all metrics |
| `acrapppilot` | `diag-acrapppilot` | Repository events, login events, and all metrics |
| `stapppilot` | `diag-stapppilot` | Capacity and transaction metrics |
| `stapppilot/tableServices/default` | `diag-stapppilot-tables` | Table read, write, and delete logs plus capacity and transaction metrics |

OpenAI `RequestResponse` logging is deliberately disabled so prompts and model
responses are not retained in Log Analytics. A resource-group policy compliance
rescan was requested after creating the settings.

The relay's Container Apps console and system logs also flow to
`law-apppilot`; live relay logs and the `/healthz` endpoint have been verified.
There are currently no Azure Monitor metric alerts, activity-log alerts,
scheduled-query alerts, or action groups in `rg-apppilot`. Alerting is not
needed for the initial low-volume deployment, but it must be added with an
approved notification owner before automatic operational notifications are
expected.

## Container Apps network policy

The management-group policy `ACA-VNetInjection-V1` is assigned with a `deny`
effect. It requires Container Apps environments to have:

```text
vnetConfiguration.infrastructureSubnetId
```

`cae-apppilot` is healthy but has no VNet configuration, so policy reports it
as non-compliant. The environment currently contains no Container Apps.

The policy rule applies specifically to resources of type
`Microsoft.App/managedEnvironments`. A direct Azure deployment what-if for
`ca-apppilot-relay` completed successfully with no policy diagnostic, confirming
that a Container App can be created in the existing environment without
recreating or updating that environment.

The VNet setting is chosen when an environment is created and cannot be added
to this environment in place. Making the environment itself compliant would
require:

1. A VNet and delegated infrastructure subnet.
2. Recreating `cae-apppilot` with that subnet.
3. A Container Apps infrastructure resource group. Azure CLI documents that a
   name can be supplied through `--infrastructure-resource-group`; otherwise
   Azure generates one.

Creating a separate infrastructure resource group would violate the current
strict boundary that nothing be created outside `rg-apppilot`. The environment
was therefore not deleted or replaced. A policy-approved skip tag was also not
used because tags were declined and it would bypass rather than satisfy the
network requirement.

This remains an environment-compliance finding, but it is **not a blocker for
deploying the relay Container App into the existing environment**.

## Overall status

**Operational.** Every resource that currently exists in `rg-apppilot` has
`Succeeded` provisioning state. Regional infrastructure is located in South
India; Azure Communication Services resources use their required global
resource location with India data residency. The complete AppPilot cloud path
is operational: the relay is deployed, `/healthz` is healthy, ACS delivery
validation succeeded, and the managed-identity telemetry path recorded a
`SuiteRuns` validation entity.

The core AppPilot model path is operational: the local client supports Entra
authentication, the signed-in work identity has `Cognitive Services OpenAI
User` scoped directly to `aoai-apppilot`, and a live request to
`gpt-4.1-mini` returned successfully.

## Directly verified resources

| Resource | Type | Location | Health | Verified configuration |
|---|---|---|---|---|
| `rg-apppilot` | Resource group | South India | Succeeded | No tags or resource locks |
| `aoai-apppilot` | Azure OpenAI | South India | Succeeded | `S0`, public network enabled, local/API-key authentication disabled; Entra inference validated |
| `gpt-4.1-mini` | Model deployment | South India | Succeeded | Version `2025-04-14`, regional `Standard`, capacity `50`, 50,000 TPM, 50 RPM, `Microsoft.DefaultV2` content policy |
| `acrapppilot` | Azure Container Registry | South India | Succeeded | Basic, admin disabled, public network enabled, zone redundancy disabled |
| `law-apppilot` | Log Analytics workspace | South India | Succeeded | `PerGB2018`, 30-day retention, no daily ingestion cap |
| `cae-apppilot` | Container Apps environment | South India | Succeeded | Consumption workload profile, public network enabled, Log Analytics connected, zone redundancy disabled |
| `stapppilot` | Storage account | South India | Succeeded | StorageV2, Hot, Standard_LRS, HTTPS-only, TLS 1.2, shared-key access enabled, blob public access disabled |
| `SuiteRuns` | Azure Table | South India | Exists | Telemetry table under `stapppilot`; managed-identity write validated |
| `id-apppilot-relay` | User-assigned managed identity | South India | Succeeded | Relay-only identity; `AcrPull` is scoped only to `acrapppilot` |
| `emailcs-apppilot` | Email Communication Service | Global | Succeeded | India data location |
| `AzureManagedDomain` | Azure-managed email domain | Global | Succeeded | Child of `emailcs-apppilot`; India data location, all domain/SPF/DKIM/DMARC checks verified, engagement tracking disabled |
| `acs-apppilot` | Azure Communication Service | Global | Succeeded | India data location; linked only to `emailcs-apppilot/AzureManagedDomain` |
| `ca-apppilot-relay` | Container App | South India | Succeeded / Healthy | HTTPS-only relay at `https://ca-apppilot-relay.ambitioussmoke-8a5df56b.southindia.azurecontainerapps.io`; one active revision, 0.5 vCPU, 1 GiB, scale 0-2, user-assigned identity |

## Relay image

An ACR remote build submitted only the `email_service/` directory as its build
context. No root project files, `.env` files, APKs, workbooks, or main AppPilot
code were included.

```text
Registry:   acrapppilot.azurecr.io
Repository: apppilot-relay
Tag:        20260915-telemetry-mi1
Digest:     sha256:ce40533d96be39339efaf7ee35c5060d65d9903b6fe61afebc65586b9be455ad
```

The managed-identity telemetry build succeeded on September 15, 2026. A
user-assigned identity named
`id-apppilot-relay` was created in `rg-apppilot`, and the `AcrPull` role was
verified directly through Azure Resource Manager at the `acrapppilot` resource
scope. No broader registry, resource-group, subscription, or tenant role was
granted.

The same identity has `Storage Table Data Contributor` scoped only to the
existing `SuiteRuns` table. The relay now uses `DefaultAzureCredential` with
the Table service endpoint, so no Storage account key, connection string, or
SAS token is stored in the image or planned Container App secrets.

The Bicep template declaratively ensures `SuiteRuns` and this exact table-scoped
role assignment exist before the Container App. If `stapppilot` is ever
recreated, the template must be deployed again before telemetry is enabled; the
least-privileged relay identity intentionally cannot create tables at runtime.

The Container App definition is maintained locally as
`email_service/containerapp.bicep`. It references the immutable image digest,
existing environment, registry, Storage endpoint, and user-assigned identity;
defines HTTPS-only external ingress on port 8000, `/healthz`
liveness/readiness probes, 0.5 vCPU, 1 GiB memory, and scale 0-2; and accepts
only the ACS and two relay API keys as secure deployment parameters. It was
deployed successfully as `containerapp-relay-20260915`; the secrets are stored
only in the Container App and the gitignored, permission-restricted `.env.work`
file. Its image parameter is required on each release, so future template
deployments cannot silently roll the relay back to an earlier image.

## Azure OpenAI authentication

`aoai-apppilot` currently reports:

```text
disableLocalAuth: true
```

The repository's `src/shared/model_client.py` now supports both its existing
API-key mode and an explicit `APPPILOT_MODEL_AUTH=entra` mode. Entra mode uses
`DefaultAzureCredential`, requests `https://ai.azure.com/.default`, and does not
store or transmit an Azure OpenAI API key. To prevent bearer-token disclosure,
Entra mode accepts only an `https://<resource>.openai.azure.com/openai/v1`
endpoint; API-key mode retains its broader OpenAI-compatible endpoint support.

Two updates scoped directly to `aoai-apppilot` attempted to set
`disableLocalAuth=false`. Both requests completed, but a fresh resource read
still returned `disableLocalAuth=true`. Policy state shows that disabling local
authentication is compliant and that a modify-policy assignment applies to the
resource. There was no effective configuration change, and no policy exception
is now required.

The signed-in work identity was granted `Cognitive Services OpenAI User` at the
`aoai-apppilot` resource scope only. After Azure RBAC propagation, the actual
AppPilot `ChatModelClient` completed a live `gpt-4.1-mini` request successfully.

## Missing or intentionally deferred resources

| Planned component | Status | Reason |
|---|---|---|
| `emailcs-apppilot` | Created | Global resource location with India data location |
| `AzureManagedDomain` | Created and verified | Nested under `emailcs-apppilot`; Azure-managed sender domain with engagement tracking disabled |
| `acs-apppilot` | Created and linked | Global resource location with India data location; linked to `AzureManagedDomain` |
| `ca-apppilot-relay` | Deployed and healthy | One active revision with 100% traffic; uses `id-apppilot-relay` and the immutable relay image digest |
| Relay image | Built and running | Managed-identity telemetry image is pulled from `acrapppilot` by the relay identity |
| Relay managed identity and roles | In use | `AcrPull` is scoped to `acrapppilot`; `Storage Table Data Contributor` is scoped only to `SuiteRuns` |
| Relay deployment template | Deployed | `email_service/containerapp.bicep` deployed successfully without secret values in source control |
| Container App secrets | Configured securely | ACS connection string and distinct email/telemetry API keys are stored only as Container App secrets |

Registering `Microsoft.Communication` is a subscription-level modification
outside `rg-apppilot`, so it was not performed under the resource-group-only
boundary.

## Telemetry status

The storage layer is ready:

```text
stapppilot
└── SuiteRuns
```

Telemetry is not operational until the relay exists and these values are
configured:

```text
Container App secrets:
  telemetry-api-key

Container App environment:
  APPPILOT_TELEMETRY_API_KEY=secretref:telemetry-api-key
  TELEMETRY_TABLE_ENDPOINT=https://stapppilot.table.core.windows.net
  AZURE_CLIENT_ID=<id-apppilot-relay client ID>

Client environment:
  APPPILOT_TELEMETRY_URL=https://<relay-fqdn>/telemetry
  APPPILOT_TELEMETRY_API_KEY=<generated-telemetry-key>
```

The email and telemetry API keys must be different. Storage authentication uses
`id-apppilot-relay`; no Storage connection string or account key is required.

## Capacity assessment

The provisioned capacity is sufficient for the planned initial workload:

- Azure OpenAI provides 50,000 TPM and 50 RPM. This is higher than the personal
  deployment and is a reasonable initial allocation for up to ten approximately
  100-case suites per day.
- Basic ACR is sufficient for one small relay image and infrequent releases.
- The Consumption Container Apps environment supports scale-to-zero and is
  sufficient for the low relay request volume.
- Standard_LRS Table Storage is sufficient because telemetry records one small
  entity per suite, not per test case.
- Thirty-day Log Analytics retention is sufficient for initial diagnostics.

Capacity can be increased later. There are no current compute, storage,
authentication, or service-provisioning blockers for the deployed AppPilot
path.

## Code covered by the deployed relay

The deployed image contains only the isolated `email_service/` container:

```text
email_service/app.py
email_service/Dockerfile
email_service/requirements.txt
email_service/.dockerignore
```

The Dockerfile installs the relay dependencies, copies `app.py`, and starts
Uvicorn on port `8000`. The image is running in `ca-apppilot-relay`, which
exposes `/healthz`, `/send-report`, and `/telemetry`, uses Azure Communication
Services Email, and writes telemetry to Azure Table Storage.

It does **not** deploy the main AppPilot CLI, Android automation, test workbook,
login code, local credentials, APK, or test artifacts. Those remain on the
developer machine.

## End-to-end validation

- The deployed Container App revision is `Healthy`, and `/healthz` returns
  `{"status":"ok"}`.
- A real telemetry request returned `{"status":"recorded"}`, confirming that
  `id-apppilot-relay` can write to `SuiteRuns` through its table-scoped
  `Storage Table Data Contributor` role.
- Two relay email validation requests were accepted by ACS. A final tracked
  delivery-status lookup returned `Succeeded` with no provider error.
- `.env.work` contains the HTTPS email and telemetry endpoints and their
  distinct scoped API keys. It remains gitignored with `600` permissions.
- When `.env.work` selects Entra model authentication, `/init` and `./run`
  first verify that an Azure AI token can be acquired. An interactive terminal
  offers to run `az login` if needed; no Azure password is collected, no login
  is launched in non-interactive runs, and AppPilot never changes the active
  Azure subscription.

Tags and a deletion lock remain intentionally deferred pending explicit
approval.
