# AppPilot

AppPilot is a **goal-driven AI agent for Android regression testing**. Given a
test goal and a connected Android device, it drives a real app toward that goal
by *reasoning over the live UI* — it is **not** a fixed A→B→C script and **not**
a collection of hardcoded Maestro flows.

The agent runs a simple loop: observe the UI → check whether the goal is reached
→ select a known, successfully learned, or model-decided action → validate it
for safety → execute it → observe again. UIAutomator is the fast primary
observer, ADB handles simple deterministic actions, and Maestro remains the
secure/complex-action fallback. It reaches **PASS** when the goal evaluator
recognizes the goal state and **FAIL** when it cannot safely proceed or an
action limit is hit.

## Runtime behavior

- **`AppPilotAgent` is the orchestrator:** observe → evaluate expected output →
  decide → validate → execute → repeat.
- **UIAutomator observes every run**, including the first; Maestro hierarchy is
  used automatically in the same observation step if direct capture times out,
  errors, or returns invalid XML, without restarting the run.
- **`AdaptiveDecisionCache` chooses and learns decisions; it is not the
  orchestrator.** It tries seeded known decisions, then decisions learned from
  successful runs, then the model.
- With an empty cache, the first unknown state uses the model. Its decision stays
  pending and is cached only if the complete run reaches its expected output.
  Later matching states reuse it; new states use the model and follow the same
  success-only learning rule.
- **Execution:** seeded/cached taps and Back use fast ADB against the current
  observation. Model decisions are re-observed through the same UIAutomator-first
  observer, then the normal executor uses Maestro selectors or ADB coordinates
  as appropriate. Username entry can use verified paced ADB input; password
  entry uses Maestro's secure clipboard path and clears the Android clipboard
  immediately after paste, with best-effort cleanup on failure paths.
- Every action still passes safety validation. Failed or incomplete runs save no
  new decisions.

See [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) for the full
architecture, decision-model contract, safety model, and design principles.

## Project structure

- `src/apppilot/` — core framework and `/init` setup.
- `src/shared/` — reusable, use-case-agnostic nodes.
- `src/usecases/` — use-case-specific functionality.
- `src/flows/` — compatibility shims.
- `docs/` — project documentation.
- `testcases/` — test data.

Dependency direction is one-way — `usecases → shared → apppilot` — so shared
nodes stay generic and never import a specific use case. Put reusable nodes in
`src/shared/` and use-case-specific logic in `src/usecases/`.

## Prerequisites

### Python environment

AppPilot supports **Python 3.11, 3.12, and 3.13**. The main Android runner is
stdlib-only; third-party Python packages are needed only for development checks
and the separately deployed email relay.

Create a development environment from the repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

`pyproject.toml` declares the supported Python range and root project metadata.
`requirements-dev.txt` installs pinned pyflakes/pip-audit versions and the
separately pinned `email_service/requirements.txt`. The root AppPilot package
has no third-party runtime dependency.

### Tool requirements by validation level

| Level | Requirements |
| --- | --- |
| Unit/source checks | Python 3.11-3.13 plus `requirements-dev.txt`. No Android SDK, device, APK, model, or production credentials. |
| Emulator/device development | Unit requirements plus Android SDK Platform Tools with ADB 1.0.41-compatible commands, Maestro CLI 2.x (known-good: 2.8.0), Java 17+, and one explicitly selected ready Android target. |
| Full production execution | Device requirements plus `apksigner` and either `apkanalyzer` or `aapt`, a trusted signed APK, expected package/certificate configuration, and required model/account settings. |

ADB, Maestro, Java, Android SDK tools, devices, and APKs are external runtime
prerequisites; they are intentionally not Python dependencies.

### macOS

- **Python 3.11-3.13**.
- **Android SDK Platform Tools and command-line/build tools** — ensure `adb`,
  `apksigner`, and either `apkanalyzer` or `aapt` are on `PATH`.
- **Maestro CLI** (requires Java 17+):
  ```bash
  curl -fsSL "https://get.maestro.mobile.dev" | bash
  # then ensure $HOME/.maestro/bin is on your PATH
  maestro --version
  ```
- **An Android device** — an emulator/AVD or a physical device connected over
  USB with USB debugging enabled. Confirm it is visible:
  ```bash
  adb devices
  ```

### Windows

AppPilot itself is cross-platform. On Windows, Maestro runs through WSL.
Install the following prerequisites:

- **Python 3.11-3.13** — from [python.org](https://www.python.org/downloads/windows/)
  (tick "Add python.exe to PATH").
- **Android Studio / Android SDK** — the simplest way to get the emulator and to
  create an AVD. Add the SDK's `platform-tools`, `emulator`, `build-tools`, and
  `cmdline-tools` binaries to your `PATH`; AppPilot requires `adb`,
  `apksigner`, and either `apkanalyzer` or `aapt`.
- **Android Platform Tools / ADB** — bundled with the SDK; confirm with
  `adb devices`.
- **Java 17+** — required by Maestro (JDK 17 or newer on your `PATH`).
- **A device** — an **Android Emulator / AVD** or a **physical device** over USB
  with USB debugging enabled.
- **Maestro — requires WSL on Windows.** Maestro has no native Windows build, so
  install and configure the
  [Windows Subsystem for Linux](https://learn.microsoft.com/windows/wsl/install)
  first (`wsl --install`), install Maestro *inside* your WSL distribution per the
  [Maestro docs](https://maestro.mobile.dev), and run AppPilot from that WSL
  shell. `adb` running on Windows is reachable from WSL, so a device started on
  Windows can still be driven through Maestro under WSL.

The target package and trusted signing certificate are required configuration;
AppPilot no longer assumes an application ID from the filename or UI.

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

`/init` **does not** install missing prerequisites, build APKs, or send email —
it only checks, configures, and reports how to fix what is missing.

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

`scripts/check.py` runs every source check above except Docker. Dependency audit
requires access to the Python vulnerability index. Intentional compatibility
re-exports are declared through `__all__`; pyflakes otherwise runs without a
blanket exclusion, and a regression test proves a new unused import fails.

### CI validation strategy

Every pull request runs `.github/workflows/pr-source-validation.yml` with a
read-only GitHub token and no repository or production secrets. It validates
Python 3.11-3.13 syntax/tests, full-tree pyflakes, the 48-case workbook contract,
high-confidence secret patterns, changed-line whitespace, email relay behavior,
declared dependency vulnerabilities, and the email-service Docker build.
Official GitHub actions are pinned to immutable commit SHAs, checkout
credentials are not persisted, and the workflow uses `pull_request`, never
`pull_request_target`.

Ordinary PR CI intentionally does **not** run an emulator, physical device,
Maestro, ADB, APK installation, real model calls, account switching, Store
handoff, email delivery, or telemetry delivery.

Nightly or manually controlled validation should cover emulator smoke, fake-APK
integration, a non-sensitive model transport canary, and controlled workbook
execution. Release validation belongs in a credentialed controlled environment
and must cover the trusted real APK/signature, a real Android device, account
switching, Store handoff, supported links, and physical-device acceptance.

## Configuration

AppPilot reads configuration from environment variables. A local, **git-ignored**
`.env` file at the repository root is loaded automatically at startup (real
environment variables always take precedence).

| Variable | Required | Purpose |
| --- | --- | --- |
| `APPPILOT_MODEL` | yes* | Decision-model deployment/name (e.g. `gpt-4.1-mini`). |
| `APPPILOT_MODEL_AUTH` | no | Model authentication mode: `api_key` (default) or `entra`. |
| `APPPILOT_MODEL_API_KEY` | for API-key auth | API key for the model endpoint. Must be blank for Entra authentication. |
| `APPPILOT_MODEL_BASE_URL` | no for API-key auth; yes for Entra auth | OpenAI-compatible base URL. Entra mode requires an `https://<resource>.openai.azure.com/openai/v1` Azure OpenAI endpoint, so an Entra token can never be sent to another host. API-key mode defaults to the OpenAI v1 endpoint; remote endpoints must use HTTPS and HTTP is allowed only for loopback development endpoints. |
| `APPPILOT_EXPECTED_PACKAGE_ID` | yes for APK runs | Android application/package ID expected in the local and installed APK. |
| `APPPILOT_TRUSTED_CERT_SHA256` | yes for APK runs | Trusted signing-certificate SHA-256 fingerprint. Colons are optional; the value must contain exactly 64 hexadecimal characters. |
| `APPPILOT_USERNAME` | for standalone login | Default sign-in username/email for the standalone login prototype and legacy non-profile callers. |
| `APPPILOT_PASSWORD` | for standalone login | Default sign-in password, handled with the same local credential isolation. |
| `APPPILOT_USERNAME_<LICENSE>` | for Deeplink login | Username for an Excel License profile, where `<LICENSE>` is uppercase and separators become `_` (for example `Entra Premium` → `ENTRA_PREMIUM`). |
| `APPPILOT_PASSWORD_<LICENSE>` | for Deeplink login | Password paired with the corresponding License profile. |
| `APPPILOT_MAX_ACTIONS` | no | Absolute upper bound on actions per run (default `30`). |
| `APPPILOT_MAX_STUCK_ACTIONS` | no | Consecutive actions with no meaningful UI change before an early controlled FAIL (default `5`). |
| `APPPILOT_EMAIL_API_URL` | for email | HTTPS relay endpoint used to send the suite report. |
| `APPPILOT_EMAIL_API_KEY` | for email | Scoped API key for the email relay. |
| `APPPILOT_CONTACT_EMAIL` | no | Optional contact address shown in the report footer (omitted if unset). |
| `APPPILOT_TELEMETRY_URL` | for telemetry | HTTPS relay `/telemetry` endpoint. When set, each real suite run best-effort submits one minimal record (suite, test-case count, host OS, target platform) — nothing identifying. Unset disables telemetry. |
| `APPPILOT_TELEMETRY_API_KEY` | for telemetry | Telemetry-only API key; it must be different from the email relay credential. |

\* If no model is configured, the agent does **not** guess — it reports that it
cannot proceed and how to configure one.

Security reminders:

- Login credentials are resolved locally and are never included in model
  prompts, UI observations, logs, or source code.
- Before credential injection, AppPilot revalidates APK trust, the foreground
  package/window, and the expected field from a fresh hierarchy. Android's
  separate ADB/Maestro automation processes do not provide a mathematically
  atomic focus-and-inject primitive, so a residual platform race remains after
  that immediate validation.
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
# Optional: prevents Azure CLI from showing the subscription selector during
# AppPilot's interactive sign-in. It does not select or change a subscription.
APPPILOT_ENTRA_TENANT_ID=your-work-tenant-id
```

Entra mode uses `DefaultAzureCredential` and requests the
`https://ai.azure.com/.default` scope. The signed-in identity must have the
`Cognitive Services OpenAI User` role on the Azure OpenAI resource.

When Entra mode is selected, `/init` and `./run` first check whether an Azure
AI token can be acquired. In an interactive terminal, a missing or expired
credential offers to run `az login`; Azure CLI then completes its normal
browser or device-code sign-in flow. AppPilot never collects Azure passwords,
never changes the active Azure subscription, and does not launch a login in a
non-interactive terminal. A successful token check still requires the signed-in
identity to have the role above.

When `APPPILOT_ENTRA_TENANT_ID` is set, AppPilot limits Azure CLI sign-in to
that tenant, temporarily disables Azure CLI's subscription selector for that
one command, and suppresses its subscription-list output. Model inference does
not need a selected Azure subscription.

Deeplink suites resolve every workbook `License` before touching the APK or
device. License values are trimmed, compared case-insensitively, and converted
to an environment suffix by replacing non-alphanumeric separators with `_`.
Missing profile variables and ambiguous labels that map to the same suffix fail
configuration explicitly; Deeplink never falls back to the global credentials.
For installed cases, AppPilot installs the APK once and processes normalized
License groups contiguously. It verifies the active account, switches to an
existing account or uses **Add an account** with the cached profile login flow,
then performs exactly two stabilization cycles before that group's cases.
Account email matching stays local and is never sent to the decision model or
written to reports.

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

To run the deeplink use case directly (equivalent, bypassing use-case
selection):

```bash
python3 src/deeplink_runner.py
```

Options: `--excel` (explicit workbook path; otherwise a suite is selected from
`testcases/deeplinks/`), `--device`, `--max-attempts` (default `2`),
`--verify-timeout` (default `30` seconds), `--no-warm-up`, and `--apk`.

`--device` is an **optional** override. When omitted, the device selected during
`/init` (saved, gitignored, in `.apppilot_device`) is reused when still
connected and its saved identity still matches. If the saved device is no longer
connected, a single usable device is auto-detected. An unverified matching
transport fails cleanly, and legacy serial-only saved selections must be
refreshed by running `/init` again. If several usable devices are connected and
none is chosen, the run exits cleanly (code `2`) asking for
`--device <serial>`.

Only one AppPilot process may target a given normalized device serial at a
time. The runner acquires a fail-fast inter-process lock before device mutation
and releases it on normal completion and exceptions. If a lock error names a
live owner PID/run ID, stop that run or choose another device. A valid local
lock whose owner process is no longer alive is reclaimed automatically;
malformed or unverifiable lock metadata is retained and fails closed.

To run the standalone goal-driven agent prototype (login preparation only):

```bash
python3 src/apppilot_agent.py
```

Options: `--device`, `--max-actions`, `--max-stuck-actions`, `--guidance`.

Standalone login uses the same device resolution contract as the deeplink
runner. An explicit `--device` must be authorized and fully booted. When the
option is omitted, AppPilot reuses a valid device saved by `/init` only after
its identity is revalidated. If that saved device is absent, it can uniquely
auto-detect a usable device. It exits cleanly instead of guessing when a
matching transport's identity cannot be verified or when no usable device or
multiple usable devices are present.

## Deeplink test suite

The data-driven runner executes deeplink test cases from an Excel workbook,
reusing the same Maestro executor, UI observer, and model configuration as the
agent. The workbook requires **Test ID**, **Deep Link**, **License**, and
**Expected Result**, with optional **Installed**. Test suites live in
`testcases/deeplinks/`; when `--excel` is omitted the runner auto-runs the only
workbook there, prompts to choose when several exist, and exits cleanly (code
`2`) when none is found.

Workbook validation is strict. Explicit header rows must contain all required
columns; Test IDs must be unique case-insensitively; deeplinks must use HTTPS
and one of the AppPilot supported-link hosts. `Installed` accepts only
`true`, `t`, `yes`, `y`, `1`, `installed`, `false`, `f`, `no`, `n`, `0`,
`uninstalled`, `not installed`, or `fresh` (case-insensitive). Invalid data is
rejected with worksheet row/column context rather than being reinterpreted.

For each case, the suite establishes a clean state, launches the **exact**
deeplink through an Android VIEW intent over ADB, observes the resulting UI,
checks package/activity and known destination evidence deterministically, and
then uses the **AI judge** only for remaining semantic ambiguity. Deterministic
contradictions and empty UI cannot be overridden by the model. The
`--verify-timeout` window is a per-attempt monotonic polling deadline that starts
after the deeplink is launched. Preparation, installation, login, retry setup,
and cleanup do not consume it. Model, ADB/Maestro, and installer operations keep
their own finite bounds, and ordinary destination-mismatch failures stay
distinguishable. Failed cases retry (default **2 attempts** total) with a fresh
verification window, and the suite continues to the next case.

Case cleanup always attempts to stop the target app and clear the clipboard.
Suite-final cleanup repeats those safety operations and restores AppPilot's
temporary supported-link user-selection, handling, and domain-verification
state when they were captured. Android domain verification is asynchronous, so
restoration waits within the cleanup deadline and reports a cleanup failure if
the captured state cannot be re-established. AppPilot intentionally does
**not** reinstall a previously present APK, restore accounts, permissions, app
cache/data, or unrelated applications.

Each failed case retains bounded, sanitized JSON evidence under
`.apppilot_artifacts/<run-id>/`, and the final report prints the artifact path.
Artifacts include available run/case/device/APK, package/activity,
deterministic/model, retry, UI-observation, and cleanup context. They are
uniquely named per case/attempt, capped at 64 KiB, and gitignored. Screenshots
are not retained because AppPilot cannot currently guarantee pixel-level secret
redaction. See
[`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) for the
installed/uninstalled orchestration details.

## APK

AppPilot installs **only a user-provided local `.apk`** — there is **no** build,
enlistment, or store acquisition. Configure it through `/init`, or supply the
path at runtime with `--apk PATH`. If omitted, a previously saved path (in
`.apppilot_apk`, gitignored) is offered as a bracketed default —
`APK path [<saved-path>]:` — where Enter keeps the saved path and a new valid
path replaces it; with no saved path you are asked for one (`APK path:`). The
path is normalized and validated (exists, is a file, `.apk`, readable) before the
suite runs; an invalid path exits cleanly with code `2`.

## Email reporting

Emailing the suite report is **optional**. In `/init`, the flow asks
`Configure email reporting? [y/N]` (default No) and, when enabled, prompts for
`Email recipient [saved@email.com]`. On a normal run the suite asks up front
`Send the suite report by email when it finishes? [y/N]`; answering **Yes**
always shows the recipient prompt (Enter reuses the saved recipient, a new
address replaces it). The saved recipient is stored in `.apppilot_recipient`
(gitignored).

Email delivery needs `APPPILOT_EMAIL_API_URL` and `APPPILOT_EMAIL_API_KEY`; an
optional `APPPILOT_CONTACT_EMAIL` appears in the report footer. Neither `/init`
nor email prompting ever sends mail on its own — delivery only happens after a
suite finishes, and never affects the run verdict.

## Telemetry

Telemetry is **optional** and off unless `APPPILOT_TELEMETRY_URL` is set. When
configured, each real suite run best-effort submits **one** minimal record —
suite name, test-case count, host OS, and target platform — over HTTPS to the
relay's `/telemetry` endpoint, which stores it in Azure Table Storage. Nothing
identifying is collected, no Azure credential lives in the CLI, and a failure
never affects the suite.

## Documentation

- [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) — architecture,
  component boundaries, decision-model contract, safety model, and the deeplink
  use-case execution details.
