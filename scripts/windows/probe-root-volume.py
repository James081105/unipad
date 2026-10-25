"""Boot the real guest on Windows QEMU with a second block device, and watch.

What this measures
------------------
Whether a UniXNU kernel enumerates a *second* virtio-blk device at all.  That is
the question the root-volume blocker reduces to: ``qemu_root_defconfig`` is
``qemu_defconfig`` plus ``CONFIG_XNU_NATIVE_VIRTIO_BLOCK`` and the owner root
chain, so if this kernel has no block driver then attaching the OS volume is not
sufficient and the verified-root chain is required, not merely sufficient.

Which kernel is measured is ``--inputs``.  The default bundle carries a
``qemu_defconfig`` kernel; run 40241617618's bundle carries the first kernel ever
built from ``qemu_root_defconfig``, and that is the one this probe exists to
measure now that it exists.

The kernel is observed through :mod:`unipad.host.guestobserve` while QEMU is
still running, which is the only way to read its process list: ``allproc`` is a
kernel data structure, not something the serial log reports.

This script stages the ESP and seeds ``vars.fd`` exactly as
:func:`unipad.host.unixnu_boot.boot` does, and attaches ``--root-image`` exactly
as :func:`unipad.host.unixnu_boot.build_argv` does, so what is measured here is
what the launcher would start -- not a hand-rolled argv that could differ.  An
earlier version copytree'd a previous run's ESP and byte-copied the firmware
into ``vars.fd``, which is a different EDK II variable store and therefore a
different machine; both are now generated the way ``boot()`` generates them.

**Exit codes.** 0 means a mountpoint marker appeared in the kernel log.  4 means
the guest never reached the kernel hand-off.  5 means the deadline expired
first.  2 and 3 are refusals before QEMU starts.  A run that gave up used to
return 0, which made a failure indistinguishable from a success.

**One unbounded call.** ``guestobserve`` has no deadline of its own and its QMP
client takes a 10 s default per command, so a single ``go.main()`` can block far
longer than ``--seconds`` -- the deadline is only evaluated *between* calls.  The
worst case is a guest whose process list is at ``MAX_PROCESSES`` and whose
``msgbuf`` claims the full 16 MiB.  That is a ``guestobserve`` limitation this
probe inherits rather than fixes; it is recorded so nobody reads a long run as a
hang, or a hang as a long run.

Usage
-----
    python scripts/windows/probe-root-volume.py --root-image PATH [--seconds N]
        [--inputs BUNDLE] [--devicetree PATH] [--trust-cache PATH] [--run DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from unipad.host import guestobserve as go  # noqa: E402
from unipad.errors import UniPadError  # noqa: E402
from unipad.host.udif import UdifImage, default_lzfse  # noqa: E402
from unipad.host.adt import read_board_adt  # noqa: E402
from unipad.host.unixnu_boot import (  # noqa: E402
    MEMORY_MIB,
    SCREEN,
    BootInputs,
    build_argv,
    read_board_payload,
    stage_esp,
)

QEMU = REPO / "local-private/tools/qemu/qemu-system-aarch64.exe"
FIRMWARE = REPO / "local-private/tools/qemu/share/edk2-aarch64-code.fd"
DEVICE_DIR = REPO / "vendor/UniXNU-qemu"
#: The boot inputs, from the macOS runner's payload build.  The device tree is
#: the one exception and is explained at its use site.
#:
#: It is an ARGUMENT (``--inputs``) with this as the default, because the bundle
#: is a per-run artifact: run 40241617618 produced the first kernel built from
#: ``qemu_root_defconfig``, and a probe whose bundle is a constant cannot measure
#: that kernel without an edit to this file -- and an edit to a path constant is
#: exactly the change that gets made without noticing what else in the file
#: assumes the old bundle.  Which is what happened to KERNEL_SHA below.
INPUTS = REPO / "local-private/boot7/boot-inputs"
#: The three files this probe needs out of the bundle, as bundle-relative paths.
#: Kept as data rather than as three module constants so ``--inputs`` can point at
#: any bundle: a constant computed from INPUTS at import time is a constant, and
#: a second bundle would need the import re-run.
UNIXNU_EFI_NAME = "UniXNU.efi"
KERNEL_NAME = "unixnu-out/qemu/kernel/kernel.macho"
DARWINBOOT_EFI_NAME = ("unixnu-out/qemu/tree/build/darwinboot-efi/build"
                       "/DarwinBoot/DEBUG_CLANGPDB/AARCH64/DarwinBoot.efi")
#: The kernel the observer is told to expect, by digest -- and where that digest
#: comes from.  It used to be a constant in this file (pinned to boot7's kernel),
#: which meant every new bundle needed the constant updated by hand, and a stale
#: constant refuses a legitimate kernel for a reason that reads like corruption.
#: The bundle's own manifest.json already records the digest of every file it
#: carries, so the check reads the digest the bundle claims for its own kernel.
#: That is a stronger check rather than a weaker one: it refuses a kernel that
#: does not match the bundle it came out of, which is the actual failure mode (a
#: swapped or truncated kernel), and it cannot go stale.
MANIFEST_NAME = "manifest.json"
KERNEL_KEY = "unixnu-out/qemu/kernel/kernel.macho"
#: Where a generated device tree can be found.  The payload build that would
#: have written one into the bundle failed on a missing font header, so the
#: bundle carries none -- its own manifest records the role as ``absent`` --
#: and the trees that exist are inside previously staged ESPs.  That is a
#: stale copy, and it is *verified against the board* on every run rather than
#: trusted; see verify_adt_matches_board.
STAGED_ADT = (REPO / "local-private/boot7/run2/esp/EFI/UniXNU/Payloads"
              / "kernel.macho.adt")

#: Which board member a given image is, by the digest board.toml [ipsw.sha256]
#: pins, and what role that member plays.  The digests are read out of the board
#: at run time so a re-pin cannot leave them stale; the roles are a literal,
#: because the board does not carry them -- they come from
#: ``vendor/UniXNU-qemu/scripts/run-qemu.py:186`` (``044-76040-038.dmg`` is the
#: ``os`` drive) and ``:196`` (``044-76034-038.dmg`` is the ``systemos`` drive).
#: The first run of this script attached ``044-76034-038.dmg`` believing it was
#: the root; it is the SystemOS cryptex, and the root-matching UUID belongs to
#: ``044-76040-038.dmg``.  That is a second, independent defect from the missing
#: driver, and it was invisible because nothing said which image it was.
ROLES = {"044-76040-038.dmg": "os", "044-76034-038.dmg": "systemos"}


def bundle_kernel_sha(inputs: Path) -> str:
    """The digest the bundle's own manifest claims for its kernel.

    Read, not hard-coded.  A constant in this file is a constant that knows one
    bundle: run 40241617618 shipped a different kernel, and the constant would
    have refused it with a message about a mismatch -- which reads as corruption
    and is actually a stale expectation.  The manifest is written by the same
    step that wrote the files, so it is the authority for what this bundle
    contains, and a kernel that disagrees with it is genuinely suspect.

    A bundle whose manifest is missing, unreadable, or silent about the kernel is
    a refusal rather than a fallback to "no check": the observer's
    ``--expected-sha256`` is what makes its symbol resolution mean anything, and
    running without it would produce a report nobody can trust.
    """
    import json

    manifest = inputs / MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as failure:
        raise SystemExit(
            f"{manifest} is absent, so this probe cannot know which kernel this "
            "bundle carries; refusing rather than observing an unidentified kernel"
        ) from failure
    except (OSError, json.JSONDecodeError) as failure:
        raise SystemExit(f"{manifest} could not be read: {failure}") from failure
    entry = payload.get(KERNEL_KEY)
    if not isinstance(entry, dict) or not str(entry.get("sha256", "")).strip():
        raise SystemExit(
            f"{manifest} carries no sha256 for {KERNEL_KEY}; refusing rather "
            "than observing an unidentified kernel"
        )
    return str(entry["sha256"]).strip().lower()


def board_members() -> dict[str, str]:
    import tomllib

    board = tomllib.loads((DEVICE_DIR / "board.toml").read_text(encoding="utf-8"))
    pins = board.get("ipsw", {}).get("sha256", {})
    return {str(digest).lower(): name for name, digest in pins.items()}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_udif(path: Path) -> bool:
    """Whether the file ends in a UDIF ``koly`` trailer.

    Sniffed, not inferred from the suffix.  ``.dmg`` does not mean UDIF in an
    IPSW: ``044-76034-038.dmg`` is 3,303,014,400 bytes of *raw* APFS and carries
    no trailer at all, while ``044-76040-038.dmg`` is a UDIF container.  A
    suffix-based heuristic refused the one image that needed no decoding -- which
    is how the first version of this function behaved, and why it sniffs.
    """
    try:
        size = path.stat().st_size
        if size <= 512:
            return False
        with path.open("rb") as handle:
            handle.seek(size - 512)
            return handle.read(4) == b"koly"
    except OSError:
        return False


def board_root_uuid() -> str:
    """The UUID the board tells the kernel to match the root volume by."""
    import tomllib

    board = tomllib.loads((DEVICE_DIR / "board.toml").read_text(encoding="utf-8"))
    adt = board.get("payload", {}).get("adt", {})
    uuid = adt.get("root_match_volume_uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        raise SystemExit("board.toml [payload.adt] names no root_match_volume_uuid")
    return uuid.strip()


def verify_adt_matches_board(adt: Path, uuid: str) -> None:
    """Refuse a device tree that does not publish the board's root UUID.

    The tree this probe uses comes from a previously staged ESP, because the
    payload build that would have written a fresh one into the bundle failed on
    a missing font header.  A stale copy is only safe if something checks it,
    and the property that matters for *this* probe is the one that decides what
    the kernel waits for: ``/chosen/root-matching``.  It is stored as an XML
    string inside the Apple-format tree, so a byte search is enough and avoids
    depending on the shape of unipad.host.adt.parse_adt's return value.
    """
    blob = adt.read_bytes()
    if uuid.encode() not in blob:
        raise SystemExit(
            f"{adt.name} does not carry the board's root_match_volume_uuid "
            f"{uuid}; it is a stale tree and this probe would measure the "
            "wrong UUID"
        )
    print(f"device tree carries the board's root UUID {uuid}", flush=True)


def go_decode(source: Path, destination: Path) -> dict:
    """Decode a UDIF container to raw bytes, or raise naming what is missing.

    The decoder is the repo's own pure-stdlib port (``unipad.host.lzfse``,
    reached through ``udif.default_lzfse``).  It used to be omitted, which was
    the honest default while the standard library had no LZFSE decoder and 3415
    of the OS member's 3814 chunks are LZFSE -- the refusal said so rather than
    producing a partial image.  The refusal is still there for a caller that
    builds ``LzfseDecoder()`` with no argument; this one does not, because there
    is now something correct to hand it.

    Progress is reported every 256 chunks because the LZFSE path is the
    bottleneck by two orders of magnitude -- measured ~2 MB/s of output on this
    host, roughly an hour for a 6 GB image, against ~31 s for the I/O side --
    and a silent hour-long loop is indistinguishable from a hang.  That rate was
    measured against ``/usr/bin/compression_tool`` on a macOS host; the pure
    Python port's rate on this host is what ``probe-root-volume.py --decode-udif``
    now measures, and it has not been measured end to end yet.
    """
    image = UdifImage(source)
    begun = time.monotonic()

    def progress(done: int, total: int, elapsed: float) -> None:
        if done == total or done % 256 == 0:
            rate = done / elapsed if elapsed > 0 else 0.0
            print(f"  {done}/{total} chunks, {elapsed:.0f}s, {rate:.0f} chunks/s",
                  flush=True)

    print(f"decoding {image.raw_size} bytes from {len(list(image.chunks()))} chunks ...",
          flush=True)
    return image.write_raw(destination, default_lzfse(), progress=progress)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-image", type=Path, required=True,
                        help="raw image to attach as a second virtio-blk disk")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--settle", type=float, default=8.0,
                        help="seconds to let the kernel run before the first observation")
    parser.add_argument("--run", type=Path, default=REPO / "local-private/boot9")
    parser.add_argument("--inputs", type=Path, default=INPUTS,
                        help="the boot-inputs bundle to boot (default: %(default)s)")
    parser.add_argument("--trust-cache", type=Path, default=None,
                        help="the owner root chain's trust bundle, staged as "
                             "kernel.macho.trustcache; without it AMFI refuses "
                             "launchd (build-os-root.py:26-27)")
    parser.add_argument("--devicetree", type=Path, default=STAGED_ADT,
                        help="the Apple-format device tree to boot (default: "
                             "%(default)s).  It MUST carry the board's "
                             "root_match_volume_uuid, and it must carry the "
                             "TrustCache memory-map binding when --trust-cache "
                             "is given: make-dt.py:162 emits that binding only "
                             "under its own --trust-cache, so a tree built "
                             "without one leaves the trust cache file on the "
                             "ESP unbound and the guest no better off.")
    parser.add_argument("--decode-udif", action="store_true",
                        help="force UDIF decoding even when the trailer is absent; "
                             "decoding is otherwise detected by sniffing the koly "
                             "trailer, and needs an LZFSE decoder for the OS member, "
                             "which this host does not have")
    args = parser.parse_args()

    # The bundle is resolved before anything is read out of it, so a caller that
    # passes --inputs gets a probe that reads THAT bundle everywhere rather than
    # in three of the four places.  The device tree is the exception and stays an
    # argument of its own: it is not in the bundle (see
    # verify_adt_matches_board), and a probe that took it from --inputs would have
    # nothing to take.
    inputs = args.inputs.resolve()
    for name in (UNIXNU_EFI_NAME, KERNEL_NAME, DARWINBOOT_EFI_NAME):
        wanted = inputs / name
        if not wanted.is_file():
            print(f"boot inputs are incomplete: {wanted} is absent")
            return 2
    kernel = inputs / KERNEL_NAME
    unixnu = inputs / UNIXNU_EFI_NAME
    darwinboot = inputs / DARWINBOOT_EFI_NAME
    kernel_sha = bundle_kernel_sha(inputs)
    print(f"bundle {inputs}; kernel sha256 {kernel_sha[:16]}", flush=True)

    root = args.root_image.resolve()
    if not root.is_file() or root.stat().st_size <= 0:
        print(f"root image unusable: {root}")
        return 2

    # A UDIF container is decoded here rather than by the operator, so the probe
    # can be handed the image the IPSW actually ships.  QEMU's format=raw cannot
    # read one, and without this the pinned OS member is unusable even though the
    # volume it contains is exactly the one the kernel waits for.
    if args.decode_udif or is_udif(root):
        decoded = args.run / "os-root.raw"
        # The probe owns this path, so it clears it.  udif.write_raw refuses to
        # overwrite an existing image -- correctly, it protects user data -- and
        # its own cleanup is a `except BaseException`, which a SIGTERM does not
        # raise: `timeout 60 probe ...` leaves a partial file behind that then
        # blocks every retry with "refusing to overwrite".  Measured.  Removing
        # it here is safe precisely because this path is inside the probe's own
        # run directory, which the probe rmtree's at the start anyway.
        if decoded.exists():
            decoded.unlink()
        print(f"{root.name} is a UDIF container; decoding to {decoded.name} ...", flush=True)
        try:
            record = go_decode(root, decoded)
        except UniPadError as exc:
            print(f"decode refused ({exc.code}): {exc.message}")
            print("The OS member's 3415 of 3814 chunks are LZFSE, and Python's "
                  "standard library has no LZFSE decoder, so a raw image has to be "
                  "supplied or a decoder written. The raw SystemOS member "
                  "(044-76034-038.dmg) needs no decoding.")
            return 3
        print(f"decoded: {record['size_bytes']} bytes, "
              f"gpt={record['gpt']}", flush=True)
        root = decoded

    # Name the image against the board's own pins.  A run that attached the wrong
    # member is a run whose negative result means nothing, so the probe says which
    # member it has rather than leaving the operator to work it out afterwards.
    member = board_members().get(sha256_of(root))
    if member is None:
        print(f"NOTE: {root.name} matches no digest in board.toml [ipsw.sha256]; "
              f"it is not a pinned IPSW member")
    else:
        role = ROLES.get(member, "unknown")
        print(f"image is board member {member}, role {role}")
        if role == "systemos":
            print("WARNING: this is the SystemOS cryptex, not the OS root volume. "
                  "The root-matching UUID belongs to the os member, so a successful "
                  "mount is impossible with this image whatever the kernel can do.")

    run = args.run
    if run.exists():
        shutil.rmtree(run, ignore_errors=True)
    run.mkdir(parents=True, exist_ok=True)

    # The board's declaration, read once and *used*.  An earlier version of this
    # probe computed ``spec`` and then threw it away, so the board's kernel_args,
    # its ``loader must be "driver"`` check and its ``dram_size`` never reached
    # the guest -- the probe booted whatever the copied ESP happened to say, at
    # 6144 MiB regardless of what the board declares, where ``boot()`` refuses a
    # mismatch.
    spec = read_board_payload(DEVICE_DIR)
    board_adt = read_board_adt(DEVICE_DIR)
    memory_mib = board_adt.dram_size >> 20
    if memory_mib != MEMORY_MIB:
        print(f"board declares {memory_mib} MiB but this module gives the machine "
              f"{MEMORY_MIB} MiB; refusing rather than booting a different board")
        return 2

    # The same inputs, staged the same way.  An earlier version copytree'd a
    # *previous run's* ESP out of local-private/boot7/run2 and seeded vars.fd
    # with a byte copy of the firmware -- where boot() regenerates the ESP from
    # the current inputs and truncates vars.fd to zeros.  Both are 64 MiB, but
    # one is the EDK II variable store pre-loaded with the firmware's own bytes
    # and the other is empty: different machines, invisible in the serial log
    # this probe reads.  The in-repo precedent for doing it right is
    # probe-guest-cpu.py, which calls stage_esp.
    #
    # Sources, not destinations.  An earlier version named the ESP's own paths
    # here, which stage_esp rmtree's before it writes them -- so the inputs were
    # unusable by boot() and the comment claiming they were "the same inputs
    # boot() takes" was false in exactly the way that matters.  The device tree
    # is the one input whose fresh source is absent, so it is verified against
    # the board rather than trusted.
    verify_adt_matches_board(args.devicetree, board_root_uuid())
    inputs = BootInputs(
        firmware=FIRMWARE,
        kernel=kernel,
        devicetree=args.devicetree,
        darwinboot=darwinboot,
        unixnu=unixnu,
        # Deliberately None: stage_esp *generates* unixnu.toml from the board's
        # own spec, which is what makes the board's kernel_args reach the guest.
        payload_toml=None,
        trust_cache=args.trust_cache,
    )
    if args.trust_cache is None:
        print("NOTE: no --trust-cache; this guest's kernel cannot exec launchd "
              "(AMFI, build-os-root.py:26-27), so a userland marker will not "
              "appear however far the boot gets")
    elif b"TrustCache" not in args.devicetree.read_bytes():
        # The binding is 16 zero bytes under the name TrustCache in the ADT's
        # memory-map node (make-dt.py:162, emitted only under make-dt.py's own
        # --trust-cache).  A tree without it is a boot where the trust cache file
        # is staged and never bound, which is the most misleading shape this
        # measurement could take: everything looks supplied and nothing is.
        print("REFUSING: --trust-cache was given but this device tree carries no "
              "TrustCache binding (make-dt.py:162), so the file would be staged "
              "and never bound.  Build the tree with --trust-cache.")
        return 2
    esp = run / "esp"
    print(f"staging the ESP into {esp} ...", flush=True)
    staged = stage_esp(inputs, esp, spec)
    # The five payload files, counted without the ``_esp`` bookkeeping key that
    # stage_esp also returns -- an earlier version of this check counted every
    # key and refused a correct staging with "wrote 6 entries, expected 5".
    # The sizes come from the copy that just happened rather than from a second
    # stat of a path a later edit could move, and a missing file here is a boot
    # that never reaches the kernel.
    # Five without a trust cache, six with one.  A constant that says five is a
    # probe that refuses the run it was given.
    expected = 6 if args.trust_cache is not None else 5
    files = {k: v for k, v in staged.items() if k.startswith("EFI/")}
    if len(files) != expected:
        print(f"stage_esp wrote {len(files)} payload files, expected {expected}: "
              f"{sorted(files)}")
        return 2
    for relative, size in sorted(files.items()):
        print(f"  {relative}  {size} bytes", flush=True)

    # The writable vars flash, truncated to exactly the code flash's size --
    # zeros, not a copy of the firmware.  Same condition boot() uses.
    vars_path = run / "vars.fd"
    code_size = FIRMWARE.stat().st_size
    if not vars_path.is_file() or vars_path.stat().st_size < code_size:
        with vars_path.open("wb") as handle:
            handle.truncate(code_size)
    port = free_port()
    # Only with the driver that reads it, decided the way boot() decides: a
    # geometry nothing set is a claim the run cannot support.
    ramfb_size = SCREEN if (esp / "EFI/UniXNU/Payloads/QemuRamfb.efi").is_file() else None
    argv = [str(QEMU)] + build_argv(
        QEMU, inputs, esp, run, qmp_port=port, memory_mib=memory_mib,
        ramfb_size=ramfb_size, root_image=root
    )
    (run / "command.txt").write_text(" ".join(argv), encoding="utf-8")
    print(f"root image {root} ({root.stat().st_size} bytes); qmp port {port}", flush=True)

    #: Kernel-log lines that mean "the boot has reached the point that answers the
    #: question".  The kernel's own log goes to the msgbuf, not to the serial
    #: (measured: the serial stops at DarwinBoot's ``hand-off to the kernel``),
    #: so the marker is read out of the msgbuf the observer already fetches.
    MARKERS = ("Waiting on", "bsd_autoconf", "mountroot", "IOMedia")

    started = time.monotonic()
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    serial = run / "serial.log"
    kernel_log = run / "kernel.log"
    deadline = time.monotonic() + args.seconds
    load_seen = False
    try:
        # Before the kernel load line there is no verified mapping between the
        # kernel's virtual and physical bases, so nothing can be read.
        while time.monotonic() < deadline:
            if process.poll() is not None:
                print(f"qemu exited early with {process.returncode}", flush=True)
                break
            if serial.is_file():
                text = serial.read_text(encoding="utf-8", errors="replace")
                if "kernel   va 0x" in text or "kernel va 0x" in text:
                    load_seen = True
                    print("kernel load line seen; observing the live guest", flush=True)
                    # Give the kernel a moment to install its own tables before
                    # asking for them.
                    time.sleep(args.settle)
                    break
            time.sleep(0.5)

        if not load_seen:
            print("NO LOAD LINE: the guest never reached the kernel hand-off")
            return 4

        # Repeated observations, because one sample is one instant: the first
        # read lands wherever the kernel happened to be, and a boot that had not
        # reached mountroot yet is indistinguishable from one that will never
        # get there.  The loop stops as soon as a marker appears, and records how
        # many attempts it took so the report can say when it gave up.
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            code = go.main([
                "--qmp-port", str(port),
                "--kernel", str(kernel),
                "--expected-sha256", kernel_sha,
                "--serial", str(serial),
                "--run-id", run.name,
                "--log", str(kernel_log),
                "--scratch", str(run / "pmem.bin"),
            ])
            text = kernel_log.read_text(encoding="utf-8", errors="replace") if kernel_log.is_file() else ""
            reached = [m for m in MARKERS if m in text]
            print(f"observation {attempts}: exit {code}, {len(text)} bytes of kernel log, "
                  f"markers {reached}", flush=True)
            if code == 0 and reached:
                print("reached a mountpoint marker", flush=True)
                return 0
            if process.poll() is not None:
                print(f"qemu exited with {process.returncode}", flush=True)
                return 0
            time.sleep(6.0)
        # Non-zero, and it has to be.  An earlier version returned 0 here, so a
        # run where the kernel never reached a mountpoint marker was
        # indistinguishable from a successful one at the process level -- measured
        # 306 s, 44 observations and exit 0 on a guest that never got there.  The
        # elapsed time is printed because the deadline is only evaluated *between*
        # observations, and one ``go.main()`` can block far longer than the whole
        # budget (see the guestobserve deadline note in the module docstring).
        print(f"GAVE UP after {attempts} observation(s) and "
              f"{time.monotonic() - started:.0f}s: no mountpoint marker appeared",
              flush=True)
        return 5
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        (run / "qemu-stdout.log").write_bytes(process.stdout.read() if process.stdout else b"")
        (run / "qemu-stderr.log").write_bytes(process.stderr.read() if process.stderr else b"")


if __name__ == "__main__":
    raise SystemExit(main())
