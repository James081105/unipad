"""Preserve the original gamma ABI and independently guard selector dispatch."""

from __future__ import annotations

import ast
import os
import struct
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


def test_gamma_slot_200_stays_inherited_and_opaque() -> None:
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
    assert values == [0, 1, 7, 14, 84, 86, 87, 141, 155, 184, 188, 288, 290, 296]
    # Exercise the actual declaration loop. Slot 200 receives the user's gamma
    # bytes; it must retain the original virtual position and implementation.
    loop = next(node for node in tree.body if isinstance(node, ast.For))
    namespace = {"header": "", "pure": [211, 245, 246, 306, 307, 338, 345]}
    exec(compile(ast.Module(body=[loop], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    declarations = namespace["header"].splitlines()
    assert len(declarations) == 190
    assert "void opaque200();" in declarations[200 - 168]
    assert "submitSwap" not in namespace["header"]
    assert "require(i in owned_concrete_slots or i in pure" in source
    assert "sorted(list(owned_concrete_slots)+pure)" in source


# Observed bytes from the exact original family in kernel 0533abe5. Keep this
# independent of the producer's expected values so a wrong edit fails here.
REFERENCE_READS = {
    (0xFFFFFFF005F35658, 24): struct.pack("<Q4I", 0xFFFFFFF00577A0C8, 0, 3084, 0, 0),
    (0xFFFFFFF005F35538, 24): struct.pack("<Q4I", 0xFFFFFFF005779470, 0, 1288, 0, 0),
    (0xFFFFFFF005778054, 36): bytes.fromhex(
        "e80740f9006d40f9e10340f9080040f9082143f900013fd6fd7b41a9ff830091c0035fd6"
    ),
    (0xFFFFFFF0057772D4, 40): bytes.fromhex(
        "e20740f9406c40f9e10f40f9080040f908c942f9030080d200013fd6a0c31fb801000014a0c35fb8"
    ),
}


def _run_contract_guards(reads: dict[tuple[int, int], bytes]) -> None:
    tree = ast.parse(_source(), filename=str(SCRIPT))
    guards = [
        node for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "require"
        and len(node.value.args) == 2
        and isinstance(node.value.args[1], ast.Constant)
        and str(node.value.args[1].value).startswith(("Original selector-17", "Original selector-5"))
    ]
    assert len(guards) == 4

    def require(condition: bool, message: str) -> None:
        assert condition, message

    exec(compile(ast.Module(body=guards, type_ignores=[]), str(SCRIPT), "exec"), {
        "read": lambda va, size: reads[(va, size)], "struct": struct, "require": require,
    })


def test_original_selector_contract_guards_accept_matched_family() -> None:
    _run_contract_guards(REFERENCE_READS)


@pytest.mark.parametrize("key", REFERENCE_READS)
def test_original_selector_contract_guards_reject_changed_family(key: tuple[int, int]) -> None:
    reads = dict(REFERENCE_READS)
    data = bytearray(reads[key])
    data[0] ^= 1
    reads[key] = bytes(data)
    with pytest.raises(AssertionError, match="Original selector-"):
        _run_contract_guards(reads)

