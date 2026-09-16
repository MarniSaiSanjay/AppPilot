# Shared nodes

This folder holds reusable, use-case-agnostic building blocks.

## Rules

- Shared nodes stay generic.
- Use cases pass policy, context, predicates, or collaborators at runtime.
- Use-case-specific orchestration belongs in `src/usecases/`.
- Shared nodes provide defaults for common flows.

## Current shared nodes

### `credentials.py` — credential profile resolution
Builds a credential-backed `RuntimeContext` from profile labels and environment
variables.

### `account/` — account sessions
Prepares one resolved credential profile as the active app account.
`account/__init__.py` owns the contract, `account/session.py` the Android
implementation, and `account/safety.py` the scoped action policy.

### `model_client.py` — `ChatModelClient`
Work Azure OpenAI chat transport. It owns endpoint configuration, Microsoft
Entra token acquisition, JSON parsing, and `ModelTransportError` failures.
Callers build prompts and interpret responses.

### `login/` — the shared login node
A generic AppPilotAgent-based login/onboarding capability. A use case supplies a
**`LoginPolicy`** that defines when login stops.

- `policy.py` — `LoginPolicy` plus terminal abstractions.
- `goal.py` — the goal evaluators: the deterministic Copilot login-completion
  detector, the model-backed login-completion judge, their authoritative
  composition (the **default** terminal), and a generic
  `SemanticStateEvaluator` used to judge natural-language terminal states.
- `builder.py` — `build_login_agent(...)`, `resolve_decision_provider`, and the
  CLI entry point.
- `login_decision_cache.py` — `LoginDecisionCache`, containing only validated
  login-specific seeded states over the generic `AdaptiveDecisionCache`.
- `flow.py` — `SharedLoginFlow` / `LoginCapability`.
- `profiled.py` — `ProfiledLoginFlowFactory`: binds resolved credential profiles
  to login agents and caches one reusable `SharedLoginFlow` per normalized key.

Login flow:

```
LOGIN PREPARES  ->  LOGIN STOPS  ->  USE-CASE VERIFIES
```

Login gets the app to the declared boundary and returns control.
