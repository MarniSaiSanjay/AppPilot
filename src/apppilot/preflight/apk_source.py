"""APK source selection node (choose once, confirm every run).

One responsibility: decide HOW AppPilot gets the officemobile app onto the
device for this run. This is what makes a local build optional - not everyone
has (or wants) the Office Mobile enlistment + build toolchain.

Three sources:

  * ``build``     - build a fresh local APK from the enlistment (needs the
                    enlistment path + JDK 17/omrdroid; the original behaviour).
  * ``existing``  - install an APK the operator already has (they provide the
                    path; no enlistment or build tools required).
  * ``playstore`` - use the app already installed from the Play Store; AppPilot
                    installs nothing and drives it purely via deeplinks.

The choice is remembered in :class:`config_store.ConfigStore`, but - unlike the
path nodes - it is re-confirmed on every interactive run: the remembered value
is shown as the default and the operator just presses Enter to keep it (same
feel as the email-recipient prompt). Non-interactive runs silently honour the
saved value (or the default), so CI keeps working. Never raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .config_store import ConfigStore

# Config key under which the chosen source is remembered between runs.
APK_SOURCE_KEY = "apk_source"

# The valid sources, in menu order.
BUILD = "build"
EXISTING = "existing"
PLAYSTORE = "playstore"
SOURCES = (BUILD, EXISTING, PLAYSTORE)

# One-line human labels for the menu.
_LABELS = {
    BUILD: "build a fresh local APK from the Office Mobile enlistment",
    EXISTING: "install an APK you already have (you'll provide the path)",
    PLAYSTORE: "use the app already installed from the Play Store (install nothing)",
}

# Accept either the menu number or the source name (case-insensitive).
_CHOICES = {
    "1": BUILD, BUILD: BUILD,
    "2": EXISTING, EXISTING: EXISTING,
    "3": PLAYSTORE, PLAYSTORE: PLAYSTORE,
}

_MAX_ATTEMPTS = 3

# prompter(prompt_text) -> raw reply; output(line) -> show a line. Injectable so
# tests need no real stdin/stdout.
Prompter = Callable[[str], str]
Output = Callable[[str], None]


@dataclass(frozen=True)
class ApkSourceResult:
    """The chosen APK source, plus an operator-facing message."""

    ok: bool
    source: Optional[str] = None
    message: str = ""


def select_apk_source(
    *,
    store: ConfigStore,
    default: str = BUILD,
    interactive: bool = False,
    prompter: Prompter = input,
    output: Output = print,
    max_attempts: int = _MAX_ATTEMPTS,
) -> ApkSourceResult:
    """Return the APK source to use. See the module docstring for the order."""
    saved = store.get(APK_SOURCE_KEY)
    current = saved if saved in SOURCES else (default if default in SOURCES else BUILD)

    # Non-interactive: honour the remembered (or default) choice silently.
    if not interactive:
        origin = "saved" if saved in SOURCES else "default"
        return ApkSourceResult(
            True, current, f"APK source: {current} ({origin}; non-interactive)."
        )

    output("APK source - how should AppPilot get the officemobile app?")
    for number, source in enumerate(SOURCES, start=1):
        output(f"  {number}) {source:<9} - {_LABELS[source]}")

    chosen = current
    for _ in range(max(1, max_attempts)):
        try:
            answer = prompter(f"Choose 1/2/3 [Enter = keep '{current}']: ").strip()
        except (EOFError, KeyboardInterrupt):
            # Unattended-friendly: an aborted prompt keeps the current choice
            # rather than failing a run that could otherwise proceed.
            answer = ""
        if not answer:
            chosen = current
            break
        picked = _CHOICES.get(answer.lower())
        if picked:
            chosen = picked
            break
        output("Please enter 1, 2, or 3.")
    else:
        chosen = current  # attempts exhausted: fall back, never block the run.

    store.set(APK_SOURCE_KEY, chosen)
    return ApkSourceResult(True, chosen, f"APK source: {chosen}.")
