"""``build-os-root`` on the macOS runner: the chain, and what a second pass owes it.

Why this file exists
--------------------
``step_build_os_root`` was unreachable on the runner.  The plan puts it before
``build-kernel`` and ``build-payload``, but its own first sub-step boots the
kernel step's ``kernel.macho`` (``build-storage-abi.py:18,31``) -- so on a fresh
workspace it recorded ``not_run``, ``build-kernel`` built the board's
``qemu_defconfig``, and the runner never produced a kernel carrying
``CONFIG_XNU_NATIVE_VIRTIO_BLOCK``.  Measured on this host against the real
guest: such a kernel reaches ``mountroot``, matches the board's UUID and has APFS
loaded, and enumerates nothing.

The vendor chain documents the sequence (README:96-98): run the script once, let
``build-kernel`` produce the baseline kernel, then run it *again* -- the second
invocation finds the chain's outputs on disk and builds ``qemu_root_defconfig``.
The workflow made exactly one invocation, so the chain could never run.

What this file pins
-------------------
* ``test_the_chain_already_on_disk_is_recorded_skipped_not_ran`` -- the
  load-bearing one.  A second invocation must not claim work it did not do, and
  must not re-spend the two volumes' 7.88 GiB to arrive at the same place.  If
  this ever reports ``ran``, the ledger is lying about who ran the chain.
* ``test_no_chain_on_disk_is_still_not_run`` -- the guard is a short-circuit, not
  a green light.  Without the chain's three inputs the stage still records
  ``not_run`` with its reason, so a missing chain can never read as a pass.
* ``test_the_workflow_runs_the_chain_between_two_build_passes`` -- the defect
  itself, pinned as data.  The workflow has to invoke ``build-macos.sh`` twice
  and hand the chain the members it reads, or every kernel it produces is a
  ``qemu_defconfig`` kernel again.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "build-macos.yml"
REAL_LOCK = ROOT / "config" / "sources.lock.json"

pytestmark = pytest.mark.acceptance

UNIXNU_COMMIT = "b881ac7dc1695ce676c856873051a487c6a9f963"
DEVICE_COMMIT = "97b75b1f63c6b7e895cd7646029db741d5fe5488"

#: The three files ``root_chain_present()`` checks, spelled the way
#: ``build-macos.sh`` spells them: the Data seed (which carries the volume UUID
#: the kernel build exports) and the two archives ``qemu_root_defconfig`` links.
CHAIN_FILES = (
    ".cache/firmware/owner-data/seed.json",
    "tree/build/native-virtio-block-verified-bundle/libNativeVirtioBlock.a",
    "tree/build/verified-root-os-xnu-bundle/libVerifiedRootProbe.a",
)

VOLUME_UUID = "04769D26-2CA0-4825-B443-E2E5F88300F2"


def _bash() -> str:
    """A bash that actually starts, or a named skip.

    Resolving a name is not the same as running it, and on this host the two
    came apart on 2026-10-25: ``shutil.which("bash")`` returns
    ``C:\\Windows\\System32\\bash.exe`` -- the WSL launcher -- and the WSL
    distributions it would start have no ``ext4.vhdx``, so every invocation
    dies with ``Bash/Service/CreateInstance/MountDisk/HCS/ERROR_FILE_NOT_FOUND``
    before the script under test sees a single byte of input.  The result is a
    ``FileNotFoundError`` on ``build-report.json``, which reads as "the build
    script is broken" when nothing about the build script was ever exercised.

    So each candidate is *run*, and a candidate that cannot answer
    ``--version`` is not a candidate.  Nothing here decides that the tests are
    unimportant: the skip names the measured failure, and the assertions below
    are untouched.
    """
    for candidate in ("bash", "bash.exe"):
        located = shutil.which(candidate)
        if not located:
            continue
        try:
            probe = subprocess.run(
                [located, "--version"],
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as failure:  # pragma: no cover
            continue
        if probe.returncode == 0:
            return located
    pytest.skip(
        "no runnable bash on this host: every bash on PATH is a launcher whose "
        "distribution cannot start (measured on 2026-10-25: WSL's Ubuntu has no "
        "ext4.vhdx, so bash.exe exits with HCS/ERROR_FILE_NOT_FOUND). These tests "
        "run build-macos.sh itself and need a working shell."
    )


def _tool_shims(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``xcodebuild``/``clang``/``python3`` all resolve.

    ``require_tools()`` refuses the whole run when ``xcodebuild`` or ``clang`` is
    absent, and on the Windows development host both are.  They are shimmed
    rather than skipped because the thing under test is *after* the preflight:
    the plan has to run for ``build-os-root`` to be reached at all.

    Extension-less executables, not ``.cmd`` and not a ``#!/bin/sh`` wrapper: this
    host's ``bash`` does not resolve a ``.cmd`` through ``command -v``, and a shell
    wrapper that ``exec``s the Windows interpreter deadlocks Cygwin's heap on the
    next fork ("child_copy: cygheap read copy failed").  A copy of the interpreter
    itself is the one spelling that survives both.

    Nothing here makes the host look like macOS -- ``apple_step_gate`` reads
    ``uname -s`` and still records ``not_run`` for every Apple stage -- so the
    shims buy the plan, not a green stage.
    """
    import shutil as _shutil
    import sys

    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    for tool in ("xcodebuild", "clang"):
        path = shim / tool
        path.write_text(
            f'#!/bin/sh\necho "synthetic {tool}: no macOS toolchain on this host"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    interpreter = Path(sys.executable)
    if interpreter.suffix.lower() == ".exe":
        _shutil.copyfile(interpreter, shim / "python3.exe")
    else:  # pragma: no cover - a host whose interpreter is not a Windows binary
        (shim / "python3").write_text(
            f'#!/bin/sh\nexec "{interpreter}" "$@"\n', encoding="utf-8"
        )
        (shim / "python3").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _python_on_path(tmp_path: Path) -> dict[str, str]:
    return _tool_shims(tmp_path)


def _lock_payload() -> dict:
    """A lock shaped like the real one, with no submodules and no feeds.

    The empty lists are load-bearing: ``offline_missing()`` walks exactly those
    two lists, so an empty pair reads as "every pinned source is already on
    disk" and ``prepare-sources`` records ``ran`` instead of setting
    ``SOURCES_INCOMPLETE`` -- which would make every build stage record
    ``not_run`` before any of them was reached, and this test would prove
    nothing about ``build-os-root``.
    """
    payload = (
        json.loads(REAL_LOCK.read_text(encoding="utf-8"))
        if REAL_LOCK.is_file()
        else {"schema_version": 1}
    )
    payload["frozen"] = True
    payload.setdefault("unixnu", {})["commit"] = UNIXNU_COMMIT
    payload.setdefault("device_repo", {})["commit"] = DEVICE_COMMIT
    payload["submodules"] = []
    payload["feeds"] = []
    return payload


def _scratch(tmp_path: Path, *, chain: bool) -> tuple[Path, Path, Path]:
    """A workspace the build script can run its plan in, with or without the chain.

    ``chain=True`` places the three files ``root_chain_present()`` checks, which
    is the state a second invocation of the script is in after the workflow has
    run ``build-os-root.py`` between the two passes.
    """
    workspace = tmp_path / "ws"
    device = workspace / "device"
    # The pinned UniXNU checkout.  ``prepare-sources`` looks for ``.git`` and
    # nothing else, so an empty directory with one is enough: the lock declares no
    # submodules and no feeds, so nothing is ever read out of either checkout.
    for checkout in ("unixnu", "device"):
        (workspace / checkout / ".git").mkdir(parents=True, exist_ok=True)
    (device / "configs").mkdir(parents=True, exist_ok=True)
    (device / "board.toml").write_text(
        "[board]\n"
        'name = "qemu"\n'
        "\n"
        "[kernel]\n"
        'defconfig = "qemu_defconfig"\n'
        "\n"
        "[payload]\n"
        'volume = "UNIPAD"\n'
        'size_mib = 192\n'
        'loader = "firmware"\n',
        encoding="utf-8",
    )
    (device / "configs" / "qemu_defconfig").write_text(
        "# a board defconfig, for the stage's own existence check\n", encoding="utf-8"
    )
    # The defconfig the kernel stage switches to when the chain is on disk.  Its
    # existence is checked before anything else in that stage, so without it the
    # run would die on a missing file rather than reach the ledger.
    (device / "configs" / "qemu_root_defconfig").write_text(
        "# the root-chain defconfig, for the kernel stage's own check\n",
        encoding="utf-8",
    )
    config = workspace / "config"
    config.mkdir(parents=True, exist_ok=True)
    lock = config / "sources.lock.json"
    lock.write_text(json.dumps(_lock_payload(), indent=2), encoding="utf-8")

    out = _out_dir(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    if chain:
        seed = device / ".cache/firmware/owner-data/seed.json"
        seed.parent.mkdir(parents=True, exist_ok=True)
        seed.write_text(json.dumps({"volume_uuid": VOLUME_UUID}), encoding="utf-8")
        # Relative to tree_dir(), which is "$UNIXNU_OUT/$BOARD_NAME/tree" -- so
        # "tree/build/..." would put them one directory below where the stage
        # looks, and the short-circuit would silently never fire.
        tree = out / "unixnu-out" / "qemu" / "tree"
        for relative in CHAIN_FILES[1:]:
            target = tree / relative.removeprefix("tree/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic archive\n")
    return lock, device, out


def _run(tmp_path: Path, *, chain: bool) -> tuple[subprocess.CompletedProcess, Path]:
    lock, device, out = _scratch(tmp_path, chain=chain)
    return (
        subprocess.run(
        [
            _bash(),
            SCRIPT.as_posix(),
            "--stage",
            "M2",
            "--experiment",
            "os-root-gate",
            "--lock",
            lock.as_posix(),
            "--out",
            out.as_posix(),
            "--unixnu",
            (tmp_path / "ws" / "unixnu").as_posix(),
            "--device",
            device.as_posix(),
        ],
        capture_output=True,
        text=True,
        env=_python_on_path(tmp_path),
        cwd=str(tmp_path),
        timeout=300,
        ),
        out,
    )


#: What ``build-macos.sh`` requires of the output filesystem
#: (``DEFAULT_MIN_FREE_GB``), in bytes.  Read here so the relocation below and
#: the gate itself cannot drift apart.
REQUIRED_FREE_BYTES = 8 * (1024 ** 3)


def _out_dir(tmp_path: Path) -> Path:
    """Where this run writes its output: ``tmp_path/out`` when it can host one.

    ``DEFAULT_MIN_FREE_GB=8`` is measured against the output filesystem, and
    pytest's ``tmp_path`` lives under ``%LOCALAPPDATA%`` -- ``C:`` on this host,
    which held 5.1 GiB free on 2026-10-25.  The script then refuses before the
    plan runs and the test never reaches the ledger entry it exists to pin.

    So the output is moved to a volume that can actually host it, rather than
    the gate being relaxed: ``--min-free-gb`` is not passed, the 8 GiB
    requirement still applies, and the run satisfies it on merit.  The
    directory name carries ``tmp_path.name``, which pytest already makes unique
    per test, so two concurrent runs cannot collide; it is removed on the way
    out.  When no volume has room the caller's directory is returned unchanged
    and the script's own refusal is what the reader sees.
    """
    preferred = tmp_path / "out"
    try:
        if shutil.disk_usage(tmp_path).free >= REQUIRED_FREE_BYTES:
            return preferred
    except OSError:  # pragma: no cover - an unreadable volume
        return preferred
    for letter in "DEFG":
        root = Path(f"{letter}:/")
        if not root.is_dir():
            continue
        try:
            if shutil.disk_usage(root).free < REQUIRED_FREE_BYTES:
                continue
        except OSError:  # pragma: no cover - an unreadable volume
            continue
        relocated = root / "unipad-test-out" / tmp_path.name
        shutil.rmtree(relocated, ignore_errors=True)
        relocated.mkdir(parents=True, exist_ok=True)
        atexit.register(shutil.rmtree, relocated, True)
        return relocated
    return preferred


def _report(out: Path, result: subprocess.CompletedProcess) -> dict:
    """The run's build-report.json, or a named skip for a host precondition.

    A missing report is normally a defect: the script is supposed to write one
    whatever the stages decided, so its absence means it died before the ledger
    existed.  One case is different, and it was measured rather than assumed on
    2026-10-25 -- ``DEFAULT_MIN_FREE_GB=8`` against a ``C:`` volume holding 5.1
    GiB free.  The script refuses that with exit 3 and a ``blocked`` reason that
    names both numbers, which is the correct behaviour for the gate and the
    wrong outcome for a test whose subject is the ``build-os-root`` ledger
    entry.  Skipping on that exact reason keeps the distinction visible; failing
    would report a host's disk as this script's defect, and letting the
    ``FileNotFoundError`` through would report it as nothing at all.
    """
    path = out / "build-report.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    text = f"{result.stdout or ''}{result.stderr or ''}"
    if "not enough free space" in text:
        free = re.search(r"([0-9.]+) GiB free, ([0-9.]+) GiB required", text)
        detail = (
            f" ({free.group(1)} free, {free.group(2)} required)"
            if free
            else ""
        )
        pytest.skip(
            "the output filesystem cannot host a build: build-macos.sh refused "
            f"with 'not enough free space'{detail}. DEFAULT_MIN_FREE_GB=8 is a "
            "real gate and was not relaxed for this test."
        )
    raise AssertionError(
        "build-macos.sh produced no build-report.json, so no stage ledger "
        f"exists (exit {result.returncode}).\n{text[-2000:]}"
    )


def _stages(result: subprocess.CompletedProcess, out: Path) -> dict[str, dict[str, str]]:
    report = _report(out, result)
    return {
        entry["name"]: {"status": entry["status"], "reason": entry.get("reason", "")}
        for entry in report["steps"]
    }


def test_the_chain_already_on_disk_is_recorded_skipped_not_ran(tmp_path: Path) -> None:
    """A second invocation must not claim it ran the chain.

    The workflow runs ``build-os-root.py`` itself, between the two passes, and
    deletes the volumes afterwards -- 7.88 GiB of DMG that a second invocation
    would have to re-extract to reach the same place.  So the stage short-circuits
    on ``root_chain_present()``, and it says ``skipped`` rather than ``ran``:
    ``ran`` would be this script taking credit for work a different step did,
    which is precisely the kind of ledger entry nobody can audit later.
    """
    result, out = _run(tmp_path, chain=True)
    stages = _stages(result, out)
    entry = stages.get("build-os-root", {})
    assert entry.get("status") == "skipped", (entry, result.stdout, result.stderr)
    # ...and it names what it found, so a reader can tell inherited work from
    # work this run did.
    assert "seed.json" in entry.get("reason", ""), entry
    assert "libNativeVirtioBlock.a" in entry.get("reason", ""), entry
    assert "libVerifiedRootProbe.a" in entry.get("reason", ""), entry
    assert "qemu_root_defconfig" in entry.get("reason", ""), entry
    # It must not stop the plan: the kernel stage is the consumer.
    assert "build-kernel" in stages, stages


def test_no_chain_on_disk_is_still_not_run(tmp_path: Path) -> None:
    """The short-circuit is not a green light.

    Without the chain's three inputs the stage still records ``not_run`` with a
    reason that names what is missing -- so a workspace where the chain never ran
    reads as unverified rather than as a pass.  This is the guard that makes the
    ``skipped`` above trustworthy.
    """
    result, out = _run(tmp_path, chain=False)
    stages = _stages(result, out)
    entry = stages.get("build-os-root", {})
    assert entry.get("status") == "not_run", (entry, result.stdout, result.stderr)
    assert entry.get("reason"), entry
    assert "skipped" not in entry.get("reason", ""), entry


def test_the_workflow_runs_the_chain_between_two_build_passes() -> None:
    """The defect itself, pinned as data.

    One invocation means the chain can never run: the plan puts ``build-os-root``
    before the kernel and payload it consumes, so the first pass records it
    ``not_run`` and only a second pass can build ``qemu_root_defconfig``.  The
    workflow has to (a) invoke the script twice, (b) publish the chain's members
    out of the image it already downloads, (c) run ``build-os-root.py`` with an
    ``--ipsw``, and (d) hand the second pass the same inputs as the first.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    invocations = text.count("bash scripts/macos/build-macos.sh")
    assert invocations >= 2, (
        "the workflow invokes build-macos.sh once, so build-os-root can never "
        "run: its first sub-step needs the kernel and payload the plan builds "
        "after it, and only a second invocation sees the chain on disk. "
        f"found {invocations} invocation(s)"
    )
    # The chain's members, published from the image rather than the image kept.
    for flag in ("--os-volume", "--systemos-volume", "--static-trustcache", "--refer-ipsw"):
        assert flag in text, f"the workflow never asks for {flag}"
    # ...and the driver's own IPSW cache, which is not one of the chain's
    # members: build-platform-power.py reads it on any build pass that already
    # has a reference kernel.  Measured on run 40206274286, which got
    # os_root_status=pass for the first time and then died in the second pass
    # on archive 10 of 10 with "The original kernelcache is not in the IPSW
    # cache; run \"unixnu ipsw fetch\"" -- the first pass passes that archive
    # because it links a no-op bridge when no reference kernel exists, so the
    # second pass is the first one that needs the file, and nothing here runs
    # `unixnu ipsw fetch`.
    assert "--kernelcache-cache" in text, (
        "the workflow never populates the driver's IPSW cache, so the second "
        "build pass -- the one that builds qemu_root_defconfig -- dies on "
        "build-platform-power.py with no original kernelcache to audit"
    )
    # The chain itself, run the way README:96 runs it.
    assert "scripts/build-os-root.py" in text, (
        "the workflow never runs build-os-root.py, so the owner root chain is "
        "never built and every kernel is a qemu_defconfig kernel"
    )
    assert "--ipsw" in text, (
        "the workflow never passes --ipsw to anything, so the OS and SystemOS "
        "images and the System trust cache have no source"
    )
    # The volumes are Apple material and must not outlive the job (PRD 12).
    assert "target-volumes" in text, (
        "the workflow never removes the OS and SystemOS volumes it placed; "
        "Apple firmware material would survive the job"
    )


# ---------------------------------------------------------------------------
# the display driver the chain compiles at step 9
# ---------------------------------------------------------------------------
#: ``drivers/NativeMobileDisplay`` in the lock, or ``None`` when the lock names
#: no such entry at all.
def _display_entry() -> dict | None:
    payload = json.loads(REAL_LOCK.read_text(encoding="utf-8"))
    return next(
        (
            entry
            for entry in payload.get("submodules") or []
            if entry.get("path") == "drivers/NativeMobileDisplay"
        ),
        None,
    )


#: The pinned device repository's display-ABI generator.  ``vendor/`` is not
#: tracked (``.git/info/exclude``), so a checkout without it cannot answer the
#: questions below -- which is why those two tests are conditional rather than
#: silently green.
DEVICE_REPO_DIR = ROOT / "vendor" / "UniXNU-qemu"
DISPLAY_DIR = ROOT / "vendor" / "UniXNU" / "drivers" / "NativeMobileDisplay"


def test_the_locked_display_driver_declares_its_divergence_from_the_gitlink() -> None:
    """A submodule moved off UniXNU's own gitlink must say so in the lock.

    ``unixnu fetch`` initialises every module submodule at the commit the
    driver repository's gitlink names, so a lock that pins a *different* commit
    is a divergence from the pin by construction.  That is allowed -- and on
    2026-10-25 it is required -- but only if it is recorded: a ``commit`` that
    quietly disagrees with ``gitlink_commit`` is a second place that knows a
    sha, which is the class this project keeps hitting.

    The assertion is about the *shape*, not the value: either the two agree, or
    both are present and the note explains the move.
    """
    entry = _display_entry()
    assert entry is not None, "the lock pins no drivers/NativeMobileDisplay"
    commit = entry.get("commit") or ""
    assert re.fullmatch(r"[0-9a-f]{40}", commit), entry
    gitlink = entry.get("gitlink_commit") or ""
    if commit != gitlink:
        assert re.fullmatch(r"[0-9a-f]{40}", gitlink), (
            "the locked display driver differs from the gitlink but does not "
            f"record which gitlink: {entry}"
        )
        assert len(entry.get("commit_note") or "") > 200, (
            "a submodule moved off its gitlink needs the measured reason "
            f"recorded, not a sentence: {entry}"
        )


def test_the_workflow_forces_the_locked_display_driver_before_the_chain() -> None:
    """The divergence has to reach the runner, not just the lock file.

    The runner clones ``vendor/`` fresh at the locked commits and then lets
    ``unixnu fetch`` initialise the submodules from the gitlink -- so a lock
    entry alone changes nothing on the runner.  The workflow must read the
    locked commit and move the assembled tree's checkout to it before it runs
    ``build-os-root.py``, and it must refuse rather than continue if it cannot.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "drivers/NativeMobileDisplay" in text, (
        "the workflow never names the display driver, so the tree's checkout "
        "stays at UniXNU's gitlink commit whatever the lock says"
    )
    assert "gitlink_commit" in text or "git -C \"$display\" fetch" in text, (
        "the workflow does not read the lock's divergence, so a moved submodule "
        "is not applied on the runner"
    )
    for required in (
        "git -C \"$display\" fetch",
        "git -C \"$display\" checkout",
        "git -C \"$display\" rev-parse HEAD",
    ):
        assert required in text, (
            f"the workflow never runs `{required}`, so it cannot prove the "
            "tree's display driver is the locked one"
        )


def test_the_workflow_applies_the_divergence_where_the_sources_are_laid_down() -> None:
    """A moved submodule has to be moved in the acquisition, not only before the chain.

    Measured on run 40199915736, which is the run that proved the first attempt
    at this was in the wrong place.  Moving the lock's
    ``drivers/NativeMobileDisplay`` commit back one commit -- without moving the
    checkout -- made ``prepare-sources`` exit 1 with::

        pinned sources are missing and this script performs no network access,
        so the upstream driver cannot be allowed to fetch them:
        submodule:drivers/NativeMobileDisplay

    because ``unixnu fetch`` had initialised the submodule at the gitlink's
    commit and ``offline_missing`` compares the on-disk HEAD against the LOCK.
    The job then skipped every remaining step, so nothing about the build had
    been exercised.  The acquisition step is where the sources are laid down, so
    that is where the lock has to win -- and the chain step's own forcing stays
    as the second, idempotent half.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "def apply_submodule_divergences" in text, (
        "the acquisition step never applies the lock's submodule commits, so a "
        "submodule the lock pins away from the driver's gitlink stays at the "
        "gitlink and prepare-sources refuses the whole build"
    )
    # It has to run after the driver has initialised the submodules, or there is
    # nothing to move -- and before the cross-check, or the report does not say
    # what was moved.  The CALL is what carries the ordering; the definition is
    # further up the file by construction, and matching the bare name would find
    # the `def` and prove nothing.
    position = text.index("divergences = apply_submodule_divergences(")
    assert text.index('run_driver("fetch")') < position, (
        "the divergence is applied before the driver fetches the submodules, so "
        "there is nothing on disk to move"
    )
    assert text.index('run_driver("tree")') < position, (
        "the divergence is applied before the driver assembles the tree, so the "
        "tree may still carry the gitlink's version"
    )
    assert text.index("= cross_check(feeds") > position, (
        "the divergence is applied after the cross-check, so the report never "
        "records what was moved"
    )
    assert text.index('"submodule_divergences"') > position, (
        "the acquisition report does not record the divergences it applied"
    )


@pytest.mark.skipif(
    not DEVICE_REPO_DIR.is_dir() or not DISPLAY_DIR.is_dir(),
    reason="vendor/UniXNU-qemu and vendor/UniXNU/drivers/NativeMobileDisplay are "
    "not present in this checkout (vendor/ is git-excluded, so the dependency "
    "is fixed rather than vendored)",
)
def test_the_locked_display_driver_overrides_only_declared_slots() -> None:
    """The skew itself, pinned from both sides.

    Measured on run 40187668943: the chain reached its step 9 and died with
    ``NativeLegacyDisplay.cpp:206:59: error: only virtual member functions can
    be marked 'override'``.  ``build-native-legacy-display.py`` generates
    ``OriginalLegacyFramebufferABI.h`` from a fixed slot table, so a source that
    overrides a name that table does not declare cannot compile -- and the same
    script then refuses it again on its owned-set invariant.

    Both halves are checked here, because fixing only one of them is how this
    comes back: the source must not declare an override the generator does not
    know, and the generator must not grow a declaration the source does not
    implement.  ``registerService`` is the one legitimate exception -- it is an
    ``IOService`` virtual, outside ``IOMobileFramebufferLegacy``'s slot table
    entirely -- and ``opaque<n>`` comes from the ``LEAF(n)`` macro, whose
    expansion is not spelled out in the source.
    """
    source = DISPLAY_DIR / "NativeLegacyDisplay.cpp"
    generator = DEVICE_REPO_DIR / "scripts" / "build-native-legacy-display.py"
    text = source.read_text(encoding="utf-8", errors="replace")
    script = generator.read_text(encoding="utf-8", errors="replace")

    # What the generated header declares.  Two places, because the generator
    # builds it in two: the triple-quoted base template re-declares the IOService
    # lifecycle the class inherits, and the `for i in range(168, 358)` loop
    # appends one declaration per original slot.  Reading only one of them is
    # how a check like this passes while the compile still fails.
    base_at = script.index("header='''") + len("header='''")
    base = script[base_at : script.index("'''", base_at)]
    appended = "".join(re.findall(r"header\s*\+=\s*'(.*?)'\n", script))
    declaration = r"\b(?:virtual\s+)?[A-Za-z_][\w:<>,*&\s]*?\b([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?(?:override\s*)?;"
    declared = set(re.findall(declaration, base, re.MULTILINE))
    declared |= set(re.findall(declaration, appended))
    # The pure virtuals end in `= 0`, not `;`, so the declaration pattern above
    # cannot see them -- and they are exactly the ones a missing override turns
    # into an uninstantiable class.
    pure = set(re.findall(r"\b([A-Za-z_]\w*)\s*\([^;]*\)\s*=\s*0", appended))
    declared |= pure

    # Drop the LEAF macro definition so its `opaque##n() override` body is not
    # read as a named override; the expansions are what matter and they are all
    # guarded leaves the generator already accounts for.
    macro = re.search(r"#define LEAF\(n\).*", text)
    body = text[: macro.start()] + text[macro.end():] if macro else text
    overrides = set(
        re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*override\b", body)
    )
    assert declared, "the generator's declarations were not found; the extraction changed"
    missing = overrides - declared - {"registerService"}
    assert not missing, (
        "the locked NativeLegacyDisplay.cpp overrides "
        f"{sorted(missing)}, which the pinned device repository's "
        "build-native-legacy-display.py does not declare in the "
        "IOMobileFramebufferLegacy table it generates: the compile fails with "
        "'only virtual member functions can be marked override'. Re-pin "
        "drivers/NativeMobileDisplay, or update the generator's slot table."
    )
    # ...and the other direction, narrowed to what actually breaks: a PURE
    # virtual the generator emits with no implementation is a class that cannot
    # be instantiated, which surfaces as a linker error rather than a compile
    # error and is therefore far harder to read out of a runner log.  The
    # non-pure virtuals are meant to inherit the original's body.
    for name in sorted(pure):
        assert name in overrides, (
            f"the generator declares {name} as a pure virtual but the locked "
            "NativeLegacyDisplay.cpp does not override it, so the class cannot "
            "be instantiated"
        )

# ---------------------------------------------------------------------------
# the refer image the chain's tenth step reads
# ---------------------------------------------------------------------------
def _closing_fi(lines: list[str], opening: int) -> int:
    """The ``fi`` that closes the ``if`` starting at or after ``opening``."""
    return next(i for i in range(opening, len(lines)) if lines[i].strip() == "fi")


def _refer_block() -> str:
    """The chain step's refer-path resolution, exactly as the shell reads it.

    Extracted from the workflow rather than retyped, so the assertion below is
    about the code the runner executes.  A retyped copy would keep passing
    after the runner's version drifted, which is the failure mode this whole
    file exists to prevent.

    Both shapes are extracted -- with and without the absolute-isation -- so the
    test is about the PATH the step computes and not about whether a particular
    guard happens to be present.  The first draft demanded the guard's exact
    spelling and therefore failed with ``StopIteration`` on the unfixed
    workflow, which proves the extractor is brittle and nothing about the
    defect.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, line in enumerate(lines) if 'refer="$(ls "$REFER_DIR"/*.ipsw' in line
    )
    empty = next(i for i in range(start, len(lines)) if '[ -z "$refer" ]' in lines[i])
    ranges = [(start, _closing_fi(lines, empty))]
    # Whatever follows the empty-check: the absolute-isation and its own guard,
    # when the workflow has them.  Comments are skipped so the reason they give
    # is not read as code; the first line that is neither a comment nor about
    # ``refer`` ends the block.
    cursor = ranges[0][1] + 1
    while cursor < len(lines):
        line = lines[cursor]
        if line.strip() == "" or line.lstrip().startswith("#"):
            cursor += 1
            continue
        if "refer=" not in line and '[ ! -f "$refer" ]' not in line:
            break
        ranges.append((cursor, _closing_fi(lines, cursor)))
        cursor = ranges[-1][1] + 1
    return "\n".join("\n".join(lines[begin : end + 1]) for begin, end in ranges)


def test_the_chain_hands_build_os_root_an_absolute_refer_path(tmp_path: Path) -> None:
    """The refer path must be absolute, because the module resolves it from the tree.

    Measured on run 40203832707 -- the run that took nine of the chain's ten
    steps to PASS for the first time (storage-abi, both policies, 1483785 OS
    blocks and 806400 SystemOS blocks verified by real C/RSA, the verified-root
    probe, both block drivers in verified mode, the 1 GiB owner data, and the
    whole display/GOP/HID audit) and then died on the tenth with::

        FileNotFoundError: [Errno 2] No such file or directory:
          '.../build-output/unixnu-out/qemu/tree/refer/iPad_Pro_HFR_...ipsw'

    ``build-os-root.py`` runs ``cd "$tree"`` first and then does
    ``ipsw.symlink_to(Path(args.ipsw).resolve())``, so a workspace-relative
    ``--ipsw`` becomes a symlink pointing at ``<tree>/ipsw-work/refer/...`` --
    a path that does not exist.  The traceback names the *symlink*, so it reads
    as a missing refer image when the image was there the whole time.

    The same relative path is handed to ``extract-target-volume.py``, so this
    also covers the two volumes' extraction on a run that starts with an empty
    ``.cache`` -- which this run skipped only because the volumes were already
    on disk.
    """
    block = _refer_block()

    workspace = tmp_path / "work"
    refer_dir = workspace / "ipsw-work" / "refer"
    refer_dir.mkdir(parents=True)
    image = refer_dir / "iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw"
    image.write_bytes(b"not an IPSW; the path is what is under test\n")
    # The build tree, which is the cwd build-os-root.py runs from and therefore
    # the base its Path(args.ipsw).resolve() is relative to.
    tree = workspace / "build-output" / "unixnu-out" / "qemu" / "tree"
    tree.mkdir(parents=True)

    script = tmp_path / "refer.sh"
    script.write_text(
        "set -euo pipefail\n"
        'REFER_DIR="ipsw-work/refer"\n'
        + block
        + "\nprintf '%s\\n' \"$refer\"\n",
        encoding="utf-8",
    )
    # The step's own cwd is the workspace: that is where REFER_DIR resolves.
    # The TREE is where the module runs, and the two are not the same place --
    # which is the entire defect.
    result = subprocess.run(
        [_bash(), str(script)], cwd=workspace, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        "the step's own refer block did not run:\n"
        + (result.stdout + result.stderr)[-2000:]
    )
    printed = result.stdout.strip().splitlines()[-1]

    assert os.path.isabs(printed), (
        f"the refer path is workspace-relative ({printed!r}).  build-os-root.py "
        "does `Path(args.ipsw).resolve()` from inside the build tree, so a "
        "relative --ipsw becomes a symlink pointing at "
        "<tree>/ipsw-work/refer/... -- a path that does not exist -- and "
        "build-original-system-trust.py then fails with FileNotFoundError "
        "naming the symlink rather than its target.  Measured on run "
        "40203832707."
    )
    assert Path(printed).name == image.name, (
        f"the refer path names {Path(printed).name!r}, not the lock's "
        f"{image.name!r}; build-original-system-trust.py hard-codes that name"
    )
    # The invariant the traceback was actually reporting, and the reason the
    # absolute form matters: the path has to name a real file *from the tree*,
    # because that is the cwd the module resolves it against.  Bash does the
    # stat, so this holds on every host the workflow can run on -- a Python
    # comparison of two absolute spellings does not, because a Cygwin `pwd` and
    # a Windows `Path` describe the same file differently.
    stat = subprocess.run(
        [_bash(), "-c", 'test -f "$1"', "_", printed],
        cwd=tree,
        capture_output=True,
        text=True,
    )
    assert stat.returncode == 0, (
        f"{printed!r} is not a file when stat'ed from the build tree, which is "
        "where build-os-root.py runs and resolves --ipsw.  Its refer symlink "
        "would dangle and its tenth step would fail naming the symlink, not "
        "the target (measured on run 40203832707)."
    )
