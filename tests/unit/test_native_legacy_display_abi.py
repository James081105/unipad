"""Keep the slot-200 ABI declaration and owned-vtable audit in lockstep."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(
    os.environ.get(
        "UNIPAD_LEGACY_ABI_SCRIPT",
        str(ROOT / "vendor" / "UniXNU-qemu" / "scripts" / "build-native-legacy-display.py"),
    )
)


def _source() -> str:
    if not SCRIPT.is_file():
        pytest.skip("device repository checkout is not present")
    return SCRIPT.read_text(encoding="utf-8")


def test_slot_200_is_declared_and_owned_by_the_same_collection() -> None:
    source = _source()
    tree = ast.parse(source, filename=str(SCRIPT))
    assignments = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "owned_concrete_slots"
    }
    slots = assignments.get("owned_concrete_slots")
    assert isinstance(slots, ast.Tuple)
    values = [node.value for node in slots.elts if isinstance(node, ast.Constant)]
    assert values == [0, 1, 7, 14, 84, 86, 87, 141, 155, 184, 188, 200, 288, 290, 296]
    assert "elif i==200:header+=' virtual void submitSwap(const OriginalLegacySwapRequest *);\\n'" in source
    assert "require(i in owned_concrete_slots or i in pure" in source
    assert "sorted(list(owned_concrete_slots)+pure)" in source

