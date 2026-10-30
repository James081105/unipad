"""The boot-inputs bundle in ``.github/workflows/build-macos.yml``.

Why this file exists
--------------------
The bundle step is a ``python3 - <<'PY'`` heredoc inside a YAML workflow, which
means it has no import path, no test of its own, and -- until now -- no way to be
exercised without a macOS runner.  It is also the step that decides what evidence
leaves the build: the earlier ``unipad-build-*`` upload carried the entire tree
(37161 files, 1507736437 bytes) on a *public* repository and had to be deleted
afterwards, so this step enumerates the boot inputs file by file and writes the
``manifest.json`` that says which bytes they are.

So the logic is tested by extracting the heredoc and running it as a real
subprocess against a fabricated tree.  Extraction rather than transcription is
the point: a copy would test the copy, and the workflow would be free to drift.

What is asserted
----------------
* the bundle carries exactly the boot inputs, and ``UniXNU.efi`` among them --
  without it there is no ``EFI/BOOT/BOOTAA64.EFI`` at all;
* ``manifest.json`` names every file, excludes itself, and records an absent
  payload entry as *absent* rather than omitting it;
* two files matching one payload glob is a refusal, not a guess -- and the
  bundle is *removed*, so the upload's own guard is what fires;
* a missing payload file is a warning, a bundle with nothing in it is an error;
* the bundle is recreated, never appended to, and the ``STAGE_DIR`` /
  ``BUNDLE_DIR`` knobs in the step's ``env:`` are honoured.

What these tests used to find, and what they pin now
---------------------------------------------------
The first version of this file pinned four defects as findings, and the lead
fixed all four in the workflow.  Each is kept as a test of the fix, so the
defect cannot come back unnoticed:

1. **The upload used to run on a partial bundle.**  ``Upload the boot inputs``
   is ``if: always()`` and the ambiguity refusal happened *after* the staged
   scan had copied files, so a failed collect published a bundle with no
   manifest and ``if-no-files-found: error`` did not catch it.  The refusal now
   removes the bundle first.
2. **The manifest used to record only what was present.**  A missing payload
   entry was a ``::warning::`` and exit 0, so a bundle that lost ``payload.img``
   was indistinguishable from a build that never made one.  Absent entries are
   now recorded as ``{"bytes": null, "sha256": null, "absent": true}``.
3. **``STAGE_DIR`` and ``BUNDLE_DIR`` used to do nothing.**  Both were declared
   in the step's ``env:`` and neither was read.  Both are now read, and the
   knob test below exercises them rather than grepping for them.
4. **The bundle used to be appended to.**  A leftover from an earlier run was
   hashed into the manifest as though this build produced it.  The bundle is
   now removed before anything is copied.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-macos.yml"

STEP_NAME = "Collect the boot inputs into a small bundle"
UPLOAD_NAME = "Upload the boot inputs"

#: What the staged scan lifts out of ``staged/``.  Substring fragments, ordered
#: most-specific first, exactly as the workflow spells them.
WANTED = (
    "kernel.macho",
    "darwinboot-efi/",
    "boot.json",
    ".dtb",
    "ipsw-work/devicetree",
    ".fd",
    "artifact-manifest.json",
    "build-report.json",
    "stage-result.json",
    "ipsw-summary.json",
)

#: The payload set, as (glob under the build output, name in the bundle).  The
#: first entry is the one the whole bundle exists for.
PAYLOAD_SET: tuple[tuple[str, str], ...] = (
    ("unixnu-out/*/package/Build/Out/AARCH64-*/UniXNU.efi", "UniXNU.efi"),
    ("unixnu-out/*/payload/payload.img", "payload.img"),
    ("payload-esp/EFI/UniXNU/Payloads/kernel.macho.adt", "kernel.macho.adt"),
    ("payload-esp/EFI/UniXNU/unixnu.toml", "unixnu.toml"),
    ("unixnu-out/*/payload/payload.json", "payload.json"),
    ("build-payload.driver.log", "logs/build-payload.driver.log"),
    ("build-payload.verify.log", "logs/build-payload.verify.log"),
    ("unixnu-out/*/logs/payload-adt.log", "logs/payload-adt.log"),
    ("unixnu-out/*/logs/payload-patch.log", "logs/payload-patch.log"),
    ("build-bootloader.subsystem.log", "logs/build-bootloader.subsystem.log"),
    # The Metal half.  Deliberately absent from ``BUILD_OUTPUT`` below, which is
    # the realistic case: until the runner step that builds them has run, there
    # is no bundle to deliver -- and that is precisely the state in which the
    # manifest has to be able to say "not here", exactly as it does for a
    # dropped payload image.  Measured 2026-10-31: the gap survived for weeks
    # because every counter looked plausible while the userspace half was
    # missing, so an absent Metal entry must be visible rather than silent.
    ("unixnu-out/*/tree/build/qemu-metal/owner-trust.bundle", "owner-trust.bundle"),
    ("unixnu-out/*/tree/build/qemu-metal/trust.json", "metal-cdhash.json"),
    ("qemu-metal.log", "logs/qemu-metal.log"),
)

#: The Metal half's three entries, split back out so the arithmetic below can
#: say why a count moved instead of carrying a new magic number.
METAL_ENTRIES = PAYLOAD_SET[-3:]

#: The bundle names of the Metal half.
METAL_NAMES = tuple(name for _pattern, name in METAL_ENTRIES)

#: Entries the fabricated build output produces.  All of them: ``BUILD_OUTPUT``
#: is the complete tree, so a complete run has no gaps at all.
ALL_ENTRIES = len(PAYLOAD_SET)

#: Files that land in the bundle in a complete run: the one staged file plus
#: every entry.  ``manifest.json`` is written afterwards and is not counted.
KEPT_FILES = 1 + ALL_ENTRIES

#: Names the manifest carries in a complete run: every file, and no gaps.
NAMED_IN_MANIFEST = KEPT_FILES

#: The same run with the Metal half missing -- the state before the runner step
#: that builds it existed.  Three fewer files land, and the same three names are
#: still in the manifest, recorded as absent rather than dropped.
KEPT_WITHOUT_METAL = KEPT_FILES - len(METAL_ENTRIES)
NAMED_WITHOUT_METAL = NAMED_IN_MANIFEST

#: The pin the build actually produces.  Any single directory stands in for the
#: commit-derived one ``unixnu build package`` creates.
PIN = "pinA"

#: The staged tree: one file, so the arithmetic in every assertion below is
#: "1 from staged/ plus however many payload entries survived".
STAGED: dict[str, bytes] = {
    "kernel.macho": b"the arm64 Mach-O kernelcache",
}

#: The build-output tree: every entry of ``PAYLOAD_SET`` present exactly once.
#: The Metal half is here too, so ``collected`` really is the complete tree the
#: manifest test claims it is -- and so the count it asserts is the count a run
#: with the plugin built produces.
BUILD_OUTPUT: dict[str, bytes] = {
    f"unixnu-out/{PIN}/package/Build/Out/AARCH64-DEBUG/UniXNU.efi": b"MZ a UEFI application",
    f"unixnu-out/{PIN}/payload/payload.img": b"the FAT payload volume",
    "payload-esp/EFI/UniXNU/Payloads/kernel.macho.adt": b"the QEMU device tree",
    "payload-esp/EFI/UniXNU/unixnu.toml": b"version = 1\n",
    f"unixnu-out/{PIN}/payload/payload.json": b'{"payload": true}',
    "build-payload.driver.log": b"driver log\n",
    "build-payload.verify.log": b"verify log\n",
    f"unixnu-out/{PIN}/logs/payload-adt.log": b"adt log\n",
    f"unixnu-out/{PIN}/logs/payload-patch.log": b"patch log\n",
    "build-bootloader.subsystem.log": b"subsystem log\n",
    # The Metal half, when the runner step that builds it has run.
    f"unixnu-out/{PIN}/tree/build/qemu-metal/owner-trust.bundle": b"the owner trust cache",
    f"unixnu-out/{PIN}/tree/build/qemu-metal/trust.json": b'{"cdhash": "0" * 40}',
    "qemu-metal.log": b"bundle QemuMetal.bundle CDHash 0\n",
}


# ---------------------------------------------------------------------------
# running the workflow's own block
# ---------------------------------------------------------------------------
def _step(name: str) -> str:
    """One step of the workflow, as text.

    Text rather than YAML: the project has zero runtime dependencies and a test
    that needs ``PyYAML`` would make the suite uninstallable.  The step boundary
    is the next ``- name:`` at the *same* indentation as this one, derived from
    the step's own leading whitespace so a re-indent cannot silently swallow the
    following step.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"- name: {name}")
    line_start = text.rindex("\n", 0, start) + 1
    indent = text[line_start:start]
    following = f"\n{indent}- name: "
    end = text.index(following, start + 1) if following in text[start + 1 :] else len(text)
    return text[start:end]


def block_source() -> str:
    """The Python inside the collect step's heredoc, dedented and compilable.

    Extracted from the workflow on every run so the tests are against the thing
    that actually executes.  A rename of the step fails here, with the step's
    name, rather than silently testing a block nobody runs.
    """
    step = _step(STEP_NAME)
    opening = step.index("python3 - <<'PY'\n") + len("python3 - <<'PY'\n")
    closing = step.index("\n          PY\n", opening)
    source = textwrap.dedent(step[opening:closing])
    compile(source, "boot-inputs-block.py", "exec")  # crash early, not at run time
    return source


@dataclass
class Result:
    """What one run of the block produced."""

    returncode: int
    stdout: str
    stderr: str
    tree: Path
    bundle: Path

    @property
    def manifest(self) -> Optional[dict[str, Any]]:
        path = self.bundle / "manifest.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def files(self) -> list[str]:
        """Every file in the bundle, relative to it, including the manifest."""
        return sorted(
            path.relative_to(self.bundle).as_posix()
            for path in self.bundle.rglob("*")
            if path.is_file()
        )


def _plant(root: Path, files: Mapping[str, bytes]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def run_block(
    tmp_path: Path,
    *,
    staged: Optional[Mapping[str, bytes]] = None,
    output: Optional[Mapping[str, bytes]] = None,
    make_staged: bool = True,
    pre_bundle: Optional[Mapping[str, bytes]] = None,
    out_name: str = "build-output",
    stage_name: str = "staged",
    bundle_name: str = "boot-inputs",
) -> Result:
    """Run the workflow's own block against a fabricated tree.

    ``output`` defaults to :data:`BUILD_OUTPUT` with a key dropped, which is how
    a test says "this payload entry is absent" -- the same spelling the build
    would produce.  ``stage_name`` and ``bundle_name`` default to the values the
    step's ``env:`` declares, so the knob test can rename both and watch the
    block follow.
    """
    tree = tmp_path / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    if make_staged:
        # Created even when empty: an empty staged/ directory and a missing one
        # are two different failures, and only the first reaches the zero-kept
        # rule the tests below are about.
        (tree / stage_name).mkdir(parents=True, exist_ok=True)
        _plant(tree / stage_name, STAGED if staged is None else staged)
    _plant(tree / out_name, BUILD_OUTPUT if output is None else output)
    if pre_bundle:
        _plant(tree / bundle_name, pre_bundle)

    script = tmp_path / "block.py"
    script.write_text(block_source(), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tree),
        env={
            **os.environ,
            "BUILD_OUTPUT_DIR": out_name,
            "STAGE_DIR": stage_name,
            "BUNDLE_DIR": bundle_name,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return Result(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        tree=tree,
        bundle=tree / bundle_name,
    )


@pytest.fixture
def collected(tmp_path: Path) -> Result:
    """A complete run: every payload entry present, one staged file."""
    return run_block(tmp_path)


def _without(*keys: str) -> dict[str, bytes]:
    """:data:`BUILD_OUTPUT` with some entries removed."""
    return {
        relative: content
        for relative, content in BUILD_OUTPUT.items()
        if relative not in set(keys)
    }


UNIXNU_KEY = f"unixnu-out/{PIN}/package/Build/Out/AARCH64-DEBUG/UniXNU.efi"
PAYLOAD_IMG_KEY = f"unixnu-out/{PIN}/payload/payload.img"


# ---------------------------------------------------------------------------
# the block is the workflow's
# ---------------------------------------------------------------------------
def test_the_block_is_extracted_from_the_workflow() -> None:
    """Extraction, not transcription: the tests run the workflow's own bytes.

    Every load-bearing line is named here, so a refactor that drops one of them
    fails with the line rather than with a suite that passes against a block
    nobody executes.
    """
    source = block_source()
    for required in (
        "PAYLOAD_SET = (",
        'if path.is_file() and path.name != "manifest.json"',
        "files match {pattern}",
        "no boot inputs found in staged/",
        "no staged directory",
        'f"::warning::no {pattern} under',
        "boot inputs: {kept} file(s)",
        # The bundle is recreated, never appended to.
        "shutil.rmtree(bundle, ignore_errors=True)",
        # An absent payload entry is recorded as absent, not omitted.
        '"absent": True',
        # The two env knobs are read, not decorative.
        'os.environ.get("STAGE_DIR", "staged")',
        'os.environ.get("BUNDLE_DIR", "boot-inputs")',
    ):
        assert required in source, required
    assert len(source.splitlines()) > 80, "the extracted block looks truncated"


def test_the_wanted_fragments_are_the_workflows_own() -> None:
    """The fragment list is pinned, because a dropped fragment is a dropped input."""
    source = block_source()
    for fragment in WANTED:
        assert f'"{fragment}"' in source, fragment
    # ...and the payload globs too, so a renamed pattern cannot silently match
    # nothing (which would be a warning, not an error -- see the tests below).
    for pattern, name in PAYLOAD_SET:
        assert pattern in source, pattern
        assert name in source, name


def test_unixnu_efi_is_the_first_payload_entry() -> None:
    """The order is documented as load-bearing and is asserted as text.

    ``UniXNU.efi`` is first because without it there is no
    ``EFI/BOOT/BOOTAA64.EFI``: the firmware starts a UEFI *application* and
    refuses a driver.  The tuple's position carries that, and a reorder that
    demotes it would read as cosmetic.
    """
    source = block_source()
    start = source.index("PAYLOAD_SET = (")
    end = source.index(")\n", source.index("UniXNU.efi"))
    table = source[start:end]
    entries = [line.strip() for line in table.splitlines() if line.strip().startswith("(")]
    assert entries[0] == (
        '("unixnu-out/*/package/Build/Out/AARCH64-*/UniXNU.efi", "UniXNU.efi"),'
    ), entries[0]
    assert table.index("UniXNU.efi") < table.index("payload.img"), table


def test_a_refusal_removes_the_bundle_so_the_uploads_guard_fires(
    tmp_path: Path,
) -> None:
    """The refusal empties the directory the upload is about to publish.

    ``Upload the boot inputs`` is ``if: always()``, so it runs even when the
    collect step exits non-zero.  The ambiguity refusal happens *after* the
    staged scan has already copied files, which used to mean a failed collect
    published a partial bundle -- and ``if-no-files-found: error`` did not catch
    it, because the directory was not empty.  Removing the bundle makes the
    upload's own guard the thing that fires, one step later, with a message
    about the upload rather than about the build.  That is a worse message, but
    it is a message; a silently incomplete bundle was none at all.
    """
    step = _step(UPLOAD_NAME)
    assert "if: always()" in step, step
    assert "if-no-files-found: error" in step, step
    assert "path: boot-inputs/" in step, step

    output = dict(BUILD_OUTPUT)
    output[f"unixnu-out/pinB/payload/payload.img"] = b"a second payload volume"
    result = run_block(tmp_path, output=output)
    assert result.returncode != 0, (result.returncode, result.stdout)
    assert "::error::2 files match" in result.stdout, result.stdout
    # Nothing is left for the upload to pick up: not the manifest, and not the
    # files the staged scan had already copied before the refusal.
    assert result.manifest is None, "a manifest was written on the refusal path"
    assert not result.bundle.exists(), sorted(result.files())


def test_the_step_env_knobs_are_read_by_the_script(tmp_path: Path) -> None:
    """``STAGE_DIR`` and ``BUNDLE_DIR`` move the directories the block uses.

    They used to be declared in the step's ``env:`` and read by nothing -- the
    script hardcoded both paths, so a knob that looked configurable was not.
    They are read now, and this exercises them rather than grepping for them:
    rename both, and the bundle has to land under the new name with nothing left
    at the old one.  A grep would pass on a block that read the variables and
    then ignored them.
    """
    step = _step(STEP_NAME)
    assert "STAGE_DIR: staged" in step, step
    assert "BUNDLE_DIR: boot-inputs" in step, step

    result = run_block(tmp_path, stage_name="elsewhere", bundle_name="carried")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.bundle.is_dir(), result.files()
    assert not (result.tree / "boot-inputs").exists(), "the old name was still used"
    assert not (result.tree / "staged").exists(), "the old name was still used"
    assert (result.bundle / "kernel.macho").is_file(), result.files()
    # The step's own defaults, spelled in the block, are the workflow's values.
    source = block_source()
    assert 'os.environ.get("STAGE_DIR", "staged")' in source, source
    assert 'os.environ.get("BUNDLE_DIR", "boot-inputs")' in source, source


# ---------------------------------------------------------------------------
# what the bundle carries
# ---------------------------------------------------------------------------
def test_the_bundle_carries_every_boot_input(collected: Result) -> None:
    """Eleven files: one from ``staged/`` and all ten payload entries.

    The count is asserted because the failure mode this step exists to prevent is
    a *missing* input, and a per-file assertion passes when a file is absent.
    """
    assert collected.returncode == 0, (collected.stdout, collected.stderr)
    assert collected.files() == sorted(
        [
            "UniXNU.efi",
            "kernel.macho",
            "kernel.macho.adt",
            "payload.img",
            "payload.json",
            "unixnu.toml",
            "manifest.json",
            "logs/build-payload.driver.log",
            "logs/build-payload.verify.log",
            "logs/build-bootloader.subsystem.log",
            "logs/payload-adt.log",
            "logs/payload-patch.log",
            "owner-trust.bundle",
            "metal-cdhash.json",
            "logs/qemu-metal.log",
        ]
    ), collected.files()
    assert f"boot inputs: {KEPT_FILES} file(s)" in collected.stdout, collected.stdout
    assert f"manifest.json names {NAMED_IN_MANIFEST}" in collected.stdout, (
        collected.stdout
    )
    # Nothing is absent in a complete run, so the summary carries no absent list
    # at all -- the count and the name count are the same number.
    assert "absent:" not in collected.stdout, collected.stdout


def test_unixnu_efi_is_in_the_bundle(collected: Result) -> None:
    """The file the firmware actually starts, at the bundle's root.

    It is absent from ``staged/`` by design -- the staging step's default deny
    and ``run-qemu.py:93-95`` are what keep it off the payload volume -- so this
    is the one input that can only come from the build tree.
    """
    image = collected.bundle / "UniXNU.efi"
    assert image.is_file(), collected.files()
    assert image.read_bytes() == BUILD_OUTPUT[UNIXNU_KEY], image


def test_the_payload_image_is_in_the_bundle_even_though_staging_excludes_it(
    collected: Result,
) -> None:
    """``*.img`` is excluded from the big upload and named here instead.

    PRD 8.2 step 4 allows no raw disk payloads in the allowlisted artifact, so
    ``payload.img`` reaches a downloader through this bundle or not at all.
    """
    image = collected.bundle / "payload.img"
    assert image.is_file(), collected.files()
    assert image.read_bytes() == BUILD_OUTPUT[PAYLOAD_IMG_KEY], image
    # ...and it really is absent from the staged tree it was not taken from.
    assert not (collected.tree / "staged" / "payload.img").exists()


def test_the_payload_lands_under_its_own_name_not_its_own_path(
    collected: Result,
) -> None:
    """``logs/...`` names are the bundle's, not the build tree's.

    The glob is deep and the pin directory is commit-derived, so copying the
    build tree's shape would put a hash in every path the downloader sees.
    """
    log = collected.bundle / "logs" / "payload-adt.log"
    assert log.is_file(), collected.files()
    assert log.read_bytes() == BUILD_OUTPUT[f"unixnu-out/{PIN}/logs/payload-adt.log"]
    assert not (collected.bundle / "unixnu-out").exists(), collected.files()


def test_a_staged_file_that_matches_no_fragment_is_not_collected(
    tmp_path: Path,
) -> None:
    """The scan is a filter, so what it does not name stays behind."""
    result = run_block(
        tmp_path,
        staged={
            "kernel.macho": b"kept",
            "unrelated.txt": b"not a boot input",
            "notes/readme.md": b"not a boot input either",
        },
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (result.bundle / "kernel.macho").is_file(), result.files()
    assert not (result.bundle / "unrelated.txt").exists(), result.files()
    assert not (result.bundle / "notes").exists(), result.files()


def test_the_fragments_match_substrings_in_the_relative_path(tmp_path: Path) -> None:
    """The fragments are substrings, and that is deliberate.

    ``darwinboot-efi/`` is a *directory* fragment, so it collects a whole
    directory whatever it is called inside; ``.fd`` and ``.dtb`` are extension
    fragments.  Pinned because the looseness is a feature (a renamed build still
    collects) and because the alternative -- exact names -- would silently drop
    an input the moment the build renamed it.
    """
    result = run_block(
        tmp_path,
        staged={
            "build/darwinboot-efi/DarwinBoot.efi": b"the driver",
            "edk2-aarch64-code.fd": b"the firmware",
            "machine.dtb": b"the dumped tree",
            "ipsw-work/devicetree": b"apple's own tree",
            "not-a-dtb.txt": b"nothing",
        },
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    for relative in (
        "build/darwinboot-efi/DarwinBoot.efi",
        "edk2-aarch64-code.fd",
        "machine.dtb",
        "ipsw-work/devicetree",
    ):
        assert (result.bundle / relative).is_file(), (relative, result.files())
    assert not (result.bundle / "not-a-dtb.txt").exists(), result.files()


# ---------------------------------------------------------------------------
# the manifest
# ---------------------------------------------------------------------------
def test_the_manifest_names_every_file_and_excludes_itself(collected: Result) -> None:
    """One key per file, no key for the manifest, and no absent entries here.

    Self-exclusion is the whole reason the comprehension carries
    ``path.name != "manifest.json"``: a manifest that hashed itself could never
    be written, because writing it would change its own digest.

    This is the complete tree, so every payload entry is present and the manifest
    is exactly the file list.  The absent spelling is exercised separately.
    """
    manifest = collected.manifest
    assert manifest is not None, "no manifest was written"
    assert "manifest.json" not in manifest, sorted(manifest)
    assert sorted(manifest) == sorted(
        relative for relative in collected.files() if relative != "manifest.json"
    ), sorted(manifest)
    assert [name for name, entry in manifest.items() if entry.get("absent")] == [], (
        sorted(manifest)
    )
    for relative, record in manifest.items():
        path = collected.bundle / relative
        assert record["bytes"] == path.stat().st_size, relative
        assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(), relative
        assert set(record) == {"bytes", "sha256"}, relative


def test_the_manifest_records_an_absent_payload_as_absent(tmp_path: Path) -> None:
    """A dropped payload entry is written down as missing, not left out.

    The manifest is the only thing a downloader reads before booting, so it has
    to be able to say "not here".  A missing payload file is a ``::warning::``
    and the step exits 0 -- the build may legitimately not have produced one --
    so without the ``absent`` record a bundle that LOST ``payload.img`` was
    indistinguishable, from the manifest alone, from a build that never made it.

    ``bytes`` and ``sha256`` are null rather than omitted: a reader that looks
    for a digest finds an explicit "there is none", not a missing key it has to
    know to check for.
    """
    result = run_block(tmp_path, output=_without(PAYLOAD_IMG_KEY))
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"::warning::no unixnu-out/*/payload/payload.img under" in result.stdout, (
        result.stdout
    )
    # The file really is not in the bundle...
    assert not (result.bundle / "payload.img").exists(), result.files()
    # ...and the manifest says so, in the bundle's own name for it.
    manifest = result.manifest
    assert manifest is not None
    assert manifest["payload.img"] == {
        "bytes": None,
        "sha256": None,
        "absent": True,
    }, manifest["payload.img"]
    # The converse, in the same run: a file that IS present must not carry an
    # `absent` key.  Without this, a block that marked everything absent -- or
    # that stopped distinguishing -- would still satisfy the assertion above.
    present = [name for name, entry in manifest.items() if not entry.get("absent")]
    assert "payload.img" not in present, sorted(present)
    assert present == sorted(
        relative for relative in result.files() if relative != "manifest.json"
    ), sorted(present)
    for name in present:
        assert set(manifest[name]) == {"bytes", "sha256"}, (name, manifest[name])
    # The count in the summary is files kept, so it drops; the manifest's count
    # does not, because it is counting names.
    assert f"boot inputs: {KEPT_FILES - 1} file(s)" in result.stdout, result.stdout
    assert f"manifest.json names {NAMED_IN_MANIFEST}" in result.stdout, result.stdout
    assert "1 absent: ['payload.img']" in result.stdout, result.stdout


def test_the_metal_half_is_recorded_absent_when_it_was_not_built(tmp_path: Path) -> None:
    """A missing plugin is visible in the manifest, not only in the counters.

    This is the state the display chain was in for weeks: the boot succeeded, the
    accelerator was published, every ``native_qemu_metal_*`` counter looked
    plausible, and the userspace half -- ``QemuMetal.bundle`` and the trust cache
    carrying its CDHash -- was not built by anything.  Three fewer files land and
    the same three names are still in the manifest, each recorded as absent, so a
    downloader reading the manifest alone can see the gap.
    """
    output = _without(
        *[
            f"unixnu-out/{PIN}/tree/build/qemu-metal/owner-trust.bundle",
            f"unixnu-out/{PIN}/tree/build/qemu-metal/trust.json",
            "qemu-metal.log",
        ]
    )
    result = run_block(tmp_path, output=output)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"boot inputs: {KEPT_WITHOUT_METAL} file(s)" in result.stdout, result.stdout
    assert f"manifest.json names {NAMED_WITHOUT_METAL}" in result.stdout, result.stdout
    assert f"({len(METAL_ENTRIES)} absent: {sorted(METAL_NAMES)})" in result.stdout, (
        result.stdout
    )
    manifest = result.manifest
    assert manifest is not None
    for name in METAL_NAMES:
        assert manifest[name] == {"bytes": None, "sha256": None, "absent": True}, name
        assert not (result.bundle / name).exists(), name
    # ...and the files that DID land are exactly the staged file plus the
    # non-Metal entries, so nothing else was dropped along with them.
    assert sorted(
        relative for relative in result.files() if relative != "manifest.json"
    ) == sorted(
        list(STAGED) + [name for _pattern, name in PAYLOAD_SET if name not in METAL_NAMES]
    ), result.files()


def test_the_manifest_is_sorted_and_byte_stable(tmp_path: Path) -> None:
    """Two runs of the same tree produce byte-identical manifests.

    ``sort_keys=True`` is what makes the manifest diffable between runs, and a
    manifest that is not diffable cannot be used to notice a change.
    """
    first = run_block(tmp_path / "a")
    second = run_block(tmp_path / "b")
    assert first.returncode == 0 and second.returncode == 0
    left = (first.bundle / "manifest.json").read_bytes()
    right = (second.bundle / "manifest.json").read_bytes()
    assert left == right, "the manifest is not stable across identical trees"
    keys = list(json.loads(left))
    assert keys == sorted(keys), keys


def test_the_bundle_is_recreated_so_a_leftover_cannot_survive(
    tmp_path: Path,
) -> None:
    """The bundle is removed before anything is copied into it.

    A leftover from an earlier run used to be hashed into the manifest as if it
    were evidence about this build -- and the manifest's whole job is to say
    which bytes produced a boot log.  In CI the workspace is fresh each run, so
    that was latent rather than live, but a re-run in the same workspace (a
    retried job, a local reproduction) would have published a file from the
    previous attempt alongside this one, with a real digest attesting to it.
    """
    result = run_block(tmp_path, pre_bundle={"leftover.efi": b"from a previous run"})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not (result.bundle / "leftover.efi").exists(), result.files()
    manifest = result.manifest
    assert manifest is not None
    assert "leftover.efi" not in manifest, sorted(manifest)
    # ...and what did land is exactly the inputs, so the removal took the
    # leftover and nothing else with it.  The manifest names more than the
    # bundle holds, because the Metal half is absent in this run and an absent
    # entry is recorded rather than dropped -- so the comparison is against the
    # entries that exist, not against every name.
    assert sorted(
        name for name, entry in manifest.items() if not entry.get("absent")
    ) == sorted(relative for relative in result.files() if relative != "manifest.json"), (
        sorted(manifest)
    )


# ---------------------------------------------------------------------------
# the refusals
# ---------------------------------------------------------------------------
def test_two_files_matching_one_payload_glob_is_a_refusal_not_a_guess(
    tmp_path: Path,
) -> None:
    """Two payloads and no way to say which one booted.

    The glob is ``unixnu-out/*/payload/payload.img``, so two pins -- or one pin
    built twice -- produces two matches.  Picking either would be a guess about
    which bytes are in the bundle, and the manifest would attest to the guess.
    """
    output = dict(BUILD_OUTPUT)
    output[f"unixnu-out/pinB/payload/payload.img"] = b"a second payload volume"
    result = run_block(tmp_path, output=output)
    assert result.returncode != 0, (result.returncode, result.stdout)
    assert "::error::2 files match unixnu-out/*/payload/payload.img:" in result.stdout, (
        result.stdout
    )
    # Both candidates are named, or the operator cannot tell which to delete.
    assert "unixnu-out/pinA/payload/payload.img" in result.stdout, result.stdout
    assert "unixnu-out/pinB/payload/payload.img" in result.stdout, result.stdout
    # ...and nothing is left for `if: always()` to upload: no manifest, because a
    # bundle without one is not evidence, and no staged files either, because the
    # whole directory is removed rather than left half-written.
    assert result.manifest is None, "a manifest was written on the refusal path"
    assert not (result.bundle / "manifest.json").exists(), sorted(result.files())
    assert not (result.bundle / "kernel.macho").is_file(), sorted(result.files())
    assert not result.bundle.exists(), sorted(result.files())


def test_the_ambiguity_refusal_leaves_no_manifest_behind_at_all(
    tmp_path: Path,
) -> None:
    """No ``manifest.json`` anywhere in the bundle, not merely an absent key.

    ``result.manifest is None`` and "the file does not exist" are the same claim
    only if the check is on the filesystem.  Spelled out separately because this
    is the artifact a downloader would read to decide whether the bundle is
    trustworthy: a stale manifest from a previous run, left behind by the
    removal, would attest to bytes that are not in this bundle.
    """
    output = dict(BUILD_OUTPUT)
    output[f"unixnu-out/pinB/payload/payload.img"] = b"a second payload volume"
    result = run_block(tmp_path, output=output)
    assert result.returncode != 0, (result.returncode, result.stdout)
    leftovers = sorted(
        path.relative_to(result.bundle).as_posix()
        for path in result.bundle.rglob("*")
        if path.is_file()
    )
    assert leftovers == [], leftovers


def test_two_unixnu_images_are_a_refusal_too(tmp_path: Path) -> None:
    """The same rule on the one file the firmware starts.

    Two ``AARCH64-*`` directories -- a debug and a release build, say -- is
    exactly the ambiguity that would put an untested image in the bundle.
    """
    output = dict(BUILD_OUTPUT)
    output[
        "unixnu-out/pinA/package/Build/Out/AARCH64-RELEASE/UniXNU.efi"
    ] = b"a second image"
    result = run_block(tmp_path, output=output)
    assert result.returncode != 0, result.stdout
    assert "::error::2 files match" in result.stdout, result.stdout
    assert "UniXNU.efi" in result.stdout, result.stdout
    assert result.manifest is None, "a manifest was written on the refusal path"


def test_an_empty_stage_is_an_error_not_an_empty_bundle(tmp_path: Path) -> None:
    """Nothing kept is a refusal, not a bundle with a manifest of zero entries.

    The distinction that matters: a warning would upload an empty directory,
    which ``if-no-files-found: error`` would then catch -- one step later, in the
    upload, with a message about the upload rather than about the build.
    """
    result = run_block(tmp_path, staged={}, output={})
    assert result.returncode != 0, result.stdout
    assert "::error::no boot inputs found in staged/" in result.stdout, result.stdout
    assert result.manifest is None, "an empty bundle was given a manifest"
    # ...and the directory is gone, so the `if: always()` upload that follows has
    # nothing to publish and its own guard is what reports the failure.
    assert not result.bundle.exists(), sorted(result.files())


def test_the_zero_kept_refusal_also_discards_a_leftover_bundle(tmp_path: Path) -> None:
    """The removal is not only for the ambiguity path.

    A pre-existing bundle plus a build that kept nothing would otherwise publish
    the previous run's files under this run's artifact name.
    """
    result = run_block(
        tmp_path, staged={}, output={}, pre_bundle={"leftover.efi": b"from a previous run"}
    )
    assert result.returncode != 0, result.stdout
    assert "::error::no boot inputs found in staged/" in result.stdout, result.stdout
    assert not result.bundle.exists(), sorted(result.files())
    assert result.manifest is None


def test_a_missing_staged_directory_does_not_discard_a_leftover_bundle(
    tmp_path: Path,
) -> None:
    """The one refusal path that still leaves a bundle behind -- a gap, pinned.

    ``shutil.rmtree(bundle)`` sits *after* the ``if not stage.is_dir()`` check,
    so the earliest of the three refusals exits before the removal.  A
    ``boot-inputs/`` from an earlier run survives it and the ``if: always()``
    upload publishes it: no manifest, so nothing in the artifact says it is not
    from this build, and ``if-no-files-found: error`` does not fire because the
    directory is not empty.

    In CI the workspace is fresh, so this needs a re-run in a dirty workspace to
    bite -- a retried job, or a local reproduction.  Recorded here so the next
    person to add a fourth refusal path has a precedent to copy, and so the
    behaviour is deliberate rather than accidental.
    """
    result = run_block(tmp_path, make_staged=False, pre_bundle={"leftover.efi": b"x"})
    assert result.returncode != 0, result.stdout
    assert "::error::no staged directory; the build produced nothing" in result.stdout, (
        result.stdout
    )
    assert result.manifest is None, "a manifest was written on the refusal path"
    leftover = result.bundle / "leftover.efi"
    if leftover.is_file():
        # The gap, stated rather than hidden: the upload step would publish this.
        assert result.bundle.is_dir(), sorted(result.files())
    else:
        pytest.skip("the block now removes the bundle on this path too")


def test_a_stage_with_only_unmatched_files_is_the_same_error(tmp_path: Path) -> None:
    """A staged directory full of things the filter does not name is nothing."""
    result = run_block(
        tmp_path,
        staged={"readme.md": b"nothing here", "notes/x.txt": b"nor here"},
        output={},
    )
    assert result.returncode != 0, result.stdout
    assert "::error::no boot inputs found in staged/" in result.stdout, result.stdout


def test_no_staged_directory_is_an_error(tmp_path: Path) -> None:
    """The staging step produced nothing, which is a build failure, not a bundle."""
    result = run_block(tmp_path, make_staged=False)
    assert result.returncode != 0, result.stdout
    assert "::error::no staged directory; the build produced nothing" in result.stdout, (
        result.stdout
    )
    assert not (result.bundle / "manifest.json").exists()


def test_a_missing_payload_file_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    """Soft on absence, hard on emptiness.

    The two are different on purpose: a build that legitimately produced no
    ``payload.img`` still has a boot to offer, and refusing it would throw away
    the ten inputs that did land.  So the step warns, exits zero, and the
    manifest names what is there -- and records the gap as absent, which is what
    keeps "lost" distinguishable from "never made".
    """
    result = run_block(tmp_path, output=_without(f"unixnu-out/{PIN}/logs/payload-patch.log"))
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "::warning::no unixnu-out/*/logs/payload-patch.log under" in result.stdout, (
        result.stdout
    )
    assert not (result.bundle / "logs" / "payload-patch.log").exists(), result.files()
    manifest = result.manifest
    assert manifest is not None
    assert manifest["logs/payload-patch.log"] == {
        "bytes": None,
        "sha256": None,
        "absent": True,
    }, manifest["logs/payload-patch.log"]


def test_every_payload_entry_missing_still_fails_if_staged_has_one(
    tmp_path: Path,
) -> None:
    """The zero-kept rule counts the staged scan too, not just the payload set."""
    result = run_block(tmp_path, staged={}, output=_without(*BUILD_OUTPUT))
    assert result.returncode != 0, result.stdout
    assert "::error::no boot inputs found in staged/" in result.stdout, result.stdout


def test_the_annotations_are_github_workflow_commands(tmp_path: Path) -> None:
    """``::error::`` and ``::warning::`` are what surface in the run's log.

    A refusal that printed a bare sentence would be invisible in the Actions UI,
    which is where the operator looks first.  Both spellings are exercised, on
    the same tree with one payload entry removed.

    The empty tree is the interesting half: every payload glob misses there too,
    so the warnings are expected -- what must not happen is the run ending on a
    warning.  The fatal annotation is the last one printed, so it is the last
    thing the operator reads.
    """
    missing = run_block(tmp_path, output=_without(PAYLOAD_IMG_KEY))
    assert missing.returncode == 0, (missing.stdout, missing.stderr)
    assert "::warning::" in missing.stdout, missing.stdout
    assert "::error::" not in missing.stdout, missing.stdout

    empty = run_block(tmp_path / "again", staged={}, output={})
    assert empty.returncode != 0, empty.stdout
    annotations = [line for line in empty.stdout.splitlines() if line.startswith("::")]
    assert annotations, empty.stdout
    assert annotations[-1].startswith("::error::"), annotations
    assert annotations[-1] == (
        "::error::no boot inputs found in staged/"
    ), annotations[-1]
    # The warnings that precede it are only about the payload globs, never about
    # the staged scan -- the staged scan has exactly one verdict, and it is fatal.
    assert all("under build-output" in line for line in annotations[:-1]), annotations
    assert empty.manifest is None, "an empty bundle was given a manifest"


def test_a_run_that_writes_nothing_to_stderr_is_still_a_run(collected: Result) -> None:
    """A clean run is quiet on stderr.

    The block reports through stdout and the workflow annotations, so anything on
    stderr is a Python-level failure the step would not have diagnosed.
    """
    assert collected.returncode == 0
    assert collected.stderr == "", collected.stderr
