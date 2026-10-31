"""Where the pinned upstreams live, and why a missing one is a skip.

The problem this module exists for
----------------------------------
``vendor/`` is fetched material.  It is excluded from git -- ``.gitignore``
carries the rule so a clone inherits it, and ``.git/info/exclude`` still has
the local copy -- so a clean checkout has no ``vendor/`` directory at all, and
``preflight``'s unit stage (``.github/workflows/preflight.yml:241``) runs
``pytest tests/unit`` on exactly such a checkout.  A test that opens a file
under ``vendor/`` there dies with ``FileNotFoundError`` (or, if it asserts
``is_file()`` first, with an ``AssertionError``) before any of its own
assertions run, and the stage goes red for a reason that has nothing to do with
the code.

That is not hypothetical.  Measured 2026-10-31 on a ``git archive HEAD``
checkout: 30 failures and 3 errors in ``tests/unit``, from three files that read
``vendor/UniXNU-qemu/package/QemuMetal/*`` and
``vendor/UniXNU/drivers/NativeMobileDisplay/*`` with no guard at all.  The same
class of defect was recorded twice before -- 2026-10-24 in
``tests/unit/test_clean_checkout_selfsufficiency.py``'s own docstring (two
tests whose guard sat *after* the read it guarded), and fact 32 of
``docs/implementation-plan.md`` (two tests whose guard condition was false on
every machine).  Three instances, three different guard shapes, one cause:
every file wrote its own.

So the guard lives here, once.

Why a skip and not a pass
-------------------------
A unit test that cannot read its subject has measured nothing.  Reporting it as
a pass would say "the contract holds" about a file nobody looked at, which is
the exact failure shape the tests in this repository exist to prevent -- a step
whose absence is indistinguishable from success.  Reporting it as a failure
would make ``preflight`` red on every clean checkout for a reason no code
change can fix, which trains people to ignore the stage.

A skip says the truthful thing: *not verified here*.  It is visible in the
``-rs`` summary, it is counted, and the stage that must not read green when
everything in it skips is ``tests/integration`` -- the preflight integration
job clones the two pinned repositories (``preflight.yml:465``) and then treats
pytest exit code 5 ("collected no tests", i.e. everything skipped) as a
FAILURE (``preflight.yml:581-585``).  The split is what makes a skip honest
rather than a way to stop checking something.

What the guard keys on, and why it is the checkout and not the file
-------------------------------------------------------------------
``require`` skips when the *owning checkout directory* is absent, and fails when
the checkout is present and the file is not.  Keying on the file itself would
collapse two very different situations into one skip: "this host never acquired
the pin" and "the file this guard describes was renamed upstream".  The second
one is what ``test_the_files_this_guard_describes_exist`` exists to catch, and
it must stay red.

Testability
-----------
The vendor root is read at call time and can be redirected with
``UNIPAD_VENDOR_ROOT``, or passed explicitly as ``root=``.  Without one of those
the only way to prove the guard fires is to delete ``vendor/`` on disk, which
is not something a test should do to its own repository; with them,
``tests/unit/test_clean_checkout_selfsufficiency.py`` runs the guarded modules
in a subprocess against a root that does not exist and asserts they skip rather
than error.  The variable is a test seam and nothing else -- no production code
reads it, and no workflow sets it.

Both ``tests/unit`` and ``tests/integration`` import this module; it sits at the
``tests/`` root because that is where the suite's other shared helpers live
(``tests/fixtures``, ``tests/fakes``) and ``tests/conftest.py:40`` puts the
repository root on ``sys.path``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: The repository root, from this file (``tests/vendor_paths.py``).
REPO = Path(__file__).resolve().parents[1]

#: Where the two pinned upstream checkouts live when nothing overrides them.
DEFAULT_ROOT = REPO / "vendor"

#: Redirects :func:`vendor_root`.  A test seam: see the module docstring.
VENDOR_ROOT_ENV = "UNIPAD_VENDOR_ROOT"

#: The two pinned upstreams, by the directory names the lock and the workflows
#: use for them (``config/sources.lock.json`` keys ``device_repo`` and
#: ``unixnu``; ``preflight.yml:487`` maps them to these two directories).
DEVICE_REPO = "UniXNU-qemu"
DRIVER_REPO = "UniXNU"

_MISSING_CHECKOUT = (
    "{checkout} is not present in this checkout.  {root}/ holds only material "
    "that has been acquired -- it is git-excluded, so a fresh clone has none of "
    "it and preflight's unit stage runs against exactly such a checkout.  This "
    "check runs where the pin has been cloned, in tests/integration, whose CI "
    "job refuses to read green when every check in it skips.  Skipping is the "
    "honest outcome; passing would report a contract nobody measured."
)

_MISSING_FILE = (
    "{path} is missing from {checkout}, which IS present.  That is not a "
    "clean-checkout skip: the pinned tree was acquired and the file this guard "
    "describes is not in it, so either the pin moved or the path is stale."
)


def vendor_root() -> Path:
    """The directory holding the two pinned upstream checkouts.

    ``UNIPAD_VENDOR_ROOT`` overrides it, resolved against the repository root
    when relative.  The override exists so a test can point the guard at a root
    that does not exist without touching the real one.
    """
    override = os.environ.get(VENDOR_ROOT_ENV)
    if override:
        candidate = Path(override)
        return candidate if candidate.is_absolute() else REPO / candidate
    return DEFAULT_ROOT


def label(path: Path) -> str:
    """The path as written from the repository root, for messages and skips."""
    try:
        return path.resolve().relative_to(REPO.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _resolve(path: Path, base: Path) -> Path:
    """``path`` expressed against ``base`` instead of the default vendor root.

    This is what makes the override real.  The callers build their paths from
    ``REPO / "vendor" / ...`` at import time, long before any override is set,
    so without the translation an override that is not an ancestor of the real
    tree would leave every read pointing at the real ``vendor/`` -- the guard
    would then pass on a host that has the pin and the liveness run would
    measure nothing.  That is the dead-guard failure mode one level out, and it
    was measured: the first revision of this module had it.

    A path that is not under the default root at all (a ``tmp_path`` file, say)
    is returned unchanged, so the guard degrades to a plain existence check
    rather than inventing a location.
    """
    if base == DEFAULT_ROOT:
        return path
    try:
        relative = path.relative_to(DEFAULT_ROOT)
    except ValueError:
        return path
    return base.joinpath(*relative.parts)


def _checkout_of(path: Path, root: Path) -> Path | None:
    """The pinned checkout that owns ``path``: ``root/UniXNU-qemu``, say.

    ``None`` when the path is not under the vendor root at all, which is what a
    caller passing a ``tmp_path`` file gets -- the guard then degrades to a
    plain existence check rather than guessing.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    return root / parts[0]


def require(*paths: Path, root: Path | None = None) -> Path:
    """Every path must exist in its acquired checkout, or the test skips.

    The existence check is the first thing that happens, before any read: a
    guard written after the thing it guards is dead code, and that is the
    2026-10-24 defect this module's docstring records.

    * the owning checkout directory is absent -> ``pytest.skip`` naming it;
    * the checkout is present and a file is not -> ``AssertionError``, because
      that is a moved anchor and must not read as "not verified here".

    ``root`` overrides where the pinned checkouts are looked for; it defaults to
    :func:`vendor_root`, which honours ``UNIPAD_VENDOR_ROOT``.  Paths are
    re-based onto it, so a caller can build them from the repository root and
    still be redirected -- see :func:`_resolve`.

    Returns the first path so the call can be used inline::

        source = _text(require(ACCELERATOR))

    An empty argument list is a programming error and fails loudly rather than
    silently guarding nothing.
    """
    if not paths:
        raise AssertionError("require() needs at least one path to guard")
    base = vendor_root() if root is None else root
    resolved = [_resolve(path, base) for path in paths]
    for original, path in zip(paths, resolved):
        checkout = _checkout_of(path, base)
        if checkout is not None and not checkout.is_dir():
            pytest.skip(
                _MISSING_CHECKOUT.format(checkout=label(checkout), root=label(base))
            )
        if not path.is_file():
            where = label(checkout) if checkout is not None else label(base)
            raise AssertionError(_MISSING_FILE.format(path=label(original), checkout=where))
    return resolved[0]
