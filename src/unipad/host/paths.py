"""Host-side path redaction for anything an operator or a guest can read.

PRD 9.2 forbids account tokens, signing private keys and user documents in any
output or error detail.  On this host an absolute path embeds the Windows
username -- the profile directory is ``C:\\Users\\<name>\\...`` -- so the moment
an error detail carries ``str(path)`` the username reaches whatever renders the
envelope, and a run directory under ``AppData\\Local\\Temp`` names the account
twice over.

The fix is not to stop naming the path: an operator has to be able to tell two
candidate files apart.  It is to name the part that distinguishes them without
saying where the machine keeps them, which is the final component.
"""

from __future__ import annotations

import getpass
from pathlib import Path

__all__ = ["account_name", "safe_location", "scrub_account"]


def account_name() -> str:
    """The host account name, or ``""`` when it cannot be determined.

    ``getpass.getuser()`` raises when neither ``LOGNAME``/``USER``/``LNAME``/
    ``USERNAME`` is set and ``pwd`` is unavailable (a stripped container, some
    service accounts).  An empty string is the honest answer there: a caller
    that scrubs ``""`` would corrupt every string it touched, so it must be able
    to tell "no account known" from "an account named nothing".
    """
    try:
        return getpass.getuser() or ""
    except Exception:  # noqa: BLE001 - any failure means "unknown", never a crash
        return ""


def scrub_account(text: str) -> str:
    """Replace every occurrence of the host account name in ``text``.

    Why this exists alongside :func:`unipad.acceptance.record.redact`
    ------------------------------------------------------------------
    ``redact`` replaces the *profile root* -- ``C:/Users/<name>`` becomes
    ``<redacted:user_path>`` -- which is enough for a path whose account appears
    once.  It is not enough for a path whose account appears twice, and those
    exist: this host's pytest fixture root is
    ``.../pytest-of-SomeBody/pytest-2676/test_x0/``, so after the profile
    root is redacted the account name is still sitting in the remainder, and a
    test that greps stdout for the username still finds it.

    Measured, on this host, through the real ``doctor`` envelope::

        redact("C:/Users/<name>/AppData/.../pytest-of-<name>/pytest-2676/x/budget-usage.json")
          -> "<redacted:user_path>/AppData/.../pytest-of-<name>/pytest-2676/x/budget-usage.json"

    The account survived.  PRD 9.2 is about the account name, not about the shape
    of the path that carries it, so the guarantee has to be made directly: remove
    the name wherever it appears.  This is the over-redaction direction and it is
    the safe one -- a string that happens to contain the username is either a path
    (where the username is a leak) or content an operator typed (where naming the
    account back is still a leak).
    """
    name = account_name()
    if not name:
        return text
    return text.replace(name, "<redacted:account>")


def safe_location(path: Path | str) -> str:
    """The part of ``path`` an error detail may name: its final component.

    Two directories that differ anywhere differ in their final component, so
    this is enough to tell candidates apart.  What it withholds -- the drive,
    the profile, the username -- is exactly what PRD 9.2 protects.

    A path with no final component (a drive root, or ``""``) redacts to ``""``
    rather than to something that looks like a name, so an empty result is
    always "there was nothing safe to say" and never a mistaken identity.
    """
    return Path(path).name
