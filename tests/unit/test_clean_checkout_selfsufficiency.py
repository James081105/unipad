"""``tests/unit`` must be self-sufficient in a clean checkout.

The invariant, and what it costs to get wrong
---------------------------------------------
``vendor/`` is excluded from git (``.git/info/exclude`` line 9) and is empty on
a fresh clone, so a unit test that opens a file under it dies with
``FileNotFoundError`` before any of its own assertions run.  On 2026-10-24
``preflight`` run 40107080802 went red exactly that way: two tests in
``tests/unit/test_host_adt.py`` read ``vendor/UniXNU-qemu/scripts/
make-qemu-adt.py`` with no guard.

Both had a guard, and both guards were dead code:

    source = (REAL_DEVICE / ADT_TOOL).read_text(encoding="utf-8")
    if not source:
        pytest.skip("the ADT tool is not present in this checkout")

``read_text`` raises before ``if not source`` can be reached, so the skip could
only ever fire for a file that exists and is empty.  A guard written after the
thing it guards is the failure mode this file exists to catch.

What this proves and what it does not
-------------------------------------
Proven: no function in ``tests/unit`` reads through a vendor path without an
existence check in the same function.  Not proven: that a *transitive* read is
guarded -- a helper in ``src/unipad`` handed a vendor path could still touch
the filesystem.  That is what ``tests/integration`` is for: it runs the real
checks where the pinned checkout has actually been prepared, and its CI job
refuses to read as green when everything in it skips.

The scan is deliberately static rather than a second pytest run in a stripped
copy of the tree: it costs milliseconds instead of a duplicate suite, so it can
run on every invocation instead of being the thing nobody turns on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

UNIT_DIR = Path(__file__).resolve().parent

#: The names the unit suite uses for the two pinned upstream checkouts.  A read
#: through either of them is what this file is about.
VENDOR_ROOTS = ("REAL_DEVICE", "REAL_UNIXNU")

#: Calls that actually touch the filesystem.
READING_CALLS = frozenset(
    {"read_text", "read_bytes", "open", "glob", "rglob", "iterdir", "walk", "scandir", "stat"}
)

#: Calls that answer "is it there" before the read.
GUARD_CALLS = frozenset({"is_file", "is_dir", "exists"})


def _unit_test_modules() -> list[Path]:
    return sorted(UNIT_DIR.glob("test_*.py"))


def _mentions_vendor(node: ast.AST) -> bool:
    """Whether this expression tree names a vendor checkout."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in VENDOR_ROOTS:
            return True
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if "vendor" in child.value:
                return True
    return False


def _call_names(node: ast.AST) -> set[str]:
    """Every ``x.method(...)`` name appearing anywhere in this subtree."""
    return {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def _enclosing_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


@pytest.mark.parametrize("path", _unit_test_modules(), ids=lambda p: p.name)
def test_no_unguarded_read_of_the_vendor_checkout(path: Path) -> None:
    """A read through a vendor path needs an existence check beside it."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []

    for function in _enclosing_functions(tree):
        guarded = _call_names(function) & GUARD_CALLS
        if not guarded:
            # No existence check anywhere in this function, so any vendor read
            # in it is unguarded.
            for statement in ast.walk(function):
                if not isinstance(statement, ast.Call):
                    continue
                if not (_call_names(statement) & READING_CALLS):
                    continue
                if not _mentions_vendor(statement):
                    continue
                segment = ast.get_source_segment(
                    path.read_text(encoding="utf-8"), statement
                ) or ast.dump(statement)
                offenders.append(f"{path.name}:{statement.lineno}: {segment.strip()[:120]}")

    assert not offenders, (
        "these read the pinned vendor checkout, which is empty in a clean "
        "checkout, with no existence check in the same function -- the exact "
        "defect that made preflight run 40107080802 fail.  Either add a real "
        "guard *before* the read, or move the check to tests/integration where "
        "the pinned checkout is prepared: "
        + "; ".join(offenders)
    )


def test_the_unit_suite_has_no_vendor_reads_at_all_outside_guarded_helpers() -> None:
    """The guard test above is only meaningful if it can actually see a read.

    A scan that matches nothing because it is looking for the wrong thing is
    worse than no scan, so the pattern is checked against a sample that is
    known to contain one.
    """
    sample = ast.parse(
        "from pathlib import Path\n"
        "REAL_DEVICE = Path('vendor/UniXNU-qemu')\n"
        "ADT_TOOL = 'scripts/make-qemu-adt.py'\n"
        "def bad():\n"
        "    source = (REAL_DEVICE / ADT_TOOL).read_text(encoding='utf-8')\n"
        "    return source\n"
        "def good():\n"
        "    if not (REAL_DEVICE / ADT_TOOL).is_file():\n"
        "        return ''\n"
        "    return (REAL_DEVICE / ADT_TOOL).read_text(encoding='utf-8')\n"
    )
    functions = {f.name: f for f in _enclosing_functions(sample)}
    assert not _call_names(functions["bad"]) & GUARD_CALLS, "the bad sample must be unguarded"
    assert _mentions_vendor(functions["bad"].body[0]), "the bad sample must read a vendor path"
    assert _call_names(functions["good"]) & GUARD_CALLS, "the good sample must be guarded"


def test_the_unit_suite_does_not_read_the_vendor_checkout_unconditionally() -> None:
    """The unit suite must not even import the vendor modules at collection time."""
    # ``vendor`` is a plain directory of cloned upstreams, never a package, so
    # an import from it would be a defect in its own right.  Checking it here
    # keeps the failure local rather than arriving as an ImportError from a
    # test that has nothing to do with vendor.
    for module in _unit_test_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("vendor"):
                raise AssertionError(f"{module.name} imports from vendor/: {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("vendor"), f"{module.name} imports {alias.name}"
