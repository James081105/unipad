"""UniPad test suite.

Layout (docs/testing-strategy.md):

- ``tests/unit/``       per-module unit tests (pure, fast, no processes)
- ``tests/fakes/``      simulation doubles: fake QEMU binary, fake guest service
- ``tests/fixtures/``   builders for real-shaped inputs (IPAs, Mach-O slices)
- ``tests/acceptance/`` milestone-gate simulations (M0-M4, IPA-MVP)

This directory is a *regular* package (``__init__.py`` at every level) on
purpose: an unrelated third-party package named ``tests`` is installed in
this host's site-packages, and without ``__init__.py`` the local directory
would lose the name to it (PEP-420 namespace packages rank below regular
packages already on ``sys.path``).
"""
