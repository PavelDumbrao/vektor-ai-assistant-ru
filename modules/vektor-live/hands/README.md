# Mac Hands

This directory is the Mac-side execution agent used by ВЕКТОР Live. The production
Python source and JXA/AppleScript helpers are frozen by the release manifest.

Runtime requirements observed on 2026-09-08 are pinned in `requirements.txt`.
Install them for Python 3.13, restore `VEKTOR_HANDS_KEY` and `VEKTOR_HANDS_URL` from
the private secret store, then run `python3 vektor_hands.py` from this directory's parent.

The agent opens an outbound WebSocket only. macOS Accessibility and Screen Recording
permissions are required. The VPS and Mac both enforce the same mode/action allowlist.
Passwords are blocked through the `AXSecureTextField` check. The irreversible-action
restriction is currently model policy, not an OS-level enforcement boundary.
