"""Display, audio and install-progress decisions for the guest (PRD 3.2 P1 (4)).

PRD 3.2 P1 reads ``旋转、可选分辨率、音频和安装包体验`` -- rotation, selectable
resolution, audio and the install-package experience.  This module is the
*decision* layer for all four: it turns a request into a validated,
closed-vocabulary answer plus the exact argv pieces the launcher needs.  It is
deliberately **not** the argv emitter -- :mod:`unipad.host.qemu` stays the
single place that builds the command line, and splices this module's
:meth:`DisplayDecision.to_qemu_fragments` into it.

Four decisions live here:

1. **Resolution** (:data:`DISPLAY_MODES`) -- a closed catalogue of framebuffer
   geometries.  A pair outside the catalogue is REFUSED with ``config_invalid``.
   It is never snapped to a neighbour: PRD 9.2 requires a screenshot to be
   evidence, and a framebuffer that is not the one that was asked for makes the
   recorded geometry disagree with the pixels in the image.
2. **Rotation** (:class:`Rotation`) -- an explicit request, defaulting to the
   unrotated board orientation.  A 90 or 270 degree turn swaps the framebuffer
   axes; 0 and 180 do not.
3. **Audio** (:class:`AudioDecision`) -- ``none`` is a valid, explicit choice
   that emits nothing.  The guest audio stack is UNVERIFIED on this host (there
   is no iPadOS guest to hear it), so the module must be able to say "no audio
   device emitted" as a first-class answer rather than pretend.
4. **Install progress** (:class:`InstallStageProgress`, :func:`render_progress`) --
   the host-side half of the install-package experience, using the stage
   vocabulary :mod:`unipad.ipa.installer` actually produces.

Route disclosure
----------------

A decision is only half of the answer: the other half is which parts of it the
argv actually carried.  On the ``ramfb`` route the two agree -- the ``-fw_cfg``
item *is* the requested geometry.  On the ``virtio-gpu`` route (ADR-0004 step 3)
the guest driver negotiates its own geometry, so the validated resolution and
rotation never reach the guest.  That is a real property of the route, not a
bug, and this module does not try to force geometry onto it.  What it must not
do is stay silent about it: :attr:`DisplayDecision.framebuffer_in_argv` and
:attr:`DisplayDecision.route_limitations` state the fact in machine-readable
form, :attr:`DisplayDecision.route_notes` states it in prose.

Where that disclosure reaches an operator is stated here rather than implied,
because the answer is not symmetric and the asymmetry used to be papered over:

* the **prose** reaches an operator today, through ``config/machine.json``
  ``display.resolution_note`` and ``display.rotation_note``.  Those notes are
  the operator-facing surface, and ``tests/unit/test_host_display_route.py``
  pins that they name the route and the drop;
* the **machine-readable** keys do not reach anything yet.  They live in
  :meth:`DisplayDecision.to_dict`, and no module outside this one names them:
  ``QemuLauncher.build_args`` consumes a decision through
  :meth:`DisplayDecision.to_qemu_fragments` and never asks for its evidence
  view, and ``cmd_start`` builds ``launch.json`` from ``LaunchResult.to_dict``,
  which carries no display block.  The one place the product calls
  ``to_dict()`` on a decision is :mod:`unipad.acceptance.p1`'s own display
  check, and it reads ``framebuffer`` -- the geometry -- not the disclosure.

So the drop is disclosed, and it is disclosed in the two forms this module can
actually deliver; it is **not** in any evidence bundle, and nothing here claims
otherwise.  ``tests/unit/test_host_display_disclosure.py`` fails if the prose
promises a consumer the keys do not have, and skips itself the moment one
lands.  The failure mode being designed against is a run that succeeds, a
screenshot of whatever the guest negotiated, and PRD 9.2 evidence that
describes a geometry nobody configured.

Provenance of the catalogue
---------------------------

``ramfb-pinned`` (1376x1024) is the geometry this project boots: it is
``config/machine.json`` ``display`` and ``run-qemu.py:39-43``.  The other four
entries are iPad-class *reference panel* geometries rounded up to the
framebuffer constraint.  Their panel pixel dimensions are published device
specifications, **not measurements from this host**, and no resolution other
than the pinned one has been shown to render on a real iPadOS guest.  They are
offered so an operator can ask for a different framebuffer; whether the guest
renders correctly at one is a field question this module cannot answer.

The constraint (``config/machine.json`` display note): width must be a multiple
of 16 and rows a multiple of 64 bytes.  At 32bpp a 64-byte row is 16 pixels, so
this module enforces the conservative conjunction -- ``width % 16 == 0`` *and*
``height % 16 == 0`` -- and asserts it in the test suite, so an entry that
would be mis-rendered cannot be added quietly.

Windows/simulation only
-----------------------

Everything here is stdlib-only and runs on a bare Windows host.  Nothing in
this module starts a process, opens a socket or touches hardware; the argv it
produces is a *proposal* until :mod:`unipad.host.qemu` launches it.  No field
verification is claimed anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, TextIO, Union

from unipad.errors import ErrorCode, UniPadError

__all__ = [
    # constants
    "PINNED_DISPLAY",
    "DEFAULT_DISPLAY_MODE",
    "DISPLAY_FORMATS",
    "DISPLAY_MODES",
    "ROTATIONS",
    "ORIENTATIONS",
    "AUDIO_MODES",
    "ROUTE_LIMITATIONS",
    "ROUTE_LIMITATION_NOTES",
    "ROUTE_LIMITATIONS_BY_FORMAT",
    "DEFAULT_ROTATION",
    "DEFAULT_AUDIO",
    "AUDIO_DEVICE_ID",
    "AUDIO_UNVERIFIED",
    "GUEST_AUDIO_VERIFIED",
    "STAGE_ORDER",
    "TERMINAL_STAGES",
    # types
    "Rotation",
    "DisplayMode",
    "AudioDecision",
    "DisplayDecision",
    "InstallStageProgress",
    # functions
    "decide",
    "rotate_geometry",
    "rotate_point",
    "render_progress",
    "stage_vocabulary_drift",
]

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: The shipped display, mirroring ``config/machine.json`` ``display`` and
#: :attr:`unipad.models.MachineConfig.display`.  Three spellings of one number
#: is how a second, silently different machine gets shipped, so the test suite
#: asserts all three agree.  Read-only on purpose: a mutable module-level
#: default that decides the machine's geometry is shared state, and one
#: careless assignment would drift every later ``decide()`` call.
PINNED_DISPLAY: Mapping[str, Any] = MappingProxyType(
    {"width": 1376, "height": 1024, "format": "ramfb"}
)

#: Display backends the launcher can translate.  ``ramfb`` is the firmware
#: GOP path (ADR-0004 step 1, PRD 4.2); ``virtio-gpu`` is the probe route
#: (ADR-0004 step 3).  Anything else is a usage error, not a guess.
DISPLAY_FORMATS: tuple[str, ...] = ("ramfb", "virtio-gpu")

#: What a route **does not** configure, as a closed vocabulary of keys.  A
#: route that drops part of a request it already validated has to say so in a
#: form a machine can read: an operator who asks for 2400x1680 rotated 90 on
#: the VirtIO route, watches the run succeed, and is then handed PRD 9.2
#: evidence built from the resulting screenshot has no way to notice the drop
#: from the *absence* of a ``-fw_cfg`` item alone (reverse review M1).  The
#: keys are asserted to be exactly the ones :data:`ROUTE_LIMITATIONS_BY_FORMAT`
#: uses, so a new limitation cannot be added to one and not the other.
ROUTE_LIMITATIONS: tuple[str, ...] = (
    "resolution_not_configured",
    "rotation_not_configured",
)

#: One operator-facing sentence per limitation key.  Prose, because the
#: operator is the reader: the keys above are for a machine comparing two
#: evidence sets, and a key alone does not tell anyone what to do about it.
ROUTE_LIMITATION_NOTES: Mapping[str, str] = MappingProxyType(
    {
        "resolution_not_configured": (
            "This route negotiates its own framebuffer geometry with the guest "
            "driver (ADR-0004), so the requested resolution was validated and "
            "recorded but was NOT configured on the guest: no -fw_cfg geometry "
            "item is emitted and the guest runs whatever it negotiated."
        ),
        "rotation_not_configured": (
            "Rotation reaches the guest only through the -fw_cfg geometry item, "
            "which this route does not emit, so the requested turn was applied "
            "to the recorded framebuffer but was NOT configured on the guest."
        ),
    }
)

#: Which limitations each display route carries.  ``ramfb`` carries none: its
#: ``-fw_cfg`` item is exactly the geometry the operator asked for, so the
#: evidence and the argv agree by construction.  The mapping is keyed by
#: :data:`DISPLAY_FORMATS` and the test suite asserts the key sets are equal,
#: so adding a third route forces its disclosure to be written down.
ROUTE_LIMITATIONS_BY_FORMAT: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "ramfb": (),
        "virtio-gpu": ROUTE_LIMITATIONS,
    }
)

#: Quarter turns, clockwise.  The closed vocabulary of :class:`Rotation`.
ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)

#: Which way the framebuffer is wider than it is tall.  Derived from the
#: *resulting* geometry, never from the angle alone, so a portrait-native
#: catalogue entry cannot silently invert the meaning.
ORIENTATIONS: tuple[str, ...] = ("portrait", "landscape")

#: Audio backends.  ``none`` is a real choice -- it emits no audio device at
#: all -- and is the default.  ``hda`` emits an Intel HDA codec-less device for
#: a guest driver to bind to; see :class:`AudioDecision`.
AUDIO_MODES: tuple[str, ...] = ("none", "hda")

#: The ``id=`` of the ``-audiodev`` backend, referenced by the audio device.
AUDIO_DEVICE_ID = "unipad-audio"

#: What an audio device does **not** prove.  A property of the host, not of a
#: decision: nothing in this module can set it, so no caller can claim a guest
#: audio stack was heard by constructing an object.
GUEST_AUDIO_VERIFIED = False

#: The things an ``hda`` decision explicitly leaves unproved.  Reported with
#: the decision so an operator reading the evidence set sees the gap.
AUDIO_UNVERIFIED: tuple[str, ...] = (
    "whether the pinned board exposes an audio controller the guest can bind to",
    "whether an iPadOS guest audio stack exists and initialises at all",
    "whether any host sound device was opened (driver=none opens none)",
    "whether audio was ever heard on a real host: no field verification exists",
)

#: Forward install stages, in order, exactly as :mod:`unipad.ipa.faults`
#: defines them.  ``failed`` is not a progress stage and is handled separately.
#: :func:`stage_vocabulary_drift` compares this against the real modules so a
#: vocabulary change upstream fails the suite instead of rendering a stage the
#: installer can no longer produce.
STAGE_ORDER: tuple[str, ...] = ("received", "checked", "transferred", "installed", "registered")

#: Stages after which the install chain performs no further work (PRD 9.1).
TERMINAL_STAGES: tuple[str, ...] = ("installed", "registered", "failed")

#: Framebuffer constraint from ``config/machine.json``: width a multiple of 16,
#: rows a multiple of 64 bytes.  At 32bpp a 64-byte row is 16 pixels.
_STRIDE_PIXELS = 16
_BYTES_PER_PIXEL = 4


@dataclass(frozen=True)
class DisplayMode:
    """One selectable framebuffer geometry.

    ``provenance`` says where the numbers came from, because a resolution is
    only as trustworthy as the claim behind it: the pinned entry is a geometry
    this project boots, the rest are reference panel sizes.
    """

    name: str
    width: int
    height: int
    provenance: str = ""

    @property
    def size(self) -> tuple[int, int]:
        """The geometry as a ``(width, height)`` pair."""
        return (self.width, self.height)


#: The closed resolution catalogue.  Adding an entry requires it to satisfy the
#: stride constraint (asserted in ``tests/unit/test_host_display.py``) and to be
#: landscape-native, because rotation is what produces portrait.
DISPLAY_MODES: tuple[DisplayMode, ...] = (
    DisplayMode(
        name="ramfb-pinned",
        width=1376,
        height=1024,
        provenance=(
            "Pinned board geometry: config/machine.json display and "
            "run-qemu.py:39-43. The real 1366-wide panel rounded up to the "
            "16-pixel width multiple."
        ),
    ),
    DisplayMode(
        name="ipad-4-3-standard",
        width=2048,
        height=1536,
        provenance=(
            "4:3 iPad-class reference panel (2048x1536). Already a multiple of "
            "16 on both axes, so the framebuffer constraint needs no rounding."
        ),
    ),
    DisplayMode(
        name="ipad-pro-12-9",
        width=2736,
        height=2048,
        provenance=(
            "iPad Pro 12.9-inch reference panel (2732x2048) rounded up: 2732 "
            "is not a multiple of 16, the framebuffer width is 2736."
        ),
    ),
    DisplayMode(
        name="ipad-pro-11",
        width=2400,
        height=1680,
        provenance=(
            "iPad Pro 11-inch reference panel (2388x1668) rounded up on both "
            "axes to 2400x1680."
        ),
    ),
    DisplayMode(
        name="ipad-mini-8-3",
        width=2288,
        height=1488,
        provenance=(
            "iPad mini (8.3-inch) reference panel (2266x1488) rounded up: 2266 "
            "is not a multiple of 16, the framebuffer width is 2288."
        ),
    ),
)

#: The default selection: the geometry this project boots.
DEFAULT_DISPLAY_MODE = DISPLAY_MODES[0]


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class Rotation(int, Enum):
    """A quarter turn of the framebuffer, clockwise.

    An ``int`` enum so a caller may pass ``90`` or ``Rotation.DEG_90`` and get
    the same object, and so the value survives a JSON round trip as a number.
    ``__str__`` is pinned to the numeric spelling because ``str()`` of an
    ``IntEnum`` changed across Python versions and these values end up in logs.
    """

    DEG_0 = 0
    DEG_90 = 90
    DEG_180 = 180
    DEG_270 = 270

    def __str__(self) -> str:
        """Return the degree count so JSON and logs share one spelling."""
        return str(int(self))

    @property
    def swaps_axes(self) -> bool:
        """``True`` when this turn exchanges width and height."""
        return self in (Rotation.DEG_90, Rotation.DEG_270)


#: The unrotated board orientation: the turn a request without one gets.  It
#: is a *named* default rather than a literal at the call site because the
#: spelling used to exist in three places -- this module's parameter default,
#: the caller's ``.get`` fallback and ``config/machine.json`` -- and three
#: spellings of one default is how a configured turn gets dropped (standards
#: review N-3 / CG-6).  A caller reaches it from here or spells its own.
#: Defined below :class:`Rotation` because it is one of that class's members.
DEFAULT_ROTATION = Rotation.DEG_0

#: The audio backend a request without one gets.  ``none`` emits nothing, so
#: the default is the silent machine rather than a device nobody asked for.
DEFAULT_AUDIO = "none"


def _coerce_rotation(value: Any) -> Rotation:
    """Return ``value`` as a :class:`Rotation`, or refuse it.

    A turn QEMU cannot express is a usage error.  It is never rounded: a 45
    degree request that became 90 would rotate the framebuffer the caller did
    not ask for, and the screenshot would not describe what was configured.
    """
    if isinstance(value, Rotation):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "rotation must be one of 0, 90, 180 or 270 degrees",
            requested_rotation=value,
            supported_rotations=list(ROTATIONS),
            reason="rotation must be an integer number of degrees",
        )
    try:
        return Rotation(value)
    except ValueError:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "rotation must be a quarter turn (0, 90, 180 or 270 degrees)",
            requested_rotation=value,
            supported_rotations=list(ROTATIONS),
        ) from None


def rotate_geometry(
    size: tuple[int, int],
    rotation: Union[int, Rotation],
) -> tuple[int, int]:
    """Return the framebuffer geometry after rotating ``size``.

    A 90 or 270 degree turn swaps the axes; 0 and 180 keep them.  This is the
    number the guest driver reads out of ``fw_cfg
    opt/org.unixnu/ramfb-size``, so it has to be right: a wrong swap makes the
    guest mis-render rather than fail visibly.

    Args:
        size: ``(width, height)`` of the unrotated framebuffer.
        rotation: Quarter turn in degrees, or a :class:`Rotation`.

    Returns:
        The ``(width, height)`` the framebuffer occupies once rotated.

    Raises:
        UniPadError: ``config_invalid`` for a non-quarter turn or a
            non-positive size.
    """
    width, height = _as_size(size, "framebuffer size")
    turn = _coerce_rotation(rotation)
    return (height, width) if turn.swaps_axes else (width, height)


def rotate_point(
    point: tuple[int, int],
    base_size: tuple[int, int],
    rotation: Union[int, Rotation],
) -> tuple[int, int]:
    """Map a point from the *displayed* (rotated) window onto the framebuffer.

    This is the inverse of the rotation, which is what an input sink needs: a
    click arrives in the window the operator sees, and must be delivered to the
    framebuffer the guest driver actually owns.  It is the counterpart of
    :func:`unipad.host.input.scale_point` and stays pure for the same reason.

    Args:
        point: ``(x, y)`` in the displayed window, where the displayed size is
            :func:`rotate_geometry` of ``base_size``.
        base_size: ``(width, height)`` of the unrotated framebuffer.
        rotation: Quarter turn in degrees, or a :class:`Rotation`.

    Returns:
        ``(x, y)`` in the unrotated framebuffer, ``0 <= x < width`` and
        ``0 <= y < height``.

    Raises:
        ValueError: When ``point`` is not a pair of integers, when
            ``base_size`` is not positive, or when the point lies outside the
            displayed window (it cannot be mapped honestly).
        UniPadError: ``config_invalid`` for a non-quarter turn.
    """
    base_width, base_height = _as_size(base_size, "base framebuffer size")
    turn = _coerce_rotation(rotation)
    if len(point) != 2:
        raise ValueError(f"point must be an (x, y) pair, got {point!r}")
    px, py = point
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (px, py)):
        raise ValueError(f"point coordinates must be integers, got {point!r}")
    displayed_width, displayed_height = rotate_geometry((base_width, base_height), turn)
    if not (0 <= px < displayed_width and 0 <= py < displayed_height):
        raise ValueError(
            f"point {point!r} is outside the displayed {displayed_width}x{displayed_height} window"
        )
    # Rotating the framebuffer 90 degrees clockwise moves its bottom-left
    # corner to the top-left of the displayed window, so a click there maps to
    # (0, base_height - 1).  270 is the other way round and 180 flips both axes.
    if turn is Rotation.DEG_90:
        return (py, base_height - 1 - px)
    if turn is Rotation.DEG_270:
        return (base_width - 1 - py, px)
    if turn is Rotation.DEG_180:
        return (base_width - 1 - px, base_height - 1 - py)
    return (px, py)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioDecision:
    """The audio backend decision for one run.

    ``none`` emits no audio device at all.  ``hda`` emits an Intel HDA device
    with a **null** host driver, which gives a guest driver something to bind
    to while touching no host sound hardware -- so nothing about host audio can
    be claimed either way.  A real host driver (``dsound``/``wav``) is
    deliberately absent from the vocabulary: it would open a host device whose
    behaviour nobody has verified on this machine.

    Whether the *guest* audio stack works is not a field of this object.
    :attr:`guest_audio_verified` is a host fact (:data:`GUEST_AUDIO_VERIFIED`)
    that no construction of this class can set, so no caller can promote a
    configuration into a claim that audio was heard.
    """

    backend: str

    def __post_init__(self) -> None:
        """Refuse a backend outside the closed vocabulary."""
        if self.backend not in AUDIO_MODES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown audio backend",
                requested_audio=self.backend,
                supported_audio=list(AUDIO_MODES),
                reason="an unrecognised audio backend must not silently become 'none'",
            )

    @property
    def guest_audio_verified(self) -> bool:
        """Always ``False``: no guest audio stack has been heard on this host."""
        return GUEST_AUDIO_VERIFIED

    @property
    def unverified(self) -> tuple[str, ...]:
        """What an audio device does not prove."""
        return AUDIO_UNVERIFIED

    def emits_device(self) -> bool:
        """``True`` when this decision puts an audio device on the command line."""
        return self.backend != "none"

    def to_qemu_fragments(self) -> list[str]:
        """Return the argv pieces for this decision (empty for ``none``).

        Order matters: ``-audiodev`` precedes the devices that reference it, or
        QEMU rejects the whole argv.
        """
        if self.backend == "none":
            return []
        return [
            "-audiodev",
            f"driver=none,id={AUDIO_DEVICE_ID}",
            "-device",
            "intel-hda",
            "-device",
            f"hda-duplex,audiodev={AUDIO_DEVICE_ID}",
        ]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view (PRD 9.2: no secrets)."""
        return {
            "backend": self.backend,
            "emits_device": self.emits_device(),
            "guest_audio_verified": self.guest_audio_verified,
            "unverified": list(self.unverified),
        }


# ---------------------------------------------------------------------------
# The display decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DisplayDecision:
    """One validated display/audio decision, ready for the launcher.

    ``base`` is the geometry that was requested; ``framebuffer`` is the geometry
    the guest will see once ``rotation`` is applied.  Keeping both means the
    evidence set records what was asked for as well as what was configured, and
    a later reader can tell a rotated run from an unrotated one.

    ``framebuffer`` is what was **asked for**; :attr:`framebuffer_in_argv` says
    whether the argv actually carried it.  On a route that negotiates its own
    geometry the two differ, and a reader who is told only ``framebuffer``
    concludes the guest ran a geometry QEMU never received (callchain review
    2.4).  :attr:`route_limitations` names what the route does not configure so
    the drop is traced rather than inferred from an absent argv item.
    """

    format: str
    base: tuple[int, int]
    rotation: Rotation
    framebuffer: tuple[int, int]
    orientation: str
    mode_name: str
    audio: AudioDecision = field(default_factory=lambda: AudioDecision("none"))

    @property
    def rotation_degrees(self) -> int:
        """The turn in degrees (the enum's own value)."""
        return int(self.rotation)

    @property
    def framebuffer_in_argv(self) -> bool:
        """``True`` when :meth:`to_qemu_fragments` carries this geometry.

        The one place that knows the answer is the fragment builder, so the
        rule is stated once and derived from ``format`` -- which is also what
        the fragment builder branches on -- rather than restated here where the
        two could drift apart.
        """
        return self.format == "ramfb"

    @property
    def route_limitations(self) -> tuple[str, ...]:
        """What this route does not configure, as closed-vocabulary keys.

        Empty on a complete route.  A key here is a *disclosure*, not an error:
        the request was valid and the run is allowed to proceed, but the
        evidence must not read as though the guest received the geometry.
        """
        return ROUTE_LIMITATIONS_BY_FORMAT.get(self.format, ())

    @property
    def route_notes(self) -> tuple[str, ...]:
        """One operator-facing sentence per :attr:`route_limitations` key."""
        return tuple(ROUTE_LIMITATION_NOTES[key] for key in self.route_limitations)

    def to_qemu_fragments(self) -> list[str]:
        """Return the exact argv pieces for this decision.

        The list is **display first, audio second**, and it *replaces* the
        display block :meth:`unipad.host.qemu.QemuLauncher.build_args` emits
        today.  Appending to that block instead would put ``-device ramfb`` on
        the command line twice, which QEMU rejects.

        ``-display none`` for a headless run is deliberately **not** here: it is
        a window decision the launcher already owns, and folding it in would
        give two modules the power to hide the guest display.

        Returns:
            ``["-device", "ramfb", "-fw_cfg", "name=opt/org.unixnu/ramfb-size,string=WxH"]``
            on the ramfb path, ``["-device", "virtio-gpu-pci"]`` on the VirtIO
            path, followed by :meth:`AudioDecision.to_qemu_fragments`.

        The geometry is emitted **only** on the ramfb path, which is why
        :attr:`framebuffer_in_argv` and :attr:`route_limitations` exist: a
        route that emits no geometry must still be able to say so.
        """
        width, height = self.framebuffer
        fragments: list[str] = []
        if self.format == "ramfb":
            # The guest driver reads the geometry out of fw_cfg
            # (QemuRamfbPkg), so a size that only exists in our config never
            # reaches it: without this item the framebuffer keeps the EDK2
            # default and every row is misaligned.
            fragments.extend(["-device", "ramfb"])
            fragments.extend(
                ["-fw_cfg", f"name=opt/org.unixnu/ramfb-size,string={width}x{height}"]
            )
        else:
            # ADR-0004: the VirtIO route negotiates its own geometry with the
            # guest driver, so no fw_cfg item is emitted for it.  Rotation and
            # resolution therefore reach the guest only on the ramfb path --
            # a limitation of the route, not of this module.  It is a
            # *disclosed* limitation: to_dict reports it through
            # route_limitations/route_notes rather than leaving the reader to
            # infer it from an argv item that is not there.
            fragments.extend(["-device", "virtio-gpu-pci"])
        fragments.extend(self.audio.to_qemu_fragments())
        return fragments

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view (PRD 9.2: no secrets, paths only).

        ``framebuffer`` is the geometry that was requested, and
        ``framebuffer_in_argv`` says whether the argv carried it.  Both are
        reported because a future reproduction is built from this dict: on a
        route that negotiates its own geometry, ``framebuffer`` alone would
        describe a guest state the command line never produced.
        ``route_limitations`` is the closed-vocabulary form of the same fact
        and ``route_notes`` is its operator-facing prose.
        """
        return {
            "format": self.format,
            "base": [self.base[0], self.base[1]],
            "rotation_degrees": self.rotation_degrees,
            "framebuffer": [self.framebuffer[0], self.framebuffer[1]],
            "framebuffer_in_argv": self.framebuffer_in_argv,
            "orientation": self.orientation,
            "mode_name": self.mode_name,
            "route_limitations": list(self.route_limitations),
            "route_notes": list(self.route_notes),
            "audio": self.audio.to_dict(),
        }


def decide(
    display: Union[Mapping[str, Any], Any, None] = None,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    rotation: Optional[Union[int, Rotation]] = None,
    audio: Optional[str] = None,
) -> DisplayDecision:
    """Turn a display/audio request into one validated decision.

    Args:
        display: The configured display: a mapping such as
            ``machine.display``, an object exposing a ``display`` mapping (a
            :class:`unipad.models.MachineConfig`), or ``None`` for the pinned
            geometry in :data:`PINNED_DISPLAY`.  ``rotation`` and ``audio``
            nested in this mapping are honoured, so ``decide(machine)`` alone
            reproduces what the configuration asks for.
        width: Requested framebuffer width; overrides ``display``.
        height: Requested framebuffer height; overrides ``display``.  Both must
            be given together -- a half-specified resolution is a usage error.
        rotation: Quarter turn in degrees, or a :class:`Rotation`.  ``None``
            (the default) takes the value from ``display``, falling back to
            :data:`DEFAULT_ROTATION`: rotation is never inferred, and a
            rotation that is only in the configuration must not be ignored.
        audio: One of :data:`AUDIO_MODES`.  ``None`` (the default) takes the
            value from ``display``, falling back to :data:`DEFAULT_AUDIO`.

    Returns:
        A :class:`DisplayDecision`.

    Raises:
        UniPadError: ``config_invalid`` for an unsupported resolution (never
            snapped), a non-quarter turn, an unknown display format or an
            unknown audio backend.

    Example:
        >>> decide(rotation=90).framebuffer
        (1024, 1376)
        >>> decide({"width": 1376, "height": 1024, "rotation": 90}).framebuffer
        (1024, 1376)
    """
    requested = _display_mapping(display)
    display_format = _display_format(requested.get("format", PINNED_DISPLAY["format"]))
    # A rotation or an audio backend that exists only in the configuration is
    # still a request.  Defaulting the *parameter* to the board orientation and
    # reading nothing from the mapping would make ``decide(machine)`` silently
    # drop a configured rotation -- the feature would look wired and do nothing.
    #
    # This is the SINGLE reader of the display mapping (standards review CG-6):
    # a caller hands over ``machine`` or ``machine.display`` and gets the whole
    # decision, including the two keys a partial reader used to leave behind.
    # A caller that re-reads the mapping itself to pass ``rotation=`` /
    # ``audio=`` back in is re-spelling this module's defaults and is what
    # produced the bug.
    if rotation is None:
        rotation = requested.get("rotation", DEFAULT_ROTATION)
    if audio is None:
        audio = requested.get("audio", DEFAULT_AUDIO)

    if width is None and height is None:
        raw_width = requested.get("width")
        raw_height = requested.get("height")
        if raw_width is None or raw_height is None:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "display geometry is incomplete: width and height are both required",
                display=dict(requested),
                supported=[list(mode.size) for mode in DISPLAY_MODES],
            )
    elif width is None or height is None:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a resolution override needs both width and height",
            requested=[width, height],
            supported=[list(mode.size) for mode in DISPLAY_MODES],
        )
    else:
        raw_width, raw_height = width, height

    pair = (
        _strict_int(raw_width, "display width", "width"),
        _strict_int(raw_height, "display height", "height"),
    )
    mode = _select_mode(pair)
    turn = _coerce_rotation(rotation)
    framebuffer = rotate_geometry(pair, turn)
    orientation = "portrait" if framebuffer[0] < framebuffer[1] else "landscape"

    return DisplayDecision(
        format=display_format,
        base=pair,
        rotation=turn,
        framebuffer=framebuffer,
        orientation=orientation,
        mode_name=mode.name,
        audio=AudioDecision(_audio_backend(audio)),
    )


# ---------------------------------------------------------------------------
# Install progress (安装包体验)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallStageProgress:
    """One point in a package import, in the installer's own vocabulary.

    ``stage`` is the furthest stage *actually completed* -- the same rule
    :class:`unipad.ipa.installer.IpaInstaller` follows, and the same reason a
    verified file transfer alone never reports ``installed`` (PRD 9.1).

    ``percent`` is the stage's ordinal position in :data:`STAGE_ORDER`, or
    ``None`` when no progress claim can be made (``failed``).  It is a
    milestone count, not a byte counter: the transfer's real progress is the
    transport's business, and inventing a smooth percentage here would be a
    number nobody measured.
    """

    stage: str
    percent: Optional[int] = None
    #: ``None`` when the chain never learned the value, which is what
    #: :class:`unipad.models.InstallResult` declares for ``bundle_id``.  An
    #: empty string would claim the id was observed and found to be empty; the
    #: install contract records a fact, not a guess, so absence stays absent.
    bundle_id: Optional[str] = None
    package_sha256: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        """Refuse a stage outside the installer's vocabulary."""
        if self.stage not in STAGE_ORDER and self.stage != "failed":
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown install stage",
                requested_stage=self.stage,
                supported_stages=list(STAGE_ORDER) + ["failed"],
                reason=(
                    "a progress renderer that silently mapped an unknown stage "
                    "onto 'failed' would misreport the furthest stage reached"
                ),
            )
        if self.percent is not None:
            if isinstance(self.percent, bool) or not isinstance(self.percent, int):
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "install progress percent must be an integer or None",
                    percent=self.percent,
                )
            if not 0 <= self.percent <= 100:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "install progress percent must be within 0..100",
                    percent=self.percent,
                )

    @property
    def failed(self) -> bool:
        """``True`` when the chain stopped without completing."""
        return self.stage == "failed"

    @property
    def complete(self) -> bool:
        """``True`` once the guest confirmed an install or a registration."""
        return self.stage in TERMINAL_STAGES and not self.failed

    @property
    def succeeded(self) -> bool:
        """``True`` only for a completed chain with no error code."""
        return self.complete and self.error_code is None

    @classmethod
    def for_stage(
        cls,
        stage: str,
        *,
        bundle_id: Optional[str] = None,
        package_sha256: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> "InstallStageProgress":
        """Build progress for one stage, filling in its ordinal percentage.

        ``failed`` carries no percentage: the stage does not say how far the
        chain got, so rendering a number would be a guess.
        """
        if stage == "failed":
            percent: Optional[int] = None
        elif stage in STAGE_ORDER:
            ordinal = STAGE_ORDER.index(stage) + 1
            percent = round(100 * ordinal / len(STAGE_ORDER))
        else:
            # Delegate the vocabulary check to the constructor so there is one
            # place that decides what a stage is.
            return cls(
                stage,
                None,
                bundle_id,
                package_sha256,
                error_code,
                error_message,
            )
        return cls(
            stage,
            percent,
            bundle_id,
            package_sha256,
            error_code,
            error_message,
        )

    @classmethod
    def from_install_result(cls, result: Any) -> "InstallStageProgress":
        """Map a :class:`unipad.models.InstallResult` onto the progress model.

        Duck-typed on purpose: the CLI hands over whatever the installer
        returned, and this module must not need to import
        :mod:`unipad.models` to read five attributes.
        """
        stage = getattr(result, "stage", None)
        if not isinstance(stage, str) or not stage:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "install result carries no stage",
                result_type=type(result).__name__,
                supported_stages=list(STAGE_ORDER) + ["failed"],
            )
        return cls.for_stage(
            stage,
            # One rule for all four optional attributes.  Coercing with
            # ``str(x or "")`` would turn an absent id into an empty string,
            # which claims the value was observed and found to be empty; the
            # source declares these Optional, so absence has to survive the
            # mapping -- and a reader must not have to know which of the four
            # lines a field landed on to know what an absent one becomes.
            bundle_id=_optional_str(getattr(result, "bundle_id", None)),
            package_sha256=_optional_str(getattr(result, "package_sha256", None)),
            error_code=_optional_str(getattr(result, "error_code", None)),
            error_message=_optional_str(getattr(result, "error_message", None)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view (PRD 9.2: no secrets)."""
        return {
            "stage": self.stage,
            "percent": self.percent,
            "bundle_id": self.bundle_id,
            "package_sha256": self.package_sha256,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def render_progress(
    progress: InstallStageProgress,
    *,
    width: int = 24,
    stream: Optional[TextIO] = None,
) -> str:
    """Render one :class:`InstallStageProgress` as a single plain-text line.

    ASCII only, no ANSI, no external dependencies: the Windows console code
    page must not be able to mangle a progress line, and a launcher that emits
    escape sequences it does not own is worse than one that emits none.

    This is the *host-side render* model.  It is deliberately not the
    guest's ``InstallProgress`` wire message
    (:class:`unipad.guest.protocol.InstallProgress`), which is an informational
    ``stage``/``detail`` notification that never carries a percentage; the two
    share a vocabulary, not a type.

    Args:
        progress: The progress point to render.
        width: Bar width in characters.
        stream: When given, the line is written to it followed by a newline
            (the CLI renders into a live terminal).

    Returns:
        The rendered line, without a trailing newline.

    Raises:
        UniPadError: ``config_invalid`` when ``progress`` is not an
            :class:`InstallStageProgress`.
    """
    if not isinstance(progress, InstallStageProgress):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "render_progress expects an InstallStageProgress",
            received_type=type(progress).__name__,
        )
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "progress bar width must be a positive integer",
            width=width,
        )

    if progress.percent is None:
        filled = 0
        label = "  ?%"
    else:
        filled = min(max(round(width * progress.percent / 100), 0), width)
        label = f"{progress.percent:>3}%"
    bar = "#" * filled + "-" * (width - filled)

    if progress.failed:
        marker = "[!!]"
    elif progress.complete:
        marker = "[ok]"
    else:
        marker = "[..]"

    parts = [marker, f"[{bar}]", label, progress.stage]
    if progress.bundle_id:
        parts.append(f"bundle_id={progress.bundle_id}")
    if progress.package_sha256:
        # A full digest is evidence for the bundle, not for a progress bar.
        parts.append(f"sha256={progress.package_sha256[:12]}")
    if progress.error_code:
        parts.append(f"error={progress.error_code}")
    if progress.error_message:
        parts.append(f"reason={_one_line(progress.error_message)}")

    line = " ".join(parts)
    if stream is not None:
        stream.write(line + "\n")
    return line


def stage_vocabulary_drift() -> tuple[str, ...]:
    """Report every way :data:`STAGE_ORDER` disagrees with the real modules.

    The install vocabulary belongs to :mod:`unipad.ipa`, not to this package, so
    it can change without this module noticing.  Comparing lazily (rather than
    importing at module load) keeps :mod:`unipad.host` free of a dependency on
    :mod:`unipad.ipa` while still failing the suite the moment the two drift.

    Returns:
        A tuple of human-readable mismatches; empty means the vocabularies
        agree.  ``("unipad.ipa is unavailable",)`` when the package cannot be
        imported at all -- that is drift too, just of a different kind.
    """
    try:
        from ..ipa.faults import InstallStage as RealInstallStage
        from ..models import InstallResult as RealInstallResult
    except ImportError as exc:  # pragma: no cover - only on a broken checkout
        return (f"unipad.ipa is unavailable: {type(exc).__name__}",)

    drift: list[str] = []
    real_forward = tuple(
        stage.value for stage in RealInstallStage if stage.value != "failed"
    )
    if real_forward != STAGE_ORDER:
        drift.append(
            f"forward stages: display.py has {list(STAGE_ORDER)}, "
            f"unipad.ipa.faults.InstallStage has {list(real_forward)}"
        )
    real_all = tuple(stage.value for stage in RealInstallStage)
    if set(real_all) != set(STAGE_ORDER) | {"failed"}:
        drift.append(
            f"stage set: display.py has {sorted(set(STAGE_ORDER) | {'failed'})}, "
            f"InstallStage has {sorted(set(real_all))}"
        )
    if tuple(RealInstallResult.TERMINAL_STAGES) != TERMINAL_STAGES:
        drift.append(
            f"terminal stages: display.py has {list(TERMINAL_STAGES)}, "
            f"InstallResult.TERMINAL_STAGES has {list(RealInstallResult.TERMINAL_STAGES)}"
        )
    return tuple(drift)


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------


def _display_mapping(display: Any) -> dict[str, Any]:
    """Normalise the ``display`` argument into a plain mapping.

    Accepts ``None`` (the pinned geometry), a mapping (``machine.display``), or
    an object carrying a ``display`` mapping (``MachineConfig``).  Anything else
    is a usage error rather than an empty display: an unrecognised shape is a
    configuration bug, and defaulting from it would hide it.
    """
    if display is None:
        return dict(PINNED_DISPLAY)
    if isinstance(display, Mapping):
        return {str(key): value for key, value in display.items()}
    inner = getattr(display, "display", None)
    if isinstance(inner, Mapping):
        return {str(key): value for key, value in inner.items()}
    raise UniPadError(
        ErrorCode.CONFIG_INVALID,
        "display must be a mapping, a MachineConfig, or None",
        received_type=type(display).__name__,
        expected="mapping with width/height/format, or an object exposing one",
    )


def _display_format(value: Any) -> str:
    """Return the validated display format spelling.

    ``ramfb`` and ``virtio-gpu`` are the two routes (ADR-0004).  A value
    starting with ``virtio`` is normalised to ``virtio-gpu`` because that is
    the reading :meth:`unipad.host.qemu.QemuLauncher.build_args` already
    performs, so an existing config does not become invalid.  Everything else
    is refused: an unknown format is a route nobody has designed.
    """
    if not isinstance(value, str) or not value.strip():
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "display format must be a non-empty string",
            format=value,
            supported_formats=list(DISPLAY_FORMATS),
        )
    normalised = value.strip().lower()
    if normalised in DISPLAY_FORMATS:
        return normalised
    if normalised.startswith("virtio"):
        return "virtio-gpu"
    raise UniPadError(
        ErrorCode.CONFIG_INVALID,
        "unknown display format",
        format=value,
        supported_formats=list(DISPLAY_FORMATS),
    )


def _select_mode(pair: tuple[int, int]) -> DisplayMode:
    """Return the catalogue entry for ``pair``, or refuse it.

    Refusing is the whole point: a snapped resolution makes the recorded
    geometry disagree with the framebuffer, and PRD 9.2 evidence built on that
    disagreement is worthless.
    """
    for mode in DISPLAY_MODES:
        if mode.size == pair:
            return mode
    raise UniPadError(
        ErrorCode.CONFIG_INVALID,
        "unsupported display resolution",
        requested=[pair[0], pair[1]],
        supported=[list(mode.size) for mode in DISPLAY_MODES],
        reason=(
            "an unsupported resolution is refused rather than snapped to a "
            "neighbour: the framebuffer must be the one that was asked for, or "
            "a screenshot no longer describes the configured geometry"
        ),
    )


def _audio_backend(value: Any) -> str:
    """Return the validated audio backend spelling."""
    if value not in AUDIO_MODES:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown audio backend",
            requested_audio=value,
            supported_audio=list(AUDIO_MODES),
        )
    return str(value)


def _strict_int(value: Any, label: str, detail_key: str) -> int:
    """Return ``value`` as an ``int``, refusing floats, strings and booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"{label} must be an integer",
            **{detail_key: value},
        )
    return value


def _as_size(size: Any, label: str) -> tuple[int, int]:
    """Return ``size`` as a validated positive ``(width, height)`` pair."""
    if not isinstance(size, Iterable) or isinstance(size, (str, bytes)):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"{label} must be a (width, height) pair",
            size=size,
        )
    pair = tuple(size)
    if len(pair) != 2 or any(
        isinstance(v, bool) or not isinstance(v, int) for v in pair
    ):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"{label} must be a (width, height) pair of integers",
            size=size,
        )
    if pair[0] <= 0 or pair[1] <= 0:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"{label} must be positive",
            size=size,
        )
    return (pair[0], pair[1])


def _optional_str(value: Any) -> Optional[str]:
    """Return ``value`` as a non-empty string, or ``None``.

    An absent value stays ``None`` rather than becoming an empty string: the
    install contract records "信任方式" as a fact, not a guess, and the same
    rule applies to every other optional field.
    """
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _one_line(text: str) -> str:
    """Collapse a message onto one line so a progress bar stays one line."""
    return " ".join(str(text).split())
