"""No committed file may spell the host account name.

Why this exists
---------------
The standing rule (PRD 9.2 and the session constraints) is that the Windows
account name must never appear in a committed file -- including as test fixture
data, and including in a sentence explaining why something must not be printed.
It did, three times, and always in exactly that innocent shape: a docstring
explaining that a path is unsafe to relay, which then relays the unsafe spelling
as its own example.  Two of the three were already on ``origin/main``.

A rule enforced by review alone is a rule that gets reviewed once.  This is the
same rule enforced by the suite, over every tracked file, and it checks BOTH
spellings Windows uses: the account name itself, and the 8.3 short name
(``USERNA~1``) that Windows substitutes when the profile path contains a space
or a non-ASCII character.  The second is the one that actually leaked, because
:func:`unipad.host.paths.scrub_account` matches the account name and therefore
cannot match it -- which is precisely why the sentences that carried it existed.

The name is derived from the host rather than written here, so this test cannot
become the leak it is looking for.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from unipad.host.paths import account_name

REPO = Path(__file__).resolve().parents[2]

#: Below this length an accidental match is far more likely than a leak, and a
#: test that fails on the word "root" teaches people to disable it.
MIN_NAME_LENGTH = 4


def _spellings() -> set[str]:
    """Every spelling of this host's account name that could appear in a file.

    The 8.3 form is derived rather than read from the filesystem: Windows builds
    it from the first six characters that are valid in a short name, upper-cased,
    with a numeric tail.  ``ZhuanZ（无密码）`` therefore becomes ``ZHUANZ~1`` --
    the full-width parentheses and the space are dropped, which is why the
    spelling shares nothing with the name it came from.
    """
    name = account_name()
    if not name or len(name) < MIN_NAME_LENGTH:
        return set()
    spellings = {name}
    usable = "".join(c for c in name if c.isascii() and c.isalnum())[:6]
    if len(usable) >= 3:
        spellings.add(usable.upper() + "~1")
    return spellings


def _tracked_files() -> list[Path]:
    """Every file git tracks, which is exactly the set that gets published.

    ``git ls-files`` rather than a walk, so the gitignored trees -- ``vendor/``
    and ``local-private/`` -- are excluded by the same rule that keeps them out
    of a commit.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(REPO),
        capture_output=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"git ls-files failed in {REPO}: {result.stderr[:200]!r}")
    names = [n for n in result.stdout.decode("utf-8", "replace").split("\0") if n]
    return [REPO / n for n in names]


@pytest.mark.skipif(not _spellings(), reason="the host account name cannot be determined")
def test_no_tracked_file_spells_the_host_account():
    """Every tracked file is scanned; a hit names the file and the line."""
    spellings = _spellings()
    offenders: list[str] = []

    for path in _tracked_files():
        try:
            raw = path.read_bytes()
        except OSError:
            continue                      # a submodule dir, a broken symlink
        if len(raw) > 8_000_000:
            continue                      # a binary or a fixture too large to grep
        for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            for spelling in spellings:
                if spelling in line:
                    shown = line.strip()
                    if len(shown) > 100:
                        at = shown.index(spelling)
                        shown = shown[max(0, at - 40):at + 60]
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{number}: {shown}")

    assert not offenders, (
        "these committed files spell the host account name (or its 8.3 short "
        "form). PRD 9.2: the account name must never appear in a committed "
        "file, including in a sentence explaining why something must not be "
        "printed -- genericise the example instead. "
        + "; ".join(offenders[:10])
    )


def test_the_derivation_reproduces_the_8_3_shape():
    """The guard must be able to see the spelling that actually leaked.

    ``scrub_account`` replaces the account name and therefore cannot match the
    8.3 form; if this derivation stopped producing it, the guard would go quiet
    about the one spelling that reached origin/main.
    """
    name = account_name()
    if not name or len(name) < MIN_NAME_LENGTH:
        pytest.skip("the host account name cannot be determined")
    usable = "".join(c for c in name if c.isascii() and c.isalnum())[:6]
    if len(usable) < 3:
        pytest.skip("this account has no ASCII alphanumeric characters to shorten")
    assert usable.upper() + "~1" in _spellings(), (
        "the 8.3 short-name spelling is not in the set the guard checks"
    )
    # And the guard must not be satisfiable by the name alone: the whole point is
    # that the two spellings share no substring.
    assert not set(_spellings()) <= {name}, (
        "the guard checks only the account name; the 8.3 spelling that leaked "
        "is the one it would miss"
    )
