"""``unipad.host.adt`` against a synthetic tree built on this host.

Why this file exists
--------------------
``make-qemu-adt.py`` needs ``qemu-system-aarch64`` to dump the machine's own
device tree, so the *generation* half of the module cannot run here.  Everything
around the bytes can: the board.toml-to-flag mapping, the seed derivation, the
merged build tree, and -- most of all -- reading a generated tree back and
checking it carries the identity the board asked for.  So this file writes small
ADT-shaped trees by hand with :mod:`struct` and runs the real
:func:`~unipad.host.adt.verify_adt` over them.

The defects these tests are written after
-----------------------------------------
* **``chip_id`` is hex, not decimal.**  ``[payload.adt] chip_id = "207"`` is
  ``0x207`` = 519, because ``make-qemu-adt.py:68`` reads it with
  ``int(value, 16)``.  ``int("207", 0)`` is 207.  The disagreement is invisible
  until the tree is read back, so ``test_the_boards_chip_id_is_hex_not_decimal``
  pins the number the tool actually writes.
* **A board with no ``[payload.adt]`` was answered with defaults.**  The QEMU
  board *needs* a generated tree; a board that declares none would get one
  nobody asked for.  Both that and a missing ``[payload] dram_size`` are
  refusals here, not fallbacks.
* **``prepare_tree`` merged nothing.**  ``make-qemu-adt.py`` lives in the device
  repository but invokes ``scripts/qemu-entry/make-dt.py``, which lives only in
  UniXNU.  A tree that has one and not the other cannot run, and the failure
  arrives as a Python traceback from a subprocess.

What is *not* proven here
-------------------------
That ``make-qemu-adt.py`` produces a tree at all: that needs QEMU and a macOS
runner.  What is proven is that the arguments handed to it are the board's, in
the board's order, and that whatever comes back is checked against the board
before the build believes it.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import pytest

from unipad.host import adt
from unipad.host.adt import BoardAdt
from unipad.errors import ErrorCode, UniPadError

ROOT = Path(__file__).resolve().parents[2]
REAL_DEVICE = ROOT / "vendor" / "UniXNU-qemu"
REAL_UNIXNU = ROOT / "vendor" / "UniXNU"

#: The board's ``[payload.adt] chip_id``, spelled as a hex string with no 0x.
CHIP_ID = "207"
#: ...and the number the tool writes into ``/chosen/chip-id``.  519, not 207:
#: ``int("207", 16)`` is what ``make-qemu-adt.py:68`` does, and ``int("207", 0)``
#: is the mistake this constant exists to make impossible to forget.
CHIP_VALUE = 0x207
assert CHIP_VALUE == 519

#: ``[payload] dram_size``, the 6 GiB the machine gets.
DRAM_SIZE = 0x180000000

#: A platform seed.  Any 64-bit value is accepted; this one is chosen so its hex
#: spelling is obviously not a digest of anything.  The spelling is ``f"{v:x}"``
#: because that is what :func:`verify_adt` compares against -- a leading zero
#: would make the two sides disagree about a seed that is byte-for-byte right.
SEED_INT = 0x0123456789ABCDEF
SEED_HEX = f"{SEED_INT:x}"
assert SEED_HEX == "123456789abcdef"

VOLUME_UUID = "04769D26-2CA0-4825-B443-E2E5F88300F2"
OWNED_MEMORY = "0x100000000"
ADT_TOOL = "scripts/make-qemu-adt.py"

#: The real board's ``[payload.adt]`` table, as TOML lines.  A synthetic board
#: is this table with keys dropped or overridden, so a test that mutates one key
#: cannot accidentally mutate the rest.
ADT_TABLE: dict[str, str] = {
    "tool": f'"{ADT_TOOL}"',
    "chip_id": f'"{CHIP_ID}"',
    "owned_memory_size": f'"{OWNED_MEMORY}"',
    "root_match_volume_uuid": f'"{VOLUME_UUID}"',
    "cpu_regs": '"0"',
    "packed_data_ramdisk": "true",
    "ipad_identity": "true",
    "unique_chip_id": '"required"',
}


# ---------------------------------------------------------------------------
# a synthetic device tree
# ---------------------------------------------------------------------------
def _node(
    name: str,
    properties: Sequence[tuple[str, bytes]] = (),
    children: Sequence[list] = (),
) -> list:
    """One ADT node, in the ``[name, properties, children]`` form both parsers use.

    The ``name`` property comes first because that is where ``make-dt.py`` puts
    it, and ``parse`` looks it up by key rather than by position -- so the order
    is cosmetic here, but keeping it identical to the tool's keeps a diff
    against a real tree readable.
    """
    return [name, [("name", name.encode() + b"\0"), *properties], list(children)]


def _serialize(node: list) -> bytes:
    """The byte form :func:`unipad.host.adt.parse_adt` reads.

    A transcription of ``make-qemu-adt.py:serialize``: a property count and a
    child count, then that many ``[32-byte key][u32 length][padded value]``
    triples, then that many children.
    """
    _, properties, children = node
    out = struct.pack("<II", len(properties), len(children))
    for key, data in properties:
        out += struct.pack("<32sI", key.encode(), len(data)) + data + bytes(-len(data) % 4)
    return out + b"".join(_serialize(child) for child in children)


def _tree(
    *,
    chip_id: int = CHIP_VALUE,
    unique_chip_id: Optional[int] = SEED_INT,
    model: str = "iPad7,1",
    arm_io_children: Sequence[list] = (),
    with_chosen: bool = True,
    root_properties: Sequence[tuple[str, bytes]] = (),
) -> bytes:
    """A whole ``kernel.macho.adt``: a root, a ``chosen``, a ``defaults``, an ``arm-io``.

    Every field the module reads is present with a *correct* value unless the
    test says otherwise, so a refusal can only be about the one thing the test
    changed.  That is the difference between a test that pins a rule and a test
    that pins "some tree was refused".
    """
    chosen_properties: list[tuple[str, bytes]] = []
    if chip_id is not None:
        chosen_properties.append(("chip-id", struct.pack("<I", chip_id)))
    if unique_chip_id is not None:
        chosen_properties.append(("unique-chip-id", struct.pack("<Q", unique_chip_id)))
    chosen = _node(
        "chosen",
        [
            *chosen_properties,
            ("root-matching", VOLUME_UUID.encode()),
            ("certificate-production-status", struct.pack("<I", 0)),
            ("effective-security-mode-sep", struct.pack("<I", 1)),
        ],
    )
    defaults = _node(
        "defaults",
        [
            ("sleep-disabled", struct.pack("<I", 1)),
            ("kern.maxvnodes", struct.pack("<I", (4 << 30) // 65536 + 1024)),
        ],
    )
    arm_io = _node("arm-io", [("compatible", b"arm-io\0")], arm_io_children)
    cpu0 = _node("cpu0", [("timebase-frequency", struct.pack("<I", 24_000_000))])
    product = _node(
        "product", [("product-name", b"iPad Pro (12.9-inch) (2nd generation)\0")]
    )
    children: list[list] = [defaults, arm_io, cpu0, product]
    if with_chosen:
        children.insert(0, chosen)
    root = _node(
        "",
        [
            ("model", model.encode() + b"\0"),
            ("target-type", b"J120\0"),
            *root_properties,
        ],
        children,
    )
    return _serialize(root)


def _write_tree(directory: Path, blob: bytes, name: str = "kernel.macho.adt") -> Path:
    path = directory / name
    path.write_bytes(blob)
    return path


# ---------------------------------------------------------------------------
# a synthetic board.toml
# ---------------------------------------------------------------------------
def _write_board(
    directory: Path,
    *,
    with_adt: bool = True,
    with_dram_size: bool = True,
    dram_size: Optional[str] = None,
    drop: Iterable[str] = (),
    overrides: Optional[dict[str, str]] = None,
) -> Path:
    """Write a ``board.toml`` shaped like the QEMU board's.

    ``drop`` removes keys from ``[payload.adt]`` and ``overrides`` replaces them,
    so each test states the one thing that differs from the real board.  The
    ``[payload]`` block always carries ``loader`` and the trust-cache list the
    real one does, so a test that only cares about the ADT table is not also
    silently changing the payload block.
    """
    table = {key: value for key, value in ADT_TABLE.items() if key not in set(drop)}
    table.update(overrides or {})
    text = '[board]\nname = "synthetic"\n\n[payload]\n'
    if with_dram_size:
        text += f'dram_size = "{DRAM_SIZE:#x}"\n'
    elif dram_size is not None:
        text += f"dram_size = {dram_size}\n"
    text += 'loader = "driver"\ntitle = "synthetic board"\n'
    if with_adt:
        text += "\n[payload.adt]\n"
        text += "".join(f"{key} = {value}\n" for key, value in table.items())
    path = directory / "board.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _synthetic_board(tmp_path: Path, **kwargs: Any) -> BoardAdt:
    """A :class:`BoardAdt` read from a synthetic board.toml in ``tmp_path``."""
    _write_board(tmp_path, **kwargs)
    return adt.read_board_adt(tmp_path)


@pytest.fixture(scope="module")
def real_board() -> BoardAdt:
    """The checked-in QEMU board, or a skip when the vendor tree is absent."""
    if not (REAL_DEVICE / "board.toml").is_file():
        pytest.skip("vendor/UniXNU-qemu is not present in this checkout")
    return adt.read_board_adt(REAL_DEVICE)


def _refused(call, *args: Any, **kwargs: Any) -> UniPadError:
    """Run ``call`` and return the refusal, failing loudly if it does not refuse."""
    with pytest.raises(UniPadError) as caught:
        call(*args, **kwargs)
    return caught.value


# ---------------------------------------------------------------------------
# the real board's declaration
# ---------------------------------------------------------------------------
def test_the_real_board_declares_what_the_tool_needs(real_board: BoardAdt) -> None:
    """Every value the ADT step forwards, pinned against the checked-in board.

    This is the one test in the file that touches the repository's real data,
    and it is here because every synthetic test below is a mutation of it.  If
    board.toml changes, this is what says so -- with the value, not with a
    stack trace from three steps later.
    """
    assert real_board.tool == ADT_TOOL, real_board
    assert real_board.chip_id == CHIP_ID, real_board
    assert real_board.owned_memory_size == OWNED_MEMORY, real_board
    assert real_board.root_match_volume_uuid == VOLUME_UUID, real_board
    assert real_board.cpu_regs == "0", real_board
    assert real_board.dram_size == DRAM_SIZE, real_board
    assert real_board.ipad_identity is True, real_board
    assert real_board.packed_data_ramdisk is True, real_board
    assert real_board.requires_seed is True, real_board
    # The board does *not* declare a PSCI method, so none may be forwarded: the
    # tool's own default is the one virt-without-EL2 needs.
    assert real_board.psci_method is None, real_board
    # Absolute, because the tool runs with the tree as its cwd.
    assert real_board.device_dir.is_absolute(), real_board
    assert real_board.tree_dir.is_absolute(), real_board


def test_the_boards_chip_id_is_hex_not_decimal(real_board: BoardAdt, tmp_path: Path) -> None:
    """``chip_id = "207"`` is 0x207 = 519, and the tree must say 519.

    The bug that cost an hour: ``int("207", 0)`` is 207, and a caller that read
    the board that way produced a tree whose ``/chosen/chip-id`` disagreed with
    the board -- which only surfaces when the tree is read back, long after the
    tool reported success.  ``verify_adt`` is what catches it, so it is given a
    tree carrying the *correct* 519 here and must accept it; the wrong reading is
    pinned by ``test_a_chip_id_that_disagrees_with_the_board_is_refused``.
    """
    assert int(CHIP_ID, 16) == CHIP_VALUE == 519
    assert int(CHIP_ID, 0) == 207, "the decimal reading is what the bug produced"
    assert int(CHIP_ID, 0) != int(CHIP_ID, 16)
    path = _write_tree(tmp_path, _tree(chip_id=CHIP_VALUE))
    record = adt.verify_adt(path, real_board, seed=SEED_HEX)
    assert record["chip_id"] == CHIP_VALUE, record
    assert record["unique_chip_id"] == SEED_INT, record


def test_a_board_with_no_adt_table_is_refused(tmp_path: Path) -> None:
    """No ``[payload.adt]`` is a refusal, not a set of defaults.

    The QEMU board needs a *generated* tree.  Answering a board that declares
    none with defaults would hand the build a tree nobody asked for -- and the
    failure would surface as a guest with no platform identity, not here.
    """
    _write_board(tmp_path, with_adt=False)
    error = _refused(adt.read_board_adt, tmp_path)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "[payload.adt]" in error.message, error


def test_a_board_with_no_dram_size_is_refused(tmp_path: Path) -> None:
    """``--dram-size`` is ``required=True`` in the tool, so it is required here.

    It is also the size of the machine the tool is asked to dump, so a default
    would describe a different machine than the one that boots.
    """
    _write_board(tmp_path, with_dram_size=False)
    error = _refused(adt.read_board_adt, tmp_path)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "dram_size" in error.message, error


def test_a_board_with_no_chip_id_is_refused(tmp_path: Path) -> None:
    """``make-qemu-adt.py:76-77`` refuses ``--unique-chip-id`` without ``--chip-id``."""
    _write_board(tmp_path, drop=("chip_id",))
    error = _refused(adt.read_board_adt, tmp_path)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "chip_id" in error.message, error


def test_a_chip_id_that_is_not_hex_is_refused(tmp_path: Path) -> None:
    """The property is four bytes; a value that does not fit is caught here."""
    _write_board(tmp_path, overrides={"chip_id": '"not-hex"'})
    error = _refused(adt.read_board_adt, tmp_path)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "hexadecimal" in error.message, error


def test_a_chip_id_larger_than_the_property_is_refused(tmp_path: Path) -> None:
    """``/chosen/chip-id`` is a u32; a bigger value cannot be written into it."""
    _write_board(tmp_path, overrides={"chip_id": '"1ffffffff"'})
    error = _refused(adt.read_board_adt, tmp_path)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "4-byte" in error.message, error


def test_a_dram_size_that_is_not_a_number_is_refused(tmp_path: Path) -> None:
    """The string spelling is hex; a non-number is a refusal, not a zero."""
    _write_board(tmp_path, with_dram_size=False, dram_size='"six gibibytes"')
    error = _refused(adt.read_board_adt, tmp_path)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "dram_size" in error.message, error


def test_a_dram_size_that_is_a_bare_integer_is_accepted(tmp_path: Path) -> None:
    """``0x180000000``, ``"0x180000000"`` and ``6442450944`` are one fact."""
    _write_board(tmp_path, with_dram_size=False, dram_size="6442450944")
    assert _synthetic_board(tmp_path).dram_size == DRAM_SIZE


def test_a_missing_board_toml_is_refused(tmp_path: Path) -> None:
    """Named by its final component, which is all an error may carry (PRD 9.2)."""
    error = _refused(adt.read_board_adt, tmp_path / "nowhere")
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "board.toml not found" in error.message, error
    assert error.details.get("path") == "board.toml", error.details


# ---------------------------------------------------------------------------
# the seed
# ---------------------------------------------------------------------------
def test_the_seed_is_deterministic_and_unique_per_pin() -> None:
    """The same pin gives the same identity; a different pin gives a different one.

    Reproducibility is the reason the seed is *derived* rather than drawn: a
    random value would make every build of one pin a different guest, and a
    value in Git would make every checkout the same guest.
    """
    commit = "f03599a" + "0" * 33
    first = adt.derive_seed(commit)
    assert first == adt.derive_seed(commit), "the same pin derived two seeds"
    assert len(first) == 8, first
    assert adt.derive_seed(commit + "1") != first, "two pins shared a seed"
    assert adt.derive_seed("") != first


def test_the_seed_is_not_a_plain_digest_of_the_commit() -> None:
    """The domain separator is part of the derivation, not decoration.

    ``SEED_LABEL`` is what stops the seed being mistaken for a hash of the
    commit -- and, more practically, what stops a future refactor to
    ``sha256(commit)`` from looking harmless.
    """
    commit = "f03599a" + "0" * 33
    plain = hashlib.sha256(commit.encode()).digest()[:8]
    assert adt.derive_seed(commit) != plain
    assert adt.derive_seed(commit) == hashlib.sha256(
        adt.SEED_LABEL + b"\0" + commit.encode("ascii")
    ).digest()[:8]


def test_a_board_that_requires_a_seed_refuses_without_one(real_board: BoardAdt, tmp_path: Path) -> None:
    """``unique_chip_id = "required"`` means the caller must supply one.

    The seed is deliberately in no repository, so the alternative to refusing is
    inventing one -- which would boot a guest whose identity nobody recorded.
    """
    assert real_board.requires_seed is True
    error = _refused(adt.adt_command, real_board, tmp_path / "kernel.macho.adt")
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "platform seed" in error.message, error


def test_a_board_that_does_not_require_a_seed_needs_none(tmp_path: Path) -> None:
    """The literal string ``"required"`` is the switch; anything else is not."""
    board = _synthetic_board(tmp_path, overrides={"unique_chip_id": '"unused"'})
    assert board.requires_seed is False
    command = adt.adt_command(board, tmp_path / "kernel.macho.adt")
    assert "--unique-chip-id" not in command, command
    # ...and the tree is then not required to carry one either.
    path = _write_tree(tmp_path, _tree(unique_chip_id=None))
    assert adt.verify_adt(path, board)["chip_id"] == CHIP_VALUE


def test_a_board_that_omits_unique_chip_id_needs_no_seed(tmp_path: Path) -> None:
    """An absent key is the same answer as a non-``required`` one."""
    board = _synthetic_board(tmp_path, drop=("unique_chip_id",))
    assert board.requires_seed is False


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------
def test_the_command_is_the_boards_own_in_the_boards_own_order(
    real_board: BoardAdt, tmp_path: Path
) -> None:
    """The whole argv, asserted as a list.

    Asserted whole rather than flag-by-flag because the failure mode this
    prevents is a *missing* flag, and a per-flag assertion passes when a flag is
    absent.  ``--dram-size`` in particular is ``required=True`` in the tool: the
    macOS runner used to pass only ``--output`` and ``--unique-chip-id``, so that
    call could only ever fail.
    """
    output = tmp_path / "kernel.macho.adt"
    command = adt.adt_command(real_board, output, seed=SEED_HEX)
    assert command == [
        "python3",
        str(real_board.tree_dir / ADT_TOOL),
        "--output", str(output),
        "--dram-size", str(DRAM_SIZE),
        "--owned-memory-size", OWNED_MEMORY,
        "--root-match-volume-uuid", VOLUME_UUID,
        "--chip-id", CHIP_ID,
        "--cpu-regs", "0",
        "--unique-chip-id", SEED_HEX,
        "--ipad-identity",
    ], command


def test_the_command_carries_no_psci_method_for_a_board_that_omits_it(
    real_board: BoardAdt, tmp_path: Path
) -> None:
    """``--psci-method`` is only emitted when the board declares one.

    The tool's default is ``hvc``, which is what virt without EL2 uses; sending
    the flag anyway would be this module deciding the board's machine.
    """
    command = adt.adt_command(real_board, tmp_path / "kernel.macho.adt", seed=SEED_HEX)
    assert "--psci-method" not in command, command
    # ...and a board that *does* declare one gets it, in the same slot.
    board = _synthetic_board(tmp_path, overrides={"psci_method": '"smc"'})
    with_method = adt.adt_command(board, tmp_path / "kernel.macho.adt", seed=SEED_HEX)
    assert with_method[with_method.index("--psci-method") + 1] == "smc", with_method


def test_the_ramdisk_and_trust_cache_flags_follow_the_boards_own_spelling(
    real_board: BoardAdt, tmp_path: Path
) -> None:
    """``packed_data_ramdisk`` says *which* ramdisk flag, not *whether*.

    The board sets it, so a data ramdisk is ``--packed-data-ramdisk`` and not
    ``--data-ramdisk``; with the key absent the plain spelling is the only one
    that is true.  ``--trust-cache`` has no such pair and is emitted on request.
    """
    output = tmp_path / "kernel.macho.adt"
    packed = adt.adt_command(real_board, output, seed=SEED_HEX, data_ramdisk=True)
    assert "--packed-data-ramdisk" in packed, packed
    assert "--data-ramdisk" not in packed, packed
    # The switches come last, so the ramdisk flag is the one before them.
    assert packed[-2:] == ["--packed-data-ramdisk", "--ipad-identity"], packed

    plain_board = _synthetic_board(tmp_path, drop=("packed_data_ramdisk",))
    plain = adt.adt_command(plain_board, output, seed=SEED_HEX, data_ramdisk=True)
    assert "--data-ramdisk" in plain, plain
    assert "--packed-data-ramdisk" not in plain, plain

    for command in (packed, plain):
        assert "--trust-cache" not in command, command
    with_cache = adt.adt_command(
        real_board, output, seed=SEED_HEX, data_ramdisk=True, trust_cache=True
    )
    assert with_cache[-2:] == ["--trust-cache", "--ipad-identity"], with_cache


def test_no_ramdisk_flag_is_emitted_when_no_ramdisk_is_supplied(
    real_board: BoardAdt, tmp_path: Path
) -> None:
    """The board's ``packed_data_ramdisk`` is not a request for one."""
    command = adt.adt_command(real_board, tmp_path / "kernel.macho.adt", seed=SEED_HEX)
    assert "--packed-data-ramdisk" not in command, command
    assert "--data-ramdisk" not in command, command


def test_a_board_without_ipad_identity_emits_no_ipad_flag(tmp_path: Path) -> None:
    """``--ipad-identity`` changes ``/model``; it is not a default."""
    board = _synthetic_board(tmp_path, overrides={"ipad_identity": "false"})
    command = adt.adt_command(board, tmp_path / "kernel.macho.adt", seed=SEED_HEX)
    assert "--ipad-identity" not in command, command


def test_the_tool_path_is_absolute_under_the_tree(tmp_path: Path) -> None:
    """The tool is run with the tree as its cwd, so its path cannot be relative."""
    board = _synthetic_board(tmp_path)
    command = adt.adt_command(board, tmp_path / "kernel.macho.adt", seed=SEED_HEX)
    tool = Path(command[1])
    assert tool.is_absolute(), tool
    assert tool.name == "make-qemu-adt.py", tool


# ---------------------------------------------------------------------------
# reading a tree back
# ---------------------------------------------------------------------------
def test_a_synthetic_tree_round_trips_through_the_parser(tmp_path: Path) -> None:
    """The parser is a transcription of the tool's, so a tree it wrote reads back.

    Proving the round trip on a *hand-built* tree is what makes the refusals
    below meaningful: without it, "the tree was refused" could be "the tree was
    never parsed".
    """
    blob = _tree()
    root, end = adt.parse_adt(blob)
    assert end == len(blob), (end, len(blob))
    assert root[0] == "", root[0]
    chosen = adt.find_node(root, "chosen")
    assert chosen is not None
    assert adt.node_property(chosen, "chip-id") == struct.pack("<I", CHIP_VALUE)
    assert adt.node_property(chosen, "unique-chip-id") == struct.pack("<Q", SEED_INT)
    assert adt.node_property(adt.find_node(root, "defaults"), "sleep-disabled") == struct.pack("<I", 1)
    assert adt.find_node(root, "nobody") is None
    assert adt.node_property(chosen, "absent") is None


def test_a_tree_that_carries_the_boards_identity_is_accepted(
    real_board: BoardAdt, tmp_path: Path
) -> None:
    """The whole record, so a change to what is *reported* also breaks a test."""
    path = _write_tree(tmp_path, _tree())
    record = adt.verify_adt(path, real_board, seed=SEED_HEX)
    assert record["chip_id"] == CHIP_VALUE, record
    assert record["unique_chip_id"] == SEED_INT, record
    assert record["model"] == "iPad7,1", record
    assert record["target_type"] == "J120", record
    assert record["sleep_disabled"] is True, record
    assert record["maxvnodes"] == (4 << 30) // 65536 + 1024, record
    assert record["timebase_hz"] == 24_000_000, record
    assert record["root_matching"] == VOLUME_UUID, record
    assert record["product_name"] == "iPad Pro (12.9-inch) (2nd generation)", record
    assert record["bytes"] == path.stat().st_size, record
    assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(), record


def test_a_tree_without_an_ipad_identity_is_accepted_when_the_board_does_not_ask(
    tmp_path: Path,
) -> None:
    """The iPad identity is the board's request, so a plain tree is fine without it."""
    board = _synthetic_board(tmp_path, overrides={"ipad_identity": "false"})
    path = _write_tree(tmp_path, _tree(model="QEMU virt"))
    record = adt.verify_adt(path, board, seed=SEED_HEX)
    assert record["model"] == "QEMU virt", record
    assert "product_name" not in record, record


def test_a_tree_with_trailing_bytes_is_refused(tmp_path: Path) -> None:
    """The tool itself refuses this (``make-qemu-adt.py:126``), so the check stays.

    Trailing bytes mean the writer and the reader disagree about where the tree
    ends, which is a truncation or a concatenation -- either way the tree is not
    what was verified.
    """
    board = _synthetic_board(tmp_path)
    path = _write_tree(tmp_path, _tree() + b"\0" * 8)
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "trailing bytes" in error.message, error
    assert error.details.get("parsed") == path.stat().st_size - 8, error.details


def test_a_tree_with_no_chosen_node_is_refused(tmp_path: Path) -> None:
    """``/chosen`` is where the identity lives; a tree without one has none."""
    board = _synthetic_board(tmp_path)
    path = _write_tree(tmp_path, _tree(with_chosen=False))
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "no /chosen node" in error.message, error


def test_a_chip_id_of_the_wrong_length_is_refused(tmp_path: Path) -> None:
    """The property is a u32; two bytes is a different property, not a shorter one."""
    board = _synthetic_board(tmp_path)
    blob = _tree()
    root, _ = adt.parse_adt(blob)
    chosen = adt.find_node(root, "chosen")
    chosen[1] = [(k, struct.pack("<H", CHIP_VALUE) if k == "chip-id" else v) for k, v in chosen[1]]
    path = _write_tree(tmp_path, _serialize(root))
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "4-byte" in error.message, error
    assert error.details.get("found") == 2, error.details


def test_a_chip_id_that_disagrees_with_the_board_is_refused(tmp_path: Path) -> None:
    """The decimal reading of ``"207"``, which is the bug this file is about."""
    board = _synthetic_board(tmp_path)
    path = _write_tree(tmp_path, _tree(chip_id=int(CHIP_ID, 0)))
    assert int(CHIP_ID, 0) != CHIP_VALUE
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "does not match board.toml" in error.message, error
    assert error.details.get("found") == 207, error.details
    assert error.details.get("expected") == CHIP_VALUE, error.details


def test_an_absent_chip_id_is_refused(tmp_path: Path) -> None:
    """A tree that parses but carries no chip-id is not a tree this board asked for."""
    board = _synthetic_board(tmp_path)
    path = _write_tree(tmp_path, _tree(chip_id=None))
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "4-byte" in error.message, error
    assert error.details.get("found") is None, error.details


def test_a_unique_chip_id_that_is_not_the_seed_passed_is_refused(tmp_path: Path) -> None:
    """The seed is the identity; a different one is a different guest.

    This is the check that stops a tree built for one checkout being reused for
    another, which would otherwise look like a successful boot of the wrong
    device.
    """
    board = _synthetic_board(tmp_path)
    path = _write_tree(tmp_path, _tree(unique_chip_id=0xDEADBEEFDEADBEEF))
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "is not the platform seed" in error.message, error
    assert error.details.get("found") == "deadbeefdeadbeef", error.details


def test_a_unique_chip_id_of_the_wrong_length_is_refused(tmp_path: Path) -> None:
    """Eight bytes, because ``make-qemu-adt.py:67`` packs it as ``<Q``."""
    board = _synthetic_board(tmp_path)
    blob = _tree()
    root, _ = adt.parse_adt(blob)
    chosen = adt.find_node(root, "chosen")
    chosen[1] = [
        (k, struct.pack("<I", 0x207) if k == "unique-chip-id" else v) for k, v in chosen[1]
    ]
    path = _write_tree(tmp_path, _serialize(root))
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "8-byte" in error.message, error
    assert error.details.get("found") == 4, error.details


def test_a_seed_is_optional_when_the_board_does_not_require_one(tmp_path: Path) -> None:
    """A tree with no ``unique-chip-id`` is only a problem when one was promised."""
    board = _synthetic_board(tmp_path, overrides={"unique_chip_id": '"unused"'})
    path = _write_tree(tmp_path, _tree(unique_chip_id=None))
    record = adt.verify_adt(path, board)
    assert "unique_chip_id" not in record, record


def test_a_tree_that_still_carries_qemus_ufs_nodes_is_refused(tmp_path: Path) -> None:
    """``make-qemu-adt.py:127`` removes them; their presence means the wrong tree.

    QEMU's own dumped tree has a UFS controller the guest has no driver for, so
    a tree that still names one was not produced by this board's tool.
    """
    board = _synthetic_board(tmp_path)
    path = _write_tree(
        tmp_path, _tree(arm_io_children=[_node("ufshc", [("compatible", b"ufshc\0")])])
    )
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "UFS nodes" in error.message, error
    assert error.details.get("nodes") == ["ufshc"], error.details


def test_a_tree_without_the_ipad_identity_the_board_asked_for_is_refused(
    tmp_path: Path,
) -> None:
    """``--ipad-identity`` is a request; a tree that ignores it is refused.

    MobileGestalt reads ``/model``, so a tree without it boots a guest that
    reports itself as a QEMU machine -- which userland then treats as an unknown
    device rather than as an iPad.
    """
    board = _synthetic_board(tmp_path)
    assert board.ipad_identity is True
    path = _write_tree(tmp_path, _tree(model="QEMU virt"))
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "iPad identity" in error.message, error
    assert error.details.get("model") == "QEMU virt", error.details
    assert error.details.get("expected") == "iPad7,1", error.details


def test_a_truncated_tree_is_refused_rather_than_parsed_partially(tmp_path: Path) -> None:
    """A truncation says so, instead of reading as a tree with fewer nodes."""
    board = _synthetic_board(tmp_path)
    blob = _tree()
    path = _write_tree(tmp_path, blob[: len(blob) // 2])
    error = _refused(adt.verify_adt, path, board, SEED_HEX)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert "truncated" in error.message, error


# ---------------------------------------------------------------------------
# the merged build tree
# ---------------------------------------------------------------------------
def test_prepare_tree_merges_both_repositories(tmp_path: Path) -> None:
    """Neither repository alone can run the tool.

    ``make-qemu-adt.py`` is the device repository's; ``scripts/qemu-entry/
    make-dt.py``, which it invokes at line 82, is UniXNU's.  The merged tree has
    both, and the device's spelling of a script it shares wins because it is
    copied second.
    """
    if not (REAL_DEVICE / "scripts").is_dir() or not (REAL_UNIXNU / "scripts").is_dir():
        pytest.skip("the vendor repositories are not present in this checkout")
    tree = adt.prepare_tree(REAL_UNIXNU, REAL_DEVICE, tmp_path / "tree")
    assert tree == (tmp_path / "tree").resolve()
    assert (tree / ADT_TOOL).is_file(), "the device repository's tool is missing"
    assert (tree / "scripts" / "qemu-entry" / "make-dt.py").is_file(), (
        "UniXNU's make-dt.py is missing; the tool cannot run"
    )
    # A script only UniXNU carries, and one only the device carries: the merge
    # is a union, not an overwrite.
    assert (tree / "scripts" / "qemu-entry" / "entry.S").is_file()
    assert (tree / "scripts" / "run-qemu.py").is_file()
    # ...and the driver package itself is not copied into the tree.
    assert not (tree / "scripts" / "unixnu").exists(), "the driver package was copied in"


def test_prepare_tree_skips_the_driver_package_and_bytecode_caches(tmp_path: Path) -> None:
    """``tree.py:_copy_scripts`` skips both, for the same reason: shadowing.

    The scripts locate the tree from their own real path, so a copy of the driver
    inside the tree would shadow the real one; and a ``__pycache__`` copied from
    a different interpreter is a stale-code bug waiting to happen.
    """
    unixnu = tmp_path / "unixnu"
    (unixnu / "scripts" / "qemu-entry").mkdir(parents=True)
    (unixnu / "scripts" / "qemu-entry" / "make-dt.py").write_text("# unixnu make-dt\n", encoding="utf-8")
    (unixnu / "scripts" / "unixnu" / "payload").mkdir(parents=True)
    (unixnu / "scripts" / "unixnu" / "payload" / "payload.py").write_text("# driver\n", encoding="utf-8")
    (unixnu / "scripts" / "__pycache__").mkdir()
    (unixnu / "scripts" / "__pycache__" / "make-dt.cpython-312.pyc").write_bytes(b"\0")

    device = tmp_path / "device"
    (device / "scripts").mkdir(parents=True)
    (device / "scripts" / "make-qemu-adt.py").write_text("# device tool\n", encoding="utf-8")

    tree = adt.prepare_tree(unixnu, device, tmp_path / "tree")
    assert (tree / "scripts" / "qemu-entry" / "make-dt.py").is_file()
    assert (tree / ADT_TOOL).is_file()
    assert not (tree / "scripts" / "unixnu").exists(), "the driver package was copied in"
    assert not list((tree / "scripts").rglob("__pycache__")), "a bytecode cache was copied in"


def test_prepare_tree_lets_the_device_repository_win(tmp_path: Path) -> None:
    """The device is copied second, so its spelling of a shared script wins.

    This is ``unixnu/tree.py:assemble``'s own rule, and it is the difference
    between running the board's tool and running someone else's.
    """
    unixnu = tmp_path / "unixnu"
    (unixnu / "scripts" / "qemu-entry").mkdir(parents=True)
    (unixnu / "scripts" / "qemu-entry" / "make-dt.py").write_text("# unixnu\n", encoding="utf-8")
    (unixnu / "scripts" / "make-qemu-adt.py").write_text("# unixnu spelling\n", encoding="utf-8")

    device = tmp_path / "device"
    (device / "scripts").mkdir(parents=True)
    (device / "scripts" / "make-qemu-adt.py").write_text("# device spelling\n", encoding="utf-8")

    tree = adt.prepare_tree(unixnu, device, tmp_path / "tree")
    assert (tree / ADT_TOOL).read_text(encoding="utf-8") == "# device spelling\n"


def test_prepare_tree_refuses_when_make_dt_is_missing(tmp_path: Path) -> None:
    """The tool invokes it by name; without it every run fails in a subprocess."""
    unixnu = tmp_path / "unixnu"
    (unixnu / "scripts").mkdir(parents=True)
    (unixnu / "scripts" / "something-else.py").write_text("# not make-dt\n", encoding="utf-8")
    device = tmp_path / "device"
    (device / "scripts").mkdir(parents=True)
    (device / "scripts" / "make-qemu-adt.py").write_text("# device tool\n", encoding="utf-8")

    error = _refused(adt.prepare_tree, unixnu, device, tmp_path / "tree")
    assert error.code is ErrorCode.ARTIFACT_MISSING, error
    assert "make-dt.py" in error.message, error


def test_prepare_tree_refuses_when_no_repository_supplies_the_tool(tmp_path: Path) -> None:
    """A tree with no tool is a tree nothing can run."""
    unixnu = tmp_path / "unixnu"
    (unixnu / "scripts" / "qemu-entry").mkdir(parents=True)
    (unixnu / "scripts" / "qemu-entry" / "make-dt.py").write_text("# unixnu\n", encoding="utf-8")
    device = tmp_path / "device"
    device.mkdir()

    error = _refused(adt.prepare_tree, unixnu, device, tmp_path / "tree")
    assert error.code is ErrorCode.ARTIFACT_MISSING, error
    assert ADT_TOOL in error.message, error


def test_prepare_tree_recreates_the_tree_so_a_stale_script_cannot_survive(
    tmp_path: Path,
) -> None:
    """A pin change must not leave the previous pin's script behind."""
    unixnu = tmp_path / "unixnu"
    (unixnu / "scripts" / "qemu-entry").mkdir(parents=True)
    (unixnu / "scripts" / "qemu-entry" / "make-dt.py").write_text("# unixnu\n", encoding="utf-8")
    device = tmp_path / "device"
    (device / "scripts").mkdir(parents=True)
    (device / "scripts" / "make-qemu-adt.py").write_text("# device tool\n", encoding="utf-8")

    tree_dir = tmp_path / "tree"
    stale = tree_dir / "scripts" / "stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("# from a previous pin\n", encoding="utf-8")

    tree = adt.prepare_tree(unixnu, device, tree_dir)
    assert not (tree / "scripts" / "stale.py").exists(), "a stale script survived"


def test_prepare_tree_copies_nothing_when_neither_repository_has_scripts(
    tmp_path: Path,
) -> None:
    """An empty tree is a refusal about the tool, not a crash."""
    error = _refused(adt.prepare_tree, tmp_path / "a", tmp_path / "b", tmp_path / "tree")
    assert error.code is ErrorCode.ARTIFACT_MISSING, error


# ---------------------------------------------------------------------------
# the board.toml-to-flag mapping is one mapping
# ---------------------------------------------------------------------------
def test_the_ramdisk_and_trust_cache_switches_reach_the_command(tmp_path: Path) -> None:
    """Asking for the ramdisk and the trust cache puts them on the command line.

    The other half of this check -- that the tool's own parser spells
    ``--packed-data-ramdisk`` and ``--trust-cache`` -- needs the device
    repository's source and lives in ``tests/integration/``, which runs where
    the pinned vendor checkout has been prepared.  Both halves used to be one
    test here, and the vendor half read ``vendor/UniXNU-qemu/scripts/
    make-qemu-adt.py`` unguarded, so ``preflight`` went red on a clean checkout
    (run 40107080802, 2026-10-24) with a ``FileNotFoundError`` raised before a
    single assertion of its own ran.

    This half is pure: a synthetic board is all it needs.
    """
    board = _synthetic_board(tmp_path)
    command = adt.adt_command(
        board, tmp_path / "kernel.macho.adt", seed=SEED_HEX, data_ramdisk=True, trust_cache=True
    )
    for flag in ("--packed-data-ramdisk", "--trust-cache", "--ipad-identity"):
        assert flag in command, (flag, command)
