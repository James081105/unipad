"""``unipad.host.unixnu_boot`` against a synthetic payload built on this host.

Why this file exists
--------------------
This module is the Windows counterpart of the device repository's
``scripts/run-qemu.py``, and it exists because that script assumes macOS: it
finds QEMU and the EDK II firmware through Homebrew, extracts the ESP with
``mcopy``, attaches the OS disk with ``hdiutil`` and offers a cocoa window.  What
*transfers* is the machine description and the ESP layout -- both of which the
guest sees, so both of which have to be byte-for-byte right -- and what does not
transfer is refused rather than approximated.

A boot needs QEMU, an EDK II firmware flash, a built UniXNU and a real
``DarwinBoot.efi``, so :func:`boot` itself is not run to completion here.  What
*is* proven here is everything the run depends on and nothing else depends on:
the command line the guest will see, the ESP it will be handed, the kernel
command line the board asked for, and the two refusals that stop a run from
looking successful when it is not.

The defects these tests are written after
-----------------------------------------
* **Relative paths.**  QEMU is started with the run directory as its cwd, so a
  relative ``-drive file=`` resolves against the *run* directory rather than the
  caller's -- which is how a firmware that exists becomes "Could not open" with a
  path that looks right.  Every path in the argv is asserted absolute here, and
  forward-slashed because a backslash is an escape in a QEMU option value.
* **``loader = "driver"``.**  DarwinBoot is a *driver* UniXNU loads; the firmware
  cannot start it.  A payload that treats it as an application produces
  ``failed to load Boot0001 ... Not Found``, which names the device and not the
  cause -- so the check that turns that into a diagnosis lives in
  :func:`pe_subsystem` and is pinned here.
* **``[payload] dram_size`` disagreeing with the machine.**  The device tree and
  the machine describe the same memory; a board that says otherwise is a
  different board, not a rounding error.

What is *not* proven here
-------------------------
That the guest boots: no QEMU, no firmware, no kernel on this host.  The
``DarwinBoot.efi`` test is skipped when ``local-private/`` is absent, and it is
absent from every checkout by design (PRD 12).
"""

from __future__ import annotations

import dataclasses
import getpass
import hashlib
import json
import socket
import struct
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import unixnu_boot as ub
from unipad.host.unixnu_boot import BootInputs, BootReport, stage_esp

ROOT = Path(__file__).resolve().parents[2]
REAL_DEVICE = ROOT / "vendor" / "UniXNU-qemu"
REAL_PRIVATE = ROOT / "local-private"

#: The five files the ESP carries, in the layout ``unixnu/payload.py`` stages
#: and ``run-qemu.py`` copies out of ``payload.img``.
ESP_FILES = (
    "EFI/BOOT/BOOTAA64.EFI",
    "EFI/UniXNU/unixnu.toml",
    "EFI/UniXNU/Payloads/kernel.macho",
    "EFI/UniXNU/Payloads/kernel.macho.adt",
    "EFI/UniXNU/Payloads/DarwinBoot.efi",
)


# ---------------------------------------------------------------------------
# a synthetic Apple device tree
# ---------------------------------------------------------------------------
def _adt_node(name: str, properties=(), children=()) -> list:
    """One node of the format ``adt.parse_adt`` reads.

    A transcription of ``make-qemu-adt.py:serialize``, the same way
    ``tests/unit/test_host_adt.py`` builds its own trees: a property count and a
    child count, then that many ``[32-byte key][u32 length][padded value]``
    triples, then that many children.
    """
    return [name, [(key, value) for key, value in properties], list(children)]


def _serialize_adt(node: list) -> bytes:
    _, properties, children = node
    out = struct.pack("<II", len(properties), len(children))
    for key, data in properties:
        out += struct.pack("<32sI", key.encode(), len(data)) + data + bytes(-len(data) % 4)
    return out + b"".join(_serialize_adt(child) for child in children)


def _synthetic_tree(
    *,
    ramdisk: Optional[bytes] = None,
    ramdisk_key: str = "PackedRAMDisk",
    both: bool = False,
    addon: Optional[bytes] = None,
    addon_key: str = "GpuAddon",
) -> bytes:
    """A parseable tree whose ``/chosen/memory-map`` is the interesting part.

    ``ramdisk`` is what the placeholder property carries; ``both`` writes the
    two properties at once, which is the state ``AdtNode`` refuses.  Everything
    else is the minimum a parse needs: names on the three nodes, and a
    ``TrustCache`` so the tree is shaped like the real one.

    ``addon`` writes the GPU addon placeholder, which is a fourth property
    beside the ramdisk and the trust cache.  ``AdtNode`` resolves every one of
    them *by name* (``DarwinBoot.c:225-243``), so a fourth property is legal --
    which is why the real tree grew from three to four without anything
    complaining, and why the missing file it names was invisible until the
    firmware refused the boot.
    """
    memory_map = [("name", b"memory-map\0"), ("TrustCache", bytes(16))]
    if both:
        memory_map += [("PackedRAMDisk", bytes(16)), ("RAMDisk", bytes(16))]
    elif ramdisk is not None:
        memory_map.append((ramdisk_key, ramdisk))
    if addon is not None:
        memory_map.append((addon_key, addon))
    return _serialize_adt(
        _adt_node("", [("name", b"root\0")], [
            _adt_node("chosen", [("name", b"chosen\0")], [
                _adt_node("memory-map", memory_map),
            ]),
        ])
    )


def _synthetic_addon(*, size: int = 4096, magic: bytes = b"NGPUADD2",
                     declared: Optional[int] = None) -> bytes:
    """A blob satisfying the header ``DarwinBoot.c:593-595`` checks.

    ``make-gpu-addon.py`` writes an eight-byte magic, then a u64 at offset 16
    recording the total length, and the firmware repeats all three checks.  A
    real blob is Apple-independent build output but it is 12 MB and belongs to a
    particular build, so what the tests need is the *contract*: magic, a size of
    at least 24, and a self-describing length.
    """
    blob = bytearray(max(size, 24))
    blob[:8] = magic
    struct.pack_into("<Q", blob, 16, size if declared is None else declared)
    # Truncated back to the requested size, so a caller asking for a 16-byte
    # blob gets exactly the too-short blob the firmware would refuse -- and not
    # a 24-byte one whose length field then disagrees for a different reason.
    return bytes(blob[:size]) if size < 24 else bytes(blob)


def _synthetic_seed(*, logical: int = 1 << 30, extents=((0, 4096),)) -> bytes:
    """A valid DBRDISK1 packed ramdisk, built here rather than shipped.

    ``prepare-owner-data-fixture.py`` needs macOS to make a real one, and a real
    one is Apple-derived material that does not belong in the repository (PRD
    12).  What the tests need is a blob that satisfies the format contract, and
    that contract is 32 header bytes plus one 16-byte extent per dirty region.
    """
    blob = struct.pack("<8sIIQQ", b"DBRDISK1", 1, 4096, logical, len(extents))
    payload = b""
    for _, length in extents:
        payload += bytes(length)
    return blob + b"".join(
        struct.pack("<QQ", offset, length) for offset, length in extents
    ) + payload


# ---------------------------------------------------------------------------
# synthetic inputs
# ---------------------------------------------------------------------------
def _pe32plus(subsystem: int, machine: int = ub.PE_MACHINE_ARM64) -> bytes:
    """A PE32+ AArch64 image carrying ``subsystem``, and nothing else.

    Only the fields the loader reads are laid out: the ``MZ`` stub,
    ``e_lfanew``, the ``PE\\0\\0`` signature, the machine type and the optional
    header's subsystem.  Everything else is zeroes, which is exactly as much of
    a real image as :func:`pe_subsystem` looks at -- and building it by hand
    rather than shipping a fixture is what keeps a real ``DarwinBoot.efi`` out of
    the repository (PRD 12).
    """
    size = 0x200
    blob = bytearray(size)
    blob[:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3C, 0x40)  # e_lfanew -> the PE signature
    blob[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", blob, 0x44, machine, 1, 0, 0, 0, 0xF0, 0x22)
    # The optional header starts right after the 20-byte COFF header.
    optional = 0x58
    struct.pack_into("<H", blob, optional, 0x20B)  # PE32+ (64-bit)
    struct.pack_into("<H", blob, optional + 68, subsystem)
    return bytes(blob)


@pytest.fixture
def inputs(tmp_path: Path) -> BootInputs:
    """Five present files, each with a distinct size.

    Distinct sizes on purpose: ``stage_esp`` records the size of everything it
    writes, and a test where every file is the same length would pass even if
    two of them were swapped.  ``unixnu`` is a real PE32+ *application* because
    that is what ``BOOTAA64.EFI`` has to be, and a stand-in that is not one would
    make every ``boot`` test fail for the wrong reason.

    The device tree is a *parseable* Apple tree, not a bag of bytes, because
    :func:`stage_esp` now reads it back to check the packed-ramdisk pair -- and
    a fixture that cannot be parsed would make every staging test fail on the
    tree rather than on what the test is about.
    """
    firmware = tmp_path / "edk2-aarch64-code.fd"
    firmware.write_bytes(b"\0" * 4096)
    kernel = tmp_path / "kernel.macho"
    kernel.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic kernelcache" * 8)
    devicetree = tmp_path / "kernel.macho.adt"
    devicetree.write_bytes(_synthetic_tree())
    darwinboot = tmp_path / "DarwinBoot.efi"
    darwinboot.write_bytes(_pe32plus(11) + b"darwinboot-driver" * 3)
    unixnu = tmp_path / "UniXNU.efi"
    unixnu.write_bytes(_pe32plus(ub.PE_SUBSYSTEM_APPLICATION) + b"unixnu-application" * 4)
    return BootInputs(
        firmware=firmware,
        kernel=kernel,
        devicetree=devicetree,
        darwinboot=darwinboot,
        unixnu=unixnu,
    )


@pytest.fixture
def qemu(tmp_path: Path) -> Path:
    """A stand-in executable: ``boot`` checks it exists, and nothing launches it."""
    path = tmp_path / "qemu-system-aarch64.exe"
    path.write_bytes(b"stand-in")
    return path


@pytest.fixture(scope="module")
def real_spec() -> dict[str, Any]:
    """The checked-in board's ``[payload]`` block."""
    if not (REAL_DEVICE / "board.toml").is_file():
        pytest.skip("vendor/UniXNU-qemu is not present in this checkout")
    return ub.read_board_payload(REAL_DEVICE)


@pytest.fixture(scope="module")
def real_line(real_spec: dict[str, Any]) -> str:
    """The board's kernel command line, spelled the way the module spells it."""
    return ub.kernel_args_line(real_spec)


class _AnswersObjectHelp:
    """A ``subprocess.run`` that answers ``-object help`` and nothing else.

    The probe's whole question is "what does this binary register", so the test
    answers it rather than launching something.  A different command is an
    assertion failure, not a silent empty answer: a probe that quietly accepts
    any command is a probe that would pass against a QEMU which never ran.
    """

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode

    def run(self, argv: list[str], **_kwargs: Any) -> Any:
        assert "-object" in argv and "help" in argv, argv
        return subprocess.CompletedProcess(argv, self.returncode,
                                           stdout=self.stdout, stderr="")


def _refused(call, *args: Any, **kwargs: Any) -> UniPadError:
    """Run ``call`` and return the refusal, failing loudly if it does not refuse."""
    with pytest.raises(UniPadError) as caught:
        call(*args, **kwargs)
    return caught.value


def _flag(argv: list[str], name: str) -> str:
    """The value that follows ``name`` in ``argv``."""
    return argv[argv.index(name) + 1]


def _values(argv: list[str], name: str) -> list[str]:
    """Every value that follows ``name`` in ``argv``."""
    return [argv[index + 1] for index, item in enumerate(argv) if item == name]


def _scrubbed(argv: list[str], scratch: Path) -> list[str]:
    """``argv`` with the scratch directory removed from every token.

    ``build_argv`` embeds absolute paths, and pytest names each test's scratch
    directory after the test, so a run whose *name* mentions the SystemOS cryptex
    would otherwise match its own tmpdir and fail a "nothing mentions systemos"
    assertion for the wrong reason.  Both spellings go: ``build_argv`` renders
    drive files forward-slashed, while the QEMU executable path is handed over as
    ``str(path)`` and keeps its Windows separators.
    """
    forward, native = scratch.as_posix(), str(scratch)
    scrubbed = []
    for token in argv:
        scrubbed.append(token.replace(forward, "").replace(native, ""))
    return scrubbed


def _absent(name: str) -> Path:
    """A path that names a file which does not exist, wherever it is rooted.

    A bare name is used rather than a path under ``tmp_path`` so the assertion
    about what ``missing()`` reports is about the *name* and not about where the
    fixture happens to live.
    """
    return Path(name)


def _paths_in(value: str) -> list[str]:
    """Every filesystem path embedded in one QEMU option value.

    ``fat:`` is a QEMU *prefix* on the path rather than part of it, so it is
    stripped: what has to be absolute is the directory behind it.
    """
    found: list[str] = []
    for marker in ("file=", "fat:"):
        at = value.find(marker)
        while at != -1:
            found.append(value[at + len(marker) :].split(",")[0])
            at = value.find(marker, at + 1)
    # ``file=fat:<dir>`` yields ``fat:<dir>`` from the first marker as well as
    # ``<dir>`` from the second; the prefix is QEMU's, not the path's.
    return [item[len("fat:") :] if item.startswith("fat:") else item for item in found if item]


# ---------------------------------------------------------------------------
# the board's declaration
# ---------------------------------------------------------------------------
def test_the_real_board_declares_the_payload_the_module_boots(
    real_spec: dict[str, Any],
) -> None:
    """Every value the payload build reads, pinned against the checked-in board.

    This is the one test in the file that touches the repository's real data,
    and it is here because every synthetic test below is a mutation of it.
    """
    assert real_spec["title"] == "QEMU virt: source 4K XNU", real_spec
    assert real_spec["loader"] == "driver", real_spec
    assert real_spec["volume"] == "QEMUESP", real_spec
    assert real_spec["size_mib"] == 192, real_spec
    assert real_spec["dram_size"] == "0x180000000", real_spec
    assert real_spec["panic_screen"] is True, real_spec
    # 26, not the 25 this asserted after the surface wait was raised.  The
    # count is the point of the line -- a board that silently loses an argument
    # boots a different machine -- so it moves with the board rather than being
    # loosened.  The arguments themselves are asserted by name below, because a
    # count alone cannot tell you WHICH argument changed.
    assert len(real_spec["kernel_args"]) == 26, real_spec
    assert "sheng-gpu-addon=1" in real_spec["kernel_args"], real_spec
    # The kernel-side presenter.  This one is not the addon: NativeGOPPresenter::
    # start() is the only writer of native_gop_ready, and the object is only
    # constructed under phase==3 or under phase==4 with this argument, so with
    # phase=4 and without it the presenter does not exist and gop_ready is 0 no
    # matter what the userspace half does.
    assert "sheng-fb-gpu=1" in real_spec["kernel_args"], real_spec
    # The display probe's own surface wait.  The 120 s default let six of ten
    # boots of one matched build reach the userland and then show no display at
    # all -- surface_status 0 (the IOSurface kext loaded) with surface_found 0,
    # i.e. waitForMatchingService() timed out.  Asserted by name AND by value:
    # the name says the wait is configured rather than defaulted, and the value
    # is the measured one that took the same build to stage 10 on both of the
    # next two boots.
    wait = [a for a in real_spec["kernel_args"]
            if a.startswith("offline-legacy-surface-wait-ms=")]
    assert wait == ["offline-legacy-surface-wait-ms=600000"], wait


def test_the_memory_the_module_gives_the_machine_is_the_boards(
    real_spec: dict[str, Any],
) -> None:
    """``MEMORY_MIB`` and ``[payload] dram_size`` are the same 6 GiB.

    Two spellings of one number is exactly the kind of thing that drifts, and
    the device tree gets its size from the board while the machine gets it from
    this constant -- so :func:`boot` compares them and refuses a disagreement.
    """
    assert int(real_spec["dram_size"], 0) >> 20 == ub.MEMORY_MIB, real_spec
    assert ub.MEMORY_MIB == 6144


def test_a_board_with_no_payload_block_is_refused(tmp_path: Path) -> None:
    """This module exists to boot that block; inventing one would boot fiction."""
    (tmp_path / "board.toml").write_text('[board]\nname = "synthetic"\n', encoding="utf-8")
    error = _refused(ub.read_board_payload, tmp_path)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "[payload]" in error.message, error


def test_a_missing_board_toml_is_refused(tmp_path: Path) -> None:
    """Named by its final component, which is all an error may carry (PRD 9.2)."""
    error = _refused(ub.read_board_payload, tmp_path / "nowhere")
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "board.toml not found" in error.message, error
    assert error.details.get("path") == "board.toml", error.details


def test_an_unparseable_board_toml_is_refused(tmp_path: Path) -> None:
    """A half-written board is a refusal, not a board with no payload."""
    (tmp_path / "board.toml").write_text("[payload\n", encoding="utf-8")
    error = _refused(ub.read_board_payload, tmp_path)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "not valid TOML" in error.message, error


# ---------------------------------------------------------------------------
# the kernel command line
# ---------------------------------------------------------------------------
def test_the_line_is_the_boards_own_arguments_verbatim(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """No reordering, no re-spelling, no dropped argument.

    The consumer is ``unixnu/payload.py:check_args``, which is the same consumer
    upstream's payload build feeds -- so anything this module did to the list
    would be a command line the board never asked for.
    """
    assert real_line == " ".join(real_spec["kernel_args"]), real_line
    assert real_line.split() == list(real_spec["kernel_args"])
    assert len(real_line) <= ub.ARGUMENT_LIMIT, len(real_line)


@pytest.mark.parametrize(
    "argument",
    [
        "not a valid argument",  # a space is neither NAME nor NAME=VALUE
        "a=b=c",  # one '=' is the whole rule
        "name=with space",
        "name=with/slash",
        "name=with:colon",
        "",
    ],
)
def test_an_argument_that_is_neither_name_nor_name_value_is_refused(
    real_spec: dict[str, Any], argument: str
) -> None:
    """The same rule ``unixnu/payload.py:29-37`` applies, copied not relaxed."""
    error = _refused(ub.kernel_args_line, real_spec, [argument])
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert error.details.get("argument") == argument, error.details


def test_a_command_line_over_the_buffer_is_refused(real_spec: dict[str, Any]) -> None:
    """DarwinBoot copies the line into a 1024-byte buffer; 1000 is the limit.

    Refused rather than truncated: a silently shortened command line boots a
    guest missing an argument, which reads as a kernel bug rather than as this
    module having dropped something.
    """
    padding = "pad=" + "a" * 700
    line = ub.kernel_args_line(real_spec)  # the board's own line is legal
    assert len(line) + len(padding) + 1 > ub.ARGUMENT_LIMIT
    error = _refused(ub.kernel_args_line, real_spec, [padding])
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert error.details.get("bytes") > ub.ARGUMENT_LIMIT, error.details
    assert "limit is 1000" in error.message, error


def test_a_line_exactly_at_the_limit_is_accepted(real_spec: dict[str, Any]) -> None:
    """The boundary is inclusive, so the limit is what the message says it is."""
    spec = dict(real_spec, kernel_args=[])
    argument = "a=" + "b" * (ub.ARGUMENT_LIMIT - 2)
    assert len(argument) == ub.ARGUMENT_LIMIT
    assert ub.kernel_args_line(spec, [argument]) == argument
    # ...and one byte more is refused, so the boundary is not off by one.
    _refused(ub.kernel_args_line, spec, [argument + "b"])


def test_an_extra_argument_replaces_the_boards_one_of_the_same_name(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """``debug=0x14e`` is replaced, not duplicated.

    ``check_args`` builds a dict, so a second ``debug=`` would simply overwrite
    the board's -- and a caller that *appended* would produce a line where the
    last one wins, which is not what the argument list says.
    """
    assert "debug=0x14e" in real_line
    merged = ub.kernel_args_line(real_spec, ["debug=0x8"])
    assert merged.count("debug=") == 1, merged
    assert "debug=0x8" in merged
    assert "debug=0x14e" not in merged
    # ...and the argument *list* is the board's with that one name removed, plus
    # the replacement at the end -- nothing else is added or dropped, and the
    # board's own order is preserved.
    expected = [
        item for item in real_spec["kernel_args"] if item.split("=", 1)[0] != "debug"
    ] + ["debug=0x8"]
    assert merged.split() == expected, merged
    assert merged.split()[-1] == "debug=0x8", merged


def test_an_extra_flag_argument_replaces_the_boards_one_too(
    real_spec: dict[str, Any],
) -> None:
    """A bare ``NAME`` has a name too, so the same replacement applies."""
    assert "-v" in real_spec["kernel_args"]
    merged = ub.kernel_args_line(real_spec, ["-vv"])
    assert merged.count("-v") == 0 or "-vv" in merged
    assert merged.count("-vv") == 1, merged


def test_an_extra_argument_that_names_nothing_is_appended(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """Replacement is by name, not by position: a new name is simply added."""
    merged = ub.kernel_args_line(real_spec, ["unipad-test=1"])
    assert merged == real_line + " unipad-test=1", merged


# ---------------------------------------------------------------------------
# the ESP configuration
# ---------------------------------------------------------------------------
def test_the_toml_loads_darwinboot_as_a_driver(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """``[[driver]] path = "Payloads/DarwinBoot.efi"`` is the load-bearing line.

    It is what makes UniXNU load DarwinBoot rather than expecting the firmware to
    have run it, and ``run-qemu.py:96-98`` refuses a payload without it -- so its
    absence is a build error here too, not a default.
    """
    text = ub.unixnu_toml(real_spec, real_line)
    assert '[[driver]]\npath = "Payloads/DarwinBoot.efi"' in text, text
    parsed = tomllib.loads(text)
    assert parsed["version"] == 1, parsed
    assert parsed["boot"]["default"] == "xnu", parsed
    assert parsed["kits"]["autoload"] is False, parsed
    assert parsed["driver"] == [{"path": "Payloads/DarwinBoot.efi"}], parsed
    assert len(parsed["entry"]) == 1, parsed
    entry = parsed["entry"][0]
    assert entry["id"] == "xnu", entry
    assert entry["type"] == "xnu", entry
    assert entry["title"] == real_spec["title"], entry
    assert entry["path"] == "Payloads/kernel.macho", entry
    assert entry["args"] == real_line, entry


def test_a_board_whose_loader_is_not_driver_is_refused(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """``loader = "firmware"`` would mean the firmware starts DarwinBoot.

    It cannot: ``DarwinBootPkg`` builds it as ``UEFI_DRIVER`` and BDS reports
    ``The image is not an application.``  Generating that payload anyway would
    produce a run whose failure names the device and not the cause.
    """
    for loader in ("firmware", "application", "", None):
        spec = dict(real_spec)
        if loader is None:
            spec.pop("loader")
        else:
            spec["loader"] = loader
        error = _refused(ub.unixnu_toml, spec, real_line)
        assert error.code is ErrorCode.CONFIG_INVALID, (loader, error)
        assert 'loader must be "driver"' in error.message, error
        # An absent key is the same answer as a wrong one, because the module's
        # own default for a missing loader is "firmware".
        expected = loader if loader is not None else "firmware"
        assert error.details.get("loader") == repr(expected), error.details


def test_the_timeout_is_the_boards_own(real_spec: dict[str, Any], real_line: str) -> None:
    """``[payload] timeout`` is honoured, and 3 is the fallback only when absent."""
    text = ub.unixnu_toml(dict(real_spec, timeout=9), real_line)
    assert tomllib.loads(text)["boot"]["timeout"] == 9, text
    bare = {key: value for key, value in real_spec.items() if key != "timeout"}
    assert tomllib.loads(ub.unixnu_toml(bare, real_line))["boot"]["timeout"] == 3


# ---------------------------------------------------------------------------
# staging the ESP
# ---------------------------------------------------------------------------
def test_the_esp_carries_exactly_the_five_files(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """Exactly five, at exactly these paths, with exactly these sizes.

    The layout is the one ``unixnu/payload.py`` stages, so anything extra here is
    a file the guest was never meant to see and anything missing is a file it
    needs.  The sizes come from the sources rather than from constants, so a
    wrong source is caught rather than copied faithfully.
    """
    esp = tmp_path / "esp"
    written = ub.stage_esp(inputs, esp, real_spec)
    found = sorted(
        path.relative_to(esp).as_posix() for path in esp.rglob("*") if path.is_file()
    )
    assert found == sorted(ESP_FILES), found
    assert sorted(key for key in written if not key.startswith("_")) == sorted(ESP_FILES), written
    assert written["EFI/BOOT/BOOTAA64.EFI"] == inputs.unixnu.stat().st_size, written
    assert written["EFI/UniXNU/Payloads/kernel.macho"] == inputs.kernel.stat().st_size, written
    assert (
        written["EFI/UniXNU/Payloads/kernel.macho.adt"]
        == inputs.devicetree.stat().st_size
    ), written
    assert (
        written["EFI/UniXNU/Payloads/DarwinBoot.efi"]
        == inputs.darwinboot.stat().st_size
    ), written
    # The generated configuration, sized by its own encoded length.
    expected = ub.unixnu_toml(real_spec, ub.kernel_args_line(real_spec))
    assert written["EFI/UniXNU/unixnu.toml"] == len(expected.encode("utf-8")), written
    assert (esp / "EFI/UniXNU/unixnu.toml").read_text(encoding="utf-8") == expected
    # The copies are the caller's own bytes, not a re-encoding of them.
    assert (esp / "EFI/BOOT/BOOTAA64.EFI").read_bytes() == inputs.unixnu.read_bytes()
    assert (esp / "EFI/UniXNU/Payloads/DarwinBoot.efi").read_bytes() == (
        inputs.darwinboot.read_bytes()
    )
    # ``QemuRamfb.efi`` is deliberately *not* staged: it is the payload build's
    # driver, and a second copy here would be a second definition of SCREEN.
    assert not list(esp.rglob("QemuRamfb.efi")), "a second copy of the framebuffer driver"


def test_staging_recreates_the_esp_so_a_stale_file_cannot_survive(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """A previous run's leftovers would be presented to the guest as the ESP."""
    esp = tmp_path / "esp"
    (esp / "EFI/UniXNU/Payloads").mkdir(parents=True)
    (esp / "EFI/UniXNU/Payloads/stale.efi").write_bytes(b"from a previous run")
    ub.stage_esp(inputs, esp, real_spec)
    assert not (esp / "EFI/UniXNU/Payloads/stale.efi").exists(), "a stale file survived"


def test_a_payload_toml_that_does_not_load_darwinboot_is_refused(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """An adopted ``unixnu.toml`` is still checked for the driver entry.

    ``run-qemu.py:96-98`` refuses one without it, so adopting a caller's file
    cannot be a way round the check -- otherwise the failure would arrive as a
    guest that starts UniXNU and then stops, with nothing naming the reason.
    """
    adopted = tmp_path / "unixnu.toml"
    adopted.write_text(
        'version = 1\n[boot]\ntimeout = 3\ndefault = "xnu"\n'
        '[[entry]]\nid = "xnu"\ntitle = "no driver"\ntype = "xnu"\n'
        'path = "Payloads/kernel.macho"\nargs = "-v"\n',
        encoding="utf-8",
    )
    esp = tmp_path / "esp"
    error = _refused(
        ub.stage_esp,
        dataclasses.replace(inputs, payload_toml=adopted),
        esp,
        {"loader": "driver"},
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "does not load DarwinBoot as a driver" in error.message, error


def test_a_board_whose_loader_is_not_driver_is_refused_while_staging(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The same refusal, reached through the staging path with no toml to adopt.

    The message is :func:`unixnu_toml`'s rather than the "does not load
    DarwinBoot" one, because here there is no adopted file to check -- the
    configuration is being generated, and generating it is what is refused.
    """
    esp = tmp_path / "esp"
    error = _refused(ub.stage_esp, inputs, esp, {"loader": "firmware"})
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert 'loader must be "driver"' in error.message, error
    assert not (esp / "EFI/UniXNU/unixnu.toml").exists(), "a config was written anyway"


def test_an_adopted_payload_toml_is_used_verbatim(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The runner's own ``unixnu.toml`` wins over a locally generated one.

    The runner's build writes one with the board's exact arguments, and it is the
    file the real payload was verified with -- regenerating it here would be a
    second spelling of the same configuration.
    """
    adopted = tmp_path / "unixnu.toml"
    # A real adopted file: the runner's own, carrying the driver entry, which is
    # what makes it a legal payload at all.
    adopted.write_text(
        'version = 1\n[boot]\ntimeout = 3\ndefault = "xnu"\n'
        '[[driver]]\npath = "Payloads/DarwinBoot.efi"\n'
        '[[entry]]\nid = "xnu"\ntitle = "the runner\'s own"\ntype = "xnu"\n'
        'path = "Payloads/kernel.macho"\nargs = "-v"\n',
        encoding="utf-8",
    )
    esp = tmp_path / "esp"
    written = ub.stage_esp(
        dataclasses.replace(inputs, payload_toml=adopted), esp, real_spec
    )
    text = adopted.read_text(encoding="utf-8")
    assert (esp / "EFI/UniXNU/unixnu.toml").read_text(encoding="utf-8") == text
    # The recorded size is the length of the *text*, which on Windows is shorter
    # than the file: ``write_text`` translates each newline to CRLF.  Both are
    # asserted rather than assumed equal, because a record that disagreed with
    # the file would be evidence about nothing.
    assert written["EFI/UniXNU/unixnu.toml"] == len(text.encode("utf-8")), written
    assert (esp / "EFI/UniXNU/unixnu.toml").stat().st_size >= len(text.encode("utf-8"))


def test_a_missing_source_is_a_plain_error_not_a_partial_esp(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """``shutil.copyfile`` on an absent source is a ``FileNotFoundError``.

    Not a refusal with a stable code, and that is worth pinning: the caller has
    already established the inputs exist (``BootInputs.missing``), so reaching
    this at all means something deleted a file mid-run.
    """
    inputs.kernel.unlink()
    with pytest.raises(FileNotFoundError):
        ub.stage_esp(inputs, tmp_path / "esp", real_spec)


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def test_the_hid_devices_arrive_with_the_os_root(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """The guest's HID devices are VirtIO ones, and they come with the OS root.

    This is the finding that explains why no input has ever moved the
    framebuffer, turned into a guard.

    ``run-qemu.py:208-210`` emits ``virtio-tablet-device`` +
    ``virtio-keyboard-device`` and its comment names the consumer:
    ``package/VirtioInput.cpp``.  ``build_argv`` emitted ``qemu-xhci`` +
    ``usb-kbd`` instead -- a USB keyboard the guest cannot see, because
    ``kernel.macho.adt`` contains **zero** occurrences of ``usb``, ``xhci``,
    ``dwc3`` or ``hid`` and 32 of ``virtio-mmio``.  There is no USB controller
    node for an xHCI driver to bind to; the 32 VirtIO MMIO nodes are the
    transport ``VirtioInput.cpp`` uses.

    The consequence was measured, not reasoned: a real run with ``usb-kbd``
    answered QMP ``send-key`` with ``{}`` and changed neither the guest log nor
    the framebuffer hash over 65 seconds.  QEMU accepted the event; the guest
    had no device to receive it.

    Gated on ``root`` for upstream's reason: without an OS root there is no
    userland to consume a touch, and a device the guest enumerates but nothing
    drives is a silent difference between two runs that look identical.
    """
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"\0" * (1 << 20))
    work = tmp_path / "run"
    argv = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                         root_image=root)
    devices = _values(argv, "-device")
    assert "virtio-tablet-device" in devices, devices
    assert "virtio-keyboard-device" in devices, devices
    assert "usb-kbd" not in devices, devices
    assert "qemu-xhci" not in devices, devices


def test_without_an_os_root_there_are_no_hid_devices(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """The other side of the same gate.

    A guard that permits the case it was built to permit and refuses the case it
    was built to refuse is only half a guard.  Asserted because the obvious
    implementation -- emitting the pair unconditionally -- passes every test
    above.
    """
    work = tmp_path / "run"
    argv = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none")
    devices = _values(argv, "-device")
    assert "virtio-tablet-device" not in devices, devices
    assert "virtio-keyboard-device" not in devices, devices
    assert devices == ["virtio-blk-pci,drive=esp,bootindex=0", "ramfb"], devices


def test_the_argv_is_the_boards_machine(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The device list is upstream's, in upstream's order, with three changes.

    Each change has a reason and each reason is asserted here: ``-accel tcg``
    because a Windows QEMU that picks its own accelerator can pick WHPX, which
    cannot execute AArch64; ``-qmp`` over TCP because Windows has no
    ``AF_UNIX``; ``-display none`` because a guest window is an operator
    decision.
    """
    esp = tmp_path / "esp"
    work = tmp_path / "run"
    argv = ub.build_argv(tmp_path / "qemu-system-aarch64.exe", inputs, esp, work)

    assert _flag(argv, "-machine") == "virt,virtualization=off,secure=off,gic-version=3", argv
    assert _flag(argv, "-cpu") == "cortex-a710", argv
    assert _flag(argv, "-accel") == "tcg", argv
    assert _flag(argv, "-smp") == "1", argv
    assert _flag(argv, "-m") == str(ub.MEMORY_MIB), argv
    assert "-no-reboot" in argv, argv
    assert _flag(argv, "-nic") == "none", argv
    assert _flag(argv, "-display") == "none", argv
    assert _flag(argv, "-monitor") == "none", argv
    assert _flag(argv, "-name") == "unipad-unixnu", argv
    assert _flag(argv, "-qmp") == "tcp:127.0.0.1:14444,server=on,wait=off", argv
    # No root image, so no VirtIO input devices either -- see
    # ``test_the_hid_devices_arrive_with_the_os_root`` for why they are gated
    # there.  ``usb-kbd`` is gone: the device tree has no USB/xHCI node at all,
    # so it was a keyboard the guest had no driver for.
    assert _values(argv, "-device") == [
        "virtio-blk-pci,drive=esp,bootindex=0",
        "ramfb",
    ], argv

    drives = _values(argv, "-drive")
    assert len(drives) == 3, drives
    firmware = inputs.firmware.resolve().as_posix()
    assert drives[0] == f"if=pflash,format=raw,readonly=on,file={firmware}", drives
    assert drives[1] == (
        f"if=pflash,format=raw,readonly=off,file={(work / 'vars.fd').resolve().as_posix()}"
    ), drives
    assert drives[2] == (
        f"if=none,id=esp,format=raw,readonly=on,file=fat:{esp.resolve().as_posix()}"
    ), drives
    # The ESP is read-only and FAT: the only disk form UniXNU boots.
    assert "readonly=on" in drives[2] and drives[2].startswith("if=none,id=esp"), drives
    assert _flag(argv, "-serial") == f"file:{(work / 'serial.log').resolve().as_posix()}", argv


def test_the_serial_port_is_a_tcp_chardev_when_a_port_is_given(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """The one change that makes guest input possible at all.

    ``-serial file:`` is one-way.  QEMU writes what the guest emits and discards
    anything a host might send, and that is why no keystroke has ever reached
    this guest: the guest's own kernel log says ``Serial keyboard started``
    (t=43.75s), and XNU's ``serial_keyboard_init``
    (``osfmk/console/serial_general.c:51-68``) only prints that when
    ``serialmode & SERIALMODE_INPUT`` -- which ``serial=3`` sets.  The thread it
    starts polls ``_serial_getc(false)`` every 16 ms and feeds each byte to
    ``cons_cinput`` (``serial_general.c:79-97``).  The guest is listening; the
    port it listens on cannot be written to.

    With a port the UART becomes a chardev, and the log is still written by QEMU
    itself (``logfile=``/``logappend=``) so ``serial.log`` is unchanged.
    """
    work = tmp_path / "run"
    work.mkdir()
    argv = ub.build_argv(
        qemu, inputs, work / "esp", work, serial_port=14544, display="none"
    )
    chardev = _flag(argv, "-chardev")
    assert chardev.startswith("socket,id=unixnu-serial,host=127.0.0.1,port=14544,"), chardev
    # Both halves matter: the socket is what carries input, and the logfile is
    # what keeps the run's serial evidence.  Dropping either produces a boot
    # that either cannot be typed at or cannot be read.
    assert f"logfile={(work / 'serial.log').resolve().as_posix()}" in chardev, chardev
    assert "logappend=on" in chardev, chardev
    assert "server=on,wait=off" in chardev, chardev
    assert _flag(argv, "-serial") == "chardev:unixnu-serial", argv
    assert not any(item.startswith("file:") for item in argv if "serial" in item), argv


def test_a_serial_port_outside_the_tcp_range_is_refused(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """A port QEMU will refuse to bind is caught here, not by QEMU's exit.

    The same rule ``qmp_port`` already follows, and the same reason: an argv
    that cannot start costs a whole launch cycle to discover, and the report
    says "QEMU exited" rather than naming the port.
    """
    work = tmp_path / "run"
    work.mkdir()
    for bad in (0, 80, 70000):
        caught = _refused(
            ub.build_argv, qemu, inputs, work / "esp", work, serial_port=bad
        )
        assert caught.code == ErrorCode.CONFIG_INVALID, caught
        assert bad == caught.details.get("port"), caught.details


def test_no_serial_port_keeps_the_file_spelling_it_always_had(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """The default is byte-for-byte what it was, chardev and all.

    ``serial_port=None`` has to be a no-op: every existing caller and every
    recorded boot used the file spelling, and a change here would silently move
    the serial evidence somewhere else.
    """
    work = tmp_path / "run"
    work.mkdir()
    argv = ub.build_argv(qemu, inputs, work / "esp", work, display="none")
    assert _flag(argv, "-serial") == f"file:{(work / 'serial.log').resolve().as_posix()}", argv
    assert not any("unixnu-serial" in item for item in argv), argv


def test_boot_passes_the_serial_port_to_the_command_it_builds(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``boot(serial_port=...)`` reaches ``build_argv``, not just its signature.

    A keyword that exists on :func:`boot` and is never forwarded is the exact
    shape of the ``--trust-cache`` gap recorded at ``--trust-cache`` in the CLI:
    every layer handles it, the CLI cannot express it, and the run reports
    success while ignoring what the caller asked for.  Asserted on the written
    ``command.txt`` rather than on a captured call so the test fails if the
    forwarding is added somewhere ``build_argv`` is not the consumer.
    """
    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    # ``_watch`` must hand the report back: ``boot`` writes the serial
    # byte count onto whatever it returns, so a ``None`` return turns the
    # last line of every boot into an ``AttributeError``.
    monkeypatch.setattr(ub, "_watch", lambda *a, **k: a[1])

    ub.boot(
        inputs, tmp_path / "run", qemu=qemu, device_dir=_synthetic_device(tmp_path),
        seconds=0, serial_port=14533,
    )

    assert launched, "boot never reached Popen"
    argv = launched[0]
    assert "port=14533" in " ".join(argv), argv
    assert "chardev:unixnu-serial" in argv, argv
    # ``ok`` is deliberately not asserted: ``_watch`` is stubbed, so
    # ``reached_qmp`` is false by construction and the report says so.  What
    # this test is about is the command, and the command is the evidence above.


def test_boot_without_a_serial_port_writes_the_file_spelling(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default is still the one-way file, end to end.

    ``build_argv``'s own default is asserted above; this asserts that nothing
    between :func:`boot` and ``build_argv`` invents one, which is what a
    ``serial_port or DEFAULT`` fallback would do.
    """
    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    monkeypatch.setattr(ub, "_watch", lambda *a, **k: a[1])

    ub.boot(
        inputs, tmp_path / "run", qemu=qemu, device_dir=_synthetic_device(tmp_path),
        seconds=0,
    )

    assert launched, "boot never reached Popen"
    argv = launched[0]
    assert "unixnu-serial" not in argv, argv
    assert _flag(argv, "-serial") == (
        f"file:{(tmp_path / 'run' / 'serial.log').resolve().as_posix()}"
    ), argv


def test_the_cli_forwards_the_serial_port_it_was_given(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--serial-port 14533`` arrives as ``serial_port=14533``.

    The same "prove the payload, not the destination" rule as the attempt-count
    test: ``def fake_boot(_inputs, work, **_)`` would swallow the keyword and
    leave a hard-coded value in ``main`` green.
    """
    received: dict[str, object] = {}

    def fake_boot(_inputs: BootInputs, work: Path, **kwargs: object) -> BootReport:
        received.update(kwargs)
        return BootReport(ok=True, workdir=str(work))

    monkeypatch.setattr(ub, "boot", fake_boot)

    ub.main([
        "--device", str(_synthetic_device(tmp_path)), "--qemu", str(qemu),
        "--firmware", str(inputs.firmware), "--kernel", str(inputs.kernel),
        "--devicetree", str(inputs.devicetree), "--darwinboot", str(inputs.darwinboot),
        "--unixnu", str(inputs.unixnu), "--workdir", str(tmp_path / "run"),
        "--serial-port", "14533",
    ])

    assert received.get("serial_port") == 14533, received


def test_the_cli_leaves_the_serial_port_unset_when_no_flag_is_given(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No flag means ``None`` -- the one-way file -- and not a default port.

    A default would move the serial evidence to a chardev nobody asked for, and
    the run would look identical from the outside.
    """
    received: dict[str, object] = {}

    def fake_boot(_inputs: BootInputs, work: Path, **kwargs: object) -> BootReport:
        received.update(kwargs)
        return BootReport(ok=True, workdir=str(work))

    monkeypatch.setattr(ub, "boot", fake_boot)

    ub.main([
        "--device", str(_synthetic_device(tmp_path)), "--qemu", str(qemu),
        "--firmware", str(inputs.firmware), "--kernel", str(inputs.kernel),
        "--devicetree", str(inputs.devicetree), "--darwinboot", str(inputs.darwinboot),
        "--unixnu", str(inputs.unixnu), "--workdir", str(tmp_path / "run"),
    ])

    assert "serial_port" in received, received
    assert received["serial_port"] is None, received


def test_the_retry_chain_forwards_the_serial_port_to_every_attempt(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each attempt is a fresh ``boot`` and gets the same channel.

    Without the forwarding, attempt 1 is writable and attempt 2 is not -- a
    difference that only shows up in the retry case, which is the case that
    fires on the 12.5% Data-mount race.
    """
    import json

    seen: list[object] = []

    def fake_boot(_inputs: BootInputs, work: Path, **kwargs: object) -> BootReport:
        seen.append(kwargs.get("serial_port"))
        work.mkdir(parents=True, exist_ok=True)
        report = BootReport(ok=True, workdir=str(work))
        (work / "boot-report.json").write_text(
            json.dumps(report.to_dict(), indent=2) + chr(10), encoding="utf-8"
        )
        return report

    monkeypatch.setattr(ub, "boot", fake_boot)

    ub.boot_until_mounted(
        inputs, tmp_path / "chain", qemu=qemu,
        device_dir=_synthetic_device(tmp_path), attempts=2, serial_port=14533,
    )

    assert seen == [14533, 14533], seen


def test_the_display_and_the_port_are_the_callers_to_choose(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """A window is an operator decision, and the port is chosen per run."""
    work = tmp_path / "run"
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        work,
        qmp_port=15555,
        display="gtk",
    )
    assert _flag(argv, "-qmp") == "tcp:127.0.0.1:15555,server=on,wait=off", argv
    assert _flag(argv, "-display") == "gtk", argv
    assert ub.DEFAULT_QMP_PORT == 14444


def test_every_path_in_the_argv_is_absolute_and_forward_slashed(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The bug that made an existing firmware read as "Could not open".

    QEMU is started with the run directory as its cwd, so a *relative* path
    resolves against the run directory rather than the caller's.  A backslash is
    an escape in a QEMU option value, so it is never used either.  Both rules are
    asserted over every path the argv carries, not over one of them.
    """
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run"
    )
    embedded = [path for value in argv for path in _paths_in(value)]
    assert embedded, "no paths were found; the extraction stopped working"
    for path in embedded:
        assert Path(path).is_absolute(), path
        assert "\\" not in path, path
    # ...and nothing in the argv is spelled with a backslash at all.
    for value in argv:
        assert "\\" not in value, value


def test_the_memory_is_the_callers_too(inputs: BootInputs, tmp_path: Path) -> None:
    """``-m`` is a parameter so the machine can be made to agree with a board."""
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        memory_mib=8192,
    )
    assert _flag(argv, "-m") == "8192", argv


def test_the_ramfb_geometry_is_only_claimed_with_the_driver_that_sets_it(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """``-fw_cfg`` carries ``SCREEN``, and only when ``QemuRamfb.efi`` is staged.

    ``run-qemu.py:105-116`` puts the driver on the ESP ahead of DarwinBoot and it
    is what reads the item, so publishing the geometry without the driver would be
    a claim about a frame buffer nothing ever resized.  :func:`boot` therefore
    passes ``ramfb_size`` only when it sees the file.
    """
    without = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run"
    )
    assert "-fw_cfg" not in without, without
    with_geometry = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
        ramfb_size=ub.SCREEN,
    )
    assert with_geometry[-2:] == [
        "-fw_cfg",
        f"name=opt/org.unixnu/ramfb-size,string={ub.SCREEN}",
    ], with_geometry
    assert "\\" not in with_geometry[-1], with_geometry[-1]


@pytest.mark.parametrize("port", [0, -1, 65536, 70000])
def test_a_port_outside_the_tcp_range_is_refused(
    inputs: BootInputs, tmp_path: Path, port: int
) -> None:
    """Validated here rather than by QEMU's exit.

    QEMU would refuse the port too, but it refuses it *after* it has started, and
    a run that started is a run that produced logs to read.
    """
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
        qmp_port=port,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "1..65535" in error.message, error
    assert error.details.get("qmp_port") == repr(port), error.details


def test_a_missing_firmware_is_refused(inputs: BootInputs, tmp_path: Path) -> None:
    """The vars flash is sized from the code flash, so neither can start alone."""
    inputs.firmware.unlink()
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
    )
    assert error.code is ErrorCode.IMAGE_NOT_FOUND, error
    assert "code flash" in error.message, error


def test_an_empty_firmware_is_refused_the_same_way(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """A zero-length file is as unusable as an absent one."""
    inputs.firmware.write_bytes(b"")
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
    )
    assert error.code is ErrorCode.IMAGE_NOT_FOUND, error


# ---------------------------------------------------------------------------
# the root volume
# ---------------------------------------------------------------------------
def test_the_root_volume_is_a_second_read_only_mmio_disk(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The OS volume is attached read-only, over MMIO, on its own drive id.

    Three properties, each with the reason it is asserted rather than assumed:

    * ``readonly=on``.  This is the shipped system volume; a boot that writes to
      it is a different experiment with different consequences (PRD 4.2's
      "read-only property correct" row).
    * Its own ``id``.  Two block devices sharing one drive id is one device, and
      the ESP would stop being the disk the firmware booted from.
    * **MMIO, not PCI.**  ``NativeVirtioBlock::probe``
      (``vendor/UniXNU/drivers/NativeVirtioBlock/NativeVirtioBlock.cpp:148-154``)
      returns ``nullptr`` unless the provider's ``compatible`` is exactly the
      twelve bytes ``"virtio,mmio"``, so ``virtio-blk-pci`` can never be bound
      however the kernel is configured.  This is the one assertion in the file
      that was written *after* the code got it wrong: the first version emitted
      PCI, QEMU accepted the argv without complaint, and the only thing that
      revealed the mistake was reading the driver's ``probe``.
    """
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"\0" * (1 << 20))
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        root_image=root,
    )

    assert _values(argv, "-drive")[-1] == (
        f"if=none,id=osroot,format=raw,readonly=on,file={root.resolve().as_posix()}"
    ), argv
    assert _values(argv, "-device") == [
        "virtio-blk-pci,drive=esp,bootindex=0",
        "virtio-blk-device,drive=osroot",
        "ramfb",
        # The guest's actual HID devices.  ``usb-kbd`` was here instead, and
        # ``kernel.macho.adt`` has zero usb/xhci/dwc3/hid nodes against 32
        # virtio-mmio ones, so the guest had no driver for it at all.
        "virtio-tablet-device",
        "virtio-keyboard-device",
    ], argv
    # The ESP stays on PCI: EDK II has to find it on the virtual PCI bus
    # (BdsDxe loads Boot0001 from PciRoot(0x0)/Pci(0x1,0x0)), so the two disks
    # are on different transports by necessity, not by oversight.
    assert "virtio-blk-pci,drive=osroot" not in argv, argv
    # ...and upstream's global, without which the MMIO transport stays in legacy
    # mode and the driver's register offsets do not hold (run-qemu.py:203-208).
    assert _flag(argv, "-global") == "virtio-mmio.force-legacy=false", argv
    # After the ESP, so the ESP keeps the device number every earlier run recorded.
    assert argv.index("virtio-blk-pci,drive=esp,bootindex=0") < argv.index(
        "virtio-blk-device,drive=osroot"
    ), argv


def test_without_a_root_image_the_argv_is_exactly_what_it_was(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The parameter is additive, not a rewrite of the device list.

    The guard this pins: a change that *reordered* the ESP drive, or emitted the
    root drive with a different spelling, would still satisfy every assertion
    about the root drive itself while silently changing the machine every
    earlier run described.  So the no-root argv is asserted to be the root argv
    with exactly ten tokens removed -- the ``-global`` pair, the root drive
    pair, the root block-device pair, and the two VirtIO input device pairs
    that arrive with the OS root.
    """
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"\0" * (1 << 20))
    with_root = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        root_image=root,
    )
    without = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run"
    )
    assert len(with_root) - len(without) == 10, (len(with_root), len(without))
    # The ten added tokens, removed, leave the argv exactly as it was.  A
    # change that reordered anything would still pass every assertion above
    # while changing the machine every earlier run described.
    added_at = next(i for i, token in enumerate(with_root) if "id=osroot" in token)
    # The transport pair and the drive pair, asserted as a contiguous block so a
    # reordering cannot pass by accident.
    assert with_root[added_at - 3 : added_at + 3] == [
        "-global",
        "virtio-mmio.force-legacy=false",
        "-drive",
        with_root[added_at],
        "-device",
        "virtio-blk-device,drive=osroot",
    ], with_root[added_at - 3 : added_at + 3]
    remainder = with_root[: added_at - 3] + with_root[added_at + 3 :]
    # Then the two HID device pairs, which ``run-qemu.py:208-210`` also emits as
    # a contiguous pair.  Removing both blocks has to leave exactly the no-root
    # argv -- that is the whole claim of this test.
    hid_at = remainder.index("virtio-tablet-device")
    assert remainder[hid_at - 1 : hid_at + 3] == [
        "-device", "virtio-tablet-device", "-device", "virtio-keyboard-device",
    ], remainder[hid_at - 1 : hid_at + 3]
    assert remainder[: hid_at - 1] + remainder[hid_at + 3 :] == without, without


def test_a_missing_root_image_is_refused_before_qemu_starts(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """Refused here, not by QEMU's exit 1.

    QEMU would refuse the path too, but *after* it started -- and a run that
    started is a run that produced a serial log to read, one that stops at
    ``Waiting on IOMedia`` and looks exactly like a run that had no root volume
    at all.  The message says so, because that is the failure an operator hits.
    """
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
        root_image=tmp_path / "not-there.raw",
    )
    assert error.code is ErrorCode.IMAGE_NOT_FOUND, error
    assert "vfs_mountroot" in error.message, error


def test_an_empty_root_image_is_refused(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """A zero-length file is a valid raw disk to QEMU and a useless root volume.

    The code is CONFIG_INVALID, not IMAGE_NOT_FOUND.  ``IMAGE_NOT_FOUND`` is
    absent from ``errors._CODE_TO_EXIT`` so it maps to exit 1 -- indistinguishable
    from every other unmapped code -- and a caller that branches on it and retries
    retries forever, because retrying can never change a file's length from zero.
    The message used to be the only discriminator, which defeats the point of the
    taxonomy.  The pre-existing firmware check makes the same merge; this one was
    fixed rather than copying it.
    """
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"")
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
        root_image=root,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "empty" in error.message, error


def test_a_udif_container_is_refused_as_a_root_volume(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """A UDIF container passes every size check and is still not a raw disk.

    ``044-76040-038.dmg`` is 5,157,406,069 bytes, so it passes ``is_file()`` and
    ``st_size > 0`` and QEMU opens it happily -- ``format=raw`` accepts any bytes.
    The guest is then handed a compressed container: no GPT, no APFS, no volume
    UUID, and the boot blocks on the same ``Waiting on IOMedia`` line as a run
    with no root volume at all, while the caller reads ``report.root_image`` set
    and ``main()`` prints "root volume attached".  The argv's success was the lie.

    The suffix is *not* used to decide.  ``044-76034-038.dmg`` is 3,303,014,400
    bytes of raw APFS with no trailer, and a suffix heuristic would refuse the one
    image that needs no decoding -- which is exactly what an earlier version of
    the probe did.  Both halves are asserted here.
    """
    container = tmp_path / "os-root.dmg"
    container.write_bytes(b"\0" * 1024 + b"koly" + b"\0" * 508)
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
        root_image=container,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "UDIF container" in error.message, error
    assert "udif" in error.message, error

    # ...and a raw APFS image carrying the same suffix is accepted.
    raw = tmp_path / "systemos.dmg"
    raw.write_bytes(b"NXSB" + b"\0" * (1 << 20))
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        root_image=raw,
    )
    assert any("id=osroot" in token for token in argv), argv


def test_the_report_says_which_kind_of_run_it_was() -> None:
    """``root_image`` is recorded, and is ``None`` when none was attached.

    A run with a root volume and a run without one are indistinguishable in the
    serial log until the kernel blocks; the report has to state which it was,
    and ``None`` has to mean "no root volume" rather than "not recorded" -- so
    the default survives a round trip through :meth:`BootReport.to_dict`.
    """
    default = ub.BootReport(ok=True)
    assert default.root_image is None, default
    assert default.to_dict()["root_image"] is None, default.to_dict()
    attached = ub.BootReport(ok=True, root_image="F:/somewhere/os-root.raw")
    assert attached.to_dict()["root_image"] == "F:/somewhere/os-root.raw", attached.to_dict()


# ---------------------------------------------------------------------------
# the SystemOS cryptex
# ---------------------------------------------------------------------------
def _verified_command() -> list[str]:
    """The argv of the boot that measured the SystemOS attachment.

    ``local-private/boot24/command.txt`` is the command line of the run that took
    ``_native_owner_cache_stage`` from 1 to 6, so it is the only authority there
    is on where the pair belongs and how it is spelled.  It is absent from every
    checkout by design (PRD 12) -- the same reason ``DarwinBoot.efi`` is -- so a
    test that pins against it skips rather than inventing a shape to pin.
    """
    path = REAL_PRIVATE / "boot24" / "command.txt"
    if not path.is_file():
        pytest.skip("local-private/boot24/command.txt is not present on this host")
    return path.read_text(encoding="utf-8").split()


def _without_file(drive: str) -> str:
    """One ``-drive`` value with its ``file=`` taken out.

    What is left is the part of the spelling that is *not* about this host's
    paths, which is the part worth pinning: ``if=none,id=systemos,format=raw,
    readonly=on``.  Two runs of the same configuration spell that identically
    and differ in the path.
    """
    head, _, _ = drive.partition(",file=")
    return head


def test_the_systemos_cryptex_is_a_third_read_only_mmio_disk(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The attachment the official path was missing, and the three ways it is spelled.

    * ``readonly=on``.  This is a signed, shipped cryptex; a boot that writes to
      it is a different experiment with different consequences (PRD 4.2).
    * Its own ``id``.  Two block devices sharing one drive id is one device, and
      the ESP would stop being the disk the firmware booted from.
    * **MMIO, not PCI.**  ``NativeVirtioBlock::probe``
      (``vendor/UniXNU/drivers/NativeVirtioBlock/NativeVirtioBlock.cpp:148-154``)
      returns ``nullptr`` unless the provider's ``compatible`` is exactly the
      twelve bytes ``"virtio,mmio"`` -- the same reason the OS root volume is
      MMIO, and the reason a PCI spelling would be accepted by QEMU and bound by
      nothing.
    * **Last, after ``-name``.**  A position, not a formatting preference: it is
      where the verified run puts it, and the verified run is the only evidence
      there is about it.
    """
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"\0" * (1 << 20))
    cryptex = tmp_path / "044-76034-038.dmg"
    cryptex.write_bytes(b"NXSB" + b"\0" * (1 << 20))
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        root_image=root, systemos_image=cryptex,
    )

    systemos_drive = (
        f"if=none,id=systemos,format=raw,readonly=on,file={cryptex.resolve().as_posix()}"
    )
    # The last four tokens are the pair, in this order, with the drive spelled
    # exactly as the verified command spells it.
    assert argv[-4:] == [
        "-drive", systemos_drive, "-device", "virtio-blk-device,drive=systemos",
    ], argv[-4:]
    assert _values(argv, "-drive")[-1] == systemos_drive, argv
    assert _values(argv, "-device") == [
        "virtio-blk-pci,drive=esp,bootindex=0",
        "virtio-blk-device,drive=osroot",
        "ramfb",
        "virtio-tablet-device",
        "virtio-keyboard-device",
        "virtio-blk-device,drive=systemos",
    ], argv
    assert "virtio-blk-pci,drive=systemos" not in argv, argv
    assert "readonly=on" in systemos_drive, systemos_drive
    # After ``-name``, which is what "last" means in a command line whose last
    # option is the one that was appended.
    assert argv.index("-name") < argv.index("virtio-blk-device,drive=systemos"), argv
    # The ESP keeps its place and its transport: it is the disk the firmware
    # boots from, and it is found on the virtual PCI bus.
    assert argv.index("virtio-blk-pci,drive=esp,bootindex=0") < argv.index(
        "virtio-blk-device,drive=systemos"
    ), argv
    # Every path in it is absolute and forward-slashed, like every other one.
    for path in _paths_in(systemos_drive):
        assert Path(path).is_absolute(), path
        assert "\\" not in path, path


def test_the_systemos_pair_is_the_verified_commands_spelling(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """Order and spelling, pinned against the run that measured the attachment.

    The whole point of wiring this in is to reproduce a configuration that was
    *measured*, so the test reads the measured command line and compares the two
    token for token -- with ``file=`` removed, because that is the one part that
    is about this host's paths rather than about the machine.  A change that
    reordered the pair, dropped ``readonly=on``, renamed the drive id, or moved
    the pair somewhere other than last fails here.
    """
    verified = _verified_command()
    at = verified.index("virtio-blk-device,drive=systemos")
    # The verified command's own tail: ``-drive`` then ``-device``, and nothing
    # after them.  ``at`` is the device spec, so the drive spec is two back.
    assert verified[at - 3 : at + 1] == [
        "-drive", verified[at - 2], "-device", verified[at],
    ], verified[at - 3 : at + 1]
    assert at + 1 == len(verified), verified[at:]
    assert verified[at - 5] == "-name" and verified[at - 4] == "unipad-unixnu", verified[at - 5:at]

    root = tmp_path / "os-root.raw"
    root.write_bytes(b"\0" * (1 << 20))
    cryptex = tmp_path / "044-76034-038.dmg"
    cryptex.write_bytes(b"NXSB" + b"\0" * (1 << 20))
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        root_image=root, systemos_image=cryptex,
    )
    # ``-drive <spec> -device <spec>``, so the drive spec is two before the
    # device spec -- the token immediately before it is the ``-device`` flag.
    built = argv[argv.index("virtio-blk-device,drive=systemos") - 2]
    assert _without_file(built) == _without_file(verified[at - 2]), (built, verified[at - 2])
    # ...and the pair is this module's last four tokens too, in the same order.
    assert argv[-4:-2] == ["-drive", built], argv[-4:]
    assert argv[-1] == "virtio-blk-device,drive=systemos", argv[-1]
    # The verified run's OS root volume and this one differ only in path, so the
    # shape being pinned is the machine's and not one run's.
    assert _without_file(_values(argv, "-drive")[-2]) == _without_file(
        _values(verified, "-drive")[-2]
    ), argv


def test_the_esp_device_is_the_one_the_verified_run_used(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The ESP's device, byte for byte, and the reason it carries a bootindex.

    ``bootindex=0`` is what keeps the firmware off the UEFI Shell once a third
    block device exists.  That is measured, not inferred: adding the SystemOS
    cryptex flipped UEFI's automatic enumeration order and the serial log stopped
    at ``Shell>`` with DarwinBoot never running, and the run that produced the
    stage-6/status-0 numbers is the one carrying this token.  It is emitted
    unconditionally rather than only when a third disk is attached, because the
    ESP is the disk to boot from in every machine this module builds -- and the
    combination a "third disk" condition would get wrong is reachable from here:
    a cryptex with no OS root volume.

    The spelling is read out of the verified command rather than restated here,
    so the file is the authority and this test fails when the two drift apart.
    The two assertions after it are the ones that hold on a checkout without
    ``local-private/``.
    """
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"\0" * (1 << 20))
    cryptex = tmp_path / "044-76034-038.dmg"
    cryptex.write_bytes(b"NXSB" + b"\0" * (1 << 20))
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        root_image=root, systemos_image=cryptex,
    )
    built = _values(argv, "-device")[0]
    # First, and immediately behind its own ``-drive``: the ESP is the disk the
    # firmware starts, and everything else on the command line is behind it.
    assert built.startswith("virtio-blk-pci,drive=esp,"), built
    assert argv[argv.index(built) - 1] == "-device", argv
    assert "id=esp" in argv[argv.index(built) - 2], argv
    assert "bootindex=0" in built, built
    # One bootindex, on the ESP, on no other device.
    assert sum("bootindex" in item for item in argv) == 1, argv
    assert built.index("bootindex") > built.index("drive=esp"), built

    verified = _verified_command()
    theirs = next(item for item in verified if item.startswith("virtio-blk-pci,drive=esp"))
    assert built == theirs, (built, theirs)


def test_without_a_systemos_image_the_argv_has_no_third_disk(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The parameter is additive: no cryptex, no drive, no extra global.

    Every run without one is the state this module has produced since it was
    written, and an unattached run has to stay byte-identical to it -- a
    ``-device`` that appeared unconditionally would change the machine every
    earlier run described.
    """
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run"
    )
    # Scrub the scratch directory first: it is named after this test, and a
    # "systemos" inside a path is the tmpdir, not a drive.
    assert not any("systemos" in token for token in _scrubbed(argv, tmp_path)), argv
    assert "virtio-blk-device,drive=systemos" not in argv, argv
    assert not any(item.startswith("if=none,id=systemos") for item in argv), argv
    assert _values(argv, "-device") == [
        "virtio-blk-pci,drive=esp,bootindex=0", "ramfb",
    ], argv
    assert len(_values(argv, "-drive")) == 3, argv
    assert _values(argv, "-global") == [], argv


def test_a_cryptex_without_an_os_root_volume_still_gets_the_modern_mmio_transport(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The global is about the transport, not about the root volume.

    ``-device virtio-blk-device`` is MMIO whichever disk it fronts, and legacy
    mode would hand the cryptex the wrong register layout -- the driver's
    ``mapDeviceMemoryWithIndex(0)`` has to be at least 0x108, which is the modern
    layout's offset.  The global normally rides with the OS root volume because
    that is where upstream puts it; with no root volume it goes in front of the
    cryptex instead.
    """
    cryptex = tmp_path / "044-76034-038.dmg"
    cryptex.write_bytes(b"NXSB" + b"\0" * (1 << 20))
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        systemos_image=cryptex,
    )
    assert argv[-6:] == [
        "-global", "virtio-mmio.force-legacy=false",
        "-drive",
        f"if=none,id=systemos,format=raw,readonly=on,file={cryptex.resolve().as_posix()}",
        "-device", "virtio-blk-device,drive=systemos",
    ], argv[-6:]
    # ...and with a root volume it is emitted once, before that drive, not twice.
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"\0" * (1 << 20))
    both = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        root_image=root, systemos_image=cryptex,
    )
    assert _values(both, "-global") == ["virtio-mmio.force-legacy=false"], both


def test_a_missing_systemos_image_is_refused_before_qemu_starts(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """Refused here, not by QEMU's exit 1 -- and the message says what is lost.

    The failure this prevents is *not* the same as a missing root volume: without
    a cryptex the guest gets past ``vfs_mountroot``, passes the root checks, and
    panics.  So the message has to name the panic rather than the mountroot
    block, or an operator who has seen one will go looking for the other.
    """
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
        systemos_image=tmp_path / "not-there.dmg",
    )
    assert error.code is ErrorCode.IMAGE_NOT_FOUND, error
    assert "SystemOS cryptex" in error.message, error
    assert "native_owner_cache_prepare" in error.message, error


def test_an_empty_systemos_image_is_refused(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """A zero-length file is a valid raw disk to QEMU and a useless cryptex.

    ``CONFIG_INVALID``, not ``IMAGE_NOT_FOUND``, for the reason the root volume
    gives: retrying can never change a file's length from zero, so a caller that
    branched on the code and retried would retry forever.
    """
    cryptex = tmp_path / "044-76034-038.dmg"
    cryptex.write_bytes(b"")
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
        systemos_image=cryptex,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "empty" in error.message, error
    assert "SystemOS cryptex" in error.message, error


def test_a_udif_container_is_refused_as_a_systemos_image(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The two ``.dmg`` candidates in an IPSW are told apart by their last 512 bytes.

    ``044-76034-038.dmg`` is 3,303,014,400 bytes of raw APFS with no trailer and
    needs no decoding; ``044-76040-038.dmg`` is a UDIF container that passes every
    size check and would be handed to the guest compressed.  Both carry the same
    suffix, so the suffix decides nothing -- and a run given the container would
    attach a device that publishes no volume, which is indistinguishable from
    attaching nothing at all.
    """
    container = tmp_path / "044-76040-038.dmg"
    container.write_bytes(b"\0" * 1024 + b"koly" + b"\0" * 508)
    error = _refused(
        ub.build_argv,
        tmp_path / "qemu-system-aarch64.exe",
        inputs,
        tmp_path / "esp",
        tmp_path / "run",
        systemos_image=container,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "UDIF container" in error.message, error
    assert "udif" in error.message, error

    # ...and a raw APFS image carrying the same suffix is accepted.
    raw = tmp_path / "044-76034-038.dmg"
    raw.write_bytes(b"NXSB" + b"\0" * (1 << 20))
    argv = ub.build_argv(
        tmp_path / "qemu-system-aarch64.exe", inputs, tmp_path / "esp", tmp_path / "run",
        systemos_image=raw,
    )
    assert any("id=systemos" in token for token in argv), argv


def test_the_report_names_the_systemos_attachment() -> None:
    """``systemos_image`` is recorded, and is ``None`` when none was attached.

    A run with a cryptex and a run without one both reach the kernel, and both
    print ``XNU:`` lines -- so the serial log cannot separate them and the report
    has to.  ``None`` has to mean "no cryptex" rather than "not recorded", which
    is why the default is asserted through a round trip.
    """
    default = ub.BootReport(ok=True)
    assert default.systemos_image is None, default
    assert default.to_dict()["systemos_image"] is None, default.to_dict()
    attached = ub.BootReport(ok=True, systemos_image="044-76034-038.dmg")
    assert attached.to_dict()["systemos_image"] == "044-76034-038.dmg", attached.to_dict()


# ---------------------------------------------------------------------------
# the PE subsystem
# ---------------------------------------------------------------------------
def test_a_non_pe_file_has_no_subsystem(tmp_path: Path) -> None:
    """``None`` rather than an exception: the caller's message is the same either way."""
    path = tmp_path / "not-a-pe"
    path.write_bytes(b"this is not a PE image at all")
    assert ub.pe_subsystem(path) is None
    assert ub.pe_subsystem(tmp_path / "absent") is None


def test_a_pe32_image_has_no_subsystem(tmp_path: Path) -> None:
    """AArch64 UEFI is PE32+; PE32 is the 32-bit form and cannot be this."""
    blob = bytearray(_pe32plus(ub.PE_SUBSYSTEM_APPLICATION))
    struct.pack_into("<H", blob, 0x58, 0x10B)  # PE32, not PE32+
    path = tmp_path / "pe32.efi"
    path.write_bytes(bytes(blob))
    assert ub.pe_subsystem(path) is None


def test_a_pe32plus_driver_reads_as_a_driver(tmp_path: Path) -> None:
    """Subsystem 11 is what ``DarwinBootPkg`` builds, and what the firmware refuses."""
    path = tmp_path / "driver.efi"
    path.write_bytes(_pe32plus(11))
    assert ub.pe_subsystem(path) == 11
    assert ub.PE_SUBSYSTEM_NAMES[11] == "EFI boot service driver"


def test_the_real_darwinboot_is_a_boot_service_driver(tmp_path: Path) -> None:
    """The measured fact this module's diagnosis rests on.

    ``DarwinBoot.efi`` is built as ``UEFI_DRIVER``, which is why UniXNU loads it
    from ``Payloads/`` and why the firmware cannot be handed it as
    ``BOOTAA64.EFI``.  ``local-private/`` is not committed (PRD 12), so this is
    skipped wherever the real build output is absent.
    """
    candidates = sorted(REAL_PRIVATE.rglob("DarwinBoot.efi")) if REAL_PRIVATE.is_dir() else []
    if not candidates:
        pytest.skip("no DarwinBoot.efi under local-private/ on this host")
    assert ub.pe_subsystem(candidates[0]) == 11, candidates[0]


def test_a_truncated_pe_is_not_a_pe(tmp_path: Path) -> None:
    """A file that starts like a PE and stops is not one."""
    path = tmp_path / "truncated.efi"
    path.write_bytes(_pe32plus(11)[:0x30])
    assert ub.pe_subsystem(path) is None


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def test_boot_refuses_before_writing_anything_when_qemu_is_absent(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """Nothing is written, so a refusal leaves no run directory to clean up."""
    work = tmp_path / "run"
    error = _refused(
        ub.boot,
        inputs,
        work,
        qemu=tmp_path / "qemu-system-aarch64.exe",
        device_dir=REAL_DEVICE,
    )
    assert error.code is ErrorCode.QEMU_LAUNCH_FAILED, error
    assert "qemu-system-aarch64 not found" in error.message, error
    assert not work.exists(), list(tmp_path.rglob("*"))


def test_boot_refuses_before_writing_anything_when_an_input_is_absent(
    inputs: BootInputs, qemu: Path, tmp_path: Path
) -> None:
    """The missing input is named by role, and nothing has been staged yet.

    ``boot`` is the first place all five inputs are known together, so it is the
    place that can say *which* one is missing -- and it says so before it creates
    a directory, because a run directory full of logs from a run that never
    started is misleading evidence.
    """
    inputs = dataclasses.replace(inputs, devicetree=tmp_path / "absent.adt")
    work = tmp_path / "run"
    error = _refused(ub.boot, inputs, work, qemu=qemu, device_dir=REAL_DEVICE)
    assert error.code is ErrorCode.IMAGE_NOT_FOUND, error
    missing = error.details.get("missing", [])
    assert missing == ["devicetree=absent.adt"], missing
    assert not work.exists(), list(tmp_path.rglob("*"))
    assert not (tmp_path / "esp").exists()


def test_boot_names_every_absent_input_at_once(
    inputs: BootInputs, qemu: Path, tmp_path: Path
) -> None:
    """One refusal listing all of them, rather than one per run.

    ``BootInputs.missing`` is the list the refusal carries, and a run that names
    only the first absent file makes an operator iterate.
    """
    inputs = dataclasses.replace(
        inputs,
        devicetree=tmp_path / "absent.adt",
        darwinboot=tmp_path / "absent.efi",
        payload_toml=tmp_path / "absent.toml",
    )
    error = _refused(ub.boot, inputs, tmp_path / "run", qemu=qemu, device_dir=REAL_DEVICE)
    assert error.details.get("missing") == [
        "devicetree=absent.adt",
        "darwinboot=absent.efi",
        "payload_toml=absent.toml",
    ], error.details


def test_boot_refuses_a_driver_as_bootaa64(inputs: BootInputs, qemu: Path, tmp_path: Path) -> None:
    """A subsystem-11 ``BOOTAA64.EFI`` is found, opened, and then rejected.

    Measured on this host with the pinned EDK II firmware: the UEFI shell prints
    ``The image is not an application.`` and BDS prints ``failed to load Boot0001
    ... Not Found`` -- neither of which names the subsystem.  The refusal here
    has to, or the run looks like a missing file.
    """
    inputs.unixnu.write_bytes(_pe32plus(11))
    work = tmp_path / "run"
    error = _refused(ub.boot, inputs, work, qemu=qemu, device_dir=REAL_DEVICE)
    assert error.code is ErrorCode.INVALID_PACKAGE, error
    assert "PE subsystem 11" in error.message, error
    assert "EFI boot service driver" in error.message, error
    assert "subsystem 10" in error.message, error
    assert error.details.get("subsystem") == 11, error.details
    assert error.details.get("required_subsystem") == 10, error.details
    # ...and the reason names where the rule comes from, so the operator can
    # check it rather than trust it.
    assert "UniXNU.inf" in error.details.get("reason", ""), error.details
    assert "MODULE_TYPE = UEFI_APPLICATION" in error.details.get("reason", ""), error.details
    # Before anything is written: no ESP, no vars flash, no logs.
    assert not work.exists(), list(tmp_path.rglob("*"))


def test_boot_refuses_an_image_that_is_not_pe32plus_at_all(
    inputs: BootInputs, qemu: Path, tmp_path: Path
) -> None:
    """A non-PE is refused with its own message, not as "subsystem None"."""
    inputs.unixnu.write_bytes(b"not a PE image")
    error = _refused(
        ub.boot, inputs, tmp_path / "run", qemu=qemu, device_dir=REAL_DEVICE
    )
    assert error.code is ErrorCode.INVALID_PACKAGE, error
    assert "is not a PE32+ AArch64 image" in error.message, error


def test_boot_refuses_a_board_whose_dram_size_disagrees_with_the_machine(
    inputs: BootInputs, qemu: Path, tmp_path: Path
) -> None:
    """The tree and the machine describe the same memory or the run is meaningless."""
    device = tmp_path / "device"
    device.mkdir()
    (device / "board.toml").write_text(
        '[payload]\nloader = "driver"\ndram_size = "0x80000000"\n'
        "[payload.adt]\nchip_id = \"207\"\n",
        encoding="utf-8",
    )
    error = _refused(
        ub.boot, inputs, tmp_path / "run", qemu=qemu, device_dir=device
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "dram_size" in error.message, error
    assert error.details.get("board_mib") == 2048, error.details
    assert error.details.get("machine_mib") == ub.MEMORY_MIB, error.details


def test_boot_refuses_a_device_with_no_payload_block(
    inputs: BootInputs, qemu: Path, tmp_path: Path
) -> None:
    """Read before anything is staged, so the refusal names the board."""
    device = tmp_path / "device"
    device.mkdir()
    (device / "board.toml").write_text('[board]\nname = "x"\n', encoding="utf-8")
    error = _refused(
        ub.boot, inputs, tmp_path / "run", qemu=qemu, device_dir=device
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "[payload]" in error.message, error
    assert not (tmp_path / "run").exists(), list(tmp_path.rglob("*"))


def test_the_inputs_record_names_every_role_by_final_component(inputs: BootInputs) -> None:
    """``to_dict`` is what lands in the evidence bundle (PRD 9.2)."""
    record = inputs.to_dict()
    assert set(record) == {"firmware", "kernel", "devicetree", "darwinboot", "unixnu"}, record
    assert record["firmware"] == inputs.firmware.name, record
    with_toml = dataclasses.replace(inputs, payload_toml=Path("unixnu.toml"))
    assert with_toml.to_dict()["payload_toml"] == "unixnu.toml"


def test_the_inputs_record_reports_each_absent_role(inputs: BootInputs) -> None:
    """``missing()`` is the list the refusal carries, so it is checked directly."""
    assert inputs.missing() == []
    absent = dataclasses.replace(
        inputs,
        darwinboot=_absent("DarwinBoot.efi"),
        payload_toml=_absent("absent.toml"),
    )
    assert absent.missing() == ["darwinboot=DarwinBoot.efi", "payload_toml=absent.toml"]


def test_a_report_serialises_every_field() -> None:
    """The report is evidence, so it has to survive a round trip through JSON."""
    import json

    report = ub.BootReport(ok=False, workdir="run", command=["qemu", "-m", "6144"])
    report.reached_qmp = True
    report.qmp_evidence = {"port": 14444, "attempts": 2}
    report.serial_bytes = 12
    report.serial_markers = ["UNIXNU: hello"]
    report.screenshots = ["screen-001s.png"]
    report.inputs = {"kernel": "kernel.macho"}
    report.error_code = "qemu_launch_failed"
    report.error_message = "qemu exited before qmp became ready"
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["ok"] is False, payload
    assert payload["reached_qmp"] is True, payload
    assert payload["command"] == ["qemu", "-m", "6144"], payload
    assert payload["qmp_evidence"]["attempts"] == 2, payload
    assert payload["serial_markers"] == ["UNIXNU: hello"], payload
    assert payload["error_code"] == "qemu_launch_failed", payload
    assert report.exit_code is None


def test_the_defaults_are_the_ones_the_docstrings_claim() -> None:
    """The numbers an operator has to be able to reason about, pinned."""
    assert ub.DEFAULT_QMP_PORT == 14444
    assert ub.DEFAULT_SECONDS == 300
    assert ub.DEFAULT_EVERY == 20
    assert ub.ARGUMENT_LIMIT == 1000
    assert ub.SCREEN == "1376x1024"
    assert ub.MEMORY_MIB == 6144




# ---------------------------------------------------------------------------
# the report, and what it says about a root volume
# ---------------------------------------------------------------------------
class _ExitedQEMU:
    """A stand-in for a QEMU that is already gone when ``_watch`` looks.

    ``boot`` reaches ``_watch``, sees ``poll()`` return non-None on the first
    iteration, and builds the rest of the report anyway -- the staged ESP, the
    argv, ``vars.fd`` and ``boot-report.json`` all survive.  That is the one
    path that runs ``boot`` to completion with no QEMU, no firmware and no
    kernel on the host, which is the only way this test can exist at all.

    ``stdout`` and ``stderr`` are ``None`` because ``boot`` opens its own log
    handles and hands them to the child; a fake that claimed to own them would
    close files ``boot`` is still inside a ``with`` block for.
    """

    returncode = 3
    stdout = None
    stderr = None

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def _already_exited(launched: list[list[str]]):
    """A ``Popen`` that records the argv and reports the process as exited."""

    def start(command: list[str], **_: Any) -> _ExitedQEMU:
        launched.append(list(command))
        return _ExitedQEMU()

    return start


def _synthetic_device(tmp_path: Path) -> Path:
    """A ``device_dir`` whose board.toml is present, valid and self-consistent.

    These tests exist to cover the *report* path, which is reached only after
    ``read_board_payload`` has accepted a board -- so they need a board, and
    ``REAL_DEVICE`` is not one they can have: ``vendor/`` is git-excluded, so the
    unit stage runs in a checkout where it does not exist.  Measured on CI
    2026-10-25 (run 40169385708): three tests died with
    ``board.toml not found in UniXNU-qemu`` -- they were the only tests in this
    module that pass a *real* qemu and therefore the only ones that get far
    enough to need the board, which is why the module's ``real_spec`` fixture
    and its ``pytest.skip`` never covered them.

    A synthetic board is the right answer rather than depending on ``real_spec``
    and skipping, because skipping would mean the report path has no coverage
    anywhere a clean checkout runs -- which is every CI run.  ``dram_size`` is
    spelled to agree with ``MEMORY_MIB`` because ``boot`` refuses a board that
    disagrees with the machine, and a test that failed for that reason would
    prove nothing about the report.
    """
    device = tmp_path / "device"
    device.mkdir(exist_ok=True)
    (device / "board.toml").write_text(
        '[payload]\nloader = "driver"\n'
        f'dram_size = "{ub.MEMORY_MIB * 1024 * 1024:#x}"\n'
        # ``read_board_adt`` refuses a board with no [payload.adt] table at all,
        # and a ``chip_id`` is the one key it will not default -- so the minimal
        # self-consistent board is these three lines.  The hex string carries no
        # 0x prefix because make-qemu-adt.py reads it with ``int(value, 16)``.
        '[payload.adt]\nchip_id = "207"\n',
        encoding="utf-8",
    )
    return device


def test_boot_builds_a_report_that_says_a_root_volume_was_attached(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report names the attachment, and the run directory proves it.

    ``root_image`` is the one boot argument that is checked *after* the ESP has
    been written (``build_argv`` runs after ``stage_esp``), so a bad root volume
    leaves a staged run behind -- and until this test existed nothing covered
    the assignment that puts it in the report at all.  The property that matters
    is not that the field is set but that a reader of ``boot-report.json`` can
    tell an attached run from an unattached one, because those two runs differ
    in whether the guest can ever reach ``launchd``.
    """
    import json

    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    work = tmp_path / "run"
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"raw os volume, not a UDIF container")

    report = ub.boot(
        inputs, work, qemu=qemu, device_dir=_synthetic_device(tmp_path), root_image=root
    )

    # The failure is the fake's, not the attachment's: QEMU "exited" before QMP
    # answered, which is exactly what this stand-in is for.
    assert report.error_code is ErrorCode.QEMU_LAUNCH_FAILED, report
    assert report.exit_code == 3, report
    assert report.ok is False, report

    # ``safe_location`` keeps the final component only (PRD 9.2), so the report
    # says *which* volume without carrying the drive, the profile or the
    # username.  That is a property, not an accident, and it is pinned here.
    assert report.root_image == "os-root.raw", report.root_image
    assert str(tmp_path) not in report.root_image, report.root_image

    written = json.loads((work / "boot-report.json").read_text(encoding="utf-8"))
    assert written["root_image"] == "os-root.raw", written
    assert written["error_code"] == "qemu_launch_failed", written
    assert launched, "boot never reached Popen"

    # The attachment is in the argv the guest was actually offered, and it is
    # the MMIO transport the driver binds -- not the PCI one the ESP needs.
    command = launched[0]
    assert "virtio-blk-device,drive=osroot" in command, command
    assert "virtio-mmio.force-legacy=false" in command, command
    assert not any("virtio-blk-pci,drive=osroot" in item for item in command), command
    assert f"if=none,id=osroot,format=raw,readonly=on,file={root.resolve().as_posix()}" in command, command

    # And the run directory is a real one: five ESP files, a vars flash the size
    # of the code flash, and a record of what was staged.
    assert sorted(p.relative_to(work / "esp").as_posix() for p in (work / "esp").rglob("*")
                  if p.is_file()) == sorted(ESP_FILES)
    assert (work / "vars.fd").stat().st_size == inputs.firmware.stat().st_size
    assert (work / "vars.fd").read_bytes() == b"\0" * inputs.firmware.stat().st_size
    staged = json.loads((work / "esp-manifest.json").read_text(encoding="utf-8"))
    assert sorted(key for key in staged if key.startswith("EFI/")) == sorted(ESP_FILES), staged


def test_boot_without_a_root_volume_says_so_in_the_report(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unattached run is labelled too, and its argv has no second disk.

    An unattended run and an attached one are different machines: the first
    blocks in ``vfs_mountroot`` forever and the second might not.  A report that
    left ``root_image`` null in both cases would make the two
    indistinguishable, which is the failure this pins.
    """
    import json

    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    work = tmp_path / "run"

    report = ub.boot(
        inputs, work, qemu=qemu, device_dir=_synthetic_device(tmp_path)
    )

    assert report.root_image is None, report.root_image
    written = json.loads((work / "boot-report.json").read_text(encoding="utf-8"))
    assert written["root_image"] is None, written
    assert not any("osroot" in item for item in launched[0]), launched[0]
    assert not any("virtio-mmio.force-legacy=false" in item for item in launched[0]), launched[0]


def test_the_cli_prints_which_kind_of_run_it_was(
    inputs: BootInputs, qemu: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """``main`` says out loud whether a root volume went with the run.

    Both branches, because the line an operator reads is the one that tells
    them which of the two machines they just booted -- and the unattached
    branch used to be silent, so a run that could never reach ``launchd``
    looked like any other.
    """
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited([]))
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"raw os volume")
    base = [
        "--device", str(_synthetic_device(tmp_path)),
        "--qemu", str(qemu),
        "--firmware", str(inputs.firmware),
        "--kernel", str(inputs.kernel),
        "--devicetree", str(inputs.devicetree),
        "--darwinboot", str(inputs.darwinboot),
        "--unixnu", str(inputs.unixnu),
        "--workdir", str(tmp_path / "run"),
    ]

    assert ub.main([*base, "--root-image", str(root)]) == 1
    attached = capsys.readouterr().out
    assert "root volume attached: os-root.raw" in attached, attached
    assert str(tmp_path) not in attached, attached

    assert ub.main(base) == 1
    unattached = capsys.readouterr().out
    assert "no root volume attached: the guest has the ESP alone" in unattached, unattached
    # The exact line, not the phrase: "no root volume attached" *contains*
    # "root volume attached", so the substring form passes on the branch it is
    # meant to exclude.  An earlier version of this test had that bug and the
    # suite went green anyway.
    assert "root volume attached: os-root.raw" not in unattached, unattached


def test_the_cli_reaches_every_optional_input_bootinputs_has(
    inputs: BootInputs, qemu: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """The unified path's own CLI must be able to say everything ``boot()`` can.

    ``--data-ramdisk`` and ``--systemos-image`` exist because the official path
    was dropping them.  ``--trust-cache`` did not exist, so the CLI of the very
    module that carries ``BootInputs.trust_cache`` could not express one: the
    diagnostics could (``probe-root-volume.py`` passes it into ``BootInputs``),
    which is exactly how a verified configuration ended up reachable only from
    the diagnostics.  ``stage_esp``, ``missing()``, ``to_dict()`` and
    ``_check_carried`` all handle the field; the flag was the one place it was
    not wired.

    Two-sided, and the teeth are the flag itself rather than the behaviour: the
    parser is asked for ``--trust-cache`` by name, so deleting the argument is a
    red test even though every downstream assertion would then be vacuously
    about the default branch.
    """
    import pytest as _pytest

    bundle = tmp_path / "original-static-system.bundle"
    bundle.write_bytes(b"synthetic trust bundle")

    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited([]))
    base = [
        "--device", str(_synthetic_device(tmp_path)),
        "--qemu", str(qemu),
        "--firmware", str(inputs.firmware),
        "--kernel", str(inputs.kernel),
        "--devicetree", str(inputs.devicetree),
        "--darwinboot", str(inputs.darwinboot),
        "--unixnu", str(inputs.unixnu),
        "--workdir", str(tmp_path / "run"),
    ]

    assert ub.main([*base, "--trust-cache", str(bundle)]) == 1
    attached = capsys.readouterr().out
    assert "trust cache staged: original-static-system.bundle" in attached, attached
    # The ESP is where "staged" has to be proven, not the line that claims it.
    staged = tmp_path / "run" / "esp" / TRUSTCACHE_NAME
    assert staged.is_file(), sorted(p.name for p in staged.parent.iterdir())
    assert staged.read_bytes() == b"synthetic trust bundle", staged
    # No absolute path in the operator's view, the rule the root-image test keeps.
    assert str(tmp_path) not in attached, attached

    assert ub.main(base) == 1
    absent = capsys.readouterr().out
    assert "no trust cache staged: AMFI will refuse to exec launchd" in absent, absent
    # The full line, not the phrase -- and this is the same trap the root-image
    # test two functions up documents: "no trust cache staged:" *contains*
    # "trust cache staged:", so the substring form passes on the branch it is
    # meant to exclude.  This test had that bug on its first run and the suite
    # caught it, which is the argument for writing the assertion both ways.
    assert "trust cache staged: original-static-system.bundle" not in absent, absent
    assert not (tmp_path / "run" / "esp" / TRUSTCACHE_NAME).is_file()

    # The flag exists.  Without this the three assertions above pass on a CLI
    # that ignores ``--trust-cache`` entirely -- argparse rejects an unknown
    # option, so a deleted flag fails with SystemExit(2) and the test dies on
    # the return code, which is a weaker failure than naming the flag.
    with _pytest.raises(SystemExit) as caught:
        ub.main(["--device", "x", "--qemu", "y", "--trust-cache", str(bundle)])
    assert caught.value.code == 2, caught.value


# ---------------------------------------------------------------------------
# the optional inputs, through boot() rather than through stage_esp
# ---------------------------------------------------------------------------
def _both_halves(inputs: BootInputs, tmp_path: Path) -> BootInputs:
    """The input set a boot with a trust cache *and* a Data seed needs.

    Both halves of the ramdisk pair, because a boot that carries one without the
    other is refused by :func:`stage_esp` -- so a test about the *other* two
    optional fields has to supply this pair correctly or it fails on the ramdisk
    and proves nothing about what it is about.
    """
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    bundle = tmp_path / "original-static-system.bundle"
    bundle.write_bytes(b"synthetic trust bundle")
    return replace(
        inputs, devicetree=tree, data_ramdisk=seed, trust_cache=bundle
    )


def test_a_boot_that_asks_for_a_trust_cache_and_a_data_ramdisk_carries_both(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both optional inputs survive ``boot()`` and land on the ESP the guest gets.

    This is the test the defect was found by.  ``boot()`` rebuilt ``BootInputs``
    by listing the five required paths by hand, so ``trust_cache`` and
    ``data_ramdisk`` were checked by ``missing()``, resolved, and then dropped:
    the ESP carried five files, ``report.inputs`` recorded five, and the guest
    booted without either.  Neither loss is visible in the serial log -- AMFI
    refusing to exec launchd (namespace 9, EXEC, code 8, SECURITY_POLICY,
    ``build-os-root.py:26-27``) and DarwinBoot refusing the ramdisk half with
    ``EFI_COMPROMISED_DATA`` before the kernel starts (``DarwinBoot.c:563-569``)
    -- so the run report was the only place either could have been caught, and it
    was the place that agreed with the lie.

    Reintroducing the defect makes this test fail with the guard's own message,
    which names the field: "resolving the input paths does not carry the
    trust_cache this run asked for".  The per-role assertions below are what keep
    it that way -- a fix that staged the files but recorded something else fails on
    "boot() dropped <role>" rather than on a size mismatch somewhere unrelated.
    """
    import json

    assert ub.TRUSTCACHE_NAME == TRUSTCACHE_NAME, ub.TRUSTCACHE_NAME
    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    work = tmp_path / "run"

    report = ub.boot(
        _both_halves(inputs, tmp_path), work, qemu=qemu, device_dir=_synthetic_device(tmp_path)
    )

    # The run itself is the fake's failure, not the inputs': QEMU "exited" before
    # QMP answered, which is what this stand-in is for.
    assert report.error_code is ErrorCode.QEMU_LAUNCH_FAILED, report
    assert launched, "boot never reached Popen"

    # The ESP carries seven files, not five: the trust cache at the name the
    # loader looks for, and the seed at the one name the config can spell.
    staged_files = sorted(
        path.relative_to(work / "esp").as_posix() for path in (work / "esp").rglob("*")
        if path.is_file()
    )
    assert staged_files == sorted(
        ESP_FILES + (TRUSTCACHE_NAME, f"EFI/UniXNU/Payloads/{ub.DATA_RAMDISK_NAME}")
    ), staged_files
    assert (work / "esp" / TRUSTCACHE_NAME).read_bytes() == b"synthetic trust bundle"
    assert f'initrd = "Payloads/{ub.DATA_RAMDISK_NAME}"' in (
        work / "esp" / "EFI/UniXNU/unixnu.toml"
    ).read_text(encoding="utf-8")

    # ...and the report says so, in both the object and the file on disk.  These
    # are the two lines that used to name five files.
    #
    # ``report.inputs`` names the FILE THE CALLER GAVE -- ``owner-data-1g.dbr`` is
    # the name :func:`_both_halves` writes the seed under -- and the staged
    # manifest names what it became on the ESP.  They are deliberately separate
    # assertions: they used to be the same string by coincidence, because
    # DATA_RAMDISK_NAME was the seed's host name as well as its ESP name, and
    # that coincidence is what let the ESP spelling drift from the payload
    # build's until the first verified delivery was refused by
    # ``_check_ramdisk_pair``.
    for role, expected in (
        ("trust_cache", "original-static-system.bundle"),
        ("data_ramdisk", "owner-data-1g.dbr"),
    ):
        assert report.inputs.get(role) == expected, (
            f"boot() dropped {role}: the report records {sorted(report.inputs)}"
        )
    written = json.loads((work / "boot-report.json").read_text(encoding="utf-8"))
    assert written["inputs"].get("trust_cache") == "original-static-system.bundle", written
    assert written["inputs"].get("data_ramdisk") == "owner-data-1g.dbr", written
    # ...and what it was staged as is the payload build's spelling, recorded in
    # the ESP manifest rather than inferred from the input's own name.
    staged_manifest = json.loads((work / "esp-manifest.json").read_text(encoding="utf-8"))
    assert staged_manifest.get(f"EFI/UniXNU/Payloads/{ub.DATA_RAMDISK_NAME}"), staged_manifest

    # ...and the ESP manifest, which is the record of what was actually written.
    manifest = json.loads((work / "esp-manifest.json").read_text(encoding="utf-8"))
    assert manifest.get(TRUSTCACHE_NAME) == len(b"synthetic trust bundle"), manifest
    assert (
        manifest.get(f"EFI/UniXNU/Payloads/{ub.DATA_RAMDISK_NAME}")
        == (work / "esp" / "EFI/UniXNU/Payloads" / ub.DATA_RAMDISK_NAME).stat().st_size
    ), manifest


def test_a_boot_without_either_optional_input_records_neither(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default is unchanged, so the guard above is not a guard against everything.

    A run that asks for neither must still record neither -- a fix that made the
    report name a trust cache unconditionally would make the record useless as an
    answer to "did this boot carry one".
    """
    import json

    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    work = tmp_path / "run"

    report = ub.boot(inputs, work, qemu=qemu, device_dir=_synthetic_device(tmp_path))

    assert TRUSTCACHE_NAME not in report.inputs, report.inputs
    assert "data_ramdisk" not in report.inputs, report.inputs
    assert sorted(
        path.relative_to(work / "esp").as_posix() for path in (work / "esp").rglob("*")
        if path.is_file()
    ) == sorted(ESP_FILES)
    written = json.loads((work / "boot-report.json").read_text(encoding="utf-8"))
    assert "trust_cache" not in written["inputs"], written


def test_a_staged_esp_that_is_missing_an_asked_for_input_is_refused(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing test for the guard: a stage that drops a field is refused.

    ``missing()`` says the file was on disk when the run began; the guard says the
    file is on the ESP the guest is handed.  Those are different claims, and the
    second is the one that failed -- so this test takes a *correct* staging away
    one key at a time and requires the run to refuse, naming the role.

    The ``where`` detail is what keeps it honest.  Without it, a run whose
    *resolution* dropped the field would satisfy every assertion below by raising
    at the other call site, and this test would pass without ever exercising the
    staging half of the guard.
    """
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited([]))
    real_stage = ub.stage_esp

    def dropping(the_inputs: BootInputs, esp: Path, spec: Any) -> dict:
        written = real_stage(the_inputs, esp, spec)
        written.pop(TRUSTCACHE_NAME)
        return written

    monkeypatch.setattr(ub, "stage_esp", dropping)
    error = _refused(
        ub.boot,
        _both_halves(inputs, tmp_path),
        tmp_path / "run",
        qemu=qemu,
        device_dir=_synthetic_device(tmp_path),
    )
    assert error.code is ErrorCode.INVALID_PACKAGE, error
    assert "trust_cache" in error.message, error
    assert error.details.get("role") == "trust_cache", error.details
    assert error.details.get("where") == "the staged ESP", error.details


def test_a_boot_with_a_systemos_cryptex_attaches_it_and_records_it(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attachment is in the argv the guest was offered, and in the report.

    ``boot()`` is where the parameter has to arrive for the official path to
    produce the measured machine, so this asserts all three places it has to show
    up: the argv, ``report.systemos_image`` and ``boot-report.json``.  The
    run-directory half is the same evidence ``root_image``'s test collects, which
    is why the two tests are shaped alike.
    """
    import json

    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    work = tmp_path / "run"
    root = tmp_path / "os-root.raw"
    root.write_bytes(b"raw os volume, not a UDIF container")
    cryptex = tmp_path / "044-76034-038.dmg"
    cryptex.write_bytes(b"NXSB" + b"\0" * (1 << 20))

    report = ub.boot(
        inputs, work, qemu=qemu, device_dir=_synthetic_device(tmp_path),
        root_image=root, systemos_image=cryptex,
    )

    assert report.error_code is ErrorCode.QEMU_LAUNCH_FAILED, report
    assert report.systemos_image == "044-76034-038.dmg", report.systemos_image
    assert str(tmp_path) not in report.systemos_image, report.systemos_image
    written = json.loads((work / "boot-report.json").read_text(encoding="utf-8"))
    assert written["systemos_image"] == "044-76034-038.dmg", written

    command = launched[0]
    assert command[-4:] == [
        "-drive",
        f"if=none,id=systemos,format=raw,readonly=on,file={cryptex.resolve().as_posix()}",
        "-device",
        "virtio-blk-device,drive=systemos",
    ], command[-4:]
    assert "virtio-blk-pci,drive=systemos" not in command, command
    # ...and ``command.txt`` in the run directory is the same command line.
    assert (work / "command.txt").read_text(encoding="utf-8").split()[-1] == (
        "virtio-blk-device,drive=systemos"
    )


def test_a_boot_without_a_systemos_cryptex_says_so(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unattached run is labelled too, and its argv has no third disk.

    An attached run and an unattached one both reach the kernel and both print
    ``XNU:`` lines, so the serial log cannot separate them; the report has to, and
    ``None`` has to be what an unattached run carries rather than an absent key.
    """
    import json

    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    work = tmp_path / "run"

    report = ub.boot(inputs, work, qemu=qemu, device_dir=_synthetic_device(tmp_path))

    assert report.systemos_image is None, report.systemos_image
    written = json.loads((work / "boot-report.json").read_text(encoding="utf-8"))
    assert written["systemos_image"] is None, written
    assert not any("systemos" in item for item in _scrubbed(launched[0], tmp_path)), launched[0]
    assert "virtio-blk-device,drive=systemos" not in launched[0], launched[0]


def test_the_cli_prints_whether_a_cryptex_went_with_the_run(
    inputs: BootInputs, qemu: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """``main`` says out loud which of the two machines the operator just booted.

    Both branches, and the exact line rather than the phrase for the same reason
    the root volume's test uses it: "no SystemOS cryptex attached" does not
    contain "SystemOS cryptex attached" as a substring, but asserting the phrase
    would pass on either branch and the whole point is that they are different.
    """
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited([]))
    cryptex = tmp_path / "044-76034-038.dmg"
    cryptex.write_bytes(b"NXSB" + b"\0" * (1 << 20))
    base = [
        "--device", str(_synthetic_device(tmp_path)),
        "--qemu", str(qemu),
        "--firmware", str(inputs.firmware),
        "--kernel", str(inputs.kernel),
        "--devicetree", str(inputs.devicetree),
        "--darwinboot", str(inputs.darwinboot),
        "--unixnu", str(inputs.unixnu),
        "--workdir", str(tmp_path / "run"),
    ]

    assert ub.main([*base, "--systemos-image", str(cryptex)]) == 1
    attached = capsys.readouterr().out
    assert "SystemOS cryptex attached: 044-76034-038.dmg" in attached, attached
    assert str(tmp_path) not in attached, attached

    assert ub.main(base) == 1
    unattached = capsys.readouterr().out
    assert "no SystemOS cryptex attached" in unattached, unattached
    assert "SystemOS cryptex attached: 044-76034-038.dmg" not in unattached, unattached


# ---------------------------------------------------------------------------
# the helpers the run leans on
# ---------------------------------------------------------------------------
def test_the_png_writer_emits_a_real_png(tmp_path: Path) -> None:
    """A dependency-free PPM-to-PNG conversion, checked by decoding it back.

    The controller must run on a bare Windows host, so the conversion is done
    with :mod:`zlib` and :mod:`struct` alone.  What is asserted is the one thing
    that matters for evidence: the result parses as a PNG with the right
    geometry and the right pixel.
    """
    import zlib

    width, height = 2, 1
    pixels = bytes([0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF])
    ppm = tmp_path / "screen.ppm"
    ppm.write_bytes(b"P6\n%d %d\n255\n" % (width, height) + pixels)
    target = tmp_path / "screen.png"
    ub._write_png(ppm, target)
    blob = target.read_bytes()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", blob[:8]
    # IHDR: length, tag, width, height, bit depth, colour type, then the CRC.
    assert blob[12:16] == b"IHDR", blob[12:16]
    assert struct.unpack(">II", blob[16:24]) == (width, height), blob[16:24]
    assert blob[24] == 8 and blob[25] == 2, (blob[24], blob[25])
    assert blob[-8:-4] == b"IEND", blob[-8:-4]
    # ...and the IDAT really deflates the rows, each with its leading filter byte.
    start = blob.index(b"IDAT") + 4
    length = struct.unpack(">I", blob[start - 8 : start - 4])[0]
    rows = zlib.decompress(blob[start : start + length])
    assert rows == b"\0" + pixels, rows


def test_the_serial_markers_are_reported_never_asserted(tmp_path: Path) -> None:
    """``_serial_markers`` summarises; it does not decide whether a boot worked."""
    log = tmp_path / "serial.log"
    log.write_text(
        "noise\nUNIXNU: loading kernel\nDARWINBOOT: hello\npanic: nothing\n",
        encoding="utf-8",
    )
    markers = ub._serial_markers(log)
    assert len(markers) == 3, markers
    assert markers[0].startswith("UNIXNU:"), markers
    assert ub._serial_markers(tmp_path / "absent.log") == []


def test_the_tail_is_bounded(tmp_path: Path) -> None:
    """A 6.8 GB-class log cannot be read into an error detail whole."""
    path = tmp_path / "big.log"
    path.write_bytes(b"x" * 5000)
    tail = ub._tail(path, limit=100)
    assert len(tail) <= len("...(truncated)...\n") + 100, len(tail)
    assert ub._tail(tmp_path / "absent.log") == ""


def test_the_qmp_round_trip_speaks_the_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Greeting first, then ``qmp_capabilities``, then the command.

    A fresh connection per command is the point -- the screendump loop is the
    only other user, and a shared connection would serialise the two.  What is
    asserted is the shape of the exchange, against a socket that answers exactly
    as QEMU does.

    This is the承重 test for the ``return``/``break`` distinction in
    ``_qmp``: an earlier spelling returned from *inside* the request loop, so it
    answered after ``qmp_capabilities`` and never sent the command it was given.
    Every screendump would then have been a no-op and ``qmp_evidence`` would have
    recorded a reply to a question nobody asked -- both of which read as a
    successful run.
    """
    seen: list[dict] = []

    class FakeStream:
        def readline(self) -> bytes:
            if not seen:
                return b'{"QMP": {"version": {"qemu": {"micro": 1, "minor": 1, "major": 11}}}}\n'
            return b'{"return": {}}\n'

        def write(self, data: bytes) -> None:
            import json

            seen.append(json.loads(data))

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def settimeout(self, value: float) -> None:
            return None

        def makefile(self, *args: object, **kwargs: object) -> FakeStream:
            return FakeStream()

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: FakeSocket())
    reply = ub._qmp(ub.DEFAULT_QMP_PORT, "query-status")
    assert reply == {"return": {}}, reply
    assert [item["execute"] for item in seen] == [
        "qmp_capabilities",
        "query-status",
    ], seen


def test_the_cli_requires_every_path() -> None:
    """A default would create a run directory nobody cleans."""
    import pytest as _pytest

    with _pytest.raises(SystemExit) as caught:
        ub.main(["--device", "x", "--qemu", "y"])
    assert caught.value.code == 2, caught.value


# ---------------------------------------------------------------------------
# the trust cache
# ---------------------------------------------------------------------------
#: The name the payload step gives it, and the only place it is spelled.
TRUSTCACHE_NAME = "EFI/UniXNU/Payloads/kernel.macho.trustcache"


def test_a_trust_cache_is_staged_at_the_name_the_payload_uses(
    tmp_path: Path, inputs: BootInputs, real_spec: dict[str, Any]
) -> None:
    """The sixth file, and why it is not always there.

    ``unixnu/payload.py:143-144`` copies the trust cache to
    ``kernel.macho.trustcache`` beside the kernel, and ``build-os-root.py:26-27``
    records what its absence costs: AMFI refuses to run launchd (namespace 9,
    EXEC, code 8, SECURITY_POLICY).  A boot input set that has one must stage it
    under that exact name, because that is the name the loader looks for.
    """
    bundle = tmp_path / "original-static-system.bundle"
    bundle.write_bytes(b"synthetic trust bundle")
    staged = stage_esp(
        replace(inputs, trust_cache=bundle), tmp_path / "esp", real_spec
    )
    assert staged[TRUSTCACHE_NAME] == bundle.stat().st_size, staged
    assert (tmp_path / "esp" / TRUSTCACHE_NAME).read_bytes() == b"synthetic trust bundle"


def test_a_boot_without_a_trust_cache_stages_the_same_five_files(
    tmp_path: Path, inputs: BootInputs, real_spec: dict[str, Any]
) -> None:
    """The default is unchanged, and the record says which kind of run it was.

    The key is absent rather than present-with-zero, so a caller that does not
    use a trust cache sees exactly the record it saw before the field existed --
    and so the record itself answers "did this boot carry one", which is the
    question that decides whether the guest can exec launchd at all.
    """
    staged = stage_esp(inputs, tmp_path / "esp", real_spec)
    assert TRUSTCACHE_NAME not in staged, staged
    assert not (tmp_path / "esp" / TRUSTCACHE_NAME).exists()
    assert sorted(key for key in staged if key.startswith("EFI/")) == sorted(ESP_FILES), staged


def test_a_trust_cache_that_does_not_exist_is_named_by_missing(
    tmp_path: Path, inputs: BootInputs
) -> None:
    """A declared input that is absent is a refusal, not an empty staging.

    ``missing()`` is what ``boot`` checks before it starts QEMU.  A trust cache
    that was asked for and is not on disk has to land there: silently staging
    five files would produce a boot that looks complete and cannot start a
    userland, which is the exact shape of the defect this field exists to close.
    """
    absent = tmp_path / "not-there.bundle"
    # ``safe_location`` names only the final component, by design (PRD 9.2: an
    # error detail must not carry the drive, the profile or the username), so the
    # assertion is on the name and not on the whole path.
    assert replace(inputs, trust_cache=absent).missing() == [
        "trust_cache=not-there.bundle"
    ], replace(inputs, trust_cache=absent).missing()


# ---------------------------------------------------------------------------
# the frame-buffer driver, and the check that could never pass
# ---------------------------------------------------------------------------
#: Where ``build_argv`` looks for the driver, and where ``stage_esp`` puts it.
RAMFB_NAME = "EFI/UniXNU/Payloads/QemuRamfb.efi"


def test_the_frame_buffer_driver_is_staged_at_the_path_build_argv_tests(
    tmp_path: Path, inputs: BootInputs, real_spec: dict[str, Any]
) -> None:
    """The driver a caller supplies has to land where the geometry check looks.

    This test is the whole reason the field exists.  ``build_argv`` emits
    ``-fw_cfg name=opt/org.unixnu/ramfb-size`` only when ``RAMFB_NAME`` is on the
    ESP, and ``boot`` decided :data:`SCREEN` the same way -- but nothing in this
    module could place the file, so on the official path the test was always
    false.  A check for something no caller can supply is not a check; it is a
    branch that is dead, and the 800x600 framebuffer measured 2026-10-27 in a
    run whose Data volume had mounted is what that branch cost.
    """
    driver = tmp_path / "QemuRamfb.efi"
    driver.write_bytes(b"synthetic frame-buffer driver")
    staged = stage_esp(replace(inputs, ramfb_driver=driver), tmp_path / "esp", real_spec)
    assert staged[RAMFB_NAME] == driver.stat().st_size, staged
    assert (tmp_path / "esp" / RAMFB_NAME).read_bytes() == b"synthetic frame-buffer driver"


def test_a_boot_without_the_frame_buffer_driver_stages_exactly_the_old_files(
    tmp_path: Path, inputs: BootInputs, real_spec: dict[str, Any]
) -> None:
    """The default is byte-for-byte what it was before the field existed.

    The key is absent rather than present-with-zero, so a caller that does not
    carry a driver sees the same record it always saw -- and so the record
    answers "did this boot apply the geometry", which is the question a report
    that says the framebuffer was 800x600 cannot otherwise answer.
    """
    staged = stage_esp(inputs, tmp_path / "esp", real_spec)
    assert RAMFB_NAME not in staged, staged
    assert not (tmp_path / "esp" / RAMFB_NAME).exists()
    assert sorted(key for key in staged if key.startswith("EFI/")) == sorted(ESP_FILES), staged


def test_the_geometry_is_emitted_only_when_the_driver_is_on_the_esp(
    tmp_path: Path, inputs: BootInputs, real_spec: dict[str, Any]
) -> None:
    """``ramfb_size`` follows the staged file, not the caller's argument.

    Measured consequence of the old behaviour: the item was never emitted, so
    nothing applied :data:`SCREEN`, and QEMU's ``ramfb`` kept its own default --
    800x600 rather than 1376x1024, in a framebuffer that then never changed.
    """
    without = stage_esp(inputs, tmp_path / "esp-a", real_spec)
    argv = ub.build_argv(Path("qemu"), inputs, tmp_path / "esp-a", tmp_path,
                         ramfb_size=ub.SCREEN if (tmp_path / "esp-a" / RAMFB_NAME).is_file() else None)
    assert not any("ramfb-size" in item for item in argv), argv
    assert RAMFB_NAME not in without, without

    driver = tmp_path / "QemuRamfb.efi"
    driver.write_bytes(b"synthetic frame-buffer driver")
    stage_esp(replace(inputs, ramfb_driver=driver), tmp_path / "esp-b", real_spec)
    argv = ub.build_argv(Path("qemu"), inputs, tmp_path / "esp-b", tmp_path,
                         ramfb_size=ub.SCREEN if (tmp_path / "esp-b" / RAMFB_NAME).is_file() else None)
    assert [item for item in argv if "ramfb-size" in item] == [
        f"name=opt/org.unixnu/ramfb-size,string={ub.SCREEN}"
    ], argv


def test_a_frame_buffer_driver_that_does_not_exist_is_named_by_missing(
    tmp_path: Path, inputs: BootInputs
) -> None:
    """A driver that was asked for and is not on disk is a refusal.

    ``missing()`` is what ``boot`` checks before it starts QEMU.  A driver that
    silently vanished would stage nothing, emit no geometry, and produce a
    800x600 boot that reports success -- the failure this field exists to make
    impossible, and the one the ``--ramfb-driver`` flag now surfaces.
    """
    absent = tmp_path / "not-there.efi"
    # ``safe_location`` names only the final component, by design (PRD 9.2).
    assert replace(inputs, ramfb_driver=absent).missing() == [
        "ramfb_driver=not-there.efi"
    ], replace(inputs, ramfb_driver=absent).missing()


def test_the_frame_buffer_driver_appears_in_the_inputs_record_only_when_supplied(
    tmp_path: Path, inputs: BootInputs
) -> None:
    """``to_dict`` omits the key, so the record says which kind of run it was.

    A report that named the driver unconditionally could not tell a run that
    applied the geometry from one that did not, and that difference is invisible
    in the serial log -- both boots mount the Data volume and both take
    screendumps.
    """
    assert "ramfb_driver" not in inputs.to_dict(), inputs.to_dict()
    driver = tmp_path / "QemuRamfb.efi"
    driver.write_bytes(b"synthetic frame-buffer driver")
    assert replace(inputs, ramfb_driver=driver).to_dict()["ramfb_driver"] == "QemuRamfb.efi"


def test_the_geometry_is_defined_in_exactly_one_place(tmp_path: Path) -> None:
    """``SCREEN`` is the only definition, and the driver only receives it.

    ``stage_esp`` used to refuse to stage the driver on the grounds that a second
    copy would be a second definition of the geometry.  The reasoning was wrong:
    the driver carries no geometry of its own, it reads the fw_cfg item this
    module writes.  What was actually missing was a way to say *which* driver.
    Asserted here because the alternative -- two places to change when the
    geometry changes -- is the defect the refusal was trying to prevent.

    The count is on the *quoted* literal, not the bare path: comments are
    allowed to name the file, code is not.  A second quoted spelling is the
    mutation where ``stage_esp`` writes one path and ``build_argv`` tests
    another, which stages a driver nothing runs and reports a geometry nothing
    applied -- and which passes every behavioural test above, because both
    strings still hold the same value.
    """
    source = Path(ub.__file__).read_text(encoding="utf-8")
    assert source.count(f'"{RAMFB_NAME}"') == 1, (
        f"the payload path is spelled {source.count(chr(34) + RAMFB_NAME + chr(34))} "
        f"times in code; staging and the geometry check must share one constant"
    )
    assert source.count('SCREEN = "1376x1024"') == 1, source


def test_boot_emits_the_geometry_only_when_the_driver_was_staged(
    tmp_path: Path, inputs: BootInputs, qemu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``boot``'s decision, not just ``build_argv``'s parameter.

    The mutation this closes is ``ramfb_size=SCREEN`` unconditionally in
    :func:`boot` -- a geometry nothing set, emitted anyway.  Every test above
    passes ``ramfb_size`` to ``build_argv`` by hand and so never exercises the
    branch that decides it, which is how that mutation survived three rounds of
    checking before this one was written.  What it costs is a run whose argv
    claims 1376x1024 while the framebuffer is 800x600, and the comment at the
    call site already says why that is not acceptable.
    """
    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    driver = tmp_path / "QemuRamfb.efi"
    driver.write_bytes(b"synthetic frame-buffer driver")

    report = ub.boot(
        replace(inputs, ramfb_driver=driver), tmp_path / "run",
        qemu=qemu, device_dir=_synthetic_device(tmp_path),
    )
    command = launched[0]
    assert f"name=opt/org.unixnu/ramfb-size,string={ub.SCREEN}" in command, command
    # The driver really was on the ESP the guest was offered, which is the other
    # half of the claim: an fw_cfg item nothing reads is as dead as a driver
    # nothing stages.  ``to_dict`` stores the value; the ``role=`` form is only
    # what ``missing()`` produces.
    assert report.inputs.get("ramfb_driver") == "QemuRamfb.efi", report.inputs


def test_boot_emits_no_geometry_when_no_driver_is_staged(
    tmp_path: Path, inputs: BootInputs, qemu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured 800x600 case, pinned so it cannot come back quietly.

    Without the driver there is nothing to apply :data:`SCREEN`, so the item is
    not emitted and QEMU's ``ramfb`` keeps its own default.  That is the run
    whose 13 screendumps were byte-identical at 800x600 -- and the reason this
    is asserted rather than left as "the caller's problem" is that the failure
    is invisible: the boot succeeds, the Data volume mounts, and the only
    symptom is a framebuffer that is the wrong size and never changes.
    """
    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))

    report = ub.boot(
        inputs, tmp_path / "run", qemu=qemu, device_dir=_synthetic_device(tmp_path)
    )
    command = launched[0]
    assert not any("ramfb-size" in item for item in command), command
    assert "ramfb_driver" not in report.inputs, report.inputs


def test_the_generated_config_loads_the_driver_before_darwinboot(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """The ``[[driver]]`` line, and why it comes first.

    Staging the file is only half of it.  ``QemuRamfbPkg/QemuRamfb.c:1-7`` says
    the driver exists to size ramfb "before DarwinBoot takes the frame buffer for
    the kernel", so a manifest that lists it *after* DarwinBoot would configure a
    framebuffer that has already been claimed.  The order is asserted, not
    implied: it is the one property of this line a reader of the generated file
    cannot check by eye.

    This is the half that was missing when the measurement was taken -- the file
    was staged, the fw_cfg item was emitted, and the framebuffer stayed at
    800x600 because the manifest named only DarwinBoot.
    """
    text = ub.unixnu_toml(real_spec, real_line, ramfb=True)
    drivers = [line.split('"')[1] for line in text.splitlines()
               if line.strip().startswith("path = ")]
    assert drivers == [
        RAMFB_NAME.removeprefix("EFI/UniXNU/"), "Payloads/DarwinBoot.efi",
        "Payloads/kernel.macho",
    ], drivers


def test_the_generated_config_is_unchanged_when_there_is_no_driver(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """The default config is byte-identical to what it was before this field.

    ``ramfb=False`` has to be a no-op, because this function writes the config
    for every boot that does not supply its own -- and a config that changed for
    every such boot would be a regression in the common case.
    """
    assert ub.unixnu_toml(real_spec, real_line) == ub.unixnu_toml(
        real_spec, real_line, ramfb=False
    )


def test_a_staged_driver_the_config_does_not_load_is_a_refusal(
    tmp_path: Path, inputs: BootInputs, real_spec: dict[str, Any]
) -> None:
    """A driver nothing will open is a refusal, not a silently inert file.

    This is the whole finding of the 24-minute measurement, turned into a guard.
    The delivery's ``unixnu.toml`` names exactly one driver
    (``Payloads/DarwinBoot.efi``), and UniXNU loads what the manifest names --
    the firmware does not enumerate ``Payloads/``.  So a run that staged the
    driver while adopting that config got a boot that looked fine and a
    framebuffer that was never resized: 29 screendumps, all 800x600, all the
    same sha256.

    Refusing here costs a caller one exception instead of a boot that reports
    success while ignoring what they asked for.  The real fix is a payload build
    whose manifest names the driver, which is a build change and not something
    this module should fake by editing a config that must come from the matched
    build.
    """
    driver = tmp_path / "QemuRamfb.efi"
    driver.write_bytes(b"synthetic frame-buffer driver")
    adopted = tmp_path / "unixnu.toml"
    adopted.write_text(
        'version = 1\n[boot]\ntimeout = 3\ndefault = "xnu"\n[kits]\nautoload = false\n'
        '[[driver]]\npath = "Payloads/DarwinBoot.efi"\n\n[[entry]]\nid = "xnu"\n'
        'type = "xnu"\npath = "Payloads/kernel.macho"\n',
        encoding="utf-8",
    )
    caught = _refused(
        stage_esp,
        replace(inputs, ramfb_driver=driver, payload_toml=adopted),
        tmp_path / "esp", real_spec,
    )
    # ``_refused`` returns the exception itself, so ``caught`` *is* the
    # ``UniPadError`` -- writing ``caught.value`` here raises ``AttributeError``
    # and the test fails on its own bookkeeping instead of on the contract.
    #
    # The assertion is on the message and not only on the code, because
    # ``config_invalid`` is raised by four different checks in ``stage_esp``:
    # the fixture's synthetic tree carries a packed-ramdisk placeholder, so
    # :func:`_check_ramdisk_pair` can refuse this same call for a reason that
    # has nothing to do with the driver.  Asserting only the code made the
    # "drop the guard" mutation survive a full round of checking -- the test
    # was green on the *other* refusal.
    assert caught.code == ErrorCode.CONFIG_INVALID, caught
    assert "does not load it" in caught.message, caught.message
    # The file was still written: the refusal is about the config, and a caller
    # reading the ESP afterwards should see what was staged and be able to tell
    # the two problems apart.
    assert (tmp_path / "esp" / RAMFB_NAME).is_file()


def test_a_driver_the_generated_config_loads_is_not_a_refusal(
    tmp_path: Path, inputs: BootInputs, real_spec: dict[str, Any]
) -> None:
    """The other side of the same guard: no ``payload_toml`` means we write one.

    When the caller does not adopt a config, :func:`stage_esp` generates it, and
    the generated one names the driver -- so the guard must not fire.  Asserted
    because a guard that refuses the case it was built to permit is worse than
    no guard: it would make the field unusable on the only path where it can
    work.
    """
    driver = tmp_path / "QemuRamfb.efi"
    driver.write_bytes(b"synthetic frame-buffer driver")
    written = stage_esp(replace(inputs, ramfb_driver=driver, payload_toml=None),
                        tmp_path / "esp", real_spec)
    config = (tmp_path / "esp" / "EFI/UniXNU/unixnu.toml").read_text(encoding="utf-8")
    assert f'path = "{RAMFB_NAME.removeprefix("EFI/UniXNU/")}"' in config, config
    assert written[RAMFB_NAME] == driver.stat().st_size, written


# ---------------------------------------------------------------------------
# the packed Data ramdisk, and the pair DarwinBoot insists on
# ---------------------------------------------------------------------------
def test_the_toml_names_the_initrd_when_a_data_ramdisk_is_supplied(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """``initrd`` is what turns a file on the ESP into ``Request->Ramdisk``.

    ``Config.c:1189`` reads the entry's ``initrd`` key, ``Boot.c:147`` reads that
    file off the ESP, and ``Boot.c:165`` hands it to ``RunXnu``.  Without the
    line the file is staged and never read, so the line goes in the place
    upstream writes it (``run-qemu.py:141``): immediately before ``args``.
    """
    text = ub.unixnu_toml(real_spec, real_line, initrd=f"Payloads/{ub.DATA_RAMDISK_NAME}")
    assert f'initrd = "Payloads/{ub.DATA_RAMDISK_NAME}"\nargs = "{real_line}"' in text, text
    entry = tomllib.loads(text)["entry"][0]
    assert entry["initrd"] == f"Payloads/{ub.DATA_RAMDISK_NAME}", entry
    assert entry["args"] == real_line, entry


def test_no_initrd_line_is_written_when_there_is_no_data_ramdisk(
    real_spec: dict[str, Any], real_line: str
) -> None:
    """The absent case has to stay byte-identical to what it always was.

    Every boot without a Data seed is the state this module has produced since it
    was written, and an empty ``initrd`` key would change the payload for no
    reason -- ``Config.c`` would read it as a path.
    """
    assert ub.unixnu_toml(real_spec, real_line) == ub.unixnu_toml(
        real_spec, real_line, initrd=None
    )
    assert "initrd" not in tomllib.loads(ub.unixnu_toml(real_spec, real_line))["entry"][0]


def test_the_esp_carries_the_data_ramdisk_beside_the_kernel(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """Staged at the one name the config can spell, and validated on the way in."""
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))

    esp = tmp_path / "esp"
    written = ub.stage_esp(
        replace(inputs, devicetree=tree, data_ramdisk=seed), esp, real_spec
    )
    staged = esp / "EFI/UniXNU/Payloads" / ub.DATA_RAMDISK_NAME
    assert staged.is_file(), "the seed was validated but not staged"
    assert staged.read_bytes() == seed.read_bytes()
    assert written[f"EFI/UniXNU/Payloads/{ub.DATA_RAMDISK_NAME}"] == seed.stat().st_size
    assert f'initrd = "Payloads/{ub.DATA_RAMDISK_NAME}"' in (
        esp / "EFI/UniXNU/unixnu.toml"
    ).read_text(encoding="utf-8")


def test_a_data_ramdisk_without_the_trees_placeholder_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """Half the pair is a boot that never reaches the kernel.

    DarwinBoot's gate (``DarwinBoot.c:563-569``) refuses ``Request->Ramdisk !=
    NULL`` with ``EFI_COMPROMISED_DATA`` when the tree carries no
    ``RamdiskOffset``, and it does so *before* the hand-off banner -- so the
    serial log reads as a firmware problem and not as a missing property.
    """
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    error = _refused(
        ub.stage_esp,
        replace(inputs, data_ramdisk=seed),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "accepts both or neither" in error.message, error
    assert error.details["data_ramdisk"] is True, error.details
    assert error.details["tree_placeholder"] is None, error.details


# ---------------------------------------------------------------------------
# the GPU addon: the third half of the same hand-off
# ---------------------------------------------------------------------------
def test_the_esp_carries_the_gpu_addon_beside_the_kernel(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """Staged at the one name the firmware builds by concatenation.

    ``DarwinBoot.c:587-589`` copies ``Request->KernelPath`` and appends
    ``.gpuaddon``, so the name is a lookup exactly like the trust cache's -- and
    it is also what the payload build writes (``unixnu/payload.py:145``).  Two
    spellings would stage a file nothing opens.
    """
    addon = tmp_path / "gpu-addon.ngpu"
    addon.write_bytes(_synthetic_addon())
    tree = tmp_path / "with-addon.adt"
    tree.write_bytes(_synthetic_tree(addon=bytes(16)))

    esp = tmp_path / "esp"
    written = ub.stage_esp(
        replace(inputs, devicetree=tree, gpu_addon=addon), esp, real_spec
    )
    staged = esp / "EFI/UniXNU/Payloads" / Path(ub.GPU_ADDON_NAME).name
    assert staged.is_file(), "the addon was validated but not staged"
    assert staged.read_bytes() == addon.read_bytes()
    assert written[ub.GPU_ADDON_NAME] == addon.stat().st_size
    # The name is the one the firmware derives, not one this module prefers.
    assert ub.GPU_ADDON_NAME == "EFI/UniXNU/Payloads/" + "kernel.macho" + ".gpuaddon"


def test_a_tree_placeholder_without_the_addon_blob_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """THE MEASURED DEFECT, and the reason this field exists.

    ``DarwinBoot.c:596-598`` refuses with ``EFI_COMPROMISED_DATA`` when the tree
    carries a ``GpuAddon`` placeholder and ``ReadFile`` finds no
    ``kernel.macho.gpuaddon`` -- and it refuses *before* the hand-off banner, so
    the serial log names compromised data and not a missing file.

    Measured on 2026-10-30 with bundle8, the first build that carried the blob:
    the boot never reached the kernel at all, and the log ends
    ``DARWINBOOT: boot refused: Compromised Data; firmware retained``.  The
    blob was in the bundle the whole time (``payload.img`` carries
    ``KERNEL~1.GPU`` at 12,131,336 bytes); only the staging path was absent, so
    ``BootInputs`` had no field to carry it with.
    """
    tree = tmp_path / "with-addon.adt"
    tree.write_bytes(_synthetic_tree(addon=bytes(16)))
    error = _refused(
        ub.stage_esp, replace(inputs, devicetree=tree), tmp_path / "esp", real_spec
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "accepts both or neither" in error.message, error
    assert error.details["addon"] is False, error.details
    assert error.details["tree_placeholder"] is True, error.details


def test_an_addon_blob_without_the_trees_placeholder_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The other branch of the same exclusive-or.

    A staged blob with no placeholder is the mirror image: the firmware would
    patch nothing and the kernel would have no window to find it in.
    """
    addon = tmp_path / "gpu-addon.ngpu"
    addon.write_bytes(_synthetic_addon())
    error = _refused(
        ub.stage_esp, replace(inputs, gpu_addon=addon), tmp_path / "esp", real_spec
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "accepts both or neither" in error.message, error
    assert error.details["addon"] is True, error.details
    assert error.details["tree_placeholder"] is False, error.details


def test_both_addon_halves_together_are_accepted(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The state that actually boots, so the guard is not a guard against everything."""
    addon = tmp_path / "gpu-addon.ngpu"
    addon.write_bytes(_synthetic_addon())
    tree = tmp_path / "with-addon.adt"
    tree.write_bytes(_synthetic_tree(addon=bytes(16)))

    esp = tmp_path / "esp"
    written = ub.stage_esp(
        replace(inputs, devicetree=tree, gpu_addon=addon), esp, real_spec
    )
    assert ub.GPU_ADDON_NAME in written, written


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"magic": b"NGPUADD1"}, "NGPUADD2"),
        ({"size": 16}, "at least 24"),
        ({"declared": 9999}, "disagrees with itself"),
    ],
)
def test_an_addon_whose_header_darwinboot_rejects_is_refused_here_instead(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path,
    kwargs: dict[str, Any], expected: str,
) -> None:
    """The firmware's three header checks, applied a second earlier.

    ``DarwinBoot.c:593-595`` refuses a staged addon unless the magic is
    ``NGPUADD2``, the size is at least 24, and the u64 at offset 16 equals the
    file's own length.  Its message for a failure is ``EFI_COMPROMISED_DATA``,
    which names a corrupted device tree -- so the same three checks run here,
    where the message can name the file.
    """
    addon = tmp_path / "gpu-addon.ngpu"
    addon.write_bytes(_synthetic_addon(**kwargs))
    tree = tmp_path / "with-addon.adt"
    tree.write_bytes(_synthetic_tree(addon=bytes(16)))

    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, gpu_addon=addon),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert expected in error.message, error
    assert not (tmp_path / "esp" / "EFI/UniXNU/Payloads" / Path(ub.GPU_ADDON_NAME).name).exists(), (
        "a blob the firmware would refuse was staged anyway"
    )


def test_a_nonzero_addon_placeholder_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The placeholder has to be zeroed, exactly as the ramdisk's does.

    DarwinBoot patches the 16 bytes with the blob's address and size, and treats
    a nonzero value as compromised data (``DarwinBoot.c:563`` for the ramdisk,
    and the same reasoning at :593 for the addon).  A tree carrying a nonzero
    ``GpuAddon`` is therefore a tree this module refuses rather than boots.
    """
    tree = tmp_path / "nonzero-addon.adt"
    tree.write_bytes(_synthetic_tree(addon=bytes(range(16))))
    error = _refused(
        ub.stage_esp, replace(inputs, devicetree=tree), tmp_path / "esp", real_spec
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "not zeroed" in error.message, error


def test_an_addon_placeholder_of_the_wrong_length_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """``AdtNode`` requires exactly 16 bytes and refuses the tree otherwise."""
    tree = tmp_path / "short-addon.adt"
    tree.write_bytes(_synthetic_tree(addon=bytes(8)))
    error = _refused(
        ub.stage_esp, replace(inputs, devicetree=tree), tmp_path / "esp", real_spec
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "exactly 16" in error.message, error


def test_the_addon_is_recorded_in_the_boot_report(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """``to_dict`` carries it, so a report says which blob a run used.

    The ramdisk and the trust cache are both recorded for this reason: the
    question "did this boot carry an addon" decides whether the GPU addon mount
    can be entered at all, and a report that cannot answer it is not
    diagnosable.
    """
    addon = tmp_path / "gpu-addon.ngpu"
    addon.write_bytes(_synthetic_addon())
    record = replace(inputs, gpu_addon=addon).to_dict()
    assert record["gpu_addon"] == ub.safe_location(addon), record
    assert "gpu_addon" not in inputs.to_dict(), (
        "a boot with no addon must not claim one"
    )


def test_a_tree_placeholder_without_a_data_ramdisk_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The other half, from the other branch of the same ``if``.

    A tree carrying the placeholder with no ``initrd`` is refused with
    ``EFI_UNSUPPORTED``, which is just as invisible in the serial log.
    """
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    error = _refused(
        ub.stage_esp, replace(inputs, devicetree=tree), tmp_path / "esp", real_spec
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "accepts both or neither" in error.message, error
    assert error.details["data_ramdisk"] is False, error.details
    assert error.details["tree_placeholder"] == "PackedRAMDisk", error.details


def test_both_halves_together_are_accepted(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The state that actually boots, so the guard is not a guard against everything."""
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    written = ub.stage_esp(
        replace(inputs, devicetree=tree, data_ramdisk=seed), tmp_path / "esp", real_spec
    )
    assert f"EFI/UniXNU/Payloads/{ub.DATA_RAMDISK_NAME}" in written, written


def test_a_data_ramdisk_that_is_not_the_packed_format_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """A seed the bootloader or the kernel driver would reject is refused here.

    Both parse the same format (``PackedRamdisk.h`` and ``SparseDisk.h``), and
    neither failure is visible until the guest has booted -- where it surfaces as
    a Data volume that does not exist, which reads as a kernel bug.
    """
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(b"this is not a packed ramdisk, it is just prose" + b"." * 16)
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.ARTIFACT_MANIFEST_INVALID, error
    assert "magic" in error.message, error


def test_a_data_ramdisk_shorter_than_its_header_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The header itself is 32 bytes, so nothing smaller is a seed at all."""
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(b"DBRDISK1")
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.ARTIFACT_MANIFEST_INVALID, error
    assert "shorter than the 32-byte header" in error.message, error


def test_a_data_ramdisk_with_trailing_bytes_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """Both parsers end on ``cursor != size``, so extra bytes are a refusal too.

    Truncating a seed does not reach that check -- the extent that runs off the
    end is caught first, which is the more useful message.  What reaches it is a
    file with bytes past the last extent, which is what a half-written packer
    leaves behind.
    """
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed() + b"\0" * 4)
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.ARTIFACT_MANIFEST_INVALID, error
    assert "account for" in error.message, error


def test_a_truncated_data_ramdisk_names_the_extent_that_runs_off_the_end(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """A truncated file fails on the extent, and the message says which one."""
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed()[:-512])
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.ARTIFACT_MANIFEST_INVALID, error
    assert "extent 0 needs 4096 bytes but only 3584 remain" in error.message, error


def test_a_tree_with_both_ramdisk_properties_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """``AdtNode`` refuses a tree that carries both, so this module does too."""
    tree = tmp_path / "both.adt"
    tree.write_bytes(_synthetic_tree(both=True))
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "both PackedRAMDisk and RAMDisk" in error.message, error


def test_a_placeholder_that_is_not_zeroed_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """DarwinBoot treats a nonzero placeholder as compromised data.

    The 16 bytes are where it writes the ramdisk's address and size; a value
    already there means something else has claimed them.
    """
    tree = tmp_path / "dirty.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=struct.pack("<QQ", 1, 2)))
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "not zeroed" in error.message, error


def test_a_placeholder_that_is_not_16_bytes_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """``AdtNode`` requires exactly 16 and refuses the tree otherwise."""
    tree = tmp_path / "short.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(8)))
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "exactly 16" in error.message, error


def test_the_placeholder_is_reported_by_name_and_either_spelling_counts(
    tmp_path: Path,
) -> None:
    """``AdtNode`` accepts either spelling, so the reader names which one it saw."""
    packed = tmp_path / "packed.adt"
    packed.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    assert ub.tree_ramdisk_placeholder(packed) == "PackedRAMDisk"
    plain = tmp_path / "plain.adt"
    plain.write_bytes(_synthetic_tree(ramdisk=bytes(16), ramdisk_key="RAMDisk"))
    assert ub.tree_ramdisk_placeholder(plain) == "RAMDisk"
    absent = tmp_path / "absent.adt"
    absent.write_bytes(_synthetic_tree())
    assert ub.tree_ramdisk_placeholder(absent) is None


def test_a_tree_that_does_not_parse_is_refused_rather_than_read_as_absent(
    tmp_path: Path,
) -> None:
    """An unparseable tree is not a tree without a placeholder.

    Returning ``None`` for "could not read it" would let a corrupt payload pass
    the ramdisk check and fail later, in the guest, where nothing can say why.
    """
    broken = tmp_path / "broken.adt"
    broken.write_bytes(b"synthetic device tree")
    error = _refused(ub.tree_ramdisk_placeholder, broken)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "does not parse" in error.message, error


def test_a_tree_with_trailing_bytes_is_refused(tmp_path: Path) -> None:
    """``parse_adt`` returning before the end of the file is a truncated tree."""
    tree = tmp_path / "trailing.adt"
    tree.write_bytes(_synthetic_tree() + b"\0" * 8)
    error = _refused(ub.tree_ramdisk_placeholder, tree)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "trailing bytes" in error.message, error


def test_a_tree_that_is_absent_is_missing_not_invalid(tmp_path: Path) -> None:
    """``artifact_missing`` and ``config_invalid`` are different answers."""
    error = _refused(ub.tree_ramdisk_placeholder, tmp_path / "not-there.adt")
    assert error.code is ErrorCode.ARTIFACT_MISSING, error


def test_a_data_ramdisk_that_does_not_exist_is_named_by_missing(
    tmp_path: Path, inputs: BootInputs
) -> None:
    """The same contract ``missing()`` already keeps for the trust cache."""
    absent = tmp_path / "not-there.dbr"
    assert replace(inputs, data_ramdisk=absent).missing() == [
        "data_ramdisk=not-there.dbr"
    ], replace(inputs, data_ramdisk=absent).missing()


def test_the_data_ramdisk_is_staged_under_the_name_the_build_writes() -> None:
    """``DATA_RAMDISK_NAME`` is the payload build's spelling, not the host's.

    ``unixnu/scripts/unixnu/payload.py:148`` copies the data ramdisk to
    ``payloads / 'scratch-data.img'`` and :172 writes
    ``initrd = "Payloads/scratch-data.img"``.  This module stages the same file
    under its own constant and ``unixnu.toml``'s ``initrd`` has to name what was
    staged, so the constant has to be that spelling -- it is a *lookup*, exactly
    like ``TRUSTCACHE_NAME``, not a label this module gets to choose.

    Measured on 2026-10-27: the first verified delivery was refused by
    ``_check_ramdisk_pair`` with "unixnu.toml's initrd does not name the file
    that was staged".  The constant was ``owner-data-1g.dbr`` -- the seed's name
    on the host and in the delivery -- while the delivery's own ``unixnu.toml``,
    which must come from the matched build, named ``Payloads/scratch-data.img``.
    The refusal was right and the constant was wrong.

    Asserted against the vendored source rather than against a literal here, so
    a pin bump that renames it reddens this test instead of silently staging a
    file nothing opens.
    """
    payload_py = ROOT / "vendor" / "UniXNU" / "scripts" / "unixnu" / "payload.py"
    if not payload_py.is_file():  # pragma: no cover - the pin is always fetched
        pytest.skip(f"{payload_py} is not on this host")
    source = payload_py.read_text(encoding="utf-8")

    assert "payloads / 'scratch-data.img'" in source, (
        "the payload build no longer stages the data ramdisk as scratch-data.img; "
        "DATA_RAMDISK_NAME has to follow it"
    )
    assert ub.DATA_RAMDISK_NAME == "scratch-data.img", (
        f"the data ramdisk is staged as {ub.DATA_RAMDISK_NAME!r} but the payload "
        "build writes 'scratch-data.img', so unixnu.toml's initrd names a file "
        "that was never staged"
    )
    # And the two halves of the pair the guard checks are the same spelling.
    assert f"Payloads/{ub.DATA_RAMDISK_NAME}" in source, source

# ---------------------------------------------------------------------------
# the Data-mount race, and the retry that covers it
# ---------------------------------------------------------------------------
# Why this section exists
# ----------------------
# ``selectVolumeSeed`` (NativeRamBlock.cpp:368-417) issues **one**
# ``IOService::getMatchingServices`` for the Data volume's ``IOMedia`` and
# returns 0 if that single match does not come back; ``load_init_program``
# panics on the 0.  There is no retry, sleep or wait anywhere in it.
#
# Measured 2026-10-26: 7 of 8 boots of the matched build mounted the Data
# volume; the 8th panicked inside ``load_init_program`` with ``offline scratch
# Data mount failed``.  That is a 12.5% failure rate, and nothing in the boot
# report showed it -- QEMU does not exit when XNU panics, because the panic
# parks the CPU in a ``wfe; b wfe`` loop, so ``query-status`` keeps answering
# ``running`` and the screendumps keep succeeding while the guest is dead.
#
# So two things were added: a probe that reads the guest's kernel log over QMP
# and looks for the volume's own mount-complete line, and a retry that boots
# again in a fresh directory until that line appears.  The kernel-side fix --
# making ``load_init_program`` wait for the IOMedia instead of panicking on the
# first missed match -- is deliberately *not* here: it needs a rebuilt kernel,
# which is a different artifact from the one that was verified, so it belongs to
# the next material delivery and not to this one.
#
# What is proven here is the detector, because that is the part that can be
# wrong in a way nothing else catches: a detector that cannot tell a panicked
# guest from a slow one turns a 12.5% failure into a silent one, which is worse
# than the failure.


def _probe(fingerprint: str, *, marker: bool = False) -> dict[str, Any]:
    """A probe entry that read the log, in the shape ``_watch`` records."""
    return {"ready": True, "bytes": 1024, "fingerprint": fingerprint, "marker": marker}


class _AliveQEMU:
    """A QEMU that is still running whenever ``_watch`` looks.

    The counterpart of ``_ExitedQEMU``: that one exercises the path where the
    child is already gone, and this one exercises the screendump loop, which is
    where the guest-log probe was added.  ``stdout`` and ``stderr`` are ``None``
    for the reason ``_ExitedQEMU`` gives -- ``_watch`` is handed handles ``boot``
    opened and is still inside a ``with`` block for.
    """

    returncode = None
    stdout = None
    stderr = None

    def poll(self) -> Optional[int]:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _screendumping_qemu():
    """A ``_qmp`` that answers ``query-status`` and writes a real PPM.

    The PPM is written because ``_watch`` checks the file exists *and* converts
    it, so a fake that only replied would leave every round reporting a
    ``screendump_error`` -- and a test that passed with the probe never running
    would prove nothing about the probe.
    """
    written: list[Path] = []

    def run(port: int, command: str, **arguments: Any) -> dict[str, Any]:
        if command == "screendump":
            target = Path(arguments["filename"])
            target.write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
            written.append(target)
            return {"return": {}}
        return {"return": {"status": "running"}}

    run.written = written  # type: ignore[attr-defined]
    return run


def test_the_detector_tells_a_panicked_guest_from_a_slow_one() -> None:
    """The acceptance test for this task, in both directions.

    A panicked guest's log stops advancing; a slow guest's does not.  Every
    other observable is the same for the two -- QEMU alive, QMP answering,
    screendumps succeeding -- so if this test fails the retry is retrying blind
    and the 12.5% race is back with no evidence anywhere.
    """
    # A panicked guest: the ring stopped, so the last two probes are identical.
    panicked = [_probe("a"), _probe("b"), _probe("c"), _probe("c")]
    assert ub.guest_stopped_progressing(panicked) is True, panicked

    # A slow guest: the ring keeps growing, so no two consecutive reads agree.
    slow = [_probe("a"), _probe("b"), _probe("c"), _probe("d")]
    assert ub.guest_stopped_progressing(slow) is False, slow

    # The case that makes the threshold two rather than one: a *single* repeated
    # read is also what a slow boot looks like between two screendumps, and
    # treating it as a stall would retry boots that were working.
    one_repeat = [_probe("a"), _probe("a"), _probe("b")]
    assert ub.guest_stopped_progressing(one_repeat) is False, one_repeat

    # And the case that makes it a detector rather than a liveness check: a
    # guest that mounted the volume and then went idle has also stopped
    # writing, and reporting that as a failure would retry every success.
    mounted_then_idle = [_probe("a"), _probe("a", marker=True)]
    assert ub.guest_stopped_progressing(mounted_then_idle) is False, mounted_then_idle


def test_the_detector_ignores_a_probe_that_could_not_read_the_log() -> None:
    """An unreadable log is not a stalled one.

    Before the kernel publishes its load line there is no verified mapping
    between its virtual and physical addresses, so the first probes of every
    good boot cannot read anything.  Counting those as a stall would call the
    first twenty seconds of a working boot a failure.
    """
    unreadable = [
        {"ready": False, "reason": "no kernel load line in the serial log yet"},
        {"ready": False, "reason": "no kernel load line in the serial log yet"},
        _probe("a"),
    ]
    # Two unreadable probes and one readable read is not a stall, because the
    # threshold counts *readable* probes and there is only one.
    assert ub.guest_stopped_progressing(unreadable) is False, unreadable

    # ...and adding a second readable read that agrees makes it one, however
    # many unreadable probes came before it.  That is the same rule from the
    # other side, and it is what stops an unreadable probe from silently
    # resetting the count.
    assert ub.guest_stopped_progressing([*unreadable, _probe("a")]) is True

    # With nothing readable at all there is nothing to say.
    assert ub.guest_stopped_progressing([]) is False
    assert ub.guest_stopped_progressing(
        [{"ready": False, "reason": "the kernel image could not be parsed"}]
    ) is False


def test_a_frozen_log_with_the_marker_is_a_success_not_a_stall() -> None:
    """The marker wins, wherever it appears in the sequence.

    The probe keeps the whole ring rather than a tail, because the ring wraps:
    the Data volume's mount line can be anywhere in it, and a probe that only
    kept the last few kilobytes would report no marker for a guest that mounted
    the volume four hundred lines ago.
    """
    late_marker = [_probe("a"), _probe("b", marker=True), _probe("b"), _probe("b")]
    assert ub.guest_stopped_progressing(late_marker) is False, late_marker
    assert ub.DATA_MOUNT_MARKER == "mount-complete volume OwnerData"


def test_the_probe_reports_instead_of_raising_when_there_is_no_load_line(
    tmp_path: Path,
) -> None:
    """A probe that raised would abort a boot that is merely slow.

    This is the normal state of the first twenty seconds: the firmware has not
    printed the kernel's load line yet, so there is no verified address mapping
    and nothing to translate with.  The probe has to say so and let the loop
    continue.
    """
    record = ub._guest_log_probe(
        14444, kernel=object(), serial=tmp_path / "absent.log", scratch=tmp_path / "s.bin"
    )
    assert record == {
        "ready": False,
        "reason": "no kernel load line in the serial log yet "
                  f"({ErrorCode.EVIDENCE_INCOMPLETE})",
    }, record

    # ...and the same for a serial log that exists but has no load line in it,
    # which is the firmware-only case rather than the absent-file case.
    partial = tmp_path / "serial.log"
    partial.write_bytes(b"UNIXNU: starting\nDARWINBOOT: loading\n")
    record = ub._guest_log_probe(
        14444, kernel=object(), serial=partial, scratch=tmp_path / "s.bin"
    )
    assert record["ready"] is False, record
    assert "no kernel load line" in record["reason"], record


def test_the_probe_reports_a_kernel_it_could_not_parse(tmp_path: Path) -> None:
    """A kernel that cannot be read costs the probe, never the boot.

    ``boot`` has already established the kernel file exists.  What can still
    fail is ``KernelImage``'s verification -- a file that is not the digest it
    was verified against, or that is not a Mach-O at all -- and a caller booting
    such a kernel must still get a boot report rather than an exception from a
    module whose job is to record what happened.
    """
    kernel = tmp_path / "kernel.macho"
    kernel.write_bytes(b"not a mach-o at all")

    image, reason = ub._kernel_image(kernel, "0" * 64)
    assert image is None, image
    assert reason, "a kernel that cannot be parsed must say why"
    assert "image_hash_mismatch" in reason or "config_invalid" in reason, reason

    # And the probe refuses with that reason rather than reaching for a QMP
    # connection it has no use for.
    record = ub._guest_log_probe(
        14444, kernel=image, serial=tmp_path / "serial.log", scratch=tmp_path / "s.bin"
    )
    assert record == {"ready": False, "reason": "the kernel image could not be parsed"}, record


# --------------------------------------------------------------------------
# the probe's two files, which are not the same file
# --------------------------------------------------------------------------

#: A load line in the shape DarwinBoot prints, which is the only thing
#: ``load_addresses_from_serial`` accepts.  Both addresses are required and both
#: must be at least eight hex digits: a truncated line used to match and produce
#: a physical base of 4 (``guestobserve._LOAD_LINE``).
LOAD_LINE = b"kernel va 0xfffffff007166000 pa 0x408166000\n"

#: The account name this module's evidence must never carry.  ``getpass`` is the
#: same source :func:`unipad.host.paths.scrub_account` uses, so a test that
#: passes proves the scrub covers the name it will actually meet.
try:
    ACCOUNT = getpass.getuser()
except Exception:  # pragma: no cover - a host with no passwd entry
    ACCOUNT = "SomeBody"


class _LeakingGuestMemory:
    """A ``GuestMemory`` that writes raw guest memory where the real one does.

    The real one cannot return bytes: ``pmemsave`` writes to a *file*, so
    :meth:`unipad.host.guestobserve.GuestMemory.read` hands QEMU a filename and
    reads it back.  A fake that only records the path -- which is what the fake
    above does -- cannot see what that means, because it never puts anything on
    disk.  This one does, and what it puts there is what a guest's RAM holds.
    """

    def __init__(self, client: Any, scratch: Path) -> None:
        self.client = client
        self.scratch = Path(scratch)

    def read(self, physical: int, size: int) -> bytes:
        self.scratch.parent.mkdir(parents=True, exist_ok=True)
        # A guest whose RAM holds the account name: a shell that exported it, a
        # path in a config file, anything.  This is not a contrived string --
        # it is the reason PRD 9.2 exists.
        self.scratch.write_bytes(f"guest ram {ACCOUNT} secrets\n".encode() * 64)
        return self.scratch.read_bytes()


class _FailingObserver:
    """An observer whose ``kernel_log`` refuses, as a corrupt ring does."""

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def kernel_log(self) -> bytes:
        raise ub.UniPadError(ErrorCode.EVIDENCE_INCOMPLETE, "the ring is not readable")


def test_the_watch_loop_gives_the_probe_two_different_paths(
    inputs: BootInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The call site, which is where the defect actually was.

    Every test above exercises ``_guest_log_probe``'s contract in isolation, and
    the contract was correct the whole time the bug was live -- because the bug
    was not in the probe, it was in the one production call site that passed a
    single path and let the probe's default make the scratch *be* the evidence
    file.  A test that only pins the contract pins a rule nobody broke.

    Reverting the call site to ``scratch=work / "guest-kernel.log"`` leaves the
    whole suite green without this one, and the consequence is a real leak: the
    guest's raw RAM persisted at the path ``boot-report.json`` names.
    """
    monkeypatch.setattr(ub, "_qmp", _screendumping_qemu())
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))
    seen: list[dict[str, Any]] = []

    def spy(port: int, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"ready": False, "reason": "stubbed"}

    monkeypatch.setattr(ub, "_guest_log_probe", spy)
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    ub._watch(
        _AliveQEMU(), report, work, qmp_port=14452, seconds=2, every=1,
        kernel=inputs.kernel, kernel_sha256="0" * 64,
    )

    assert seen, "_watch never called the guest-log probe"
    for call in seen:
        scratch = Path(call["scratch"])
        # ``call.get("log", scratch)`` and not ``call["log"]``: the mutation this
        # test kills is the call site dropping ``log=`` entirely, and that has to
        # read as "one path for both" rather than as a KeyError.
        log = Path(call["log"]) if "log" in call else scratch
        assert log != scratch, (
            f"the probe was handed one path for both the raw guest memory and the "
            f"evidence file: {scratch}"
        )
        assert log.name == "guest-kernel.log", (
            f"the evidence file is no longer named guest-kernel.log: {log.name}"
        )
        assert "memory" in scratch.name, (
            f"the raw-memory scratch no longer says what it holds: {scratch.name}"
        )


def test_a_failed_probe_leaves_no_raw_guest_memory_in_the_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's scratch file must not be the file the report points at.

    ``GuestMemory.read`` cannot hand bytes back -- ``pmemsave`` writes to a
    filename -- so whatever path the probe passes as ``scratch`` is filled with
    the guest's physical memory by QEMU itself, before anything has been read or
    scrubbed.  Passing the evidence file as that path means a probe that fails
    *after* the read (a corrupt ring, a short read, a dead QMP connection) leaves
    the guest's raw RAM sitting at the path the report names as evidence, with
    no scrub having run and nothing to scrub it later.

    Measured, not theorised: the run directory is under ``local-private`` so the
    leak is not a publication, but the report's ``log`` field and every consumer
    that follows it treat that path as the guest's *kernel log*, and PRD 9.2
    forbids the account name in any of it.  A failed probe is exactly the run a
    human goes and reads.

    Removing the ``log=`` argument at the call site and deleting the guard below
    both leave the suite green without this test.
    """
    monkeypatch.setattr("unipad.host.qmp.QmpClient", _FakeQmpClient)
    monkeypatch.setattr("unipad.host.guestobserve.GuestMemory", _LeakingGuestMemory)
    monkeypatch.setattr("unipad.host.guestobserve.GuestObserver", _FailingObserver)
    serial = tmp_path / "serial.log"
    serial.write_bytes(LOAD_LINE)

    evidence = tmp_path / "guest-kernel.log"
    record = ub._guest_log_probe(
        14444, kernel=object(), serial=serial,
        scratch=tmp_path / "guest-memory.bin", log=evidence,
    )

    assert record["ready"] is False, record
    assert not evidence.exists(), (
        f"the evidence file exists and holds {evidence.stat().st_size} bytes after "
        f"a probe that never read anything: {evidence.read_bytes()[:120]!r}"
    )


def test_the_probe_refuses_a_scratch_that_is_the_evidence_file(tmp_path: Path) -> None:
    """Two paths, because one path cannot hold both a raw read and a scrubbed copy.

    The refusal rather than a silent rename: a caller that passes one path twice
    has made the mistake this module exists to prevent, and the report has to say
    so.  Deriving a different name behind the caller's back would leave the
    evidence file absent with a report that claims success.
    """
    serial = tmp_path / "serial.log"
    serial.write_bytes(LOAD_LINE)
    same = tmp_path / "guest-kernel.log"

    record = ub._guest_log_probe(
        14444, kernel=object(), serial=serial, scratch=same, log=same
    )

    assert record["ready"] is False, record
    assert "evidence" in record["reason"] and "scratch" in record["reason"], record
    assert "guest-kernel.log" not in record["reason"], (
        "the refusal names the file, and a run directory is not safe to print"
    )


def test_the_probe_scrubs_the_account_out_of_the_evidence_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scrub has to be load-bearing, not decorative.

    Deleting the ``scrub_account`` call at the write leaves the suite green --
    which is the shape of a guard that reads as though it ran.  This test fails
    without it, because the ring the fake returns contains the account name and
    the file on disk must not.
    """
    ring = (
        b"DarwinBoot: handoff\n"
        + f"launchd: {ACCOUNT} exported a token\n".encode()
        + b"mount-complete volume OwnerData\n"
    )
    monkeypatch.setattr("unipad.host.qmp.QmpClient", _FakeQmpClient)
    monkeypatch.setattr("unipad.host.guestobserve.GuestMemory", _FakeGuestMemory)
    monkeypatch.setattr("unipad.host.guestobserve.GuestObserver", _FakeGuestObserver)
    _stub_guestobserve(monkeypatch, ring)
    serial = tmp_path / "serial.log"
    serial.write_bytes(LOAD_LINE)

    evidence = tmp_path / "guest-kernel.log"
    record = ub._guest_log_probe(
        14444, kernel=object(), serial=serial,
        scratch=tmp_path / "guest-memory.bin", log=evidence,
    )

    assert record["ready"] is True, record
    assert record["marker"] is True, record
    on_disk = evidence.read_bytes()
    assert ACCOUNT.encode() not in on_disk, (
        f"the evidence file carries the account name: {on_disk[:200]!r}"
    )
    assert b"mount-complete volume OwnerData" in on_disk, (
        "the scrub removed more than the account name and took the marker with it"
    )


def test_the_probe_reason_never_names_the_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError from the scratch file stringifies with the whole path.

    ``str(FileNotFoundError)`` is ``[Errno 2] No such file or directory:
    'F:\\...\\USERR~1.WIN\\nested'`` -- the 8.3 short-name spelling of the
    account, which ``scrub_account`` does not match because it is not the account
    name.  The reason goes straight into ``boot-report.json`` and onto stdout, so
    a probe whose scratch could not be written prints the username while
    reporting a failure.

    Measured, not assumed: the first version of this test asserted
    ``str(tmp_path) not in reason`` and passed *with the fix deleted*, because
    ``str(tmp_path)`` is the long form and the message carries the short one.
    The assertion below is on the thing that is true either way -- a redacted
    reason names no directory, so it contains no path separator -- and the short
    name is what makes it necessary.

    Windows-specific in what it *reproduces*: the short name exists because the
    profile directory has a space in it.  On a host whose account name has no
    spaces there is nothing to shorten, and the OSError still reprs its filename,
    so the separator assertion holds everywhere.
    """
    nested = tmp_path / "nested"
    nested.mkdir()
    boom = OSError(2, "No such file or directory")
    boom.filename = str(nested)

    monkeypatch.setattr("unipad.host.qmp.QmpClient", _FakeQmpClient)
    monkeypatch.setattr("unipad.host.guestobserve.GuestMemory", _FakeGuestMemory)
    serial = tmp_path / "serial.log"
    serial.write_bytes(LOAD_LINE)
    # Raise from the line the code guards, not from somewhere convenient: the
    # ``except OSError`` below exists for ``serial.read_bytes()``, and patching
    # ``load_addresses_from_serial`` instead would test a branch that cannot be
    # reached this way -- it is handed bytes, not a path.
    real_is_file, real_read = Path.is_file, Path.read_bytes

    def only_this_serial(self: Path) -> bool:
        return True if self == serial else real_is_file(self)

    def read_or_refuse(self: Path) -> bytes:
        if self == serial:
            raise boom
        return real_read(self)

    monkeypatch.setattr(Path, "is_file", only_this_serial)
    monkeypatch.setattr(Path, "read_bytes", read_or_refuse)

    record = ub._guest_log_probe(
        14444, kernel=object(), serial=serial, scratch=tmp_path / "s.bin"
    )

    assert record["ready"] is False, record
    reason = record["reason"]
    # A drive root is the signature of an absolute path, and it is the part that
    # carries the profile directory the account name is rendered into.  The
    # separator *between* final components is not a disclosure -- after the
    # redaction the message reads ``'<run dir name>\nested'``, which names the
    # file that failed and nothing above it.  Asserting on ``"\\"`` instead would
    # be satisfied only by deleting the message.
    assert ":\\" not in reason and ":/" not in reason, (
        f"the reason still carries an absolute path, so it still carries the "
        f"directory the account name is rendered into: {reason}"
    )
    assert "nested" in reason, (
        "the redaction removed the whole message instead of the directory that is "
        f"not safe to print: {reason}"
    )
    # The short-name spelling specifically: this is what ``str(tmp_path)`` misses
    # and what the first version of this test missed with it.
    assert ACCOUNT.split(" ")[0][:8].upper() not in reason.upper(), (
        f"the 8.3 short name of the account is in the reason: {reason}"
    )


def test_watch_records_the_guest_log_probe_alongside_the_screendumps(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe is wired in, interleaved, and does not cost the screendumps.

    The interleaving is not an optimisation.  QEMU serves one QMP client at a
    time, so a probe running alongside a screendump would be refused rather than
    queued -- and a probe that fails must not take the screendumps with it,
    because they are the only thing that has always worked.
    """
    monkeypatch.setattr(ub, "_qmp", _screendumping_qemu())
    # A stand-in image: ``_watch`` parses the real one once and hands it to every
    # probe, and the synthetic kernel in the fixture is not a Mach-O with the
    # symbols this probe reads -- so without this the probe would correctly
    # refuse and the wiring would never be exercised.
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))
    seen: list[dict[str, Any]] = []

    def fake_probe(port: int, **_: Any) -> dict[str, Any]:
        seen.append({"port": port})
        # Two identical reads: the guest has stopped writing.
        return _probe("frozen")

    monkeypatch.setattr(ub, "_guest_log_probe", fake_probe)
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    watched = ub._watch(
        _AliveQEMU(), report, work, qmp_port=14452, seconds=3, every=1,
        kernel=inputs.kernel, kernel_sha256="0" * 64,
    )

    # The screendumps still happened, and so did the probes -- one of each per
    # round, which is what "interleaved" means in practice.
    assert len(watched.screenshots) == len(seen) >= 2, (watched.screenshots, seen)
    assert all(entry["port"] == 14452 for entry in seen), seen
    assert watched.qmp_evidence.get("screendump_error") is None, watched.qmp_evidence

    # ...and what the probe saw is on the report, in order, stamped.
    assert len(watched.guest_probes) == len(seen), watched.guest_probes
    assert [entry["fingerprint"] for entry in watched.guest_probes] == ["frozen"] * len(seen)
    assert all("at_seconds" in entry for entry in watched.guest_probes), watched.guest_probes
    assert watched.guest_log_bytes == 1024, watched.guest_log_bytes
    assert watched.guest_log_fingerprint == "frozen", watched.guest_log_fingerprint
    assert watched.data_volume_mounted is False, watched.guest_probes
    # A frozen log with no marker is exactly the panic this detects.
    assert watched.qmp_evidence["guest_log_stalled"] is True, watched.qmp_evidence


def test_watch_sets_the_marker_when_the_guest_reports_the_volume(
    inputs: BootInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one field that describes something the *guest* did.

    ``data_volume_mounted`` is set from the guest's own mount-complete line and
    from nothing else.  A guest that mounted the volume and then stopped writing
    is a success, not a stall, and that is the difference between this test and
    the one above.
    """
    monkeypatch.setattr(ub, "_qmp", _screendumping_qemu())
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))
    monkeypatch.setattr(ub, "_guest_log_probe", lambda *a, **k: _probe("x", marker=True))
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    watched = ub._watch(
        _AliveQEMU(), report, work, qmp_port=14452, seconds=3, every=1,
        kernel=inputs.kernel, kernel_sha256="0" * 64,
    )

    assert watched.data_volume_mounted is True, watched.guest_probes
    assert watched.data_marker == ub.DATA_MOUNT_MARKER, watched.data_marker
    assert watched.qmp_evidence["guest_log_stalled"] is False, watched.qmp_evidence


def test_watch_keeps_screendumping_when_the_kernel_cannot_be_parsed(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe with nothing to read is recorded once and then left alone.

    Every later round would report the same reason, and a report with fifteen
    copies of one sentence is harder to read than a report with one.
    """
    monkeypatch.setattr(ub, "_qmp", _screendumping_qemu())
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (None, "not a mach-o"))
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    watched = ub._watch(
        _AliveQEMU(), report, work, qmp_port=14452, seconds=4, every=1,
        kernel=inputs.kernel, kernel_sha256="0" * 64,
    )

    assert len(watched.screenshots) >= 2, watched.screenshots
    assert len(watched.guest_probes) == 1, watched.guest_probes
    assert "not a mach-o" in watched.guest_probes[0]["reason"], watched.guest_probes
    assert watched.qmp_evidence.get("guest_log_stalled") is False, watched.qmp_evidence


def test_watch_with_no_kernel_at_all_still_watches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_watch`` is reachable without a kernel, and says why there are no probes.

    The default is ``None`` so a caller that only wants screendumps gets them;
    the report says why there are no probes rather than implying there were none
    to take.
    """
    monkeypatch.setattr(ub, "_qmp", _screendumping_qemu())
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    watched = ub._watch(_AliveQEMU(), report, work, qmp_port=14452, seconds=3, every=1)

    assert watched.screenshots, watched.screenshots
    # One entry, not none: a report with an empty probe list reads as "there was
    # nothing to probe", and the reason is the difference between that and "this
    # caller never asked".
    assert len(watched.guest_probes) == 1, watched.guest_probes
    assert "no kernel image" in watched.guest_probes[0]["reason"], watched.guest_probes
    assert watched.qmp_evidence.get("guest_log_stalled") is False, watched.qmp_evidence


def test_the_retry_boots_again_in_a_fresh_directory_until_the_volume_mounts(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry's two properties: a fresh workdir per attempt, and it stops.

    A reused workdir would carry the previous attempt's ``vars.fd`` into this
    one, and two boots into one directory are two different machines --
    documented at :func:`boot`, and the reason each attempt gets its own.
    """
    import json

    calls: list[Path] = []

    def fake_boot(received: BootInputs, work: Path, **_: Any) -> BootReport:
        calls.append(Path(work))
        # The first attempt loses the race; the second wins it.
        mounted = len(calls) >= 2
        report = BootReport(
            ok=True, workdir=str(work), data_volume_mounted=mounted,
            data_marker=ub.DATA_MOUNT_MARKER if mounted else "",
        )
        # What ``boot`` does, and what ``boot_until_mounted`` relies on: it makes
        # the directory and writes a provisional report, then corrects one field
        # in that file once the chain's length is known.  A stand-in that skipped
        # this would be standing in for a different function.
        work.mkdir(parents=True, exist_ok=True)
        (work / "boot-report.json").write_text(
            json.dumps(report.to_dict(), indent=2) + chr(10), encoding="utf-8"
        )
        return report

    monkeypatch.setattr(ub, "boot", fake_boot)
    workdir = tmp_path / "chain"

    report = ub.boot_until_mounted(
        inputs, workdir, qemu=qemu, device_dir=_synthetic_device(tmp_path), attempts=3
    )

    assert len(calls) == 2, calls
    # Distinct directories, one per attempt, and the successful one is the one
    # that is reported.
    assert calls == [workdir / "attempt-1", workdir / "attempt-2"], calls
    assert report.data_volume_mounted is True, report
    assert report.attempt == 2, report
    assert report.attempts == 2, report


def test_the_retry_spends_the_whole_budget_and_says_so(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain that never mounts returns the last report, with the count.

    Without the count, "the Data volume never mounted" reads as a one-off -- and
    a one-off at 12.5% is indistinguishable from a flake, which is the whole
    reason the number is recorded.
    """
    import json

    calls: list[Path] = []

    def fake_boot(_received: BootInputs, work: Path, **_: Any) -> BootReport:
        calls.append(Path(work))
        report = BootReport(ok=True, workdir=str(work), data_volume_mounted=False)
        work.mkdir(parents=True, exist_ok=True)
        (work / "boot-report.json").write_text(
            json.dumps(report.to_dict(), indent=2) + chr(10), encoding="utf-8"
        )
        return report

    monkeypatch.setattr(ub, "boot", fake_boot)
    workdir = tmp_path / "chain"

    report = ub.boot_until_mounted(
        inputs, workdir, qemu=qemu, device_dir=_synthetic_device(tmp_path), attempts=3
    )

    assert len(calls) == 3, calls
    assert report.data_volume_mounted is False, report
    assert report.attempt == 3, report
    assert report.attempts == 3, report
    # The returned attempt's report on disk agrees with the object, because
    # ``attempts`` is the chain's length and only the chain knows it.
    written = json.loads((workdir / "attempt-3" / "boot-report.json").read_text(encoding="utf-8"))
    assert written["attempts"] == 3, written
    assert written["attempt"] == 3, written


def test_the_retry_defaults_to_three_attempts(inputs: BootInputs, qemu: Path) -> None:
    """Three, from the measured 1-in-8 failure rate.

    Not a round number chosen for its own sake: 12.5% cubed is 0.2%, which is
    below anything this project has measured, and three attempts is few enough
    that a real regression still surfaces inside a few minutes.
    """
    import inspect

    signature = inspect.signature(ub.boot_until_mounted)
    assert signature.parameters["attempts"].default == ub.MAX_BOOT_ATTEMPTS
    assert ub.MAX_BOOT_ATTEMPTS == 3, ub.MAX_BOOT_ATTEMPTS


def test_the_cli_can_ask_for_the_retry(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``--attempts`` reaches the retry, and the output says what happened.

    The CLI is the only entry point most runs go through, and a retry that
    existed in the module but not on the command line would be a retry nobody
    took -- the same shape as the ``--trust-cache`` gap this module already
    records.
    """
    import json

    monkeypatch.setattr(ub, "boot", lambda *a, **k: pytest.fail("--attempts did not reach the retry"))
    seen: list[dict[str, Any]] = []

    def fake_boot(_received: BootInputs, work: Path, **_: Any) -> BootReport:
        seen.append({"work": Path(work)})
        return BootReport(ok=True, workdir=str(work), data_volume_mounted=False,
                          attempts=3, attempt=2, guest_log_bytes=1024,
                          guest_probes=[_probe("frozen")])

    monkeypatch.setattr(ub, "boot_until_mounted", fake_boot)
    workdir = tmp_path / "run"

    code = ub.main([
        "--device", str(_synthetic_device(tmp_path)), "--qemu", str(qemu),
        "--firmware", str(inputs.firmware), "--kernel", str(inputs.kernel),
        "--devicetree", str(inputs.devicetree), "--darwinboot", str(inputs.darwinboot),
        "--unixnu", str(inputs.unixnu), "--workdir", str(workdir),
        "--attempts", "3",
    ])

    assert code == 0, capsys.readouterr().out
    assert len(seen) == 1, seen
    out = capsys.readouterr().out
    assert "data volume mounted: False" in out, out
    assert "boot attempts: 3 (this was attempt 2)" in out, out
    assert "guest kernel log: 1024 bytes from 1 probes" in out, out


# ---------------------------------------------------------------------------
# the wiring the mutation review found unpinned
# ---------------------------------------------------------------------------
# Why this section exists
# ----------------------
# The first version of the retry passed 118/0 and still had a defect that would
# have made it dead in the real path: ``boot`` could have stopped handing the
# kernel to ``_watch`` and nothing would have noticed, because every test either
# calls ``_watch`` directly with a synthetic kernel or monkeypatches ``boot``
# away.  A 对质 review ran 55 mutations against the first version; 37 went red
# and 18 stayed green, and these tests are the ones written after the green ones.
#
# The rule being enforced is the one in this module's own header: a test that
# goes red on its own defect is worse than no test, and a test that stays green
# while the behaviour it names is deleted is the same failure seen from the other
# side.  Each test below is named after the mutation it kills.


class _FakeQmpClient:
    """A QMP client that connects and closes and does nothing else.

    ``_guest_log_probe`` is the only caller, and it never issues a command
    through the client directly -- it hands it to ``GuestMemory``, which is
    faked separately.  So the only behaviour that matters is that ``connect``
    and ``close`` are called once each, which is what pins "one connection for
    the whole probe, opened after the serial log is read".
    """

    connected = 0
    closed = 0

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def connect(self) -> dict[str, Any]:
        type(self).connected += 1
        return {"greeting": {}, "capabilities": {}}

    def close(self) -> None:
        type(self).closed += 1


class _FakeGuestMemory:
    """A ``GuestMemory`` that records what it was handed and reads nothing."""

    def __init__(self, client: Any, scratch: Path) -> None:
        self.client = client
        self.scratch = Path(scratch)


class _FakeGuestObserver:
    """An observer whose ``kernel_log`` returns whatever the test says."""

    ring = b""
    last: "_FakeGuestObserver | None" = None

    def __init__(self, memory: Any, image: Any, load: Any) -> None:
        self.memory = memory
        self.image = image
        self.load = load
        type(self).last = self

    def kernel_log(self) -> bytes:
        return type(self).ring


def _stub_guestobserve(monkeypatch: pytest.MonkeyPatch, ring: bytes) -> None:
    """Point ``_guest_log_probe``'s lazily-imported names at fakes.

    The imports inside ``_guest_log_probe`` are function-local, so patching the
    *modules* is what takes effect -- patching ``ub.GuestObserver`` would do
    nothing, because that name does not exist there.
    """
    _FakeGuestObserver.ring = ring
    _FakeQmpClient.connected = 0
    _FakeQmpClient.closed = 0
    monkeypatch.setattr("unipad.host.qmp.QmpClient", _FakeQmpClient)
    monkeypatch.setattr("unipad.host.guestobserve.GuestMemory", _FakeGuestMemory)
    monkeypatch.setattr("unipad.host.guestobserve.GuestObserver", _FakeGuestObserver)


def test_boot_hands_the_verified_kernel_to_the_watch_loop(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``boot`` passes the kernel and its digest to ``_watch``.

    This is the test the 对质 review asked for.  Deleting ``kernel=`` and
    ``kernel_sha256=`` from the ``_watch`` call in :func:`boot` leaves the whole
    suite green -- and the consequence is that no probe ever runs,
    ``data_volume_mounted`` is permanently ``False``, and :func:`boot_until_mounted`
    spends its whole three-attempt budget on every boot.  The 12.5% race comes
    back with no evidence anywhere, which is the exact failure this module's
    header says is worse than the race.

    The reason it was invisible is structural and worth stating: every other test
    either calls ``_watch`` directly with a synthetic kernel, or monkeypatches
    ``boot`` away entirely.  This one spies on the seam between them.
    """
    import hashlib
    import json

    seen: dict[str, Any] = {}

    def spy(_process: Any, report: BootReport, _work: Path, **kwargs: Any) -> BootReport:
        seen.update(kwargs)
        seen["returned"] = report
        return report

    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    monkeypatch.setattr(ub, "_watch", spy)
    work = tmp_path / "run"

    report = ub.boot(inputs, work, qemu=qemu, device_dir=_synthetic_device(tmp_path))

    assert launched, "boot never reached Popen"
    assert "kernel" in seen, f"_watch was called without a kernel: {sorted(seen)}"
    # The *resolved* path, because that is the file ``stage_esp`` copied onto the
    # ESP and the only spelling whose digest means anything.
    assert seen["kernel"] == inputs.kernel.resolve(), seen["kernel"]
    expected = hashlib.sha256(inputs.kernel.read_bytes()).hexdigest()
    assert seen["kernel_sha256"] == expected, seen["kernel_sha256"]
    # ...and the digest is of the kernel that was actually staged, not of some
    # other file that happens to be named kernel.macho.
    staged_digest = hashlib.sha256(
        (work / "esp" / "EFI" / "UniXNU" / "Payloads" / "kernel.macho").read_bytes()
    ).hexdigest()
    assert seen["kernel_sha256"] == staged_digest, (
        "the digest handed to the probe is not the digest of the kernel the guest "
        "was given, so every symbol address derived from it would be wrong"
    )
    # The report is the same object, so whatever the probe finds lands on the
    # report ``boot`` returns.
    assert seen["returned"] is report, "the watch loop was handed a different report"


def test_watch_survives_a_probe_that_could_not_read_the_log(
    inputs: BootInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A not-ready probe must not take the screendumps down with it.

    Replacing ``if entry.get("ready"):`` with ``if True:`` in :func:`_watch`
    leaves the suite green -- and the consequence is a ``KeyError: 'bytes'`` on
    the *first* round of every real boot, because the probe cannot read anything
    until the firmware has printed the kernel's load line, which is the normal
    state of the first twenty seconds.  The whole screendump loop dies.

    Every other test that reaches the probe monkeypatches it to return a *ready*
    entry, which is why this was invisible.  The real ``_guest_log_probe`` returns
    this exact record, and this test feeds exactly that record.
    """
    monkeypatch.setattr(ub, "_qmp", _screendumping_qemu())
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))
    rounds: list[int] = []

    def not_ready(port: int, **_: Any) -> dict[str, Any]:
        rounds.append(port)
        # Verbatim what ``_guest_log_probe`` returns before the load line exists.
        return {"ready": False,
                "reason": "no kernel load line in the serial log yet "
                          f"({ErrorCode.EVIDENCE_INCOMPLETE})"}

    monkeypatch.setattr(ub, "_guest_log_probe", not_ready)
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    watched = ub._watch(
        _AliveQEMU(), report, work, qmp_port=14452, seconds=4, every=1,
        kernel=inputs.kernel, kernel_sha256="0" * 64,
    )

    # It kept probing, and the screendumps kept coming.
    assert len(rounds) >= 2, rounds
    assert len(watched.screenshots) == len(rounds), (watched.screenshots, rounds)
    assert watched.qmp_evidence.get("screendump_error") is None, watched.qmp_evidence
    # ...and nothing was recorded that a reader could mistake for a log.
    assert all(entry["ready"] is False for entry in watched.guest_probes), watched.guest_probes
    assert watched.guest_log_bytes == 0, watched.guest_log_bytes
    assert watched.guest_log_fingerprint is None, watched.guest_log_fingerprint
    assert watched.data_volume_mounted is False, watched.guest_probes
    # An unreadable log is not a stalled one: this is the first twenty seconds of
    # a *good* boot, and calling it a failure would retry every success.
    assert watched.qmp_evidence["guest_log_stalled"] is False, watched.qmp_evidence


def test_the_probe_finds_the_marker_anywhere_in_the_ring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole ring is searched, not its tail.

    Substituting ``DATA_MOUNT_MARKER in text[-512:]`` for ``in text`` leaves the
    suite green, and the consequence is a successful boot reported as a failure:
    the ring wraps, so the Data volume's mount line can be anywhere in it, and a
    tail-only search reports ``marker: False`` for a guest that mounted the volume
    four hundred lines ago -- followed by a frozen log, ``stalled: True``, and a
    retry of every success.

    The ring here is shaped like the real one: the mount line early, then
    hundreds of kilobytes of userland chatter after it.
    """
    before = b"UNIXNU: starting\nDARWINBOOT: loading\n" + LOAD_LINE
    mount = b"disk5s1 mount-complete volume OwnerData\n"
    # Well past any tail: the ring is a few hundred KiB and the marker is near
    # its start, which is what a wrapped ring looks like from the reader's side.
    after = b"launchd[1] doing its thing\n" * 4096
    assert len(after) > 512, len(after)
    assert mount not in after[-512:], "the fixture stopped discriminating"
    _stub_guestobserve(monkeypatch, before + mount + after)

    serial = tmp_path / "serial.log"
    serial.write_bytes(before)
    # Two paths: the raw memory QEMU writes, and the evidence the probe writes.
    # They were one path, and that is the defect these tests now spell out.
    scratch = tmp_path / "guest-memory.bin"
    log = tmp_path / "guest-kernel.log"

    record = ub._guest_log_probe(
        14444, kernel=object(), serial=serial, scratch=scratch, log=log
    )

    assert record["ready"] is True, record
    assert record["marker"] is True, "the mount line was in the ring and was missed"
    assert record["bytes"] == len(before) + len(mount) + len(after), record["bytes"]
    # The whole ring is kept on disk, not a tail: the file is the evidence, and a
    # truncated one cannot answer a question asked later.
    assert log.read_bytes() == before + mount + after, "the ring was truncated on disk"
    # One connection for the whole probe, opened and closed.
    assert _FakeQmpClient.connected == 1, _FakeQmpClient.connected
    assert _FakeQmpClient.closed == 1, _FakeQmpClient.closed


def test_the_probe_fingerprints_the_raw_bytes_and_scrubs_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fingerprint is of the bytes read; the file on disk is scrubbed.

    Two properties that had no pin.  The fingerprint is taken from the
    *unscrubbed* bytes so it is comparable across probes; the file on disk is
    scrubbed because it is evidence and PRD 9.2 forbids the account name in any
    of it.  Getting either backwards is silent: a fingerprint of the scrubbed
    text would still be stable within one run, and an unscrubbed file is only a
    problem for whoever reads it.
    """
    import hashlib

    from unipad.host.paths import account_name

    name = account_name()
    if not name:  # pragma: no cover - a stripped container has no account
        pytest.skip("this host reports no account name, so there is nothing to scrub")
    ring = LOAD_LINE + f"some line mentioning {name} in passing\n".encode() + b"more log\n"
    _stub_guestobserve(monkeypatch, ring)

    serial = tmp_path / "serial.log"
    serial.write_bytes(LOAD_LINE)
    scratch = tmp_path / "guest-memory.bin"
    log = tmp_path / "guest-kernel.log"

    record = ub._guest_log_probe(
        14444, kernel=object(), serial=serial, scratch=scratch, log=log
    )

    assert record["ready"] is True, record
    # The fingerprint is of the raw bytes, so two probes of an unchanged guest
    # agree however many times the account name appears.
    assert record["fingerprint"] == hashlib.sha256(ring).hexdigest(), record["fingerprint"]
    # The copy on disk is scrubbed, and the scrub is visible in it.
    written = log.read_text(encoding="utf-8")
    assert name not in written, "the account name reached the evidence file"
    assert "<redacted:account>" in written, written
    # The path in the record is redacted to its final component (PRD 9.2).
    assert record["log"] == "guest-kernel.log", record["log"]
    assert str(tmp_path) not in record["log"], record["log"]


def test_the_probe_survives_losing_the_scratch_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log that cannot be written costs the evidence, not the probe.

    Turning the ``except OSError: pass`` around the scratch write into a
    ``raise`` leaves the suite green, and the consequence is a boot that aborts
    because a directory went read-only halfway through -- after the bytes were
    already read and the fingerprint computed.
    """
    import hashlib

    ring = LOAD_LINE + b"mount-complete volume OwnerData\n"
    _stub_guestobserve(monkeypatch, ring)

    serial = tmp_path / "serial.log"
    serial.write_bytes(LOAD_LINE)
    # A directory in the evidence file's place: writing a file *at* it fails, and
    # it is a failure that has nothing to do with the guest.
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    record = ub._guest_log_probe(
        14444, kernel=object(), serial=serial,
        scratch=tmp_path / "guest-memory.bin", log=blocked,
    )

    assert record["ready"] is True, record
    assert record["marker"] is True, record
    assert record["fingerprint"] == hashlib.sha256(ring).hexdigest(), record["fingerprint"]


def test_the_probe_hands_the_scratch_to_the_memory_it_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pmemsave`` writes where it was told, and it was told the run directory.

    Handing the probe ``qemu-stdout.log`` instead of a dedicated scratch, or
    ``screen.ppm`` instead of the evidence file, both leave the suite green --
    pure plumbing, but plumbing that decides whether the guest's memory lands in
    a file that gets overwritten by the next screendump.

    The scratch is asserted to be its own file and *not* the evidence file, which
    is the half of this that was once wrong: the two were one path, so QEMU wrote
    the guest's raw memory into the file the report names as evidence.
    """
    _stub_guestobserve(monkeypatch, LOAD_LINE + b"x\n")
    serial = tmp_path / "serial.log"
    serial.write_bytes(LOAD_LINE)
    scratch = tmp_path / "guest-memory.bin"
    log = tmp_path / "guest-kernel.log"

    ub._guest_log_probe(
        14444, kernel=object(), serial=serial, scratch=scratch, log=log
    )

    memory = _FakeGuestObserver.last.memory
    assert memory.scratch == scratch, memory.scratch
    assert memory.scratch.name == "guest-memory.bin", memory.scratch
    assert memory.scratch != log, (
        "the raw guest memory and the evidence file are the same path again"
    )
    # And the observer was built with the kernel image and the load addresses the
    # serial log reported, not with defaults.
    assert _FakeGuestObserver.last.load.kernel_va == 0xFFFFFFF007166000
    assert _FakeGuestObserver.last.load.kernel_pa == 0x408166000
    assert _FakeGuestObserver.last.image is not None


def test_the_cli_forwards_the_attempt_count_it_was_given(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``--attempts 3`` arrives as ``attempts=3``, not as whatever the default is.

    The first version of ``test_the_cli_can_ask_for_the_retry`` replaced
    ``boot_until_mounted`` with ``def fake_boot(_received, work, **_)`` -- the
    ``**_`` swallowed the keyword -- so hard-coding ``attempts=1`` in ``main``
    stayed green.  The test proved the destination and not the payload.
    """
    received: dict[str, Any] = {}

    def fake_boot(_inputs: BootInputs, work: Path, **kwargs: Any) -> BootReport:
        received.update(kwargs)
        return BootReport(ok=True, workdir=str(work), data_volume_mounted=False)

    monkeypatch.setattr(ub, "boot_until_mounted", fake_boot)

    ub.main([
        "--device", str(_synthetic_device(tmp_path)), "--qemu", str(qemu),
        "--firmware", str(inputs.firmware), "--kernel", str(inputs.kernel),
        "--devicetree", str(inputs.devicetree), "--darwinboot", str(inputs.darwinboot),
        "--unixnu", str(inputs.unixnu), "--workdir", str(tmp_path / "run"),
        "--attempts", "3",
    ])

    assert received.get("attempts") == 3, received


def test_a_zero_attempt_count_means_one_boot_not_a_refusal(
    inputs: BootInputs, qemu: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max(int(attempts), 1)`` is a floor, and dropping it is silent.

    Without it, ``attempts=0`` turns "boot once" into ``UniPadError`` from a
    guard marked ``# pragma: no cover`` -- which is a refusal nobody asked for,
    arriving from a branch no test reaches.
    """
    import json

    calls: list[Path] = []

    def fake_boot(_inputs: BootInputs, work: Path, **_: Any) -> BootReport:
        calls.append(Path(work))
        report = BootReport(ok=True, workdir=str(work))
        work.mkdir(parents=True, exist_ok=True)
        (work / "boot-report.json").write_text(
            json.dumps(report.to_dict(), indent=2) + chr(10), encoding="utf-8"
        )
        return report

    monkeypatch.setattr(ub, "boot", fake_boot)

    report = ub.boot_until_mounted(
        inputs, tmp_path / "chain", qemu=qemu,
        device_dir=_synthetic_device(tmp_path), attempts=0,
    )

    assert len(calls) == 1, calls
    assert report.attempts == 1, report.attempts
    assert report.attempt == 1, report.attempt


def test_the_probe_runs_after_the_screendump_in_every_round(
    inputs: BootInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is pinned, not just the counts.

    The first version of ``test_watch_records_the_guest_log_probe_alongside_the_screendumps``
    asserted only that there was one probe per screendump, so moving the probe
    *before* the screendump stayed green for any run that had a kernel.  The
    reason the order matters is in :func:`_qmp`'s own docstring: QEMU serves one
    QMP client at a time, so a probe issued while a screendump is in flight is
    refused rather than queued -- and the screendump is the thing that has always
    worked.
    """
    order: list[str] = []
    monkeypatch.setattr(ub, "_qmp", _screendumping_qemu())

    def recording_qmp(port: int, command: str, **arguments: Any) -> dict[str, Any]:
        # The command's own name, not a label: ``_watch`` also issues ``quit``
        # on the way out, and a recorder that called everything which is not a
        # screendump a "query-status" would count that as a readiness probe.
        order.append(command)
        if command == "screendump":
            target = Path(arguments["filename"])
            target.write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
        return {"return": {"status": "running"}}

    monkeypatch.setattr(ub, "_qmp", recording_qmp)
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))

    def probe(port: int, **_: Any) -> dict[str, Any]:
        order.append("probe")
        return _probe("a")

    monkeypatch.setattr(ub, "_guest_log_probe", probe)
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    ub._watch(
        _AliveQEMU(), report, work, qmp_port=14452, seconds=4, every=1,
        kernel=inputs.kernel, kernel_sha256="0" * 64,
    )

    # One query-status for readiness, then screendump/probe pairs, then one quit
    # on the way out -- and never a probe before the screendump it belongs to.
    assert order[0] == "query-status", order
    assert order[-1] == "quit", order
    pairs = order[1:-1]
    assert len(pairs) >= 4, order
    assert pairs == ["screendump", "probe"] * (len(pairs) // 2), order


def test_a_broken_screendump_does_not_silence_the_data_mount_probe(
    inputs: BootInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed screendump must cost the screendump and nothing else.

    The screendump used to be inline in ``_watch`` with three ``continue``
    statements, and every one of them skipped the probe as well.  So a run whose
    screendumps broke -- QMP refused the command, or ``_write_png`` raised --
    reported no ``guest_probes`` at all, ``data_volume_mounted`` stayed ``False``,
    and :func:`boot_until_mounted` spent its whole three-attempt budget re-booting
    a guest that had mounted its Data volume on the first try.

    That is the worst shape a bug can take here, because it is *invisible on the
    good path*: screendumps have never failed in a measured boot, so the coupling
    never fires, and the retry that is supposed to make the 12.5% race go away
    turns into a 100% failure rate the moment an unrelated subsystem breaks.  The
    two observations answer different questions and must fail independently.
    """
    order: list[str] = []

    def failing_qmp(port: int, command: str, **arguments: Any) -> dict[str, Any]:
        order.append(command)
        if command == "screendump":
            # The exact shape QEMU answers with, which is one of the three ways
            # the inline version used to skip the probe.
            return {"error": {"class": "GenericError", "desc": "screendump is not available"}}
        return {"return": {"status": "running"}}

    monkeypatch.setattr(ub, "_qmp", failing_qmp)
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))

    def probe(port: int, **_: Any) -> dict[str, Any]:
        order.append("probe")
        return _probe("mounted", marker=True)

    monkeypatch.setattr(ub, "_guest_log_probe", probe)
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    ub._watch(
        _AliveQEMU(), report, work, qmp_port=14453, seconds=4, every=1,
        kernel=inputs.kernel, kernel_sha256="0" * 64,
    )

    # The probe ran anyway, and its answer survived.
    assert order.count("probe") >= 3, order
    assert report.guest_probes, "the probe was suppressed by the screendump failure"
    assert report.data_volume_mounted, report.to_dict()
    assert report.data_marker == ub.DATA_MOUNT_MARKER
    # The screendump failure is still *recorded* -- suppressing it would be the
    # opposite bug.
    assert "screendump_error" in report.qmp_evidence, report.qmp_evidence
    assert report.screenshots == [], report.screenshots


def test_a_screendump_that_cannot_be_encoded_does_not_silence_the_probe(
    inputs: BootInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third ``continue`` -- a PPM ``_write_png`` cannot decode.

    This one is separate from the QMP-refusal case because it fails in different
    code: the QMP round trip succeeds, the file exists, and only the encoding
    raises.  The inline version caught it with ``continue`` too.
    """
    order: list[str] = []

    def undecodable_qmp(port: int, command: str, **arguments: Any) -> dict[str, Any]:
        order.append(command)
        if command == "screendump":
            Path(arguments["filename"]).write_bytes(b"not a ppm at all")
        return {"return": {"status": "running"}}

    monkeypatch.setattr(ub, "_qmp", undecodable_qmp)
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))

    def probe(port: int, **_: Any) -> dict[str, Any]:
        order.append("probe")
        return _probe("mounted", marker=True)

    monkeypatch.setattr(ub, "_guest_log_probe", probe)
    work = tmp_path / "run"
    work.mkdir()
    report = BootReport(ok=False, workdir=str(work))

    ub._watch(
        _AliveQEMU(), report, work, qmp_port=14454, seconds=4, every=1,
        kernel=inputs.kernel, kernel_sha256="0" * 64,
    )

    assert order.count("probe") >= 3, order
    assert report.data_volume_mounted, report.to_dict()
    assert "screendump_error" in report.qmp_evidence, report.qmp_evidence
    # The scratch PPM is cleaned up even when it could not be decoded, so a run
    # does not accumulate one per round.
    assert not (work / "screen.ppm").exists()


def test_the_probe_reports_a_port_it_cannot_even_construct_a_client_for(
    inputs: BootInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad ``--qmp-port`` is reported, not raised.

    ``QmpClient.__init__`` raises ``UniPadError(CONFIG_INVALID)`` for a port
    outside 1..65535.  The probe used to construct the client *outside* its
    ``try``, so that exception escaped ``_guest_log_probe`` -- and therefore
    :func:`_watch`, and therefore :func:`boot` -- for a run that was merely
    misconfigured.  The function's own docstring says "every failure mode returns
    a record instead of raising"; this is the failure mode it was not covering,
    and it is the one a caller hits by typing ``--qmp-port 0``.
    """
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))
    work = tmp_path / "run"
    work.mkdir()
    serial = work / "serial.log"
    serial.write_bytes(LOAD_LINE)

    # The real constructor, deliberately: a fake would not raise.
    entry = ub._guest_log_probe(
        0, kernel=object(), serial=serial,
        scratch=work / "guest-memory.bin", log=work / "guest-kernel.log",
    )

    assert entry["ready"] is False, entry
    # The reason is ``f"{code}: {message}"``, and ``code`` is the enum's *value*,
    # which is lowercase -- ``ErrorCode.CONFIG_INVALID.value`` is
    # ``"config_invalid"``.  Asserting the uppercase spelling here is the mistake
    # this line exists to prevent being "fixed" into.
    assert "config_invalid" in entry["reason"], entry["reason"]
    assert "qmp port" in entry["reason"], entry["reason"]


# ---------------------------------------------------------------------------
# the Data seed against the payload build's own record
# ---------------------------------------------------------------------------

def _payload_report(tmp_path: Path, digest: object) -> Path:
    """A ``payload.json`` carrying ``data_ramdisk_sha256``."""
    report = tmp_path / "payload.json"
    report.write_text(json.dumps({"data_ramdisk_sha256": digest}), encoding="utf-8")
    return report


def test_a_seed_that_is_not_the_payload_builds_own_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The defect this closes, in one test.

    On Windows the payload image is never attached as a drive -- the ESP is a
    staged directory behind QEMU's ``fat:`` block driver -- so the seed inside
    the image is invisible to the guest and this directory is the only delivery
    channel.  A seed staged from another build replaces the image's own with
    nothing to notice.  Measured on 2026-10-30 with ``bundle7``: the image
    carried ``6fa3a34a…`` whose volume UUID ``974BCE86-…`` is the one its own
    kernel was built for, and what was staged was ``784a8507…`` with
    ``E3E8AE2D-…``.  The guest logged ``native_owner_data_stage = 0`` /
    ``status = 2`` (ENOENT), so the owner Data volume never mounted and the GPU
    addon mount's only call site was never entered.
    """
    seed = tmp_path / "borrowed.dbr"
    blob = _synthetic_seed()
    seed.write_bytes(blob)
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    report = _payload_report(tmp_path, "0" * 64)

    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed, payload_report=report),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "not the one the payload build put in the image" in error.message, error
    # Both digests, because the question an operator has is "which of these do I
    # have" and a message naming one of them does not answer it.
    assert error.details["staged"] == hashlib.sha256(blob).hexdigest(), error.details
    assert error.details["declared"] == "0" * 64, error.details
    # And what it costs, in the message rather than only in a code.
    assert "owner Data volume will not mount" in error.message, error
    assert "GPU addon mount" in error.message, error


def test_a_seed_that_matches_the_payload_builds_record_is_staged(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The green control: the matched pair stages, and says it was checked.

    Without this the refusal above could pass for the wrong reason -- a guard
    that refuses everything is not a guard.
    """
    seed = tmp_path / "matched.dbr"
    blob = _synthetic_seed()
    seed.write_bytes(blob)
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    report = _payload_report(tmp_path, hashlib.sha256(blob).hexdigest())

    esp = tmp_path / "esp"
    written = ub.stage_esp(
        replace(inputs, devicetree=tree, data_ramdisk=seed, payload_report=report),
        esp, real_spec,
    )
    staged = esp / "EFI/UniXNU/Payloads" / ub.DATA_RAMDISK_NAME
    assert staged.read_bytes() == blob
    # The record says the check ran, the way the trust cache's key does: a later
    # mismatch cannot have slipped through a run whose report says otherwise.
    assert written["_data_seed_declared"] == hashlib.sha256(blob).hexdigest(), written
    assert written["_data_seed_sha256"] == hashlib.sha256(blob).hexdigest(), written


def test_a_seed_with_no_payload_report_is_staged_unchecked(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """The check is additive: a caller with no report gets the old behaviour.

    Every existing caller passes no ``payload_report``, and a field that
    silently turned their boots into refusals would be a different defect.
    """
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))

    written = ub.stage_esp(
        replace(inputs, devicetree=tree, data_ramdisk=seed), tmp_path / "esp", real_spec
    )
    assert (tmp_path / "esp" / "EFI/UniXNU/Payloads" / ub.DATA_RAMDISK_NAME).is_file()
    assert "_data_seed_declared" not in written, written


def test_a_payload_built_without_a_seed_declares_no_digest(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """``null`` is not a disagreement.

    ``unixnu/payload.py:188`` writes ``data_ramdisk_sha256: null`` for a payload
    built without a seed.  A boot that adds one to such a payload is doing
    something the build did not, and there is nothing to compare against -- so
    refusing would make the check a way to fail rather than a way to be sure.
    """
    seed = tmp_path / "added.dbr"
    seed.write_bytes(_synthetic_seed())
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    report = _payload_report(tmp_path, None)

    written = ub.stage_esp(
        replace(inputs, devicetree=tree, data_ramdisk=seed, payload_report=report),
        tmp_path / "esp", real_spec,
    )
    assert "_data_seed_declared" not in written, written
    assert (tmp_path / "esp" / "EFI/UniXNU/Payloads" / ub.DATA_RAMDISK_NAME).is_file()


def test_a_payload_report_that_does_not_parse_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """A malformed report refuses rather than skipping the check.

    Silently skipping a check because a file is unreadable is how the check
    stops running without anyone noticing -- which is the shape this whole
    change exists to remove.
    """
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    report = tmp_path / "payload.json"
    report.write_text("{ not json", encoding="utf-8")

    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed, payload_report=report),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "does not parse" in error.message, error


def test_a_declared_digest_that_is_not_a_sha256_is_refused(
    inputs: BootInputs, real_spec: dict[str, Any], tmp_path: Path
) -> None:
    """A truncated digest would refuse every seed forever.

    Comparing against a value that cannot be a digest reads as a broken reader
    rather than a broken argument, so it is named as what it is.
    """
    seed = tmp_path / "owner-data-1g.dbr"
    seed.write_bytes(_synthetic_seed())
    tree = tmp_path / "with-placeholder.adt"
    tree.write_bytes(_synthetic_tree(ramdisk=bytes(16)))
    report = _payload_report(tmp_path, "6fa3a34a")

    error = _refused(
        ub.stage_esp,
        replace(inputs, devicetree=tree, data_ramdisk=seed, payload_report=report),
        tmp_path / "esp",
        real_spec,
    )
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "not a sha256" in error.message, error


def test_the_payload_report_is_an_input_like_any_other(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The report is named by ``missing()`` and carried by ``to_dict()``.

    A field that is checked when present but never reported absent is a field
    whose absence is invisible -- and the absence is what turns the check off.
    """
    absent = replace(inputs, payload_report=tmp_path / "absent.json")
    assert "payload_report=absent.json" in absent.missing(), absent.missing()
    assert inputs.to_dict().get("payload_report") is None
    with_report = replace(inputs, payload_report=Path("payload.json"))
    assert with_report.to_dict()["payload_report"] == "payload.json"


# ---------------------------------------------------------------------------
# the shared window
# ---------------------------------------------------------------------------
def test_no_window_by_default_keeps_the_machine_it_always_was(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """Zero is the default and zero must mean *nothing changes*.

    Every run recorded before 2026-10-30 had one memory node, and a parameter
    that silently adds a second one turns every one of those runs into a
    different machine without saying so.  Asserted as an exact token list rather
    than "no shared-window item", because the failure mode is an extra ``-object``
    or ``-numa`` that nothing asked for.
    """
    work = tmp_path / "run"
    argv = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none")
    assert _values(argv, "-m") == [str(ub.MEMORY_MIB)], argv
    assert _values(argv, "-object") == [], argv
    assert _values(argv, "-numa") == [], argv
    assert not any("shared-window" in token for token in argv), argv


def test_the_window_is_a_second_memory_node_not_a_bigger_first_one(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """The split is the point, and both halves of it are asserted.

    ``SharedWindow.cpp:133`` refuses a window that is memory the kernel manages
    (``pmap_valid_address(base)`` must be false), so the window has to start
    *after* the DRAM the device tree declares.  ``run-qemu.py:171-173`` records
    the other half: "Sharing all of the guest's memory instead stopped the
    userland at launchd, spinning."  One node of 8 GiB therefore fails for a
    different reason than two nodes of 6 + 2, and only the second one works.
    """
    work = tmp_path / "run"
    argv = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                         shared_window_mib=2048)
    # -m grows by exactly the window: QEMU needs a guest-physical range to put
    # the second node in, and the guest's own view of RAM does not change.
    assert _values(argv, "-m") == [str(ub.MEMORY_MIB + 2048)], argv
    backends = _values(argv, "-object")
    assert f"memory-backend-ram,id=ram0,size={ub.MEMORY_MIB}M" in backends, backends
    window = next((b for b in backends if b.startswith("memory-backend-file,id=ram1")), "")
    assert window, backends
    assert f"size={2048}M" in window, window
    # share=on is what makes the window visible outside the guest at all.
    assert ",share=on" in window, window
    nodes = _values(argv, "-numa")
    assert nodes == ["node,nodeid=0,cpus=0,memdev=ram0", "node,nodeid=1,memdev=ram1"], nodes


def test_the_windows_fw_cfg_address_is_upstreams_formula_not_a_constant(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """``BASE:SIZE:MODE``, with BASE derived rather than written down.

    ``run-qemu.py:27`` spells it ``0x40000000 + (MEMORY_MIB << 20)``, which for
    the 6 GiB this module gives the machine is 0x1c0000000 -- the address the
    flag's own help text names.  Hard-coding that address passes this test for
    the current board and silently overlaps the guest's RAM for any other one,
    so the assertion is on the arithmetic: a different ``memory_mib`` must move
    the base by exactly that much.
    """
    work = tmp_path / "run"
    argv = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                         shared_window_mib=2048)
    item = _values(argv, "-fw_cfg")
    window = next((v for v in item if "shared-window" in v), "")
    assert window, item
    expected = (f"name=opt/org.unixnu/shared-window,string="
                f"{ub.SHARED_WINDOW_BASE + (ub.MEMORY_MIB << 20):x}:{2048 << 20:x}:wb")
    assert window == expected, (window, expected)
    # And the mode really is honoured, because SharedWindow.cpp:131 refuses
    # anything that is not wb or device and leaves the stage at 1.
    device = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                           shared_window_mib=2048, shared_window_cache="device")
    assert _values(device, "-fw_cfg")[-1].endswith(":device"), _values(device, "-fw_cfg")
    # And the base really tracks ``memory_mib`` rather than being this board's
    # address written down.  Without this the assertion above is satisfied by
    # ``0x1c0000000`` as a literal, which is correct here and an overlap with the
    # guest's own RAM on any board whose dram_size differs.
    other = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                          memory_mib=4096, shared_window_mib=1024)
    moved = next(v for v in _values(other, "-fw_cfg") if "shared-window" in v)
    assert moved == (f"name=opt/org.unixnu/shared-window,string="
                     f"{ub.SHARED_WINDOW_BASE + (4096 << 20):x}:{1024 << 20:x}:wb"), moved


def test_the_metal_clients_item_rides_with_the_window(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """The grants are a separate item, and they need the window to mean anything.

    ``QemuMetalPublish.cpp:57-70`` reads ``opt/org.unixnu/metal-clients`` and
    publishes a grant per name -- but only after it has published the
    accelerator, which it will not do without a window.  So emitting the clients
    without a window would be a claim the guest cannot act on.  The render server
    is deliberately NOT in this list: :50 hard-codes ``backboardd`` as
    ``NativeSandboxGrantProcess`` on the accelerator itself.
    """
    work = tmp_path / "run"
    argv = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                         shared_window_mib=2048,
                         metal_clients="iconservicesagent")
    item = _values(argv, "-fw_cfg")
    assert "name=opt/org.unixnu/metal-clients,string=iconservicesagent" in item, item
    # ...and without a window, nothing.
    alone = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                          metal_clients="iconservicesagent")
    assert not any("metal-clients" in token for token in alone), alone


def test_a_cache_mode_the_probe_refuses_is_refused_here(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """``wb`` or ``device``, and anything else is a refusal rather than a run.

    ``SharedWindow.cpp:130-131`` accepts exactly those two and returns with
    ``native_shared_window_stage`` still 1 on anything else.  Letting it through
    would produce a boot that looks configured and is not, which is the same
    shape as the ``--ramfb-driver`` gap.
    """
    work = tmp_path / "run"
    with pytest.raises(UniPadError) as caught:
        ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                      shared_window_mib=2048, shared_window_cache="writeback")
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "wb" in caught.value.message, caught.value


def test_the_report_says_whether_the_run_had_a_window(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """A field nothing reports absent is a field whose absence is invisible.

    ``native_shared_window_stage`` staying 0 makes the Metal device unpublished,
    which makes nothing present -- and a run that reached the kernel, mounted its
    Data volume and drew nothing is otherwise indistinguishable from one that
    never had a window.  Both directions asserted, because the obvious
    implementation (always an int) passes the first and fails the second.
    """
    work = tmp_path / "run"
    argv = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                         shared_window_mib=2048)
    report = ub.BootReport(ok=True, command=argv, shared_window_mib=2048)
    assert report.shared_window_mib == 2048
    plain = ub.BootReport(ok=True, command=argv)
    assert plain.shared_window_mib is None, plain


# -- which backend, and asking QEMU before believing it ----------------------
def test_the_file_backend_is_the_default_and_the_only_one_that_shares(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """``file`` is upstream's; ``ram`` is the diagnostic for a QEMU without it.

    Measured 2026-10-30 on this host's pinned QEMU 11.1.0: ``-object help``
    prints ``memory-backend-ram`` and nothing else, so ``memory-backend-file``
    is an *invalid object type*, QEMU exits 1, and the run leaves a
    correctly-sized ``shared.bin`` beside a 0-byte ``serial.log`` with the
    whole reason on stderr.  The fallback keeps the guest's second memory node
    at the same address, which is what the guest-side stages need, and gives up
    only the sharing -- so the two argv must differ in exactly that one object.
    """
    work = tmp_path / "run"
    shared = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                           shared_window_mib=2048)
    diagnostic = ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                               shared_window_mib=2048, shared_window_backend="ram")
    joined_shared = " ".join(shared)
    joined_ram = " ".join(diagnostic)
    assert "memory-backend-file,id=ram1,size=2048M" in joined_shared, joined_shared
    assert "share=on" in joined_shared, joined_shared
    assert "memory-backend-ram,id=ram1,size=2048M" in joined_ram, joined_ram
    assert "share=on" not in joined_ram, joined_ram
    # Everything else about the machine is the same, or the comparison the
    # fallback exists to make would be a comparison of two different machines.
    assert shared[:shared.index("-object")] == diagnostic[:diagnostic.index("-object")]
    assert (shared[shared.index("-numa"):] == diagnostic[diagnostic.index("-numa"):])


def test_a_backend_nobody_defined_is_refused_not_ignored(
    inputs: BootInputs, tmp_path: Path, qemu: Path
) -> None:
    """A typo must not silently become the diagnostic.

    ``shared_window_backend="filee"`` would otherwise fall through to the
    ``else`` branch and emit the un-shared node, which is a run that claims a
    window nothing outside the guest can see -- the exact state the field
    exists to distinguish.
    """
    work = tmp_path / "run"
    with pytest.raises(UniPadError) as caught:
        ub.build_argv(qemu, inputs, tmp_path / "esp", work, display="none",
                      shared_window_mib=2048, shared_window_backend="filee")
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "ram" in caught.value.message and "file" in caught.value.message, caught.value


def test_the_probe_asks_the_binary_rather_than_trusting_the_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registered object types are a property of the build, not the version.

    A version string cannot answer this: QEMU 11.1.0 for w64 registers
    ``memory-backend-ram`` alone, while another 11.1.0 registers
    ``memory-backend-file`` too.  The answer is read from the binary rather
    than assumed, so this test answers the probe the way this host's QEMU does
    and asserts the parse -- including that the header line, which is not an
    identifier, is not mistaken for a type.
    """
    monkeypatch.setattr(
        ub.subprocess, "run",
        _AnswersObjectHelp(
            "List of user creatable objects:\n"
            "  authz-list\n"
            "  memory-backend-ram\n"
            "  rng-egd\n"
        ).run,
    )
    found = ub.qemu_object_types(tmp_path / "qemu-system-aarch64.exe")
    assert found == {"authz-list", "memory-backend-ram", "rng-egd"}, found
    assert "memory-backend-file" not in found
    assert "List of user creatable objects:" not in found


def test_a_nonzero_object_help_answers_with_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A QEMU that refuses the question has not said yes.

    The only caller acts on the absence of a name, and a name that was never
    in a failed answer must not be treated as absent for the right reason.
    """
    monkeypatch.setattr(ub.subprocess, "run", _AnswersObjectHelp("", returncode=1).run)
    assert ub.qemu_object_types(tmp_path / "qemu-system-aarch64.exe") == set()


def test_a_binary_that_cannot_be_asked_answers_with_nothing(
    tmp_path: Path,
) -> None:
    """An empty set, not an exception and not a guess.

    The caller's question is "can I use this type", and the conservative answer
    to a question that could not be asked is no.  Raising here would turn a
    missing QEMU into a crash inside :func:`boot` instead of the named refusal
    that a caller can act on.
    """
    missing = tmp_path / "no-such-qemu"
    assert ub.qemu_object_types(missing) == set()


def test_a_window_this_qemu_cannot_build_is_refused_before_anything_starts(
    inputs: BootInputs, tmp_path: Path, qemu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight that would have saved four minutes of polling a corpse.

    The 2026-10-30 run wrote a 2 GiB ``shared.bin``, produced a 0-byte serial
    log, and spent 240 seconds reporting ``native_legacy_stage=None`` while
    QEMU had already exited.  ``boot()`` did record ``QEMU_LAUNCH_FAILED`` and
    the stderr tail; the caller ignored both.  Refusing here means the run
    never begins, and the message names the remedy.
    """
    work = tmp_path / "run"
    monkeypatch.setattr(ub, "qemu_object_types", lambda _qemu, timeout=30.0: {"memory-backend-ram"})
    with pytest.raises(UniPadError) as caught:
        ub.boot(inputs, work, qemu=qemu, device_dir=_synthetic_device(tmp_path),
                seconds=5, shared_window_mib=2048)
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    message = caught.value.message
    assert "memory-backend-file" in message, message
    assert "shared_window_backend='ram'" in message, message
    # Nothing was started, so nothing was staged and no backing file exists.
    assert not (work / "shared.bin").exists(), "a refused run must not leave a window"


def test_the_diagnostic_backend_skips_the_preflight_and_seeds_no_file(
    inputs: BootInputs, tmp_path: Path, qemu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ram`` is the answer to a QEMU without the file backend, so it must not
    be refused by the probe that exists for the file backend.

    And it must not leave a 2 GiB artefact behind.  This goes through
    :func:`boot` rather than :func:`build_argv`, because the seeding lives in
    ``boot`` -- asserting it on ``build_argv`` alone is how the first version of
    this test passed against a mutant that seeded the file anyway.
    """
    work = tmp_path / "run"
    monkeypatch.setattr(ub, "qemu_object_types", lambda _qemu, timeout=30.0: set())
    launched: list[list[str]] = []
    monkeypatch.setattr(ub.subprocess, "Popen", _already_exited(launched))
    seen: list[Any] = []
    # ``_watch`` hands its report back, which is the report ``boot`` writes:
    # capturing it here asserts on the run's own record rather than on a file
    # some other test also writes.
    monkeypatch.setattr(ub, "_watch", lambda *a, **k: (seen.append(a[1]), a[1])[1])
    ub.boot(inputs, work, qemu=qemu, device_dir=_synthetic_device(tmp_path),
            seconds=0, shared_window_mib=2048, shared_window_backend="ram")
    assert not (work / "shared.bin").exists(), "a ram window left a file nothing reads"
    # And the run still says which machine it was, because a report that
    # records the size but not the backend reads the same as its opposite.
    assert seen and seen[0].shared_window_backend == "ram", seen[0]
