"""Compile every Python heredoc ``build-macos.sh`` embeds.

Why this file exists
--------------------
``scripts/macos/build-macos.sh`` carries real Python inside quoted heredocs --
``"$PY" - "$arg" <<'PY'`` -- and ``bash -n`` cannot see inside one: to the shell
it is text, so a Python file that cannot be parsed passes the shell check, the
YAML check and any test that only greps for a substring.  That is not
hypothetical either.  Measured on run 40238050858, the step that names the
button sink in the generated kernel config died with::

    SyntaxError: unterminated string literal (detected at line 13)

because a ``\\n`` in the heredoc's ``config.write_text("\\n".join(lines) ...)``
had arrived as a real newline.  The stage then recorded ``failed=0`` with a
``SyntaxError`` above it -- the counter said nothing failed and the stage did.

This is the same broken window ``test_workflow_python_syntax.py`` closed for the
workflow's YAML and ``test_shell_script_syntax.py`` closed for shell: a class of
defect that needs no macOS host, no QEMU and no network to find.

What is proven here
-------------------
That every heredoc ``build-macos.sh`` feeds to an interpreter is a Python program
that compiles.  A heredoc that does not is a stage that will crash on the runner
after the paid minutes are spent, with a ``SyntaxError`` no shell check and no
substring assertion can see.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"

pytestmark = pytest.mark.acceptance

#: ``python3 - <<'PY'``, ``"$PY" - <<'PY'``, ``"$PY" - "$arg" <<'PY'`` and the
#: ``python3 - "$arg" <<'PY'`` forms this script actually uses.  The tag is
#: captured so the block can be read up to its own terminator.
HEREDOC = re.compile(r"<<'([A-Za-z_][A-Za-z0-9_]*)'\s*$")
#: The line has to hand the block to a Python interpreter.  Without this the
#: guard also reads the shell usage text (``cat <<'EOF'``) and the button sink's
#: C source (``cat >"$source" <<'STUB'``), neither of which is Python -- and a
#: guard that reports them is a guard nobody trusts.
PYTHON_LINE = re.compile(r'(?:"\$PY"|\$PY|python3)\b[^|]*<<')


def _blocks() -> list[tuple[int, str, str]]:
    """``(line number, tag, source)`` for every Python heredoc in the script."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, str, str]] = []
    index = 0
    while index < len(lines):
        match = HEREDOC.search(lines[index])
        if not match or not PYTHON_LINE.search(lines[index]):
            index += 1
            continue
        tag = match.group(1)
        start = index + 1
        end = next(
            (i for i in range(start, len(lines)) if lines[i].strip() == tag), None
        )
        if end is None:
            pytest.fail(f"{SCRIPT.name}:{start} opens <<'{tag}' and never closes it")
        found.append((start + 1, tag, "\n".join(lines[start:end])))
        index = end + 1
    return found


def test_every_heredoc_the_script_feeds_an_interpreter_compiles() -> None:
    """The guard itself: each block is a Python program that parses.

    ``compile`` rather than ``ast.parse`` so the failure names the line, and
    ``<heredoc>`` as the filename so a traceback says which block and not
    ``<unknown>``.
    """
    blocks = _blocks()
    assert blocks, (
        "no heredoc was found in build-macos.sh, so this guard is not looking "
        "at anything -- the pattern or the script has moved"
    )
    broken = []
    for number, tag, source in blocks:
        try:
            compile(source, f"<{tag}@{number}>", "exec")
        except SyntaxError as failure:
            broken.append(
                f"  {SCRIPT.name}:{number} (<<'{tag}'): "
                f"{failure.msg} at line {failure.lineno}"
            )
    assert not broken, (
        "these heredocs are not Python that compiles, so the stage feeding them "
        "will die with a SyntaxError on the runner -- measured on run "
        "40238050858, where a \\n that arrived as a real newline took the whole "
        "build-kernel stage with it while the ledger counted failed=0:\n"
        + "\n".join(broken)
    )


def test_the_guard_has_teeth(tmp_path: Path) -> None:
    """A heredoc that does not compile is reported, not waved through.

    The defect this file was written for is driven through the same code path: a
    block with the unterminated string literal run 40238050858 actually hit.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    marker = "<<'PYEDITOR'"
    assert marker in text, (
        "build-macos.sh no longer carries the config editor's heredoc, so this "
        "test has nothing to break"
    )
    # Raw strings, because the file holds a backslash and an 'n' as two
    # characters -- which is correct Python source.  A non-raw search string
    # holds a real newline, finds nothing, and the test then reports "nothing to
    # break" instead of breaking anything: the sixth time this session a teeth
    # check has gone red on its own defect rather than on the contract.
    good = r'config.write_text("\n".join(lines) + "\n", encoding="utf-8")'
    bad = 'config.write_text("\n".join(lines) + "\n", encoding="utf-8")'
    assert good in text, (
        "the config editor's write_text line is not spelled the way this test "
        "expects, so the test would pass without breaking anything"
    )
    broken = text.replace(good, bad, 1)
    assert broken != text
    scratch = tmp_path / "build-macos.sh"
    scratch.write_text(broken, encoding="utf-8")

    saved = SCRIPT
    try:
        globals()["SCRIPT"] = scratch
        blocks = _blocks()
        assert blocks, "the edited script yielded no heredoc at all"
        failures = []
        for number, tag, source in blocks:
            try:
                compile(source, f"<{tag}@{number}>", "exec")
            except SyntaxError as failure:
                failures.append(f"{tag}@{number}: {failure.msg}")
    finally:
        globals()["SCRIPT"] = saved
    assert failures, (
        "a heredoc with an unterminated string literal compiled anyway, so the "
        "guard cannot see the defect run 40238050858 hit"
    )
