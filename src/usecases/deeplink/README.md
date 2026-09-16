# Deeplink use case

Data-driven deeplink regression suite. Each workbook row launches the exact
deeplink, observes the Android UI, and verifies the natural-language Expected
Result.

## High-level flow

```
load test cases (Excel)
  -> for each case: launch the EXACT deeplink (deterministic, ADB VIEW intent)
      -> observe the resulting Android UI (UIAutomator, Maestro fallback)
          -> AI judges observed UI vs the Expected Result (semantic)
              -> PASS, or kill + wait + retry (deterministic)
  -> final report (+ optional email)
```

The deeplink and Expected Result come from the workbook.

## Excel-driven testcases

The workbook is the source of truth (`deeplink_testcase_loader.py`). Explicit
header rows must contain Test ID, Deep Link, License, and Expected Result;
Installed is optional. Headerless positional A-D compatibility remains.
Duplicate Test IDs, missing required values, non-HTTPS links, and links outside
the supported App Link domains are rejected with row/column context.
`Installed` accepts only the documented true/false tokens listed in the root
README. The License selects profile-specific credentials from local environment
variables; credentials are never stored in the workbook.

## Installed vs uninstalled

The `INSTALLED` column selects the scenario:

- **INSTALLED=TRUE** — install once, group by normalized License, prepare the
  active account, run two stabilization cycles, and execute each group
  contiguously. Per-case retry reopens the same deeplink.
- **INSTALLED=FALSE** — uninstall, open the deeplink through the store handoff,
  install the local APK, complete login, enable supported App Link domains, and
  replay the exact workbook URL. Retries recreate fresh state before login and
  replay supported-domain links after login.

## Shared dependencies

Deeplink composes shared login, installer, warm-up, account-session, and model
client nodes. `supported_links.py` owns the App Link domain list.

## Verification

The runner first verifies deeplink delivery evidence, expected package,
foreground package/activity, non-empty UI, and reliable destination signatures.
Deterministic contradictions cannot be overridden by the model.
`LLMExpectationJudge` receives the Expected Result and redacted observed UI for
semantic ambiguity. Each retry receives a fresh verification window; preparation,
installation, login, retry setup, and cleanup do not consume it. Researcher Add
setup is handled deterministically when the screen explicitly offers that
action. Named destinations and prompt requirements must match.

## Device ownership, cleanup, and evidence

The CLI holds a per-device process lock before mutation. Case cleanup stops the
target app and clears the clipboard. Suite cleanup restores captured
supported-link state. Cleanup failures are reported separately.

Failed cases with recorded verification attempts retain sanitized JSON under
`.apppilot_artifacts/<run-id>/`. Artifact paths are not printed in reports or
email.

## Entry point

`cli.py` (`main`) wires everything and runs the suite. Exposed via the
`deeplink_runner` and `flows.deeplink` compatibility facades.
