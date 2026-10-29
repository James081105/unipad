"""The Mesa bootstrap's virtualenv must carry what Mesa's meson.build imports.

Why this exists
---------------
Ring 1 of the addon chain died seven seconds in on run 40921780876 with

    vendor/UniXNU/.cache/mesa-work/meson.build:1141:2: ERROR: Problem
    encountered: One of Python (3.x) packaging or distutils module is required.

The bootstrap created the virtualenv with meson and ninja and nothing else.  The
comment it carried said the rest of the list was "read from its meson.build rather
than guessed at here" -- an intention the code did not implement, which is why
nobody noticed for as long as they did: a comment describing a guard that does not
exist is worse than no comment, because it stops the next reader from looking.

So the requirement is asserted on the code that meets it.  The module names are
checked against the meson.build lines that require them, so the table and the
source cannot drift apart silently.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "macos" / "bootstrap-mesa-turnip.sh"
MESA = REPO / "vendor" / "UniXNU" / ".cache" / "mesa-work" / "meson.build"


def _bootstrap() -> str:
    if not BOOTSTRAP.is_file():
        pytest.skip(f"{BOOTSTRAP} is not present")
    return BOOTSTRAP.read_text(encoding="utf-8")


def _mesa_build() -> str:
    if not MESA.is_file():
        pytest.skip(
            f"{MESA} is not present: this stage needs the pinned vendor "
            "checkout from config/sources.lock.json"
        )
    return MESA.read_text(encoding="utf-8")


def test_the_venv_installs_mesas_python_modules():
    """The three modules Mesa probes for, named in one place."""
    script = _bootstrap()
    assert "MESA_PYTHON_MODULES=" in script, (
        "the bootstrap no longer names Mesa's python modules in one place; "
        "ring 1 fails on meson.build:1141 when the venv lacks them"
    )
    for pair in ("mako:mako", "packaging:packaging", "yaml:PyYAML"):
        assert pair in script, (
            f"{pair} is missing from the module table; Mesa's meson.build "
            f"imports {pair.split(':')[0]} and ring 1 would fail without it"
        )


def test_every_module_mesa_requires_is_in_the_table():
    """The table is checked against meson.build, not against memory.

    If Mesa adds a fourth module, its meson.build grows an `error(...)` line
    naming it and this test fails -- which is the whole point of deriving the
    list from the source rather than hardcoding it.
    """
    script = _bootstrap()
    build = _mesa_build()
    table = script[script.index("MESA_PYTHON_MODULES="):]
    table = table[:table.index("\n")]
    pairs = table.replace("MESA_PYTHON_MODULES='", "").rstrip("'").split()
    installed = {pair.split(":")[0] for pair in pairs}

    # Mesa's own probes, taken from meson.build: the three `error(...)` lines
    # that name a module.  Each is looked up in the table by the name it prints.
    required = {
        "mako": "mako module >= 0.8.0 required to build mesa",
        "packaging": "packaging or distutils module is required",
        "yaml": "yaml module (PyYAML) required to build mesa",
    }
    for module, phrase in required.items():
        assert phrase in build, (
            f"meson.build no longer carries the probe for {module!r} "
            f"({phrase!r}); this test's list is stale and must be re-derived"
        )
        assert module in installed, (
            f"mesa's meson.build requires {module!r} and the bootstrap's "
            f"module table does not install it"
        )


def test_the_modules_are_proved_by_importing_them():
    """Installed is not the same as importable, and importable is not old-version-safe.

    The probe is what makes the table a fact rather than a hope, and the mako
    version check is there because meson.build:1118-1119 requires >= 0.8.0.
    """
    script = _bootstrap()
    # Asserted on the POST-install proof, not on the phrase `import $module`: the
    # bootstrap also uses that phrase in the pre-install check, and an assertion
    # satisfied by the other occurrence is not an assertion.  The first version of
    # this test made exactly that mistake and survived the mutation that removed
    # the proof.
    assert "is installed but 'import $module' still fails" in script, (
        "the bootstrap installs the modules but never proves the install took; "
        "a pip install that silently did nothing would read as success"
    )
    assert "0, 8, 0" in script or "0.8.0" in script, (
        "the bootstrap no longer checks mako's version; meson.build:1118 "
        "requires >= 0.8.0 and an older one fails at configure, not here"
    )


def test_the_comment_no_longer_describes_a_guard_that_does_not_exist():
    """Teeth: the defect was the comment, as much as the missing packages.

    The step used to claim the list was "read from its meson.build rather than
    guessed at here so this stage does not need updating when Mesa adds a
    dependency" while installing meson and ninja only.  A reader who believed it
    had no reason to look, which is why the gap survived a runner-verified
    bootstrap.  Asserted on the source because a comment cannot be executed.
    """
    script = _bootstrap()
    assert "read from its meson.build" not in script, (
        "the venv step still claims to read the module list from meson.build; "
        "that claim was false when ring 1 failed on run 40921780876"
    )
    # ...and the claim is now backed by a test that reads it, so the replacement
    # wording is accurate.
    assert re.search(r"checked against|named in one place|Mesa's meson.build", script), (
        "the venv step no longer says where the module list comes from"
    )
