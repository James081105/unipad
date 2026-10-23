"""Unit tests for :mod:`unipad.host.display` (PRD 3.2 P1 bullet 4).

PRD 3.2 P1 reads ``旋转、可选分辨率、音频和安装包体验`` -- rotation, selectable
resolution, audio and the install-package experience.  ``unipad.host.display``
is the *decision* layer for all four: it turns a request into a validated,
closed-vocabulary answer plus the exact argv pieces the launcher needs.

What these tests pin, and why each one matters:

- **Resolution is a closed set.**  A width/height pair outside the catalogue is
  REFUSED with ``config_invalid``.  It is never snapped to a neighbour: a
  snapped framebuffer makes a screenshot useless as evidence (PRD 9.2), because
  the recorded geometry would no longer describe the pixels in the image.
- **Rotation swaps the geometry at 90/270 and not at 0/180.**  The guest driver
  reads the framebuffer geometry out of ``fw_cfg opt/org.unixnu/ramfb-size``
  (``config/machine.json`` display note), so a wrong swap produces a guest that
  mis-renders rather than an obvious failure.
- **Audio ``none`` is explicit and emits nothing.**  No iPadOS guest audio stack
  has been heard on this host, so the module must be able to say "no audio
  device emitted" as a first-class answer instead of pretending.
- **The installer stage vocabulary matches the real one.**  The progress model
  is asserted against :class:`unipad.ipa.faults.InstallStage`, so if the
  installer's vocabulary changes this test fails instead of the CLI rendering a
  stage that no longer exists.

Every claim here is a Windows/simulation claim.  Nothing in this file runs a
real QEMU process or a real iPadOS guest, and no resolution other than the
pinned one has been shown to render on real hardware.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.display import (
    AUDIO_MODES,
    DISPLAY_FORMATS,
    DISPLAY_MODES,
    ORIENTATIONS,
    PINNED_DISPLAY,
    ROTATIONS,
    STAGE_ORDER,
    AudioDecision,
    DisplayDecision,
    DisplayMode,
    InstallStageProgress,
    Rotation,
    decide,
    render_progress,
    rotate_geometry,
    rotate_point,
    stage_vocabulary_drift,
)
from unipad.ipa.faults import InstallStage
from unipad.models import InstallResult, MachineConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The catalogue itself
# ---------------------------------------------------------------------------


def test_the_catalogue_is_closed_and_non_empty():
    """PRD 3.2 P1: "可选分辨率" is a choice from a fixed set, not a free-form size."""
    assert DISPLAY_MODES, "an empty catalogue would make every resolution unsupported"
    names = [mode.name for mode in DISPLAY_MODES]
    assert len(names) == len(set(names)), "a duplicate catalogue entry is ambiguous"
    sizes = [(mode.width, mode.height) for mode in DISPLAY_MODES]
    assert len(sizes) == len(set(sizes)), "two entries with the same geometry are ambiguous"


def test_every_mode_satisfies_the_framebuffer_stride_constraint():
    """``config/machine.json``: width a multiple of 16, rows a multiple of 64 bytes.

    At 32bpp a 64-byte row is 16 pixels, so this module enforces the
    *conjunction* of both readings (``width % 16 == 0`` and ``height % 16 == 0``).
    A catalogue entry that violates it would be mis-rendered by the guest
    driver, so the invariant is asserted rather than assumed.
    """
    for mode in DISPLAY_MODES:
        assert mode.width % 16 == 0, f"{mode.name}: width {mode.width} is not a multiple of 16"
        assert mode.height % 16 == 0, f"{mode.name}: height {mode.height} is not a multiple of 16"
        assert (mode.width * 4) % 64 == 0, f"{mode.name}: row stride is not a multiple of 64 bytes"
        assert (mode.height * 4) % 64 == 0, f"{mode.name}: column pitch is not a multiple of 64 bytes"


def test_every_mode_is_landscape_native():
    """Rotation is what produces portrait; the catalogue is the unrotated board.

    If an entry were portrait-native, ``orientation`` would flip its meaning and
    a 90-degree rotation would silently *remove* the portrait the caller asked
    for.
    """
    for mode in DISPLAY_MODES:
        assert mode.width > mode.height, f"{mode.name} is not landscape-native"


def test_the_pinned_geometry_is_in_the_catalogue_and_is_the_default():
    """``config/machine.json`` display is the shipped geometry and the default."""
    pinned = (PINNED_DISPLAY["width"], PINNED_DISPLAY["height"])
    assert pinned == (1376, 1024)
    assert pinned in [(mode.width, mode.height) for mode in DISPLAY_MODES]
    assert decide().base == pinned


def test_the_pinned_geometry_agrees_with_config_and_the_model_default():
    """Drift guard: machine.json, MachineConfig and this module must agree.

    Three spellings of one number is exactly how a second, silently different
    machine gets shipped, so the test reads the real file and the real model.
    """
    config = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))
    display = config["display"]
    assert (display["width"], display["height"]) == (PINNED_DISPLAY["width"], PINNED_DISPLAY["height"])
    assert display["format"] == PINNED_DISPLAY["format"]

    model_default = MachineConfig().display
    assert (model_default["width"], model_default["height"]) == (
        PINNED_DISPLAY["width"],
        PINNED_DISPLAY["height"],
    )
    assert model_default["format"] == PINNED_DISPLAY["format"]


# ---------------------------------------------------------------------------
# decide(): resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", DISPLAY_MODES, ids=lambda mode: mode.name)
def test_every_supported_resolution_round_trips(mode: DisplayMode):
    """Selecting a supported pair is honoured exactly -- no snapping, no scaling."""
    decision = decide(width=mode.width, height=mode.height)
    assert decision.base == (mode.width, mode.height)
    assert decision.framebuffer == (mode.width, mode.height)
    assert decision.mode_name == mode.name
    assert decision.orientation == "landscape"
    assert decision.to_dict()["framebuffer"] == [mode.width, mode.height]


def test_decide_accepts_the_display_mapping_from_machine_config():
    """``machine.display`` is the natural input; a MachineConfig works too."""
    from_mapping = decide({"width": 2048, "height": 1536, "format": "ramfb"})
    assert from_mapping.base == (2048, 1536)

    from_model = decide(MachineConfig())
    assert from_model.base == (PINNED_DISPLAY["width"], PINNED_DISPLAY["height"])
    assert from_model.format == "ramfb"


def test_explicit_width_and_height_override_the_mapping():
    """A request wins over the configured default, so the CLI can override."""
    decision = decide({"width": 2048, "height": 1536}, width=2288, height=1488)
    assert decision.base == (2288, 1488)


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (1366, 1024),    # the real panel width: a multiple of 2, not of 16
        (1377, 1024),    # one pixel off the pinned width
        (1376, 1025),    # one pixel off the pinned height
        (2732, 2048),    # the un-rounded Pro 12.9" panel
        (2388, 1668),    # the un-rounded Pro 11" panel
        (2266, 1488),    # the un-rounded mini panel
        (0, 1024),
        (1376, 0),
        (-1376, -1024),
        (800, 600),      # a classic VESA mode that is not in the catalogue
    ],
)
def test_an_unsupported_resolution_is_refused_not_snapped(width, height):
    """PRD 3.2 P1: an unsupported pair is a usage error, never a neighbour.

    ``config_invalid`` is the taxonomy's usage-error code and maps to
    ``ExitCode.USAGE``, so a script branches on it without parsing prose.
    """
    with pytest.raises(UniPadError) as excinfo:
        decide(width=width, height=height)
    error = excinfo.value
    assert error.code == ErrorCode.CONFIG_INVALID
    assert error.exit_code == 2
    # The refusal names what was asked for and what is available, so the
    # operator can pick from the list rather than guess.
    assert error.details["requested"] == [width, height]
    assert error.details["supported"] == [
        [mode.width, mode.height] for mode in DISPLAY_MODES
    ]
    assert "1366" not in str(error.details["supported"])


def test_a_non_integer_resolution_is_refused():
    """A float or a string is not a framebuffer geometry."""
    for bad in (1376.0, "1376", None, True, (1376,), [1376]):
        with pytest.raises(UniPadError) as excinfo:
            decide(width=bad, height=1024)  # type: ignore[arg-type]
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_the_rotation_vocabulary_is_the_quarter_turns():
    """Only 0/90/180/270 are expressible; the enum is the closed vocabulary."""
    assert ROTATIONS == (0, 90, 180, 270)
    assert tuple(int(r) for r in Rotation) == ROTATIONS
    assert str(Rotation.DEG_90) == "90"
    assert int(Rotation.DEG_270) == 270


@pytest.mark.parametrize("mode", DISPLAY_MODES, ids=lambda mode: mode.name)
@pytest.mark.parametrize("degrees", [0, 90, 180, 270])
def test_rotation_geometry(mode: DisplayMode, degrees: int):
    """90 and 270 swap the axes; 0 and 180 do not.

    The swap is the whole point of rotation: a 90-degree turn of a 1376x1024
    framebuffer is a 1024x1376 one, and the fw_cfg item must say so.
    """
    rotated = rotate_geometry((mode.width, mode.height), degrees)
    if degrees in (90, 270):
        assert rotated == (mode.height, mode.width)
    else:
        assert rotated == (mode.width, mode.height)


def test_rotation_swaps_the_framebuffer_in_the_decision():
    """``decide(rotation=90)`` reports the swapped geometry and portrait."""
    landscape = decide(rotation=0)
    assert landscape.framebuffer == (1376, 1024)
    assert landscape.orientation == "landscape"

    portrait = decide(rotation=90)
    assert portrait.framebuffer == (1024, 1376)
    assert portrait.orientation == "portrait"
    assert portrait.base == (1376, 1024), "rotation must not rewrite the requested base"


def test_180_degrees_keeps_the_geometry_and_the_orientation():
    """An upside-down panel is still the same shape."""
    flipped = decide(rotation=180)
    assert flipped.framebuffer == (1376, 1024)
    assert flipped.orientation == "landscape"


def test_the_default_is_the_unrotated_board_orientation():
    """Rotation must be an explicit request (PRD 3.2 P1: no silent rotation)."""
    assert decide().rotation is Rotation.DEG_0
    assert decide().orientation == "landscape"


def test_rotation_is_accepted_as_an_int_or_the_enum():
    """Both spellings must land on the same decision object."""
    assert decide(rotation=90).rotation is Rotation.DEG_90
    assert decide(rotation=Rotation.DEG_90) == decide(rotation=90)


@pytest.mark.parametrize("degrees", [45, -90, 360, 91, 900, "90", 1.5])
def test_a_non_quarter_turn_is_refused(degrees):
    """A rotation QEMU cannot express is a usage error, not a rounded value."""
    with pytest.raises(UniPadError) as excinfo:
        decide(rotation=degrees)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert excinfo.value.details["requested_rotation"] == degrees
    assert excinfo.value.details["supported_rotations"] == [0, 90, 180, 270]


def test_a_null_rotation_in_the_config_is_refused():
    """``rotation: null`` is a half-finished decision, not the board orientation."""
    with pytest.raises(UniPadError) as excinfo:
        decide({"width": 1376, "height": 1024, "rotation": None})
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_rotation_none_means_take_it_from_the_config():
    """The default is "unspecified", so a configured rotation is honoured."""
    assert decide(rotation=None).rotation is Rotation.DEG_0
    configured = {"width": 1376, "height": 1024, "rotation": 90}
    assert decide(configured, rotation=None).rotation is Rotation.DEG_90
    # An explicit request still wins over the configuration.
    assert decide(configured, rotation=180).rotation is Rotation.DEG_180
    # ... and so does a configured audio backend.
    assert decide({"width": 1376, "height": 1024, "audio": "hda"}).audio.backend == "hda"


def test_orientation_is_derived_from_the_geometry_not_the_angle():
    """A portrait-native catalogue entry would flip the meaning; assert the rule."""
    # 1376x1024 is landscape, so 0/180 are landscape and 90/270 are portrait.
    assert decide(rotation=0).orientation == "landscape"
    assert decide(rotation=180).orientation == "landscape"
    assert decide(rotation=90).orientation == "portrait"
    assert decide(rotation=270).orientation == "portrait"
    assert ORIENTATIONS == ("portrait", "landscape")


# ---------------------------------------------------------------------------
# rotate_point: displayed window -> framebuffer the guest driver owns
# ---------------------------------------------------------------------------


def test_rotate_point_identity_and_180():
    """0 and 180 keep the point in the same framebuffer, mirrored for 180."""
    base = (1376, 1024)
    assert rotate_point((100, 200), base, 0) == (100, 200)
    # 180 flips both axes: (x, y) -> (width - 1 - x, height - 1 - y).
    assert rotate_point((100, 200), base, 180) == (1275, 823)
    assert rotate_point((0, 0), base, 180) == (1375, 1023)


def test_rotate_point_90_and_270():
    """A click in the rotated window lands on the pixel the user aimed at.

    Rotating a 1376x1024 framebuffer 90 degrees clockwise puts its bottom-left
    corner at the top-left of the displayed 1024x1376 window, so a click at
    (0, 0) must map to framebuffer (0, 1023).
    """
    base = (1376, 1024)
    assert rotate_point((0, 0), base, 90) == (0, 1023)
    assert rotate_point((0, 1375), base, 90) == (1375, 1023)
    # 270 is the other way: the top-right corner arrives at the top-left.
    assert rotate_point((0, 0), base, 270) == (1375, 0)
    assert rotate_point((1023, 0), base, 270) == (1375, 1023)


def test_rotate_point_round_trips_every_corner():
    """Each corner of the displayed window maps onto a distinct framebuffer corner."""
    base = (1376, 1024)
    for degrees in (0, 90, 180, 270):
        displayed = rotate_geometry(base, degrees)
        corners = {
            rotate_point((0, 0), base, degrees),
            rotate_point((displayed[0] - 1, 0), base, degrees),
            rotate_point((0, displayed[1] - 1), base, degrees),
            rotate_point((displayed[0] - 1, displayed[1] - 1), base, degrees),
        }
        assert len(corners) == 4, f"rotation {degrees} collapses two corners"
        for x, y in corners:
            assert 0 <= x < base[0]
            assert 0 <= y < base[1]


def test_rotate_point_rejects_points_outside_the_displayed_window():
    """A coordinate outside the rotated window cannot be mapped honestly."""
    with pytest.raises(ValueError):
        rotate_point((1024, 0), (1376, 1024), 90)
    with pytest.raises(ValueError):
        rotate_point((0, 1376), (1376, 1024), 90)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def test_audio_none_is_explicit_and_emits_nothing():
    """PRD 3.2 P1: "no audio device emitted" is a first-class answer."""
    decision = AudioDecision(backend="none")
    assert decision.backend == "none"
    assert decision.emits_device() is False
    assert decision.to_qemu_fragments() == []
    assert decision.guest_audio_verified is False


def test_audio_none_is_the_default():
    """Nothing is emitted unless audio is explicitly requested."""
    assert decide().audio.backend == "none"
    assert AUDIO_MODES == ("none", "hda")
    assert decide().to_qemu_fragments() == decide(rotation=0).to_qemu_fragments()


def test_audio_hda_emits_exactly_the_three_qemu_pieces_in_order():
    """``-audiodev`` before the devices that reference it, or QEMU rejects the argv."""
    decision = AudioDecision(backend="hda")
    fragments = decision.to_qemu_fragments()
    assert fragments == [
        "-audiodev",
        "driver=none,id=unipad-audio",
        "-device",
        "intel-hda",
        "-device",
        "hda-duplex,audiodev=unipad-audio",
    ]
    assert decision.emits_device() is True
    assert fragments.index("-audiodev") < fragments.index("-device")


def test_audio_hda_uses_the_null_host_driver_on_purpose():
    """A real host driver would open host audio we have not verified.

    ``driver=none`` gives the guest a device to bind to while touching no host
    sound hardware, so nothing about host audio can be claimed.
    """
    fragments = AudioDecision(backend="hda").to_qemu_fragments()
    assert "driver=none" in fragments[1]
    assert not any("dsound" in f or "wav" in f or "sdl" in f for f in fragments)


def test_guest_audio_is_never_reported_as_verified():
    """No construction of this module can claim a guest audio stack was heard."""
    for backend in AUDIO_MODES:
        assert AudioDecision(backend=backend).guest_audio_verified is False
    assert decide(audio="hda").audio.guest_audio_verified is False


def test_audio_states_what_is_not_proved():
    """The unverified list is part of the answer, not a footnote."""
    decision = AudioDecision(backend="hda")
    assert decision.unverified, "an audio device with no unverified list is a claim"
    assert any("iPadOS" in item or "guest" in item for item in decision.unverified)


def test_an_unknown_audio_backend_is_refused():
    """A misspelled backend must not silently become ``none``."""
    with pytest.raises(UniPadError) as excinfo:
        decide(audio="intel-hda")
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert excinfo.value.details["requested_audio"] == "intel-hda"
    assert excinfo.value.details["supported_audio"] == list(AUDIO_MODES)


def test_audio_none_is_distinguishable_from_an_unset_audio():
    """``none`` is a decision; it must not look like "nobody decided"."""
    explicit = decide(audio="none")
    assert explicit.audio.backend == "none"
    assert explicit.to_dict()["audio"]["backend"] == "none"
    assert explicit.to_dict()["audio"]["emits_device"] is False


# ---------------------------------------------------------------------------
# Display format
# ---------------------------------------------------------------------------


def test_ramfb_emits_the_device_and_the_geometry_item():
    """The fw_cfg item is how the geometry reaches the guest driver."""
    fragments = decide().to_qemu_fragments()
    assert fragments[:2] == ["-device", "ramfb"]
    assert fragments[2:4] == ["-fw_cfg", "name=opt/org.unixnu/ramfb-size,string=1376x1024"]


def test_the_fw_cfg_item_carries_the_rotated_geometry():
    """A rotated panel must be described to the guest rotated."""
    assert decide(rotation=90).to_qemu_fragments()[3].endswith("string=1024x1376")
    assert decide(rotation=270).to_qemu_fragments()[3].endswith("string=1024x1376")
    assert decide(rotation=180).to_qemu_fragments()[3].endswith("string=1376x1024")


def test_virtio_gpu_emits_the_device_and_no_geometry_item():
    """ADR-0004: the VirtIO path is a separate route, not a ramfb variant."""
    decision = decide({"width": 1376, "height": 1024, "format": "virtio-gpu"})
    assert decision.format == "virtio-gpu"
    assert decision.to_qemu_fragments() == ["-device", "virtio-gpu-pci"]


def test_an_unknown_display_format_is_refused():
    """A format the launcher cannot translate is a usage error."""
    with pytest.raises(UniPadError) as excinfo:
        decide({"width": 1376, "height": 1024, "format": "vga"})
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert excinfo.value.details["supported_formats"] == list(DISPLAY_FORMATS)


def test_the_full_fragment_list_is_display_then_audio():
    """The launcher splices one list; the order must be display first, audio second."""
    fragments = decide(rotation=90, audio="hda").to_qemu_fragments()
    assert fragments[:2] == ["-device", "ramfb"]
    assert fragments[4] == "-audiodev"


# ---------------------------------------------------------------------------
# Install progress: the stage vocabulary
# ---------------------------------------------------------------------------


def test_the_stage_order_matches_the_real_installer_vocabulary():
    """PRD 3.2 P1: the stages are ``unipad.ipa.installer``'s, not invented ones.

    Asserted against the real enum *and* the real ``InstallResult`` contract, so
    a vocabulary change upstream fails here instead of rendering a stage the
    installer can no longer produce.
    """
    real_forward = [stage.value for stage in InstallStage if stage is not InstallStage.FAILED]
    assert STAGE_ORDER == tuple(real_forward)
    assert STAGE_ORDER == ("received", "checked", "transferred", "installed", "registered")
    assert set(STAGE_ORDER) | {"failed"} == {stage.value for stage in InstallStage}
    assert set(STAGE_ORDER) | {"failed"} == set(InstallResult.TERMINAL_STAGES) | set(STAGE_ORDER)


def test_stage_vocabulary_drift_is_empty():
    """Runtime drift guard used by the suite; non-empty means the vocabulary moved."""
    assert stage_vocabulary_drift() == ()


@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_every_stage_is_accepted_and_maps_to_a_percentage(stage: str):
    """Each forward stage is expressible and carries an ordinal percentage."""
    progress = InstallStageProgress.for_stage(stage)
    assert progress.stage == stage
    assert progress.percent is not None
    assert 0 <= progress.percent <= 100
    assert progress.failed is False


def test_percentage_is_monotonic_along_the_chain():
    """A progress model that goes backwards is a lie."""
    percents = [InstallStageProgress.for_stage(stage).percent for stage in STAGE_ORDER]
    assert percents == sorted(percents)
    assert percents[0] == 20      # received is the first of five stages
    assert percents[-1] == 100    # registered completes the chain


def test_failed_has_no_percentage_claim():
    """``failed`` does not say how far it got, so it must not render a number."""
    progress = InstallStageProgress.for_stage("failed", error_code="transfer_failed")
    assert progress.failed is True
    assert progress.percent is None
    assert progress.complete is False
    assert progress.error_code == "transfer_failed"


def test_registered_is_complete_and_succeeded():
    """Only the guest can report installed/registered (PRD 9.1)."""
    assert InstallStageProgress.for_stage("registered").complete is True
    assert InstallStageProgress.for_stage("registered").succeeded is True
    assert InstallStageProgress.for_stage("transferred").complete is False
    assert InstallStageProgress.for_stage("transferred").succeeded is False


def test_a_completed_stage_with_an_error_is_not_a_success():
    """An error code means the chain did not finish, whatever the stage says."""
    progress = InstallStageProgress(stage="installed", percent=80, error_code="dyld_failed")
    assert progress.complete is True
    assert progress.succeeded is False


def test_an_unknown_stage_is_refused():
    """A stage outside the vocabulary is a usage error, not a silent ``failed``."""
    with pytest.raises(UniPadError) as excinfo:
        InstallStageProgress(stage="downloaded")
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert excinfo.value.details["supported_stages"] == list(STAGE_ORDER) + ["failed"]


def test_progress_from_a_real_install_result():
    """The CLI hands over an ``InstallResult``; the mapping is mechanical."""
    result = InstallResult(
        package_sha256="a" * 64,
        bundle_id="com.example.app",
        stage="transferred",
    )
    progress = InstallStageProgress.from_install_result(result)
    assert progress.stage == "transferred"
    assert progress.percent == 60
    assert progress.bundle_id == "com.example.app"
    assert progress.package_sha256 == "a" * 64


def test_progress_from_a_failed_install_result_keeps_the_error():
    """A blocked install must render its reason, not a bare failure."""
    result = InstallResult(
        package_sha256="b" * 64,
        stage="failed",
        error_code="transfer_failed",
        error_message="hash mismatch",
    )
    progress = InstallStageProgress.from_install_result(result)
    assert progress.failed is True
    assert progress.percent is None
    assert progress.error_code == "transfer_failed"
    assert "hash mismatch" in render_progress(progress)


def test_progress_keeps_an_absent_bundle_id_as_none():
    """standards R-8: the mapping must not fabricate an empty string.

    ``unipad.models.InstallResult`` declares ``bundle_id: Optional[str] = None``
    -- an install that never learned the bundle's id is a real state, not a
    bundle whose id happens to be the empty string.  ``from_install_result``
    coerced it with ``str(getattr(result, "bundle_id", "") or "")`` while
    calling ``_optional_str`` on the two fields beside it in the same
    four-line expression, and ``_optional_str``'s own docstring promises the
    opposite: an absent value stays ``None``.  Two rules in four lines is the
    defect, so this pins the one rule on the field the source declares
    optional.
    """
    result = InstallResult(package_sha256="c" * 64, stage="transferred")

    progress = InstallStageProgress.from_install_result(result)

    assert result.bundle_id is None, "the fixture must exercise the absent case"
    assert progress.bundle_id is None
    # ... and the PRD 9.2 evidence view keeps it absent rather than inventing
    # a value nobody observed.
    assert progress.to_dict()["bundle_id"] is None


def test_the_optional_progress_fields_share_one_absence_rule():
    """The regression guard: the four optional attributes cannot drift apart.

    ``from_install_result`` maps four optional attributes.  Two of them were
    coerced to an empty string and two went through ``_optional_str``, so the
    rule an absent value followed depended on which line it happened to land
    on.  This asserts all of the genuinely optional ones at once, against a
    result that carries none of them, so re-introducing a second rule fails
    here rather than in whichever consumer notices first.
    """
    result = InstallResult(package_sha256="d" * 64, stage="failed")

    progress = InstallStageProgress.from_install_result(result)
    view = progress.to_dict()

    for name in ("bundle_id", "error_code", "error_message"):
        assert view[name] is None, (
            f"{name} was fabricated as {view[name]!r}; an absent optional field "
            "must stay None, which is what _optional_str already promises for "
            "the others"
        )


def test_a_real_installer_run_maps_onto_the_progress_model(tmp_path):
    """End-to-end against the real :class:`IpaInstaller` stage vocabulary.

    The installer is driven by the suite's recording guest channel, so no real
    guest exists; what is asserted is that every stage it can report is a stage
    this module can render.
    """
    from tests.fixtures.ipa_builder import hello_ipa
    from tests.fixtures.fake_guest_channel import RecordingGuestChannel

    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    installer = IpaInstaller(RecordingGuestChannel({"status": "installed", "installed": True}))
    result = installer.install(ipa)
    assert result.stage == "installed"

    progress = InstallStageProgress.from_install_result(result)
    assert progress.stage == "installed"
    assert progress.percent == 80
    text = render_progress(progress)
    assert text.isascii()
    assert "installed" in text
    assert "[ok]" in text


def test_a_blocked_installer_run_maps_onto_the_progress_model(tmp_path):
    """A guest that confirms nothing still renders as a failure, not a hang."""
    from tests.fixtures.ipa_builder import hello_ipa
    from tests.fixtures.fake_guest_channel import RecordingGuestChannel

    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    # The default reply is a non-confirming status, so the installer must stop
    # at ``failed`` rather than invent an install.
    installer = IpaInstaller(RecordingGuestChannel())
    result = installer.install(ipa)
    assert result.stage == "failed"
    assert result.error_code is not None

    progress = InstallStageProgress.from_install_result(result)
    assert progress.failed is True
    assert progress.percent is None
    assert "[!!]" in render_progress(progress)


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("percent", [0, 1, 25, 50, 75, 99, 100])
def test_the_renderer_handles_zero_partial_and_one_hundred(percent: int):
    """A terminal renderer must not crash at either end of the range."""
    text = render_progress(InstallStageProgress(stage="checked", percent=percent))
    assert text.isascii()
    assert f"{percent}%" in text
    assert "checked" in text
    assert "\n" not in text


def test_the_renderer_handles_an_unknown_percentage():
    """``failed`` has no percentage; the renderer shows a placeholder, not a crash."""
    text = render_progress(InstallStageProgress.for_stage("failed", error_code="blocked"))
    assert "?%" in text
    assert "blocked" in text
    assert "failed" in text


def test_the_bar_grows_with_the_percentage():
    """The filled length is a function of the percentage, not of the stage name."""
    empty = render_progress(InstallStageProgress(stage="received", percent=0), width=20)
    half = render_progress(InstallStageProgress(stage="checked", percent=50), width=20)
    full = render_progress(InstallStageProgress(stage="registered", percent=100), width=20)
    assert empty.count("#") == 0
    assert half.count("#") == 10
    assert full.count("#") == 20
    assert empty.count("-") == 20


def test_the_renderer_marks_completion_and_failure_distinctly():
    """A finished install and a failed one must never look alike."""
    done = render_progress(InstallStageProgress.for_stage("registered"))
    failed = render_progress(InstallStageProgress.for_stage("failed", error_code="blocked"))
    assert "[ok]" in done
    assert "[!!]" in failed
    assert "[!!]" not in done


def test_the_renderer_writes_to_a_stream_when_asked():
    """The CLI renders into a live terminal, so a stream target is required."""
    import io

    stream = io.StringIO()
    render_progress(InstallStageProgress.for_stage("checked"), stream=stream)
    assert stream.getvalue().endswith("\n")
    assert "checked" in stream.getvalue()


def test_the_renderer_is_a_single_line_for_every_stage():
    """A progress line that wraps mid-render is unreadable."""
    for stage in STAGE_ORDER + ("failed",):
        text = render_progress(
            InstallStageProgress.for_stage(stage, bundle_id="com.example.app", package_sha256="c" * 64)
        )
        assert "\n" not in text
        assert "\r" not in text


# ---------------------------------------------------------------------------
# PRD 9.2: no secrets in the output
# ---------------------------------------------------------------------------


def test_the_rendered_line_truncates_the_package_hash():
    """A 64-character digest in a progress bar is noise, and a hash is evidence."""
    sha = "d" * 64
    text = render_progress(InstallStageProgress(stage="checked", percent=40, package_sha256=sha))
    assert sha not in text
    assert sha[:12] in text


def test_no_decision_dict_carries_a_secret_shaped_key():
    """PRD 9.2: the decision object is serialised into the evidence bundle."""
    forbidden = ("password", "passwd", "secret", "token", "credential", "trust_mode", "private_key")
    for decision in (
        decide(),
        decide(rotation=90, audio="hda"),
        decide({"width": 2048, "height": 1536, "format": "virtio-gpu"}),
    ):
        as_dict = decision.to_dict()
        flat = json.dumps(as_dict)
        for key in forbidden:
            assert key not in flat, f"{key} must never appear in a display decision"


def test_the_decision_dict_round_trips_through_json():
    """The evidence bundle is JSON; the decision must survive the round trip."""
    decision = decide(rotation=270, audio="hda", width=2736, height=2048)
    restored = json.loads(json.dumps(decision.to_dict()))
    assert restored["framebuffer"] == [2048, 2736]
    assert restored["rotation_degrees"] == 270
    assert restored["audio"]["backend"] == "hda"
    assert restored["base"] == [2736, 2048]


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_the_decision_is_immutable():
    """A decision that can be rewritten after the fact is not evidence."""
    decision = decide()
    with pytest.raises(Exception):
        decision.framebuffer = (1024, 1376)  # type: ignore[misc]
    with pytest.raises(Exception):
        decision.audio.backend = "hda"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The config surface: what reaches MachineConfig, and what does not
# ---------------------------------------------------------------------------


def test_nested_display_keys_survive_machine_config_from_dict():
    """``display`` is an opaque ``dict[str, Any]``, so nested keys pass through.

    ``MachineConfig.from_dict`` drops unknown keys **at the top level only**.
    This is the contract the whole feature rests on: ``display.rotation`` and
    ``display.audio`` must arrive at the launcher intact, or the feature would
    pass every unit test here while doing nothing in production.
    """
    machine = MachineConfig.from_dict(
        {
            "name": "ipad-tcg",
            "display": {
                "width": 2048,
                "height": 1536,
                "format": "ramfb",
                "rotation": 90,
                "audio": "hda",
            },
        }
    )
    assert machine.display["rotation"] == 90
    assert machine.display["audio"] == "hda"
    assert decide(machine).rotation is Rotation.DEG_90
    assert decide(machine).audio.backend == "hda"


def test_the_real_config_file_carries_the_display_keys():
    """``config/machine.json`` must actually name rotation and audio.

    Reading the file the launcher reads is the only way to prove the keys are
    wired; a model default would prove nothing about production.
    """
    config = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))
    display = config["display"]
    assert "rotation" in display, "config/machine.json must name display.rotation"
    assert "audio" in display, "config/machine.json must name display.audio"
    assert display["rotation"] in [int(r) for r in ROTATIONS]
    assert display["audio"] in AUDIO_MODES


def test_a_top_level_typo_is_what_from_dict_actually_drops():
    """The silent-drop trap is at the *top level*, and ``unknown_keys`` sees it.

    ``MachineConfig.unknown_keys`` exists precisely because a dropped key means
    the file describes one machine while the launch builds another.  Naming the
    trap here stops a future reader from "fixing" the pass-through above by
    restructuring ``display`` into typed fields and breaking this feature.
    """
    typo = {"display": {"width": 1376, "height": 1024}, "displays": {"width": 1}}
    assert MachineConfig.unknown_keys(typo) == ("displays",)
    assert "displays" not in MachineConfig.from_dict(typo).to_dict()

    # A *nested* key is never a top-level typo, so it is never reported.
    nested = {"display": {"width": 1376, "height": 1024, "rotation": 90}}
    assert MachineConfig.unknown_keys(nested) == ()


# ---------------------------------------------------------------------------
# Rotation and the input transform (PRD 4.2, FR04)
# ---------------------------------------------------------------------------


def test_rotation_zero_does_not_move_a_tap():
    """The default must not change where a tap lands -- no regression.

    ``rotate_point`` is the identity at 0 degrees, so the existing input chain
    (``scale_point`` -> ``guest_to_qemu_abs``) is untouched for every run that
    does not ask for a rotation.
    """
    from unipad.host.input import guest_to_qemu_abs, scale_point

    base = (1376, 1024)
    displayed = rotate_geometry(base, 0)
    guest = scale_point((640, 512), displayed, displayed)
    assert rotate_point(guest, base, 0) == guest
    assert guest_to_qemu_abs(rotate_point(guest, base, 0), base) == guest_to_qemu_abs(guest, base)


def test_a_rotated_window_transposes_the_tap_onto_the_framebuffer():
    """PRD 4.2: a rotated framebuffer must transpose the input transform too.

    Without the transpose a tap lands 90 degrees off, which is the failure mode
    that makes a rotation feature worse than none.  The chain is: host window ->
    ``scale_point`` (onto the *displayed*, rotated window) -> ``rotate_point``
    (onto the framebuffer the guest driver owns) -> ``guest_to_qemu_abs``.
    """
    from unipad.host.input import guest_to_qemu_abs, scale_point

    base = (1376, 1024)
    for degrees in ROTATIONS:
        displayed = rotate_geometry(base, degrees)
        # A 1:1 window keeps scale_point the identity, so the only transform
        # under test is the rotation itself.
        window_click = scale_point((0, 0), displayed, displayed)
        framebuffer_pixel = rotate_point(window_click, base, degrees)
        assert 0 <= framebuffer_pixel[0] < base[0]
        assert 0 <= framebuffer_pixel[1] < base[1]
        # ... and it must be a *corner* of the framebuffer, not an interior point.
        assert framebuffer_pixel in {
            (0, 0),
            (base[0] - 1, 0),
            (0, base[1] - 1),
            (base[0] - 1, base[1] - 1),
        }, f"rotation {degrees} moved the top-left click off a framebuffer corner"


def test_the_input_transpose_is_the_inverse_of_the_geometry_rotation():
    """Every framebuffer pixel must be reachable from exactly one window point.

    If ``rotate_point`` were not the exact inverse, some framebuffer pixels
    would be unreachable and others would be double-delivered.
    """
    base = (1376, 1024)
    for degrees in ROTATIONS:
        displayed = rotate_geometry(base, degrees)
        seen = set()
        for x in range(0, displayed[0], 97):
            for y in range(0, displayed[1], 89):
                pixel = rotate_point((x, y), base, degrees)
                assert 0 <= pixel[0] < base[0]
                assert 0 <= pixel[1] < base[1]
                seen.add(pixel)
        assert len(seen) > 1, f"rotation {degrees} collapsed the whole window onto one pixel"


def test_the_launcher_cannot_pick_a_resolution_of_its_own(tmp_path):
    """Answering "can any caller be handed an arbitrary resolution?" with a test.

    Proved behaviourally through the real launcher rather than by reading its
    source: an out-of-catalogue geometry is refused on the ``build_args`` path,
    and a catalogue entry is honoured.  So the only way to select a resolution
    is ``config/machine.json``, and a size nobody catalogued cannot reach QEMU.
    """
    from unipad.host.qemu import QemuLauncher

    def build(display: dict) -> list[str]:
        machine = MachineConfig(display=display)
        return QemuLauncher(machine).build_args(
            tmp_path / "fw", tmp_path / "sys", None, tmp_path, 4444
        )

    for path in (tmp_path / "fw", tmp_path / "sys"):
        path.write_bytes(b"unipad-test-image")

    # A geometry nobody catalogued never reaches the argv.
    with pytest.raises(UniPadError) as excinfo:
        build({"width": 1112, "height": 834, "format": "ramfb"})
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    # A catalogue entry does, and the route is chosen by format, not geometry.
    args = build({"width": 2048, "height": 1536, "format": "ramfb"})
    assert "name=opt/org.unixnu/ramfb-size,string=2048x1536" in args
    assert "-device" in args and args[args.index("-device") + 1] == "ramfb"


# ---------------------------------------------------------------------------


# Imported at the bottom of the import block so the module-level import list
# stays about the unit under test; the installer is the integration partner.
from unipad.ipa.installer import IpaInstaller  # noqa: E402
