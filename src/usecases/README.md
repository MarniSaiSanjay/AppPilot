# Use cases

This folder holds AppPilot **business/test use cases**. A use case is a concrete
test scenario (for example the deeplink regression suite) with its own testcase
schema, orchestration, verification, results and CLI.

## usecases vs shared

- **`usecases/`** — owns **WHAT / WHY**. Use-case-specific knowledge lives here:
  the testcase representation, how the scenario is driven, how it verifies, its
  result/report shapes and its command-line entry point.
- **`shared/`** — owns **HOW**. Generic, reusable nodes (login, device
  install/open, warm-up, the model client). Shared nodes must stay generic and
  must **never** learn about a specific use case.
- **`apppilot/`** — the framework/infrastructure primitives (Android/Maestro,
  the agent loop, adaptive decision replay, models, safety, logtags, email,
  build).

## Rules

- **Every new use case gets its own folder** under `usecases/`.
- A use case **composes shared nodes**; it does not fork them.
- A use case **supplies runtime policy/context** to a shared node when it needs
  different behavior — for example a custom `LoginPolicy`. It expresses that
  intent naturally (e.g. *"If the FRI screen is reached, treat it as the
  expected terminal state."*) and hands it to the shared node; it does **not**
  edit the shared node.
- Use-case-specific behavior stays **inside** the use case.
- A use case that has a model-driven UI action loop must wrap its model decision
  provider with
  `apppilot.adaptive_decision_cache.AdaptiveDecisionCache` (or a use-case
  subclass). Generic successful-run learning stays in `apppilot/`; permanent
  known controls and cache namespaces stay with the owning use case. Do not add
  an adaptive provider to a deterministic use case that has no action-decision
  loop.

## How adaptive decisions work for a new use case

For a model-driven action loop, wrap the normal model provider once with a
stable, use-case-specific namespace:

```python
decision_provider = AdaptiveDecisionCache(
    model_provider,
    cache_namespace="my-use-case",
)
```

Each run automatically tries **seeded known decision → successfully learned
decision → model fallback**. Model decisions remain pending and are cached only
when the complete run reaches its declared expected output; failed, interrupted,
or incomplete runs save nothing. After each successful run with new pending
decisions, `AdaptiveDecisionCache` updates
`~/.cache/apppilot/<namespace>-decisions-v1.json` (or
`$XDG_CACHE_HOME/apppilot/...` when configured). Reuse requires the same
namespace, goal, guidance, and one unique matching semantic action, and safety
validation always still runs. Ambiguous, changed, or unchanged repeated states
fall back to the model.

For every new model-driven use case:

1. Use a Python-safe `snake_case` name and create
   `usecases/<usecase_name>/<usecase_name>_decision_cache.py`.
2. Define `<UseCaseName>DecisionCache`, a use-case-specific subclass of
   `AdaptiveDecisionCache`.
3. Put only that use case's validated permanent states in
   `_seeded_decision(...)`; unknown states automatically use the model and
   success-gated cache.
4. Give it a stable, unique lowercase `cache_namespace` (hyphens are allowed);
   this namespace—not the Python filename—determines the JSON cache name.
5. **Do not modify `apppilot/adaptive_decision_cache.py` to add use-case
   behavior.** It is shared infrastructure and changes only for improvements
   that apply to every use case.

Login provides an example in `shared/login/login_decision_cache.py`. The Python
filename does not determine the JSON cache name; `cache_namespace="my-use-case"`
produces `my-use-case-decisions-v1.json`. The cache stores only SHA-256
signatures—never raw UI, credentials, account identifiers, element IDs, or
coordinates. Deterministic flows without model-selected actions, such as
Deeplink today, do not need adaptive action learning.

## Current use cases

- **`deeplink/`** — data-driven deeplink regression suite. See
  `deeplink/README.md`.
