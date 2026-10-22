"""Shared pytest configuration for the UniPad test suite.

Path setup
----------
Three directories are put on ``sys.path`` so every import style the suite uses
resolves the same way regardless of how pytest was invoked:

* the repository root -- for ``import unipad...`` (the package lives in ``src/``
  and is installed as an editable shim on this host; the entry is added
  defensively so a fresh clone works before the shim exists);
* ``src/`` -- same reason, kept explicit because the ``.pth`` shim is a host
  artefact, not a checked-in file;
* ``tests/`` -- for ``import fakes.fake_qemu`` / ``import fixtures.ipa_builder``.

The ``tests`` directory is a *regular* package (``__init__.py`` at every level)
on purpose: an unrelated third-party package named ``tests`` is installed in
this host's site-packages, and without ``__init__.py`` the local directory would
lose the name to it (PEP-420 namespace packages rank below regular packages
already on ``sys.path``).  Adding ``tests/`` itself to ``sys.path`` is still
useful, because ``tests/fakes/fake_qemu.py`` documents both import styles and
the subprocess side of the fake must be runnable as a plain script.

Determinism rules enforced here (docs/testing-strategy.md D3): fakes must be
deterministic, tests must not sleep for long, and nothing may touch the network
or the repository working tree.  The markers declared in ``pyproject.toml``
(``slow`` / ``acceptance``) are registered by pytest itself; this file only
guards the path setup and fails early if it is wrong (crash early, rather than
letting a shadowed import produce a misleading collection error).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
SRC = ROOT / "src"

for entry in (str(ROOT), str(SRC), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# Fail early and loudly: a silent path mistake would surface much later as a
# shadowed import or a "module not found" inside one test.
try:
    import unipad  # noqa: F401 - importability guard
    from fakes import fake_qemu as _fake_qemu  # noqa: F401 - importability guard
except Exception as exc:  # pragma: no cover - only reachable on a broken checkout
    raise RuntimeError(
        "tests/conftest.py could not import the suite dependencies; check that "
        f"{ROOT}, {SRC} and {TESTS} are importable (original error: {exc!r})"
    ) from exc
