# Shared nodes

This folder holds **reusable, use-case-agnostic building blocks** ("shared
nodes") that more than one AppPilot use case can consume. A use case (Deeplink
today; FRI and others later) wires these nodes together and adds its own
orchestration, but the nodes themselves live here so they are never duplicated.

## The one rule: shared nodes stay generic

A shared node must **not** contain use-case-specific knowledge. There is no
`if deeplink: ...` or `if fri: ...` branching anywhere in this folder.

Instead:

- **A shared node provides a generic capability plus sensible defaults.**
  It owns *how* something is done.
- **A use case supplies its own intent** through runtime configuration/policy.
  It owns *what* it wants and *why*.

So the same shared node can serve different use cases without being copied or
edited. When a use case needs different behavior, it passes that behavior in at
runtime (as a policy, a terminal-state description, a predicate, or an injected
collaborator) — it does not reach into the shared node and add a special case.

Every shared node should ship **sensible defaults** so that, with no extra
configuration, it behaves exactly as it does today.

## Add reusable nodes here

If you build something another use case could reuse, put it in this folder (a
module, or a subpackage like `login/`). Keep it generic, give it defaults, and
document how a use case customizes it. Keep use-case-specific orchestration out
of here — that belongs with the use case.

## Current shared nodes

### `model_client.py` — `ChatModelClient`
The single OpenAI-compatible chat client. It owns endpoint configuration
(environment resolution via `config_from_env`) and the HTTP transport
(`send`: Bearer-auth POST + JSON parse, raising `ModelTransportError` on
failure). It knows nothing about prompts or any use case — callers build the
payload and interpret the response. This consolidates the model transport that
the model decision provider, the deeplink expectation judge, and the login goal
evaluator previously duplicated.

### `login/` — the shared login node
A generic AppPilotAgent-based login/onboarding capability. No UI steps are
hardcoded: the model decides each action. A use case describes what it wants via
a **`LoginPolicy`**; the node drives sign-in and stops at the first terminal
state the policy declares, before offering any action.

- `policy.py` — `LoginPolicy` plus the terminal abstractions
  (`DeterministicTerminalState`, `SemanticTerminalState`,
  `CompositeTerminalEvaluator`). A use case authors terminals in natural
  language (e.g. *"the First-Run / FRI screen is displayed"*) or, when it
  already has reliable structured evidence, as a deterministic predicate.
- `goal.py` — the goal evaluators: the deterministic Copilot login-completion
  detector, the model-backed login-completion judge, their authoritative
  composition (the sensible **default** terminal), and a generic
  `SemanticStateEvaluator` used to judge natural-language terminal states.
- `builder.py` — `build_login_agent(...)` (assembles the agent from a policy;
  default = today's behavior), `resolve_decision_provider`, and the CLI entry
  point.
- `flow.py` — `SharedLoginFlow` / `LoginCapability`: a small adapter the caller
  invokes to prepare login, plus the `[LOGIN]` execution-trace observability.

Customization at a glance:

- **Defaults** — `LoginPolicy.default()` reproduces today's login exactly: the
  prototype goal/guidance and only the built-in login-completion terminal.
- **Terminal states** — a use case lists ordered terminal states; terminal
  detection happens *before* any action is offered, so reaching a terminal
  always wins over dismissing/continuing an incidental screen. Declaration order
  is priority.
- **Precedence** — the first terminal that reports "reached" wins. Unless a
  policy opts out, the built-in login-completion terminal is appended as the
  lowest-priority fallback so login still stops normally when the custom
  terminals never appear.
- **Safety** — a policy only chooses *when login stops*; it cannot bypass the
  agent's existing safety validation, credential isolation, or action bounds.

The invariant this preserves across use cases:

```
LOGIN PREPARES  ->  LOGIN STOPS  ->  USE-CASE VERIFIES
```

Login gets the app to the boundary the use case declared and returns control;
the use case then performs its own verification.
