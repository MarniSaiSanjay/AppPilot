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
| **Brain** | The LLM decision model — chooses the next action by reasoning over the current UI and the goal. |
| **Eyes** | The Android UI hierarchy (via Maestro/ADB), reduced to a small, useful observation. |
| **Hands** | Maestro — executes the chosen action. Execution layer only. |
| **Objective** | The test goal (and optional guidance) that defines success. |

## Core loop

```
                    TEST GOAL
                        |
                        v
                     OBSERVE  (Android UI hierarchy -> compact observation)
                        |
                Is goal reached?  (deterministic evaluator)
                   /          \
                 YES           NO
                  |             |
                PASS      ASK THE MODEL what to do next
                              |
                     VALIDATE the proposed action  (safety validator)
                              |
                     EXECUTE through Maestro
                              |
                          OBSERVE again
                              |
                     (repeat toward the goal)
```

The loop ends when:

- the goal is reached &rarr; **PASS**;
- the model cannot safely propose a next action &rarr; **FAIL** (controlled);
- a configurable action/step limit is reached &rarr; **FAIL**.

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
| **Goal evaluator** | Deterministically deciding whether the goal state is reached. | Choosing actions; it is independent of the model. |
| **Decision model (provider)** | Proposing the single next action, or declaring "cannot safely proceed", given the goal, guidance, observation, available safe actions, and execution context. | Executing anything; declaring PASS; inventing elements. |
| **Safety validator** | Enumerating safe actions and validating every proposed action before execution. | Deciding intent. |
| **Maestro executor** | Executing a validated action against the device. | Orchestration or decision-making. |
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

## Goal evaluation

Goal evaluation is **deterministic and separate** from the decision model. For
the current prototype, PASS means a **usable signed-in Microsoft 365 Copilot
experience** is reached — the Copilot message composer is present and the app is
past authentication/onboarding. Two presentations both qualify: the normal
signed-in landing screen (empty composer, which may read "Message Copilot") and
a deeplink-opened Copilot screen where a prompt is already populated in the
composer. The introductory random *suggested-prompt* screen is **not** a PASS
state — it must be dismissed via its close control rather than sent, and a
visible/pre-populated prompt is never submitted just because it is present. The
model must never be the authority that declares PASS.

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

- **The model is the decision-maker.** Do not grow a hardcoded list of
  popup labels (Accept / Continue / OK / Never / ...) or a deterministic popup
  handler. Unexpected screens are handled by reasoning, not by pre-scripting
  every screen.
- **Maestro is the execution layer only.**
- **Deterministic PASS.** Goal evaluation is separate and never delegated to
  the model.
- **Independent safety.** Validation is separate from decision-making and gates
  every action.
- **Replaceable model boundary.** The real model can be connected via
  configuration without changing the agent loop, observer, goal evaluator,
  safety validator, or Maestro executor.
- **Small and modular.** Introduce components only when the current milestone
  needs them.

## Intentionally out of scope for the agent core

Proving the core smart agent comes first. The following are deferred and must
not be added while establishing the core: Excel/test-case and user workbook
loading, credential management, login/deep-link automation as fixed scripts,
report generation, scheduling, many test cases or users, parallel or cloud
execution, a web UI, historical reporting, self-healing across runs, and
automatic APK installation.
