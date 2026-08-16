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
  the agent loop, models, safety, logtags, email, build).

## Rules

- **Every new use case gets its own folder** under `usecases/`.
- A use case **composes shared nodes**; it does not fork them.
- A use case **supplies runtime policy/context** to a shared node when it needs
  different behavior — for example a custom `LoginPolicy`. It expresses that
  intent naturally (e.g. *"If the FRI screen is reached, treat it as the
  expected terminal state."*) and hands it to the shared node; it does **not**
  edit the shared node.
- Use-case-specific behavior stays **inside** the use case.

## Current use cases

- **`deeplink/`** — data-driven deeplink regression suite. See
  `deeplink/README.md`.
