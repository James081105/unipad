"""No step of this workflow may reference a variable nothing binds.

Why this file exists
--------------------
``set -euo pipefail`` is the right setting and it turns a typo into a hard stop,
which is what you want.  What it cannot do is tell you the variable was never
bound *before* the minutes are spent.  Measured on run 40201825941:

    /Users/runner/work/_temp/....sh: line 107: LOCK_PATH: unbound variable

The chain step referenced ``$LOCK_PATH`` and this workflow declares no
workflow-level and no job-level ``env:`` block -- ``LOCK_PATH`` is set per step,
by the steps that need it, and this one did not.  The step died at its
display-driver divergence, *after* the two 3.3 GB and 5.2 GB volumes had been
moved into place and after the kernel and the payload had been proved present.
``bash -n`` passes on that script: the quoting is valid, the variable is simply
absent, and only ``-u`` at runtime notices.

The same run then had a second, worse instance of the same class waiting one
line further down: ``print(f"... (the lock's recorded divergence)")`` inside a
``python3 -c '...'`` heredoc.  The apostrophe in ``lock's`` terminates the
single-quoted shell string, and the step then fails with ``unexpected EOF while
looking for matching ')'`` -- reproduced locally, not theorised.

What is proven here
-------------------
That every ``$VAR`` / ``${VAR}`` a step's shell expands is bound by something a
reader can see: the step's own ``env:``, an assignment or ``for``/``read`` in the
script itself, an explicit ``${VAR:-default}``, or a variable GitHub itself
provides.  Anything else is a variable that will stop the step under ``-u``.

This is a static guard and it is deliberately crude -- see ``_script`` for the
two things it does not attempt (it does not model shell quoting, and it does not
execute anything).  It is also cheap: no macOS host, no QEMU, no network, and it
runs in milliseconds.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-macos.yml"

pytestmark = pytest.mark.acceptance

#: Variables GitHub itself provides to every step.  The prefixes cover the
#: contexts; the names cover the ones this workflow actually reads.
GITHUB_PREFIXES = ("GITHUB_", "RUNNER_", "ACTIONS_", "INPUTS_")
ALWAYS_PRESENT = {
    "HOME", "PATH", "PWD", "OLDPWD", "CI", "SHELL", "TMPDIR", "LANG", "LC_ALL",
    "USER", "TZ", "IFS", "RANDOM", "SECONDS", "LINENO", "BASH_SOURCE",
    "FUNCNAME", "PIPESTATUS", "REPLY", "OPTARG", "OPTIND", "ERR",
}

#: ``name=``, ``declare -a name=``, ``export name=``, ``local name=``, and the
#: ``for name in`` / ``read name`` forms.  The ``-flag`` group is what makes
#: ``declare -a payload_args=()`` count as a binding; without it every array in
#: the workflow reads as unbound.
ASSIGNMENT = re.compile(
    r"(?:^|[;&|]|\b(?:local|export|declare|read|for))\s*(?:-[A-Za-z]+\s+)*([A-Za-z_]\w*)=",
    re.MULTILINE,
)
EXPANSION = re.compile(r"\$\{?([A-Za-z_]\w*)")


def _workflow(path: Path | None = None) -> Any:
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover - depends on the environment
        pytest.skip("PyYAML is required to parse build-macos.yml")
    return yaml.safe_load((path or WORKFLOW).read_text(encoding="utf-8"))


def _script(run: str) -> str:
    """A step's shell with GitHub expressions and comments taken out.

    Comments go FIRST, and that ordering is the whole lesson of the first draft:
    an apostrophe in a comment is prose, so matching it against the next
    apostrophe deletes the code between them.  The first version stripped quoted
    spans before comments and reported a clean file while not reading half of it.

    Quoted spans are deliberately NOT stripped.  A multi-line
    ``python3 -c '...'`` heredoc cannot survive a regex that treats quotes as
    delimiters, and the result was three real assignments read as unbound.  The
    cost is accepted: a ``$VAR`` inside a single-quoted string is a false
    positive this guard cannot tell from the real thing, and there are none in
    this workflow today.
    """
    source = re.sub(r"\$\{\{[^}]*\}\}", " ", run)
    return "\n".join(
        "" if line.lstrip().startswith("#") else line for line in source.splitlines()
    )


def _steps(path: Path | None = None) -> list[tuple[str, str, set[str]]]:
    """``(step name, shell source, bound names)`` for every bash step.

    A step whose ``run:`` feeds a Python heredoc is skipped: its shell is one
    line long and its variables are Python's, which this guard cannot speak
    about.  ``test_acquire_submodule_divergences.py`` is what runs that code.
    """
    found = []
    for job, spec in _workflow(path)["jobs"].items():
        for step in spec.get("steps") or []:
            run = step.get("run")
            if not isinstance(run, str) or "python3 - <<'PY'" in run:
                continue
            source = _script(run)
            bound = set(ASSIGNMENT.findall(source))
            bound |= set(re.findall(r"^\s*for\s+([A-Za-z_]\w*)\s+in\b", source, re.M))
            bound |= set(re.findall(r"\bread\s+(?:-\w+\s+)*([A-Za-z_]\w*)", source))
            bound |= set((step.get("env") or {}).keys())
            found.append((step.get("name") or f"{job}/<unnamed>", source, bound))
    return found


def _unbound(path: Path | None = None) -> dict[str, set[str]]:
    """``{step name: variables it expands and nothing binds}``."""
    unbound: dict[str, set[str]] = {}
    for name, source, bound in _steps(path):
        for match in EXPANSION.finditer(source):
            variable = match.group(1)
            if (
                variable in bound
                or variable in ALWAYS_PRESENT
                or variable.startswith(GITHUB_PREFIXES)
            ):
                continue
            after = source[match.end():]
            # ${VAR:-default}, ${VAR-default}, ${VAR:+alt}, ${VAR+alt},
            # ${VAR:=...}, ${VAR:?...} and ${VAR[...]} are all expansions the
            # script itself makes safe.
            if after[:1] in "-+[" or after[:2] in (":-", ":+", ":=", ":?"):
                continue
            unbound.setdefault(name, set()).add(variable)
    return unbound


def _base_indent(source: str) -> int:
    """The indentation of the script's outermost statements."""
    indents = [
        len(line) - len(line.lstrip(" "))
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return min(indents) if indents else 0


def _conditionally_bound(path: Path | None = None) -> dict[str, set[str]]:
    """``{step name: variables it expands at the top level but only binds inside a block}``.

    The check above asks "is this name assigned anywhere".  This one asks the
    question that actually matters under ``set -u``: is it assigned on a path
    that always runs?  A name whose every assignment sits inside an ``if`` --
    while the name is expanded at the script's own indentation, outside any
    block -- dies the moment that branch is not taken.

    Measured, not theorised: the QemuMetal step first assigned ``trust_cache``
    only inside ``if [ -f "$metal/owner-trust.bundle" ]`` and expanded it at the
    top level, so every run without the Metal half stopped with
    ``trust_cache: unbound variable``.  The guard above passed it, because the
    assignment *is* there -- its own docstring says it does not execute
    anything, and it does not model conditionals either.

    Only the top-level case is reported.  A name assigned and expanded inside
    the same block is fine, and reporting it would make this check noisy enough
    to be ignored -- which is how a guard stops guarding.
    """
    found: dict[str, set[str]] = {}
    for name, source, _bound in _steps(path):
        base = _base_indent(source)
        top_level_assignments: set[str] = set()
        nested_assignments: set[str] = set()
        for line in source.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            matches = ASSIGNMENT.findall(line)
            if not matches:
                continue
            target = (
                top_level_assignments
                if (len(line) - len(line.lstrip(" "))) <= base
                else nested_assignments
            )
            target.update(matches)
        # A name is safe if it is assigned at the top level, or only ever
        # assigned and expanded inside the same block -- the latter is detected
        # by the expansion being deeper than the base indent too.
        expanded_at_top: set[str] = set()
        for line in source.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if (len(line) - len(line.lstrip(" "))) > base:
                continue
            expanded_at_top.update(EXPANSION.findall(line))
        risky = {
            variable
            for variable in expanded_at_top
            if variable in nested_assignments
            and variable not in top_level_assignments
            and variable not in ALWAYS_PRESENT
            and not variable.startswith(GITHUB_PREFIXES)
        }
        if risky:
            found[name] = risky
    return found


def test_no_step_binds_a_top_level_variable_only_inside_a_block() -> None:
    """The conditional half of the guard, against the workflow as committed.

    ``test_every_variable_a_step_expands_is_bound`` cannot see this: it finds
    the assignment inside the ``if`` and is satisfied.  This is the check that
    would have stopped the QemuMetal step's ``trust_cache`` before a runner
    minute was spent.
    """
    conditional = _conditionally_bound()
    assert not conditional, (
        "these steps expand a variable at the top level but only assign it "
        "inside a block, so `set -u` stops them on every run that does not take "
        "that branch (measured with trust_cache in the QemuMetal step):\n"
        + "\n".join(
            f"  {step}: {sorted(names)}" for step, names in sorted(conditional.items())
        )
    )


def test_the_conditional_half_has_teeth(tmp_path: Path) -> None:
    """A binding moved inside an ``if`` is reported.

    The same discipline as the guard above: a check nobody has seen go red is
    not a check.  The mutation is the exact one that reached ``main`` -- the
    default assignment deleted, the override left inside the branch.
    """
    source = WORKFLOW.read_text(encoding="utf-8")
    default = (
        '          trust_cache="$root_tree/build/original-static-system-trust/'
        'original-static-system.bundle"\n'
    )
    assert default in source, (
        "the anchor is gone; the QemuMetal step no longer assigns trust_cache "
        "unconditionally, so this mutation no longer describes it"
    )
    broken = source.replace(default, "", 1)
    assert broken != source
    mutated = tmp_path / "build-macos.yml"
    mutated.write_text(broken, encoding="utf-8")
    conditional = _conditionally_bound(mutated)
    assert conditional, "the conditional check did not report the moved binding"
    assert any(
        "trust_cache" in names for names in conditional.values()
    ), conditional


def test_every_variable_a_step_expands_is_bound() -> None:
    """The guard itself, against the workflow as committed."""
    unbound = _unbound()
    assert not unbound, (
        "these steps expand variables nothing binds, so `set -euo pipefail` "
        "will stop them at runtime after the paid minutes are spent "
        "(measured on run 40201825941 with LOCK_PATH):\n"
        + "\n".join(
            f"  {step}: {sorted(names)}" for step, names in sorted(unbound.items())
        )
    )


def test_the_guard_has_teeth(tmp_path: Path) -> None:
    """A step that loses a binding is reported, not waved through.

    Neither state of the first two drafts proved anything: one flagged ten steps
    full of real assignments, the other read half the file and reported clean.
    So the guard is driven with the exact defect the runner hit -- the chain
    step's ``LOCK_PATH`` binding taken away -- and the edited file is handed to
    the same code path.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    binding = "          LOCK_PATH: config/sources.lock.json\n"
    assert binding in text, (
        "the workflow no longer pins LOCK_PATH anywhere, so this test has "
        "nothing to take away"
    )
    # Remove the CHAIN STEP's binding, not the first one in the file: four steps
    # declare LOCK_PATH and `str.replace(..., 1)` would take the "Validate
    # inputs" one, which is a different step's variable and proves nothing.  The
    # first draft of this test did exactly that and reported a clean file.
    marker = "      - name: Build the owner root chain (build-os-root.py)"
    head, separator, tail = text.partition(marker)
    assert separator, "the workflow has no chain step to take the binding from"
    assert binding in tail, "the chain step no longer binds LOCK_PATH"
    scratch = tmp_path / "build-macos.yml"
    scratch.write_text(head + separator + tail.replace(binding, "", 1), encoding="utf-8")

    unbound = _unbound(scratch)

    assert any("LOCK_PATH" in names for names in unbound.values()), (
        "removing the chain step's LOCK_PATH binding was not reported: "
        f"{ {step: sorted(names) for step, names in unbound.items()} }"
    )
    # ...and the file as committed must not carry the same complaint, or the
    # assertion above is passing for the wrong reason.
    assert not any(
        "LOCK_PATH" in names for names in _unbound().values()
    ), "the committed workflow reports LOCK_PATH as unbound; the fix is not in place"
