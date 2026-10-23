"""Any module that carries a stage record must spell its fields canonically.

CLAUDE.md "代码阶段与现场阶段分离" requires every *phase* to record four
fields -- 实现状态, Windows/模拟测试状态, 现场验证状态, 阻断原因 -- and
``docs/implementation-plan.md`` carries that table for WP00..WP04.  Two modules
under ``src/unipad/`` additionally put a stage record in their own docstring
(:mod:`unipad.host.httpsvc` and :mod:`unipad.ipa.matrix`), which is the house
pattern a third adopter would copy.

The defect this file pins was found by re-verifying standards CG-2 against the
tree: ``matrix.py`` wrote the second field as ``Windows 模拟测试状态`` -- a
space instead of the slash every authoritative source uses
(``CLAUDE.md:182``, ``docs/testing-strategy.md``, ``docs/implementation-plan.md``,
``docs/launcher.md`` and ``httpsvc.py`` itself).  One conforming module was
therefore the one module that spelled a field name wrong, and any test that
walked ``src/unipad/**/*.py`` looking for the four canonical names -- the remedy
the register itself proposed -- would have reported the *conforming* module as
missing the record.

So the assertion is scoped to what can actually be enforced today: a module that
carries a stage record at all must carry all four fields, under the canonical
spelling, in the module docstring.  A fourth spelling cannot be introduced
quietly, and a record that loses a field fails here rather than in whichever
reader notices first.

Deliberately **not** asserted: that every module under ``src/unipad/`` carries a
record.  Only 2 of 40 do, the CLAUDE.md clause is about phases rather than
modules, and the phase-level table in ``docs/implementation-plan.md`` satisfies
it.  Widening this to all 40 would be 38 modules of docstring churn -- including
``errors.py``, which is an enum -- for a guarantee the phase table already
provides.  If that widening is ever wanted it is a decision to take on purpose,
with the module list written down, not a side effect of a conformance test.

Field status: host-side, no guest involved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "unipad"

#: The four fields, spelled exactly as CLAUDE.md:182 and every authoritative
#: document spells them.  The second is the one that drifted.
CANONICAL_FIELDS: tuple[str, ...] = (
    "实现状态",
    "Windows/模拟测试状态",
    "现场验证状态",
    "阻断原因",
)

#: ``Windows`` and ``模拟`` must be joined by a slash.  A space, a hyphen, a
#: full-width slash or nothing at all are all a *different field name* from the
#: one every authoritative document uses, and the space form is the one that was
#: really observed (``matrix.py``, until it was corrected).  Expressed as a
#: separator rule rather than a list of wrong spellings because the list can
#: never be exhaustive and a substring list is worse than useless -- the
#: shortened form ``Windows/模拟测试`` is a prefix of the canonical name and so
#: would match every conforming module.
SEPARATOR_RE = re.compile(r"Windows[^/]模拟")


def _modules() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _docstring(path: Path) -> str:
    """The module docstring only, so a field named in a comment or a string
    literal does not count as carrying the record."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return ast.get_docstring(tree) or ""


def _carriers() -> list[Path]:
    """Modules whose docstring carries at least one of the four fields."""
    return [p for p in _modules() if any(f in _docstring(p) for f in CANONICAL_FIELDS)]


def test_the_canonical_spellings_are_the_ones_the_documents_use():
    """The authority check: these four strings are what CLAUDE.md names.

    Without this, the test could be passing against a spelling nobody else
    uses -- which is the same failure as the one it was written to catch, one
    level up.
    """
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for field in CANONICAL_FIELDS:
        assert field in claude_md, f"CLAUDE.md does not name {field!r}"


def test_at_least_one_module_carries_the_record_so_this_proves_something():
    """A conformance test over an empty set passes vacuously."""
    carriers = _carriers()
    assert carriers, "no module under src/unipad/ carries a stage record"


@pytest.mark.parametrize("path", _carriers(), ids=lambda p: p.name)
def test_a_module_that_carries_the_record_carries_all_four(path):
    """A partial record is worse than none: it reads as complete."""
    doc = _docstring(path)
    missing = [f for f in CANONICAL_FIELDS if f not in doc]
    assert not missing, f"{path.name} carries a stage record but not {missing}"


@pytest.mark.parametrize("path", _carriers(), ids=lambda p: p.name)
def test_a_carrying_module_joins_windows_and_simulation_with_a_slash(path):
    """The NEW-1 regression, stated as the rule rather than a list of typos.

    A field spelled differently is a field a reader cannot find -- and the
    module that spells it wrongly is the *conforming* one, so a conformance
    test written as "does this module name the four fields" reports the good
    module as missing and lets the bad one pass.
    """
    doc = _docstring(path)
    match = SEPARATOR_RE.search(doc)
    assert match is None, (
        f"{path.name} joins Windows and 模拟 with {match.group(0)!r}; the "
        "canonical field name is 'Windows/模拟测试状态', with a slash"
    )


def test_the_phase_table_keeps_all_four_columns():
    """The clause this whole file defers to.

    CLAUDE.md requires the four fields per *phase*; ``implementation-plan.md``
    is where that lives.  A dropped column there is the real regression, and it
    is the one this test actually guards.
    """
    plan = (REPO_ROOT / "docs" / "implementation-plan.md").read_text(encoding="utf-8")
    header = "| 阶段 | 实现状态 | Windows/模拟测试状态 | 现场验证状态 | 阻断原因 |"
    assert header in plan, (
        "the implementation plan's four-field table header changed; every "
        "phase row below it is written against these column names"
    )

    # ... and at least one phase row really fills all four, so the header is
    # not the only thing that survived.
    rows = [
        line
        for line in plan.splitlines()
        if line.startswith("| WP") and line.count("|") >= 6
    ]
    filled = [r for r in rows if all(cell.strip() for cell in r.split("|")[2:-2])]
    assert filled, "no WP row fills all four fields"
