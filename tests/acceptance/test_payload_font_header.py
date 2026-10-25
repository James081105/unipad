"""The panic screen's font header: pinned, wired, and proven to reach the driver.

Why this file exists
--------------------
The macOS runner's ``build-payload`` stage failed twice with the same ledger
entry -- runs ``40094234507`` (2026-10-24T12:30Z) and ``40096928333``
(13:09Z)::

    payload: FAIL build-output/unixnu-out/qemu/tree/uefi-sheng/Silicon/Silicium/
             SiliciumPkg/Library/FrameBufferSerialPortLib/Font.h is missing

``board.toml [payload] panic_screen = true`` (UniXNU-qemu/board.toml:94) makes
the driver compile the device repository's ``scripts/xnu-panic-screen.c``, whose
one include is ``"Font.h"`` (xnu-panic-screen.c:294) out of a Mu-Silicium
checkout.  ``patch-xnu-entry-probe.py:183`` looks for it at
``(UNIXNU_UEFI or <tree>/uefi-sheng)/Silicon/Silicium/SiliciumPkg/Library/
FrameBufferSerialPortLib`` -- and the assembled tree never contains
``uefi-sheng/`` at all (``tree.py:assemble`` creates only ``drivers/ kits/
patches/ refer/ build/ .cache/ package/ scripts/``).

The checkout itself is public and always was: ``https://github.com/UniXNU/
uefi-sheng`` (default branch ``sheng``, BSD-2-Clause, the Xiaomi Pad 6S Pro
platform integration) carries that exact file.  The comment this failure left
behind called Mu-Silicium "neither a feed nor part of either pinned
repository", which reads as "unavailable" and sent the next reader looking for
a workaround instead of a pin.  The root cause is not availability: it is that
nothing provided the checkout -- the workflow passed neither spelling of the
option, and no repository pinned or fetched it.  So the fix pins it, acquires
it, forwards it and verifies the bytes.

On the two channels, since the first draft of this file got it wrong and a
reviewer caught it: BOTH reach the patcher.  ``cli.py:136`` sets ``ctx.uefi``
from ``ctx.option('uefi', 'UNIXNU_UEFI')``, which reads ``--uefi`` first and the
``UNIXNU_UEFI`` environment variable second (config.py:110-117), and
``payload.py:142`` then runs the patcher with ``UNIXNU_UEFI=str(ctx.uefi)``.
Measured on this host against the real ``config.py``: exporting ``UNIXNU_UEFI``
with no flag gives ``ctx.uefi`` that path and the patcher receives it; giving
neither gives ``ctx.uefi = None`` and the patcher receives ``''``.  This
workflow passes the flag because it lands in the driver's argv.log and
``need_uefi()`` (cli.py:67-73) reads the same option -- not because the
environment spelling is broken.

What is proven here
-------------------
The lock pins the checkout and the font's digest; the workflow's acquisition
step acquires it; the build step forwards it; and -- run for real against the
scratch harness in ``test_p0p1_gate_main.py`` -- the payload stage hands the
driver ``--uefi`` and refuses, by name, when the font is absent or is not the
bytes the lock promised.

What is NOT proven here
-----------------------
That the panic screen DRAWS.  That needs a macOS runner, the real kernel, a
frame buffer and a panic: ``xnu-panic-screen.c`` is compiled by the device
repository's own patcher and linked into the kernel's text cave.  Everything
below is about the font header reaching the compiler and being the right bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

# Imported, not copied: test_p0p1_gate_main owns the "run the real build entry
# point against a scratch repository with a recording driver" machinery, and a
# second copy of it is the divergence class this repository keeps hitting.
from acceptance.test_p0p1_gate_main import (
    ACQUISITION_STEP,
    BUILD_STEP,
    LOCK_PATH,
    _ScratchRepo,
    _load_step,
    _step,
    _step_python,
)

pytestmark = pytest.mark.acceptance

ROOT = Path(__file__).resolve().parents[2]
VENDOR_DEVICE = ROOT / "vendor" / "UniXNU-qemu"

#: Where the font sits inside a Mu-Silicium checkout, and therefore the ``-I``
#: directory ``patch-xnu-entry-probe.py:183`` hands clang.  Asserted against
#: BOTH the lock and the patcher below rather than trusted from either.
FONT_RELATIVE = "Silicon/Silicium/SiliciumPkg/Library/FrameBufferSerialPortLib/Font.h"

#: The lock's own key for the third pinned checkout.
UEFI_KEY = "uefi_sheng"

#: A stand-in for the real 3692-byte glyph table.  It does not have to render:
#: what is under test is that the file reaches the compiler and is the file the
#: lock pinned, so any bytes will do as long as the digest matches.
FONT_STUB = b"/* Font.h stand-in for the scratch Mu-Silicium checkout */\n"

#: Other bytes at the same path, for the digest-mismatch case.
OTHER_FONT = b"/* someone else's glyph table */\n"


# ---------------------------------------------------------------------------
# 1. The lock pins the source, and agrees with the patcher about where it is
# ---------------------------------------------------------------------------


def _real_lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def test_the_lock_pins_the_mu_silicium_checkout_the_panic_screen_needs() -> None:
    """The font has a pin, a commit and a digest -- or the run is not reproducible.

    Before this, ``config/sources.lock.json`` named two repositories and the
    payload stage needed a third.  A pin is the only thing that stops a build
    from silently using whatever the branch happened to point at, so the shape
    asserted here is the whole fix on the lock side.
    """
    lock = _real_lock()
    assert UEFI_KEY in lock, (
        f"{LOCK_PATH.name} has no {UEFI_KEY!r} pin: the panic screen's font "
        "header has no pinned source, which is the run 40094234507 failure"
    )
    pin = lock[UEFI_KEY]
    assert pin["repo"] == "https://github.com/UniXNU/uefi-sheng", pin
    assert re.fullmatch(r"[0-9a-f]{40}", pin["commit"]), pin
    # A branch name is not a pin; the ref is recorded for a human and the commit
    # is what is fetched.
    assert pin.get("ref") == "sheng", pin

    font = pin["font_header"]
    assert font["path"] == FONT_RELATIVE, font
    assert re.fullmatch(r"[0-9a-f]{64}", font["sha256"]), font
    assert isinstance(font.get("bytes"), int) and font["bytes"] > 0, font
    # Attribution is part of the pin: the file is someone else's copyrighted
    # work and the lock must say whose (goal doc 4.4, "核对许可与署名").
    assert "Android Open Source Project" in font.get("copyright", ""), font
    assert "BSD-2-Clause" in pin.get("license", ""), pin


def test_the_environment_spelling_of_uefi_also_reaches_the_patcher(tmp_path: Path) -> None:
    """``export UNIXNU_UEFI=DIR`` works, and the build script must not claim otherwise.

    This test exists because the first draft of this fix claimed the opposite --
    that ``payload.py:142``'s ``env=dict(os.environ, UNIXNU_UEFI=str(ctx.uefi or
    ''))`` discards an exported value.  It does not: the driver *reads* the
    variable first (``cli.py:136`` -> ``config.py`` ``Context.option``) and then
    forwards what it read, so an exported value survives.  Only an unset one
    arrives as ``''``.

    Proven here by running the real ``config.py`` rather than by reading it,
    because a comment asserting a fact about someone else's code is exactly the
    kind of claim nobody re-checks -- and this one was wrong.
    """
    driver = VENDOR_DEVICE.parent / "UniXNU" / "scripts" / "unixnu" / "config.py"
    if not driver.is_file():  # pragma: no cover - vendor/ is fetched material
        pytest.skip(f"the pinned UniXNU checkout is not present at {driver.parent}")

    package = tmp_path / "unixnu"
    package.mkdir()
    for name in ("__init__.py", "config.py", "util.py"):
        source = driver.parent / name
        if source.is_file():
            (package / name).write_bytes(source.read_bytes())
    device = tmp_path / "device"
    device.mkdir()
    (device / "board.toml").write_text('[board]\nname = "probe"\n', encoding="utf-8")
    # Bound before the class body: a name assigned inside a class body is a
    # class-local, so `device = device.as_posix()` there would read the
    # not-yet-bound local and raise NameError.
    device_dir = device.as_posix()
    out_dir = (tmp_path / "out").as_posix()

    class _Args:
        uefi = None
        device = device_dir
        out = out_dir
        jobs = 1

    saved = os.environ.copy()
    sys.path.insert(0, str(tmp_path))
    try:
        for key in ("UNIXNU_UEFI", "UNIXNU_DEVICE"):
            os.environ.pop(key, None)
        from unixnu.config import Context

        # (1) the environment spelling: ctx.uefi picks it up, and the child
        #     environment payload.py:142 builds carries it, not ''.
        os.environ["UNIXNU_UEFI"] = (tmp_path / "mu-silicium").as_posix()
        ctx = Context(_Args())
        ctx.uefi = ctx.option("uefi", "UNIXNU_UEFI")
        assert ctx.uefi is not None, "an exported UNIXNU_UEFI did not reach ctx.uefi"
        child = dict(os.environ, UNIXNU_UEFI=str(ctx.uefi or ""))["UNIXNU_UEFI"]
        assert child == str(ctx.uefi), child

        # (2) neither spelling: ctx.uefi is None and the patcher would see '' --
        #     which is why <tree>/uefi-sheng is what it then falls back to.
        os.environ.pop("UNIXNU_UEFI")
        bare = Context(_Args())
        assert bare.option("uefi", "UNIXNU_UEFI") is None
        assert dict(os.environ, UNIXNU_UEFI=str(bare.option("uefi", "UNIXNU_UEFI") or ""))[
            "UNIXNU_UEFI"
        ] == ""
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("unixnu", None)
        sys.modules.pop("unixnu.config", None)
        sys.modules.pop("unixnu.util", None)
        os.environ.clear()
        os.environ.update(saved)


def test_the_locked_font_path_is_the_one_the_patcher_actually_uses() -> None:
    """Two spellings of the font's location, and they must agree.

    The lock's ``uefi_sheng.font_header.path`` and the literal in
    ``patch-xnu-entry-probe.py:183`` are the same path from two sides.  If they
    ever disagree one of them has moved, and a build that follows the lock while
    the compiler follows the patcher fails exactly the way this file is about --
    so the disagreement deserves a witness of its own.
    """
    patcher = VENDOR_DEVICE / "scripts" / "patch-xnu-entry-probe.py"
    if not patcher.is_file():  # pragma: no cover - vendor/ is fetched material
        pytest.skip(f"the pinned device repository is not checked out at {VENDOR_DEVICE}")

    source = patcher.read_text(encoding="utf-8")
    # The literal, verbatim, so this test breaks when upstream moves the font
    # rather than quietly following a stale lock.
    assert "'Silicon/Silicium/SiliciumPkg/Library/FrameBufferSerialPortLib'" in source, (
        "patch-xnu-entry-probe.py no longer spells the font directory this "
        "repository pins; re-read the patcher and re-pin uefi_sheng.font_header"
    )
    # ...and the source really does take it from UNIXNU_UEFI with a fallback,
    # which is the two-channel shape build-macos.sh documents.
    assert "os.environ.get('UNIXNU_UEFI')" in source, source

    locked = _real_lock()[UEFI_KEY]["font_header"]["path"]
    assert locked.endswith("Font.h"), locked
    assert locked == FONT_RELATIVE, (
        f"the lock pins the font at {locked!r} but the patcher's include "
        f"directory is {FONT_RELATIVE.rsplit('/', 1)[0]!r}"
    )


# ---------------------------------------------------------------------------
# 2. The workflow acquires the third checkout and forwards it
# ---------------------------------------------------------------------------


def test_the_acquisition_step_acquires_all_three_pinned_checkouts(tmp_path: Path) -> None:
    """The step's own Python, loaded and asked what it would acquire.

    ``ACQUISITIONS`` is the tuple that drives both the clone and the
    cross-check, so a third pin that is not in it is a pin nothing fetches.
    Reading the constant out of the step's extracted source rather than
    grepping the YAML means a dropped entry fails here.
    """
    module = _load_step(ACQUISITION_STEP, tmp_path)
    directories = {directory for _key, directory in module.ACQUISITIONS}
    assert directories == {"UniXNU", "UniXNU-qemu", "uefi-sheng"}, sorted(module.ACQUISITIONS)

    # ...and the lock really does carry a pin for every key the tuple names,
    # because load_pins() refuses a key the lock does not have.
    lock = _real_lock()
    for key, _directory in module.ACQUISITIONS:
        assert key in lock, f"{ACQUISITION_STEP} acquires {key!r} but the lock does not pin it"


def test_the_acquisition_step_publishes_the_third_pin_as_an_output() -> None:
    """The build step must be able to name what was fetched, not what was asked for."""
    source = _step_python(ACQUISITION_STEP)
    assert "uefi_sheng_commit=" in source, (
        "the acquisition step does not publish the uefi_sheng pin, so nothing "
        "downstream can attest to the commit that was actually fetched"
    )


def test_the_build_step_forwards_the_checkout_as_uefi() -> None:
    """The workflow hands the build script the checkout it just acquired.

    Asserted on the step's shell text because the step *is* the whole job
    (acquisition, IPSW, brew, staging) and cannot be run in isolation here.
    What matters is the wiring: the flag is built, it names the directory the
    acquisition step writes, and it reaches the invocation rather than only a
    log line.  ``--uefi`` rather than ``UNIXNU_UEFI`` is a preference, not a
    requirement -- both reach the patcher (see
    :func:`test_the_environment_spelling_of_uefi_also_reaches_the_patcher`).
    """
    run = _step(BUILD_STEP)["run"]
    assert "uefi_args+=(--uefi vendor/uefi-sheng)" in run, (
        "the build step does not add --uefi vendor/uefi-sheng, so the panic "
        "screen's font header has no source (runs 40094234507, 40096928333)"
    )
    assert '${uefi_args[@]+"${uefi_args[@]}"}' in run, "--uefi is built but never handed to build-macos.sh"
    # All three checkouts are named on the same command line, so the directory
    # the acquisition step writes and the one the build reads cannot drift.
    assert "--unixnu vendor/UniXNU" in run and "--device vendor/UniXNU-qemu" in run, run
    assert "--uefi vendor/uefi-sheng" in run, (
        "the build step's --uefi does not name the directory the acquisition "
        "step writes (vendor/uefi-sheng)"
    )


# ---------------------------------------------------------------------------
# 3. The payload stage, run for real against the scratch harness
# ---------------------------------------------------------------------------


def _panic_board() -> str:
    """The scratch board plus the one switch the real board has: panic_screen.

    Imported from the harness module rather than restated, so the scratch board
    stays the pinned board's shape and only the switch under test differs.
    """
    import acceptance.test_p0p1_gate_main as harness

    board = harness._SCRATCH_BOARD_TOML
    assert "panic_screen" not in board, board
    patched = board.replace(
        '[payload]\nloader = "driver"',
        '[payload]\nloader = "driver"\npanic_screen = true',
    )
    assert patched != board, "the scratch board no longer spells [payload] the way this test expects"
    return patched


def _pin_uefi(repo: _ScratchRepo, font_bytes: bytes, *, path: str = FONT_RELATIVE) -> str:
    """Add the uefi_sheng pin to the scratch lock; returns the font's digest."""
    digest = hashlib.sha256(font_bytes).hexdigest()
    lock_path = repo.repo / "config" / "sources.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock[UEFI_KEY] = {
        "repo": "https://example.invalid/uefi-sheng",
        "ref": "sheng",
        "commit": "d" * 40,
        "license": "BSD-2-Clause",
        "font_header": {
            "path": path,
            "sha256": digest,
            "bytes": len(font_bytes),
            "copyright": "Copyright (C) 2008, The Android Open Source Project",
        },
    }
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return digest


def _mu_silicium(root: Path, font_bytes: bytes) -> Path:
    """A scratch Mu-Silicium checkout: the font, plus enough to look like one."""
    checkout = root / "vendor" / "uefi-sheng"
    font = checkout / FONT_RELATIVE
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(font_bytes)
    # need_uefi() (cli.py:67-73) validates exactly this file, so a checkout
    # without it is not one -- and step_assemble_image refuses the whole run.
    (checkout / "Resources" / "Scripts").mkdir(parents=True, exist_ok=True)
    (checkout / "Resources" / "Scripts" / "mkbootimg.py").write_text("# scratch\n", encoding="utf-8")
    return checkout


def _tools_shimmed(repo: _ScratchRepo) -> None:
    """qemu-system-aarch64 and mtools, so the payload stage reaches the driver.

    The font check sits BEFORE that gate, so the absent-font test does not need
    them; the tests that assert what the driver was handed do, and all of them
    must run through the same fixture.
    """
    import acceptance.test_p0p1_gate_main as harness

    for name in ("qemu-system-aarch64", "mformat", "mcopy"):
        harness._shim(repo.darwindir / name, "#!/bin/sh\nexit 0\n")


@pytest.fixture
def panic_repo(tmp_path_factory, monkeypatch) -> _ScratchRepo:
    """A scratch repository whose board asks for the panic screen.

    Built by the same ``_ScratchRepo`` the section-6 tests use, with the board
    constant patched for the duration: the harness is hundreds of lines of fake
    driver, and a second copy of it would be the divergence class this
    repository keeps hitting.
    """
    import acceptance.test_p0p1_gate_main as harness

    monkeypatch.setattr(harness, "_SCRATCH_BOARD_TOML", _panic_board())
    repo = harness._ScratchRepo(tmp_path_factory.mktemp("payload-font")).build(frozen=False)
    _tools_shimmed(repo)
    return repo


@pytest.fixture
def plain_repo(tmp_path_factory) -> _ScratchRepo:
    """The control: the same harness with the scratch board's own [payload].

    No ``panic_screen``, exactly like a board that never asked for a panic
    screen.  Without it the font gate could be satisfied by demanding a
    Mu-Silicium checkout of every payload build.
    """
    import acceptance.test_p0p1_gate_main as harness

    repo = harness._ScratchRepo(tmp_path_factory.mktemp("payload-plain")).build(frozen=False)
    _tools_shimmed(repo)
    return repo


def _payload_step(repo: _ScratchRepo) -> dict:
    return repo.steps()["build-payload"]


def _payload_calls(repo: _ScratchRepo) -> list[list[str]]:
    return [call for call in repo.argv_log() if call[0] == "build" and "payload" in call]


def test_the_payload_stage_fails_naming_the_font_when_no_uefi_is_given(
    panic_repo: _ScratchRepo, tmp_path: Path
) -> None:
    """The CI failure, reproduced on this host.

    ``panic_screen = true`` and no ``--uefi``: the stage must record *failed*
    with a reason that names the file, the upstream line that reads it and the
    flag that fixes it.  A reader told only "Font.h is missing" cannot act,
    which is why the original clang diagnostic was not enough.
    """
    _pin_uefi(panic_repo, FONT_STUB)
    kernelcache = tmp_path / "kernelcache.bin"
    kernelcache.write_text("fake kernelcache\n", encoding="utf-8")

    result = panic_repo.run("--payload", "--kernelcache", kernelcache.as_posix())
    assert result.returncode != 0, result.stdout + result.stderr

    step = _payload_step(panic_repo)
    assert step["status"] == "failed", step
    reason = step["reason"]
    assert "Font.h" in reason, reason
    # Both spellings of the option are named, so an operator can use either.
    assert "--uefi" in reason, reason
    assert "UNIXNU_UEFI" in reason, reason
    assert "patch-xnu-entry-probe.py:183" in reason, reason
    assert "uefi_sheng" in reason, reason
    # The path it names is the one the patcher would be given.
    assert FONT_RELATIVE in reason, reason
    # ...and nothing was built: the refusal happens before the driver runs.
    assert not _payload_calls(panic_repo)


def test_the_payload_stage_hands_the_driver_the_uefi_checkout(
    panic_repo: _ScratchRepo, tmp_path: Path
) -> None:
    """With ``--uefi`` the font gate passes and the flag reaches the driver.

    Read out of the driver's own ``argv.log``, not out of the script's text: a
    flag that is spelled right in a comment and dropped in the code is the
    defect this closes, and the payload stage had no argv witness at all before
    this file existed.
    """
    checkout = _mu_silicium(tmp_path, FONT_STUB)
    _pin_uefi(panic_repo, FONT_STUB)
    kernelcache = tmp_path / "kernelcache.bin"
    kernelcache.write_text("fake kernelcache\n", encoding="utf-8")

    result = panic_repo.run(
        "--payload",
        "--uefi", checkout.as_posix(),
        "--kernelcache", kernelcache.as_posix(),
    )

    # The gate passed, in the log, with the file's own digest.
    assert "payload: panic-screen font" in result.stdout, result.stdout + result.stderr
    assert "FAIL" not in result.stdout.split("payload: panic-screen font")[1].splitlines()[0], (
        result.stdout
    )

    calls = _payload_calls(panic_repo)
    assert len(calls) == 1, panic_repo.argv_log()
    payload_call = calls[0]
    assert "--uefi" in payload_call, payload_call
    assert payload_call[payload_call.index("--uefi") + 1] == checkout.resolve().as_posix(), (
        "the driver was handed a --uefi that is not the checkout this run passed"
    )
    # The stage got past the font gate; it stops later, at reading the volume
    # back, which is a different (and honest) failure.
    step = _payload_step(panic_repo)
    assert "Font.h" not in step["reason"], step


def test_a_font_that_is_not_the_locked_bytes_is_refused(
    panic_repo: _ScratchRepo, tmp_path: Path
) -> None:
    """A file at the right path with the wrong bytes is not the pinned font.

    The digest is what makes the pin mean something: any Mu-Silicium checkout
    carries a Font.h, and a panic screen drawn from another one is not this
    build.  Refusing by digest rather than by path is the difference.
    """
    checkout = _mu_silicium(tmp_path, OTHER_FONT)
    _pin_uefi(panic_repo, FONT_STUB)  # the lock pins the stub; the checkout has other bytes
    kernelcache = tmp_path / "kernelcache.bin"
    kernelcache.write_text("fake kernelcache\n", encoding="utf-8")

    result = panic_repo.run(
        "--payload",
        "--uefi", checkout.as_posix(),
        "--kernelcache", kernelcache.as_posix(),
    )
    assert result.returncode != 0, result.stdout + result.stderr

    step = _payload_step(panic_repo)
    assert step["status"] == "failed", step
    assert "not the one the lock pins" in step["reason"], step
    assert hashlib.sha256(OTHER_FONT).hexdigest() in step["reason"], step
    assert hashlib.sha256(FONT_STUB).hexdigest() in step["reason"], step
    # ...and the driver was never asked to build anything.
    assert not _payload_calls(panic_repo)


def test_a_lock_that_disagrees_with_the_patcher_about_the_path_is_refused(
    panic_repo: _ScratchRepo, tmp_path: Path
) -> None:
    """The lock cannot quietly relocate the font the compiler looks for."""
    checkout = _mu_silicium(tmp_path, FONT_STUB)
    _pin_uefi(panic_repo, FONT_STUB, path="Elsewhere/FrameBufferSerialPortLib/Font.h")
    kernelcache = tmp_path / "kernelcache.bin"
    kernelcache.write_text("fake kernelcache\n", encoding="utf-8")

    result = panic_repo.run(
        "--payload",
        "--uefi", checkout.as_posix(),
        "--kernelcache", kernelcache.as_posix(),
    )
    assert result.returncode != 0, result.stdout + result.stderr

    step = _payload_step(panic_repo)
    assert step["status"] == "failed", step
    assert "disagree about where Font.h is" in step["reason"], step
    assert "Elsewhere/FrameBufferSerialPortLib/Font.h" in step["reason"], step
    assert FONT_RELATIVE in step["reason"], step
    assert not _payload_calls(panic_repo)


def test_a_board_without_the_panic_screen_needs_no_font(
    plain_repo: _ScratchRepo, tmp_path: Path
) -> None:
    """The gate is about the board's switch, not about ``--uefi`` in general.

    Without this the absent-font refusal could be satisfied by demanding a
    Mu-Silicium checkout for every payload build, which would break a board that
    never asked for a panic screen.
    """
    kernelcache = tmp_path / "kernelcache.bin"
    kernelcache.write_text("fake kernelcache\n", encoding="utf-8")

    result = plain_repo.run("--payload", "--kernelcache", kernelcache.as_posix())
    # The run fails somewhere -- the volume it is asked to read back does not
    # exist -- but NOT at the font gate.
    step = _payload_step(plain_repo)
    assert "Font.h" not in step["reason"], step
    assert "panic-screen font" not in step["reason"], step
    assert "panic screen" not in step["reason"], step
    calls = _payload_calls(plain_repo)
    assert len(calls) == 1, plain_repo.argv_log()
    assert "--uefi" not in calls[0], (
        "the driver was handed a --uefi this run never passed; an empty value "
        "would put a misleading argument in the log"
    )
    assert "uefi:     <none" in result.stdout, result.stdout
