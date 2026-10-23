"""Parse every Python heredoc every workflow ships.

Why this file exists
--------------------
The macOS workflow carries its logic in ``python3 - <<'PY'`` blocks embedded in
YAML.  YAML parses, ``bash -n`` passes, and the workflow looks healthy -- while
one of those blocks is a Python file that cannot be compiled at all.  That is not
hypothetical: the "Step summary" step shipped with

    lines.append(
        f"- WARNING: ..."
        f"pins ..."
    )
        "",
    ]

where the call's closing ``)`` had become ``""`` and a stray ``]``.  Python
rejects it with ``SyntaxError: unmatched ']'``.  The step is ``if: always()``, so
it would have crashed on *every* run of the workflow, including the failed ones
where the summary matters most -- and nothing in the suite would have noticed,
because no test compiled the block.

This is the same broken window ``test_shell_script_syntax.py`` closed for shell:
a class of defect that needs no macOS host, no QEMU and no network to find, found
by the cheapest possible runner.

What is proven here
-------------------
That every embedded block is syntactically valid Python.  That is deliberately
modest.  It does not prove the block *does* the right thing -- that is what
``test_p0p1_gate_main.py`` does by extracting a step's code and running it.  A
block that compiles and is still wrong passes here and must fail there.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"

pytestmark = pytest.mark.acceptance

#: ``python3 - <<'PY'`` / ``python - <<'PY'``.  The quoted delimiter matters: an
#: unquoted one would let the shell expand ``${{ ... }}`` before Python saw it,
#: which is a different (and worse) bug this test cannot catch.
_HEREDOC = re.compile(
    r"^[ \t]*python3?(?:[ \t]+-)?[ \t]+-?[ \t]*<<[ \t]*'(PY|PYTHON)'[ \t]*$",
    re.MULTILINE,
)


def _workflows() -> list[Path]:
    if not WORKFLOW_DIR.is_dir():  # pragma: no cover - the repo always ships these
        return []
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def _blocks(path: Path) -> list[tuple[int, str]]:
    """Every ``<<'PY'`` block in a workflow as ``(start_line, source)``.

    The body ends at the first line whose entire content is the delimiter.  That
    is how the shell reads it too, so a block that runs is a block this extracts.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if not _HEREDOC.match(line):
            continue
        delimiter = _HEREDOC.match(line).group(1)  # type: ignore[union-attr]
        body: list[str] = []
        for end in range(index + 1, len(lines)):
            if lines[end].strip() == delimiter:
                break
            body.append(lines[end])
        else:  # pragma: no cover - an unterminated heredoc is a YAML/bash problem
            continue
        found.append((index + 1, textwrap.dedent("\n".join(body))))
    return found


WORKFLOWS = tuple(_workflows())
#: ``(workflow, start_line, source)`` per block.  The start line is the line of
#: the ``<<'PY'`` itself, so a failure points at the shell line that begins it.
BLOCKS: list[tuple[Path, int, str]] = [
    (path, start, body) for path in WORKFLOWS for start, body in _blocks(path)
]


def test_the_repository_ships_workflows_with_python_in_them() -> None:
    """A guard that covers nothing must fail, not silently pass.

    If the glob or the heredoc pattern stops matching, every parametrised case
    below disappears and the file reports "no tests ran" -- which reads as
    healthy.  This makes the empty case loud.
    """
    assert WORKFLOWS, f"no workflow files under {WORKFLOW_DIR}"
    assert BLOCKS, (
        f"no python heredocs found in {len(WORKFLOWS)} workflow(s): the pattern "
        "stopped matching, so this file would cover nothing"
    )


@pytest.mark.parametrize(
    ("path", "start", "body"),
    BLOCKS,
    ids=[f"{path.name}:{start}" for path, start, _body in BLOCKS],
)
def test_the_embedded_python_compiles(path: Path, start: int, body: str) -> None:
    """``ast.parse`` must accept it: a block that does not compile cannot run."""
    relative = path.relative_to(ROOT).as_posix()
    try:
        ast.parse(body, filename=f"{relative} (heredoc at line {start})")
    except SyntaxError as error:
        pytest.fail(
            f"{relative} has a Python heredoc starting at line {start} that does "
            f"not compile: {error.msg} (line {error.lineno} of the block).\n"
            "The step is YAML-valid and `bash -n`-clean, so nothing else in the "
            "suite sees this. The offending block:\n" + _excerpt(body, error.lineno)
        )


def _excerpt(body: str, lineno: int | None) -> str:
    lines = body.splitlines()
    if lineno is None:
        return "\n".join(f"  {i + 1:4}: {line}" for i, line in enumerate(lines))
    centre = max(0, lineno - 1)
    window = range(max(0, centre - 4), min(len(lines), centre + 3))
    return "\n".join(
        f"{'>>' if i == centre else '  '} {i + 1:4}: {lines[i]}" for i in window
    )
