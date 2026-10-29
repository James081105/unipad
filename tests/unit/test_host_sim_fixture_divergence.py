"""The host-simulation fixture divergence: what it changes, and what it must not.

Why this exists
---------------
Ring 2 of the addon chain compiles the real driver sources against the host
simulation's stub IOKit headers.  On run 40932083150 that failed with two errors,
both in ``NativeAdrenoMemory.cpp``::

    167:70: error: unknown type name 'IODirection'
    170:90: error: use of undeclared identifier 'kIOMemoryPersistent'

``XnuMemoryFixture.h`` declares a hand-picked subset of the IOKit vocabulary and
is missing both.  On a real build ``NativeAdrenoMemory.h:4`` includes
``<IOKit/IOTypes.h>``, which provides them -- so this is upstream bit-rot in a
TEST FIXTURE, not in the shipped driver.

The fix is applied by ``build-gpu-addon.sh`` as a recorded divergence rather than
edited into the pinned checkout, because the acquisition step resets vendor/ to
its pin every run.  These tests pin the two properties that make that divergence
safe: the value it adds is inert in the fixture, and it refuses rather than
mis-applying when the upstream file changes.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "macos" / "build-gpu-addon.sh"
FIXTURE = REPO / "vendor" / "UniXNU" / "drivers" / "NativeAdreno" / "tests" / "XnuMemoryFixture.h"


def _editor() -> str:
    """The Python the script runs to apply the fix, extracted verbatim.

    Extracted rather than reimplemented so the test exercises the same code the
    runner does; a second copy would be a second place for the two to drift.
    """
    if not SCRIPT.is_file():
        pytest.skip(f"{SCRIPT} is not present")
    text = SCRIPT.read_text(encoding="utf-8")
    if '<<\'PY\' || die "cannot apply the host-simulation fixture fix' not in text:
        pytest.skip("build-gpu-addon.sh no longer carries the fixture fix")
    start = text.index('    "$PY" - "$FIXTURE" <<\'PY\' || die')
    body = text[start:]
    return body[body.index("\n") + 1: body.index("\nPY\n")]


def _fixture_text() -> str:
    if not FIXTURE.is_file():
        pytest.skip(f"{FIXTURE} is not present; the pinned vendor checkout is absent")
    return FIXTURE.read_text(encoding="utf-8")


def test_the_script_records_the_divergence_rather_than_editing_the_tree_silently():
    """A build that quietly patches its own inputs is a build nobody can reproduce."""
    if not SCRIPT.is_file():
        pytest.skip(f"{SCRIPT} is not present")
    text = SCRIPT.read_text(encoding="utf-8")
    # Asserted on the CONFIRMATION line, not on the word: the script says
    # "DIVERGENCE" three times (the intent, the applied line, the rationale), and
    # an assertion satisfied by any of them is not an assertion.  The teeth check
    # that removed the applied line walked straight past the first version.
    assert "DIVERGENCE applied:" in text, (
        "build-gpu-addon.sh no longer confirms, in its log, that the fixture "
        "change was applied; a reader of the build log could not tell it happened"
    )
    assert "XnuMemoryFixture.h" in text, (
        "the fixture fix no longer names the file it changes"
    )
    # And it must be conditional on the gap still being there, so a runner whose
    # upstream has already fixed it does not get a second, conflicting copy.
    assert 'grep -q "kIOMemoryPersistent" "$FIXTURE"' in text, (
        "the fixture fix is no longer guarded by the gap's presence; it would "
        "double-apply on a tree that already carries the constant"
    )


def test_the_added_value_is_inert_in_the_fixture(tmp_path):
    """Teeth: the constant must not collide with a bit the fixture actually tests.

    ``withAddressRange`` branches on ``kIOMemoryMapperNone`` (0x800) and
    ``kIOMemoryPhysicallyContiguous`` (0x10).  A ``kIOMemoryPersistent`` sharing a
    bit with either would change the simulation's behaviour -- silently, which is
    the one outcome worse than the compile error it fixes.
    """
    text = _fixture_text()
    editor = _editor()
    work = tmp_path / "XnuMemoryFixture.h"
    work.write_text(text, encoding="utf-8")
    runner = tmp_path / "apply.py"
    runner.write_text(editor, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(runner), str(work)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-800:]
    after = work.read_text(encoding="utf-8")

    assert after.count("using IODirection=unsigned;") == 1, (
        "IODirection is not declared exactly once"
    )
    assert after.count("kIOMemoryPersistent=") == 1, (
        "kIOMemoryPersistent is not declared exactly once"
    )

    def bit_of(name: str) -> int:
        for line in after.splitlines():
            if name + "=" in line:
                # The constants share one constexpr line; take the value that
                # follows this name.
                tail = line.split(name + "=", 1)[1]
                digits = ""
                for ch in tail:
                    if ch.isdigit() or ch in "xXabcdefABCDEF":
                        digits += ch
                    else:
                        break
                return int(digits, 0)
        raise AssertionError(f"{name} is not declared in the patched fixture")

    persistent = bit_of("kIOMemoryPersistent")
    for tested in ("kIOMemoryMapperNone", "kIOMemoryPhysicallyContiguous"):
        assert persistent & bit_of(tested) == 0, (
            f"kIOMemoryPersistent={persistent:#x} shares a bit with {tested}="
            f"{bit_of(tested):#x}; the simulation's branches would change"
        )


def test_the_editor_refuses_when_the_upstream_file_has_changed(tmp_path):
    """Fail closed: an anchor that no longer matches is a refusal, not a guess.

    The alternative is a fix that applies to a file it has never seen, which is
    how a two-declaration change becomes a corrupted header.
    """
    text = _fixture_text()
    editor = _editor()
    work = tmp_path / "XnuMemoryFixture.h"
    # The anchor line, reworded: exactly what an upstream edit looks like.
    work.write_text(
        text.replace("kIOMemoryPhysicallyContiguous=0x10;", "kIOMemoryPhysicallyContiguous=0x20;", 1),
        encoding="utf-8",
    )
    runner = tmp_path / "apply.py"
    runner.write_text(editor, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(runner), str(work)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0, (
        f"the editor applied to a fixture whose anchor line had changed: "
        f"{(result.stdout + result.stderr)[-400:]!r}"
    )
    assert "re-derived" in result.stdout + result.stderr, (
        "the refusal does not say the fix must be re-derived"
    )
    # And it left the file alone.
    assert "using IODirection=unsigned;" not in work.read_text(encoding="utf-8"), (
        "the editor modified a file it refused to patch"
    )


def test_the_editor_is_idempotent_in_effect(tmp_path):
    """A second application must not stack the declarations."""
    text = _fixture_text()
    editor = _editor()
    work = tmp_path / "XnuMemoryFixture.h"
    work.write_text(text, encoding="utf-8")
    runner = tmp_path / "apply.py"
    runner.write_text(editor, encoding="utf-8")

    first = subprocess.run([sys.executable, str(runner), str(work)],
                           capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, (first.stdout + first.stderr)[-400:]
    once = work.read_text(encoding="utf-8")
    second = subprocess.run([sys.executable, str(runner), str(work)],
                            capture_output=True, text=True, timeout=120)
    assert second.returncode != 0, "a second application succeeded; it would double-apply"
    assert work.read_text(encoding="utf-8") == once, "a second application changed the file"
