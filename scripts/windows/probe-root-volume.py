"""Boot the real guest on Windows QEMU with a second block device, and watch.

What this measures
------------------
Whether a ``qemu_defconfig`` UniXNU kernel enumerates a *second* virtio-blk
device at all.  That is the question the root-volume blocker reduces to:
``qemu_root_defconfig`` is ``qemu_defconfig`` plus ``CONFIG_XNU_NATIVE_VIRTIO_BLOCK``
and the owner root chain, so if this kernel has no block driver then attaching
the OS volume is not sufficient and the verified-root chain is required, not
merely sufficient.

The kernel is observed through :mod:`unipad.host.guestobserve` while QEMU is
still running, which is the only way to read its process list: ``allproc`` is a
kernel data structure, not something the serial log reports.

This script takes a ``--root-image`` and attaches it exactly as
:func:`unipad.host.unixnu_boot.build_argv` does, so what is measured here is
what the launcher would start -- not a hand-rolled argv that could differ.

Usage
-----
    python scripts/windows/probe-root-volume.py --root-image PATH [--seconds N]
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
from unipad.host.udif import LzfseDecoder, UdifImage  # noqa: E402
from unipad.host.unixnu_boot import BootInputs, build_argv, read_board_payload  # noqa: E402

QEMU = REPO / "local-private/tools/qemu/qemu-system-aarch64.exe"
FIRMWARE = REPO / "local-private/tools/qemu/share/edk2-aarch64-code.fd"
DEVICE_DIR = REPO / "vendor/UniXNU-qemu"
SOURCE_ESP = REPO / "local-private/boot7/run2/esp"
KERNEL = REPO / "local-private/boot7/boot-inputs/unixnu-out/qemu/kernel/kernel.macho"
KERNEL_SHA = "9d4b1ab14d74fdfa6afeac12871427e26441cab9f3e2efc97e42b103275643e0"

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


def go_decode(source: Path, destination: Path) -> dict:
    """Decode a UDIF container to raw bytes, or raise naming what is missing.

    No LZFSE decoder is supplied, which is the honest default on this host:
    Python's standard library has none, and 3415 of the OS member's 3814 chunks
    are LZFSE.  The refusal says so rather than producing a partial image.
    """
    image = UdifImage(source)
    return image.write_raw(destination, LzfseDecoder())


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
    parser.add_argument("--decode-udif", action="store_true",
                        help="force UDIF decoding even when the trailer is absent; "
                             "decoding is otherwise detected by sniffing the koly "
                             "trailer, and needs an LZFSE decoder for the OS member, "
                             "which this host does not have")
    args = parser.parse_args()

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
    (run / "esp").mkdir(parents=True)
    print(f"staging the ESP into {run / 'esp'} ...", flush=True)
    shutil.copytree(SOURCE_ESP, run / "esp", dirs_exist_ok=True)
    shutil.copyfile(FIRMWARE, run / "vars.fd")

    # The same inputs unixnu_boot.boot() takes, so the argv it builds is the argv
    # the launcher would build -- this probe measures the launcher, not a copy.
    spec = read_board_payload(DEVICE_DIR)
    inputs = BootInputs(
        firmware=FIRMWARE,
        kernel=KERNEL,
        devicetree=run / "esp" / "EFI/UniXNU/Payloads/kernel.macho.adt",
        darwinboot=run / "esp" / "EFI/UniXNU/Payloads/DarwinBoot.efi",
        unixnu=run / "esp" / "EFI/BOOT/BOOTAA64.EFI",
        payload_toml=run / "esp" / "EFI/UniXNU/unixnu.toml",
    )
    port = free_port()
    argv = [str(QEMU)] + build_argv(
        QEMU, inputs, run / "esp", run, qmp_port=port, root_image=root
    )
    (run / "command.txt").write_text(" ".join(argv), encoding="utf-8")
    print(f"root image {root} ({root.stat().st_size} bytes); qmp port {port}", flush=True)

    #: Kernel-log lines that mean "the boot has reached the point that answers the
    #: question".  The kernel's own log goes to the msgbuf, not to the serial
    #: (measured: the serial stops at DarwinBoot's ``hand-off to the kernel``),
    #: so the marker is read out of the msgbuf the observer already fetches.
    MARKERS = ("Waiting on", "bsd_autoconf", "mountroot", "IOMedia")

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
                "--kernel", str(KERNEL),
                "--expected-sha256", KERNEL_SHA,
                "--serial", str(serial),
                "--run-id", "boot9",
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
        print(f"gave up after {attempts} observations", flush=True)
        return 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        (run / "qemu-stdout.log").write_bytes(process.stdout.read() if process.stdout else b"")
        (run / "qemu-stderr.log").write_bytes(process.stderr.read() if process.stderr else b"")


if __name__ == "__main__":
    raise SystemExit(main())
