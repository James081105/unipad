"""``record.redact`` must actually redact the account name (PRD 9.2).

Why this file exists
--------------------
PRD 9.2 forbids the Windows account name in any output or error detail, because
an absolute path on this host embeds it: the profile is ``C:\\Users\\<name>\\...``.
Two redactors exist in this repository and they disagree:

* ``unipad.acceptance.p1.redact`` matches ``/(?:home|Users|root)/[^\\s'\"<>|]*``
  -- it consumes the account component and everything after it up to whitespace.
* ``unipad.acceptance.record.redact`` matches ``[\\/]Users[\\/]`` -- it consumes
  **only** the literal ``/Users/`` and leaves the account name in place.

Measured, on this host, with the real username::

    redact('C:/Users/SomeBody/AppData/Local/Temp/x')
      -> '<redacted:user_path>SomeBody/AppData/Local/Temp/x'

The username survives.  That is not a cosmetic difference: ``record.redact`` is
what :func:`~unipad.acceptance.record.assert_payload_redacted` uses to build the
``excerpt`` of its own refusal, so the function whose entire job is to stop a
user path reaching a report prints the username while refusing it.  A guard that
leaks what it is guarding against is worse than no guard, because it reads as
though the check ran.

This is the third instance of the same shape found in one round -- a second
validator with subtly different rules from the canonical one, where the weaker
one sits on the path that matters (cf. ``.gitignore`` missing ``*.trustcache``
while the workflow's ``PRIVATE_PATTERNS`` listed it, and ``_check_relative_path``
claiming ``relativise`` also rejects profile markers).  The fix here is to make
the weaker one consume what the stronger one consumes, and then to prove the two
agree.

What is proven here
-------------------
That ``redact`` removes the account component, keeps the part of the path that
identifies the file, and that the two redactors in this repository no longer
disagree about the part PRD 9.2 protects.
"""

from __future__ import annotations

import getpass
from pathlib import Path

import pytest

from unipad.acceptance import record
from unipad.acceptance.p1 import redact as p1_redact

pytestmark = pytest.mark.acceptance

#: An account name planted in a path.  The real one is used when it can be read,
#: because a test that only checks a made-up name would pass on a host whose
#: username happens not to appear.
try:
    ACCOUNT = getpass.getuser()
except Exception:  # pragma: no cover - getpass failing is itself unusual
    ACCOUNT = "SomeBody"

PROFILE_PATH = f"C:/Users/{ACCOUNT}/AppData/Local/Temp/runs/lifecycle/compatibility-matrix.jsonl"


def test_redact_removes_the_account_component() -> None:
    """The username must not survive its own redaction."""
    scrubbed = record.redact(PROFILE_PATH)
    assert ACCOUNT not in scrubbed, (
        "record.redact leaves the Windows account name in place, so the username "
        f"reaches whatever renders the redacted text (PRD 9.2): {scrubbed!r}"
    )


def test_redact_keeps_the_part_that_identifies_the_file() -> None:
    """Redaction that removes the name too is useless to an operator."""
    scrubbed = record.redact(PROFILE_PATH)
    assert "compatibility-matrix.jsonl" in scrubbed, (
        f"the redaction removed the only part that identifies the file: {scrubbed!r}"
    )
    assert "AppData" in scrubbed, (
        f"the redaction removed the directory structure an operator navigates by: {scrubbed!r}"
    )


def test_the_two_redactors_agree_on_the_account_component() -> None:
    """``p1.redact`` already consumes the account; ``record.redact`` must match.

    Two redactors with different rules is the "two guards, one hole" shape.  This
    pins that the canonical one is no longer the weaker of the two for the one
    thing PRD 9.2 names explicitly.
    """
    from_record = record.redact(PROFILE_PATH)
    from_p1 = p1_redact(PROFILE_PATH)
    assert ACCOUNT not in from_record, from_record
    assert ACCOUNT not in from_p1, from_p1


def test_a_refusal_excerpt_does_not_repeat_the_username(tmp_path: Path) -> None:
    """``assert_payload_redacted`` refuses a user path without echoing it.

    The refusal is the function's whole purpose, and its ``excerpt`` is built
    with :func:`record.redact` -- so before the fix, the message that says "this
    payload carries an absolute path" carried the username along with it.

    The whole error object is searched, not ``str(exc)``: ``UniPadError.__str__``
    carries the code and the message but not ``details``, and the excerpt lives
    in ``details``.  Asserting on ``str()`` alone would pass while the leak is
    still there.
    """
    payload = {"path": PROFILE_PATH}
    with pytest.raises(record.UniPadError) as caught:
        record.assert_payload_redacted(payload)
    error = caught.value
    rendered = f"{error!r} {getattr(error, 'details', None)!r}"
    assert ACCOUNT not in rendered, (
        "the refusal's own message repeats the Windows account name (PRD 9.2): "
        f"{rendered}"
    )
    # ...and the excerpt still names the file, or the operator cannot find it.
    assert "compatibility-matrix.jsonl" in rendered, rendered


def test_a_drive_letter_path_is_redacted_everywhere() -> None:
    """``F:/iosvirtual/runs/x`` must not reach a report verbatim either."""
    scrubbed = record.redact("F:/iosvirtual/runs/exp-1/launch.json")
    assert "F:" not in scrubbed, scrubbed
    assert "iosvirtual/runs/exp-1/launch.json" in scrubbed, scrubbed


def test_a_relative_path_is_left_alone() -> None:
    """The redaction must not eat paths that carry nothing to protect."""
    for clean in ("compatibility-matrix.jsonl", "lifecycle/install-ledger.jsonl", "kernel.macho"):
        assert record.redact(clean) == clean, clean


def test_scrub_account_removes_every_occurrence() -> None:
    """The account name must not survive anywhere, including a second occurrence.

    ``record.redact`` handles the profile ROOT.  It cannot handle an account name
    that appears again further along the same path, and on this host those exist:
    the pytest fixture root is ``.../pytest-of-<name>/pytest-N/test_x0/``.  So the
    path redaction alone leaves the name in place, and this is the test that
    catches it.
    """
    from unipad.host.paths import account_name, scrub_account

    name = account_name()
    if not name:
        pytest.skip("no host account name is determinable on this host")
    twice = (
        f"C:/Users/{name}/AppData/Local/Temp/pytest-of-{name}/"
        f"pytest-2676/test_x0/budget-usage.json"
    )
    scrubbed = scrub_account(twice)
    assert name not in scrubbed, f"the account name survived: {scrubbed!r}"
    assert scrubbed.count("<redacted:account>") == 2, scrubbed
    # The useful part is still there.
    assert "budget-usage.json" in scrubbed, scrubbed


def test_the_cli_envelope_scrubs_the_account_at_every_occurrence() -> None:
    """``_emit`` applies both redactions, not just the path one.

    This is the end-to-end half: the path redaction is necessary but not
    sufficient, and a test that only checked the path redaction would pass while
    the account name is still on stdout.
    """
    from unipad.host.paths import account_name
    from unipad.controller.cli import _redact_payload

    name = account_name()
    if not name:
        pytest.skip("no host account name is determinable on this host")
    document = {
        "usage_file": f"C:/Users/{name}/AppData/Local/Temp/pytest-of-{name}/x/budget-usage.json",
        "nested": {"deeper": [f"runs/{name}/launch.json"]},
        "untouched": "kernel.macho",
    }
    scrubbed = _redact_payload(document)
    rendered = repr(scrubbed)
    assert name not in rendered, rendered
    assert "budget-usage.json" in rendered, rendered
    assert scrubbed["untouched"] == "kernel.macho", scrubbed
