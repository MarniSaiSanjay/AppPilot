# AppPilot

AppPilot is a **goal-driven AI agent for Android regression testing**. Given a
test goal and a connected Android device, it drives a real app toward that goal.

## Runtime behavior

- **`AppPilotAgent` is the orchestrator:** observe → evaluate expected output →
  decide → validate → execute → repeat.
- **UIAutomator observes every run**; Maestro hierarchy is the fallback.
- **`AdaptiveDecisionCache` chooses and learns decisions; it is not the
  orchestrator.** It tries seeded known decisions, then decisions learned from
  successful runs, then the model.
- **Execution:** ADB handles deterministic actions; Maestro handles selectors,
  secure input, and fallback actions.
- Every action passes safety validation. Failed or incomplete runs save no new
  decisions.

See [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md).

## Project structure

- `src/apppilot/` — core framework and `/init` setup.
- `src/shared/` — reusable, use-case-agnostic nodes.
- `src/usecases/` — use-case-specific functionality.
- `src/flows/` — compatibility shims.
- `docs/` — project documentation.
- `testcases/` — test data.

Shared nodes depend on `apppilot`; use cases may depend on both `shared` and
`apppilot`.

## Adding a new use case

1. Create `src/usecases/<name>/` with a `cli.py` exposing `main`.
2. Add loader, runner/orchestrator, verifier, results, and tests as needed.
3. Add test data under `testcases/<name>/` when the use case is data-driven.
4. Put reusable cross-use-case nodes in `src/shared/`.

## Prerequisites

### Python environment

AppPilot supports **Python 3.11, 3.12, and 3.13**. Entra authentication requires
the runtime `azure-identity` dependency. Development checks and the email relay
have additional dependencies.

Create a development environment from the repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

`pyproject.toml` declares the supported Python range and runtime dependencies.
`requirements-dev.txt` installs development checks and email relay dependencies.

### Runtime tools

Full Android execution requires:

- Python 3.11-3.13
- Android SDK Platform Tools and build tools (`adb`, `apksigner`, and
  `apkanalyzer` or `aapt`)
- Java 17+
- Maestro CLI 2.x
- one Android emulator/AVD or physical USB-debugging device
- trusted APK, model settings, and account settings

On Windows, run AppPilot from WSL with Maestro installed in Linux.

The target package and trusted signing certificate are required configuration.
AppPilot validates the application ID from the APK.

## Setup

Run the `/init` setup flow from the project root:

```bash
./init
```

The `./init` wrapper auto-selects a Python 3.11+ interpreter. Equivalently:

```bash
python3.11 src/apppilot_init.py
```

`/init` walks through **Environment** (Python, ADB, Maestro, Java) → **Model** →
**Android Device** → **APK** → **Email**, then prints a readiness summary. It
supports physical devices and emulators/AVDs (including multiple devices and
starting an emulator when required), and saves your selected device, APK path,
and email recipient for later runs.

```text
/init
  ✓ Environment
  ✓ Model
  ✓ Android Device
  ✓ APK
  ○ Email skipped

Ready
```

`/init` checks and saves local AppPilot configuration. It does not install
prerequisites, build APKs, or send email.

## Source validation

Canonical developer commands:

| Purpose | Command |
| --- | --- |
| Full unit suite | `python -m unittest discover -s tests -p 'test_*.py'` |
| Email relay tests | `python -m unittest tests.test_email_service` |
| Syntax | `python -m compileall -q src tests scripts email_service` |
| Static checks | `python -m pyflakes src tests scripts email_service` |
| Workbook contract | `python scripts/validate_workbook.py` |
| Secret scan | `python scripts/scan_secrets.py` |
| Dependency audit | `python -m pip_audit -r email_service/requirements.txt --progress-spinner off` |
| Local PR-equivalent source checks | `python scripts/check.py` |
| Email container build | `docker build --tag apppilot-email-relay:local ./email_service` |

`scripts/check.py` is the local PR-equivalent convenience command. Dependency
audit requires access to the Python vulnerability index.

### CI validation strategy

Every pull request runs `.github/workflows/pr-source-validation.yml`. It
validates Python 3.11-3.13 syntax/tests, pyflakes, workbook contract, secret
patterns, changed-line whitespace, email relay behavior, dependency audit, and
the email-service Docker build.

Ordinary PR CI does **not** run an emulator, physical device, Maestro, ADB, APK
installation, real model calls, account switching, Store handoff, email
delivery, or telemetry delivery.

## Configuration

AppPilot reads configuration from environment variables. A local, **git-ignored**
`.env` file at the repository root is loaded automatically at startup (real
environment variables always take precedence).

| Variable | Required | Purpose |
| --- | --- | --- |
| `APPPILOT_MODEL` | yes* | Decision-model deployment/name (e.g. `gpt-4.1-mini`). |
| `APPPILOT_MODEL_AUTH` | no | Model authentication mode: `api_key` (default) or `entra`. |
| `APPPILOT_MODEL_API_KEY` | for API-key auth | API key for the model endpoint; blank for Entra auth. |
| `APPPILOT_MODEL_BASE_URL` | no for API-key auth; yes for Entra auth | Model endpoint. Entra mode requires Azure OpenAI `/openai/v1`. |
| `APPPILOT_ENTRA_TENANT_ID` | no | Azure tenant ID for interactive Azure CLI sign-in. |
| `APPPILOT_EXPECTED_PACKAGE_ID` | yes for APK runs | Expected Android application/package ID. |
| `APPPILOT_TRUSTED_CERT_SHA256` | yes for APK runs | Trusted signing-certificate SHA-256 fingerprint. |
| `APPPILOT_USERNAME` | for standalone login | Default sign-in username/email for standalone login. |
| `APPPILOT_PASSWORD` | for standalone login | Default sign-in password. |
| `APPPILOT_USERNAME_<LICENSE>` | for Deeplink login | Username for an Excel License profile. |
| `APPPILOT_PASSWORD_<LICENSE>` | for Deeplink login | Password for an Excel License profile. |
| `APPPILOT_MAX_ACTIONS` | no | Absolute upper bound on actions per run (default `30`). |
| `APPPILOT_MAX_STUCK_ACTIONS` | no | Consecutive unchanged actions before controlled FAIL (default `5`). |
| `APPPILOT_EMAIL_API_URL` | for email | HTTPS relay endpoint used to send the suite report. |
| `APPPILOT_EMAIL_API_KEY` | for email | Scoped API key for the email relay. |
| `APPPILOT_CONTACT_EMAIL` | no | Optional contact address shown in the report footer (omitted if unset). |
| `APPPILOT_TELEMETRY_URL` | for telemetry | HTTPS relay `/telemetry` endpoint. Unset disables telemetry. |
| `APPPILOT_TELEMETRY_API_KEY` | for telemetry | Telemetry-only API key. |

\* If no model is configured, the agent does **not** guess — it reports that it
cannot proceed and how to configure one.

Security:

- Login credentials are resolved locally and are never included in model
  prompts, UI observations, logs, or source code.
- Before credential injection, AppPilot revalidates APK trust, foreground app,
  and the target field.
- Keep `.env` local and never commit real credentials or API keys.

Example `.env`:

```dotenv
APPPILOT_MODEL=gpt-4.1-mini
APPPILOT_MODEL_AUTH=api_key
APPPILOT_MODEL_API_KEY=your-key-here
APPPILOT_MODEL_BASE_URL=https://your-endpoint.openai.azure.com/openai/v1
APPPILOT_EXPECTED_PACKAGE_ID=com.example.apppilot
APPPILOT_TRUSTED_CERT_SHA256=replace-with-the-trusted-64-hex-sha256-fingerprint
APPPILOT_USERNAME=you@example.com
APPPILOT_PASSWORD=your-password
APPPILOT_USERNAME_PREMIUM=premium-user@example.com
APPPILOT_PASSWORD_PREMIUM=your-premium-password
APPPILOT_USERNAME_ENTRA_STARTER=starter-user@example.com
APPPILOT_PASSWORD_ENTRA_STARTER=your-starter-password
```

For keyless Azure OpenAI access, sign in with Azure CLI and use:

```dotenv
APPPILOT_MODEL=gpt-4.1-mini
APPPILOT_MODEL_AUTH=entra
APPPILOT_MODEL_API_KEY=
APPPILOT_MODEL_BASE_URL=https://your-endpoint.openai.azure.com/openai/v1
APPPILOT_ENTRA_TENANT_ID=your-work-tenant-id
```

Entra mode uses `DefaultAzureCredential` and requests the
`https://ai.azure.com/.default` scope. The signed-in identity must have the
`Cognitive Services OpenAI User` role on the Azure OpenAI resource.

When Entra mode is selected, `/init` and `./run` check Azure AI token
availability before model use. Interactive runs can launch `az login`.
`APPPILOT_ENTRA_TENANT_ID` constrains Azure CLI sign-in to a tenant.

Deeplink suites resolve every workbook `License` before APK or device work.
License values are converted to profile-specific environment suffixes such as
`ENTRA_PREMIUM`; Deeplink does not fall back to global credentials. See
[`src/usecases/deeplink/README.md`](src/usecases/deeplink/README.md).

## Running AppPilot

With a device connected and `.env` in place, run a **test suite**:

```bash
./run
```

`./run` (a.k.a. `/run`) selects a **use case** (auto when only one exists,
otherwise a numbered prompt), then that use case's **test suite** (auto when only
one workbook exists, otherwise a numbered prompt), then executes all of its test
cases. Skip use-case selection with `--usecase NAME`; any remaining options are
passed through to that use case's runner. The wrapper auto-selects a Python 3.11+
interpreter.

To invoke the Deeplink compatibility entry point directly, bypassing top-level
use-case selection:

```bash
python3 src/deeplink_runner.py
```

Options: `--excel` (explicit workbook path; otherwise a suite is selected from
`testcases/deeplinks/`), `--device`, `--max-attempts` (default `2`),
`--verify-timeout` (default `30` seconds), `--no-warm-up`, and `--apk`.

`--device` is optional. When omitted, AppPilot reuses the valid `/init` device
or auto-detects a single usable device. Multiple usable devices require
`--device <serial>`. A per-device process lock prevents concurrent mutation.

To run the standalone goal-driven agent prototype (login preparation only):

```bash
python3 src/apppilot_agent.py
```

Options: `--device`, `--max-actions`, `--max-stuck-actions`, `--guidance`.

Standalone login uses the same device selection rules as the Deeplink runner.

## Deeplink test suite

The Deeplink use case is a data-driven Excel regression suite. Test suites live
in `testcases/deeplinks/`; when `--excel` is omitted the runner auto-runs the
only workbook there, prompts to choose when several exist, and exits cleanly
(code `2`) when none is found.

See [`src/usecases/deeplink/README.md`](src/usecases/deeplink/README.md) for
workbook rules, installed/uninstalled flow, verification, retries, cleanup, and
artifacts.

## APK

AppPilot installs only a user-provided local `.apk`. Configure it through
`/init`, or supply `--apk PATH`. Saved APK paths live in `.apppilot_apk`
(gitignored).

## Email reporting

Emailing the suite report is **optional**. `/init` can save a default recipient
in `.apppilot_recipient` (gitignored). Suite runs prompt before sending.

Email delivery needs `APPPILOT_EMAIL_API_URL` and `APPPILOT_EMAIL_API_KEY`; an
optional `APPPILOT_CONTACT_EMAIL` appears in the report footer. Delivery only
happens after a suite finishes and never affects the run verdict. See
[`email_service/README.md`](email_service/README.md).

## Telemetry

Telemetry is **optional** and off unless `APPPILOT_TELEMETRY_URL` is set. It
uses `APPPILOT_TELEMETRY_API_KEY` and never affects the suite verdict. See
[`email_service/README.md`](email_service/README.md).

## Documentation

- [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) — architecture,
  component boundaries, decision-model contract, safety model, and the deeplink
  use-case execution details.
