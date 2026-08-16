"""AppPilot business/test use cases.

Each use case lives in its own folder here and COMPOSES the generic reusable
nodes under ``shared/`` (and the framework primitives under ``apppilot/``). A use
case owns WHAT/WHY - its testcase schema, orchestration, verification, results
and CLI - and supplies runtime policy/context to shared nodes. Shared nodes stay
generic and must never learn about a specific use case.

Current use cases:
  * deeplink - data-driven deeplink regression suite.
"""
