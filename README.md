# AppPilot

AppPilot is a **goal-driven AI agent for Android regression testing**. Given a
test goal and a connected Android device, it drives a real app toward that goal
by *reasoning over the live UI* — it is **not** a fixed A→B→C script and **not**
a collection of hardcoded Maestro flows.

The agent runs a simple loop: observe the UI → check whether the goal is reached
→ ask the decision model for one action → validate it for safety → execute it
via Maestro → observe again. The LLM is the **brain**, the Android UI hierarchy
is the **eyes**, Maestro is the **hands** (execution only), and the test goal is
the **objective**. It reaches **PASS** when the goal evaluator recognizes the
goal state and **FAIL** when it cannot safely proceed or an action limit is hit.

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

### macOS

- **Python 3.11+**.
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

- **Python 3.11+** — from [python.org](https://www.python.org/downloads/windows/)
  (tick "Add python.exe to PATH").
- **Android Studio / Android SDK** — the simplest way to get the emulator and to
  create an AVD. Add the SDK's `platform-tools` and `emulator` directories to
  your `PATH` (they ship `adb.exe` and `emulator.exe`, found automatically).
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

The prototype targets the Office hub app id
`com.microsoft.office.officehubrow`.

## Setup

Run the `/init` setup flow from the project root:

```bash
python3 src/apppilot_init.py
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

## Configuration

AppPilot reads configuration from environment variables. A local, **git-ignored**
`.env` file at the repository root is loaded automatically at startup (real
environment variables always take precedence).

| Variable | Required | Purpose |
| --- | --- | --- |
| `APPPILOT_MODEL` | yes* | Decision-model deployment/name (e.g. `gpt-4.1-mini`). |
| `APPPILOT_MODEL_API_KEY` | yes* | API key for the model endpoint. |
| `APPPILOT_MODEL_BASE_URL` | no | OpenAI-compatible base URL (defaults to the OpenAI v1 endpoint). |
| `APPPILOT_USERNAME` | for login goals | Sign-in username/email for the **app under test** — resolved locally and injected into Maestro, never sent to the model. |
| `APPPILOT_PASSWORD` | for login goals | Sign-in password for the app under test, handled the same secure way. |
| `APPPILOT_MAX_ACTIONS` | no | Absolute upper bound on actions per run (default `30`). |
| `APPPILOT_MAX_STUCK_ACTIONS` | no | Consecutive actions with no meaningful UI change before an early controlled FAIL (default `5`). |
| `APPPILOT_EMAIL_API_URL` | for email | HTTPS relay endpoint used to send the suite report. |
| `APPPILOT_EMAIL_API_KEY` | for email | Scoped API key for the email relay. |
| `APPPILOT_CONTACT_EMAIL` | no | Optional contact address shown in the report footer (omitted if unset). |

\* If no model is configured, the agent does **not** guess — it reports that it
cannot proceed and how to configure one.

Security reminders:

- Login credentials are resolved locally and are never included in model
  prompts, UI observations, logs, or source code.
- Keep `.env` local and never commit real credentials or API keys.

Example `.env`:

```dotenv
APPPILOT_MODEL=gpt-4.1-mini
APPPILOT_MODEL_API_KEY=your-key-here
APPPILOT_MODEL_BASE_URL=https://your-endpoint.openai.azure.com/openai/v1
APPPILOT_USERNAME=you@example.com
APPPILOT_PASSWORD=your-password
```

## Running AppPilot

With a device connected and `.env` in place, run the **deeplink test suite**:

```bash
python3 src/deeplink_runner.py
```

Options: `--excel` (workbook path), `--device`, `--max-attempts` (default `2`),
`--verify-timeout` (default `30` seconds), `--no-warm-up`, and `--apk`.

`--device` is an **optional** override. When omitted, the device selected during
`/init` (saved, gitignored, in `.apppilot_device`) is reused when still
connected; otherwise a single connected device is auto-detected. If several
usable devices are connected and none is chosen, the run exits cleanly (code `2`)
asking for `--device <serial>`.

To run the standalone goal-driven agent prototype (login preparation only):

```bash
python3 src/apppilot_agent.py
```

Options: `--device`, `--max-actions`, `--max-stuck-actions`, `--guidance`.

## Deeplink test suite

The data-driven runner executes deeplink test cases from an Excel workbook,
reusing the same Maestro executor, UI observer, and model configuration as the
agent. The workbook requires **Test ID**, **Deep Link**, **User Type**, and
**Expected Result**, with optional **Installed**. A bundled workbook lives at
`testcases/deeplinks/deeplink_tests.xlsx`.

For each case, the suite establishes a clean state, launches the **exact**
deeplink via Maestro `openLink`, observes the resulting UI, and the **AI judges**
whether it semantically satisfies the natural-language Expected Result (expected
error/failure states count as PASS). Failed cases retry (default **2 attempts**
total) and the suite continues to the next case, ending with a per-test report
plus totals. See [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) for
the installed/uninstalled orchestration details.

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

## Documentation

- [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) — architecture,
  component boundaries, decision-model contract, safety model, and the deeplink
  use-case execution details.
