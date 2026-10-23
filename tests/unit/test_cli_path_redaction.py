"""No command envelope may carry a host path (PRD 9.2).

Why this file exists
--------------------
``unipad.host.paths.safe_location`` exists precisely because an absolute path on
this host embeds the Windows username -- the profile directory is
``C:\\Users\\<name>\\...`` -- so ``str(path)`` in an error detail puts the account
name into whatever renders the envelope.  It is applied in 25 places.

The two ``blocked`` envelopes built by the store openers were not among them.
``_matrix`` and ``_ledger`` both refuse a path that exists but is not a file, and
both named that path with ``path.as_posix()`` -- the full absolute path, username
included, straight into the ``blocked`` envelope's details and therefore to
stdout.  A brand-new line in the delivered diff, on the command whose whole
purpose is to be run by an operator.

This is not a theoretical exposure and it is not style: it is the exact leak
PRD 9.2 names, on a surface added in the same round that added
``safe_location`` usages elsewhere.  The fix is the one every sibling already
uses.

What is proven here
-------------------
That the two store openers redact, and that they still say enough to be useful:
an operator must be able to tell two candidate files apart, which the final
component does and the redacted form must therefore keep.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from unipad.controller import cli

pytestmark = pytest.mark.acceptance

#: The account name planted as a directory component, so the test reproduces the
#: real host shape -- ``C:\Users\<name>\...`` -- rather than asserting on a string
#: that happens to contain a word.
ACCOUNT = "SomeBody"
STORE_NAME = "compatibility-matrix.jsonl"


@pytest.fixture
def not_a_file(tmp_path: Path) -> Path:
    """A store path that exists and is a directory: the refusal condition.

    The account name is a directory component on purpose.  ``safe_location``
    keeps the final component and drops everything before it, so what this
    fixture really tests is that the *whole* path is withheld while the name
    survives -- which is only observable when the withheld part is not empty.
    """
    target = (
        tmp_path / "Users" / ACCOUNT / "AppData" / "Local" / "Temp" / "runs" / "lifecycle" / STORE_NAME
    )
    target.mkdir(parents=True)
    return target


def _envelope(argv: list[str]) -> dict:
    """Run one command and return the single JSON object it printed."""
    buffer = io.StringIO()
    exit_code = cli.main(argv, buffer)
    assert exit_code != 0, f"expected a refusal, got exit {exit_code}: {buffer.getvalue()}"
    text = buffer.getvalue()
    return json.loads(text[text.index("{") :])


def test_the_matrix_opener_does_not_name_the_host_path(not_a_file: Path) -> None:
    """``matrix_not_a_file`` must not publish where the machine keeps the file."""
    envelope = _envelope(["matrix", "--matrix", str(not_a_file)])
    assert envelope["status"] == "blocked", envelope
    rendered = json.dumps(envelope, ensure_ascii=False)
    assert ACCOUNT not in rendered, (
        "the blocked envelope carries the absolute matrix path, so the Windows "
        f"account name reaches stdout (PRD 9.2): {rendered}"
    )
    # ...and it still says which file, or the refusal is useless.
    assert STORE_NAME in rendered, (
        f"the redaction removed the only part that identifies the file: {rendered}"
    )


def test_the_ledger_opener_does_not_name_the_host_path(not_a_file: Path) -> None:
    """``ledger_not_a_file`` must not publish where the machine keeps the ledger."""
    envelope = _envelope(["history", "--all", "--ledger", str(not_a_file)])
    assert envelope["status"] == "blocked", envelope
    rendered = json.dumps(envelope, ensure_ascii=False)
    assert ACCOUNT not in rendered, (
        "the blocked envelope carries the absolute ledger path, so the Windows "
        f"account name reaches stdout (PRD 9.2): {rendered}"
    )
    assert STORE_NAME in rendered, (
        f"the redaction removed the only part that identifies the file: {rendered}"
    )


def test_the_whole_path_is_withheld_not_just_the_account(not_a_file: Path) -> None:
    """The final component is the only thing that may survive.

    Asserting the account name is absent is the weaker half: an implementation
    that stripped only ``C:/Users/<name>`` and kept ``AppData/Local/Temp/...``
    would pass it.  This pins the stronger property -- nothing above the final
    component is published.
    """
    envelope = _envelope(["matrix", "--matrix", str(not_a_file)])
    rendered = json.dumps(envelope, ensure_ascii=False)
    for withheld in ("AppData", "Local", "Temp", "runs", "lifecycle", "Users"):
        assert withheld not in rendered, (
            f"the envelope still names '{withheld}', so more than the final "
            f"component is published: {rendered}"
        )
