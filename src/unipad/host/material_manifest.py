"""Which build produced the material a boot uses, and whether it still matches.

Why this file exists
--------------------
A boot consumes nine pieces of material, and they come from three different
places: the pinned IPSW, the owner root chain, and this project's own runner
stages.  Nothing in that chain records *which build* produced which piece, and
nothing checks that the pieces still belong together.  The consequence is not
theoretical.  Measured on 2026-10-26 (implementation-plan fact 81), the
``owner-data-1g.dbr`` on this host's ESP is a **36,960-byte hand-written
stub**: its ``nx_magic``/``nx_block_size``/``nx_block_count`` are all correct
(``NXSB``, 4096, 262144), so every check that looked at the packed envelope
passed, while its ``nx_uuid`` is ``00 01 02 ... 0f`` -- the sixteen sequential
bytes -- and its whole 4096-byte superblock has 51 nonzero bytes.  The real
runner-built seed is **1,245,328 bytes**.  A stub that passes the envelope and
fails the container is exactly the shape this module exists to catch, and it is
why the owner's instruction says a placeholder must never be mistakable for the
real thing.

So "matched build" is encoded here as five bindings, each of which a producer
has to satisfy and none of which a plausible-looking file can satisfy by
accident:

1. **one build id** -- every artifact carries the same one.  A kernel from run A
   and a seed from run B do not boot, and until now nothing said so.
2. **a known source** -- every artifact names the stage or the IPSW that
   produced it, from a closed set.  "Somewhere" is not a source.
3. **the hash and the size** -- recomputed from the bytes on disk, never taken
   from the manifest.
4. **the Data volume UUID** -- the seed's UUID must equal the UUID the kernel was
   built with, and that UUID must actually be *in the kernel image*.  This is the
   binding audit-r8.md 11.10 records as unverified for three rounds:
   ``build-os-root.py`` grows the container from 32 MiB to 1 GiB and verifies
   only ``NXSB`` and the block count, never the volume UUID afterwards.
5. **the pinned identity** -- the donor kernelcache's digest must equal
   ``board.toml [kernel.prelink] kernelcache_sha256``, and the ADT's root-match
   UUID must equal ``board.toml [payload.adt] root_match_volume_uuid``.  Both are
   read from the board rather than restated, because a second spelling of a pin
   is the divergence class this project keeps hitting.

What is *not* checked, and why
------------------------------
The volume UUID is a **producer-measured** field.  It cannot be read out of a
packed seed by indexing a block, because ``nx_fs_oid[0]`` and
``apfs_root_tree_oid`` are *virtual* oids: reading block ``oid`` of the image
gives a zero block, and the only way to the volume superblock is through the
container's object map (measured, and recorded in
``local-private/firmware/apfs-read.py``'s own docstring).  Rather than guess an
omap walk that has never been run against a macOS-built seed, this module
requires the producer -- which has ``diskutil`` -- to measure it with the same
tool ``prepare-owner-data-fixture.py`` uses, and then *re-derives it from the
seed's resident volume superblock* and requires the two to agree.  A seed whose
volume superblock is not resident, or carries a different UUID, is refused.

The re-derivation scans the resident pages for the ``APSB`` magic, which is a
search and not an index, so it can in principle find a ``APSB`` in a data page
that is not the volume superblock.  The failure mode of that is a refusal on
real material, not a pass on a stub -- the 4 magic bytes at one offset and 16
nonzero bytes at another have to line up by chance -- and it is the direction
this module is built to fail in.  What it cannot do is tell the operator *which*
of the two happened; a refusal here on a runner-built seed is the signal to walk
the omap properly, and it is recorded as such.

The two IPSW members are the one place a digest is accepted without a file to
hash.  They are 3.3 GiB and 5.2 GiB of Apple material that PRD 12 requires be
deleted the moment extraction finishes, so whoever writes this manifest does not
have them; their identity is the board's own ``[ipsw.sha256]`` pin, which
``unipad.host.ipsw`` already verifies extraction against.  When the file *is*
present it is hashed as well -- the pin is the floor, not the ceiling.

Error codes
-----------
Every refusal carries a stable code from :class:`unipad.errors.ErrorCode` and a
``rule`` naming the exact invariant that failed, because the taxonomy has one
code for "conditions not met" and this module has a dozen conditions.  The code
is what a caller branches on; the rule is what a human reads.  Where no code
fits, ``verification_failed`` is used with a rule rather than bending a code to
mean something it does not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from unipad.errors import ErrorCode, ExitCode, UniPadError
from unipad.host import packed_ramdisk
from unipad.host.paths import safe_location

__all__ = [
    "APFS_BLOCK_SIZE",
    "BoardPins",
    "BuildTree",
    "MATERIAL_ROLES",
    "MATERIAL_SCHEMA",
    "MaterialEntry",
    "MaterialManifest",
    "MaterialManifestError",
    "KNOWN_SOURCES",
    "PLACEHOLDER_CONTAINER_UUIDS",
    "VerifyReport",
    "collect",
    "read_board_pins",
    "read_owner_data_header",
    "verify_manifest",
]

#: The manifest's own schema tag.  A manifest without it is not a manifest.
MATERIAL_SCHEMA = "unipad-material-manifest/1"

#: The APFS block size.  Every checksummed object occupies a whole block.
APFS_BLOCK_SIZE = 4096

#: The roles a real boot consumes, in the order they are verified.  ``volume_uuid``
#: is a *value* rather than a file: the Data volume's UUID is the binding between
#: the seed and the kernel, and it has no bytes of its own.
MATERIAL_ROLES: tuple[str, ...] = (
    "kernel",
    "devicetree",
    "trust_cache",
    "data_seed",
    "systemos_image",
    "os_root_image",
    "kernelcache",
    "boot_config",
    "volume_uuid",
)

#: The closed set of producers.  A source outside it is refused: "somewhere" is
#: not an answer to "which build produced this", and an artifact whose producer
#: cannot be named cannot be checked against the build that made it.
KNOWN_SOURCES = frozenset(
    {
        "ipsw",                                # a member of the pinned IPSW
        "build-macos.sh/build-os-root",        # the owner root chain
        "build-macos.sh/build-kernel",
        "build-macos.sh/build-bootloader",
        "build-macos.sh/build-payload",
        "build-macos.sh/build-devicetree",
        "build-macos.sh/assemble-image",
    }
)

#: The container UUIDs measured on hand-written stubs on this host, spelled the
#: way ``uuid.UUID`` spells them.  Both are APFS *containers* whose superblocks
#: look right -- ``NXSB``, 4096, 262144 blocks -- and neither can be mounted.
#: ``00010203-...`` is implementation-plan fact 81's 36,960-byte stub;
#: ``11111111-...`` is the 73,840-byte one measured on 2026-10-26.  The list is
#: a backstop, not the rule: the rule is that the container must carry a volume
#: superblock, and neither stub does.
PLACEHOLDER_CONTAINER_UUIDS = frozenset(
    {
        "00010203-0405-0607-0809-0a0b0c0d0e0f",
        "11111111-2222-3333-4444-555555555555",
    }
)

#: The same set, upper-cased once, because :func:`_guid` returns upper-case and a
#: comparison that is not normalised on both sides silently never matches -- which
#: is how the first version of this check passed a stub through.
_PLACEHOLDER_CONTAINER_UUIDS = frozenset(
    value.upper() for value in PLACEHOLDER_CONTAINER_UUIDS
)

#: Offsets inside ``nx_superblock_t`` past its 32-byte object header, from the
#: APFS reference's structure order.  ``build-os-root.py:114-117`` reads the
#: first three to prove the grown seed is a 1 GiB APFS container, so they are the
#: chain's own offsets, not this module's guess.
_NX_MAGIC = 32
_NX_BLOCK_SIZE = 36
_NX_BLOCK_COUNT = 40
_NX_UUID = 72
_NX_XP_DESC_BLOCKS = 104
_NX_FIRST_FS_OID = 184

#: ``apfs_vol_uuid`` inside ``apfs_superblock_t``, from apfsprogs' own
#: ``raw.h`` column comments as transcribed by
#: ``local-private/firmware/dump-nxsb-apsb.py``.
_APFS_MAGIC = 32
_APFS_VOL_UUID = 0xF0

#: The packed seed's own header: 8s magic, I version, I page, Q logical, Q count.
_ENVELOPE = struct.Struct("<8sIIQQ")


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
# One constant per invariant, so a refusal message and a test can name the same
# thing without either restating the other.
RULE_SCHEMA = "schema-unknown"
RULE_ENTRY_MISSING = "entry-missing"
RULE_ROLE_UNKNOWN = "role-unknown"
RULE_SOURCE_UNKNOWN = "source-unknown"
RULE_BUILD_ID = "build-id-mismatch"
RULE_ARTIFACT_ABSENT = "artifact-absent"
RULE_ARTIFACT_SIZE = "artifact-size-mismatch"
RULE_ARTIFACT_HASH = "artifact-hash-mismatch"
RULE_VALUE_MALFORMED = "value-malformed"
RULE_PIN_MISSING = "pin-missing"
RULE_KERNELCACHE_PIN = "kernelcache-pin-mismatch"
RULE_ROOT_UUID_PIN = "root-uuid-pin-mismatch"
RULE_IPSW_PIN = "ipsw-member-pin-mismatch"
RULE_DATA_UUID = "data-uuid-mismatch"
RULE_KERNEL_UUID_ABSENT = "kernel-uuid-absent"
RULE_KERNELCONFIG_ABSENT = "kernelconfig-absent"
RULE_SEED_ENVELOPE = "seed-envelope-invalid"
RULE_SEED_CONTAINER = "seed-container-invalid"
RULE_SEED_VOLUME = "seed-volume-superblock"
RULE_LEAF_COLLISION = "leaf-name-collision"


class MaterialManifestError(UniPadError):
    """A piece of material does not belong to the build that claims it.

    ``rule`` is the invariant that failed.  It is carried separately from
    :attr:`code` because the fault taxonomy has one code for "the conditions
    were not met" and this module enforces a dozen conditions: a caller that
    needs to tell "wrong build" from "wrong UUID" reads the rule, and a caller
    that only needs to refuse reads the code.
    """

    def __init__(self, code: str, message: str, *, rule: str, **details: Any) -> None:
        super().__init__(code, message, rule=rule, **details)
        self.rule = rule


def _refuse(rule: str, code: str, message: str, **details: Any) -> MaterialManifestError:
    return MaterialManifestError(code, message, rule=rule, **details)


# ---------------------------------------------------------------------------
# the board's pins, read rather than restated
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BoardPins:
    """The values ``board.toml`` pins that a boot's material must satisfy.

    ``kernelcache_sha256`` is the digest of the **original Apple kernelcache**
    the prelink step uses as its donor (``board.toml [kernel.prelink]``).  It is
    an *input* pin, and it is what makes "the kernel's identity must equal the
    pinned one" checkable: the built ``kernel.macho`` is documented as not
    byte-reproducible across builds (``.github/workflows/build-macos.yml``
    records sha256 ``9fc119604838856d`` on M2 against ``1f32f09bc7668de0`` on
    M4), so pinning *it* would be pinning something the build does not promise.

    ``root_match_volume_uuid`` is the OS root volume's UUID
    (``board.toml [payload.adt]``), which the ADT publishes as
    ``/chosen/root-matching``.  It is a *different* UUID from the Data volume's
    and from the SystemOS volume's, and confusing the three is how a boot ends up
    matching the wrong disk.

    ``os_root_member_sha256`` and ``systemos_member_sha256`` are the
    ``[ipsw.sha256]`` pins of the two multi-GB members.  They are here because
    those members are deleted the moment extraction finishes (PRD 12), so a
    manifest writer does not have the bytes to hash -- the board's pin is the
    only identity available, and it is the one ``unipad.host.ipsw`` already
    verifies extraction against.
    """

    kernelcache_sha256: str
    root_match_volume_uuid: str
    os_root_member_sha256: str = ""
    systemos_member_sha256: str = ""
    board: str = ""

    @classmethod
    def from_board(cls, path: Path) -> "BoardPins":
        """Read every pin out of a ``board.toml``.

        Refuses rather than defaulting: a manifest verified against a guessed
        pin is verified against nothing, and ``board.toml`` is the authority
        every other stage in this project reads.
        """
        path = Path(path)
        try:
            board = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as failure:
            raise _refuse(
                RULE_PIN_MISSING,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                f"board.toml could not be read: {failure}",
                board=str(path),
            ) from failure
        prelink = (board.get("kernel") or {}).get("prelink") or {}
        adt = (board.get("payload") or {}).get("adt") or {}
        members = board.get("ipsw") or {}
        hashes = members.get("sha256") or {} if isinstance(members, dict) else {}
        kernelcache = str(prelink.get("kernelcache_sha256") or "").strip().lower()
        root_uuid = str(adt.get("root_match_volume_uuid") or "").strip()
        if not kernelcache:
            raise _refuse(
                RULE_PIN_MISSING,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "board.toml [kernel.prelink] declares no kernelcache_sha256, so "
                "the kernel's donor identity cannot be pinned",
                board=str(path),
            )
        if not root_uuid:
            raise _refuse(
                RULE_PIN_MISSING,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "board.toml [payload.adt] declares no root_match_volume_uuid, so "
                "the root volume the ADT matches cannot be pinned",
                board=str(path),
            )
        return cls(
            kernelcache_sha256=kernelcache,
            root_match_volume_uuid=root_uuid,
            os_root_member_sha256=str(hashes.get("044-76040-038.dmg") or "").lower(),
            systemos_member_sha256=str(hashes.get("044-76034-038.dmg") or "").lower(),
        )


def read_board_pins(path: Path) -> BoardPins:
    """:meth:`BoardPins.from_board`, spelled the way the other readers are."""
    return BoardPins.from_board(path)


# ---------------------------------------------------------------------------
# the manifest
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MaterialEntry:
    """One piece of material: where it came from, what it is, what it hashes to.

    ``path`` is relative and never absolute -- the same reasoning the staging
    manifest uses: a manifest that carries a host path cannot be read on a
    different host, and this one crosses from a macOS runner to a Windows one.
    ``value`` is for the roles that are bindings rather than bytes.
    """

    role: str
    source: str
    path: str = ""
    value: str = ""
    sha256: str = ""
    bytes: int = 0


@dataclass(frozen=True)
class MaterialManifest:
    """What one build produced, and the bindings that hold it together."""

    schema: str
    build_id: str
    board: str
    artifacts: dict[str, MaterialEntry]
    produced_by: str = ""
    runner_run: str = ""
    device_commit: str = ""
    unixnu_commit: str = ""
    notes: tuple[str, ...] = ()

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "build_id": self.build_id,
            "board": self.board,
            "produced_by": self.produced_by,
            "runner_run": self.runner_run,
            "device_commit": self.device_commit,
            "unixnu_commit": self.unixnu_commit,
            "artifacts": {
                role: {
                    "source": entry.source,
                    "path": entry.path,
                    "value": entry.value,
                    "sha256": entry.sha256,
                    "bytes": entry.bytes,
                }
                for role, entry in self.artifacts.items()
            },
            "notes": list(self.notes),
        }

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "MaterialManifest":
        """Read a manifest off disk.

        A file that is not JSON, or that is not the shape this module writes, is
        refused with ``schema-unknown`` rather than repaired: a manifest that
        half-parses is worse than one that does not, because the half it parsed
        looks like evidence.
        """
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as failure:
            raise _refuse(
                RULE_SCHEMA,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                f"the material manifest could not be read: {failure}",
                manifest=safe_location(path),
            ) from failure
        return cls.from_dict(payload, source=path)

    @classmethod
    def from_dict(cls, payload: Any, *, source: Optional[Path] = None) -> "MaterialManifest":
        where = safe_location(source) if source else ""
        if not isinstance(payload, dict):
            raise _refuse(
                RULE_SCHEMA,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "the material manifest is not a JSON object",
                manifest=where,
            )
        schema = str(payload.get("schema") or "")
        if schema != MATERIAL_SCHEMA:
            raise _refuse(
                RULE_SCHEMA,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                f"the material manifest's schema is {schema!r}, not {MATERIAL_SCHEMA!r}",
                manifest=where,
                schema=schema,
            )
        raw = payload.get("artifacts")
        if not isinstance(raw, dict):
            raise _refuse(
                RULE_SCHEMA,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "the material manifest carries no 'artifacts' object",
                manifest=where,
            )
        artifacts: dict[str, MaterialEntry] = {}
        for role, entry in raw.items():
            if not isinstance(entry, dict):
                raise _refuse(
                    RULE_SCHEMA,
                    ErrorCode.ARTIFACT_MANIFEST_INVALID,
                    f"the material manifest's '{role}' entry is not an object",
                    manifest=where,
                    role=role,
                )
            artifacts[str(role)] = MaterialEntry(
                role=str(role),
                source=str(entry.get("source") or ""),
                path=str(entry.get("path") or ""),
                value=str(entry.get("value") or ""),
                sha256=str(entry.get("sha256") or "").lower(),
                bytes=int(entry.get("bytes") or 0),
            )
        notes = payload.get("notes") or []
        return cls(
            schema=schema,
            build_id=str(payload.get("build_id") or ""),
            board=str(payload.get("board") or ""),
            artifacts=artifacts,
            produced_by=str(payload.get("produced_by") or ""),
            runner_run=str(payload.get("runner_run") or ""),
            device_commit=str(payload.get("device_commit") or ""),
            unixnu_commit=str(payload.get("unixnu_commit") or ""),
            notes=tuple(str(note) for note in notes) if isinstance(notes, list) else (),
        )


# ---------------------------------------------------------------------------
# the producer side: where the macOS runner leaves each piece
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BuildTree:
    """The paths the macOS runner's material lives at, named once.

    Every default is the layout ``build-macos.sh`` and ``build-os-root.py``
    actually produce, so a caller with the standard layout passes nothing.  The
    alternatives exist because a host that downloaded the material has yet
    another layout, and because the runner deletes the two multi-GB IPSW members
    between the chain and the second build pass.
    """

    #: The build tree -- ``work_dir`` of build-macos.sh, which holds ``kernel/``,
    #: ``payload/`` and ``.config``.  It is the driver's ``ctx.work``
    #: (``<out>/<board name>``) and it CONTAINS ``ctx.tree``; see
    #: :attr:`source_tree` before assuming anything built by a script lives here.
    root: Path
    #: The device repository -- ``DEVICE_DIR``, which holds ``.cache/firmware/``.
    device_root: Path
    #: The payload's ESP, which carries ``EFI/UniXNU/unixnu.toml``.
    esp: Optional[Path] = None
    #: The device tree, when it is not at either standard place.  A field rather
    #: than a fixed property for the reason ``esp`` and ``volumes`` are: a host
    #: that downloaded the material has its own layout, and a caller that cannot
    #: say where the device tree is would have to rename files to be believed.
    #: Setting it also selects the ``devicetree`` role's recorded source, since
    #: :attr:`devicetree_source` is derived from where the tree was found.
    devicetree: Optional[Path] = None
    #: Where the OS and SystemOS members were extracted.
    volumes: Optional[Path] = None
    #: The donor kernelcache the prelink step used.
    kernelcache: Optional[Path] = None

    @property
    def kernel(self) -> Path:
        return self.root / "kernel/kernel.macho"

    @property
    def esp_dir(self) -> Path:
        """The payload's ESP, which carries both the boot config and the tree.

        ``build-macos.sh`` passes it as ``--esp "$OUT_DIR/payload-esp"``, and
        ``$OUT_DIR`` is ``build-output`` -- NOT the work dir -- so this is a
        parameter in practice and the work-dir spelling is only the shape a
        caller with the default layout gets.
        """
        return self.esp or self.root / "payload-esp"

    @property
    def devicetree_path(self) -> Path:
        """The device tree this build holds, resolved rather than assumed.

        A build tree carries up to three files named ``kernel.macho.adt`` and
        they are NOT interchangeable.  Measured on run 40369137221, whose boot
        inputs bundle lists all of them:

        * ``payload-esp/EFI/UniXNU/Payloads/kernel.macho.adt`` (17768 bytes) --
          written by ``unixnu/scripts/unixnu/payload.py:149``
          (``'--output', payloads / 'kernel.macho.adt'``).  This is the one the
          guest boots: ``unixnu.toml`` names ``Payloads/kernel.macho.adt`` and
          ``build-macos.sh:3031`` records exactly this path as the ``devicetree``
          role.
        * ``unixnu-out/qemu/tree/build/darwinboot-*/esp/.../kernel.macho.adt``
          (17624 bytes, two of them) -- the bootloader stage's own ESPs, built
          before the payload and stale relative to it.  The second build pass's
          tree is 144 bytes larger because it carries ``--trust-cache`` and
          ``--packed-data-ramdisk``, which is a difference a manifest that took
          the wrong one would never show.
        * ``unixnu-out/qemu/kernel/kernel.macho.adt`` -- written by
          ``build-macos.sh:3430``'s ``build-devicetree/adt`` sub-step, which
          ``build-macos.sh:3358-3365`` records ``skipped`` when the payload step
          has already produced one, precisely to avoid leaving two.

        So the order is the boot's own: the ESP tree first, the devicetree
        stage's second.  The fallback is not dead code -- it is the tree that
        exists on a run where build-payload did not produce one -- and without
        it such a run is refused for a file that is sitting right there.

        This reads the filesystem, which :attr:`kernel` and :attr:`trust_cache`
        do not: the whole point is that the answer depends on which file the
        build actually wrote, and a property that guessed would be the
        ambiguity above made permanent.
        """
        if self.devicetree is not None:
            return self.devicetree
        for _, path in self.devicetree_candidates:
            if path.is_file():
                return path
        # Nothing is on disk.  The ESP spelling is the one to name in the
        # refusal, because that is the tree the build was supposed to produce
        # and the one a boot loads.
        return self.esp_dir / "EFI/UniXNU/Payloads/kernel.macho.adt"

    @property
    def devicetree_candidates(self) -> tuple[tuple[str, Path], ...]:
        """Every place a device tree can be, each with the stage that writes it.

        In the order :attr:`devicetree_path` looks.  The source travels with the
        path so the manifest names the producer of the file it actually hashed;
        a source restated beside a path that was resolved elsewhere is the
        "somewhere is not a source" failure this module exists to catch.
        """
        return (
            ("build-macos.sh/build-payload",
             self.esp_dir / "EFI/UniXNU/Payloads/kernel.macho.adt"),
            ("build-macos.sh/build-devicetree",
             self.root / "kernel/kernel.macho.adt"),
        )

    @property
    def devicetree_source(self) -> str:
        """Which stage produced :attr:`devicetree_path`.

        Derived from the resolved path rather than passed in, because the two
        producers write two different paths and the manifest's ``source`` field
        means nothing if it can name a stage that did not write the file it
        describes.  An explicit ``--devicetree`` override outside both places is
        attributed to the devicetree stage: that is the stage which produces a
        tree anywhere other than the ESP, and it is what the previous
        single-path version recorded.
        """
        try:
            self.devicetree_path.relative_to(self.esp_dir)
        except ValueError:
            return "build-macos.sh/build-devicetree"
        return "build-macos.sh/build-payload"

    @property
    def source_tree(self) -> Path:
        """``unixnu``'s own build tree, which is a *child* of :attr:`root`.

        ``unixnu/config.py:80-82`` derives them separately:

            self.work = self.out_root / self.name     # <out>/<board name>
            self.tree = self.work / 'tree'            # <out>/<board name>/tree

        ``<board name>`` is board.toml's ``[board] name`` -- ``qemu`` -- and NOT
        the defconfig, so on the board this repository builds the two are
        ``build-output/unixnu-out/qemu`` and
        ``build-output/unixnu-out/qemu/tree``.  Everything ``unixnu`` writes
        through ``ctx.tree`` (the kernel build, the chain, every archive
        builder) lands under ``tree/``; everything the driver writes through
        ``ctx.work`` (the prelinked kernel, the payload, the boot image, the
        generated ``.config``) lands directly under it.  Measured on run
        40339678974, whose kernel log names both paths in one line:

            bash .../build-output/unixnu-out/qemu/tree/scripts/build-xnu-4k-baseline.sh
            ... --output .../build-output/unixnu-out/qemu/kernel/kernel.macho

        The workflow's own chain step spells the split the same way
        (``tree="$(ls -d "$OUT_ROOT"/*/tree)"; work="$(dirname "$tree")"``,
        build-macos.yml:1636-1638), so this is not a private convention.
        """
        return self.root / "tree"

    @property
    def trust_cache(self) -> Path:
        """The chain's trust bundle, which lives in the tree's ``build/``.

        ``build-original-system-trust.py:10,54-56`` derives ``ROOT`` from
        ``__file__`` (``<tree>/scripts/build-original-system-trust.py``) and
        writes ``ROOT/build/original-static-system-trust/``.  It is in the
        tree's ``build/`` and not in :attr:`root`'s because ``tree.py:48-49``
        makes ``build/`` a fresh directory per tree while ``.cache`` is shared.
        """
        return self.source_tree / "build/original-static-system-trust/original-static-system.bundle"

    @property
    def data_seed(self) -> Path:
        return self.device_root / ".cache/firmware/owner-data/owner-data-1g.dbr"

    @property
    def owner_data_header(self) -> Path:
        """The header the kernel build wrote, which is what the kernel compiled.

        ``build-xnu-4k-baseline.sh:394-411`` writes it from
        ``$XNU_OWNER_DATA_VOLUME_UUID`` at every kernel build and refuses a value
        that is not a UUID, so it is the authoritative record of what the kernel
        was built with -- not this module's opinion.  ``kernel.py:100`` runs that
        script with ``cwd=ctx.tree``, and the script derives ``ROOT`` from
        ``BASH_SOURCE`` (line 5), so the header lands in the tree's ``build/``
        -- which is why this is :attr:`source_tree` and not :attr:`root`.

        Measured on run 40339678974: the stage that looked for it under
        ``unixnu-out/qemu/build/xnu/owner-data-volume.h`` recorded ``not_run``
        on both of that run's build passes, and the kernel log from the same
        seconds shows the script that writes it running from
        ``unixnu-out/qemu/tree/``.  One missing path segment, and it cost the
        run its entire encrypted Data delivery.
        """
        return self.source_tree / "build/xnu/owner-data-volume.h"

    @property
    def boot_config(self) -> Path:
        return self.esp_dir / "EFI/UniXNU/unixnu.toml"

    @property
    def volumes_dir(self) -> Path:
        return self.volumes or self.device_root / ".cache/firmware/target-volumes"

    @property
    def os_root_image(self) -> Path:
        return self.volumes_dir / "044-76040-038.dmg"

    @property
    def systemos_image(self) -> Path:
        return self.volumes_dir / "044-76034-038.dmg"


def read_owner_data_header(path: Path) -> str:
    """The Data volume UUID the kernel was compiled with, from its own header.

    ``build-xnu-4k-baseline.sh`` writes one of two things into
    ``build/xnu/owner-data-volume.h``: the ``#define`` when the owner-data mount
    is enabled, or ``/* Scratch Data IOKit selection disabled. */`` when it is
    not.  Both are read here, because a kernel that does not embed the UUID
    cannot mount the Data volume and a manifest that says otherwise is lying.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as failure:
        raise _refuse(
            RULE_KERNELCONFIG_ABSENT,
            ErrorCode.ARTIFACT_MISSING,
            f"the kernel's owner-data-volume.h could not be read: {failure}",
            header=safe_location(path),
        ) from failure
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#define") and "XNU_OWNER_DATA_VOLUME_UUID" in stripped:
            parts = stripped.rsplit('"', 2)
            if len(parts) >= 2:
                return parts[-2]
    return ""


def _sha256_of(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def collect(
    tree: BuildTree,
    *,
    build_id: str,
    board_pins: Optional[BoardPins] = None,
    board: str = "qemu_root_defconfig",
    produced_by: str = "build-macos.sh/material-manifest",
    runner_run: str = "",
    device_commit: str = "",
    unixnu_commit: str = "",
    notes: Sequence[str] = (),
) -> MaterialManifest:
    """Build the manifest for what ``tree`` holds.

    Every file is hashed here rather than by the caller, so the manifest's
    digests come from the same bytes :func:`verify_manifest` will read.  A file
    that is absent is refused with ``artifact-absent``: a manifest that records a
    role as present when the build did not produce it is the exact failure this
    module exists to prevent, and it is the one a silently-empty entry would
    cause.

    The Data volume UUID is taken from the kernel's own header rather than from
    ``seed.json``, because the header is what the kernel compiled with and
    ``seed.json`` is what the 32 MiB container was formatted with -- and those
    are the two values audit-r8.md 11.10 says have never been compared.
    """
    if not build_id.strip():
        raise _refuse(
            RULE_BUILD_ID,
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "a material manifest needs a build id; without one no two artifacts "
            "can be shown to come from the same build",
        )
    volume_uuid = read_owner_data_header(tree.owner_data_header)
    if not volume_uuid:
        raise _refuse(
            RULE_DATA_UUID,
            ErrorCode.VERIFICATION_FAILED,
            "the kernel's owner-data-volume.h embeds no Data volume UUID, so this "
            "kernel cannot mount a Data volume at /private/var "
            "(build-xnu-4k-baseline.sh:405 requires one)",
            header=safe_location(tree.owner_data_header),
        )
    files: dict[str, tuple[str, Path]] = {
        "kernel": ("build-macos.sh/build-kernel", tree.kernel),
        "devicetree": (tree.devicetree_source, tree.devicetree_path),
        "trust_cache": ("build-macos.sh/build-os-root", tree.trust_cache),
        "data_seed": ("build-macos.sh/build-os-root", tree.data_seed),
        "systemos_image": ("ipsw", tree.systemos_image),
        "os_root_image": ("ipsw", tree.os_root_image),
        "kernelcache": ("ipsw", tree.kernelcache or Path("")),
        "boot_config": ("build-macos.sh/build-payload", tree.boot_config),
    }
    #: The two members PRD 12 requires be deleted the moment extraction finishes,
    #: so the build that writes this manifest does not have them and their
    #: identity is the board's pin.  Everything else has to be on disk: a role
    #: with neither bytes nor a pin is a role nobody can check.
    board_pinned = {
        "os_root_image": (board_pins.os_root_member_sha256 if board_pins else ""),
        "systemos_image": (board_pins.systemos_member_sha256 if board_pins else ""),
    }
    artifacts: dict[str, MaterialEntry] = {}
    #: leaf name -> the role that owns it, so a second role claiming the same
    #: leaf is refused instead of overwriting the first in a flat delivery.
    leaves: dict[str, str] = {}
    recorded: list[str] = list(notes)
    # A tree that is not the boot's own, or a build carrying more than one, is
    # recorded rather than silently resolved.  ``verify_manifest`` checks the
    # tree it is given for the board's root-match UUID and every candidate above
    # carries that, so a manifest that took the bootloader stage's stale tree
    # would verify clean and describe a file no boot reads.  The note is what
    # makes the choice visible; see :attr:`BuildTree.devicetree_path`.
    #
    # The note names the stage that was passed over rather than counting files:
    # ``devicetree_candidates`` is where a tree may be looked for, not an
    # inventory of a build -- run 40369137221 carried three files of this name
    # and only two of them are candidates.
    if tree.devicetree is None:
        chosen = tree.devicetree_path
        _, esp_tree = tree.devicetree_candidates[0]
        others = [source for source, path in tree.devicetree_candidates
                  if path != chosen and path.is_file()]
        if chosen != esp_tree:
            recorded.append(
                "devicetree is the build-devicetree stage's own tree rather "
                "than the payload's: build-payload did not produce one on the "
                "ESP, so this is the tree the board has"
            )
        elif others:
            recorded.append(
                f"the build also carries the build-devicetree stage's own "
                f"{chosen.name}; the manifest describes the payload's, which is "
                "the one unixnu.toml loads"
            )
    for role in MATERIAL_ROLES:
        if role == "volume_uuid":
            continue
        source, path = files[role]
        present = bool(path) and path.is_file()
        if present:
            digest, size = _sha256_of(path)
            relative = _relative(path, tree)
        elif role in board_pinned and board_pinned[role]:
            digest, size = board_pinned[role], 0
            recorded.append(
                f"{role} was not on disk when this manifest was written (PRD 12 "
                "deletes the extracted member), so its identity is board.toml "
                "[ipsw.sha256]'s pin rather than a hash of the bytes"
            )
        else:
            raise _refuse(
                RULE_ARTIFACT_ABSENT,
                ErrorCode.ARTIFACT_MISSING,
                f"the build did not produce '{role}': "
                f"{safe_location(path) if path else '<not named>'}",
                role=role,
                path=safe_location(path) if path else "",
            )
        artifacts[role] = MaterialEntry(
            role=role,
            source=source,
            # The leaf name, because the delivery is a flat directory: a manifest
            # that carried the runner's directory structure could not be read on
            # a host that downloaded the material into one place -- the same
            # reason the boot-inputs bundle's manifest uses relative-to-bundle
            # names.  Two roles landing on one leaf would silently overwrite each
            # other in that directory, so the collision is refused here rather
            # than discovered later as a hash mismatch on the wrong file.
            path=path.name,
            sha256=digest,
            bytes=size,
        )
        if path.name and path.name in leaves:
            raise _refuse(
                RULE_LEAF_COLLISION,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                f"'{role}' and '{leaves[path.name]}' both deliver as "
                f"{path.name!r}, so a flat delivery would overwrite one of them "
                "and the manifest would describe a file that was replaced",
                role=role,
                other_role=leaves[path.name],
                leaf=path.name,
            )
        if path.name:
            leaves[path.name] = role
    artifacts["volume_uuid"] = MaterialEntry(
        role="volume_uuid",
        source="build-macos.sh/build-kernel",
        value=volume_uuid,
    )
    return MaterialManifest(
        schema=MATERIAL_SCHEMA,
        build_id=build_id,
        board=board,
        artifacts=artifacts,
        produced_by=produced_by,
        runner_run=runner_run,
        device_commit=device_commit,
        unixnu_commit=unixnu_commit,
        notes=tuple(recorded),
    )


def _relative(path: Path, tree: BuildTree) -> str:
    """``path`` as a slash-separated name under whichever tree root contains it.

    Kept for a caller that wants the tree's own structure rather than the flat
    delivery's; :func:`collect` itself uses the leaf name, because the manifest
    has to be readable on a host that downloaded the material into one directory.
    """
    path = Path(path)
    for base in (tree.root, tree.device_root, tree.esp, tree.volumes):
        if base is None:
            continue
        try:
            return path.relative_to(Path(base)).as_posix()
        except ValueError:
            continue
    return path.name


# ---------------------------------------------------------------------------
# the host side: what a boot is allowed to consume
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VerifyReport:
    """What :func:`verify_manifest` established, for the caller's record."""

    build_id: str
    checked: tuple[str, ...]
    seed: Optional[packed_ramdisk.PackedRamdisk] = None
    volume_uuid: str = ""
    container_uuid: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "checked": list(self.checked),
            "volume_uuid": self.volume_uuid,
            "container_uuid": self.container_uuid,
            "seed": (
                {
                    "sha256": self.seed.sha256,
                    "logical": self.seed.logical,
                    "resident": self.seed.resident,
                    "extents": len(self.seed.extents),
                }
                if self.seed
                else None
            ),
        }


def _is_uuid(text: str) -> bool:
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _contains_bytes(path: Path, needle: bytes) -> bool:
    """Whether ``needle`` occurs in ``path``, read in chunks.

    A 50 MB kernel is read once and never held whole.  The overlap is
    ``len(needle) - 1`` so a needle that straddles a chunk boundary is still
    found -- without it a UUID split across two reads would be reported as
    absent, which is a refusal with the wrong reason.
    """
    if not needle:
        return False
    keep = len(needle) - 1
    tail = b""
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return False
            if needle in tail + chunk:
                return True
            tail = (tail + chunk)[-keep:] if keep else b""


def _seed_pages(path: Path, seed: packed_ramdisk.PackedRamdisk) -> list[bytes]:
    """Every 4096-byte page the packed seed actually carries, in file order.

    Read once and held, because a 1.2 MB seed is small and it is walked twice --
    once for the container superblock and once for the volume superblock.  The
    alternative, reopening the file, is how a seed that changed between the two
    reads would pass both.
    """
    blob = path.read_bytes()
    at = _ENVELOPE.size + 16 * len(seed.extents)
    pages: list[bytes] = []
    for extent in seed.extents:
        chunk = blob[at : at + extent.length]
        at += extent.length
        for offset in range(0, len(chunk), APFS_BLOCK_SIZE):
            page = chunk[offset : offset + APFS_BLOCK_SIZE]
            if len(page) == APFS_BLOCK_SIZE:
                pages.append(page)
    return pages


def _container_superblock(pages: Sequence[bytes], path: Path) -> bytes:
    """The seed's container superblock, or a refusal.

    Block 0 of the packed disk is the container's first superblock
    (``nx_xp_desc_1``).  ``build-os-root.py:114-117`` reads exactly these fields
    to prove the grown seed is a 1 GiB APFS container, so the offsets are the
    chain's own, not this module's guess.
    """
    for page in pages:
        if page[_NX_MAGIC : _NX_MAGIC + 4] == b"NXSB":
            return page
    raise _refuse(
        RULE_SEED_CONTAINER,
        ErrorCode.VERIFICATION_FAILED,
        "the packed Data seed carries no APFS container superblock, so it is not "
        "an APFS container at all",
        seed=safe_location(path),
    )


def _guid(raw: bytes) -> str:
    """APFS stores a UUID as 16 raw bytes in the standard mixed-endian layout.

    ``uuid.UUID(bytes=...)`` is that layout, and it is what the driver's own
    ``uuid_unparse`` prints -- which is what ``diskutil`` reports as
    ``VolumeUUID``.  The tempting alternative, unpacking the first three fields
    little-endian, reads a real UUID as its own byte-reversal: measured against
    the value this module is built around, ``18AB5557-DC2D-469B-...`` comes back
    as ``5755AB18-2DDC-9B46-...``.  The project's own VerifiedRoot kit already
    treats an APFS UUID field this way (``kits/VerifiedRoot/tests/check.py:78``:
    ``bytes(view.volume_uuid) == uuid.UUID(record['volume_uuid']).bytes``), so
    this is the second implementation to agree with the first rather than a new
    convention.
    """
    if len(raw) != 16:
        return ""
    return str(uuid.UUID(bytes=bytes(raw))).upper()


def verify_manifest(
    manifest: MaterialManifest,
    root: Path,
    *,
    board_pins: Optional[BoardPins] = None,
    kernel_image: Optional[Path] = None,
    expected_build_id: Optional[str] = None,
) -> VerifyReport:
    """Refuse unless every piece of material belongs to ``manifest.build_id``.

    ``root`` is the directory the manifest's relative paths resolve against --
    the build tree on the runner, the download directory on the host.  It is a
    parameter rather than a constant because those are two different machines.

    ``expected_build_id`` is what turns "every artifact shares one build id" into
    a refusal.  Without it the manifest is only checked against *itself*, which
    is exactly how a kernel from one run and a seed from another pass: the host
    names the build it is about to boot and anything else is refused.  A caller
    that does not know which build it wants gets the structural and byte checks
    and not this one, and that is said in the report rather than assumed.

    The order is deliberate: structure first, then bytes, then bindings.  A
    manifest that names a role nobody knows is refused before anything is
    hashed, because "what is this?" has to be answered before "is it right?".
    """
    root = Path(root)
    checked: list[str] = []

    # -- structure --------------------------------------------------------
    if manifest.schema != MATERIAL_SCHEMA:
        raise _refuse(
            RULE_SCHEMA,
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            f"the manifest's schema is {manifest.schema!r}, not {MATERIAL_SCHEMA!r}",
            schema=manifest.schema,
        )
    if not manifest.build_id.strip():
        raise _refuse(
            RULE_BUILD_ID,
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "the manifest carries no build id, so no artifact in it can be shown "
            "to come from one build",
        )
    if expected_build_id is not None and manifest.build_id != expected_build_id:
        raise _refuse(
            RULE_BUILD_ID,
            ErrorCode.VERIFICATION_FAILED,
            f"the manifest is from build {manifest.build_id!r} and this boot wants "
            f"{expected_build_id!r}",
            on_disk=manifest.build_id,
            wanted=expected_build_id,
        )
    for role in MATERIAL_ROLES:
        if role not in manifest.artifacts:
            raise _refuse(
                RULE_ENTRY_MISSING,
                ErrorCode.ARTIFACT_MISSING,
                f"the manifest has no '{role}' entry; a manifest that omits a role "
                "cannot be told apart from one whose build never made it",
                role=role,
            )
    for role, entry in manifest.artifacts.items():
        if role not in MATERIAL_ROLES:
            raise _refuse(
                RULE_ROLE_UNKNOWN,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                f"the manifest carries a '{role}' entry, which is not a role a boot "
                "consumes",
                role=role,
            )
        if entry.source not in KNOWN_SOURCES:
            raise _refuse(
                RULE_SOURCE_UNKNOWN,
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                f"'{role}' claims source {entry.source!r}, which is not a producer "
                "this module knows",
                role=role,
                source=entry.source,
            )
    checked.append("structure")

    # -- bytes ------------------------------------------------------------
    resolved: dict[str, Path] = {}
    for role in MATERIAL_ROLES:
        entry = manifest.artifacts[role]
        if role == "volume_uuid":
            if not _is_uuid(entry.value):
                raise _refuse(
                    RULE_VALUE_MALFORMED,
                    ErrorCode.ARTIFACT_MANIFEST_INVALID,
                    f"the Data volume UUID is {entry.value!r}, which is not a UUID",
                    role=role,
                    value=entry.value,
                )
            checked.append(role)
            continue
        path = root / entry.path if entry.path else Path("")
        present = bool(entry.path) and path.is_file()
        if not present and role not in ("os_root_image", "systemos_image"):
            raise _refuse(
                RULE_ARTIFACT_ABSENT,
                ErrorCode.IMAGE_NOT_FOUND,
                f"'{role}' is named as {entry.path or '<nothing>'} but that file is "
                "not there",
                role=role,
                path=entry.path,
            )
        if present:
            size = path.stat().st_size
            if size != entry.bytes:
                raise _refuse(
                    RULE_ARTIFACT_SIZE,
                    ErrorCode.IMAGE_HASH_MISMATCH,
                    f"'{role}' is {size} bytes on disk and the manifest says "
                    f"{entry.bytes}",
                    role=role,
                    path=entry.path,
                    on_disk=size,
                    recorded=entry.bytes,
                )
            digest, _ = _sha256_of(path)
            if digest != entry.sha256:
                raise _refuse(
                    RULE_ARTIFACT_HASH,
                    ErrorCode.IMAGE_HASH_MISMATCH,
                    f"'{role}' does not hash to what the manifest records",
                    role=role,
                    path=entry.path,
                    on_disk=digest,
                    recorded=entry.sha256,
                )
            resolved[role] = path
        checked.append(role)
    checked.append("bytes")

    # -- the board's pins -------------------------------------------------
    volume_uuid = str(uuid.UUID(manifest.artifacts["volume_uuid"].value)).upper()
    if board_pins is not None:
        donor = manifest.artifacts["kernelcache"]
        if donor.sha256 != board_pins.kernelcache_sha256.lower():
            raise _refuse(
                RULE_KERNELCACHE_PIN,
                ErrorCode.VERIFICATION_FAILED,
                "the donor kernelcache does not match board.toml "
                "[kernel.prelink] kernelcache_sha256, so the kernel was not built "
                "from the pinned original",
                on_disk=donor.sha256,
                pinned=board_pins.kernelcache_sha256,
            )
        adt = manifest.artifacts["devicetree"]
        # The ADT publishes the root-match UUID as an XML string under
        # /chosen/root-matching, so the bytes are the check and no ADT parser is
        # involved: measured on local-private/boot7, the tree really does carry
        # the board's value verbatim.
        adt_bytes = (resolved.get("devicetree") or root / adt.path).read_bytes()
        if board_pins.root_match_volume_uuid.upper().encode() not in adt_bytes:
            raise _refuse(
                RULE_ROOT_UUID_PIN,
                ErrorCode.VERIFICATION_FAILED,
                "the device tree does not publish board.toml [payload.adt] "
                "root_match_volume_uuid, so the kernel would wait for a root "
                "volume this build does not have",
                pinned=board_pins.root_match_volume_uuid,
            )
        for role, pin in (
            ("os_root_image", board_pins.os_root_member_sha256),
            ("systemos_image", board_pins.systemos_member_sha256),
        ):
            if pin and manifest.artifacts[role].sha256 != pin:
                raise _refuse(
                    RULE_IPSW_PIN,
                    ErrorCode.VERIFICATION_FAILED,
                    f"'{role}' does not match board.toml [ipsw.sha256], so the "
                    "volume this build used is not the pinned one",
                    role=role,
                    on_disk=manifest.artifacts[role].sha256,
                    pinned=pin,
                )
        checked.append("board-pins")

    # -- the seed's structure ---------------------------------------------
    seed_path = resolved["data_seed"]
    try:
        seed = packed_ramdisk.validate_file(seed_path)
    except UniPadError as failure:
        raise _refuse(
            RULE_SEED_ENVELOPE,
            ErrorCode.IMAGE_HASH_MISMATCH,
            f"the packed Data seed is not a seed both parsers accept: {failure}",
            seed=safe_location(seed_path),
        ) from failure
    pages = _seed_pages(seed_path, seed)
    container = _container_superblock(pages, seed_path)
    container_uuid = _guid(container[_NX_UUID : _NX_UUID + 16])
    block_size = struct.unpack_from("<I", container, _NX_BLOCK_SIZE)[0]
    block_count = struct.unpack_from("<Q", container, _NX_BLOCK_COUNT)[0]
    if block_size != APFS_BLOCK_SIZE or block_count * block_size != seed.logical:
        raise _refuse(
            RULE_SEED_CONTAINER,
            ErrorCode.VERIFICATION_FAILED,
            f"the seed's APFS container is {block_count} blocks of {block_size} "
            f"bytes ({block_count * block_size}), which is not the {seed.logical}-byte "
            "disk the packed seed describes",
            block_size=block_size,
            block_count=block_count,
            logical=seed.logical,
        )
    if not container_uuid or container_uuid in _PLACEHOLDER_CONTAINER_UUIDS:
        raise _refuse(
            RULE_SEED_CONTAINER,
            ErrorCode.VERIFICATION_FAILED,
            f"the seed's APFS container UUID is {container_uuid or '(none)'}, which is "
            "a hand-written placeholder rather than a UUID a formatter chose",
            container_uuid=container_uuid,
        )
    # A container with no volume, or with no checkpoint area, cannot be mounted
    # however good its superblock looks -- the second is the finding
    # local-private/firmware/compare-apfs-containers.py records.
    if struct.unpack_from("<Q", container, _NX_FIRST_FS_OID)[0] == 0:
        raise _refuse(
            RULE_SEED_CONTAINER,
            ErrorCode.VERIFICATION_FAILED,
            "the seed's APFS container declares no volume (nx_fs_oid[0] is zero), "
            "so it cannot host the Data volume",
            container_uuid=container_uuid,
        )
    if struct.unpack_from("<I", container, _NX_XP_DESC_BLOCKS)[0] == 0:
        raise _refuse(
            RULE_SEED_CONTAINER,
            ErrorCode.VERIFICATION_FAILED,
            "the seed's APFS container declares no checkpoint descriptor area "
            "(nx_xp_desc_blocks is zero), so it cannot be mounted however good "
            "its superblock looks",
            container_uuid=container_uuid,
        )
    checked.append("seed-container")

    # -- the binding that was unverified for three rounds ------------------
    #
    # audit-r8.md 11.10: the volume UUID recorded in seed.json is the 32 MiB
    # container's, and the 1 GiB container is made afterwards by
    # `diskutil apfs resizeContainer`, whose effect on the volume UUID was never
    # measured.  This is that measurement, on the host, from the seed the guest
    # will actually be handed: re-derive the volume UUID from the seed's own
    # resident volume superblock and require it to be the one the kernel was
    # built with.
    #
    # `apfs_vol_uuid` is at 0xF0 of the volume superblock (apfsprogs' raw.h, as
    # transcribed by local-private/firmware/dump-nxsb-apsb.py).  The volume
    # superblock is found by scanning the resident pages for the 'APSB' magic
    # rather than by indexing nx_fs_oid[0], because that oid is *virtual* --
    # reading block `oid` gives a zero block, and the only way to the volume
    # superblock is through the container's object map.
    found = {
        candidate
        for page in pages
        if page[_APFS_MAGIC : _APFS_MAGIC + 4] == b"APSB"
        for candidate in (_guid(page[_APFS_VOL_UUID : _APFS_VOL_UUID + 16]),)
        if candidate
    }
    if not found:
        raise _refuse(
            RULE_SEED_VOLUME,
            ErrorCode.VERIFICATION_FAILED,
            "the packed Data seed carries no volume superblock, so its volume UUID "
            "cannot be established from the seed itself -- which is what a "
            "hand-written stub looks like",
            seed=safe_location(seed_path),
        )
    if found != {volume_uuid}:
        raise _refuse(
            RULE_SEED_VOLUME,
            ErrorCode.VERIFICATION_FAILED,
            f"the seed's volume UUID is {sorted(found)}, and the kernel was built "
            f"for {volume_uuid}",
            in_seed=sorted(found),
            in_kernel=volume_uuid,
        )
    checked.append("seed-volume")

    # -- the kernel really carries it -------------------------------------
    image = kernel_image or resolved["kernel"]
    if not _contains_bytes(image, volume_uuid.encode()):
        raise _refuse(
            RULE_KERNEL_UUID_ABSENT,
            ErrorCode.VERIFICATION_FAILED,
            f"the kernel image does not contain the Data volume UUID {volume_uuid} "
            "it was supposedly built with",
            kernel=safe_location(image),
            volume_uuid=volume_uuid,
        )
    checked.append("kernel-uuid")

    return VerifyReport(
        build_id=manifest.build_id,
        checked=tuple(checked),
        seed=seed,
        volume_uuid=volume_uuid,
        container_uuid=container_uuid,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m unipad.host.material_manifest",
        description=(
            "Collect the material manifest a real build produced, or verify one "
            "against the material a host holds."
        ),
    )
    parser.add_argument("--collect", action="store_true", help="write a manifest for a build tree")
    parser.add_argument("--verify", action="store_true", help="verify a manifest against a directory")
    parser.add_argument("--tree", type=Path, help="the build tree (--collect)")
    parser.add_argument("--device", type=Path, help="the device repository root (--collect)")
    parser.add_argument("--esp", type=Path, help="the payload's ESP (--collect)")
    parser.add_argument("--kernelcache", type=Path, help="the donor kernelcache (--collect)")
    parser.add_argument("--volumes", type=Path, help="where the IPSW members were extracted (--collect)")
    parser.add_argument("--manifest", type=Path, help="the manifest to read or write")
    parser.add_argument("--root", type=Path, help="the directory the manifest's paths resolve against (--verify)")
    parser.add_argument("--expect-build", default="", help="refuse a manifest from any other build (--verify)")
    parser.add_argument("--board", type=Path, help="board.toml, for the pins")
    parser.add_argument("--build-id", default="", help="the build identifier every artifact must share")
    parser.add_argument("--board-name", default="qemu_root_defconfig")
    parser.add_argument("--runner-run", default="")
    parser.add_argument("--device-commit", default="")
    parser.add_argument("--unixnu-commit", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not (args.collect or args.verify):
        parser.error("pass --collect or --verify")

    if args.collect:
        if not args.tree or not args.device:
            parser.error("--collect needs --tree and --device")
        pins = read_board_pins(args.board) if args.board else None
        manifest = collect(
            BuildTree(
                root=args.tree,
                device_root=args.device,
                esp=args.esp,
                volumes=args.volumes,
                kernelcache=args.kernelcache,
            ),
            build_id=args.build_id,
            board_pins=pins,
            board=args.board_name,
            runner_run=args.runner_run,
            device_commit=args.device_commit,
            unixnu_commit=args.unixnu_commit,
        )
        if args.manifest:
            manifest.write(args.manifest)
        if not args.quiet:
            print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
        return int(ExitCode.OK)

    if not args.manifest:
        parser.error("--verify needs --manifest")
    manifest = MaterialManifest.load(args.manifest)
    pins = read_board_pins(args.board) if args.board else None
    report = verify_manifest(
        manifest,
        args.root or Path("."),
        board_pins=pins,
        expected_build_id=args.expect_build or None,
    )
    if not args.quiet:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return int(ExitCode.OK)


if __name__ == "__main__":  # pragma: no cover - the CLI is exercised as a process
    try:
        sys.exit(main())
    except UniPadError as failure:
        print(f"material_manifest: {failure}", file=sys.stderr)
        sys.exit(int(getattr(failure, "exit_code", ExitCode.FAILURE)))
