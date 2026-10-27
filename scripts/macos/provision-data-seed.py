#!/usr/bin/env python3
"""Provision the runtime directory skeleton into the packed Data seed.

Why this exists
---------------
The Data seed is a faithful copy of the OS image's ``/private/var`` -- measured
on 2026-10-27 against iPadOS 17.7.11 (21H461): the image's ``/private/var``
holds 29 entries, the seed's volume root holds the same 29 plus ``.fseventsd``
and ``keybags``, and minus ``staged_system_apps``, which is exactly the two
deviations ``prepare-owner-data-fixture.py`` documents.  Nothing was dropped.

What that measurement also established is that **neither** the image nor the
seed contains ``/private/var/containers``.  There is no catalog entry named
``containers`` anywhere in the image -- not one, across 303,965 inodes and
349,284 directory records.  On a real device that directory is created at first
boot by ``containermanagerd``; it does not ship.

An offline seed never gets that first boot, so the directory has to be put
there.  The cost of not putting it there is measured: the guest's log is
saturated with ``Sandbox: no system container path found`` and ``no application
group container found``, and every process that needs a container extension --
SpringBoard, mediaserverd, PerfPowerServices, ReportCrash, syncdefaultsd,
iconservicesagent, audiomxd, liveactivitiesd -- dies.  ``ReportCrash`` dying is
why there is no crash report to read: the thing that would have written it is
one of the things that needed the container.

This is upstream's own answer to the same problem.  ``prepare-offline-data.py``
creates the skeleton with a documented safety property -- no original file or
symlink may change -- and ``seed-data-skip-setup.py`` does the unpack/attach/
repack cycle on a packed seed and adds ``containers/{Bundle,Data/System,
Shared/SystemGroup,Temp}`` on top of the same preference merges.  This script
is that cycle, for OUR seed, with both directory lists and both safety
properties.

What it does NOT do
-------------------
It adds empty directories.  No keys, no user records, no persona, no Apple
data, no preference.  A seed provisioned by this script is still a QEMU test
input, not a restore and not a provisioned device, and it must never be
described as one.

The volume UUID is load-bearing and is checked, not assumed.  The delivery's
manifest binds the kernel that was compiled with this seed's volume UUID to
this seed (implementation-plan fact 88 measured the failure of a mismatch: the
host's kernel embedded ``18AB5557-...`` against a seed carrying
``D2E65836-...`` and the guest's ``native_owner_data_stage`` stayed 0).  A
provisioning step that changed the UUID would produce a seed that mounts and
then binds to nothing, so the UUID is read before and after and any change is a
refusal.

Usage: provision-data-seed.py --seed SEED.dbr --out OUT.dbr [--report REPORT.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from unipad.host.packed_ramdisk import (  # noqa: E402
    HEADER_SIZE,
    PAGE_SIZE,
    validate,
    validate_file,
    pack_file,
)

#: The directories upstream's ``prepare-offline-data.py`` creates, verbatim from
#: its ``DIRECTORIES``.  That script's own docstring says what each group is
#: for: the ``db/dslocal`` tree is so ``usermanagerd`` can create its own
#: system/default/personal personas, and the rest is the container and per-user
#: skeleton.
OFFLINE_DIRECTORIES = (
    "db", "db/dslocal", "db/dslocal/nodes", "db/dslocal/nodes/Default",
    "db/dslocal/nodes/Default/users", "db/dslocal/nodes/Default/groups",
    "db/dslocal/nodes/Default/config",
    "containers", "containers/Data", "containers/Data/System",
    "containers/Data/Application", "containers/Shared",
    "containers/Shared/SystemGroup",
    "PersonaVolumes", "preboot", "root", "tmp", "Library",
    "keybags", "keybags/backup",
    "mobile/Library/Preferences", "mobile/Library/Caches", "mobile/Library/Logs",
    "mobile/Containers", "mobile/Documents", "mobile/Media", "mobile/tmp",
)

#: The extra four upstream's ``seed-data-skip-setup.py`` creates, which
#: ``prepare-offline-data.py`` does not list.  ``Bundle`` is where the system
#: bundle container lives; ``Temp`` is the scratch container the sandbox hands
#: out.  The union of the two lists is what this script creates.
SKIP_SETUP_DIRECTORIES = (
    "containers/Bundle",
    "containers/Temp",
)

#: The two lists together, de-duplicated, in creation order (parents first).
DIRECTORIES = tuple(dict.fromkeys(OFFLINE_DIRECTORIES + SKIP_SETUP_DIRECTORIES))

#: ``com.apple.migration`` and the purplebuddy/springboard keys are NOT set
#: here.  ``seed-data-skip-setup.py`` merges them, and they are what makes a
#: seed boot to SpringBoard instead of to Setup -- but they are *preferences*,
#: and a provisioning step that also silently changes behaviour is a step whose
#: two effects cannot be told apart when something breaks.  Directories here;
#: preferences, if wanted, in a second change with its own evidence.


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def run(*command: str, **kwargs: object) -> subprocess.CompletedProcess:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, check=False, **kwargs)  # type: ignore[arg-type]


def unpack(seed: Path, raw: Path) -> tuple[int, int]:
    """Write the seed's logical disk out as a sparse raw image.

    Returns ``(logical bytes, resident bytes)``.  The resident figure is what
    the evidence needs: a seed whose packed size barely moved after
    provisioning is a seed that gained directories and nothing else, which is
    the whole claim.

    The payloads follow the extent table in file order, so extent N's bytes
    start after the header AND every extent descriptor before it.  That second
    part is load-bearing and was got wrong once: ``HEADER_SIZE`` is 32, so a
    cursor starting there reads the extent table itself as the payload -- and
    the ``NXSB`` check below is what caught it, which is the only reason it is
    checked rather than assumed.
    """
    info = validate_file(seed)
    resident = 0
    at = HEADER_SIZE + len(info.extents) * 16
    with raw.open("wb") as out, seed.open("rb") as handle:
        out.truncate(info.logical)
        for extent in info.extents:
            handle.seek(at)
            out.seek(extent.offset)
            out.write(handle.read(extent.length))
            at += extent.length
            resident += extent.length
    if raw.stat().st_size != info.logical:
        raise SystemExit(f"the unpacked image is {raw.stat().st_size} bytes, "
                         f"not the {info.logical} the seed describes")
    with raw.open("rb") as handle:
        handle.seek(32)
        if handle.read(4) != b"NXSB":
            raise SystemExit("the unpacked image is not an APFS container")
    return info.logical, resident


def inventory(mount: Path) -> dict:
    """Every file and symlink under ``mount``, with a hash of each file.

    Upstream's safety property, verbatim: adding directories must not change
    any original file or symlink.  This is that check, and it is the reason a
    provisioning step that quietly rewrote a preference would be caught here
    rather than in the guest.
    """
    result: dict[str, list] = {}
    for current, dirs, files in os.walk(mount, followlinks=False):
        for name in list(dirs) + files:
            path = Path(current) / name
            key = path.relative_to(mount).as_posix()
            if path.is_symlink():
                result[key] = ["symlink", os.readlink(path)]
            elif path.is_file():
                result[key] = ["file", sha256_of(path)]
            # Directories are deliberately not inventoried: creating the ones
            # this script creates is the entire point, so a directory diff
            # would fail on its own success.
    return result


def volume_uuid_of(mount: Path) -> str:
    listed = run("diskutil", "info", "-plist", str(mount),
                 capture_output=True, text=True)
    if listed.returncode:
        return ""
    info = plistlib.loads(listed.stdout)
    return str(info.get("VolumeUUID", ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, required=True,
                        help="the packed Data seed (.dbr) to provision")
    parser.add_argument("--out", type=Path, required=True,
                        help="the provisioned seed to write (.dbr)")
    parser.add_argument("--report", type=Path, default=None,
                        help="where to write the JSON evidence")
    args = parser.parse_args()

    seed = args.seed.resolve()
    out = args.out.resolve()
    if not seed.is_file():
        print(f"no seed at {seed}")
        return 2
    if out.exists():
        print(f"refusing to overwrite {out}")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)

    packed_before = validate_file(seed)
    print(f"seed {seed.name}: {packed_before.size} bytes packed, "
          f"{packed_before.logical} bytes logical, "
          f"{len(packed_before.extents)} extents, "
          f"{packed_before.resident} resident")

    with tempfile.TemporaryDirectory(prefix="provision-data-") as td:
        work = Path(td)
        raw = work / "seed.raw"
        logical, resident = unpack(seed, raw)
        print(f"unpacked to {raw}: {logical} bytes logical, {resident} resident")

        mount = work / "mount"
        mount.mkdir()
        attached = plistlib.loads(subprocess.check_output([
            "hdiutil", "attach", "-imagekey", "diskimage-class=CRawDiskImage",
            "-nobrowse", "-owners", "off", "-mountpoint", str(mount),
            "-plist", str(raw),
        ]))
        device = attached["system-entities"][0]["dev-entry"]
        print(f"attached {device} at {mount}")
        try:
            if not (mount / "mobile").is_dir():
                raise SystemExit("the seed has no /mobile; wrong base image?")

            uuid_before = volume_uuid_of(mount)
            before = inventory(mount)
            print(f"volume UUID {uuid_before}; {len(before)} files and symlinks "
                  f"before provisioning")

            created = []
            for name in DIRECTORIES:
                path = mount / name
                if not path.exists():
                    path.mkdir(parents=True, exist_ok=True)
                    os.chmod(path, 0o755)
                    created.append(name)
            print(f"created {len(created)} of {len(DIRECTORIES)} directories: "
                  f"{', '.join(created) or '<none; all present>'}")

            after = inventory(mount)
            if before != after:
                changed = sorted(
                    key for key in set(before) | set(after)
                    if before.get(key) != after.get(key)
                )
                raise SystemExit(
                    "provisioning changed an original file or symlink: "
                    + ", ".join(changed[:20])
                    + (" ..." if len(changed) > 20 else "")
                )
            uuid_after = volume_uuid_of(mount)
            if uuid_after != uuid_before:
                raise SystemExit(
                    f"the volume UUID changed: {uuid_before} -> {uuid_after}; "
                    "a seed whose UUID moved no longer binds to the kernel "
                    "that was built for it"
                )
            print(f"inventory unchanged ({len(after)} entries); "
                  f"volume UUID still {uuid_after}")
        finally:
            subprocess.run(["hdiutil", "detach", device], check=True)

        # Upstream's own post-detach check, which I had left out.  ``detach``
        # returns 0 whether or not it flushed, so the only way to know the
        # edits reached the image is to read the image: an image that is no
        # longer an APFS container is an image whose writes went somewhere else,
        # and packing it would ship a seed that mounts nothing.
        with raw.open("rb") as handle:
            handle.seek(32)
            if handle.read(4) != b"NXSB":
                raise SystemExit(
                    "the edited image is no APFS container any more, so the "
                    "provisioned directories did not reach the seed"
                )

        packed = pack_file(raw)
        out.write_bytes(packed)
        print(f"wrote {out}: {len(packed)} bytes packed")

    packed_after = validate_file(out)
    if packed_after.logical != packed_before.logical:
        raise SystemExit(
            f"the provisioned seed describes {packed_after.logical} logical "
            f"bytes, not the {packed_before.logical} the original describes"
        )
    report = {
        "seed": str(seed),
        "seed_sha256": sha256_of(seed),
        "output": str(out),
        "output_sha256": sha256_of(out),
        "logical_bytes": packed_after.logical,
        "packed_bytes_before": packed_before.size,
        "packed_bytes_after": packed_after.size,
        "extents_before": len(packed_before.extents),
        "extents_after": len(packed_after.extents),
        "resident_before": packed_before.resident,
        "resident_after": packed_after.resident,
        "volume_uuid": uuid_after,
        "volume_uuid_unchanged": uuid_after == uuid_before,
        "directories_offered": list(DIRECTORIES),
        "directories_created": created,
        "original_file_symlink_count": len(after),
        "original_file_symlink_inventory_unchanged": before == after,
        "added_keys_or_user_records": False,
        "qemu_only": True,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n",
                               encoding="utf-8")
        print(f"report at {args.report}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
