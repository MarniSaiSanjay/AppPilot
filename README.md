# AppPilot

AppPilot is a **goal-driven AI agent for Android regression testing**. Given a
test goal and a connected Android device, it drives a real app toward that goal.

## Quick start

From the AppPilot folder:

- macOS, Linux, or Git Bash: `./setup` once, then `./init` and `./run`
- Windows PowerShell: `.\setup.cmd` once, then `.\init.cmd` and `.\run.cmd`

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

- `src/apppilot/` — core framework and `/init` configuration flow.
- `src/apppilot_setup.py` — one-time local `/setup` entry point.
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
- approved local APK and Deeplink test-account credentials

On Windows, run local setup with `.\setup.cmd`.

AppPilot validates the application ID and signature of the local APK you
provide.

## Setup

Run `/setup` once from the project root to install and verify local prerequisites:

```bash
./setup
```

The `./setup` wrapper auto-selects a Python 3.11-3.13 interpreter. Equivalently:

```bash
python3.11 src/apppilot_bootstrap.py
```

`/setup` installs or checks **Python**, **ADB**, **Maestro**, **Java**, and
Android APK tools after confirmation. It does not create emulators/AVDs.

Then run `/init` when you need to configure AppPilot's **Model**, **Android
Device**, **APK**, or optional **Email** recipient. It saves your selected
device, APK path, and email recipient for later runs.

```bash
./init
```

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

For interactive Deeplink runs, AppPilot prompts for any required workbook
test-account credentials. For non-interactive/CI runs, provide those
credentials in local `.env`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `APPPILOT_USERNAME_<LICENSE>` | for non-interactive Deeplink runs | Username for an Excel License profile. |
| `APPPILOT_PASSWORD_<LICENSE>` | for non-interactive Deeplink runs | Password for an Excel License profile. |

Security:

- Login credentials are resolved locally and are never included in model
  prompts, UI observations, logs, or source code.
- Before credential injection, AppPilot revalidates APK trust, foreground app,
  and the target field.
- Keep `.env` local and never commit test credentials.

Interactive Deeplink runs prompt for missing test-profile credentials and keep
them only for that run. For non-interactive/CI runs, create a local
configuration file from the tracked template:

```bash
cp .env.example .env
```

`.env.example` contains only the current workbook profile placeholders; never
commit `.env`.

`/init` and `./run` prompt for work Azure CLI sign-in when required.

Deeplink suites resolve every workbook `License` before APK or device work.
License values are converted to profile-specific environment suffixes such as
`ENTRA_PREMIUM`; Deeplink does not fall back to global credentials. See
[`src/usecases/deeplink/README.md`](src/usecases/deeplink/README.md).

## Use cases

Each use case documents its own test data and user-provided inputs. Shared
machine setup is handled once by `/setup`, and shared AppPilot configuration is
saved by `/init`.

### Deeplink

Before running the Deeplink use case, provide:

- An approved local Android `.apk`, selected during `/init` or passed with
  `--apk PATH`.
- A connected Android device or an existing emulator/AVD, selected during
  `/init` or passed with `--device SERIAL`.
- A Deeplink Excel workbook with `Test ID`, `Deep Link`, `License`, and
  `Expected Result` columns. Put it in `testcases/deeplinks/`, or pass it with
  `--excel PATH`.
- A test account for every `License` profile used in the workbook. Interactive
  runs prompt for missing credentials; non-interactive runs read the matching
  `APPPILOT_USERNAME_<LICENSE>` and `APPPILOT_PASSWORD_<LICENSE>` values from
  local `.env`.

Run the use case with:

```bash
./run --usecase deeplink
```

## Running AppPilot

With a device connected, run a **test suite**:

```bash
./run
```

`./run` (a.k.a. `/run`) selects a **use case** (auto when only one exists,
otherwise a numbered prompt), then that use case's **test suite** (auto when only
one workbook exists, otherwise a numbered prompt), then executes all of its test
cases. Skip use-case selection with `--usecase NAME`; any remaining options are
passed through to that use case's runner. The wrapper auto-selects a Python
3.11-3.13 interpreter.

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

`/init` or a suite run prompts for the recipient and saves it locally. Reports
include the AppPilot contact address. Delivery only happens after a suite
finishes and never affects the run verdict. See
[`email_service/README.md`](email_service/README.md).

## Telemetry

Telemetry is **optional** and never affects the suite verdict. See
[`email_service/README.md`](email_service/README.md).

## Documentation

- [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) — architecture,
  component boundaries, decision-model contract, safety model, and the deeplink
  use-case execution details.
