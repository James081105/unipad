"""A defconfig flag that turns on an archive must have a builder that is actually invoked.

Why this exists
---------------
``test_boot_progress_config_guard.py`` guards one flag: ``CONFIG_XNU_BOOT_PROGRESS``
turns on a branch that runs ``scripts/build-boot-progress.py``, and that script
exists nowhere in the pinned tree (R9e).  That guard is correct and it stays.

But it answers the wrong general question.  "Is this script missing" is one shape.
The shape that actually recurs in ``build-xnu-4k-baseline.sh`` is different and
quieter: the script *is* in the pinned tree, it lists the component, it names the
output archive -- and **nothing ever invokes it**.  ``vendor/UniXNU-qemu/scripts/
build-arm-driver.py:11`` lists 17 components; 9 of the archive paths in the
table below come from that script; 5 of those 9 are invoked and 4 are not,
because no invocation exists.

``XNU_NATIVE_VIRTIO_CONSOLE`` is one of the four.  Measured 2026-10-31
(``docs/kernel-tree-completeness-20261031.md``):

* the builder is present -- ``build-arm-driver.py:11`` (choice 8 of 17), ``:31``
  (output directory ``native-virtio-console``), ``:106-108``
  (``xcrun ar rcs build/native-virtio-console/libNativeVirtioConsole.a``);
* the source is present -- ``vendor/UniXNU/drivers/NativeVirtioConsole/
  NativeVirtioConsole.cpp``, submodule pinned at ``config/sources.lock.json:144-146``;
* the personality is present and correctly gated --
  ``vendor/UniXNU/patches/xnu/native-aes-personality.patch:23-29``;
* the flag is accepted and the archive is force-loaded --
  ``build-xnu-4k-baseline.sh:192-199`` and ``:666-670``;
* and the archive is never built, because ``board.toml``'s ``[kernel] archives``
  list, ``build-os-root.py`` and every other script in the tree invoke
  ``build-arm-driver.py`` for five components and never this one.

So this is not the R9e failure.  ``build-boot-progress.py`` is missing everywhere;
``NativeVirtioConsole`` is present and dormant.  The two need different fixes, and
a guard that only asks "is the file there" would report the console as fine.

What would happen if the flag were turned on today: ``:193-196`` accepts the
value, ``:198``/``:200`` install the CFLAG, the bootstrap pass skips the archive
block at ``:545`` and succeeds, and the real pass dies on the bare
``test -f`` at ``:668`` -- naming the path but not the reason.  That is
downstream of acquisition, the IPSW, the Mesa bootstrap, the GPU addon chain and a
full XNU build.

How this test is built, and the failure mode it is built against
---------------------------------------------------------------
The table below is hand-transcribed, and a hand-transcribed table that drifts is
worse than no table: it would guard a dependency that does not exist and miss the
ones that do.  R9e's guard caught exactly that on its own first run -- the branch
spelled ``$BOOT_PROGRESS``, not ``$XNU_BOOT_PROGRESS``, because the build script
renames the variable before testing it.  So every row here is read back out of
``build-xnu-4k-baseline.sh`` before it is believed, in three steps: the archive
path must appear verbatim, the variable must be guarded somewhere, and the guard
must be a ``= 1`` guard.

The second half -- "is the builder invoked" -- is a detector, and a detector that
silently returns "nothing is invoked" would make the guard pass for the wrong
reason.  So the table carries positive controls (eleven keys whose builders *are*
invoked, and which ``qemu_root_defconfig`` really does enable) and the test
asserts both directions: the enabled ones must resolve, and the dormant set the
tree actually has must equal ``DORMANT_KEYS``.  Wiring up the console builder
fails this test until the list is updated -- which is the honest outcome, not a
false alarm.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO / "vendor" / "UniXNU-qemu" / "configs"
BUILD_SCRIPT = REPO / "vendor" / "UniXNU" / "scripts" / "build-xnu-4k-baseline.sh"
VENDOR_ROOTS = (REPO / "vendor" / "UniXNU", REPO / "vendor" / "UniXNU-qemu")

#: The script that owns the 17-component choices list.  Excluded from the
#: invocation search as defensive depth -- but measured 2026-10-31, it is NOT the
#: thing that protects the check: ``build-arm-driver.py`` contains zero lines
#: naming both itself and a component, so the two-token requirement in
#: ``_is_invoked`` already rejects its choices list (``:11``), its output map
#: (``:31``) and its include line (``:81``).  The exclusion is kept because a
#: future docstring naming both would otherwise become a false positive, and
#: because it costs nothing.  Removing it was tested as a mutant and survived,
#: which is how the redundancy was found -- not assumed.
DRIVER_SCRIPT = "build-arm-driver.py"

#: A line that runs something, as opposed to a line that only names it.  Needed
#: because a script name also appears in docstrings, comments and README prose.
RUN_MARKERS = ("step(", "run(", "subprocess", "python3", "bash ", "check_call", "Popen", "os.system")

#: CONFIG_XNU_ key -> (archive path the kernel link requires, the builder that
#: produces it, whether that builder is the 17-component driver script).
#:
#: The archive paths are the *defaults* spelled in the build script, so the
#: self-check below can read them back verbatim.  Two of them can be overridden
#: from a defconfig (``XNU_NATIVE_VIRTIO_BLOCK_ARCHIVE`` and
#: ``XNU_VERIFIED_ROOT_ARCHIVE``); the override points at a different builder
#: invocation of the same component, so the check still applies.
ARCHIVE_BUILDERS = {
    "CONFIG_XNU_SOURCE_CORECRYPTO14": ("build/corecrypto-2023/libcorecrypto_kernel_noasm-namespaced.a", "build-corecrypto-14.py", False),
    "CONFIG_XNU_ARM_DT_PLATFORM": ("build/armdt-platform/libArmDTPlatform.a", "ArmDTPlatform", True),
    "CONFIG_XNU_ARMCPU": ("build/armcpu/libArmCpu.a", "ArmCpu", True),
    "CONFIG_XNU_NATIVE_AES": ("build/native-aes/libNativeAES.a", "NativeAES", True),
    "CONFIG_XNU_NATIVE_UFS": ("build/native-ufs/libNativeUfs.a", "NativeUfs", True),
    "CONFIG_XNU_NATIVE_ADRENO": ("build/native-adreno/libNativeAdreno.a", "NativeAdreno", True),
    "CONFIG_XNU_NATIVE_MOBILE_DISPLAY": ("build/native-mobile-display/libNativeMobileDisplay.late.a", "build-native-legacy-display.py", False),
    "CONFIG_XNU_NATIVE_LEGACY_DISPLAY": ("build/native-legacy-display/libNativeLegacyDisplay.late.a", "build-native-legacy-display.py", False),
    "CONFIG_XNU_NATIVE_VIRTIO_BLOCK": ("build/native-virtio-block/libNativeVirtioBlock.a", "NativeVirtioBlock", True),
    "CONFIG_XNU_NATIVE_OFFLINE_GPU": ("build/native-offline-gpu/libNativeOfflineGPU.late.a", "NativeOfflineGPU", True),
    "CONFIG_XNU_NATIVE_VIRTIO_CONSOLE": ("build/native-virtio-console/libNativeVirtioConsole.a", "NativeVirtioConsole", True),
    "CONFIG_XNU_NATIVE_RAM_BLOCK": ("build/native-ram-block/libNativeRamBlock.a", "NativeRamBlock", True),
    "CONFIG_XNU_SOURCE_PTHREAD": ("build/libpthread-kernel/libpthread_kernel.a", "build-libpthread-kernel.py", False),
    "CONFIG_XNU_VERIFIED_ROOT_PROBE": ("build/verified-root-xnu/libVerifiedRootProbe.a", "build-verified-root-probe.py", False),
    "CONFIG_XNU_SOURCE_MOCKFS": ("build/mockfs-fixture/libmockfs_fixture.a", "build-mockfs-fixture.py", False),
    "CONFIG_XNU_SHENG_PMGR": ("build/sheng-platform-power/libShengPlatformPower.a", "build-platform-power.py", False),
}

#: Keys whose builder is present in the pinned tree but never invoked, measured
#: 2026-10-31.
#:
#: ``CONFIG_XNU_NATIVE_VIRTIO_CONSOLE`` was in this set until fork commit
#: ``acf2f86`` wired it up -- ``board.toml``'s ``[kernel] archives`` gained
#: ``build-arm-driver.py NativeVirtioConsole`` and ``qemu_root_defconfig`` gained
#: ``CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y``.  Removing it here is what this test's
#: own failure message asks for, and the failure was the honest outcome: a key
#: that gained a builder is progress, and leaving it in the list would make the
#: test assert something untrue.
#:
#: What that change does NOT settle: the builder has never been *invoked* on a
#: macOS runner, so whether ``NativeVirtioConsole`` compiles is UNVERIFIED, and
#: "the archive builds" is not "the console enumerates" -- let alone works.  The
#: driver's four probe conditions are all measured on QEMU's side (fact 196); the
#: guest-side build and the transport's readiness are a boot measurement.
DORMANT_KEYS = {
    "CONFIG_XNU_NATIVE_UFS",
    "CONFIG_XNU_NATIVE_ADRENO",
    "CONFIG_XNU_NATIVE_OFFLINE_GPU",
}


def _defconfigs() -> list[Path]:
    if not CONFIG_DIR.is_dir():
        pytest.skip(f"{CONFIG_DIR} is not present; the device tree is not acquired")
    found = sorted(CONFIG_DIR.glob("*_defconfig"))
    assert found, f"no *_defconfig under {CONFIG_DIR}"
    return found


def _enabled_keys(path: Path) -> set[str]:
    """The ``CONFIG_XNU_*`` keys a defconfig turns on."""
    on = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("CONFIG_XNU_") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip() in ("y", "1", "true"):
            on.add(key.strip())
    return on


def _archives_entries(board: Path) -> list[str]:
    """The ``[kernel] archives`` list of a board.toml, one string per line.

    Parsed rather than grepped for a marker, because the list spans many lines and
    the ``archives = [`` header is on a line of its own -- the first revision of
    this helper required a run marker on the *same* line and therefore reported
    ``build-platform-power.py`` as dormant while it sat in the list.
    """
    if not board.is_file():
        return []
    entries: list[str] = []
    inside = False
    for line in board.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("archives") and "[" in stripped:
            inside = True
        if inside:
            entries.append(line)
            if "]" in stripped:
                inside = False
    return entries


def _invocation_corpus() -> list[str]:
    """Every line of the pinned trees that plausibly *runs* something.

    ``board.toml``'s archives entries plus the run-marker lines of every script
    under both vendors' ``scripts/``.  ``scripts/unixnu/`` is excluded because
    ``tree.py:29`` does not copy it into the assembled tree.  ``build-arm-driver.py``
    is excluded as redundant depth; see the note on ``DRIVER_SCRIPT``.
    """
    parts: list[str] = []
    for root in VENDOR_ROOTS:
        parts += _archives_entries(root / "board.toml")
    for root in VENDOR_ROOTS:
        scripts = root / "scripts"
        if not scripts.is_dir():
            continue
        for path in sorted(scripts.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".sh"):
                continue
            if path.name == DRIVER_SCRIPT or "unixnu" in path.parts:
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if any(marker in line for marker in RUN_MARKERS):
                    parts.append(line)
    return parts


def _branch_body(lines: list[str], start: int) -> list[str]:
    """The lines of the ``if`` whose ``= 1 ]`` guard is at ``start``.

    Walks forward to the matching ``fi``.  Indentation is what identifies it: the
    build script nests the archive block inside ``:545``'s guard, so the inner
    ``fi`` lines are indented and the outer one is not -- and the console's own
    guard at ``:666`` is indented two spaces with its ``fi`` at ``:671``.  A
    "first line starting with fi" rule would stop at the inner ones.
    """
    guard = lines[start]
    indent = len(guard) - len(guard.lstrip())
    body: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped == "fi" or stripped.startswith("fi;"):
            if (len(line) - len(line.lstrip())) <= indent:
                break
        body.append(line)
    return body


def _is_invoked(token: str, is_driver_component: bool, corpus: list[str]) -> bool:
    """Does anything in the pinned trees run ``token``?

    For a component of the driver script the token is an argv element that
    *follows* the script name on the same line -- ``"build-arm-driver.py
    ArmDTPlatform"`` in board.toml, ``step(ROOT / 'scripts/build-arm-driver.py',
    'NativeVirtioBlock', ...)`` in Python.  For a whole script the token is the
    script name itself.
    """
    for line in corpus:
        if is_driver_component:
            if DRIVER_SCRIPT in line and token in line:
                return True
        elif token in line:
            return True
    return False


def test_the_archive_table_describes_the_build_script():
    """Every row of the table is really a flag whose ``= 1`` branch needs that archive.

    Read back in three steps, because the build script does not spell things the
    way the table does:

    1. the archive path appears verbatim in the script;
    2. the flag's variable is either renamed (``NAME=${XNU_<suffix>:-0}``) or
       spelled inline (``${XNU_<suffix>:-0}``) -- R9e's guard failed on its own
       transcription here, because it assumed the rename;
    3. at least one ``= 1`` guard mentions that variable, and it is the archive
       block's guard, not only the CFLAG block's.

    A table that drifts would report a dependency that does not exist, so this
    runs before the guard and fails first.
    """
    if not BUILD_SCRIPT.is_file():
        pytest.skip(f"{BUILD_SCRIPT} is not present; the UniXNU tree is not acquired")
    lines = BUILD_SCRIPT.read_text(encoding="utf-8", errors="replace").splitlines()

    stale = []
    for key, (archive, _token, _is_component) in ARCHIVE_BUILDERS.items():
        suffix = key[len("CONFIG_XNU_"):]
        variable = "XNU_" + suffix
        if archive not in "\n".join(lines):
            stale.append(f"{key}: {archive} does not appear in {BUILD_SCRIPT.name}")
            continue

        # (2) the rename, if the script makes one.  ``\b`` matters:
        # ``${XNU_NATIVE_AES_ARCHIVE:-...}`` starts with ``${XNU_NATIVE_AES``,
        # so a plain substring test reads the archive-path override as a rename
        # of the flag and then looks for a guard on a variable that is never
        # tested.  A word boundary rejects the continuation and accepts both
        # ``${XNU_...:-0}`` and ``${XNU_...}``.
        spelled = re.compile(r"\$\{" + re.escape(variable) + r"\b")
        renamed = None
        for line in lines:
            head, sep, tail = line.partition("=")
            if not sep or "${" not in tail:
                continue
            if not spelled.search(tail):
                continue
            candidate = head.strip()
            if candidate and candidate.replace("_", "").isalnum():
                renamed = candidate
                break

        # (3) a `= 1` guard that tests either spelling, whose OWN branch body
        # names the archive.  Both spellings are needed: some flags are tested
        # inline (``${XNU_NATIVE_AES:-0}``), others are renamed first and tested
        # as ``$NATIVE_VIRTIO_CONSOLE`` -- which is exactly the transcription
        # R9e's guard got wrong on its own first run.
        #
        # Requiring the archive inside the branch body is what makes this more
        # than "the path appears somewhere in an 787-line file".  The console has
        # TWO `= 1` guards: ``:197`` installs a CFLAG and ``:666`` requires the
        # archive.  Accepting either would let a row map a flag to an archive it
        # never links, which is the drift this check exists to catch.
        renamed_spelled = re.compile(r"\$" + re.escape(renamed) + r"\b") if renamed else None
        guard_indices = [
            index for index, line in enumerate(lines)
            if re.search(r"=\s*1\s*\]", line)
            and (spelled.search(line) or (renamed_spelled and renamed_spelled.search(line)))
        ]
        archive_guard = next(
            (index + 1 for index in guard_indices
             if any(archive in line for line in _branch_body(lines, index))),
            None,
        )
        if archive_guard is None:
            stale.append(
                f"{key}: {archive} appears in {BUILD_SCRIPT.name} but no `= 1` guard on "
                + (f"${variable} or ${renamed}" if renamed else f"${variable}")
                + " has it inside its branch body"
            )
    assert not stale, (
        "the archive table no longer describes the build script:\n  "
        + "\n  ".join(stale)
    )


def test_every_enabled_archive_flag_has_an_invoked_builder():
    """The guard: a defconfig cannot require an archive nothing builds.

    This is the generalisation of ``test_boot_progress_config_guard.py`` from
    "the script is missing" to "the script is present but nobody runs it".

    The positive controls are the point: ``qemu_root_defconfig`` enables eleven
    of these keys and all eleven resolve, so a detector that reported everything
    dormant would fail here rather than pass quietly.
    """
    if not BUILD_SCRIPT.is_file():
        pytest.skip(f"{BUILD_SCRIPT} is not present; the UniXNU tree is not acquired")
    corpus = _invocation_corpus()
    assert corpus, "no invocation corpus: neither vendor tree has scripts/ or board.toml"

    unresolved = []
    resolved = 0
    for path in _defconfigs():
        enabled = _enabled_keys(path)
        for key, (archive, token, is_component) in ARCHIVE_BUILDERS.items():
            if key not in enabled:
                continue
            if _is_invoked(token, is_component, corpus):
                resolved += 1
                continue
            unresolved.append(
                f"{path.name} sets {key}=y, which requires {archive}, but nothing "
                f"in the pinned trees runs {token}"
            )
    assert resolved >= 8, (
        f"only {resolved} enabled archive flags resolved to an invoked builder; the "
        "detector is probably broken (it should find at least the eleven "
        "qemu_root_defconfig enables)"
    )
    assert not unresolved, (
        f"{len(unresolved)} defconfig flag(s) require an archive nothing builds:\n  "
        + "\n  ".join(unresolved)
        + "\n\nEither add the builder invocation (for the driver script that means "
        "a `build-arm-driver.py <Component>` entry in vendor/UniXNU-qemu/board.toml "
        "[kernel] archives) or turn the flag off.  As-is the build dies on a bare "
        "`test -f` inside build-xnu-4k-baseline.sh's archive block, naming the path "
        "but not the reason, after acquisition, the IPSW, the GPU addon chain and a "
        "full XNU build."
    )


def test_the_dormant_keys_are_not_enabled_and_stay_that_way():
    """The state pin, with teeth in both directions.

    Two assertions, and the second is why the dormant set is an *equality* rather
    than a non-emptiness check:

    * no defconfig enables a dormant key -- this is what goes red the moment
      someone adds ``CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y``, which is exactly the
      change R9e's investigation proposed for a different flag;
    * the dormant set the tree actually has equals ``DORMANT_KEYS``.  A
      non-emptiness check would pass when the console builder gets wired up,
      because the other three dormant keys would still satisfy it -- measured:
      that mutant survived the first revision of this test.  Equality fails in
      both directions, so wiring the console up *and* deleting the console from
      the list are both red, and neither can be done quietly.
    """
    if not BUILD_SCRIPT.is_file():
        pytest.skip(f"{BUILD_SCRIPT} is not present; the UniXNU tree is not acquired")
    corpus = _invocation_corpus()

    computed = {
        key for key in ARCHIVE_BUILDERS
        if not _is_invoked(ARCHIVE_BUILDERS[key][1], ARCHIVE_BUILDERS[key][2], corpus)
    }

    enabled_dormant = []
    for path in _defconfigs():
        enabled = _enabled_keys(path)
        for key in sorted(computed & enabled):
            enabled_dormant.append(f"{path.name} sets {key}=y")

    assert not enabled_dormant, (
        "defconfig(s) enable a flag whose archive builder is present in the pinned "
        "tree but is never invoked:\n  " + "\n  ".join(enabled_dormant)
        + "\n\nMeasured 2026-10-31 (docs/kernel-tree-completeness-20261031.md): the "
        "builder exists -- vendor/UniXNU-qemu/scripts/build-arm-driver.py lists the "
        "component at :11 and writes the archive at :106-108 -- and the source "
        "exists at vendor/UniXNU/drivers/NativeVirtioConsole/, pinned at "
        "config/sources.lock.json:144-146.  What is missing is the invocation: add "
        "`build-arm-driver.py NativeVirtioConsole` to board.toml's [kernel] "
        "archives (which scripts/unixnu/kernel.py:101 runs before the link at :102), "
        "then update DORMANT_KEYS here."
    )
    assert computed == DORMANT_KEYS, (
        "the dormant set moved and DORMANT_KEYS was not updated:\n"
        f"  wired up since this list was written: {sorted(DORMANT_KEYS - computed)}\n"
        f"  newly dormant: {sorted(computed - DORMANT_KEYS)}\n"
        "\nBoth directions are red on purpose.  A key that gained a builder is "
        "progress, but leaving it in the list would make this test assert something "
        "untrue; a key that lost one is a regression.  Re-read "
        "docs/kernel-tree-completeness-20261031.md and edit DORMANT_KEYS."
    )
