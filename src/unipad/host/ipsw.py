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


def _load_identity(archive: zipfile.ZipFile, board: BoardIpsw) -> dict[str, Any]:
    """The image's ``BuildManifest.plist`` and the one identity that is this board.

    Two different things are proven here, and the second is the one that makes
    the URL pin mean anything:

    * the image is the firmware *version* the board declares, for the board's
      *product* -- so a URL that starts serving a newer restore is refused
      rather than silently built against;
    * it carries an ``Erase`` build identity for this device class, chip and
      board -- the same selection upstream makes, because ``Manifest`` (which
      is what turns a component name into a zip path) is per-identity.

    Reporting is deliberately coarse: the version, the build and the supported
    product types.  A URL is never echoed (PRD 9.2, and a redirect could carry
    a token), and neither are the chip identifiers, which are not secrets but
    are noise an operator cannot act on.
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

    for item in manifest.get("BuildIdentities") or []:
        if not isinstance(item, dict):
            continue
        info = item.get("Info")
        if not isinstance(info, dict):
            continue
        chip = _intish(info.get("ApChipID"))
        board_id = _intish(info.get("ApBoardID"))
        if (
            str(info.get("DeviceClass") or "") == board.device_class
            and str(info.get("RestoreBehavior") or "") == "Erase"
            and chip == board.chip_id
            and board_id == board.board_id
        ):
            return item
    raise UniPadError(
        ErrorCode.VERIFICATION_FAILED,
        "the image carries no erase build identity for this board's device "
        "class, so its components cannot be resolved",
        device_class=board.device_class,
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
    """
    pin = read_pin(lock_path)
    board = read_board_ipsw(device_dir)
    _check_the_two_declarations_agree(pin, board)

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


def _summary(result: Acquisition, published: Path, published_bytes: int) -> dict[str, Any]:
    """The machine-readable record of one acquisition.

    Every field is a digest, a size, or a final component -- never a path.  A
    step summary and an uploaded artifact are both places PRD 9.2 applies, and
    the way to satisfy it here is not to scrub what is printed but to have
    nothing path-shaped to print: the build's input location is the caller's own
    ``--kernelcache`` argument, which it already knows.
    """
    return {
        "image_sha256": result.image_sha256,
        "image_bytes": result.image_bytes,
        "kernelcache_name": published.name,
        "kernelcache_bytes": published_bytes,
        "kernelcache_sha256": result.kernelcache_sha256,
        "kernelcache_member": result.kernelcache_member,
        "components": sorted(result.components),
        "verified_components": dict(result.verified_components),
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
        "--image",
        default=None,
        help="an already-downloaded image to verify and unpack instead of "
        "fetching it; it is checked against the same pin",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help="also write the JSON summary to this path",
    )
    args = parser.parse_args(argv)

    workspace = IpswWorkspace(Path(args.work))
    try:
        result = acquire(
            Path(args.lock),
            Path(args.device),
            workspace,
            image_override=Path(args.image) if args.image else None,
        )
        published_bytes = _publish(
            result.kernelcache, Path(args.kernelcache), result.kernelcache_sha256
        )
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
        {"ok": True, "error": None, **_summary(result, Path(args.kernelcache), published_bytes)},
        args.summary,
    )
    return int(ExitCode.OK)


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
