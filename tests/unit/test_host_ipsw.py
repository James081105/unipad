"""``unipad.host.ipsw`` against a synthetic firmware built on this host.

Why this file exists
--------------------
The pinned IPSW is 6.8 GB and lives on Apple's CDN, so it cannot be exercised
from the Windows development host.  What *can* be exercised here is everything
around the bytes: the two declarations agreeing, the whole-image digest, the
firmware identity, the Manifest resolution, the per-component pins, the
workspace's lifetime, and the three-stage kernelcache unwrap.  So this file
builds a small IPSW-shaped zip -- a ``BuildManifest.plist``, an IM4P
``kernelcache.release.j120ap``, and the members the board pins -- and runs the
real :func:`~unipad.host.ipsw.acquire` over it.

The one step that is *not* proven here
--------------------------------------
LZFSE decoding needs ``/usr/bin/compression_tool``, which ships with macOS and
nothing else.  That step is stubbed in the tests that reach it, and the stub is
declared as a stub: what those tests prove is the *orchestration* around the
decode -- that the IM4P payload is unwrapped before it is decoded, that a fat
result is reduced to its arm64 slice, that a non-Mach-O is refused, and that the
final digest is the one board.toml declares.  The decode itself is a macOS
runner's job, and the module refuses rather than guessing anywhere else.

The defect this file was written after
--------------------------------------
The first draft of the module treated ``[ipsw] fetch`` as a list of *zip member
names* and hashed the raw ``kernelcache.release.*`` member against
``[kernel.prelink] kernelcache_sha256``.  Both are wrong against a real image:
``fetch`` names Manifest *components* (``"Cryptex1,SystemTrustCache"`` is one
string and a key, not a path), and the pinned digest belongs to the
LZFSE-decoded arm64 Mach-O.  Each of those mistakes refuses every genuine image,
so both are pinned here -- ``test_the_components_resolve_through_the_manifest``
and ``test_the_kernelcache_is_the_decoded_macho_not_the_member``.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from unipad.host import ipsw
from unipad.host.paths import account_name

ROOT = Path(__file__).resolve().parents[2]
REAL_LOCK = ROOT / "config" / "sources.lock.json"
REAL_DEVICE = ROOT / "local-private" / "upstream" / "UniXNU-qemu"

URL = "https://updates.cdn-apple.com/2025SpringFCS/fullrestores/122-77166/DCA6CFE8-90E9-4B92-880D-3DE171798E40/iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw"

#: The board's identifiers, exactly as ``board.toml`` spells them.
PRODUCT = "iPad7,1"
VERSION = "17.7.11"
BUILD = "21H461"
DEVICE_CLASS = "j120ap"
CHIP_ID = "0x8011"
BOARD_ID = "0x0c"

#: A plain (non-fat) 64-bit Mach-O.  The magic is the only part the module
#: inspects; the rest exists so the file has a body to hash.
MACHO = b"\xcf\xfa\xed\xfe" + b"unipad-synthetic-kernel" * 4

KERNELCACHE_MEMBER = "kernelcache.release.j120ap"
DEVICETREE_MEMBER = "Firmware/DeviceTree.j120ap.im4p"
TRUSTCACHE_MEMBER = "Firmware/static_tc"
CRYPTEX_MEMBER = "cryptex1,SystemTrustCache/cryptex1,SystemTrustCache"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# the synthetic firmware
# ---------------------------------------------------------------------------
def _der(tag: int, body: bytes) -> bytes:
    """One DER item, in the short and long forms."""
    if len(body) < 0x80:
        length = bytes([len(body)])
    else:
        raw = len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + body


def _im4p(payload: bytes, kind: bytes = b"krnl") -> bytes:
    """An IM4P wrapping ``payload``, the way the KernelCache member really is."""
    return _der(
        0x30,
        _der(0x16, b"IM4P")
        + _der(0x16, kind)
        + _der(0x16, VERSION.encode())
        + _der(0x04, payload),
    )


def _manifest(
    *,
    version: str = VERSION,
    build: str = BUILD,
    products: list[str] | None = None,
    device_class: str = DEVICE_CLASS,
    chip_id: str = CHIP_ID,
    board_id: str = BOARD_ID,
    restore: str = "Erase",
    members: dict[str, str] | None = None,
) -> bytes:
    """A ``BuildManifest.plist`` shaped like the one inside a real IPSW.

    ``members`` is merged over the default component table rather than replacing
    it, so a caller that overrides one component still declares the rest.  The
    board's ``fetch`` list names all four, and an override that dropped the
    others failed with "declares no path for the StaticTrustCache component" --
    a message about the wrong thing entirely.
    """
    table = {
        "KernelCache": KERNELCACHE_MEMBER,
        "DeviceTree": DEVICETREE_MEMBER,
        "StaticTrustCache": TRUSTCACHE_MEMBER,
        "Cryptex1,SystemTrustCache": CRYPTEX_MEMBER,
    }
    table.update(members or {})
    return plistlib.dumps(
        {
            "ProductVersion": version,
            "ProductBuildVersion": build,
            "SupportedProductTypes": products if products is not None else [PRODUCT],
            "BuildIdentities": [
                {
                    "Info": {
                        "DeviceClass": device_class,
                        "RestoreBehavior": restore,
                        "ApChipID": chip_id,
                        "ApBoardID": board_id,
                    },
                    "Manifest": {
                        component: {"Info": {"Path": path}}
                        for component, path in table.items()
                    },
                }
            ],
        }
    )


def _build_image(
    directory: Path,
    *,
    kernelcache_member: bytes | None = None,
    manifest: bytes | None = None,
    members: dict[str, bytes] | None = None,
    name: str = "iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw",
) -> Path:
    """Write an IPSW-shaped zip and return its path.

    ``members`` is merged over the default set rather than replacing it, so a
    caller that overrides one component's bytes still gets the others the
    manifest points at.  (Replacing was the first spelling, and it made every
    single-member override fail with "DeviceTree is declared as a member this
    image does not carry" -- the override looked broken when the *other*
    members were missing.)
    """
    if kernelcache_member is None:
        kernelcache_member = _im4p(b"packed-lzfse-kernel-bytes")
    if manifest is None:
        manifest = _manifest()
    body = {
        DEVICETREE_MEMBER: b"synthetic devicetree",
        TRUSTCACHE_MEMBER: b"synthetic static trust cache",
        CRYPTEX_MEMBER: b"synthetic cryptex trust cache",
    }
    body.update(members or {})
    path = directory / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("BuildManifest.plist", manifest)
        archive.writestr(KERNELCACHE_MEMBER, kernelcache_member)
        for member, content in body.items():
            archive.writestr(member, content)
    return path


def _write_declarations(
    directory: Path,
    *,
    image: Path,
    lock_url: str = URL,
    board_url: str = URL,
    kernelcache_sha256: str = _sha(MACHO),
    component_pins: dict[str, str] | None = None,
    device_class: str = DEVICE_CLASS,
    fetch: tuple[str, ...] = (
        "KernelCache",
        "DeviceTree",
        "StaticTrustCache",
        "Cryptex1,SystemTrustCache",
    ),
) -> tuple[Path, Path]:
    """Write a ``sources.lock.json`` and a ``board.toml`` that describe ``image``."""
    if component_pins is None:
        component_pins = {
            "DeviceTree.j120ap.im4p": _sha(b"synthetic devicetree"),
            "static_tc": _sha(b"synthetic static trust cache"),
        }
    lock = directory / "sources.lock.json"
    lock.write_text(
        json.dumps(
            {
                "ipsw": {
                    "name": image.name,
                    "sha256": _sha(image.read_bytes()),
                    "size": image.stat().st_size,
                    "source": lock_url,
                    "component_pins": component_pins,
                    "note": "synthetic; Apple IPSW stays in local-private/ (PRD 12).",
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    device = directory / "device"
    device.mkdir(parents=True, exist_ok=True)
    pins = "\n".join(f'"{k}" = "{v}"' for k, v in component_pins.items())
    # TOML strings are double-quoted.  Python's repr() of a list uses single
    # quotes and produces a document that does not parse, so the fetch list is
    # spelled out rather than repr'd.
    fetch_list = ", ".join(f'"{item}"' for item in fetch)
    (device / "board.toml").write_text(
        "[ipsw]\n"
        f'url = "{board_url}"\n'
        f'product = "{PRODUCT}"\n'
        f'version = "{VERSION}"\n'
        f'build = "{BUILD}"\n'
        f'device_class = "{device_class}"\n'
        f"chip_id = {int(CHIP_ID, 16)}\n"
        f"board_id = {int(BOARD_ID, 16)}\n"
        f"fetch = [{fetch_list}]\n"
        "\n"
        "[ipsw.sha256]\n"
        f"{pins}\n"
        "\n"
        "[kernel.prelink]\n"
        f'kernelcache_sha256 = "{kernelcache_sha256}"\n',
        encoding="utf-8",
    )
    return lock, device


@pytest.fixture
def pair(tmp_path: Path):
    """A synthetic image plus the two declarations that describe it."""
    image = _build_image(tmp_path)
    lock, device = _write_declarations(tmp_path, image=image)
    return image, lock, device


@pytest.fixture
def stub_decode(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the macOS-only LZFSE decode.

    The stub is deliberately faithful to the contract the module relies on: it
    writes whatever it is given to ``destination`` and says nothing.  What it
    writes is decided per test, so each test controls the bytes that reach the
    slice selection and the digest check.
    """
    written: dict[str, Any] = {}

    def _decode(payload: bytes, destination: Path) -> None:
        written["payload"] = payload
        destination.write_bytes(written.get("result", MACHO))

    monkeypatch.setattr(ipsw, "_decode_lzfse", _decode)
    return written


# ---------------------------------------------------------------------------
# the two declarations
# ---------------------------------------------------------------------------
def test_the_real_lock_and_the_real_board_name_the_same_image() -> None:
    """The shipped pin and the shipped declaration must already agree.

    This is the one test in the file that touches the repository's real data,
    and it is here because the whole module rests on those two agreeing.  If it
    fails, every synthetic test below is testing a fiction.
    """
    if not REAL_LOCK.is_file() or not REAL_DEVICE.is_dir():
        pytest.skip("the pinned IPSW material is not present on this host")
    pin = ipsw.read_pin(REAL_LOCK)
    board = ipsw.read_board_ipsw(REAL_DEVICE)
    assert pin.source == board.url, (pin.name, board.build)
    assert board.chip_id == 0x8011, board
    assert board.board_id == 12, board
    assert board.fetch, board


def test_a_url_divergence_between_the_lock_and_the_board_is_refused(
    tmp_path: Path,
) -> None:
    """A pin that no longer matches the declaration is not a pin."""
    image = _build_image(tmp_path)
    lock, device = _write_declarations(
        tmp_path, image=image, board_url=URL.replace("122-77166", "123-99999")
    )
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        with workspace:
            ipsw.acquire(lock, device, workspace, image_override=image)
    error = caught.value
    assert error.code is ipsw.ErrorCode.CONFIG_INVALID, error
    # The refusal must be actionable without echoing the CDN path (PRD 9.2: a
    # redirect or query string can carry a token).  The host is enough to tell
    # an operator which CDN; the path is not.
    rendered = repr(error) + repr(error.details)
    assert error.details.get("lock_source_host") == "updates.cdn-apple.com", error.details
    assert error.details.get("board_source_host") == "updates.cdn-apple.com", error.details
    assert "122-77166" not in rendered, rendered
    assert URL not in rendered, rendered
    # ...and it still says which file each side wants, or the operator cannot
    # tell which of the two is stale.
    assert error.details.get("lock_source_tail") == image.name, error.details


def test_an_unpinned_image_is_refused(tmp_path: Path) -> None:
    """A null sha256 is a refusal, not a warning to fetch anyway."""
    lock = tmp_path / "sources.lock.json"
    lock.write_text(
        json.dumps({"ipsw": {"name": "x.ipsw", "sha256": None, "size": 10, "source": URL}}),
        encoding="utf-8",
    )
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.read_pin(lock)
    assert caught.value.code is ipsw.ErrorCode.CONFIG_INVALID, caught.value
    assert "unpinned" in str(caught.value), caught.value


def test_a_board_without_a_kernelcache_pin_is_refused(tmp_path: Path) -> None:
    """Without the board's own digest, an extracted kernelcache is unprovable."""
    device = tmp_path / "device"
    device.mkdir()
    (device / "board.toml").write_text(
        '[ipsw]\nurl = "https://example.invalid/x.ipsw"\n'
        f'product = "{PRODUCT}"\nversion = "{VERSION}"\nbuild = "{BUILD}"\n'
        f'device_class = "{DEVICE_CLASS}"\nchip_id = {int(CHIP_ID, 16)}\n'
        f"board_id = {int(BOARD_ID, 16)}\nfetch = []\n",
        encoding="utf-8",
    )
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.read_board_ipsw(device)
    assert caught.value.code is ipsw.ErrorCode.CONFIG_INVALID, caught.value


# ---------------------------------------------------------------------------
# the image itself
# ---------------------------------------------------------------------------
def test_the_whole_image_is_verified_before_anything_is_unpacked(
    tmp_path: Path, pair
) -> None:
    """A substituted image is refused whole; nothing is taken from it.

    The substitution keeps the length and changes the bytes, because that is the
    case the digest exists for: a same-size swap is what a redirected download
    or a CDN cache mix-up actually produces, and appending a byte would be
    caught by the length check first.
    """
    image, lock, device = pair
    tampered = tmp_path / "tampered.ipsw"
    body = bytearray(image.read_bytes())
    body[len(body) // 2] ^= 0xFF
    tampered.write_bytes(bytes(body))
    assert tampered.stat().st_size == image.stat().st_size
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        with workspace:
            ipsw.acquire(lock, device, workspace, image_override=tampered)
            # Unreachable, and stated so: the point is that acquire refuses
            # before extraction, so nothing is ever written here.
            raise AssertionError("acquire accepted a substituted image")
    assert caught.value.code is ipsw.ErrorCode.IMAGE_HASH_MISMATCH, caught.value
    with ipsw.IpswWorkspace(tmp_path / "ws") as fresh:
        assert not any(fresh.parts_dir.iterdir()), list(fresh.parts_dir.iterdir())


def test_the_image_size_is_checked(tmp_path: Path, pair) -> None:
    """The length is checked separately, so a truncation says so."""
    image, lock, device = pair
    short = tmp_path / "short.ipsw"
    short.write_bytes(image.read_bytes()[:-4])
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.acquire(lock, device, workspace, image_override=short)
    assert caught.value.code is ipsw.ErrorCode.VERIFICATION_FAILED, caught.value


def test_the_declared_firmware_version_is_checked(tmp_path: Path) -> None:
    """The right URL serving the wrong firmware is still the wrong firmware."""
    image = _build_image(tmp_path, manifest=_manifest(version="18.0", build="22A100"))
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.acquire(lock, device, workspace, image_override=image)
    error = caught.value
    assert error.code is ipsw.ErrorCode.VERIFICATION_FAILED, error
    assert error.details.get("image_version") == "18.0", error.details
    assert error.details.get("board_version") == VERSION, error.details


def test_an_image_for_another_product_is_refused(tmp_path: Path) -> None:
    """An iPhone restore of the same build is not this board's image."""
    image = _build_image(tmp_path, manifest=_manifest(products=["iPhone9,1"]))
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.acquire(lock, device, workspace, image_override=image)
    assert caught.value.code is ipsw.ErrorCode.VERIFICATION_FAILED, caught.value
    assert caught.value.details.get("board_product") == PRODUCT, caught.value.details


def test_an_image_with_no_erase_identity_for_this_board_is_refused(
    tmp_path: Path,
) -> None:
    """The Manifest is per-identity, so the wrong identity resolves nothing."""
    image = _build_image(tmp_path, manifest=_manifest(device_class="j127ap"))
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.acquire(lock, device, workspace, image_override=image)
    assert caught.value.code is ipsw.ErrorCode.VERIFICATION_FAILED, caught.value
    assert caught.value.details.get("device_class") == DEVICE_CLASS, caught.value.details


def test_an_image_with_no_manifest_is_refused(tmp_path: Path) -> None:
    """Without the Manifest the components cannot be resolved at all."""
    image = _build_image(tmp_path, manifest=b"")
    # A zero-length member still exists in the zip, so it is found and then
    # fails to parse -- which is the honest failure.
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.acquire(lock, device, workspace, image_override=image)
    assert caught.value.code is ipsw.ErrorCode.INVALID_PACKAGE, caught.value


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def test_the_components_resolve_through_the_manifest(tmp_path: Path, pair, stub_decode) -> None:
    """``fetch`` names Manifest components, not zip members.

    The first draft of this module looked each ``fetch`` string up as a literal
    zip entry, so ``"Cryptex1,SystemTrustCache"`` was never found and the comma
    form alone refused every real image.  Here the member paths deliberately
    differ from the component keys, and the extraction still has to land them.
    """
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        result = ipsw.acquire(lock, device, workspace, image_override=image)
    landed = {path.name for path in result.components.values()}
    assert landed == {
        "kernelcache.release.j120ap",
        "DeviceTree.j120ap.im4p",
        "static_tc",
        "cryptex1,SystemTrustCache",
    }, landed
    assert result.kernelcache_member == KERNELCACHE_MEMBER, result


def test_a_component_pin_mismatch_is_refused(tmp_path: Path) -> None:
    """A pinned component that does not match its pin stops the run."""
    image = _build_image(
        tmp_path, members={TRUSTCACHE_MEMBER: b"a different trust cache"}
    )
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.acquire(lock, device, workspace, image_override=image)
    error = caught.value
    assert error.code is ipsw.ErrorCode.IMAGE_HASH_MISMATCH, error
    assert error.details.get("component") == "StaticTrustCache", error.details


def test_an_unpinned_component_is_reported_as_unproven(tmp_path: Path, pair, stub_decode) -> None:
    """Silence about what was not checked reads as though it was.

    The cryptex trust cache is the one component the synthetic lock does not
    pin, so it is the one that must come back with an empty digest rather than
    being omitted from the record.
    """
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        result = ipsw.acquire(lock, device, workspace, image_override=image)
    assert result.verified_components["cryptex1,SystemTrustCache"] == "", (
        result.verified_components
    )
    assert result.verified_components["DeviceTree.j120ap.im4p"], result.verified_components


def test_a_member_that_would_climb_out_of_the_workspace_does_not(
    tmp_path: Path, pair, stub_decode
) -> None:
    """A member path with ``..`` is flattened into the workspace, not followed.

    The Manifest is data from a downloaded file, so its paths are untrusted.
    ``part_path`` takes the final component and checks the result stays inside
    ``parts/``; a ``../`` path therefore lands inside as its leaf and nothing is
    written outside.
    """
    image, lock, device = pair
    escaping = tmp_path / "escaping"
    escaping.mkdir()
    # The zip really does carry the climbing name -- zipfile stores it verbatim
    # -- so the member resolves and it is part_path() that decides where it
    # lands.  (An image without the entry would be refused earlier, for a
    # different reason, and the test would prove nothing about escapes.)
    image = _build_image(
        tmp_path,
        manifest=_manifest(
            members={
                "KernelCache": KERNELCACHE_MEMBER,
                "DeviceTree": "../../outside/devicetree",
            }
        ),
        members={"../../outside/devicetree": b"a member that tries to climb out"},
    )
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        result = ipsw.acquire(lock, device, workspace, image_override=image)
    assert (escaping / "devicetree").exists() is False, "the member escaped"
    assert result.components["devicetree"].parent == workspace.parts_dir, result


# ---------------------------------------------------------------------------
# the kernelcache
# ---------------------------------------------------------------------------
def test_the_kernelcache_is_the_decoded_macho_not_the_member(
    tmp_path: Path, pair, stub_decode
) -> None:
    """The pinned digest belongs to the decoded Mach-O, not to the member.

    This is the second of the two defects the first draft had.  Hashing the raw
    ``kernelcache.release.*`` member against ``kernelcache_sha256`` refuses
    every genuine image, because upstream decodes the IM4P payload out of it
    first.

    Everything is asserted inside the workspace's ``with`` block: the parts are
    temporary by design, so reading them after teardown would be asserting
    about deleted files.
    """
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        result = ipsw.acquire(lock, device, workspace, image_override=image)
        assert result.kernelcache.name == "kernelcache.macho", result
        assert result.kernelcache_sha256 == _sha(MACHO), result
        # The IM4P payload reached the decoder, and the member itself did not.
        assert stub_decode["payload"] == b"packed-lzfse-kernel-bytes", stub_decode
        member = result.components["kernelcache.release.j120ap"]
        assert member.read_bytes() != MACHO, "the raw member was handed back as the Mach-O"
        assert result.kernelcache.read_bytes() == MACHO, "the decoded Mach-O is not what the build gets"


def test_a_kernelcache_that_is_not_the_declared_one_is_refused(
    tmp_path: Path, pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decode that yields some other Mach-O is refused, not built with."""

    def _decode(payload: bytes, destination: Path) -> None:
        destination.write_bytes(b"\xcf\xfa\xed\xfe" + b"some other kernel")

    monkeypatch.setattr(ipsw, "_decode_lzfse", _decode)
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        with workspace:
            ipsw.acquire(lock, device, workspace, image_override=image)
    assert caught.value.code is ipsw.ErrorCode.IMAGE_HASH_MISMATCH, caught.value


def test_a_decode_that_is_not_a_macho_is_refused(
    tmp_path: Path, pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``emit_prelink.py`` must not be handed something that is not a Mach-O."""

    def _decode(payload: bytes, destination: Path) -> None:
        destination.write_bytes(b"not a mach-o at all")

    monkeypatch.setattr(ipsw, "_decode_lzfse", _decode)
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        with workspace:
            ipsw.acquire(lock, device, workspace, image_override=image)
    assert caught.value.code is ipsw.ErrorCode.INVALID_PACKAGE, caught.value


def test_a_fat_decode_is_reduced_to_its_arm64_slice(
    tmp_path: Path, pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The board pins one arm64 Mach-O, so a fat result is sliced.

    A fat kernelcache is the normal case on Apple silicon: the same file carries
    several architectures.  Taking the wrong slice would produce a kernelcache
    whose digest does not match -- but only after a full build -- so the slice is
    chosen here and the digest confirms it.
    """
    arm64 = b"\xcf\xfa\xed\xfe" + b"the arm64 slice" * 3
    other = b"\xcf\xfa\xed\xfe" + b"an x86_64 slice" * 3
    fat = (
        struct.pack(">II", 0xCAFEBABE, 2)
        + struct.pack(">5I", 0x0100000C, 0, 48, len(arm64), 0)
        + struct.pack(">5I", 0x01000007, 0, 48 + len(arm64), len(other), 0)
        + arm64
        + other
    )

    def _decode(payload: bytes, destination: Path) -> None:
        destination.write_bytes(fat)

    monkeypatch.setattr(ipsw, "_decode_lzfse", _decode)
    image = _build_image(tmp_path)
    lock, device = _write_declarations(
        tmp_path, image=image, kernelcache_sha256=_sha(arm64)
    )
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        result = ipsw.acquire(lock, device, workspace, image_override=image)
        assert result.kernelcache.read_bytes() == arm64, "the wrong slice was kept"
        assert result.kernelcache_sha256 == _sha(arm64), result


def test_a_fat_decode_with_no_arm64_slice_is_refused(
    tmp_path: Path, pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two x86_64 slices is a refusal, not a coin flip."""
    other = b"\xcf\xfa\xed\xfe" + b"an x86_64 slice" * 3
    fat = struct.pack(">II", 0xCAFEBABE, 2) + struct.pack(
        ">5I", 0x01000007, 0, 48, len(other), 0
    ) * 2 + other + other

    def _decode(payload: bytes, destination: Path) -> None:
        destination.write_bytes(fat)

    monkeypatch.setattr(ipsw, "_decode_lzfse", _decode)
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        with workspace:
            ipsw.acquire(lock, device, workspace, image_override=image)
    assert caught.value.code is ipsw.ErrorCode.VERIFICATION_FAILED, caught.value


def test_a_member_that_is_not_an_im4p_is_refused(tmp_path: Path, pair, stub_decode) -> None:
    """A KernelCache member that is not an IM4P says so, at that stage."""
    image = _build_image(tmp_path, kernelcache_member=b"raw mach-o, no IM4P")
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        with workspace:
            ipsw.acquire(lock, device, workspace, image_override=image)
    assert caught.value.code is ipsw.ErrorCode.INVALID_PACKAGE, caught.value
    assert "IM4P" in str(caught.value), caught.value


# ---------------------------------------------------------------------------
# lifetime
# ---------------------------------------------------------------------------
def test_the_image_is_deleted_the_moment_extraction_finishes(
    tmp_path: Path, pair, stub_decode
) -> None:
    """6.8 GB of Apple material does not sit on the runner for the whole build."""
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        ipsw.acquire(lock, device, workspace, image_override=image)
        assert workspace.image_dir.is_dir(), "the image directory vanished"
        assert not any(workspace.image_dir.iterdir()), list(
            workspace.image_dir.iterdir()
        )
        # The parts are still there -- deleting them here would defeat the call.
        assert any(workspace.parts_dir.iterdir()), list(workspace.parts_dir.iterdir())


def test_the_kernelcache_outlives_the_acquire_call(tmp_path: Path, pair, stub_decode) -> None:
    """``acquire`` must not close a workspace it does not own.

    The caller hands the kernelcache to a build that runs afterwards, so a
    function that deleted its own outputs would hand back paths resolving to
    nothing -- and the caller would find out inside the build, not here.
    """
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        result = ipsw.acquire(lock, device, workspace, image_override=image)
        assert workspace.closed is False, "acquire closed the caller's workspace"
        assert result.kernelcache.is_file(), result
        assert result.kernelcache_sha256 == _sha(MACHO), result


def test_a_closed_workspace_cannot_be_reused(tmp_path: Path, pair, stub_decode) -> None:
    """Re-entering a closed workspace is a refusal, not a dead path.

    The first draft returned silently from ``__enter__`` when ``path`` was
    already set, so a second ``with`` handed back a directory that had been
    deleted -- and every later use failed with a confusing "no such file"
    somewhere else instead of at the point of the mistake.
    """
    image, lock, device = pair
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        ipsw.acquire(lock, device, workspace, image_override=image)
    with pytest.raises(ipsw.UniPadError) as caught:
        with workspace:
            pass
    assert caught.value.code is ipsw.ErrorCode.CONFIG_INVALID, caught.value


def test_the_workspace_is_removed_on_failure(tmp_path: Path) -> None:
    """A failed acquisition leaves nothing behind, including the image."""
    image = _build_image(
        tmp_path, members={TRUSTCACHE_MEMBER: b"a different trust cache"}
    )
    lock, device = _write_declarations(tmp_path, image=image)
    root = tmp_path / "ws"
    workspace = ipsw.IpswWorkspace(root)
    with pytest.raises(ipsw.UniPadError):
        with workspace:
            ipsw.acquire(lock, device, workspace, image_override=image)
    # The temporary directory this object created is gone...
    assert not any(root.glob("ipsw-*")), list(root.glob("ipsw-*"))
    # ...and nothing was written anywhere else in the caller's tree either.
    assert not list(root.rglob("*")), list(root.rglob("*"))


def test_a_caller_supplied_root_keeps_its_own_contents(tmp_path: Path, pair, stub_decode) -> None:
    """``close()`` removes what it created, and nothing the caller already had.

    A workflow that points the workspace at a directory it also keeps logs in
    must not lose the logs.  The temporary directory inside it is what goes.
    """
    image, lock, device = pair
    root = tmp_path / "shared"
    root.mkdir()
    (root / "keep.txt").write_text("not mine", encoding="utf-8")
    with ipsw.IpswWorkspace(root) as workspace:
        ipsw.acquire(lock, device, workspace, image_override=image)
    assert (root / "keep.txt").is_file(), "a pre-existing file was deleted"
    assert not any(root.glob("ipsw-*")), list(root.glob("ipsw-*"))


def test_using_the_workspace_before_entering_it_is_refused() -> None:
    """A path request outside the context manager is a refusal, not a crash."""
    with pytest.raises(ipsw.UniPadError) as caught:
        _ = ipsw.IpswWorkspace().image_dir
    assert caught.value.code is ipsw.ErrorCode.CONFIG_INVALID, caught.value


# ---------------------------------------------------------------------------
# the download
# ---------------------------------------------------------------------------
def test_the_download_is_verified_while_it_streams(tmp_path: Path, pair) -> None:
    """Length and digest are checked as the bytes arrive, not afterwards.

    A ``file://`` URL is used so the test exercises the real streaming path --
    ``urlopen``, the ``Content-Length`` check, the chunked copy and the digest --
    without touching the network (the suite's determinism rule, D3).
    """
    image, _, _ = pair
    destination = tmp_path / "downloaded.ipsw"
    written = ipsw._download(
        image.as_uri(), destination, image.stat().st_size, _sha(image.read_bytes())
    )
    assert written == image.stat().st_size, written
    assert _sha(destination.read_bytes()) == _sha(image.read_bytes())


def test_a_declared_length_that_disagrees_with_the_pin_is_refused(
    tmp_path: Path, pair
) -> None:
    image, _, _ = pair
    destination = tmp_path / "downloaded.ipsw"
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw._download(
            image.as_uri(), destination, image.stat().st_size + 1, _sha(image.read_bytes())
        )
    assert caught.value.code is ipsw.ErrorCode.VERIFICATION_FAILED, caught.value
    assert caught.value.details.get("declared_length") == image.stat().st_size, caught.value.details


def test_a_download_whose_digest_disagrees_with_the_pin_is_refused(
    tmp_path: Path, pair
) -> None:
    image, _, _ = pair
    destination = tmp_path / "downloaded.ipsw"
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw._download(
            image.as_uri(),
            destination,
            image.stat().st_size,
            "0" * 64,
        )
    assert caught.value.code is ipsw.ErrorCode.IMAGE_HASH_MISMATCH, caught.value
    assert destination.exists() is False or destination.stat().st_size >= 0


def test_the_lzfse_decode_refuses_away_from_macos(tmp_path: Path) -> None:
    """The one macOS-only step refuses rather than producing a plausible file.

    ``compression_tool`` ships with macOS.  Everywhere else the honest answer is
    "this cannot be done here", because writing the LZFSE bytes out under the
    name ``kernelcache.macho`` would fail the digest check three steps later and
    read as a corrupt download rather than a missing tool.
    """
    destination = tmp_path / "kernelcache.macho"
    if Path(ipsw.COMPRESSION_TOOL).is_file():
        pytest.skip("this host has compression_tool; the refusal is not reachable")
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw._decode_lzfse(b"packed", destination)
    error = caught.value
    assert error.code is ipsw.ErrorCode.BLOCKED, error
    assert "compression_tool" in str(error), error
    assert destination.exists() is False, "a file was written by a step that refused"


# ---------------------------------------------------------------------------
# what the output may say (PRD 9.2)
# ---------------------------------------------------------------------------
def test_no_error_names_the_host_account(tmp_path: Path, pair) -> None:
    """An absolute path on this host embeds the account name; it must not travel.

    The image override is given as a path under ``tmp_path``, which on this host
    lives under ``pytest-of-<account>``, so every refusal that echoed
    ``str(path)`` would carry the username into the report.
    """
    name = account_name()
    if not name:
        pytest.skip("no host account name is determinable on this host")
    image, lock, device = pair
    missing = tmp_path / "nope" / f"{name}-missing.ipsw"
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        ipsw.acquire(lock, device, workspace, image_override=missing)
    rendered = f"{caught.value!r} {caught.value.details!r}"
    assert name not in rendered, f"the account name reached the error: {rendered}"
    # ...and the operator can still tell which file was meant.
    assert "missing.ipsw" in rendered, rendered


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def _run_cli(tmp_path: Path, *args: str) -> tuple[int, dict]:
    """Call the CLI in-process and parse the one JSON object it printed."""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = ipsw.main(list(args))
    return code, json.loads(buffer.getvalue())


def _cli_args(lock: Path, device: Path, work: Path, cache: Path, image: Path) -> list[str]:
    return [
        "--lock", str(lock),
        "--device", str(device),
        "--work", str(work),
        "--kernelcache", str(cache),
        "--image", str(image),
    ]


def test_the_cli_publishes_the_kernelcache_where_the_caller_asked(
    tmp_path: Path, pair, stub_decode
) -> None:
    """The workflow's contract: a zero exit, a real Mach-O at a known path.

    The output location is the caller's own argument rather than something read
    back out of stdout, which is what lets the workspace be temporary while the
    kernelcache outlives it.
    """
    image, lock, device = pair
    cache = tmp_path / "out" / "kernelcache.macho"
    code, payload = _run_cli(tmp_path, *_cli_args(lock, device, tmp_path / "work", cache, image))
    assert code == 0, payload
    assert payload["ok"] is True, payload
    assert payload["error"] is None, payload
    assert cache.is_file(), payload
    assert _sha(cache.read_bytes()) == _sha(MACHO), payload
    assert payload["kernelcache_sha256"] == _sha(MACHO), payload
    assert payload["kernelcache_name"] == "kernelcache.macho", payload
    assert payload["kernelcache_bytes"] == len(MACHO), payload
    assert payload["image_bytes"] == image.stat().st_size, payload
    assert payload["kernelcache_member"] == KERNELCACHE_MEMBER, payload


def test_the_cli_removes_the_workspace_on_success(
    tmp_path: Path, pair, stub_decode
) -> None:
    """6.8 GB of Apple material does not survive the command that needed it."""
    image, lock, device = pair
    work = tmp_path / "work"
    code, payload = _run_cli(
        tmp_path, *_cli_args(lock, device, work, tmp_path / "out" / "k.macho", image)
    )
    assert code == 0, payload
    assert not work.exists(), f"the workspace survived: {list(work.rglob('*'))}"


def test_the_cli_removes_the_workspace_on_failure_too(tmp_path: Path, pair) -> None:
    """The failure path is the one that leaks, because nobody looks at it."""
    image, lock, device = pair
    work = tmp_path / "work"
    tampered = tmp_path / "tampered.ipsw"
    body = bytearray(image.read_bytes())
    body[10] ^= 0xFF
    tampered.write_bytes(bytes(body))
    code, payload = _run_cli(
        tmp_path, *_cli_args(lock, device, work, tmp_path / "out" / "k.macho", tampered)
    )
    assert code != 0, payload
    assert not work.exists(), f"the workspace survived: {list(work.rglob('*'))}"
    assert not (tmp_path / "out" / "k.macho").exists(), "a failed run published an output"


def test_the_cli_writes_the_summary_file_too(tmp_path: Path, pair, stub_decode) -> None:
    """A step summary is read from a file, so the CLI writes one on request."""
    image, lock, device = pair
    summary = tmp_path / "deep" / "nested" / "ipsw.json"
    code, payload = _run_cli(
        tmp_path,
        *_cli_args(lock, device, tmp_path / "work", tmp_path / "out" / "k.macho", image),
        "--summary", str(summary),
    )
    assert code == 0, payload
    assert summary.is_file(), summary
    on_disk = json.loads(summary.read_text(encoding="utf-8"))
    assert on_disk["ok"] is True, on_disk
    assert on_disk["kernelcache_sha256"] == payload["kernelcache_sha256"], on_disk


def test_the_cli_reports_a_refusal_as_json_with_a_nonzero_exit(tmp_path: Path, pair) -> None:
    """A failure must not be a traceback: the caller branches on the code."""
    image, lock, device = pair
    tampered = tmp_path / "tampered.ipsw"
    body = bytearray(image.read_bytes())
    body[10] ^= 0xFF
    tampered.write_bytes(bytes(body))
    code, payload = _run_cli(
        tmp_path, *_cli_args(lock, device, tmp_path / "work", tmp_path / "out" / "k.macho", tampered)
    )
    assert code != 0, payload
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "image_hash_mismatch", payload
    assert set(payload["error"]["details"]) >= {"actual_sha256", "pinned_sha256"}, payload


def test_the_cli_output_carries_no_paths_and_no_account_name(
    tmp_path: Path, pair, stub_decode
) -> None:
    """PRD 9.2, structurally: nothing path-shaped is printed at all.

    The first spelling of this CLI printed the kernelcache's absolute path,
    which on this host embeds the account name -- and redacting it produced a
    path the build could not use, so the choice was "leak" or "unusable".  The
    resolution is that the output location is the caller's own argument and
    stdout carries only digests, sizes and final components.
    """
    name = account_name()
    if not name:
        pytest.skip("no host account name is determinable on this host")
    image, lock, device = pair
    code, payload = _run_cli(
        tmp_path, *_cli_args(lock, device, tmp_path / "work", tmp_path / "out" / "k.macho", image)
    )
    assert code == 0, payload
    rendered = json.dumps(payload)
    assert name not in rendered, rendered
    assert str(tmp_path) not in rendered, rendered
    assert "kernelcache.release.j120ap" in payload["components"], payload


def test_the_cli_refuses_without_the_two_required_paths(pair) -> None:
    """Both are required: a default would create a directory nobody cleans."""
    for missing in (("--work", "w"), ("--kernelcache", "k")):
        argv = ["--lock", "x", "--device", "y"]
        argv += [a for pair_ in ((("--work", "w"), ("--kernelcache", "k")))
                 if pair_ != missing for a in pair_]
        with pytest.raises(SystemExit) as caught:
            ipsw.main(argv)
        assert caught.value.code == 2, (missing, caught.value)


def test_discard_root_removes_only_an_empty_root(tmp_path: Path, pair, stub_decode) -> None:
    """``discard_root`` is ``rmdir``, so it cannot delete a caller's content.

    A workflow that points ``--work`` at a directory it also uses for logs must
    keep the logs; a root created purely to hold the workspace goes away.  Both
    are the same call, and the difference is whether anything else is in it.
    """
    image, lock, device = pair
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "keep.txt").write_text("not mine", encoding="utf-8")
    with ipsw.IpswWorkspace(shared) as workspace:
        ipsw.acquire(lock, device, workspace, image_override=image)
    assert workspace.discard_root() is False, "a non-empty root was removed"
    assert (shared / "keep.txt").is_file(), "a pre-existing file was deleted"

    throwaway = tmp_path / "throwaway"
    with ipsw.IpswWorkspace(throwaway) as workspace:
        ipsw.acquire(lock, device, workspace, image_override=image)
    assert workspace.discard_root() is True, "an empty root was left behind"
    assert not throwaway.exists(), throwaway


def test_a_chip_id_divergence_is_reported_not_refused(tmp_path: Path, stub_decode) -> None:
    """A cross-check the operator cannot see must not stop the build.

    The first real macOS run refused the genuine pinned image with "no erase
    build identity for this board's device class", because at least one of the
    four fields does not read the way board.toml spells it.  Device class plus
    restore behaviour identify the board unambiguously, so the module proceeds on
    those and *records* the chip/board divergence instead of swallowing it.
    """
    image = _build_image(
        tmp_path,
        manifest=_manifest(chip_id="0x8012", board_id="0x0d"),
    )
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with workspace:
        result = ipsw.acquire(lock, device, workspace, image_override=image)
    assert result.kernelcache_sha256 == _sha(MACHO), result
    divergence = ipsw.DIVERGENT_IDENTITY[0]
    assert divergence, "a chip/board divergence was swallowed rather than recorded"
    assert divergence["image_chip_id"] == 0x8012, divergence
    assert divergence["board_chip_id"] == 0x8011, divergence


def test_two_matching_identities_are_a_refusal_not_a_guess(tmp_path: Path, stub_decode) -> None:
    """Ambiguity is refused, with the candidates listed.

    Two erase identities for the same device class means the Manifest -- and
    therefore every component path -- could come from either.  That is a guess,
    and the refusal has to say what it saw so the next attempt can act on it.
    """
    manifest = plistlib.loads(_manifest())
    identity = manifest["BuildIdentities"][0]
    # Two identities for the same device class and restore behaviour, neither
    # matching the board's chip id -- so the strict four-field rule finds
    # nothing and the device-class rule finds two.  That is the shape the
    # ambiguity branch exists for.
    twin = json.loads(json.dumps(identity))
    twin["Info"]["ApChipID"] = "0x9999"
    manifest["BuildIdentities"].append(twin)
    identity["Info"]["ApChipID"] = "0x8888"
    image = _build_image(tmp_path, manifest=plistlib.dumps(manifest))
    lock, device = _write_declarations(tmp_path, image=image)
    workspace = ipsw.IpswWorkspace(tmp_path / "ws")
    with pytest.raises(ipsw.UniPadError) as caught:
        with workspace:
            ipsw.acquire(lock, device, workspace, image_override=image)
    error = caught.value
    assert error.code is ipsw.ErrorCode.VERIFICATION_FAILED, error
    assert error.details.get("matching_identities") == 2, error.details
    # The refusal lists what the image actually carried.
    assert len(error.details.get("image_identities", ())) == 2, error.details
