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

It reaches **PASS** when a deterministic goal evaluator recognizes the goal
state, and **FAIL** when the model cannot safely proceed or a configurable
action limit is hit. Unexpected screens (permission prompts, "save password"
dialogs, onboarding pages, error pop-ups, etc.) are handled by *reasoning*, not
by pre-scripting every screen.

> **Roles:** the LLM is the **brain** (chooses the next action), the Android UI
> hierarchy is the **eyes**, Maestro is the **hands** (execution only), and the
> test goal is the **objective**.

See [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) for the full
architecture, component boundaries, decision-model contract, safety model, and
design principles.

## Project structure

- `src/apppilot_agent.py` — the entire agent: UI observer, deterministic goal
  evaluator, replaceable decision-model provider, safety validator, Maestro
  executor, and the orchestration loop.
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
- **A running Android emulator** with the target app installed. Start one from
  Android Studio (Device Manager) or the terminal, and confirm it is connected:
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

The current prototype goal is to *complete authentication and onboarding and
reach a usable signed-in Microsoft 365 Copilot experience, without executing an
unintended suggested prompt.*

## Deeplink test suite

A data-driven runner (`src/deeplink_runner.py`) executes a suite of deeplink
test cases from an Excel workbook. It reuses the same Maestro executor, Maestro
UI observer, and model configuration as the agent — it is **not** a second
framework.

The Excel is intentionally simple, with four positional columns and one row per
test: **Test ID**, **Deep Link**, **User Type**, **Expected Result** (a header
row, if present, is skipped). A bundled copy lives at
`testcases/deeplinks/deeplink_tests.xlsx`.

For each test case:

1. A one-time first-install **warm-up** runs once before the suite (skippable
   with `--no-warm-up`); it is never repeated for retries.
2. The **exact** deeplink is launched deterministically via Maestro `openLink`.
3. The resulting Android UI is observed.
4. The **AI judges** whether the observed UI *semantically* satisfies the
   natural-language Expected Result — including expected error/failure states,
   which count as PASS when correctly observed. The model never invents or
   modifies a deeplink and never drives UI actions here.
5. On a mismatch the runner **kills the app, waits 2 s, and re-launches the same
   deeplink**, up to **3 attempts**. Any matching attempt is a PASS; three
   mismatches is a FAIL. The suite then continues with the next case.

```bash
python3 src/deeplink_runner.py --device emulator-5554
```

Options: `--excel` (workbook path), `--device`, `--max-attempts` (default `3`),
`--no-warm-up`. At the end it prints a concise per-test report plus totals
(Total / Passed / Failed).
