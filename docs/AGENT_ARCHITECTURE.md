# AppPilot Agent Architecture

## Purpose

AppPilot is a goal-driven AI agent for Android regression testing. Given a test
goal, it drives a real Android app toward that goal from the live UI.

## Mental model

| Role | Responsibility |
| --- | --- |
| **Brain** | Seeded and successfully cached decisions first; the LLM reasons over unknown states. |
| **Eyes** | UIAutomator hierarchy first, with automatic same-step Maestro fallback. |
| **Hands** | ADB for fast deterministic actions; Maestro for selector, secure, and complex actions. |
| **Objective** | The test goal (and optional guidance) that defines success. |

## Core loop

The agent repeats: observe UI, evaluate the goal, select a known/cached/model
decision, validate the action, execute through ADB/Maestro, and observe again.
It returns **PASS** when the goal is reached and controlled **FAIL** for
terminal blockers, unsafe/no-action decisions, stuck UI, or action limits.

## Components and boundaries

| Component | Owns | Does **not** own |
| --- | --- | --- |
| **Observer** | Producing bounded, redacted UI observations. | Deciding actions. |
| **Login goal evaluator** | Owning deterministic and semantic login-stop checks. | Choosing or executing actions. |
| **Adaptive decision cache** | Trying seeded, learned, then model decisions. | Executing actions or declaring PASS. |
| **Decision model (provider)** | Proposing actions or controlled failure. | Executing anything; declaring PASS; inventing elements. |
| **Safety validator** | Enumerating safe actions and validating every proposed action before execution. | Deciding intent. |
| **Android executor** | Executing a validated action through ADB or Maestro as appropriate. | Orchestration or decision-making. |
| **Agent loop** | Sequencing the run and enforcing limits. | Any UI-specific knowledge. |

## Decision model contract

The model receives the goal, optional guidance, current UI observation,
available safe actions, and execution context. It returns a UI action or a
clear **"cannot safely proceed"** decision. The model never executes actions,
declares PASS, or invents unavailable elements.

## Safety

The safety validator checks every proposed action against the latest
observation. It rejects actions that:

- does not correspond to an observed element;
- targets a disabled element, or a non-clickable element for a tap;
- is an input action against a non-input element, or lacks text;
- uses arbitrary screen coordinates; or
- is destructive — purchases, account deletion, password/security changes,
  sign-out, or unsafe permission granting.

Invalid or unsafe proposals are not executed.

## Progress / stuck detection

Two limits protect a run:

- **Absolute bound** — `--max-actions` / `APPPILOT_MAX_ACTIONS` (default `30`)
  caps total actions per run.
- **Stuck bound** — `--max-stuck-actions` / `APPPILOT_MAX_STUCK_ACTIONS`
  (default `5`) stops repeated no-progress actions.

The agent compares meaningful UI fingerprints after actions and returns a
controlled **FAIL** when the stuck bound is reached.

## Login completion

Login is preparation, not deeplink verification. The authoritative evaluator
first recognizes deterministic blockers and terminal states: authentication
controls, loading/no-actionable states, Chat/composer, Search, restricted-access
states, rejected credentials, and the suggested-prompt interruption. Rejected
credentials are terminal failures: the agent reports the configuration problem
immediately. Ambiguous actionable screens may be semantically
classified by the login judge. Once login completion is true, the agent stops
before asking the Brain and hands the current UI to deeplink verification. Login
PASS never implies deeplink PASS.

Credentials remain in `RuntimeContext`, outside observations and model requests.
Use cases may inject resolved contexts and terminal policies.

## Code layering (shared nodes and use cases)

The code is organized in three layers:

```
usecases  ->  shared  ->  apppilot
```

- **`apppilot/`** is the generic framework (agent loop, Android/Maestro,
  model boundary, safety, data contracts, reporting). It knows nothing about
  any use case.
- **`shared/`** holds reusable nodes that use cases compose:
  `model_client` (OpenAI-compatible transport), `installer`, `warmup`, and the
  generic `login/` node.
- **`usecases/`** holds business/test-specific behavior; each use case gets its
  own folder. `usecases/deeplink/` is the authoritative Deeplink
  implementation and composes the shared nodes plus the `apppilot` framework.

`flows/login.py`, `flows/deeplink.py`, and `deeplink_runner.py` are thin
compatibility shims/entry points that re-export the authoritative
`shared`/`usecases` surfaces.

## Deeplink use case boundary

`src/usecases/deeplink/` composes shared login, installer, warm-up, model,
Maestro, and Android components. The workbook supplies the deeplink and
Expected Result; the model only judges observed UI against that Expected
Result. See
[`src/usecases/deeplink/README.md`](../src/usecases/deeplink/README.md).

## Android execution layer

ADB handles high-frequency deterministic operations such as deeplink dispatch,
app launch/stop, coordinate taps, and Back. Maestro is reserved for hierarchy
fallback, selector-only controls, store-button interaction, and secure password
entry. Login, onboarding, and unexpected interruptions are handled one validated action
at a time.

Maestro receives a minimal allowlisted subprocess environment. Credential entry
uses a scoped variable, and clipboard contents are cleared immediately after
paste.

The deeplink CLI owns a per-device process lock, case cleanup, suite cleanup,
and internal sanitized artifacts.
