"""Shared pytest configuration for the UniPad test suite.

Ensures the repository root is importable regardless of how pytest was
invoked, and exposes common helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# src/ is on sys.path via the unipad.pth editable shim; add defensively so a
# fresh clone works even before the shim is installed.
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
