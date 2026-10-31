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
Three things, in order of how much they cost:

1. **Statically**: no function or module body in ``tests/unit`` reads through a
   vendor path without an existence check evaluated *before* it.  The scan
   resolves constant indirection (``KIT`` names ``vendor``;
   ``ACCELERATOR = KIT / "..."`` does not) and local helper indirection
   (``_text(ACCELERATOR)``), because both of those are how reads got past the
   first revision of this file.  It costs milliseconds.
2. **At runtime**: with the vendor root pointed somewhere that does not exist,
   every module that imports the shared guard skips instead of failing.  This is
   the measurement, and it is the one that settles the question -- a static scan
   proves a *shape*, and 2026-10-31 lost thirty tests to a shape this repository
   had already seen.  It costs about a minute in a subprocess.
3. **The other direction**: on a host that has the pin, the guard stays out of
   the way.  A guard that skips everywhere is the same defect one level out.

Not proven: that a *transitive* read is guarded -- a helper in ``src/unipad``
handed a vendor path could still touch the filesystem.  ``test_qemu_metal_
counters.py`` gets its paths from ``unipad.host.qemu_metal.SOURCES`` and is
guarded only because that fixture happens to check them; a future module taking
a vendor path from ``src/unipad`` without checking would pass the scan.  That is
what ``tests/integration`` is for: it runs the real checks where the pinned
checkout has actually been prepared, and its CI job refuses to read as green
when everything in it skips.

The static half is deliberately not a second pytest run in a stripped copy of
the tree: it costs milliseconds instead of a duplicate suite, so it can run on
every invocation instead of being the thing nobody turns on.  The runtime half
*is* a second pytest run, and it is scoped to the modules that import the shared
guard rather than the whole suite, because the whole suite is 40 minutes and the
preflight job's budget is 20.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import tests.vendor_paths as vendor_paths
from tests.vendor_paths import require

UNIT_DIR = Path(__file__).resolve().parent
ROOT = UNIT_DIR.parents[1]

#: The names the unit suite uses for the two pinned upstream checkouts.  A read
#: through either of them is what this file is about.
VENDOR_ROOTS = ("REAL_DEVICE", "REAL_UNIXNU")

#: Calls that actually touch the filesystem.
READING_CALLS = frozenset(
    {"read_text", "read_bytes", "open", "glob", "rglob", "iterdir", "walk", "scandir", "stat"}
)

#: Calls that answer "is it there" before the read.  ``require`` is the shared
#: guard in ``tests/vendor_paths.py``; the ``is_*``/``exists`` entries are the
#: per-file spelling it replaced.
GUARD_CALLS = frozenset({"is_file", "is_dir", "exists", "require"})

#: The shared guard's import line.  A module that has it is a module the
#: liveness run below has to cover, which is how that run stays complete
#: without a hand-maintained list going stale.
GUARD_IMPORT = "from tests.vendor_paths import"


def _unit_test_modules() -> list[Path]:
    return sorted(UNIT_DIR.glob("test_*.py"))


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = <expression>`` and the expression's source."""
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        out[node.targets[0].id] = ast.unparse(node.value)
    return out


def _vendor_names(tree: ast.Module) -> set[str]:
    """Constants that name a vendor checkout, directly or through each other.

    The indirection is the point.  ``KIT = REPO / "vendor" / "UniXNU-qemu" / ...``
    spells ``vendor``; ``ACCELERATOR = KIT / "QemuMetalAccelerator.cpp"`` does
    not, and a scan that only matches the literal never sees the read through
    ``ACCELERATOR``.  That is how three files in this directory carried
    unguarded reads past the first revision of this scan.
    """
    constants = _module_constants(tree)
    names: set[str] = set()
    for _ in range(8):  # fixpoint; the chains are two or three links long
        for name, value in constants.items():
            tokens = {token.strip("'\"()") for token in value.replace(",", " ").split()}
            if "vendor" in value or (tokens & names):
                names.add(name)
    return names


def _mentions_vendor(node: ast.AST, vendor_names: set[str] = frozenset()) -> bool:  # type: ignore[assignment]
    """Whether this expression tree names a vendor checkout."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and (child.id in VENDOR_ROOTS or child.id in vendor_names):
            return True
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if "vendor" in child.value:
                return True
    return False


def _call_name(node: ast.Call) -> str:
    """``read_text`` for ``x.read_text()`` and for ``require()`` alike."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _call_names(node: ast.AST) -> set[str]:
    """Every call name appearing anywhere in this subtree."""
    return {
        _call_name(child)
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _call_name(child)
    }


def _enclosing_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _reads_through_a_helper(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this function's body calls something that touches the disk."""
    return any(
        _call_name(child) in READING_CALLS
        for child in ast.walk(function)
        if isinstance(child, ast.Call)
    )


def _vendor_reads_and_guards(
    body: ast.AST, source: str, vendor_names: set[str], readers: set[str]
) -> list[tuple[int, str]]:
    """``[(line, snippet)]`` for every vendor read in ``body`` that is unguarded.

    A read is either a filesystem call on a vendor path or a call to a local
    helper that does one, handed a vendor path.  The helper case is what the
    first revision of this scan could not see, and it is the shape all three
    2026-10-31 offenders used.

    "Unguarded" means no existence check is evaluated before it.  Order is
    decided by position, with one exception that matters: in
    ``_text(require(ACCELERATOR))`` the guard is an *argument* of the read, so
    it runs first even though it starts at a later column.  A guard nested
    inside the read's own arguments therefore counts; a guard that is a later
    sibling on the same line does not, and neither does one further down.
    """
    guards: list[ast.Call] = []
    reads: list[ast.Call] = []
    for node in ast.walk(body):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if not name or not _mentions_vendor(node, vendor_names):
            continue
        if name in GUARD_CALLS:
            guards.append(node)
        elif name in READING_CALLS or name in readers:
            reads.append(node)

    unguarded: list[tuple[int, str]] = []
    for read in reads:
        nested = any(
            guard is not read and _contains(read, guard) for guard in guards
        )
        if nested:
            continue
        before = any(
            (guard.lineno, guard.col_offset) < (read.lineno, read.col_offset)
            for guard in guards
        )
        if before:
            continue
        segment = ast.get_source_segment(source, read) or ast.dump(read)
        unguarded.append((read.lineno, segment.strip()[:120]))
    return unguarded


def _contains(outer: ast.AST, inner: ast.AST) -> bool:
    """Whether ``inner`` is inside ``outer``'s subtree."""
    return any(child is inner for child in ast.walk(outer))


@pytest.mark.parametrize("path", _unit_test_modules(), ids=lambda p: p.name)
def test_no_unguarded_read_of_the_vendor_checkout(path: Path) -> None:
    """A read through a vendor path needs an existence check *before* it."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    vendor_names = _vendor_names(tree)
    readers = {fn.name for fn in _enclosing_functions(tree) if _reads_through_a_helper(fn)}
    offenders: list[str] = []

    def check(body: ast.AST, where: str) -> None:
        for lineno, segment in _vendor_reads_and_guards(body, source, vendor_names, readers):
            offenders.append(f"{path.name}:{lineno} ({where}): {segment}")

    for function in _enclosing_functions(tree):
        check(function, function.name)
    # Module-level statements run at collection time, before any test body, so
    # an unguarded one is a collection error rather than a failure.  Function
    # and class bodies are excluded: they are checked as their own scope above,
    # and a module-level pass over them would report every read twice.
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        check(statement, "<module>")

    assert not offenders, (
        "these read the pinned vendor checkout, which is empty in a clean "
        "checkout, with no existence check *before* the read in the same "
        "function -- the exact defect that made preflight run 40107080802 "
        "fail, and the one that made 2026-10-31's clean-checkout run lose 30 "
        "tests.  Either guard the read before it happens (the shared guard is "
        "tests/vendor_paths.require), or move the check to tests/integration "
        "where the pinned checkout is prepared: "
        + "; ".join(offenders)
    )


def test_the_scan_can_see_the_reads_it_claims_to_see() -> None:
    """The scan above is only meaningful if it can actually see a read.

    A scan that matches nothing because it is looking for the wrong thing is
    worse than no scan, so the pattern is checked against samples that are
    known to contain each shape -- including the one that got past the first
    revision of this file for a year.
    """
    sample_source = (
        "from pathlib import Path\n"
        "REAL_DEVICE = Path('vendor/UniXNU-qemu')\n"
        "ADT_TOOL = 'scripts/make-qemu-adt.py'\n"
        "def _text(path):\n"
        "    return path.read_text(encoding='utf-8')\n"
        "def direct():\n"
        "    return (REAL_DEVICE / ADT_TOOL).read_text(encoding='utf-8')\n"
        "def through_a_helper():\n"
        "    return _text(REAL_DEVICE / ADT_TOOL)\n"
        "def through_a_constant():\n"
        "    return _text(ADT_TOOL_PATH)\n"
        "def guarded_inline():\n"
        "    return _text(require(REAL_DEVICE / ADT_TOOL))\n"
        "def guarded_before():\n"
        "    if not (REAL_DEVICE / ADT_TOOL).is_file():\n"
        "        return ''\n"
        "    return (REAL_DEVICE / ADT_TOOL).read_text(encoding='utf-8')\n"
        "def guard_after_the_read():\n"
        "    text = (REAL_DEVICE / ADT_TOOL).read_text(encoding='utf-8')\n"
        "    if not text:\n"
        "        return ''\n"
        "    return text\n"
        "ADT_TOOL_PATH = REAL_DEVICE / ADT_TOOL\n"
    )
    sample = ast.parse(sample_source)
    names = _vendor_names(sample)
    readers = {fn.name for fn in _enclosing_functions(sample) if _reads_through_a_helper(fn)}
    functions = {fn.name: fn for fn in _enclosing_functions(sample)}

    def offenders_of(name: str) -> list[str]:
        return [
            f"{lineno}:{snippet}"
            for lineno, snippet in _vendor_reads_and_guards(
                functions[name], sample_source, names, readers
            )
        ]

    assert "ADT_TOOL_PATH" in names, (
        "the sample's constant indirection must resolve: a scan that only "
        "matches the literal 'vendor' never sees a read through a constant"
    )
    assert "_text" in readers, "the sample's helper must be recognised as a reader"
    for shape in ("direct", "through_a_helper", "through_a_constant", "guard_after_the_read"):
        assert offenders_of(shape), f"the scan must flag the {shape!r} shape"
    for shape in ("guarded_inline", "guarded_before"):
        assert not offenders_of(shape), f"the scan must accept the {shape!r} shape"


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


# --- the guard itself has to be alive ---------------------------------------
#
# The scan above is static, and a static scan proves a *shape*.  What the
# 2026-10-31 clean-checkout run lost was not a shape this repository had never
# seen -- it was thirty tests that read a path nobody had, in a checkout where
# nobody had it.  The only thing that settles that is running the guarded
# modules against a vendor root that does not exist and watching them skip.
#
# The root is redirected with ``UNIPAD_VENDOR_ROOT`` (see
# ``tests/vendor_paths.py``), so nothing on disk is touched: the real ``vendor/``
# stays exactly where it is, and the child pytest simply looks somewhere else.


def _guarded_modules() -> list[str]:
    """Every unit module that imports the shared vendor guard -- except this one.

    Derived from the import rather than listed, so a module added next month
    joins this run on its own.  A hand-maintained tuple is the thing that goes
    stale, and a stale list is a guard that stops guarding.

    This file excludes itself, and the reason is not tidiness.  It spawns the
    run it is describing: ``_run()`` executes every module in this list as a
    child pytest.  This module imports the guard, so the first version of this
    list contained it, and the child therefore re-ran this module -- which
    spawned another child, which re-ran it again.  The tree is bounded only by
    the child's 600-second timeout rather than by a depth, so the outer run
    dies of ``TimeoutExpired`` before it can report anything, and the two tests
    below failed on a machine with the vendor tree present and every guard
    correct.  MEASURED 2026-10-31: the same four modules without this one in the
    list run to completion in a few minutes and exit 0 with the expected skips.

    Excluding the spawner keeps the derivation honest for every module that is
    not the one doing the spawning, which is the property the docstring above
    is actually protecting.
    """
    this_file = Path(__file__).resolve()
    found = [
        path.relative_to(ROOT).as_posix()
        for path in _unit_test_modules()
        if path.resolve() != this_file and GUARD_IMPORT in path.read_text(encoding="utf-8")
    ]
    return sorted(found)


def _counts_line(output: str) -> str:
    """pytest's ``N passed, M skipped`` line, or the last non-empty line.

    The distinction matters here.  Measured 2026-10-31 on this host: with stdout
    redirected, ``pytest -q`` emits the progress line and then *nothing* -- the
    counts line is lost.  So a test that asserts on the counts line fails on a
    run that was perfectly fine, which is a failure of the checker rather than of
    anything it checks.  Falling back to the last non-empty line keeps the
    message useful either way.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if re.match(r"^\d+ (passed|failed|skipped|error|xfailed|xpassed)", line):
            return line
    return lines[-1] if lines else "<no output>"


def _run(modules: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``modules`` as a child pytest, in this repository, with ``env``.

    ``-rs`` is not decoration: with the counts line lost (see
    :func:`_counts_line`) the ``-rs`` report is the only place the run says which
    tests skipped and why, and "did anything skip at all" is the question this
    whole file exists to answer.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *modules],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def test_the_shared_guard_is_imported_by_something() -> None:
    """The liveness run below proves nothing if no module uses the guard."""
    modules = _guarded_modules()
    assert modules, (
        "no module in tests/unit imports the shared vendor guard "
        f"({GUARD_IMPORT!r}); either every vendor read is guarded per-file "
        "again, or the guard was deleted"
    )


def test_the_guarded_modules_skip_rather_than_error_when_the_vendor_root_is_absent(
    tmp_path: Path,
) -> None:
    """With no ``vendor/`` at all, the guarded modules must skip, not fail.

    This is the test that would have caught 2026-10-31.  Measured then, on a
    ``git archive HEAD`` checkout: ``pytest tests/unit`` exited 1 with 30
    failures and 3 errors, every one of them a read of
    ``vendor/UniXNU-qemu/package/QemuMetal/*`` or
    ``vendor/UniXNU/drivers/NativeMobileDisplay/*``.  A skip is exit code 0 with
    the count in the ``-rs`` summary; an error is a red stage on every clean
    checkout for a reason no code change can fix.

    Exit code 5 ("no tests collected") is also a failure here, and deliberately
    so: a stage where everything skips must not read as a stage with nothing in
    it -- the same rule the preflight integration job applies to
    ``tests/integration`` (``preflight.yml:581-585``).
    """
    modules = _guarded_modules()
    assert modules, "nothing imports the shared guard; see the test above"
    absent = tmp_path / "no-such-vendor-root"
    env = {
        **os.environ,
        vendor_paths.VENDOR_ROOT_ENV: str(absent),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = _run(modules, env)
    counts = _counts_line(done.stdout)
    assert done.returncode == 0, (
        f"the guarded modules did not pass with {vendor_paths.VENDOR_ROOT_ENV}="
        f"{absent} (exit {done.returncode}, {counts}); a clean checkout runs "
        "them with no vendor tree at all\n" + done.stdout[-4000:]
    )
    # pytest's own markers, in the case it spells them in, rather than a word
    # search: a path or a docstring can contain "error" without anything having
    # errored.
    assert "FAILED" not in done.stdout and "ERROR" not in done.stdout, (
        f"the guarded modules failed or errored rather than skipping: {counts}\n"
        + done.stdout[-4000:]
    )
    assert "SKIPPED [" in done.stdout, (
        f"nothing skipped ({counts}); with the vendor root absent every guarded "
        "test has to skip, so a run with no skips means the guard is not where "
        "this test thinks it is\n" + done.stdout[-4000:]
    )


def _vendor_paths_in(module_name: str) -> list[Path]:
    """Every ``Path`` constant a guarded module builds under ``vendor/``.

    Read out of the imported module rather than re-parsed, so the paths are the
    ones the tests will actually hand to the guard.

    ``module_name`` arrives as a repository-relative POSIX path -- that is what
    :func:`_guarded_modules` returns, because a path is what pytest is handed.
    ``import_module`` wants a dotted name, so the ``.py`` comes off and the
    separators become dots.  The first version passed the path straight through
    and every call died with ``ModuleNotFoundError: No module named
    'tests/unit/test_qemu_metal_bundle_guard'``, which is a failure of the
    checker rather than of anything it checks.
    """
    dotted = module_name[: -len(".py")] if module_name.endswith(".py") else module_name
    module = importlib.import_module(dotted.replace("/", "."))
    return [
        value
        for value in vars(module).values()
        # Files only.  A guarded module's directory constants -- the QemuMetal
        # kit, say -- are never handed to ``require``; the guards hand it
        # ``KIT / "QemuMetalScanout.mm"``.  Collecting the directory too made
        # ``require`` refuse it as "missing from a checkout that IS present",
        # which is the guard correctly reporting that a directory is not a file.
        if isinstance(value, Path) and "vendor" in value.parts and value.is_file()
    ]


def test_the_guard_does_not_skip_where_the_pin_is_present() -> None:
    """The other half: the guard must not swallow the check where it can run.

    A guard that skips on a host that HAS the pinned tree is the 2026-10-26
    defect one level out -- the check silently stops happening everywhere.  So
    every vendor path the guarded modules build is handed to ``require`` on this
    host, and none of them may skip.

    This is deliberately NOT a second subprocess run of the same modules with
    the real root.  Measured 2026-10-31, that costs 437 s -- the selector module
    compiles C++ with MSVC -- and the preflight job's whole budget is 20
    minutes.  The assertions themselves are covered by the ordinary suite run
    on a host that has the pin; what is checked here is the part a normal run
    cannot see, which is whether the guard stays out of the way.
    """
    modules = _guarded_modules()
    assert modules, "nothing imports the shared guard; see the test above"
    root = vendor_paths.vendor_root()
    if not (root / vendor_paths.DEVICE_REPO).is_dir():
        pytest.skip(
            f"{root / vendor_paths.DEVICE_REPO} is not present on this host, so "
            "the 'guard does not over-skip' half of this check is UNVERIFIED here"
        )
    checked = 0
    for module in modules:
        for path in _vendor_paths_in(module):
            assert vendor_paths.require(path) is path, (
                f"{module} built {path} and the guard rewrote it; on a host with "
                "the pin the guard has to be transparent"
            )
            checked += 1
    assert checked, (
        "no vendor path was found in any guarded module; the over-skip check "
        "above proved nothing"
    )


# --- the guard, on its own ---------------------------------------------------


def test_require_skips_when_the_owning_checkout_is_absent(tmp_path: Path) -> None:
    """A missing checkout is a skip, and the reason names the checkout."""
    root = tmp_path / "vendor"
    path = root / vendor_paths.DEVICE_REPO / "package" / "QemuMetal" / "QemuMetalAccelerator.cpp"
    with pytest.raises(pytest.skip.Exception) as caught:
        vendor_paths.require(path, root=root)
    assert vendor_paths.DEVICE_REPO in str(caught.value), (
        "the skip has to name which pinned checkout is missing, or the reader "
        "cannot tell 'not acquired here' from 'broken'"
    )


def test_require_fails_when_the_checkout_is_present_and_the_file_is_not(tmp_path: Path) -> None:
    """The teeth: a moved anchor is a failure, not a skip.

    Keying the guard on the file itself would collapse "this host never
    acquired the pin" and "the file this guard describes was renamed" into one
    skip, and the second is exactly what
    ``test_the_files_this_guard_describes_exist`` exists to catch.
    """
    root = tmp_path / "vendor"
    (root / vendor_paths.DEVICE_REPO).mkdir(parents=True)
    path = root / vendor_paths.DEVICE_REPO / "package" / "QemuMetal" / "Renamed.cpp"
    with pytest.raises(AssertionError) as caught:
        vendor_paths.require(path, root=root)
    assert "IS present" in str(caught.value), (
        "a missing file inside an acquired checkout must fail, and say that "
        "the checkout is there"
    )


def test_require_returns_the_path_when_everything_is_there(tmp_path: Path) -> None:
    """The guard is transparent when there is nothing to guard against."""
    root = tmp_path / "vendor"
    path = root / vendor_paths.DEVICE_REPO / "board.toml"
    path.parent.mkdir(parents=True)
    path.write_text("[payload]\n", encoding="utf-8")
    assert vendor_paths.require(path, root=root) is path


def test_require_with_no_arguments_is_a_programming_error() -> None:
    """An empty guard would silently protect nothing."""
    with pytest.raises(AssertionError):
        vendor_paths.require()


def test_the_environment_override_redirects_the_root(tmp_path: Path, monkeypatch) -> None:
    """The seam the liveness run uses, checked directly."""
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(vendor_paths.VENDOR_ROOT_ENV, str(elsewhere))
    assert vendor_paths.vendor_root() == elsewhere
    monkeypatch.delenv(vendor_paths.VENDOR_ROOT_ENV)
    assert vendor_paths.vendor_root() == vendor_paths.DEFAULT_ROOT


def test_the_override_actually_redirects_the_read(tmp_path: Path, monkeypatch) -> None:
    """The seam has to move the read, not just the reported root.

    This is the regression test for a bug in the first revision of
    ``tests/vendor_paths.py``.  The callers build their paths from
    ``REPO / "vendor" / ...`` at import time, so an override that is not an
    ancestor of the real tree left every read pointing at the real ``vendor/``:
    the guard passed on a host that has the pin, the liveness run above
    measured nothing, and it read green.  A seam that does not redirect is the
    dead-guard failure mode one level out, and it is invisible unless the read
    itself is checked.
    """
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(vendor_paths.VENDOR_ROOT_ENV, str(elsewhere))
    real = vendor_paths.DEFAULT_ROOT / vendor_paths.DEVICE_REPO / "board.toml"
    with pytest.raises(pytest.skip.Exception):
        vendor_paths.require(real)
    # And the mirror image: a file that exists only under the override is found
    # through a path built from the default root.
    planted = elsewhere / vendor_paths.DEVICE_REPO / "board.toml"
    planted.parent.mkdir(parents=True)
    planted.write_text("[payload]\n", encoding="utf-8")
    assert vendor_paths.require(real) == planted, (
        "the guard read the real vendor tree instead of the override"
    )


def test_a_path_outside_the_vendor_root_degrades_to_a_plain_check(tmp_path: Path) -> None:
    """``tmp_path`` files are not vendor material and must not be guessed at."""
    root = tmp_path / "vendor"
    (root / vendor_paths.DEVICE_REPO).mkdir(parents=True)
    elsewhere = tmp_path / "not-under-the-root.txt"
    elsewhere.write_text("x", encoding="utf-8")
    assert vendor_paths.require(elsewhere, root=root) is elsewhere
    with pytest.raises(AssertionError):
        vendor_paths.require(tmp_path / "gone.txt", root=root)
