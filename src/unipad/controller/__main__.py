"""``python -m unipad.controller`` entry point (PRD 9.1).

Delegates to :func:`unipad.controller.cli.main`, which prints exactly one
machine-readable JSON object and returns a fixed exit code.  The console
script ``unipad`` declared in ``pyproject.toml`` points at the same function.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
