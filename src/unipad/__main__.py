"""``python -m unipad`` entry point (PRD 9.1).

The console script ``unipad`` declared in ``pyproject.toml`` and the module
entry ``python -m unipad.controller`` both resolve to
:func:`unipad.controller.cli.main`.  This module makes the top-level
``python -m unipad`` form work as well, so every documented invocation
reaches the same function and returns the same fixed exit code.

Delegating rather than re-implementing keeps one command table, one error
mapper and one exit-code contract (DRY).
"""

from __future__ import annotations

import sys

from .controller.cli import main

if __name__ == "__main__":
    sys.exit(main())
