# AppPilot

AppPilot is a **goal-driven AI agent for Android regression testing**. Given a
test goal and a running Android emulator, it drives a real app toward that goal
by *reasoning over the live UI* — it is **not** a fixed A→B→C script and **not**
a collection of hardcoded Maestro flows.

The agent runs a simple loop:

```
observe the UI → is the goal reached? → ask the decision model for one action
→ validate it for safety → execute it via Maestro → observe again → repeat
```

It reaches **PASS** when the authoritative goal evaluator recognizes the goal
state, and **FAIL** when the model cannot safely proceed or a configurable
action limit is hit. Login completion uses deterministic evidence first and
semantic classification only for ambiguous screens. Unexpected screens are
handled by reasoning rather than pre-scripting every screen.

> **Roles:** the LLM is the **brain** (chooses the next action), the Android UI
> hierarchy is the **eyes**, Maestro is the **hands** (execution only), and the
> test goal is the **objective**.

See [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) for the full
architecture, component boundaries, decision-model contract, safety model, and
design principles.

## Project structure

- `src/apppilot/` — reusable agent loop, Android/Maestro integration, model
  boundary, safety validation, data contracts, build helper, and reporting.
- `src/flows/login.py` — the shared authentication/onboarding preparation flow.
- `src/flows/deeplink.py` — workbook loading, installed/uninstalled orchestration,
  retries, verification, and reporting.
- `src/apppilot_agent.py` and `src/deeplink_runner.py` — compatibility CLI
  entry points.
- `docs/` — project documentation.
- `testcases/regressions/regression_suite.xlsx` — regression workbook (reserved
  for later; workbook/Excel integration is intentionally out of scope for the
  current agent core).

## Prerequisites

- **Python 3.11+** (the agent uses only the standard library — no third-party
  packages required).
- **Maestro CLI** (requires Java 17+). Install on macOS with:
  ```bash
  curl -fsSL "https://get.maestro.mobile.dev" | bash
  # then ensure $HOME/.maestro/bin is on your PATH
  maestro --version
  ```
- **A running Android emulator**. The standalone login CLI requires the target
  app to be installed; the deeplink suite manages its local APK installation.
  Start a device and confirm it is connected:
  ```bash
  adb devices        # e.g. emulator-5554  device
  ```

The prototype targets the Office hub app id
`com.microsoft.office.officehubrow`.

## Configuration

AppPilot reads all configuration from environment variables. A local, **git-
ignored** `.env` file at the repository root is loaded automatically at startup
(real environment variables always take precedence, and nothing secret is ever
committed).

| Variable | Required | Purpose |
| --- | --- | --- |
| `APPPILOT_MODEL` | yes* | Decision-model deployment/name (e.g. `gpt-4.1-mini`). |
| `APPPILOT_MODEL_API_KEY` | yes* | API key for the model endpoint. |
| `APPPILOT_MODEL_BASE_URL` | no | OpenAI-compatible base URL (defaults to the OpenAI v1 endpoint). |
| `APPPILOT_USERNAME` | for login goals | **Sign-in username/email for the app under test** (e.g. the Microsoft account used to log into the Office hub app) — resolved locally and injected straight into Maestro, never sent to the model. Not the model API key. |
| `APPPILOT_PASSWORD` | for login goals | **Sign-in password for the app under test**, handled the same secure way. |
| `APPPILOT_MAX_ACTIONS` | no | Absolute upper bound on actions per run (default `30`). |
| `APPPILOT_MAX_STUCK_ACTIONS` | no | Consecutive actions with no meaningful UI change before the run stops early with a controlled FAIL (default `5`). |

\* If no model is configured, the agent does **not** guess — it honestly reports
that it cannot proceed and how to configure one.

Credentials are never placed in prompts, observations, logs, exceptions,
Maestro arguments, or source. The model can only *request* a credential by kind
(`username` / `password`); AppPilot resolves the real value locally and hands it
to Maestro securely.

> **Note:** `APPPILOT_USERNAME` / `APPPILOT_PASSWORD` are the **login credentials
> for the app being tested** (the account AppPilot signs in with when a scenario
> reaches a login screen). They are **only** needed for login scenarios, and are
> unrelated to the model API key (`APPPILOT_MODEL_API_KEY`) or any GitHub
> credentials.

Example `.env`:

```dotenv
APPPILOT_MODEL=gpt-4.1-mini
APPPILOT_MODEL_API_KEY=your-key-here
APPPILOT_MODEL_BASE_URL=https://your-endpoint.openai.azure.com/openai/v1
APPPILOT_USERNAME=you@example.com
APPPILOT_PASSWORD=your-password
```

## Running the agent

With the emulator running and `.env` in place:

```bash
python3 src/apppilot_agent.py --device emulator-5554
```

Options:

- `--device` — ADB device id (default `emulator-5554`).
- `--max-actions` — override the absolute per-run action bound.
- `--max-stuck-actions` — override the early-termination stuck bound.
- `--guidance` — override the default high-level guidance given to the model.

The run prints the loop explicitly, for example:

```
GOAL
OBSERVE
MODEL DECISION
SAFETY VALIDATION
ACTION
OBSERVE
GOAL REACHED
RESULT
```

The login goal is preparation only: complete authentication and required
onboarding, dismiss the initial suggested-prompt interruption when present, then
stop immediately and hand the current UI to the caller.

## Deeplink test suite

A data-driven runner (`src/deeplink_runner.py`) executes a suite of deeplink
test cases from an Excel workbook. It reuses the same Maestro executor, Maestro
UI observer, and model configuration as the agent — it is **not** a second
framework.

The Excel has four required fields: **Test ID**, **Deep Link**, **User Type**,
and **Expected Result**, plus an optional **Installed** field. Header names may
be reordered; without a recognized header the four required fields use columns
A-D. When **Installed** is omitted, the scenario is derived deterministically
from the deeplink. A bundled copy lives at
`testcases/deeplinks/deeplink_tests.xlsx`.

For each test case:

1. The suite establishes a clean app state.
2. Installed cases run as one batch: install/launch, shared login preparation,
   then one warm-up for the entire installed batch.
3. Uninstalled cases recreate uninstall → exact deeplink while absent → local
   APK install/open → shared login preparation on every attempt. They never run
   warm-up.
4. The **exact** deeplink is launched deterministically via Maestro `openLink`.
5. The resulting Android UI is observed.
6. The **AI judges** whether the observed UI *semantically* satisfies the
   natural-language Expected Result — including expected error/failure states,
   which count as PASS when correctly observed. The model never invents or
   modifies a deeplink and never drives UI actions here.
7. Installed retries stop, wait 2 seconds, and reopen the same deeplink.
   Uninstalled retries recreate the complete fresh-install sequence. The default
   is **2 attempts** total. The suite continues with the next case after failure.

```bash
python3 src/deeplink_runner.py --device emulator-5554
```

Options: `--excel` (workbook path), `--device`, `--max-attempts` (default `2`),
`--verify-timeout` (default `30` seconds), `--no-warm-up`, and `--rebuild`.
At the end it prints a concise per-test report plus totals.

The current local-APK workflow expects a clean OfficeMobile enlistment at
`/Volumes/Office/omr1/src` already on `lkg/main/android`. AppPilot never commits,
switches, or pulls that enlistment automatically. A pre-existing APK is reused
unless `--rebuild` is supplied.

For uninstalled cases, deferred deeplink/referrer delivery after sideloading is
a live Android acceptance requirement; unit tests validate orchestration but
cannot prove platform delivery.
