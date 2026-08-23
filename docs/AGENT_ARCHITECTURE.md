# AppPilot Agent Architecture

## Purpose

AppPilot is a **goal-driven AI agent for Android regression testing**. Given a
test goal, it drives a real Android app toward that goal by *reasoning over the
live UI*, rather than replaying a fixed script.

This document is the **authoritative description of AppPilot's core
smart-agent model**. AppPilot is a smart agent, **not** a collection of
hardcoded Maestro scripts and **not** a fixed A→B→C test script.

## Mental model

| Role | Responsibility |
| --- | --- |
| **Brain** | Seeded and successfully cached decisions first; the LLM reasons over unknown states. |
| **Eyes** | UIAutomator hierarchy first, with automatic same-step Maestro fallback. |
| **Hands** | ADB for fast deterministic actions; Maestro for selector, secure, and complex actions. |
| **Objective** | The test goal (and optional guidance) that defines success. |

## Core loop

```
                    TEST GOAL
                        |
                        v
                     OBSERVE  (Android UI hierarchy -> compact observation)
                        |
                Is goal reached?  (authoritative evaluator)
                   /          \
                 YES           NO
                  |             |
                PASS      SELECT known/cached/model decision
                              |
                     VALIDATE the proposed action  (safety validator)
                              |
                       EXECUTE through ADB/Maestro
                              |
                          OBSERVE again
                              |
                     (repeat toward the goal)
```

The loop ends when:

- the goal is reached &rarr; **PASS**;
- the goal evaluator recognizes a definitive terminal failure (for example,
  rejected credentials) &rarr; **FAIL** immediately with the actionable reason;
- the model cannot safely propose a next action &rarr; **FAIL** (controlled);
- the meaningful UI state does not change for too many consecutive actions
  &rarr; **FAIL** (controlled, stuck detection — see below);
- a configurable absolute action/step limit is reached &rarr; **FAIL**.

The agent must handle **unexpected intermediate UI**. For example, if a login
unexpectedly shows a "Save password to Google Password Manager" dialog with
`Never` / `Save`, the agent observes it, the model reasons that it is an
incidental interruption, chooses a safe dismissal (`Never`), executes it via
Maestro, and continues toward the original goal — **without** that screen ever
being hardcoded.

## Components and boundaries

| Component | Owns | Does **not** own |
| --- | --- | --- |
| **Observer** | Turning the raw UI hierarchy into a bounded, relevant observation (text, accessibility text, resource ids, clickable/enabled/input state, useful relationships). | Deciding actions. |
| **Login goal evaluator** | Owning the login-stop boundary: deterministic terminal/blocker evidence first, semantic classification only for ambiguous screens. | Choosing or executing actions. |
| **Adaptive decision cache** | Trying seeded rules, then decisions learned from successful runs, then the model fallback. | Executing actions or declaring PASS. |
| **Decision model (provider)** | Proposing the next action for an unknown state, or declaring "cannot safely proceed". | Executing anything; declaring PASS; inventing elements. |
| **Safety validator** | Enumerating safe actions and validating every proposed action before execution. | Deciding intent. |
| **Android executor** | Executing a validated action through ADB or Maestro as appropriate. | Orchestration or decision-making. |
| **Agent loop** | Sequencing observe &rarr; evaluate &rarr; decide &rarr; validate &rarr; execute; enforcing limits; printing the trace. | Any UI-specific knowledge. |

## Decision model contract

The model receives:

- the test goal;
- optional guidance;
- the current UI observation;
- the list of **available safe actions**;
- the current execution context (step, limit, recent actions).

The model returns **either**:

1. a **UI action** — tap an observed element, input text into an observed
   input, or press back; or
2. a clear **"cannot safely proceed"** decision.

Rules:

- The model **only** selects from the supplied safe actions, so it can never
  invent an element that is not in the current observation.
- The model **never executes** anything and **never declares PASS**.
- The concrete model is selected through configuration/environment, so the
  agent loop does not depend on any specific provider, model, or credential.
  Credentials are read from environment variables and are never hardcoded.

## Safety

The safety validator is **independent of the model**. Even though the model
picks from pre-filtered safe actions, every proposed action is re-validated
against the latest observation before Maestro runs it. An action is rejected
when it:

- does not correspond to an observed element;
- targets a disabled element, or a non-clickable element for a tap;
- is an input action against a non-input element, or lacks text;
- uses arbitrary screen coordinates; or
- is destructive — purchases, account deletion, password/security changes,
  sign-out, or unsafe permission granting.

An invalid or unsafe proposal is **not executed**; the agent returns a
controlled failure (or may re-ask the model) instead.

## Progress / stuck detection

Two independent limits protect a run:

- **Absolute bound** — `--max-actions` / `APPPILOT_MAX_ACTIONS` (default `30`)
  caps total actions per run.
- **Stuck bound** — `--max-stuck-actions` / `APPPILOT_MAX_STUCK_ACTIONS`
  (default `5`) stops the run early when it is making no meaningful progress.

After each action the agent re-observes and computes a **meaningful
fingerprint** of the screen: a stable, non-secret signature built from each
element's resource id, redacted label, and clickable/input/enabled state, and
restricted to elements that are interactive or carry a resource id. Purely
decorative, id-less, non-interactive text (clocks, animation captions) is
excluded, so volatile noise does not look like a change. Credential values are
already redacted by the observer, so a secret can never enter the fingerprint.

A **consecutive-stuck counter** advances whenever an action leaves the
meaningful fingerprint unchanged, and **resets** on any meaningful change. Only
transitions *after* an action are counted, and the generous default (`5`) leaves
room for legitimate multi-action work on a single screen (keyboard/input or
internal-state changes that do not immediately alter the hierarchy). Reaching
the threshold produces a controlled **FAIL** ("agent appears stuck"). The
detector is a safety/reliability mechanism only — it never decides *which*
action to take; the model remains the sole decision-maker.

Configuration precedence for the stuck bound is
`--max-stuck-actions` &rarr; `APPPILOT_MAX_STUCK_ACTIONS` &rarr; `5`, with
invalid environment values falling back safely to the default.

## Login completion

Login is preparation, not deeplink verification. The authoritative evaluator
first recognizes deterministic blockers and terminal states: authentication
controls, loading/no-actionable states, Chat/composer, Search, restricted-access
states, rejected credentials, and the suggested-prompt interruption. Rejected
credentials are terminal failures: the agent reports the configuration problem
immediately instead of retrying the same secret or asking the Brain to choose
another sign-in route. Ambiguous actionable screens may be semantically
classified by the login judge. Once login completion is true, the agent stops
before asking the Brain and hands the current UI to deeplink verification. Login
PASS never implies deeplink PASS.

Credentials remain in `RuntimeContext`, outside observations and model requests.
The standalone login builder retains the default global environment context,
while use cases may inject a resolved context. Deeplink normalizes each Excel
License to profile-specific environment-variable names, validates all profiles
before APK/device work, and caches one shared login flow per profile. Installed
multi-License execution uses a deterministic account-session capability:
account names/emails are inspected only in local observations for exact matching
and are never rendered into model prompts, traces, exceptions, or reports.

## Code layering (shared nodes and use cases)

The code is organized in three one-way layers:

```
usecases  ->  shared  ->  apppilot
```

- **`apppilot/`** is the generic framework (agent loop, Android/Maestro,
  model boundary, safety, data contracts, reporting). It knows nothing about
  any use case.
- **`shared/`** holds reusable, use-case-agnostic nodes that use cases compose:
  `model_client` (OpenAI-compatible transport), `installer`, `warmup`, and the
  generic `login/` node. Shared nodes stay generic — they never import a
  specific use case — and let callers supply runtime behavior (for example,
  login accepts a `LoginPolicy` describing the desired terminal states in
  natural language).
- **`usecases/`** holds business/test-specific behavior; each use case gets its
  own folder. `usecases/deeplink/` is the authoritative Deeplink
  implementation and composes the shared nodes plus the `apppilot` framework.

`flows/login.py`, `flows/deeplink.py`, and `deeplink_runner.py` are thin
compatibility shims/entry points that re-export the authoritative
`shared`/`usecases` surfaces.

## Deeplink test suite (data-driven runner)

The Deeplink use case now lives under `src/usecases/deeplink/` (composing the
shared login/installer/warmup/model-client nodes); `src/deeplink_runner.py`
remains a compatibility entry point that re-exports it. It layers a
**data-driven deeplink runner** on top of the
same components — the Maestro executor, the Maestro observer, and the same
OpenAI-compatible model configuration — rather than forking a second framework.
It keeps the core boundaries: deterministic execution, AI reasoning, and
deterministic evaluation of PASS.

Its per-case shape is deliberately narrow (a single launch + observe + judge,
not the action loop):

```
Excel test case  ->  launch EXACT deeplink (Maestro openLink, deterministic)
                 ->  observe resulting UI (UIAutomator, Maestro fallback)
                 ->  AI judges observed-vs-Expected-Result (semantic)
                 ->  PASS, or kill + wait 2s + relaunch (deterministic retry)
```

Boundaries:

- **The test case decides which deeplink to run**; it is executed verbatim. The
  model never invents, modifies, or chooses a deeplink.
- **The AI only judges** whether the observed UI *semantically* satisfies the
  natural-language Expected Result. Named destinations such as Researcher,
  Cowork, and Chat are distinct, and prompt presence, absence, or specified
  content must agree. No hardcoded selectors or app-specific success rules; an
  expected error that is correctly observed is a PASS, because the result is
  *observed vs expected*, not *did the deeplink succeed*.
- **Retry and reporting are deterministic.** Two attempts per case by default.
  Installed retries stop, wait 2 seconds, and reopen the same deeplink;
  uninstalled retries recreate the complete fresh-install sequence. Any
  matching attempt is a PASS; exhausted attempts are a FAIL.
- **Installed cases are grouped by normalized License** in first-seen order.
  The APK is installed once. After the first successful login, Android
  supported-link routing is configured once for the installed package. Each
  group then verifies/adds/switches the exact local account, runs two
  stabilization cycles, and executes its cases contiguously. Account switches
  do not repeat package link preparation. Group setup failures do not block
  later groups. Report results are restored to workbook order.
- **Stabilization runs exactly twice per successfully prepared License group**
  (skippable), never for failed groups, uninstalled cases, or per-case retries.
- **Uninstalled cases remain independent fresh-install flows** and are never
  grouped or account-switched.

The Excel has four required fields (Test ID, Deep Link, License, Expected
Result) and an optional Installed field. Recognized headers may be reordered;
otherwise columns A-D are used.

## Maestro as the execution layer

Maestro is used purely as the UI **execution layer**: the agent observes the UI,
the model chooses one action, and the agent drives Maestro to perform that single
action before observing again. The agent does **not** execute any prewritten
Maestro flow. Login, onboarding, and unexpected interruptions are all handled by
reasoning over the current UI and issuing individual Maestro actions, with the
goal coming from the test.

## Prototype execution

Run the agent against the connected emulator:

```
python3 src/apppilot_agent.py --device emulator-5554
```

The trace shows the loop explicitly:

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

If no model is configured, AppPilot does **not** guess: it honestly reports
"cannot safely proceed" and how to configure a model. This keeps the mock
boundary from pretending to be intelligent.

## Design principles

- **Adaptive decisions.** Use validated seeded rules and decisions cached only
  after complete success; unknown or ambiguous states still use the model.
- **Hybrid Android tooling.** UIAutomator observes first; ADB executes fast
  deterministic actions; Maestro handles hierarchy fallback, selectors, secure
  input, and complex actions.
- **Credential fidelity.** Credentials are resolved locally. Username entry may
  use verified paced ADB input; password entry uses Maestro's secure clipboard
  path. Secrets stay out of model requests, generated flow files, and logs.
- **Deterministic PASS.** Goal evaluation is separate and never delegated to
  the model.
- **Independent safety.** Validation is separate from decision-making and gates
  every action.
- **Replaceable model boundary.** The real model can be connected via
  configuration without changing the agent loop, observer, goal evaluator,
  safety validator, or Android executor.
- **Small and modular.** Introduce components only when the current milestone
  needs them.

## Intentionally out of scope for the agent core

The **agent core itself**
(`AppPilotAgent`, observer, goal evaluator, decision provider, safety validator)
remains free of test-data, reporting, and fixed-script concerns. Such
capabilities are only ever added as **separate layers on top** of the core — as
the deeplink runner does for Excel test-case loading and report generation,
while reusing (not modifying) the core boundaries. Still deferred: user
credential workbooks, login/deeplink automation as fixed A→B→C scripts,
scheduling, parallel or cloud execution, a web UI, historical reporting, and
self-healing across runs.
