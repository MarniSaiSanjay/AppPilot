# Deeplink use case

Data-driven deeplink regression suite. Each row of an Excel workbook is one test
case: launch the **exact** deeplink, observe the resulting Android UI, and let
the model **judge** whether the observed screen satisfies the natural-language
Expected Result — with deterministic retries and reporting.

## High-level flow

```
load test cases (Excel)
  -> for each case: launch the EXACT deeplink (deterministic, Maestro)
      -> observe the resulting Android UI (deterministic, Maestro)
          -> AI judges observed UI vs the Expected Result (semantic)
              -> PASS, or kill + wait + retry (deterministic)
  -> final report (+ optional email)
```

The Deeplink and Expected Result come from the Excel verbatim; the model only
judges expected-vs-observed. Retry and reporting are fully deterministic.

## Excel-driven testcases

The workbook is the source of truth (`testcases.py`). The loader is tolerant of
layout: it recognises a header row by name (e.g. *Launch URL*, *Expected
Screen*, *License*, *Installed*) and maps columns accordingly, falling back to a
fixed positional layout when no header is present. Each data row must provide a
Test ID, a Deep Link and an Expected Result.

## Installed vs uninstalled

The `INSTALLED` column (or a deterministic signal derived from the deeplink)
selects the scenario:

- **INSTALLED=TRUE** — run as a batch: sign in once via shared login, run the
  installed warm-up once, then each case. Per-case retry is *kill → wait → reopen*
  the same deeplink.
- **INSTALLED=FALSE** — the genuine first-open-after-install: uninstall, fire the
  deeplink (routes to the store window), install the local APK via adb, then open
  via the store's Open button. No warm-up; every retry re-establishes fresh state.

## How it consumes shared nodes

- **Login** — builds the shared login agent with the **default** `LoginPolicy`
  and wraps it in `SharedLoginFlow`. Login stops at the normal sign-in boundary
  and returns control; Deeplink then does its own verification. (A different use
  case could pass a custom `LoginPolicy` to the same shared login node.)
- **Installer** — `shared.installer.LocalApkInstaller` installs the locally built
  APK and opens the app (adb launcher or store Open button).
- **Warm-up** — `shared.warmup.MaestroWarmUp` performs first-install preparation;
  the Deeplink orchestrator decides *when* (once per installed batch).
- **Model client** — the expectation judge (`verification.py`) delegates HTTP
  transport to `shared.model_client.ChatModelClient`; only the prompt and
  match/verdict semantics are Deeplink's own.

## Verification

`LLMExpectationJudge` is given only the Expected Result and the redacted observed
UI and returns a match/mismatch verdict. The runner polls observe→judge within a
bounded window (PASS on first match; mismatch only after the window elapses) and
retries deterministically.

## Entry point

`cli.py` (`main`) wires everything and runs the suite. Exposed via the
`deeplink_runner` and `flows.deeplink` compatibility facades.
