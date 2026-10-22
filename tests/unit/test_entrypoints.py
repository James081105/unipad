"""Entry-point regression tests (PRD 9.1).

The console script ``unipad``, ``python -m unipad`` and
``python -m unipad.controller`` are three documented spellings of one command
table.  ``README.md`` documents the top-level ``python -m unipad`` form, which
is the only one that works on a host where the package cannot be pip-installed
(the editable import is provided by a ``.pth`` file).  A missing
``unipad/__main__.py`` makes that documented form fail while the other two keep
working, so these tests exercise the real interpreter rather than importing the
function: the failure mode is a missing module, not wrong logic inside it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from unipad import __main__ as top_level_main
from unipad.controller import cli
from unipad.controller import __main__ as controller_main

#: The nine commands of the controller contract (PRD 9.1).
NINE_COMMANDS: tuple[str, ...] = (
    "doctor",
    "build",
    "fetch",
    "start",
    "install",
    "verify",
    "stop",
    "collect",
    "restore",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_module(module: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python -m <module> <args>`` from the repository root."""
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


def test_every_entry_point_resolves_to_the_one_command_table() -> None:
    """All three spellings must reach the same function (DRY, PRD 9.1).

    A second command table would drift: one spelling would gain a command the
    others lack, and scripts would break depending on how they were invoked.
    """
    assert top_level_main.main is cli.main
    assert controller_main.main is cli.main


@pytest.mark.parametrize("module", ["unipad", "unipad.controller"])
def test_module_invocation_lists_the_nine_commands(module: str) -> None:
    """``python -m <module> --help`` succeeds and advertises all nine commands."""
    result = _run_module(module, "--help")
    assert result.returncode == 0, f"{module} --help failed:\n{result.stderr}"
    for command in NINE_COMMANDS:
        assert command in result.stdout, f"{module} --help is missing '{command}'"


def test_documented_top_level_invocation_answers_a_real_command() -> None:
    """The README's ``python -m unipad doctor`` form must actually run.

    ``--help`` only proves argparse built the table; this proves the delegation
    chain executes and the command returns the contract's success exit code.
    """
    result = _run_module("unipad", "doctor")
    assert result.returncode == 0, f"unipad doctor failed:\n{result.stderr}"
    assert '"command": "doctor"' in result.stdout, "doctor must print its JSON envelope"
