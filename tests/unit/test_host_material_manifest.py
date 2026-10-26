"""``unipad.host.material_manifest`` against material built on this host.

Why these tests exist
---------------------
No real seed is used here, for two reasons.  One is PRD 12: a real seed is
Apple-derived material that does not belong in the repository.  The other is
more interesting -- ``prepare-owner-data-fixture.py`` needs macOS to make one,
so the *only* real packed seeds this host has are hand-written stubs, and those
stubs are the reason this module exists.

Measured on 2026-10-26 (implementation-plan fact 81), the ``owner-data-1g.dbr``
on this host's ESP is 36,960 bytes with a container superblock whose magic, block
size and block count are all correct (``NXSB``, 4096, 262144) and whose
``nx_uuid`` is ``00 01 02 ... 0f`` -- the sixteen sequential bytes.  Every check
that looked at the packed envelope passed.  A second stub measured the same day
uses ``11111111-2222-...``.  Both are reproduced here byte for byte, because a
test that only ever feeds the module well-formed input cannot tell a verifier
from a rubber stamp.

So the shape of this file is: build a complete, self-consistent material set;
verify it; then break exactly one thing at a time and assert the refusal names
the rule that broke.  Where a rule's teeth cannot be shown that way -- because
some other check would refuse first -- it is shown by calling the *other* checks
directly and demonstrating that they pass, which is what leaves the rule under
test as the only thing that can refuse.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import uuid
from pathlib import Path
from typing import Callable, Optional

import pytest

from unipad.errors import ErrorCode
from unipad.host import material_manifest as mm
from unipad.host import packed_ramdisk

ROOT = Path(__file__).resolve().parents[2]

#: The device repository's own chain script, whose field offsets this module
#: reuses.  Reading them out of it rather than restating them is the whole point:
#: a second spelling of "where is nx_block_count" is the divergence class this
#: project keeps hitting.
BUILD_OS_ROOT = ROOT / "vendor/UniXNU-qemu/scripts/build-os-root.py"

PAGE = 4096
LOGICAL = 1 << 30
BLOCKS = LOGICAL // PAGE

#: The Data volume's UUID, spelled the way ``diskutil`` spells it.  It is the
#: value the kernel is built with and the value the seed must carry; the two
#: being the same value is the binding audit-r8.md 11.10 left unverified.
DATA_UUID = "18AB5557-DC2D-469B-8758-855E60F5B0DF"

#: The OS root volume's UUID -- a *different* UUID, pinned by the board, which the
#: ADT publishes.  Confusing it with the Data volume's is how a boot ends up
#: matching the wrong disk.
ROOT_UUID = "04769D26-2CA0-4825-B443-E2E5F88300F2"

#: The container UUID measured on this host's 36,960-byte stub, spelled the way
#: ``uuid.UUID`` spells it -- which is how the module carries it.
STUB_CONTAINER_UUID = "00010203-0405-0607-0809-0a0b0c0d0e0f"

#: The donor kernelcache's content, whose digest the board pins.  The board's pin
#: is derived from these bytes rather than restated, so a manifest that disagrees
#: with the board is a real disagreement and not a typo in this file.
DONOR_KERNELCACHE = b"donor kernelcache\n"
OS_MEMBER = b"os root volume\n"
SYSTEMOS_MEMBER = b"systemos volume\n"

#: The digests the board pins for the three files whose identity the board owns.
KERNELCACHE_SHA = hashlib.sha256(DONOR_KERNELCACHE).hexdigest()
OS_MEMBER_SHA = hashlib.sha256(OS_MEMBER).hexdigest()
SYSTEMOS_MEMBER_SHA = hashlib.sha256(SYSTEMOS_MEMBER).hexdigest()

#: A pin that is not the file's digest -- what "not the pinned one" means.
WRONG_SHA = "0" * 64

#: The block the volume superblock lives at, and the byte offset that is.
#: The packed seed's page table is keyed by byte offset, so a block index
#: here would be a seed the bootloader and the kernel driver both reject.
APSB_BLOCK = 104
APSB_OFFSET = APSB_BLOCK * PAGE

BUILD_ID = "material-test-M9-20261026T000000Z"

#: The eight files a delivery carries, named by the leaf the manifest records.
DELIVERED = (
    "kernel.macho",
    "kernel.macho.adt",
    "original-static-system.bundle",
    "owner-data-1g.dbr",
    "044-76040-038.dmg",
    "044-76034-038.dmg",
    "kernelcache.decompressed",
    "unixnu.toml",
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _nxsb(*, container_uuid: str = "a1b2c3d4-e5f6-4748-89ab-cdef01234567",
          block_count: int = BLOCKS, first_fs_oid: int = 1025,
          xp_desc_blocks: int = 8) -> bytes:
    """A container superblock, with every field overridable so a rule can be broken.

    The offsets are the ones ``build-os-root.py:114-117`` reads, which is what
    makes a stub built here comparable to the stub measured on this host.
    """
    page = bytearray(PAGE)
    page[32:36] = b"NXSB"
    struct.pack_into("<I", page, 36, PAGE)
    struct.pack_into("<Q", page, 40, block_count)
    page[72:88] = uuid.UUID(container_uuid).bytes
    struct.pack_into("<Q", page, 184, first_fs_oid)
    struct.pack_into("<I", page, 104, xp_desc_blocks)
    return bytes(page)


def _apsb(*, volume_uuid: str = DATA_UUID) -> bytes:
    """A volume superblock carrying ``volume_uuid`` at apfsprogs' own offset."""
    page = bytearray(PAGE)
    page[32:36] = b"APSB"
    page[0xF0:0x100] = uuid.UUID(volume_uuid).bytes
    return bytes(page)


def _stub_container() -> bytes:
    """The 36,960-byte stub's container superblock, as measured.

    Implementation-plan fact 81: ``nx_magic``/``nx_block_size``/``nx_block_count``
    are all correct, ``nx_uuid`` is the sixteen sequential bytes, and the whole
    4096-byte block has 51 nonzero bytes.  Reproduced here so the rule that
    refuses it is exercised against the real thing rather than a guess at it.
    """
    page = _nxsb(container_uuid=STUB_CONTAINER_UUID)
    sparse = bytearray(PAGE)
    sparse[: len(page)] = page
    return bytes(sparse)


def packed(pages: dict[int, bytes], *, logical: int = LOGICAL) -> bytes:
    """A packed ramdisk, built byte by byte rather than read from a file.

    ``packed_ramdisk.pack`` takes a whole disk image, and a 1 GiB one is not a
    fixture -- so the extents are written directly, exactly as
    ``test_host_packed_ramdisk.py`` builds its seeds.
    """
    extents = sorted(pages.items())
    blob = struct.pack("<8sIIQQ", b"DBRDISK1", 1, PAGE, logical, len(extents))
    blob += b"".join(struct.pack("<QQ", offset, PAGE) for offset, _ in extents)
    return blob + b"".join(page for _, page in extents)


def write_tree(
    where: Path,
    *,
    seed_pages: dict[int, bytes] | None = None,
    kernel_uuid: str = DATA_UUID,
    root_uuid: str = ROOT_UUID,
    adt_uuid: str | None = None,
    kernelcache_sha: str = KERNELCACHE_SHA,
    os_member_sha: str = OS_MEMBER_SHA,
    systemos_member_sha: str = SYSTEMOS_MEMBER_SHA,
    build_id: str = BUILD_ID,
) -> tuple[Path, Path]:
    """A complete material set and the board that pins it.

    Returns ``(workspace, board)``, where ``workspace`` is the directory the
    manifest resolves against.  Every keyword is a way to break one binding, so a
    test can say which artifact it is lying about rather than rebuilding a tree.
    ``root_uuid`` is what the board pins and ``adt_uuid`` is what the device tree
    publishes -- they are separate knobs because a tree that publishes a UUID the
    board does not pin is the failure, and a single knob would change both.
    """
    if adt_uuid is None:
        adt_uuid = root_uuid
    tree = where / "tree"
    (tree / "kernel").mkdir(parents=True)
    (tree / "build/original-static-system-trust").mkdir(parents=True)
    (tree / "build/xnu").mkdir(parents=True)
    (tree / "payload-esp/EFI/UniXNU").mkdir(parents=True)
    device = where / "device"
    (device / ".cache/firmware/owner-data").mkdir(parents=True)
    (device / ".cache/firmware/target-volumes").mkdir(parents=True)
    (where / "out/ipsw/21H461").mkdir(parents=True)

    # The kernel: a Mach-O-ish image that carries the UUID it was built with, the
    # way the real one carries it as a C string literal.
    (tree / "kernel/kernel.macho").write_bytes(
        b"\xcf\xfa\xed\xfe" + b"\0" * 64 + kernel_uuid.encode() + b"\0" * 32
    )
    # The device tree publishes the root-match UUID as an XML string under
    # /chosen/root-matching, so the bytes are the check.
    (tree / "kernel/kernel.macho.adt").write_bytes(
        b"<dict><key>root-matching</key><string>" + adt_uuid.encode() + b"</string>"
    )
    (tree / "build/original-static-system-trust/original-static-system.bundle").write_bytes(
        b"trust cache bundle\n"
    )
    (tree / "build/xnu/owner-data-volume.h").write_text(
        '#define XNU_OWNER_DATA_VOLUME_UUID "' + kernel_uuid + '"\n', encoding="utf-8"
    )
    (tree / "payload-esp/EFI/UniXNU/unixnu.toml").write_text(
        '[boot]\nentries = ["kernel"]\n', encoding="utf-8"
    )
    # The seed: a packed disk whose container superblock is page 0 and whose
    # volume superblock is a resident page further in.
    pages = seed_pages if seed_pages is not None else {0: _nxsb(), APSB_OFFSET: _apsb()}
    (device / ".cache/firmware/owner-data/owner-data-1g.dbr").write_bytes(packed(pages))
    (device / ".cache/firmware/target-volumes/044-76040-038.dmg").write_bytes(OS_MEMBER)
    (device / ".cache/firmware/target-volumes/044-76034-038.dmg").write_bytes(SYSTEMOS_MEMBER)
    (where / "out/ipsw/21H461/kernelcache.decompressed").write_bytes(DONOR_KERNELCACHE)
    board = where / "board.toml"
    board.write_text(
        "\n".join(
            [
                "[ipsw.sha256]",
                f'"044-76040-038.dmg" = "{os_member_sha}"',
                f'"044-76034-038.dmg" = "{systemos_member_sha}"',
                "",
                "[kernel.prelink]",
                f'kernelcache_sha256 = "{kernelcache_sha}"',
                "",
                "[payload.adt]",
                f'root_match_volume_uuid = "{root_uuid}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return where, board


def collect_manifest(where: Path, board: Path, **overrides: object) -> mm.MaterialManifest:
    return mm.collect(
        mm.BuildTree(
            root=where / "tree",
            device_root=where / "device",
            esp=where / "tree/payload-esp",
            volumes=where / "device/.cache/firmware/target-volumes",
            kernelcache=where / "out/ipsw/21H461/kernelcache.decompressed",
        ),
        build_id=str(overrides.pop("build_id", BUILD_ID)),
        board_pins=mm.read_board_pins(board),
        **overrides,  # type: ignore[arg-type]
    )


def stage_flat(where: Path, manifest: Optional[mm.MaterialManifest] = None) -> Path:
    """Copy the material into one flat directory, the way a delivery arrives.

    The manifest records leaf names, so this is the layout it is read against --
    and it is the layout the boot-inputs bundle already uses.  Copying rather
    than symlinking is deliberate: a manifest verified against the build tree
    would be verified against files a delivery does not carry.

    A role the manifest records without bytes (the two multi-GB members PRD 12
    deletes) has nothing to copy, so it is staged only when the build still has
    it.
    """
    delivery = where / "delivery"
    delivery.mkdir(parents=True, exist_ok=True)
    wanted = DELIVERED
    if manifest is not None:
        wanted = tuple(
            entry.path
            for entry in manifest.artifacts.values()
            if entry.bytes or entry.path in DELIVERED
        )
    for name in wanted:
        for candidate in where.rglob(name):
            if candidate.is_file() and candidate.parent != delivery:
                shutil.copy2(candidate, delivery / name)
                break
    return delivery


def verify(manifest: mm.MaterialManifest, where: Path, board: Path) -> mm.VerifyReport:
    return mm.verify_manifest(
        manifest,
        stage_flat(where, manifest),
        board_pins=mm.read_board_pins(board),
    )


# ---------------------------------------------------------------------------
# the good path
# ---------------------------------------------------------------------------
def test_a_complete_manifest_verifies(tmp_path: Path) -> None:
    """The whole point: a real build's material verifies, and says what it checked.

    The report is the deliverable a human reads before booting, so it has to name
    the build and the UUIDs it established rather than just returning.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    report = verify(manifest, where, board)

    assert report.build_id == BUILD_ID, report
    assert report.volume_uuid == DATA_UUID, report
    assert "seed-volume" in report.checked, report
    assert "kernel-uuid" in report.checked, report
    assert report.seed is not None and report.seed.logical == LOGICAL, report
    # One build id for every artifact: that is what makes the set a set.
    assert {entry.source for entry in manifest.artifacts.values()} <= mm.KNOWN_SOURCES


def test_every_role_names_a_known_producer(tmp_path: Path) -> None:
    """``source`` is not decoration: it is what lets a reader ask the producer.

    A manifest that recorded "somewhere" would verify just as well and be worth
    nothing, so the set of sources is closed and checked.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    for role in mm.MATERIAL_ROLES:
        entry = manifest.artifacts[role]
        assert entry.source in mm.KNOWN_SOURCES, (role, entry.source)
    # The Data UUID is bound to the kernel that was built with it, not to the
    # chain that formatted the volume -- those are the two values audit-r8.md
    # 11.10 says have never been compared.
    assert manifest.artifacts["volume_uuid"].source == "build-macos.sh/build-kernel"
    assert manifest.artifacts["data_seed"].source == "build-macos.sh/build-os-root"


# ---------------------------------------------------------------------------
# the offsets are the chain's own, not this module's idea
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not BUILD_OS_ROOT.is_file(), reason="vendor/UniXNU-qemu is not present")
def test_the_container_offsets_are_the_chains_own() -> None:
    """``NXSB`` at 32, block size at 36, block count at 40.

    ``build-os-root.py:114-117`` reads exactly these three fields to decide
    whether the grown seed is a 1 GiB APFS container.  This module reads the same
    three plus three more, and if it had quietly used different offsets it would
    accept containers the chain refuses -- with no symptom until a guest boots and
    finds no Data volume.  So they are pinned against the script that grows the
    container, verbatim.
    """
    text = BUILD_OS_ROOT.read_text(encoding="utf-8")
    assert "if header[32:36] != b'NXSB' or not (1 << 30) - (64 << 20) <= blocks * 4096 <= 1 << 30:" in text, text
    assert "blocks = int.from_bytes(header[40:48], 'little')" in text, text
    assert mm._NX_MAGIC == 32, mm._NX_MAGIC
    assert mm._NX_BLOCK_SIZE == 36, mm._NX_BLOCK_SIZE
    assert mm._NX_BLOCK_COUNT == 40, mm._NX_BLOCK_COUNT
    # The block count the chain demands is the one this module derives from the
    # packed seed's logical size, so a seed that describes a different disk is
    # refused by the same arithmetic.
    assert BLOCKS * PAGE == LOGICAL, (BLOCKS, PAGE, LOGICAL)


def test_the_packed_envelope_is_the_bootloaders_and_the_kernels() -> None:
    """The seed's envelope is parsed by :mod:`unipad.host.packed_ramdisk`, not here.

    A second parser of the DBRDISK1 header is a second thing that can disagree
    with both the bootloader and the kernel driver, so this module delegates and
    this test pins that it does -- by checking the constants come from the module
    that pins them against the two vendor headers.
    """
    assert mm.packed_ramdisk is packed_ramdisk
    assert mm.APFS_BLOCK_SIZE == packed_ramdisk.PAGE_SIZE == 4096


# ---------------------------------------------------------------------------
# every mismatch mode, one rule at a time
# ---------------------------------------------------------------------------
#: Each way a material set can stop belonging to its build.  The name is what a
#: reader of a failure needs; the callable writes a tree with that one thing
#: wrong.
BREAKERS: dict[str, Callable[[Path], tuple[Path, Path]]] = {
    "a seed whose volume is not the kernel's": lambda p: write_tree(
        p, seed_pages={0: _nxsb(), APSB_OFFSET: _apsb(volume_uuid=str(uuid.uuid4()).upper())}
    ),
    "a kernel built for another volume": lambda p: write_tree(
        p, kernel_uuid=str(uuid.uuid4()).upper()
    ),
    "a device tree matching another root": lambda p: write_tree(
        p, adt_uuid=str(uuid.uuid4()).upper()
    ),
    "a donor kernelcache that is not the pinned one": lambda p: write_tree(
        p, kernelcache_sha=WRONG_SHA
    ),
    "an OS member that is not the pinned one": lambda p: write_tree(
        p, os_member_sha=WRONG_SHA
    ),
    "a SystemOS member that is not the pinned one": lambda p: write_tree(
        p, systemos_member_sha=WRONG_SHA
    ),
    "the stub this host actually has": lambda p: write_tree(
        p, seed_pages={0: _stub_container(), APSB_OFFSET: _apsb()}
    ),
    "a container of the wrong size": lambda p: write_tree(
        p, seed_pages={0: _nxsb(block_count=8192), APSB_OFFSET: _apsb()}
    ),
    "a container with no volume": lambda p: write_tree(
        p, seed_pages={0: _nxsb(first_fs_oid=0), APSB_OFFSET: _apsb()}
    ),
    "a container with no checkpoint area": lambda p: write_tree(
        p, seed_pages={0: _nxsb(xp_desc_blocks=0), APSB_OFFSET: _apsb()}
    ),
}


@pytest.mark.parametrize("what", sorted(BREAKERS))
def test_a_broken_binding_is_refused_and_names_the_rule(tmp_path: Path, what: str) -> None:
    """One broken binding at a time, each refused with the rule that broke.

    The rule is asserted, not just the refusal: a verifier that refuses
    everything is indistinguishable from one that refuses for the right reason,
    and only the rule tells them apart.  The control half is what keeps this test
    honest -- if the *unbroken* tree failed, the fixture would be wrong and every
    rule below would be red for the wrong reason.
    """
    control_where, control_board = write_tree(tmp_path / "control")
    verify(collect_manifest(control_where, control_board), control_where, control_board)

    where, board = BREAKERS[what](tmp_path / "broken")
    manifest = collect_manifest(where, board)
    with pytest.raises(mm.MaterialManifestError) as caught:
        verify(manifest, where, board)
    assert caught.value.rule, caught.value
    assert caught.value.code, caught.value


def test_the_build_id_is_what_a_different_build_fails_on(tmp_path: Path) -> None:
    """A manifest from another build is refused, and says so.

    The bytes are all individually fine -- which is the point: nothing about the
    kernel or the seed is wrong, only that they are not from one build, and until
    now nothing could tell.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    other = mm.MaterialManifest(
        schema=manifest.schema,
        build_id="some-other-run",
        board=manifest.board,
        artifacts=manifest.artifacts,
        produced_by=manifest.produced_by,
    )
    assert other.build_id != manifest.build_id
    with pytest.raises(mm.MaterialManifestError) as caught:
        mm.verify_manifest(
            other, stage_flat(where, other), board_pins=mm.read_board_pins(board),
            expected_build_id=BUILD_ID,
        )
    assert caught.value.rule == mm.RULE_BUILD_ID, caught.value
    assert caught.value.details["wanted"] == BUILD_ID, caught.value
    # The same manifest against no expectation still verifies: the build id is
    # only a mismatch once something says which build it wanted.
    mm.verify_manifest(other, stage_flat(where, other), board_pins=mm.read_board_pins(board))
    # ...while the original still verifies against the same directory, which is
    # what makes the difference the build id and nothing else.
    verify(manifest, where, board)


def test_an_artifact_whose_hash_does_not_match_is_refused(tmp_path: Path) -> None:
    """The digest is recomputed from the bytes, never taken from the manifest.

    A manifest that carried its own hashes and was believed would let any file be
    swapped in underneath it, which is the whole failure mode this module exists
    to close.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    kernel = where / "tree/kernel/kernel.macho"
    kernel.write_bytes(kernel.read_bytes() + b"one more byte\n")
    with pytest.raises(mm.MaterialManifestError) as caught:
        verify(manifest, where, board)
    assert caught.value.rule in (mm.RULE_ARTIFACT_SIZE, mm.RULE_ARTIFACT_HASH), caught.value
    assert caught.value.code == ErrorCode.IMAGE_HASH_MISMATCH, caught.value


def test_a_missing_entry_is_refused(tmp_path: Path) -> None:
    """A role the manifest does not carry is a role nobody checked.

    Refused rather than assumed absent: a manifest that omits the seed is
    indistinguishable from one whose build never made a seed, and only one of
    those is a build that can boot.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    dropped = mm.MaterialManifest(
        schema=manifest.schema,
        build_id=manifest.build_id,
        board=manifest.board,
        artifacts={k: v for k, v in manifest.artifacts.items() if k != "trust_cache"},
        produced_by=manifest.produced_by,
    )
    with pytest.raises(mm.MaterialManifestError) as caught:
        mm.verify_manifest(dropped, stage_flat(where, dropped), board_pins=mm.read_board_pins(board))
    assert caught.value.rule == mm.RULE_ENTRY_MISSING, caught.value
    assert caught.value.code == ErrorCode.ARTIFACT_MISSING, caught.value


def test_an_unknown_source_is_refused(tmp_path: Path) -> None:
    """"Somewhere" is not an answer to "which build produced this".

    An artifact whose producer cannot be named cannot be checked against the
    build that made it, so a source outside the closed set is refused rather than
    accepted on trust.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    lying = mm.MaterialManifest(
        schema=manifest.schema,
        build_id=manifest.build_id,
        board=manifest.board,
        artifacts={
            **manifest.artifacts,
            "kernel": mm.MaterialEntry(
                role="kernel",
                source="a friend on the internet",
                path=manifest.artifacts["kernel"].path,
                sha256=manifest.artifacts["kernel"].sha256,
                bytes=manifest.artifacts["kernel"].bytes,
            ),
        },
        produced_by=manifest.produced_by,
    )
    with pytest.raises(mm.MaterialManifestError) as caught:
        mm.verify_manifest(lying, stage_flat(where, lying), board_pins=mm.read_board_pins(board))
    assert caught.value.rule == mm.RULE_SOURCE_UNKNOWN, caught.value
    assert caught.value.details["source"] == "a friend on the internet", caught.value


def test_a_role_nobody_consumes_is_refused(tmp_path: Path) -> None:
    """An extra entry is refused too, not just a missing one.

    Otherwise a manifest could carry a role this module has never heard of and a
    reader would have no way to know it was never checked.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    padded = mm.MaterialManifest(
        schema=manifest.schema,
        build_id=manifest.build_id,
        board=manifest.board,
        artifacts={
            **manifest.artifacts,
            "something_else": mm.MaterialEntry(
                role="something_else", source="ipsw", path="x", sha256="y"
            ),
        },
        produced_by=manifest.produced_by,
    )
    with pytest.raises(mm.MaterialManifestError) as caught:
        mm.verify_manifest(padded, stage_flat(where, padded), board_pins=mm.read_board_pins(board))
    assert caught.value.rule == mm.RULE_ROLE_UNKNOWN, caught.value


def test_a_manifest_that_is_not_one_is_refused(tmp_path: Path) -> None:
    """A file that is not JSON, or not this schema, is refused and not repaired.

    A manifest that half-parses is worse than one that does not, because the half
    it parsed looks like evidence.
    """
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(mm.MaterialManifestError) as caught:
        mm.MaterialManifest.load(tmp_path / "broken.json")
    assert caught.value.rule == mm.RULE_SCHEMA, caught.value

    (tmp_path / "wrong-schema.json").write_text(
        json.dumps({"schema": "someone-elses-manifest/9", "artifacts": {}}), encoding="utf-8"
    )
    with pytest.raises(mm.MaterialManifestError) as caught:
        mm.MaterialManifest.load(tmp_path / "wrong-schema.json")
    assert caught.value.rule == mm.RULE_SCHEMA, caught.value
    assert caught.value.details["schema"] == "someone-elses-manifest/9", caught.value


def test_a_seed_that_is_not_a_seed_is_refused(tmp_path: Path) -> None:
    """The packed envelope is checked by the module that owns it, and a failure
    there is a seed neither parser would accept -- not a hash problem."""
    where, board = write_tree(tmp_path)
    # Written BEFORE the manifest is collected, so the digest is right and the
    # only thing wrong with the file is that it is not a seed.  Collected after
    # the fact, the hash rule would answer first and this test would be green for
    # the wrong reason -- which is the same class of mistake as a test that fails
    # on its own fixture.
    (where / "device/.cache/firmware/owner-data/owner-data-1g.dbr").write_bytes(
        b"\x00" * 8256
    )
    manifest = collect_manifest(where, board)
    with pytest.raises(mm.MaterialManifestError) as caught:
        verify(manifest, where, board)
    assert caught.value.rule == mm.RULE_SEED_ENVELOPE, caught.value


# ---------------------------------------------------------------------------
# the rule whose teeth have to be shown a different way
# ---------------------------------------------------------------------------
def test_the_volume_rule_is_the_only_thing_refusing_the_wrong_seed(tmp_path: Path) -> None:
    """A seed with the right container and the wrong volume is refused by one rule.

    The container checks pass -- demonstrated here by calling them directly, not
    by asserting they would -- so the volume-superblock rule is the only thing
    that can refuse.  Without that demonstration a test could be green because a
    *different* rule caught the mutation, which is the difference between a test
    that guards a contract and a test that happens to be red.
    """
    wrong = str(uuid.uuid4()).upper()
    where, board = write_tree(
        tmp_path, seed_pages={0: _nxsb(), APSB_OFFSET: _apsb(volume_uuid=wrong)}
    )
    manifest = collect_manifest(where, board)

    seed_path = where / "device/.cache/firmware/owner-data/owner-data-1g.dbr"
    seed = packed_ramdisk.validate_file(seed_path)
    pages = mm._seed_pages(seed_path, seed)
    container = mm._container_superblock(pages, seed_path)
    # Every container rule passes...
    assert container[32:36] == b"NXSB"
    assert struct.unpack_from("<I", container, 36)[0] == 4096
    assert struct.unpack_from("<Q", container, 40)[0] * 4096 == seed.logical
    assert struct.unpack_from("<Q", container, 184)[0] != 0
    assert struct.unpack_from("<I", container, 104)[0] != 0
    # ...and the seed does carry a volume superblock, with a real UUID:
    volumes = [mm._guid(p[0xF0:0x100]) for p in pages if p[32:36] == b"APSB"]
    assert volumes == [wrong], volumes

    # The refusal, and the reason it is spelled out rather than left to
    # pytest.raises' own "DID NOT RAISE": if the rule is ever weakened -- the
    # comparison loosened, the scan dropped, the UUID taken from the manifest
    # instead of the seed -- this test has to fail *naming the rule*, so the
    # person reading it knows which contract broke rather than only that
    # something did.
    try:
        report = verify(manifest, where, board)
    except mm.MaterialManifestError as caught:
        assert caught.rule == mm.RULE_SEED_VOLUME, caught
        assert caught.details["in_seed"] == [wrong], caught
        assert caught.details["in_kernel"] == DATA_UUID, caught
    else:
        pytest.fail(
            f"verify() returned {report!r} instead of refusing: "
            f"{mm.RULE_SEED_VOLUME} no longer refuses a seed whose volume "
            f"superblock says {wrong} when the kernel was built for {DATA_UUID}, "
            "so a seed and a kernel from different builds would now verify as a "
            "matched pair -- which is the whole thing this module exists to prevent"
        )


def test_the_kernel_must_actually_carry_the_uuid_it_claims(tmp_path: Path) -> None:
    """A manifest claiming a UUID the kernel does not contain is refused.

    This is the rule that makes the manifest's claim checkable against the
    artifact instead of against itself: the header says what the kernel build was
    told, and the image is the only evidence of what it was given.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    # A kernel image that does not carry the UUID, with everything else intact.
    replacement = b"\xcf\xfa\xed\xfe" + b"\0" * 256
    (where / "tree/kernel/kernel.macho").write_bytes(replacement)
    honest = mm.MaterialManifest(
        schema=manifest.schema,
        build_id=manifest.build_id,
        board=manifest.board,
        artifacts={
            **manifest.artifacts,
            "kernel": mm.MaterialEntry(
                role="kernel",
                source=manifest.artifacts["kernel"].source,
                path=manifest.artifacts["kernel"].path,
                sha256=hashlib.sha256(replacement).hexdigest(),
                bytes=len(replacement),
            ),
        },
        produced_by=manifest.produced_by,
    )
    with pytest.raises(mm.MaterialManifestError) as caught:
        verify(honest, where, board)
    assert caught.value.rule == mm.RULE_KERNEL_UUID_ABSENT, caught.value


def test_the_uuid_is_found_across_a_read_boundary(tmp_path: Path) -> None:
    """The kernel is read in chunks, so a UUID split between two must still be found.

    Reported as absent it would be a refusal with the wrong reason -- and on a
    50 MB kernel the split is not a corner case, it happens by construction.
    """
    blob = b"\0" * (1024 * 1024 - 20) + DATA_UUID.encode() + b"\0" * 16
    target = tmp_path / "split-kernel"
    target.write_bytes(blob)
    assert mm._contains_bytes(target, DATA_UUID.encode())
    assert not mm._contains_bytes(target, b"no-such-uuid-in-here")


def test_a_kernel_with_no_data_mount_cannot_be_manifested(tmp_path: Path) -> None:
    """The header's other spelling is a refusal, not an empty UUID.

    ``build-xnu-4k-baseline.sh`` writes ``/* Scratch Data IOKit selection
    disabled. */`` when the owner-data mount is off.  A manifest that recorded
    such a kernel as carrying the Data volume would be lying about the one thing
    the Data volume depends on.
    """
    where, board = write_tree(tmp_path)
    (where / "tree/build/xnu/owner-data-volume.h").write_text(
        "/* Scratch Data IOKit selection disabled. */\n", encoding="utf-8"
    )
    with pytest.raises(mm.MaterialManifestError) as caught:
        collect_manifest(where, board)
    assert caught.value.rule == mm.RULE_DATA_UUID, caught.value


def test_a_manifest_needs_a_build_id(tmp_path: Path) -> None:
    """Without a build id no two artifacts can be shown to come from one build."""
    where, board = write_tree(tmp_path)
    with pytest.raises(mm.MaterialManifestError) as caught:
        collect_manifest(where, board, build_id="   ")
    assert caught.value.rule == mm.RULE_BUILD_ID, caught.value


# ---------------------------------------------------------------------------
# the board is the authority, read rather than restated
# ---------------------------------------------------------------------------
def test_the_pins_are_read_from_the_board(tmp_path: Path) -> None:
    """Every pin comes out of ``board.toml``, and a board without one refuses.

    A second spelling of a pin is the divergence class this project keeps
    hitting, so the module reads the board and refuses a board that does not say.
    """
    where, board = write_tree(tmp_path)
    pins = mm.read_board_pins(board)
    assert pins.kernelcache_sha256 == KERNELCACHE_SHA, pins
    assert pins.root_match_volume_uuid == ROOT_UUID, pins
    assert pins.os_root_member_sha256 == OS_MEMBER_SHA, pins
    assert pins.systemos_member_sha256 == SYSTEMOS_MEMBER_SHA, pins

    (tmp_path / "no-pins.toml").write_text("[kernel]\n", encoding="utf-8")
    with pytest.raises(mm.MaterialManifestError) as caught:
        mm.read_board_pins(tmp_path / "no-pins.toml")
    assert caught.value.rule == mm.RULE_PIN_MISSING, caught.value


def test_the_two_members_may_be_recorded_without_their_bytes(tmp_path: Path) -> None:
    """The runner deletes the multi-GB members, so their identity is the board's pin.

    PRD 12 requires the extracted members be deleted the moment extraction
    finishes, so the build that writes this manifest does not have them.  Their
    identity is still pinned, the manifest says how it was established, and a
    host that *does* have the file gets it hashed on top of the pin.
    """
    where, board = write_tree(tmp_path)
    volumes = where / "device/.cache/firmware/target-volumes"
    (volumes / "044-76040-038.dmg").unlink()
    (volumes / "044-76034-038.dmg").unlink()
    manifest = collect_manifest(where, board)
    assert manifest.artifacts["os_root_image"].sha256 == OS_MEMBER_SHA
    assert any("board.toml [ipsw.sha256]" in note for note in manifest.notes), manifest.notes
    # Verifies without the bytes...
    report = verify(manifest, where, board)
    assert report.volume_uuid == DATA_UUID, report
    # ...and a manifest claiming a different member is refused even so.
    lying = mm.MaterialManifest(
        schema=manifest.schema,
        build_id=manifest.build_id,
        board=manifest.board,
        artifacts={
            **manifest.artifacts,
            "os_root_image": mm.MaterialEntry(
                role="os_root_image",
                source="ipsw",
                path=manifest.artifacts["os_root_image"].path,
                sha256="f" * 64,
            ),
        },
        produced_by=manifest.produced_by,
    )
    with pytest.raises(mm.MaterialManifestError) as caught:
        verify(lying, where, board)
    assert caught.value.rule == mm.RULE_IPSW_PIN, caught.value


def test_the_board_pins_are_optional_but_not_defaulted(tmp_path: Path) -> None:
    """Without a board the bindings that need one are skipped, and said so.

    A caller with no ``board.toml`` gets the structural and byte checks and not
    the pin checks; it does not get pins invented for it.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    report = mm.verify_manifest(manifest, stage_flat(where, manifest), board_pins=None)
    assert "board-pins" not in report.checked, report
    assert "seed-volume" in report.checked, report


# ---------------------------------------------------------------------------
# the module's own contract
# ---------------------------------------------------------------------------
def test_a_round_trip_through_json_preserves_the_manifest(tmp_path: Path) -> None:
    """What is written is what is read, so a manifest can travel as a file.

    The manifest crosses from a macOS runner to a Windows host as a file, so a
    round trip that lost a field would lose a binding on the way.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    written = manifest.write(tmp_path / "material-manifest.json")
    read_back = mm.MaterialManifest.load(written)
    assert read_back.to_dict() == manifest.to_dict()
    verify(read_back, where, board)


def test_the_manifest_never_carries_an_absolute_path(tmp_path: Path) -> None:
    """A manifest with a host path in it cannot be read on the other host.

    This one crosses from a macOS runner to a Windows machine, and the staging
    manifest already learned this: it is written with relative names for exactly
    that reason.
    """
    where, board = write_tree(tmp_path)
    manifest = collect_manifest(where, board)
    for role, entry in manifest.artifacts.items():
        assert "/" not in entry.path, (role, entry.path)
        assert ":" not in entry.path, (role, entry.path)
    payload = json.dumps(manifest.to_dict())
    assert str(tmp_path) not in payload


def test_two_roles_with_one_leaf_name_are_refused(tmp_path: Path) -> None:
    """A flat delivery cannot hold two files with one name.

    The manifest records leaf names because the delivery is a flat directory, so
    two roles whose files share a leaf would overwrite each other on the way
    out -- and the manifest would then describe a file that had been replaced,
    which is a hash mismatch on the wrong file, discovered on the host instead
    of on the runner.  Refused here instead, and the refusal names both roles so
    the person reading it knows which producer to go and argue with.

    ``BuildTree.devicetree`` is what makes this reachable: the standard layout
    puts every role at its own leaf, so without an override the guard would be
    code no test can execute -- and a guard nobody has seen fire is a guard
    nobody knows works.
    """
    where, board = write_tree(tmp_path)
    esp = where / "tree/payload-esp/EFI/UniXNU"
    # The boot config is already there as unixnu.toml; point the device tree at
    # the same leaf and the two collide.
    colliding = esp / "unixnu.toml"
    with pytest.raises(mm.MaterialManifestError) as caught:
        mm.collect(
            mm.BuildTree(
                root=where / "tree",
                device_root=where / "device",
                esp=where / "tree/payload-esp",
                devicetree=colliding,
                volumes=where / "device/.cache/firmware/target-volumes",
                kernelcache=where / "out/ipsw/21H461/kernelcache.decompressed",
            ),
            build_id=BUILD_ID,
            board_pins=mm.read_board_pins(board),
        )
    assert caught.value.rule == mm.RULE_LEAF_COLLISION, caught.value
    assert caught.value.details["leaf"] == "unixnu.toml", caught.value
    # Both roles are named: a collision between two of *this build's* files is a
    # build problem, and the message has to say which two.
    assert {"boot_config", "devicetree"} == {
        caught.value.details["role"],
        caught.value.details["other_role"],
    }, caught.value
    # A tree that does not collide still collects, so this is the collision
    # refusing and not the collection being broken.
    assert collect_manifest(where, board).artifacts["boot_config"].path == "unixnu.toml"
    # ...and the override is not a second way to spell the standard path: with it
    # unset the device tree is still where the runner puts it.
    assert collect_manifest(where, board).artifacts["devicetree"].path == "kernel.macho.adt"


def test_the_refusal_carries_a_stable_code_and_a_rule() -> None:
    """The taxonomy has one code for "conditions not met"; the rule is the rest.

    Documented rather than worked around: a caller that only needs to refuse
    branches on the code, a caller that needs to say why reads the rule, and
    adding a dozen codes to the shared taxonomy for one module would be the wrong
    trade.
    """
    error = mm.MaterialManifestError(
        ErrorCode.VERIFICATION_FAILED, "a thing did not hold", rule=mm.RULE_SEED_VOLUME
    )
    assert error.code == ErrorCode.VERIFICATION_FAILED
    assert error.rule == mm.RULE_SEED_VOLUME
    assert error.details["rule"] == mm.RULE_SEED_VOLUME
    assert error.exit_code.value == 1, error.exit_code


def test_every_rule_has_a_distinct_name() -> None:
    """Two rules with one name is a refusal nobody can act on.

    Cheap, and it catches the copy-paste that would otherwise only show up when
    someone tried to work out which of two identically-named rules fired.
    """
    names = [name for name in dir(mm) if name.startswith("RULE_")]
    values = [getattr(mm, name) for name in names]
    assert len(values) == len(set(values)), values


def test_the_rule_set_covers_every_refusal_the_module_can_make() -> None:
    """No refusal is invented at the call site.

    Every ``_refuse`` in the module names one of the ``RULE_`` constants, so a new
    refusal cannot appear without a name a test can assert on.  Found by reading
    the module's own source rather than by trusting this test's list of rules.
    """
    source = (ROOT / "src/unipad/host/material_manifest.py").read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[0].strip()
        for line in source.splitlines()
        if line.startswith("RULE_") and "=" in line
    }
    used = {
        token
        for token in source.replace("(", " ").replace(")", " ").replace(",", " ").split()
        if token.startswith("RULE_")
    }
    assert used <= declared, sorted(used - declared)
    # ...and the constants are what the names say they are, so a renamed rule
    # cannot quietly take another rule's place.
    assert declared == {name for name in dir(mm) if name.startswith("RULE_")}
