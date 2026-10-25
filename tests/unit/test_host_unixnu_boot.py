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
import socket
import struct
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import unixnu_boot as ub
from unipad.host.unixnu_boot import BootInputs, stage_esp

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
    """
    firmware = tmp_path / "edk2-aarch64-code.fd"
    firmware.write_bytes(b"\0" * 4096)
    kernel = tmp_path / "kernel.macho"
    kernel.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic kernelcache" * 8)
    devicetree = tmp_path / "kernel.macho.adt"
    devicetree.write_bytes(b"synthetic device tree")
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
    assert len(real_spec["kernel_args"]) == 23, real_spec


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
    assert _values(argv, "-device") == [
        "virtio-blk-pci,drive=esp",
        "ramfb",
        "qemu-xhci",
        "usb-kbd",
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
        "virtio-blk-pci,drive=esp",
        "virtio-blk-device,drive=osroot",
        "ramfb",
        "qemu-xhci",
        "usb-kbd",
    ], argv
    # The ESP stays on PCI: EDK II has to find it on the virtual PCI bus
    # (BdsDxe loads Boot0001 from PciRoot(0x0)/Pci(0x1,0x0)), so the two disks
    # are on different transports by necessity, not by oversight.
    assert "virtio-blk-pci,drive=osroot" not in argv, argv
    # ...and upstream's global, without which the MMIO transport stays in legacy
    # mode and the driver's register offsets do not hold (run-qemu.py:203-208).
    assert _flag(argv, "-global") == "virtio-mmio.force-legacy=false", argv
    # After the ESP, so the ESP keeps the device number every earlier run recorded.
    assert argv.index("virtio-blk-pci,drive=esp") < argv.index("virtio-blk-device,drive=osroot"), argv


def test_without_a_root_image_the_argv_is_exactly_what_it_was(
    inputs: BootInputs, tmp_path: Path
) -> None:
    """The parameter is additive, not a rewrite of the device list.

    The guard this pins: a change that *reordered* the ESP drive, or emitted the
    root drive with a different spelling, would still satisfy every assertion
    about the root drive itself while silently changing the machine every
    earlier run described.  So the no-root argv is asserted to be the root argv
    with exactly six tokens removed -- the ``-global`` pair, and the drive pair.
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
    assert len(with_root) - len(without) == 6, (len(with_root), len(without))
    # The six added tokens, removed, leave the argv exactly as it was.  A change
    # that reordered anything would still pass every assertion above while
    # changing the machine every earlier run described.
    added_at = next(i for i, token in enumerate(with_root) if "id=osroot" in token)
    assert with_root[added_at - 3 : added_at + 3] == [
        "-global",
        "virtio-mmio.force-legacy=false",
        "-drive",
        with_root[added_at],
        "-device",
        "virtio-blk-device,drive=osroot",
    ], with_root[added_at - 3 : added_at + 3]
    assert with_root[: added_at - 3] + with_root[added_at + 3 :] == without, without


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
