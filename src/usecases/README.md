# Use cases

This folder holds AppPilot business/test use cases. Each use case owns its
testcase schema, orchestration, verification, results, and CLI.

## Layering

- **`src/usecases/`** — use-case-specific testcase, orchestration, verification,
  reporting, and CLI code.
- **`src/shared/`** — reusable login, install/open, warm-up, account, and model
  client nodes.
- **`src/apppilot/`** — framework primitives (Android/Maestro,
  the agent loop, adaptive decision replay, models, safety, logtags, email,
  build).

## Rules

- **Every new use case gets its own folder** under `src/usecases/`.
- A use case composes shared nodes.
- A use case supplies runtime policy/context to shared nodes when needed.
- Use-case-specific behavior stays inside the use case.
- Model-driven action loops use a namespaced
  `apppilot.adaptive_decision_cache.AdaptiveDecisionCache`.

## Adaptive decisions

Model-driven action loops use `AdaptiveDecisionCache` with a stable
use-case-specific namespace. Add a use-case subclass only for seeded decisions
or specialized behavior. Login provides an example in
`src/shared/login/login_decision_cache.py`.

## Current use cases

- **`src/usecases/deeplink/`** — data-driven deeplink regression suite. See
  `deeplink/README.md`.
