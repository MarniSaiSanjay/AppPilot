# Deeplink use case

Data-driven deeplink regression suite. Each row of an Excel workbook is one test
case: launch the **exact** deeplink, observe the resulting Android UI, and let
the model **judge** whether the observed screen satisfies the natural-language
Expected Result — with deterministic retries and reporting.

## High-level flow

```
load test cases (Excel)
  -> for each case: launch the EXACT deeplink (deterministic, ADB VIEW intent)
      -> observe the resulting Android UI (UIAutomator, Maestro fallback)
          -> AI judges observed UI vs the Expected Result (semantic)
              -> PASS, or kill + wait + retry (deterministic)
  -> final report (+ optional email)
```

The Deeplink and Expected Result come from the Excel verbatim; the model only
judges expected-vs-observed. Retry and reporting are fully deterministic.

## Excel-driven testcases

The workbook is the source of truth (`deeplink_testcase_loader.py`). The loader is tolerant of
layout: it recognises a header row by name (e.g. *Launch URL*, *Expected
Screen*, *License*, *Installed*) and maps columns accordingly, falling back to a
fixed positional layout when no header is present. Each data row must provide a
Test ID, a Deep Link, a License and an Expected Result. The License selects
profile-specific credentials from local environment variables; credentials are
never stored in the workbook.

## Installed vs uninstalled

The `INSTALLED` column (or a deterministic signal derived from the deeplink)
selects the scenario:

- **INSTALLED=TRUE** — the APK is installed once, then cases are grouped by
  normalized License in first-seen order. Each group brings the app foreground,
  and ensures login. After the first successful login, supported-link routing is
  configured once for the installed app. Each group then adds or switches to the
  required account when necessary, verifies the active account, runs exactly two
  launch/settle/stop stabilization cycles, then reopens the app and runs the same
  login boundary again so delayed post-account privacy sheets, dialogs, and
  onboarding are completed before the first deeplink. It then runs that group's
  cases contiguously. Account switches and retries do not repeat supported-link
  preparation. Per-case retry is *kill → wait → reopen app → conditionally
  restore login → reopen the same deeplink*. Final report order is restored to
  workbook order.
- **INSTALLED=FALSE** — the genuine first-open-after-install: uninstall, fire the
  deeplink (routes to the store window), install the local APK via adb, then open
  via the store's Open button. After login, AppPilot approves any declared App
  Link domain and replays the exact workbook URL so Android hands the destination
  to the newly installed app rather than the browser. Because replay preserves
  the destination, these links may also use the shared login flow's one-time
  relaunch recovery when first-open loading stalls. No warm-up. A failure before
  login completes makes the next attempt uninstall and recreate the full fresh
  state. For supported-domain links, a failure after login preserves the
  authenticated installation and retries by restarting the app and replaying
  the exact deeplink. Other fresh-install links retry from recreated fresh
  installation state because their deferred handoff cannot be replayed. If
  restart-and-replay recovery itself fails, AppPilot also recreates fresh
  installation state within the bounded retry.
  For links outside the supported domain list, relaunch-based login recovery
  remains disabled because it cannot preserve the store's pending first-open
  deeplink; a stalled login instead retries from fresh installation state.

## How it consumes shared nodes

- **Login** — resolves every License profile before device work, builds the
  shared login agent with the **default** `LoginPolicy`, and caches one
  `SharedLoginFlow` per profile. Login stops at the normal sign-in boundary and
  returns control; Deeplink then does its own verification. (A different use
  case could pass a custom `LoginPolicy` to the same shared login node.)
- **Installer** — `shared.installer.LocalApkInstaller` installs the locally built
  APK and opens the app (adb launcher or store Open button).
- **Warm-up** — `shared.warmup.MaestroWarmUp` performs first-install preparation;
  Deeplink configures exactly two cycles and runs them once per successfully
  prepared License group.
- **Account session** — `shared.account.session.AndroidAccountSession` navigates
  Menu → drawer account → Settings account, handles the optional overlay
  permission detour, then switches or adds the required account. Account
  identifiers are matched locally and never enter model requests, logs, result
  reasons, or reports.
- **Model client** — the expectation judge (`verification.py`) delegates HTTP
  transport to `shared.model_client.ChatModelClient`; only the prompt and
  match/verdict semantics are Deeplink's own.
- **Supported links** — `supported_links.py` owns the extensible domain list.
  Android approval and user selection run once after the first successful
  installed-batch login and after each relevant fresh installation before the
  exact deeplink is replayed.

## Verification

`LLMExpectationJudge` is given only the Expected Result and the redacted observed
UI and returns a match/mismatch verdict. The runner polls observe→judge within a
bounded window (PASS on first match; mismatch only after the window elapses) and
retries deterministically. For installed cases and supported-domain
fresh-install links, one bounded in-attempt recovery handles Android or Maestro
operational failures and incomplete destination shells by stopping the app,
reopening it, checking login without allowing another relaunch, and replaying
the exact deeplink. Other fresh-install links retry from recreated installation
state. An existing signed-in session requires no login actions. Existing
supported-link setup is reused; it is repeated only after an uninstall has
removed that package state. Usable but incorrect destinations are not
restarted. In both installed and first-install flows, a Researcher screen
that explicitly offers an Add action is handled deterministically before final
verification and again after a recovery replay when needed; unknown screens go
directly to the judge. Named destinations are
distinct: an expected Researcher screen cannot match Cowork or generic Chat,
and vice versa. Prompt presence, absence, and any specified prompt content must
also match.

## Entry point

`cli.py` (`main`) wires everything and runs the suite. Exposed via the
`deeplink_runner` and `flows.deeplink` compatibility facades.
