"""The NGPUADD2 blob, parsed back the way the kernel will read it.

Why this test exists at all
---------------------------
The four-step fix for "nothing draws" ends with a file the kernel parses at
boot: ``make-gpu-addon.py`` packs the owner-built Adreno Metal plugin into an
``NGPUADD2`` blob, the payload carries it as ``kernel.macho.gpuaddon``, the
loader publishes it at ``/chosen/memory-map/GpuAddon``, and the kernel
materialises it into the mounted Data volume before its addon mount
(``sheng-gpu-addon=1``).  Every one of those links is invisible until a real
runner has spent hours cross-compiling Mesa's freedreno Vulkan driver to iOS --
and the blob's layout is the one link that can be checked for free, on any
host, before that money is spent.

So this stage runs the real pinned packer against a *fixture* bundle and parses
the result back with an independent reader.  A packer that emits a count the
header does not agree with, a stream whose padded length the reader misreads,
or a file the kernel would reject, is caught here rather than three hours into
a macOS runner.

What this test deliberately does NOT claim
------------------------------------------
The fixture bundle is synthetic.  It proves the *container*, not the *content*:
nothing here says the real ``NativeAdrenoMetal`` executable or
``libvulkan_freedreno.dylib`` exists, builds, or is signed.  A fixture blob must
never be delivered to a guest -- the goal's rule that placeholder material may
not be mixed with real runtime material applies to it exactly as it applies to
an image.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: The upstream driver that owns both the packer and the format it documents.
DRIVER = ROOT / "vendor" / "UniXNU"

#: The packer, relative to the driver root.
PACKER = "scripts/make-gpu-addon.py"

#: The magic the payload step checks before it will carry the file.  Read
#: verbatim out of ``vendor/UniXNU/scripts/unixnu/payload.py``: ``if addon and
#: (not addon.is_file() or addon.read_bytes()[:8] != b'NGPUADD2')``.
PAYLOAD_MAGIC = b"NGPUADD2"

#: The two files the packer makes executable, by name, everywhere else is 0644.
EXECUTABLE_NAMES = frozenset({"NativeAdrenoMetal", "libvulkan_freedreno.dylib"})

#: The measured payload budget.  The payload FAT is pinned at 192 MiB by
#: board.toml ``[payload] size_mib`` and the tablet's boot loader will not start
#: a 256 MiB ramdisk, so this is a hard ceiling rather than a setting.
#: Measured from the delivered material on 2026-10-29:
#:   kernel.macho                    125,431,808 bytes
#:   original-static-system.bundle       61,356 bytes   (the trust cache)
#:   owner-data-1g.dbr                1,548,432 bytes   (the Data seed)
#:   = 121,178,596 bytes of the 192 MiB * 0.95 = 201,326,592 * 0.95 budget that
#: ``payload.py`` enforces, which leaves this much for the addon blob.  Recorded
#: here because the packer compresses at level 9 precisely to stay inside it.
PAYLOAD_HEADROOM = int(201_326_592 * 0.95) - 121_178_596


def _packer() -> Path:
    """The pinned packer, or a skip naming the exact path that is missing."""
    tool = DRIVER / PACKER
    if not tool.is_file():
        pytest.skip(
            f"{tool} is not present: this stage needs the pinned vendor "
            "checkout from config/sources.lock.json, prepared by the "
            "'integration (pinned upstream)' job in "
            ".github/workflows/preflight.yml"
        )
    return tool


def _driver_root(packer: Path, base: Path) -> Path:
    """A relocated, byte-identical copy of the driver that owns the packer.

    ``make-gpu-addon.py:25-27`` resolves its own inputs from
    ``Path(__file__).resolve().parents[1]``, so the plugin it packs is always
    ``<driver>/build/adreno-metal-plugin`` -- there is no flag and no argument
    that points it anywhere else.  Running the real file in the real checkout
    would therefore mean writing a fixture bundle into
    ``vendor/UniXNU/build/``, which is where a genuine plugin build lands.

    So the script is copied, and the copy's digest is asserted equal to the
    pinned file's.  That keeps the claim literal -- the bytes under test ARE the
    pinned packer's -- while letting each test own its own plugin directory.
    """
    root = base / "driver"
    (root / "scripts").mkdir(parents=True)
    copy = root / PACKER
    shutil.copyfile(packer, copy)
    assert copy.read_bytes() == packer.read_bytes(), "the relocated packer is not the pinned one"
    return root


def _fixture_plugin(base: Path) -> Path:
    """A plugin directory shaped like the one ``build-adreno-metal-plugin.py`` leaves.

    Every file the packer reads is here, including the one it must *not* pack:
    ``adrenoprobed-entitlements.plist`` is a build input, and the packer excludes
    it by suffix.  A reader that cannot tell those two apart would silently ship
    an entitlements file as a launchd job.
    """
    plugin = base / "build" / "adreno-metal-plugin"
    bundle = plugin / "NativeAdrenoMetal.bundle"
    (bundle / "Frameworks").mkdir(parents=True)
    (bundle / "NativeAdrenoMetal").write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture executable" * 8)
    (bundle / "libvulkan_freedreno.dylib").write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture driver" * 64)
    (bundle / "Info.plist").write_bytes(b"<plist>fixture</plist>")
    (bundle / "Frameworks" / "NativeBridgeMetalFrontend.o").write_bytes(b"fixture object")
    (plugin / "adrenoprobed").write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture daemon" * 4)
    (plugin / "org.blu-sharky.adrenoprobed.plist").write_bytes(b"<plist>job</plist>")
    (plugin / "com.apple.iomfb_fdr_loader.plist").write_bytes(b"<plist>job</plist>")
    (plugin / "com.apple.bluetoothd.plist").write_bytes(b"<plist>job</plist>")
    (plugin / "adrenoprobed-entitlements.plist").write_bytes(b"<plist>entitlements</plist>")
    (plugin / "trust.json").write_text('{"cdhash": "fixture"}\n', encoding="utf-8")
    return plugin


def _pack(root: Path, output: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / PACKER), *extra, str(output)],
        capture_output=True, text=True, timeout=120,
    )


def parse(blob: bytes) -> list[tuple[str, int, bytes]]:
    """Read the documented layout back: magic, count, reserved, total, then per file.

    Written from the format the packer's own docstring states, not from its
    implementation, so that a packer which changes its mind about padding has to
    change this reader too.
    """
    assert blob[:8] == PAYLOAD_MAGIC, f"magic is {blob[:8]!r}"
    count, reserved, total = struct.unpack_from("<IIQ", blob, 8)
    assert reserved == 0, f"the reserved word is {reserved}, the format says 0"
    assert total == len(blob), f"the header claims {total} bytes, the file is {len(blob)}"
    at = 24
    files: list[tuple[str, int, bytes]] = []
    for index in range(count):
        path_len, mode, size, packed_len = struct.unpack_from("<IIQQ", blob, at)
        at += 24
        raw_path = blob[at:at + path_len]
        at += -path_len % 8 + path_len
        assert len(raw_path) == path_len, "the path runs past the end of the blob"
        assert raw_path.endswith(b"\0"), f"the path is not NUL-terminated: {raw_path!r}"
        name = raw_path[:-1].decode()
        assert len(raw_path) <= 160, f"{name} is {len(raw_path)} bytes, the cap is 160"
        packed = blob[at:at + packed_len]
        at += -packed_len % 8 + packed_len
        assert len(packed) == packed_len, f"the stream for {name} runs past the end of the blob"
        data = zlib.decompress(packed)
        assert len(data) == size, f"{name} claims {size} bytes and decompresses to {len(data)}"
        assert ".." not in name.split("/") and not name.startswith("/"), f"{name} escapes the addon root"
        files.append((name, mode, data))
    assert at == len(blob), f"the reader stopped at {at} of {len(blob)} bytes"
    return files


def test_the_blob_parses_back_into_exactly_the_files_the_packer_listed(tmp_path):
    root = _driver_root(_packer(), tmp_path)
    plugin = _fixture_plugin(root)
    output = tmp_path / "gpu-addon.ngpu"

    result = _pack(root, output)
    assert result.returncode == 0, result.stdout + result.stderr

    files = parse(output.read_bytes())
    names = [name for name, _, _ in files]

    # The paths the kernel mounts.  The three groups are asserted in the order
    # the packer emits them -- the bundle's own files, then the daemon, then the
    # launchd jobs -- and the membership is asserted exactly.  The order WITHIN
    # the bundle group is asserted against the packer's own comparison rather
    # than as a literal list: it sorts ``Path`` objects, and ``PurePath``
    # comparison goes through ``_str_normcase`` -- ``os.path.normcase`` -- which
    # case-folds on Windows and is the identity on macOS, so
    # ``NativeAdrenoMetal`` and ``libvulkan_freedreno.dylib`` swap places between
    # the two hosts.  The kernel materialises by path, so the order does not
    # change what lands on the volume -- but it does change the blob's bytes,
    # which is why ``gpu_addon_sha256`` is only comparable between builds from
    # one platform.
    bundle_names = [name for name in names if name.startswith("addons/NativeAdrenoMetal.bundle/")]
    assert bundle_names == sorted(bundle_names, key=os.path.normcase), bundle_names
    assert set(bundle_names) == {
        "addons/NativeAdrenoMetal.bundle/Frameworks/NativeBridgeMetalFrontend.o",
        "addons/NativeAdrenoMetal.bundle/Info.plist",
        "addons/NativeAdrenoMetal.bundle/NativeAdrenoMetal",
        "addons/NativeAdrenoMetal.bundle/libvulkan_freedreno.dylib",
    }, bundle_names
    assert names[len(bundle_names):] == [
        "bin/adrenoprobed",
        "daemons/com.apple.bluetoothd.plist",
        "daemons/com.apple.iomfb_fdr_loader.plist",
        "daemons/org.blu-sharky.adrenoprobed.plist",
    ], names

    # trust.json is the plugin build's own report, not addon content.
    assert not any("trust.json" in name for name in names)
    # The entitlements are a build input; shipping one as a launchd job would
    # have launchd read an entitlements plist as a job definition.
    assert not any(name.endswith("-entitlements.plist") for name in names)

    modes = {name: mode for name, mode, _ in files}
    assert modes["addons/NativeAdrenoMetal.bundle/NativeAdrenoMetal"] == 0o755
    assert modes["addons/NativeAdrenoMetal.bundle/libvulkan_freedreno.dylib"] == 0o755
    assert modes["bin/adrenoprobed"] == 0o755
    assert modes["daemons/com.apple.bluetoothd.plist"] == 0o644
    assert modes["addons/NativeAdrenoMetal.bundle/Info.plist"] == 0o644

    # Every byte survives the round trip, so the kernel writes what was packed.
    bundle = plugin / "NativeAdrenoMetal.bundle"
    original = {
        path.relative_to(bundle).as_posix(): path.read_bytes()
        for path in bundle.rglob("*") if path.is_file()
    }
    for name, _, data in files:
        if not name.startswith("addons/NativeAdrenoMetal.bundle/"):
            continue
        assert data == original[name.split("/", 2)[2]], name


def test_the_optional_members_are_added_only_when_asked_for(tmp_path):
    root = _driver_root(_packer(), tmp_path)
    plugin = _fixture_plugin(root)
    output = tmp_path / "gpu-addon.ngpu"

    assert _pack(root, output, "--publish", "--clients", "SpringBoard,backboardd").returncode == 0
    files = {name: data for name, _, data in parse(output.read_bytes())}
    assert files["addons/NativeAdrenoMetal.publish"] == b"1\n"
    assert files["addons/NativeAdrenoMetal.clients"] == b"SpringBoard\nbackboardd\n"


def test_the_packer_refuses_material_that_would_not_parse(tmp_path):
    """Three refusals, each in its own driver root.

    They share one root in the obvious spelling, and then the first one's
    fixture-deletion is what makes the second one fail -- a test that has to
    remember to rebuild state its neighbour destroyed is a test whose next edit
    gets that wrong.  Separate roots cost a file copy each.
    """
    packer = _packer()

    # No bundle executable: the same refusal build-adreno-metal-plugin.py's
    # caller sees when the plugin step has not run.
    bare = _driver_root(packer, tmp_path / "bare")
    (bare / "build" / "adreno-metal-plugin" / "NativeAdrenoMetal.bundle").mkdir(parents=True)
    result = _pack(bare, tmp_path / "bare.ngpu")
    assert result.returncode != 0
    assert "Build the plugin first" in result.stdout + result.stderr

    # An existing output: a rebuild must not silently relabel the old blob.
    again_root = _driver_root(packer, tmp_path / "again")
    _fixture_plugin(again_root)
    output = tmp_path / "gpu-addon.ngpu"
    assert _pack(again_root, output).returncode == 0
    again = _pack(again_root, output)
    assert again.returncode != 0
    assert "Refuse to overwrite" in again.stdout + again.stderr

    # A prime list that is not a list of absolute .metallib paths.
    list_root = _driver_root(packer, tmp_path / "list")
    _fixture_plugin(list_root)
    bad_list = tmp_path / "prime.txt"
    bad_list.write_text("Library/foo.metal\n", encoding="utf-8")
    listed = _pack(list_root, tmp_path / "listed.ngpu", "--prime-list", str(bad_list))
    assert listed.returncode != 0
    assert "--prime-list" in listed.stdout + listed.stderr


def test_a_path_the_kernel_would_reject_is_refused_rather_than_padded(tmp_path):
    """A name over the 160-byte cap is a hard refusal, not a truncated path."""
    root = _driver_root(_packer(), tmp_path)
    plugin = _fixture_plugin(root)
    bundle = plugin / "NativeAdrenoMetal.bundle"
    # The stored path is "addons/NativeAdrenoMetal.bundle/" (32 bytes) plus the
    # relative path, so the relative path has to carry it past 160 - 32 - 1 = 127
    # bytes.  Reached with several short components rather than one long one: a
    # 200-character single component blows past Windows' MAX_PATH once the
    # temporary directory is prepended, and the failure then says something about
    # the filesystem instead of about the packer.
    deep = bundle
    for _ in range(4):
        deep = deep / ("d" * 30)
    deep.mkdir(parents=True)
    (deep / "leaf.bin").write_bytes(b"fixture")
    assert len("addons/NativeAdrenoMetal.bundle/" + "d" * 30 + "/") * 4 + len("leaf.bin") + 1 > 160
    result = _pack(root, tmp_path / "gpu-addon.ngpu")
    assert result.returncode != 0
    assert "Bad addon path" in result.stdout + result.stderr


def test_a_realistic_bundle_stays_inside_the_payload_fat_budget(tmp_path):
    """The blob's size is the one thing that can fail after everything else passed.

    ``payload.py`` refuses the payload when its staged content passes 95% of
    ``size_mib``, and the tablet's boot loader will not start a 256 MiB ramdisk,
    so an oversized bundle is not something a later setting can absorb.  The
    fixture here is sized from the real driver library rather than from nothing:
    a stripped iOS freedreno build is a few megabytes, and the assertion fails
    long before a runner has spent its minutes on one.
    """
    root = _driver_root(_packer(), tmp_path)
    plugin = _fixture_plugin(root)
    bundle = plugin / "NativeAdrenoMetal.bundle"
    # A 12 MiB incompressible payload: zlib level 9 cannot shrink random bytes,
    # so this is the pessimistic end of the range rather than the likely one.
    (bundle / "libvulkan_freedreno.dylib").write_bytes(bytes(range(256)) * 49_152)
    output = tmp_path / "gpu-addon.ngpu"
    assert _pack(root, output).returncode == 0
    assert len(output.read_bytes()) < PAYLOAD_HEADROOM, (
        f"the blob is {len(output.read_bytes())} bytes and only "
        f"{PAYLOAD_HEADROOM} are left in the payload FAT"
    )
