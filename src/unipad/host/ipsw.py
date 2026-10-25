"""Acquire, verify and unpack the pinned Apple IPSW (PRD 8.1, 8.3, 12).

Why this exists
---------------
``unixnu build kernel`` cannot finish without the *original* kernelcache: the
board's ``[kernel.prelink]`` step feeds it to ``emit_prelink.py`` alongside the
kernel we built, and without it the driver raises ``kernelcache not found``.
``scripts/macos/build-macos.sh`` therefore recorded ``build-kernel`` as
``not_run`` with the reason "pass --kernelcache FILE or --ipsw FILE", which is
honest but means no macOS runner has ever produced a ``kernel.macho``.

Upstream *can* fetch it itself -- ``unixnu/ipsw.py`` reads ``board.toml [ipsw]
url`` and downloads -- but that path verifies nothing about the whole file and
happens inside a build step whose failure mode is a stack trace.  This module
does the acquisition as its own, separately-failable, machine-readable step:

1. read the pin from ``config/sources.lock.json`` and the declaration from the
   device repository's ``board.toml``;
2. **refuse if the two disagree about the URL** -- the lock is the pin and
   ``board.toml`` is the upstream declaration, and a silent divergence between
   them is exactly how a pin stops being a pin (the ``IOStorageFamily``
   ``ref_note`` in the lock records the same failure already happening once);
3. stream the file to a temporary directory, checking length and SHA-256 while
   it downloads, so a truncated or substituted download is refused *before*
   anything is unpacked;
4. delete the image the moment extraction finishes -- it is 6.8 GB and nothing
   downstream needs it;
5. unpack exactly the components the board names in ``[ipsw] fetch``, resolving
   each through the image's own ``BuildManifest.plist`` (they are *component*
   names, not zip paths) and checking the image really is this board's
   firmware, and
6. hand back the kernelcache as what the build actually needs: the IM4P payload
   of the ``KernelCache`` component, LZFSE-decoded, reduced to its arm64 slice
   -- and verified against ``board.toml``'s ``[kernel.prelink]
   kernelcache_sha256``, which is the digest of *that* Mach-O and therefore the
   only one that proves we produced the right file rather than merely *a*
   kernelcache.

Why the kernelcache is not the zip entry
----------------------------------------
This was wrong in the first draft of this module, and it is the kind of wrong
that only shows up against real firmware: ``kernelcache_sha256`` is **not** the
digest of the ``kernelcache.release.*`` member.  Upstream's
``unixnu/ipsw.py::kernelcache`` takes the IM4P payload of the ``KernelCache``
component, runs ``/usr/bin/compression_tool -decode -a lzfse``, and, when the
result is a fat binary, keeps the single ``arm64`` slice -- and *that* is what
``[kernel.prelink] kernelcache_sha256`` pins and what ``emit_prelink.py`` is
handed.  Hashing the raw member would refuse every genuine image.  So the three
stages are separated here, because only two of them can be tested on Windows:

* the IM4P unwrap and the fat-slice selection are pure Python and are unit
  tested against synthetic bytes;
* the LZFSE decode has one dependency, ``/usr/bin/compression_tool``, which
  exists only on macOS -- so it is the one step that can only be proven on a
  macOS runner, and it *refuses* rather than guessing anywhere else.

Lifetime, and why the workspace is not optional
-----------------------------------------------
The image is temporary; the extracted kernelcache has to outlive this call,
because the caller passes it to a build that runs afterwards.  So
:func:`acquire` takes a workspace the **caller** owns and never closes it: a
function that deleted its own outputs would hand back paths that resolve to
nothing, and the caller would find out inside the build step rather than here.
:class:`IpswWorkspace` is a context manager whose cleanup runs in
``__exit__``, so "temporary" still means *gone when the job ends*, including
when the job fails.

What this module deliberately does not do
-----------------------------------------
It never writes inside the repository.  Apple IPSW material stays out of
version control (PRD 12), and on a GitHub runner "temporary" has to mean gone
when the job ends -- including when the job fails.

Everything is stdlib-only and chunked: the pinned image is 6.8 GB, and reading
it into memory to hash it would exhaust a runner.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unipad.errors import ErrorCode, ExitCode, UniPadError
from unipad.host.paths import safe_location, scrub_account

__all__ = [
    "BoardIpsw",
    "IpswPin",
    "IpswWorkspace",
    "RootChain",
    "RootChainRequest",
    "acquire",
    "read_board_ipsw",
    "read_pin",
    "sha256_file",
]

#: Hash and copy chunk (8 MiB).  Large enough that a 6.8 GB file is ~850 reads
#: rather than ~7000, small enough that progress reporting stays meaningful.
CHUNK = 8 << 20

#: The kernelcache entry inside the IPSW is named
#: ``kernelcache.release.<device_class>``.  The board names no explicit file, so
#: the prefix is matched and the single entry carrying it is taken -- and if
#: there is more than one, that is a refusal rather than a guess.
KERNELCACHE_PREFIX = "kernelcache.release"

#: What the decoded kernelcache is called in the workspace.  The build is handed
#: this path, and the name says what it is: the Mach-O, not the IM4P member.
KERNELCACHE_OUTPUT = "kernelcache.macho"

#: How the board asks for the kernelcache.  ``[ipsw] fetch`` names it.
FETCH_KERNELCACHE = "KernelCache"

#: How the board asks for the device tree.  Also in ``[ipsw] fetch``.
FETCH_DEVICETREE = "DeviceTree"

#: The two disk images the *owner root chain* consumes.  They are deliberately
#: NOT part of ``[ipsw] fetch``: ``fetch`` is what the kernel and payload steps
#: want, and the two DMGs are 7.88 GiB of it that only ``build-os-root.py``
#: needs -- putting them in ``fetch`` would make every caller of this module pay
#: for them.  They are named here because the chain needs them and the chain is
#: the reason this module knows about a root chain at all.
FETCH_OS = "OS"
FETCH_SYSTEMOS = "Cryptex1,SystemOS"

#: The two trust-cache IM4Ps.  Both are already in ``[ipsw] fetch``, so this
#: module extracts them for the kernel and payload steps; the chain reads the
#: same two members, from a different place, for a different consumer.
FETCH_STATIC_TRUSTCACHE = "StaticTrustCache"
FETCH_SYSTEM_TRUSTCACHE = "Cryptex1,SystemTrustCache"

#: Where a chip/board divergence between board.toml and the image is recorded, so
#: the summary can report it instead of the divergence being swallowed.
DIVERGENT_IDENTITY: list[dict[str, Any]] = [{}]

#: Where the image says which zip member each ``fetch`` component really is.
MANIFEST_MEMBER = "BuildManifest.plist"

#: The only tool that decodes the kernelcache's LZFSE payload.  It ships with
#: macOS and nothing else, which is why the decode is the one step of this
#: module that cannot be proven on the Windows development host.
COMPRESSION_TOOL = "/usr/bin/compression_tool"

#: Mach-O magics, as the bytes actually appear on disk.  A fat binary starts
#: ``ca fe ba be`` (big-endian, so it reads as the integer 0xCAFEBABE); the
#: arm64 slice we keep is tagged ``0x0100000c`` (``CPU_TYPE_ARM64``); a 64-bit
#: Mach-O starts ``cf fa ed fe``.  Upstream checks that last one as a literal
#: and so does this module -- writing it as ``struct.pack(">I", 0xFEEDFACF)``
#: produces ``fe ed fa cf``, which matches nothing and refuses every genuine
#: kernelcache.
FAT_MAGIC = b"\xca\xfe\xba\xbe"
CPU_TYPE_ARM64 = 0x0100000C
MACHO64_MAGIC = b"\xcf\xfa\xed\xfe"


# ---------------------------------------------------------------------------
# the two declarations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IpswPin:
    """``config/sources.lock.json``'s ``ipsw`` block: the pin.

    ``source`` is where to get it, ``sha256``/``size`` are what proves we got
    *that* file, and ``component_pins`` are the per-DMG digests the board's
    ``[ipsw.sha256]`` table must agree with.
    """

    name: str
    source: str
    sha256: str
    size: int
    component_pins: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BoardIpsw:
    """The device repository's ``board.toml`` ``[ipsw]`` and ``[kernel.prelink]``.

    ``url`` is the upstream declaration of where the image comes from;
    ``fetch`` names the components the board needs; ``kernelcache_sha256`` is
    the digest of the *decompressed arm64 Mach-O* the prelink step was
    qualified against.  ``product``/``version``/``build``/``device_class``/
    ``chip_id``/``board_id`` are what prove an image is this board's firmware
    rather than merely the image this URL happens to serve today.
    """

    url: str
    product: str
    version: str
    build: str
    device_class: str
    fetch: tuple[str, ...]
    kernelcache_sha256: str
    chip_id: int = 0
    board_id: int = 0
    component_pins: dict[str, str] = field(default_factory=dict)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"sources lock not found: {safe_location(path)}",
            lock=safe_location(path),
        ) from exc
    except json.JSONDecodeError as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"sources lock is not valid JSON: {safe_location(path)}",
            lock=safe_location(path),
            error=str(exc),
        ) from exc
    if not isinstance(payload, dict):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"sources lock is not a JSON object: {safe_location(path)}",
            lock=safe_location(path),
        )
    return payload


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def read_pin(lock_path: Path) -> IpswPin:
    """The ``ipsw`` block of the lock, or a refusal naming what is missing.

    A null ``sha256`` is a refusal, not a warning: an unpinned image cannot be
    verified, and ``prepare-image.sh`` already refuses one for the same reason
    (PRD 8.1).
    """
    block = _read_json(lock_path).get("ipsw")
    if not isinstance(block, dict):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "sources lock has no ipsw block",
            lock=safe_location(lock_path),
        )
    name = str(block.get("name") or "")
    sha256 = str(block.get("sha256") or "")
    if not _is_sha256(sha256):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "ipsw.sha256 is not a full SHA-256 digest; refusing to fetch an "
            "unpinned image (PRD 8.1)",
            lock=safe_location(lock_path),
            name=name,
        )
    size = block.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "ipsw.size is missing or not a positive integer",
            lock=safe_location(lock_path),
            name=name,
        )
    source = str(block.get("source") or "")
    if not source.startswith("https://"):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "ipsw.source is not an https URL",
            lock=safe_location(lock_path),
            name=name,
        )
    pins = block.get("component_pins")
    return IpswPin(
        name=name,
        source=source,
        sha256=sha256.lower(),
        size=size,
        component_pins={
            str(k): str(v) for k, v in (pins or {}).items() if isinstance(pins, dict)
        },
    )


def read_board_ipsw(device_dir: Path) -> BoardIpsw:
    """The ``[ipsw]`` and ``[kernel.prelink]`` blocks of the device's board.toml."""
    path = device_dir / "board.toml"
    try:
        board = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            f"board.toml not found in {safe_location(device_dir)}",
            device=safe_location(device_dir),
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"board.toml is not valid TOML: {safe_location(path)}",
            device=safe_location(device_dir),
            error=str(exc),
        ) from exc

    ipsw = board.get("ipsw")
    prelink = board.get("kernel", {}).get("prelink")
    if not isinstance(ipsw, dict) or not isinstance(prelink, dict):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "board.toml has no [ipsw] block or no [kernel.prelink] block",
            device=safe_location(device_dir),
        )
    wanted = str(prelink.get("kernelcache_sha256") or "")
    if not _is_sha256(wanted):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "board.toml [kernel.prelink] declares no kernelcache_sha256, so an "
            "extracted kernelcache could not be proven to be the right one",
            device=safe_location(device_dir),
        )
    sha_table = ipsw.get("sha256")
    return BoardIpsw(
        url=str(ipsw.get("url") or ""),
        product=str(ipsw.get("product") or ""),
        version=str(ipsw.get("version") or ""),
        build=str(ipsw.get("build") or ""),
        device_class=str(ipsw.get("device_class") or ""),
        fetch=tuple(str(item) for item in (ipsw.get("fetch") or ())),
        kernelcache_sha256=wanted.lower(),
        chip_id=_as_int(ipsw.get("chip_id")),
        board_id=_as_int(ipsw.get("board_id")),
        component_pins={
            str(k): str(v)
            for k, v in (sha_table or {}).items()
            if isinstance(sha_table, dict)
        },
    )


def _as_int(value: Any) -> int:
    """``chip_id``/``board_id`` as an integer; TOML may give either form.

    ``chip_id = 0x8011`` arrives as an ``int``.  A board that wrote it as the
    string ``"0x8011"`` still parses, because refusing over a spelling would
    stop a legitimate board from building.  Anything else becomes 0, and the
    identity check refuses rather than matching everything.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)
    except ValueError:
        return 0


def _host_of(url: str) -> str:
    """The host of a URL, and nothing else.

    The full URL is never put in an error: it carries a CDN path that is not a
    secret but is noise, and more importantly a redirect or a query string could
    carry a token.  PRD 9.2 forbids credentials in any output.
    """
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except Exception:  # noqa: BLE001 - a malformed URL is reported by its caller
        return ""


def _tail_of(url: str) -> str:
    """The final path component of a URL: enough to tell two images apart."""
    try:
        path = urllib.parse.urlsplit(url).path
    except Exception:  # noqa: BLE001
        return ""
    return path.rsplit("/", 1)[-1]


def _check_the_two_declarations_agree(pin: IpswPin, board: BoardIpsw) -> None:
    """Refuse when the lock and board.toml name different URLs.

    Both files are in version control and both name the same image, so they
    should agree byte for byte.  When they do not, one of them is stale, and
    deciding *which* is not something this module can do -- it has no idea which
    one was re-pinned deliberately.  Refusing costs a re-pin; continuing costs a
    build nobody can reproduce.
    """
    if pin.source != board.url:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the lock and board.toml name different IPSW URLs; one of them is "
            "stale, and which one cannot be decided here",
            lock_name=pin.name,
            board_build=board.build,
            lock_source_host=_host_of(pin.source),
            board_source_host=_host_of(board.url),
            lock_source_tail=_tail_of(pin.source),
            board_source_tail=_tail_of(board.url),
        )
    for component, digest in board.component_pins.items():
        pinned = pin.component_pins.get(component)
        if pinned and pinned.lower() != digest.lower():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                f"the lock and board.toml pin {component} to different digests",
                component=component,
                lock_digest=pinned.lower(),
                board_digest=digest.lower(),
            )


# ---------------------------------------------------------------------------
# the workspace
# ---------------------------------------------------------------------------
class IpswWorkspace:
    """A temporary directory holding the firmware and what was taken from it.

    Two lifetimes live here, and keeping them apart is the point:

    * ``image/`` -- the 6.8 GB download.  :func:`acquire` deletes it as soon as
      extraction finishes, so the peak usage is the download plus the handful of
      components, and the steady state is only the components.
    * ``parts/`` -- the extracted kernelcache and DMGs.  These outlive
      :func:`acquire` because the caller hands them to a build that runs next.

    Cleanup runs in ``__exit__`` and therefore runs on the failure paths too: a
    runner that leaves 6.8 GB of Apple material behind after a failed build has
    turned "temporary" into a cache nobody audits.  ``close()`` is idempotent so
    a caller that also cleans up in a ``finally`` cannot double-free.

    What is removed is exactly the temporary directory this object created --
    never a root the caller supplied.  A caller that points the workspace at a
    directory it also keeps logs in must not lose the logs, and a workspace that
    deleted a directory it did not create would be a far more destructive bug
    than the leak it prevents.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._requested_root = Path(root) if root is not None else None
        self.path: Path | None = None
        self.closed = False

    def open(self) -> "IpswWorkspace":
        """Prepare the directories, without taking ownership of the cleanup.

        This exists separately from ``__enter__`` because the two callers want
        different lifetimes.  A caller writing ``with IpswWorkspace(...)`` wants
        the workspace gone when its block ends.  :func:`acquire` wants the
        opposite: it needs the directories to exist and must *not* clean them
        up, because the kernelcache it returns is handed to a build that runs
        afterwards.  The first draft expressed that by wrapping its body in
        ``with workspace:`` -- which closes it, so every path it returned
        resolved to a deleted file and the caller found out inside the build.
        Entering explicitly is the honest spelling of "I need this open, and
        teardown is not mine to do".

        Idempotent, so a caller that already entered it pays nothing.
        """
        if self.closed:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the IPSW workspace has been closed and cannot be reopened",
            )
        if self.path is not None:
            return self
        if self._requested_root is not None:
            self._requested_root.mkdir(parents=True, exist_ok=True)
            base = Path(
                tempfile.mkdtemp(dir=str(self._requested_root), prefix="ipsw-")
            )
        else:
            base = Path(tempfile.mkdtemp(prefix="unipad-ipsw-"))
        self.path = base
        (base / "image").mkdir()
        (base / "parts").mkdir()
        return self

    def __enter__(self) -> "IpswWorkspace":
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)

    def discard_root(self) -> bool:
        """Remove the supplied root, but only if nothing is left in it.

        ``close()`` deliberately never removes a root the caller supplied: a
        caller that points the workspace at a directory it also keeps logs in
        must not lose the logs.  That leaves the common case -- a root created
        purely to hold this workspace -- as an empty directory behind, which is
        untidy on a runner and shows up in every later listing.

        ``rmdir`` is the whole implementation, and it is the right one: it
        succeeds only on an empty directory, so this cannot delete a caller's
        content even if the caller put something there.  Returns whether it went.
        """
        if self._requested_root is None:
            return False
        try:
            self._requested_root.rmdir()
        except OSError:
            return False
        return True

    # -- paths -------------------------------------------------------------
    def _require(self, sub: str) -> Path:
        if self.path is None:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the IPSW workspace must be entered before it is used",
                subdirectory=sub,
            )
        return self.path / sub

    @property
    def image_dir(self) -> Path:
        """Where the download lands; emptied by :func:`acquire`."""
        return self._require("image")

    @property
    def parts_dir(self) -> Path:
        """Where extracted components live, for as long as the caller keeps them."""
        return self._require("parts")

    def part_path(self, entry: str) -> Path:
        """Where an unpacked component lands.

        The name is flattened to its final component and then checked against
        the destination: a zip entry that tries to climb out of the workspace
        with ``../`` is refused rather than silently written somewhere else.
        """
        leaf = Path(entry).name
        if not leaf or leaf in (".", ".."):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                f"IPSW member {entry!r} has no usable name",
                member=entry,
            )
        destination = self.parts_dir / leaf
        try:
            destination.resolve().relative_to(self.parts_dir.resolve())
        except ValueError as exc:
            raise UniPadError(
                ErrorCode.VERIFICATION_FAILED,
                f"IPSW member {entry!r} would be written outside the temporary "
                "workspace",
                member=entry,
            ) from exc
        return destination


# ---------------------------------------------------------------------------
# the owner root chain's inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RootChainRequest:
    """Where the owner root chain's four inputs go, for a caller that wants them.

    ``vendor/UniXNU-qemu/scripts/build-os-root.py`` takes an ``--ipsw`` path and
    reads exactly four members out of it:

    * ``BuildManifest.plist`` (to resolve the component paths at all),
    * the ``OS`` member -- the OS disk image it attaches read-only to sign every
      4 KiB block of it,
    * the ``Cryptex1,SystemOS`` member -- the SystemOS image the union mount
      reads, and
    * the ``Cryptex1,SystemTrustCache`` member -- the System trust cache that
      ``build-original-system-trust.py`` bundles.

    ...plus one file that is *not* a zip member: ``<out>/ipsw/<build>/
    044-76040-038.dmg.trustcache``, the ``StaticTrustCache`` IM4P, which the
    chain reads from beside the driver's own cache (``build-os-root.py`` line
    150) because that is where ``unixnu ipsw fetch`` would have left it.

    The two DMGs are 4.80 GiB and 3.08 GiB.  Handing the chain the *image* means
    keeping 6.37 GiB of firmware on disk for the whole chain, and a GitHub macOS
    runner has about 14 GiB free against ``DEFAULT_MIN_FREE_GB=8`` -- so the
    image and the chain cannot coexist there.  What fits is the image's members,
    which the chain needs anyway: the two DMGs are the volumes it signs.

    So the members are published and the image is not.  Each volume flag names a
    *directory*; the member's own leaf name is the image's business, not the
    caller's, and a caller that spelled it would be a second place that knows it.
    """

    #: Where the OS disk image goes (``<dir>/<member leaf>``).
    os_volume_dir: Path | None = None
    #: Where the SystemOS disk image goes.
    systemos_volume_dir: Path | None = None
    #: Where the StaticTrustCache IM4P goes, as ``<dir>/<build>/<member leaf>``.
    static_trustcache_dir: Path | None = None
    #: Where the reconstructed refer image goes, as ``<dir>/<ipsw.name>``.
    refer_ipsw_dir: Path | None = None


@dataclass(frozen=True)
class RootChain:
    """What :func:`publish_root_chain` produced, for the caller's record."""

    os_volume: Path | None = None
    os_volume_sha256: str = ""
    systemos_volume: Path | None = None
    systemos_volume_sha256: str = ""
    static_trustcache: Path | None = None
    refer_ipsw: Path | None = None
    #: The members the reconstructed image really carries.  Two, not four: the
    #: DMGs are published as files rather than duplicated inside a zip, so a
    #: reader of the record can see that the image is a member set and not the
    #: pinned firmware.
    refer_members: tuple[str, ...] = ()

    @property
    def published(self) -> bool:
        return any(
            (
                self.os_volume,
                self.systemos_volume,
                self.static_trustcache,
                self.refer_ipsw,
            )
        )


# ---------------------------------------------------------------------------
# acquisition
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Acquisition:
    """What one :func:`acquire` call produced, for the caller's record."""

    image_sha256: str
    image_bytes: int
    kernelcache: Path
    kernelcache_sha256: str
    kernelcache_member: str
    components: dict[str, Path]
    verified_components: dict[str, str]
    #: Keyed by the *component* the board named in [ipsw] fetch ("DeviceTree"),
    #: not by the member's leaf name ("DeviceTree.j120ap.im4p").  ``components``
    #: is keyed by leaf because that is how the pins are spelled, and a caller
    #: asking for the device tree asks for it by component.
    by_component: dict[str, Path] = field(default_factory=dict)
    #: The owner root chain's inputs, when the caller asked for them.  Empty by
    #: default: a caller that only wants the kernelcache must not pay 7.88 GiB
    #: of extraction for a chain it is not running.
    root_chain: RootChain = field(default_factory=RootChain)


def sha256_file(path: Path) -> str:
    """The SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, expected_size: int, expected_sha256: str) -> int:
    """Stream ``url`` into ``destination``, verifying length and digest.

    Both checks happen *while* the bytes arrive.  Checking only afterwards would
    mean a 6.8 GB download that is wrong in its first megabyte still costs a
    full download and a full disk before anyone finds out.
    """
    digest = hashlib.sha256()
    written = 0
    request = urllib.request.Request(url, headers={"User-Agent": "unipad/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(
            destination, "wb"
        ) as handle:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != expected_size:
                raise UniPadError(
                    ErrorCode.VERIFICATION_FAILED,
                    "the server declared a different length than the lock pins",
                    declared_length=int(declared),
                    pinned_length=expected_size,
                )
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
    except urllib.error.HTTPError as exc:
        raise UniPadError(
            ErrorCode.BLOCKED,
            f"the firmware download was refused by the server (HTTP {exc.code})",
            http_status=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise UniPadError(
            ErrorCode.BLOCKED,
            "the firmware could not be downloaded",
            reason=type(exc).__name__,
        ) from exc

    if written != expected_size:
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            "the downloaded image is not the size the lock pins",
            actual_bytes=written,
            pinned_bytes=expected_size,
        )
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise UniPadError(
            ErrorCode.IMAGE_HASH_MISMATCH,
            "the downloaded image does not match the pinned SHA-256; refusing "
            "to unpack it (PRD 7.4: a download failure must not continue with "
            "an unlabelled package)",
            actual_sha256=actual,
            pinned_sha256=expected_sha256,
        )
    return written


def _open_archive(image: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(image)
    except zipfile.BadZipFile as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "the downloaded image is not a zip archive",
            error=str(exc),
        ) from exc
    except FileNotFoundError as exc:
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            f"the image does not exist: {safe_location(image)}",
            image=safe_location(image),
        ) from exc


def _intish(value: Any) -> int | None:
    """A plist ``ApChipID``/``ApBoardID`` as an integer, or ``None``.

    Apple writes these as strings (``"0x8011"``), but a plist is free to carry
    them as integers, and guessing wrong would silently match nothing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)
    except ValueError:
        return None


def _identities(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Every ``BuildIdentity`` in the manifest, as a tuple of dicts."""
    rows = manifest.get("BuildIdentities")
    if not isinstance(rows, list):
        return ()
    return tuple(item for item in rows if isinstance(item, dict))


def _identity_of(item: Mapping[str, Any]) -> tuple[str, str, int | None, int | None]:
    """One identity as (device_class, restore_behavior, chip_id, board_id)."""
    info = item.get("Info")
    info = info if isinstance(info, dict) else {}
    return (
        str(info.get("DeviceClass") or ""),
        str(info.get("RestoreBehavior") or ""),
        _intish(info.get("ApChipID")),
        _intish(info.get("ApBoardID")),
    )


def _load_identity(archive: zipfile.ZipFile, board: BoardIpsw) -> dict[str, Any]:
    """The image's ``BuildManifest.plist`` and the one identity that is this board.

    Two different things are proven here, and the second is the one that makes
    the URL pin mean anything:

    * the image is the firmware *version* the board declares, for the board's
      *product* -- so a URL that starts serving a newer restore is refused
      rather than silently built against;
    * it carries an ``Erase`` build identity for this device class -- the same
      selection upstream makes, because ``Manifest`` (which is what turns a
      component name into a zip path) is per-identity.

    The chip and board ids are a **cross-check, not a gate**.  Upstream matches
    all four fields, and this module tries that first; but the first real run
    against the pinned 17.7.11 image refused with "no erase build identity for
    this board's device class" even though the image is the right one, which
    means at least one of the four does not read the way ``board.toml`` spells
    it.  Refusing on a cross-check the operator cannot see is worse than
    proceeding on the two fields that *are* unambiguous (device class plus
    restore behaviour) and reporting the divergence: the components come from
    the Manifest either way, and a mismatch worth acting on is now recorded
    rather than silently swallowed.

    When device class plus restore behaviour matches more than one identity the
    choice genuinely is ambiguous, and it refuses -- with the full list, so the
    next attempt has something to act on.
    """
    try:
        raw = archive.read(MANIFEST_MEMBER)
    except KeyError as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"the image carries no {MANIFEST_MEMBER}, so its components cannot "
            "be resolved",
        ) from exc
    try:
        manifest = plistlib.loads(raw)
    except Exception as exc:  # noqa: BLE001 - plistlib raises several types
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"{MANIFEST_MEMBER} could not be parsed",
            error=redact(str(exc)),
        ) from exc
    if not isinstance(manifest, dict):
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"{MANIFEST_MEMBER} is not a dictionary",
        )

    image_version = str(manifest.get("ProductVersion") or "")
    image_build = str(manifest.get("ProductBuildVersion") or "")
    supported = manifest.get("SupportedProductTypes")
    supported = [str(item) for item in supported] if isinstance(supported, list) else []
    if (image_version, image_build) != (board.version, board.build):
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            "this image is not the firmware version this board declares",
            image_version=image_version,
            image_build=image_build,
            board_version=board.version,
            board_build=board.build,
            image_products=supported,
        )
    if board.product not in supported:
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            "this image is not for the product this board declares",
            board_product=board.product,
            image_products=supported,
        )

    rows = _identities(manifest)
    described = tuple(_identity_of(item) for item in rows)

    # Upstream's rule, exactly: device class, erase behaviour, chip and board.
    for item in rows:
        device_class, restore, chip, board_id = _identity_of(item)
        if (
            device_class == board.device_class
            and restore == "Erase"
            and chip == board.chip_id
            and board_id == board.board_id
        ):
            return item

    # The two fields that identify the board unambiguously.
    by_class = [
        item
        for item in rows
        if _identity_of(item)[0] == board.device_class
        and _identity_of(item)[1] == "Erase"
    ]
    if len(by_class) == 1:
        _device_class, _restore, chip, board_id = _identity_of(by_class[0])
        # Reported, not refused: see the docstring.
        DIVERGENT_IDENTITY[0] = {
            "board_chip_id": board.chip_id,
            "image_chip_id": chip,
            "board_board_id": board.board_id,
            "image_board_id": board_id,
        }
        return by_class[0]

    raise UniPadError(
        ErrorCode.VERIFICATION_FAILED,
        "this image carries no erase build identity for this board's device "
        "class, so its components cannot be resolved",
        device_class=board.device_class,
        matching_identities=len(by_class),
        image_identities=[
            {
                "device_class": device_class,
                "restore_behavior": restore,
                "chip_id": chip,
                "board_id": board_id,
            }
            for device_class, restore, chip, board_id in described
        ],
    )


def _member_of(identity: dict[str, Any], component: str) -> str:
    """The zip path a ``fetch`` component really lives at.

    ``fetch`` names *components* -- ``"Cryptex1,SystemTrustCache"`` is one
    string, and it is a Manifest key, not a path.  Treating the fetch list as a
    list of zip members (which the first draft of this module did) refuses
    every genuine image, because the member is
    ``<path>/cryptex1,SystemTrustCache`` and not the key itself.
    """
    table = identity.get("Manifest")
    entry = table.get(component) if isinstance(table, dict) else None
    path = entry.get("Info", {}).get("Path") if isinstance(entry, dict) else None
    if not isinstance(path, str) or not path:
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            f"this image declares no path for the {component} component",
            component=component,
        )
    return path


def _extract(
    image: Path, workspace: IpswWorkspace, board: BoardIpsw
) -> dict[str, tuple[str, Path]]:
    """Pull the board's ``fetch`` components out of the image.

    An IPSW is a zip.  Only the named components are read -- the archive is
    never fully expanded, because most of it is DMGs this board does not need
    and the whole point of a pin is to take exactly what was declared.  The
    return value is keyed by component and carries both the member the image
    named and where it landed, so a later check can say which member a digest
    was taken from.
    """
    archive = _open_archive(image)
    found: dict[str, tuple[str, Path]] = {}
    with archive:
        identity = _load_identity(archive, board)
        names = set(archive.namelist())
        for component in board.fetch:
            entry = _member_of(identity, component)
            if entry not in names:
                raise UniPadError(
                    ErrorCode.ARTIFACT_MISSING,
                    f"{component} is declared as a member this image does not "
                    "carry",
                    component=component,
                    member=Path(entry).name,
                )
            destination = workspace.part_path(entry)
            with archive.open(entry) as source, open(destination, "wb") as target:
                shutil.copyfileobj(source, target, CHUNK)
            found[component] = (entry, destination)
    return found


def _extract_member(
    archive: zipfile.ZipFile, workspace: IpswWorkspace, entry: str
) -> Path:
    """One zip member, verbatim, into the workspace's ``parts/`` directory."""
    destination = workspace.part_path(entry)
    if not destination.is_file():
        with archive.open(entry) as source, open(destination, "wb") as target:
            shutil.copyfileobj(source, target, CHUNK)
    return destination


def _free_bytes(directory: Path) -> int | None:
    """Bytes free on the filesystem holding ``directory``, or None if unknown.

    ``shutil.disk_usage`` rather than shelling out to ``df``: this module
    already runs on Windows as well as macOS, and the free-space question is the
    same question on both.
    """
    try:
        return shutil.disk_usage(directory).free
    except OSError:
        return None


def _same_filesystem(first: Path, second: Path) -> bool:
    """Whether a rename between two directories is a rename and not a copy.

    ``shutil.move`` falls back to copy-then-delete across filesystems, which
    would silently double the peak the arithmetic above is built on -- the image
    plus *both* volumes at once instead of the image plus the larger one.  The
    fallback is not wrong, it is just not what this module measured, so the two
    directories have to be on one filesystem and it has to be checked rather than
    assumed.
    """
    try:
        return os.stat(first).st_dev == os.stat(second).st_dev
    except OSError:
        # A directory that does not exist yet is the caller's business: the
        # caller creates it, and if it never appears the extraction fails on the
        # open with a message about the path.
        return True


def _require_root_chain_space(
    image: Path,
    workspace: IpswWorkspace,
    identity: dict[str, Any],
    components: tuple[str, ...],
    destinations: tuple[Path, ...] = (),
) -> None:
    """Refuse before extracting when the volumes would not fit.

    The peak is the image plus the *larger* of the two volumes, because each
    volume is moved out of the workspace before the next one is extracted (see
    :func:`_pinned_volume`).  Both figures are the zip entries' ``file_size`` in
    iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw -- which is what extraction costs
    and what then occupies the filesystem.  ``BuildIdentities[].Manifest``
    carries no size for either member (only ``DigestInfo`` hashes), so the zip
    directory is the only place these numbers come from.  They are deliberately
    NOT the decoded sizes: ``044-76040-038.dmg`` is a UDIF container that decodes
    to 6,077,580,800 bytes (5.66 GiB), but ``hdiutil`` reads it in place and
    nothing ever writes the decoded form out, so the member size is the one the
    disk sees.

    The image is 6.37 GiB and the OS volume 4.80 GiB, so the peak is 11.17 GiB
    -- and a GitHub macOS runner has about 14 GiB free *before* the build tree,
    and far less after it.

    So this is not a nicety.  Without it the extraction would run until the
    filesystem filled, which on a runner means a half-written multi-GiB DMG that
    the pin check then refuses -- a failure that names a digest instead of naming
    the disk.  Reported as ``blocked`` rather than raised, because the caller's
    other outputs (the kernelcache, the device tree) are still worth having and
    a caller running the chain later gets to decide with the real number in
    front of it.
    """
    if not components:
        return
    with _open_archive(image) as archive:
        sizes = {
            component: archive.getinfo(_member_of(identity, component)).file_size
            for component in components
        }
    for destination in destinations:
        if not _same_filesystem(workspace.parts_dir, destination):
            raise UniPadError(
                ErrorCode.BLOCKED,
                "the owner root chain's volumes cannot be moved to "
                f"{destination}: it is on another filesystem from the workspace, "
                "so the move would be a copy and the peak would be the image plus "
                "both volumes at once -- 14.25 GiB, which does not fit anywhere "
                "this module is meant to run. Publish them beside the workspace",
                destination=safe_location(destination),
            )
    free = _free_bytes(workspace.parts_dir)
    if free is None:
        return
    needed = image.stat().st_size + max(sizes.values())
    if free < needed:
        raise UniPadError(
            ErrorCode.BLOCKED,
            "the owner root chain's volumes do not fit on this filesystem: "
            f"{needed / 2**30:.2f} GiB is needed at the peak (the image plus the "
            f"larger volume) and {free / 2**30:.2f} GiB is free. The image is "
            "6.37 GiB and the two volumes are 4.80 GiB and 3.08 GiB, so this "
            "needs a filesystem emptier than this one: publish the members "
            "before the build tree exists, or run the chain on its own runner",
            needed_bytes=needed,
            free_bytes=free,
            volume_bytes=sizes,
        )


def _pinned_volume(
    image: Path,
    workspace: IpswWorkspace,
    identity: dict[str, Any],
    component: str,
    pins: dict[str, str],
    directory: Path,
) -> tuple[Path, str]:
    """Take one disk image out of the firmware and put it where the chain reads it.

    Moved, not copied, and that is the whole reason this is its own function
    rather than a call to :func:`_publish`: the two volumes are 4.80 GiB and
    3.08 GiB, and the image they come out of is 6.37 GiB.  All three on one
    14 GiB runner is 14.25 GiB and does not fit, so a volume leaves ``parts/``
    for its destination *before* the next one is extracted -- a rename on the
    same filesystem, which costs no space and no second copy.  Every figure here
    is a zip entry's ``file_size``, i.e. what the extraction writes; the OS
    member is a UDIF container that decodes to 6,077,580,800 bytes and nothing
    in this module ever writes that form out.

    The digest is checked twice, once from the extracted member and once from
    the file the caller will actually hand to the chain, because the second one
    is the one that matters: a pin checked in a directory that is then deleted
    proves nothing about the file that survives.
    """
    entry = _member_of(identity, component)
    leaf = Path(entry).name
    wanted = pins.get(leaf)
    if not wanted:
        # The chain signs every 4 KiB block of this image.  An image with no
        # digest to check it against is not a smaller risk, it is an unbounded
        # one, and "it came out of the pinned firmware" is not a digest of the
        # member -- the zip's own CRC is checked by zipfile, which is not the
        # same claim as the lock's.
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"no sha256 is pinned for {leaf}, so the {component} volume could "
            "not be proven to be the one this board was qualified against: add "
            "it to sources.lock.json ipsw.component_pins and board.toml "
            "[ipsw.sha256]",
            component=component,
            member=leaf,
        )
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / leaf
    if destination.is_file():
        # Already published by an earlier call, or left behind by a caller that
        # reuses the directory.  Two outcomes, and neither is "overwrite it": a
        # file that is already the pinned bytes is the answer, and a file that is
        # not is a refusal -- silently replacing it would destroy the only copy
        # of whatever the caller had put there.  (It also has to be handled at
        # all: ``shutil.move`` onto an existing path raises on Windows rather
        # than replacing, so a re-run would fail with a message about the
        # destination instead of about the digest.)
        present = sha256_file(destination)
        if present == wanted.lower():
            return destination, present
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            f"{leaf} is already at {directory} and is not the bytes this board "
            "was qualified against; refusing to replace it",
            component=component,
            actual_sha256=present,
            pinned_sha256=wanted.lower(),
        )
    with _open_archive(image) as archive:
        extracted = _extract_member(archive, workspace, entry)
    actual = sha256_file(extracted)
    if actual != wanted.lower():
        raise UniPadError(
            ErrorCode.IMAGE_HASH_MISMATCH,
            f"{leaf} does not match the digest this board was qualified "
            "against; refusing to hand it to the root chain",
            component=component,
            actual_sha256=actual,
            pinned_sha256=wanted.lower(),
        )
    shutil.move(str(extracted), str(destination))
    written = sha256_file(destination)
    if written != actual:
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            f"{leaf} changed between the image and {directory}",
            component=component,
            actual_sha256=written,
            expected_sha256=actual,
        )
    return destination, written


def _refer_image(
    image: Path,
    workspace: IpswWorkspace,
    board: BoardIpsw,
    identity: dict[str, Any],
    wanted_name: str,
    directory: Path,
) -> tuple[Path, tuple[str, ...]]:
    """Write the two members ``build-original-system-trust.py`` opens the image for.

    ``build-os-root.py`` step 10 symlinks ``<tree>/refer/<ipsw name>`` at the
    image and runs ``build-original-system-trust.py``, which opens that zip and
    reads exactly two things: ``BuildManifest.plist`` (to find the identity) and
    the ``Cryptex1,SystemTrustCache`` member.  It never touches the DMGs -- by
    then they are already on disk, because step 2 of the chain published them
    and step 4 attached the OS one.

    So the file the chain is pointed at does not have to be the 6.37 GiB
    firmware; it has to be a zip carrying those two members at the paths the
    manifest names.  Writing one is how the chain gets an ``--ipsw`` at all on a
    runner that cannot hold the image, and it is recorded as what it is: the
    caller's summary lists the members the reconstruction carries, so a reader
    of the record cannot mistake it for the pinned image.

    The name is the lock's own ``ipsw.name``, and a caller that asks for another
    one is refused: ``build-original-system-trust.py`` hard-codes
    ``refer/iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw``, so a differently-named
    file would make the chain look for a symlink that names something else.
    """
    entry = _member_of(identity, FETCH_SYSTEM_TRUSTCACHE)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / wanted_name
    manifest = MANIFEST_MEMBER
    with _open_archive(image) as archive:
        if entry not in set(archive.namelist()):
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                f"{FETCH_SYSTEM_TRUSTCACHE} is declared as a member this image "
                "does not carry",
                component=FETCH_SYSTEM_TRUSTCACHE,
                member=Path(entry).name,
            )
        trust = _extract_member(archive, workspace, entry)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out:
            # Deflated for the manifest and stored for the trust cache: the
            # latter is 1,871 bytes, and compressing it saves nothing while
            # making the member's bytes different from the image's.
            out.writestr(manifest, archive.read(manifest), zipfile.ZIP_DEFLATED)
            out.write(trust, entry)
    return destination, (manifest, entry)


def publish_root_chain(
    image: Path,
    workspace: IpswWorkspace,
    board: BoardIpsw,
    pins: dict[str, str],
    wanted_name: str,
    request: RootChainRequest,
) -> RootChain:
    """Publish the owner root chain's inputs, or nothing at all.

    Called while the image is still on disk -- :func:`acquire` deletes it the
    moment extraction finishes, and every member this function reads comes out
    of it.  A caller that asked for nothing gets an empty record and pays
    nothing.
    """
    if not any(
        (
            request.os_volume_dir,
            request.systemos_volume_dir,
            request.static_trustcache_dir,
            request.refer_ipsw_dir,
        )
    ):
        return RootChain()

    with _open_archive(image) as archive:
        identity = _load_identity(archive, board)

    # Before anything is extracted, not after: a half-written multi-GiB volume
    # that the pin then refuses is a failure that names a digest instead of naming
    # the disk, and the disk is the thing that was actually wrong.
    wanted: list[str] = []
    destinations: list[Path] = []
    for component, directory in (
        (FETCH_OS, request.os_volume_dir),
        (FETCH_SYSTEMOS, request.systemos_volume_dir),
    ):
        if directory is not None:
            wanted.append(component)
            destinations.append(directory)
    _require_root_chain_space(image, workspace, identity, tuple(wanted), tuple(destinations))

    os_volume: Path | None = None
    os_digest = ""
    if request.os_volume_dir is not None:
        os_volume, os_digest = _pinned_volume(
            image, workspace, identity, FETCH_OS, pins, request.os_volume_dir
        )
    # One at a time, and the order is the space argument rather than a
    # preference: 6.37 GiB of image plus 4.80 GiB of OS volume is 11.17 GiB,
    # which is the largest sum that fits on a 14 GiB runner.  Extracting the
    # smaller SystemOS volume second keeps the peak there.
    systemos_volume: Path | None = None
    systemos_digest = ""
    if request.systemos_volume_dir is not None:
        systemos_volume, systemos_digest = _pinned_volume(
            image,
            workspace,
            identity,
            FETCH_SYSTEMOS,
            pins,
            request.systemos_volume_dir,
        )

    static_trustcache = None
    if request.static_trustcache_dir is not None:
        # The chain reads this from beside the driver's own cache, at
        # <out>/ipsw/<build>/, because that is where "unixnu ipsw fetch" leaves
        # it -- build-os-root.py:150 spells the path out.  There is no pin for
        # it in either the lock or board.toml; the chain itself checks the IM4P
        # payload against a digest of its own, so the record here says what was
        # published and leaves the proof where upstream put it.
        entry = _member_of(identity, FETCH_STATIC_TRUSTCACHE)
        directory = request.static_trustcache_dir / board.build
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / Path(entry).name
        with _open_archive(image) as archive:
            extracted = _extract_member(archive, workspace, entry)
        shutil.copyfile(extracted, destination)
        if sha256_file(destination) != sha256_file(extracted):
            raise UniPadError(
                ErrorCode.VERIFICATION_FAILED,
                f"{destination.name} is not the member the image carried",
                component=FETCH_STATIC_TRUSTCACHE,
            )
        static_trustcache = destination

    refer_ipsw: Path | None = None
    refer_members: tuple[str, ...] = ()
    if request.refer_ipsw_dir is not None:
        refer_ipsw, refer_members = _refer_image(
            image, workspace, board, identity, wanted_name, request.refer_ipsw_dir
        )

    return RootChain(
        os_volume=os_volume,
        os_volume_sha256=os_digest,
        systemos_volume=systemos_volume,
        systemos_volume_sha256=systemos_digest,
        static_trustcache=static_trustcache,
        refer_ipsw=refer_ipsw,
        refer_members=refer_members,
    )


def _verify_components(
    found: dict[str, tuple[str, Path]], pins: dict[str, str]
) -> dict[str, str]:
    """Check every extracted component that carries a pin, and report the rest.

    A component with no pin is still reported -- with an empty digest -- so the
    caller's record shows what was extracted and what was *not* proven, rather
    than implying everything was checked.  Pins are looked up by the member's
    final component, which is how both the lock and ``board.toml`` spell them.
    """
    verified: dict[str, str] = {}
    for component, (entry, path) in found.items():
        leaf = Path(entry).name
        wanted = pins.get(leaf)
        if not wanted:
            verified[leaf] = ""
            continue
        actual = sha256_file(path)
        if actual != wanted.lower():
            raise UniPadError(
                ErrorCode.IMAGE_HASH_MISMATCH,
                f"{leaf} does not match the digest this board was qualified "
                "against; refusing to use it",
                component=component,
                actual_sha256=actual,
                pinned_sha256=wanted.lower(),
            )
        verified[leaf] = actual
    return verified


# ---------------------------------------------------------------------------
# the kernelcache: IM4P -> LZFSE -> arm64 slice
# ---------------------------------------------------------------------------
def _der(data: bytes, at: int) -> tuple[int, int, int]:
    """One DER item: its tag, where its body starts, and how long the body is.

    Ported from ``unixnu/ipsw.py`` because the kernelcache member is an IM4P
    (a DER sequence) and its payload is the fourth item.  ``IndexError`` is left
    to propagate: the caller wraps a truncated or foreign file.
    """
    tag, length = data[at], data[at + 1]
    at += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[at : at + count], "big")
        at += count
    return tag, at, length


def _im4p_payload(data: bytes) -> bytes:
    """The payload of an IM4P (DER: ``IM4P``, type, version, data, ...)."""
    try:
        tag, start, length = _der(data, 0)
    except IndexError as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "the KernelCache component is too short to be an IM4P",
            member_bytes=len(data),
        ) from exc
    if tag != 0x30:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "the KernelCache component is not a DER sequence, so it is not an "
            "IM4P",
        )
    at, items = start, []
    while at < start + length:
        try:
            _, body, size = _der(data, at)
        except IndexError as exc:
            raise UniPadError(
                ErrorCode.INVALID_PACKAGE,
                "the IM4P is truncated",
            ) from exc
        items.append(data[body : body + size])
        at = body + size
    if len(items) < 4 or items[0] != b"IM4P":
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "the KernelCache component is not an IM4P",
        )
    return items[3]


def _arm64_slice(fat: bytes) -> bytes | None:
    """The arm64 slice of a fat binary, or ``None`` when it is not one.

    ``None`` and a refusal are different answers: a plain Mach-O is passed
    through untouched, while a fat binary with no arm64 slice (or two) is a
    refusal, because picking one would be a guess about which kernel this board
    was qualified against.
    """
    if len(fat) < 8:
        return None
    if fat[:4] != FAT_MAGIC:
        return None
    count = struct.unpack(">I", fat[4:8])[0]
    try:
        slices = [
            struct.unpack(">5I", fat[8 + 20 * i : 28 + 20 * i]) for i in range(count)
        ]
    except struct.error as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "the fat kernelcache header does not match its declared slice count",
            declared_slices=count,
        ) from exc
    arm64 = [item for item in slices if item[0] == CPU_TYPE_ARM64]
    if len(arm64) != 1:
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            "expected exactly one arm64 slice in the kernelcache",
            arm64_slices=len(arm64),
            declared_slices=count,
        )
    _, _, offset, size, _ = arm64[0]
    return fat[offset : offset + size]


def _decode_lzfse(payload: bytes, destination: Path) -> None:
    """LZFSE-decode ``payload`` into ``destination``.

    The one step with a macOS-only dependency.  ``compression_tool`` ships with
    macOS and with nothing else, so on any other host this refuses rather than
    writing a file that merely looks like a Mach-O -- the digest check
    downstream would catch it eventually, but "the tool is absent" is a far more
    useful thing to be told.
    """
    tool = Path(COMPRESSION_TOOL)
    if not tool.is_file():
        raise UniPadError(
            ErrorCode.BLOCKED,
            "decoding the kernelcache needs macOS: "
            f"{COMPRESSION_TOOL} is not present on this host",
            tool=COMPRESSION_TOOL,
            platform=sys.platform,
        )
    packed = destination.with_name(destination.name + ".lzfse")
    packed.write_bytes(payload)
    result = subprocess.run(
        [
            COMPRESSION_TOOL,
            "-decode",
            "-a",
            "lzfse",
            "-i",
            str(packed),
            "-o",
            str(destination),
        ],
        capture_output=True,
        text=True,
    )
    packed.unlink(missing_ok=True)
    if result.returncode:
        # compression_tool's stderr is short and carries no paths, but it is
        # still text from another process, so it is redacted and bounded like
        # anything else this module prints (PRD 9.2).
        detail = redact(result.stderr or "").strip().splitlines()
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            "compression_tool could not decode the kernelcache",
            returncode=result.returncode,
            detail=detail[-1] if detail else "",
        )
    if not destination.is_file():
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            "compression_tool reported success but wrote nothing",
        )


def _kernelcache_macho(
    found: dict[str, tuple[str, Path]], board: BoardIpsw, workspace: IpswWorkspace
) -> tuple[Path, str, str]:
    """The decompressed arm64 kernelcache Mach-O, proven against the board.

    This is what ``emit_prelink.py`` is handed and what
    ``[kernel.prelink] kernelcache_sha256`` pins -- not the zip member.  Each
    stage refuses on its own terms, so a failure says which stage failed rather
    than "the digest did not match".
    """
    entry = found.get(FETCH_KERNELCACHE)
    if entry is None:
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            f"the KernelCache component was not extracted; board.toml [ipsw] "
            f"fetch must name {FETCH_KERNELCACHE!r}",
            fetch=list(board.fetch),
        )
    member, packed = entry
    payload = _im4p_payload(packed.read_bytes())

    destination = workspace.parts_dir / KERNELCACHE_OUTPUT
    _decode_lzfse(payload, destination)

    decoded = destination.read_bytes()
    sliced = _arm64_slice(decoded)
    if sliced is not None:
        destination.write_bytes(sliced)
        decoded = sliced

    if decoded[:4] != MACHO64_MAGIC:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "the decoded kernelcache is not a 64-bit Mach-O",
            member=Path(member).name,
        )
    actual = sha256_file(destination)
    if actual != board.kernelcache_sha256:
        raise UniPadError(
            ErrorCode.IMAGE_HASH_MISMATCH,
            "the extracted kernelcache does not match the digest board.toml "
            "[kernel.prelink] declares, so it is not the kernelcache this board "
            "was qualified against",
            member=Path(member).name,
            actual_sha256=actual,
            pinned_sha256=board.kernelcache_sha256,
        )
    return destination, Path(member).name, actual


def acquire(
    lock_path: Path,
    device_dir: Path,
    workspace: IpswWorkspace,
    image_override: Path | None = None,
    root_chain_request: RootChainRequest | None = None,
) -> Acquisition:
    """Fetch (or accept) the pinned image, verify it, and unpack what the board needs.

    ``workspace`` is required and is **not** closed here: the kernelcache it
    holds has to outlive this call, because the caller passes it to a build that
    runs afterwards.  Closing it would hand back paths that resolve to nothing.

    ``image_override`` takes an already-downloaded image -- the path a Windows
    host would use, since the 6.8 GB firmware already lives under
    ``local-private/`` and re-downloading it to test this module would be
    absurd.  It is verified against exactly the same pin, so an override cannot
    smuggle in an unchecked image.

    ``root_chain_request`` is how a caller running the owner root chain gets the
    four members ``build-os-root.py`` reads out of the image.  It defaults to
    None and None means nothing is extracted for it: the two disk images are
    7.88 GiB and a caller that only wants a kernelcache must not pay for them.
    """
    pin = read_pin(lock_path)
    board = read_board_ipsw(device_dir)
    _check_the_two_declarations_agree(pin, board)
    if root_chain_request is None:
        root_chain_request = RootChainRequest()

    # open(), not "with": the kernelcache has to outlive this call, so the
    # workspace's teardown belongs to the caller and not to us.
    workspace.open()

    if image_override is not None:
        image = Path(image_override)
        if not image.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                f"the given image does not exist: {safe_location(image)}",
                image=safe_location(image),
            )
        image_bytes = image.stat().st_size
        if image_bytes != pin.size:
            raise UniPadError(
                ErrorCode.VERIFICATION_FAILED,
                "the given image is not the size the lock pins",
                image=safe_location(image),
                actual_bytes=image_bytes,
                pinned_bytes=pin.size,
            )
        actual = sha256_file(image)
        if actual != pin.sha256:
            raise UniPadError(
                ErrorCode.IMAGE_HASH_MISMATCH,
                "the given image does not match the pinned SHA-256",
                image=safe_location(image),
                actual_sha256=actual,
                pinned_sha256=pin.sha256,
            )
    else:
        image = workspace.image_dir / pin.name
        image_bytes = _download(pin.source, image, pin.size, pin.sha256)

    found = _extract(image, workspace, board)

    # The owner root chain's inputs, while the image is still here.  This runs
    # before the unlink below and not after it, because every member the chain
    # reads comes out of this file.
    root_chain = publish_root_chain(
        image,
        workspace,
        board,
        {**pin.component_pins, **board.component_pins},
        pin.name,
        root_chain_request,
    )

    # The image is 6.8 GB and nothing downstream needs it.  Deleting it here
    # rather than at workspace teardown keeps the peak usage to the download
    # plus the handful of components, instead of both plus the parts for the
    # whole build.
    if image.parent == workspace.image_dir:
        image.unlink(missing_ok=True)

    verified = _verify_components(
        found, {**pin.component_pins, **board.component_pins}
    )
    cache, member, cache_digest = _kernelcache_macho(found, board, workspace)

    return Acquisition(
        image_sha256=pin.sha256,
        image_bytes=image_bytes,
        kernelcache=cache,
        kernelcache_sha256=cache_digest,
        kernelcache_member=member,
        components={Path(entry).name: path for _, (entry, path) in found.items()},
        verified_components=verified,
        by_component={component: path for component, (_, path) in found.items()},
        root_chain=root_chain,
    )


def redact(text: str) -> str:
    """Apply every redaction this module's output must carry (PRD 9.2)."""
    return scrub_account(text)



# ---------------------------------------------------------------------------
# the command line the workflow calls
# ---------------------------------------------------------------------------
def _publish(cache: Path, destination: Path, expected_sha256: str) -> int:
    """Copy the decoded Mach-O out of the workspace, and prove the copy.

    This is what lets the workspace be temporary *and* lets the kernelcache
    outlive it.  The alternative -- handing the caller a path inside the
    workspace -- means either the workspace is never cleaned, or the caller is
    handed a path that resolves to nothing.  Copying to a caller-named place
    makes the output location deterministic, so the workflow does not have to
    parse stdout to find out where the build's input went.

    The copy is re-hashed rather than trusted: a truncated or partial copy is
    exactly the failure that would surface three build-steps later as a
    confusing prelink refusal.
    """
    if destination.parent and not destination.parent.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cache, destination)
    written = sha256_file(destination)
    if written != expected_sha256:
        destination.unlink(missing_ok=True)
        raise UniPadError(
            ErrorCode.VERIFICATION_FAILED,
            "the published kernelcache does not match the one that was verified",
            actual_sha256=written,
            expected_sha256=expected_sha256,
        )
    return destination.stat().st_size


def _summary(
    result: Acquisition,
    published: Path,
    published_bytes: int,
    devicetree: Path | None = None,
    devicetree_bytes: int = 0,
) -> dict[str, Any]:
    """The machine-readable record of one acquisition.

    Every field is a digest, a size, or a final component -- never a path.  A
    step summary and an uploaded artifact are both places PRD 9.2 applies, and
    the way to satisfy it here is not to scrub what is printed but to have
    nothing path-shaped to print: the build's input location is the caller's own
    ``--kernelcache`` argument, which it already knows.
    """
    chain = result.root_chain
    return {
        "image_sha256": result.image_sha256,
        "image_bytes": result.image_bytes,
        "kernelcache_name": published.name,
        "kernelcache_bytes": published_bytes,
        "kernelcache_sha256": result.kernelcache_sha256,
        "kernelcache_member": result.kernelcache_member,
        "devicetree_name": devicetree.name if devicetree else "",
        "devicetree_bytes": devicetree_bytes,
        "components": sorted(result.components),
        "verified_components": dict(result.verified_components),
        # The root chain, as what it is and not as "an IPSW was published".
        # ``refer_members`` is the honest part: a reconstructed image carrying
        # two members is not the pinned 6.37 GiB firmware, and a record that
        # said otherwise would let a later reader believe the whole image had
        # been verified against the lock when only its members were.
        "root_chain": {
            "os_volume_sha256": chain.os_volume_sha256,
            "systemos_volume_sha256": chain.systemos_volume_sha256,
            "refer_members": list(chain.refer_members),
            "published": chain.published,
        },
    }


def _redacted_details(details: dict[str, Any]) -> dict[str, Any]:
    """Every detail value passed through the account redaction (PRD 9.2).

    The digests and counts this module reports are safe by construction, but a
    detail is still a string that came from somewhere -- a plist, another
    process's stderr, a path -- and the cost of scrubbing all of them is one
    comprehension.
    """
    safe: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, str):
            safe[key] = redact(value)
        elif isinstance(value, list):
            safe[key] = [redact(item) if isinstance(item, str) else item for item in value]
        else:
            safe[key] = value
    return safe


def _print(payload: dict[str, Any], summary_path: str | None) -> None:
    """One JSON object on stdout, and the same one at ``summary_path``."""
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    print(redact(text))
    if summary_path:
        destination = Path(summary_path)
        if destination.parent and not destination.parent.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(redact(text) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """``python -m unipad.host.ipsw`` -- acquire, verify and unpack the pin.

    Both paths are required, and neither is a convenience default:

    ``--work``
        The temporary workspace.  It holds the 6.8 GB image and is removed
        before this process exits -- including when the acquisition fails --
        so "temporary" means gone, not "gone unless something went wrong".

    ``--kernelcache``
        Where the decoded Mach-O is published.  Required because the kernelcache
        has to outlive the workspace: the build that consumes it is the next
        step, not part of this one.

    Exactly one JSON object is printed, on success and on failure alike, and it
    carries no paths -- so the caller never parses a traceback, and a step
    summary never has to be scrubbed before it is published.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m unipad.host.ipsw",
        description=(
            "Fetch the pinned Apple IPSW, verify it whole, and publish the "
            "kernelcache the XNU build needs (PRD 8.1, 8.3, 12)."
        ),
    )
    parser.add_argument(
        "--lock",
        default="config/sources.lock.json",
        help="the sources lock carrying the ipsw pin (default: %(default)s)",
    )
    parser.add_argument(
        "--device",
        default="vendor/UniXNU-qemu",
        help="the device repository whose board.toml declares the image "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--work",
        required=True,
        help="the temporary workspace; it is removed before this command exits",
    )
    parser.add_argument(
        "--kernelcache",
        required=True,
        help="where to publish the decoded kernelcache Mach-O",
    )
    parser.add_argument(
        "--devicetree",
        default=None,
        help="also publish the board's device tree, so build-devicetree does "
        "not need the whole 6.8 GB image on disk (build-macos.sh --devicetree)",
    )
    parser.add_argument(
        "--image",
        default=None,
        help="an already-downloaded image to verify and unpack instead of "
        "fetching it; it is checked against the same pin",
    )
    parser.add_argument(
        "--os-volume",
        default=None,
        metavar="DIR",
        help="also publish the OS disk image into DIR, under the member name "
        "the image itself declares (build-os-root.py step 2 attaches it "
        "read-only to sign every 4 KiB block).  4.80 GiB.",
    )
    parser.add_argument(
        "--systemos-volume",
        default=None,
        metavar="DIR",
        help="also publish the SystemOS disk image into DIR (build-os-root.py "
        "step 5 signs it under the same key).  3.08 GiB.",
    )
    parser.add_argument(
        "--static-trustcache",
        default=None,
        metavar="DIR",
        help="also publish the StaticTrustCache IM4P as DIR/<build>/<member>, "
        "which is where build-os-root.py:150 reads it from -- the place "
        "'unixnu ipsw fetch' would have left it",
    )
    parser.add_argument(
        "--refer-ipsw",
        default=None,
        metavar="DIR",
        help="also write a reconstructed image into DIR carrying only "
        "BuildManifest.plist and the System trust-cache member, which is all "
        "build-original-system-trust.py opens the image for.  Named after the "
        "lock's ipsw.name, because the chain hard-codes that name in refer/",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help="also write the JSON summary to this path",
    )
    args = parser.parse_args(argv)

    workspace = IpswWorkspace(Path(args.work))
    devicetree: Path | None = None
    devicetree_bytes = 0
    root_chain = RootChainRequest(
        os_volume_dir=Path(args.os_volume) if args.os_volume else None,
        systemos_volume_dir=Path(args.systemos_volume) if args.systemos_volume else None,
        static_trustcache_dir=Path(args.static_trustcache)
        if args.static_trustcache
        else None,
        refer_ipsw_dir=Path(args.refer_ipsw) if args.refer_ipsw else None,
    )
    try:
        result = acquire(
            Path(args.lock),
            Path(args.device),
            workspace,
            image_override=Path(args.image) if args.image else None,
            root_chain_request=root_chain,
        )
        published_bytes = _publish(
            result.kernelcache, Path(args.kernelcache), result.kernelcache_sha256
        )
        # The device tree is published only when asked for, because
        # build-devicetree is a later milestone's step and a caller that does not
        # need it should not be handed a file it did not ask for.
        if args.devicetree:
            tree = result.by_component.get(FETCH_DEVICETREE)
            if tree is None:
                raise UniPadError(
                    ErrorCode.ARTIFACT_MISSING,
                    "the board declares no DeviceTree component to publish; "
                    "board.toml [ipsw] fetch must name %r"
                    % FETCH_DEVICETREE,
                    fetch=list(result.components),
                )
            devicetree_bytes = _publish(
                tree, Path(args.devicetree), sha256_file(tree)
            )
            devicetree = Path(args.devicetree)
    except UniPadError as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": redact(exc.message),
                    "details": _redacted_details(exc.details),
                },
            },
            args.summary,
        )
        return int(exc.exit_code)
    finally:
        # Always.  A runner that leaves 6.8 GB of Apple material behind after a
        # failed acquisition has turned "temporary" into a cache nobody audits.
        workspace.close()
        # The throwaway root goes too, when --work named one that is now empty.
        # discard_root() uses rmdir, so a root the caller shares with anything
        # else survives untouched.
        workspace.discard_root()

    _print(
        {
            "ok": True,
            "error": None,
            **_summary(
                result,
                Path(args.kernelcache),
                published_bytes,
                devicetree,
                devicetree_bytes,
            ),
        },
        args.summary,
    )
    return int(ExitCode.OK)


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
