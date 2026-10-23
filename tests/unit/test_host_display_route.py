"""Route-evidence regression tests for :mod:`unipad.host.display`.

Three findings, one file.  Each is a *route* defect -- a place where the
decision, the argv and the evidence set stop describing the same run -- so each
test below compares the decision against the argv the launcher would actually
build rather than against a restatement of the rule.

1. **reverse review M1** -- on the ``virtio-gpu`` route a validated resolution
   and rotation are silently inert.  The drop used to be traceable only as the
   *absence* of a ``-fw_cfg`` item, so a run succeeded, the screenshot showed
   whatever the guest negotiated, and nothing said the geometry was dropped.
   Disclosure is the fix (ADR-0004 says the route negotiates its own geometry,
   which is a real design fact, so the routing is not touched).

2. **callchain review 2.4** -- ``DisplayDecision.to_dict()`` reported the
   rotated framebuffer unconditionally while ``to_qemu_fragments()`` discarded
   it on the VirtIO route, so the PRD 9.2 evidence described a geometry QEMU
   never received.  ``to_dict`` must not assert a geometry the argv does not
   carry.

3. **standards review CG-6 / N-3** -- ``decide()`` used to read three of the
   five keys the display mapping carries, ignoring a configured ``rotation``
   and ``audio``, and the caller compensated by re-reading the same mapping.
   ``decide`` is now the single reader, and the defaults live in named
   constants instead of being spelled at three call sites.

The closed vocabularies are load-bearing and are *not* loosened anywhere here:
the resolution catalogue, the 0/90/180/270 turn vocabulary and the audio
backend vocabulary all still refuse what they refused before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.display import (
    AUDIO_MODES,
    DEFAULT_AUDIO,
    DEFAULT_ROTATION,
    DISPLAY_FORMATS,
    DISPLAY_MODES,
    PINNED_DISPLAY,
    ROTATIONS,
    ROUTE_LIMITATIONS,
    ROUTE_LIMITATIONS_BY_FORMAT,
    ROUTE_LIMITATION_NOTES,
    Rotation,
    decide,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A request whose resolution *and* rotation are both outside the pinned
#: geometry, so a test that accidentally used the shipped numbers would pass
#: even if the route dropped nothing at all.
NON_PINNED_MODE = DISPLAY_MODES[3]  # ipad-pro-11, 2400x1680


def _fw_cfg_geometry(fragments: list[str]) -> str | None:
    """Return the geometry the joined argv actually carries, or ``None``.

    Parsed out of the argv rather than restated from the decision: the whole
    point of findings 2.4 and M1 is that the evidence and the argv must be
    compared, and a test that derived the expected geometry from
    ``decision.framebuffer`` would agree with a lying ``to_dict`` by
    construction.
    """
    for index, piece in enumerate(fragments):
        if piece == "-fw_cfg":
            value = fragments[index + 1]
            _, _, geometry = value.partition("string=")
            return geometry
    return None


# ---------------------------------------------------------------------------
# M1 -- the virtio-gpu route must disclose what it does not configure
# ---------------------------------------------------------------------------


def test_the_virtio_gpu_route_still_emits_no_geometry_item():
    """ADR-0004: the routing is correct and must not be "fixed".

    This is the guard that stops a future "fix" from forcing geometry onto the
    VirtIO route.  The defect was the missing disclosure, never the routing.
    """
    decision = decide(
        {
            "width": NON_PINNED_MODE.width,
            "height": NON_PINNED_MODE.height,
            "format": "virtio-gpu",
            "rotation": 90,
            "audio": "hda",
        }
    )

    fragments = decision.to_qemu_fragments()

    assert fragments[:2] == ["-device", "virtio-gpu-pci"]
    assert _fw_cfg_geometry(fragments) is None
    # The audio decision is unaffected by the route: it is a separate argv
    # block and a disclosure about geometry must not swallow it.
    assert "-audiodev" in fragments


def test_the_virtio_gpu_route_reports_that_the_geometry_is_not_in_the_argv():
    """M1: the drop is stated, not inferred from an absent argv item."""
    decision = decide(
        {
            "width": NON_PINNED_MODE.width,
            "height": NON_PINNED_MODE.height,
            "format": "virtio-gpu",
            "rotation": 90,
        }
    )

    assert decision.framebuffer_in_argv is False
    assert decision.route_limitations == (
        "resolution_not_configured",
        "rotation_not_configured",
    )
    # Operator-facing prose for every machine-readable key: a key alone tells a
    # reader comparing two evidence sets that something was dropped, but not
    # what to do about it.
    assert len(decision.route_notes) == len(decision.route_limitations)
    assert all(note.strip() for note in decision.route_notes)
    assert "ADR-0004" in decision.route_notes[0]


def test_the_disclosure_is_in_the_evidence_dict():
    """M1/2.4: the PRD 9.2 evidence set is what a reproduction is built from."""
    decision = decide(
        {
            "width": NON_PINNED_MODE.width,
            "height": NON_PINNED_MODE.height,
            "format": "virtio-gpu",
            "rotation": 90,
        }
    )

    evidence = decision.to_dict()

    assert evidence["framebuffer_in_argv"] is False
    assert evidence["route_limitations"] == [
        "resolution_not_configured",
        "rotation_not_configured",
    ]
    assert evidence["route_notes"]
    # JSON round trip: the marker has to survive the evidence file on disk.
    assert json.loads(json.dumps(evidence))["framebuffer_in_argv"] is False


def test_a_complete_route_reports_no_limitations():
    """The disclosure must be empty where nothing was dropped, or it is noise."""
    decision = decide({"width": 1376, "height": 1024, "format": "ramfb", "rotation": 0})

    assert decision.framebuffer_in_argv is True
    assert decision.route_limitations == ()
    assert decision.route_notes == ()
    assert decision.to_dict()["route_limitations"] == []
    assert decision.to_dict()["route_notes"] == []


def test_every_display_route_has_its_disclosure_written_down():
    """A new route cannot be added without deciding what it drops."""
    assert set(ROUTE_LIMITATIONS_BY_FORMAT) == set(DISPLAY_FORMATS)
    for spelling in DISPLAY_FORMATS:
        limitations = ROUTE_LIMITATIONS_BY_FORMAT[spelling]
        assert isinstance(limitations, tuple)
        assert set(limitations) <= set(ROUTE_LIMITATIONS)


def test_every_limitation_key_has_a_note():
    """A machine-readable key with no prose is half a disclosure."""
    assert set(ROUTE_LIMITATION_NOTES) == set(ROUTE_LIMITATIONS)


def test_the_route_limitation_vocabulary_is_closed():
    """The keys are a vocabulary, not free text, so two runs are comparable."""
    assert set(ROUTE_LIMITATIONS) == {
        "resolution_not_configured",
        "rotation_not_configured",
    }


def test_the_shipped_config_states_the_resolution_limitation_operator_facing():
    """M1's asymmetry: rotation's limitation was operator-facing, resolution's
    existed only as a code comment.  The config note closes that gap, and this
    test fails if the note is dropped from ``config/machine.json`` again."""
    display = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))[
        "display"
    ]

    assert "resolution_note" in display
    assert "rotation_note" in display
    # Both notes must name the same route limitation in the same words, or the
    # asymmetry the review found (rotation documented, resolution not) returns.
    for note in (display["resolution_note"], display["rotation_note"]):
        assert "virtio-gpu" in note
        assert "does NOT reach the guest" in note


# ---------------------------------------------------------------------------
# reverse-m3 -- the catalogue must be discoverable without reading the source
# ---------------------------------------------------------------------------


def _operator_catalogue() -> list[dict[str, Any]]:
    """Return the catalogue as the operator-facing config lists it.

    Parsed out of ``config/machine.json`` rather than out of
    :data:`DISPLAY_MODES`: the point of reverse-m3 is that the operator never
    opens ``display.py``, so the surface under test is the file they *do* read.
    """
    display = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))[
        "display"
    ]
    surface = display.get("resolution_catalogue")
    assert isinstance(surface, list), (
        "config/machine.json display carries no resolution_catalogue, so the "
        "catalogue exists only as source literals in display.py"
    )
    return surface


def test_the_catalogue_is_listed_on_a_surface_that_needs_no_source():
    """reverse-m3: the five geometries must be readable without opening display.py.

    The comparison is between two independently stored lists -- the config's
    operator-facing surface and the module's closed catalogue -- so it fails in
    *both* directions: a sixth mode added to ``DISPLAY_MODES`` without the
    config, and a geometry in the config the code does not accept.
    """
    listed = {
        (int(entry["width"]), int(entry["height"])) for entry in _operator_catalogue()
    }

    assert listed == {mode.size for mode in DISPLAY_MODES}
    assert len(listed) == len(DISPLAY_MODES)


def test_the_catalogue_surface_names_every_mode():
    """Names, not just numbers: ``error.details.supported`` carries numbers, and
    a mode's name is what the evidence set records."""
    listed = {entry["name"] for entry in _operator_catalogue()}

    assert listed == {mode.name for mode in DISPLAY_MODES}


def test_the_catalogue_surface_identifies_the_pinned_geometry():
    """An operator reading the config must be able to tell which of the five is
    the one this project boots, or the list is five equal options."""
    display = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))[
        "display"
    ]
    shipped = (display["width"], display["height"])
    entries = {
        (int(entry["width"]), int(entry["height"])): entry
        for entry in _operator_catalogue()
    }

    assert shipped in entries
    assert entries[shipped].get("pinned") is True
    # ... and exactly one entry claims to be the pinned one.
    assert sum(1 for entry in _operator_catalogue() if entry.get("pinned")) == 1


def test_the_resolution_note_does_not_send_the_operator_to_the_source():
    """The m3 defect in my own first note: it said "see
    unipad.host.display.DISPLAY_MODES", which points at the source instead of
    listing anything.  A note that names a module is not a readable surface."""
    display = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))[
        "display"
    ]

    assert "unipad.host.display" not in display["resolution_note"]
    assert "resolution_catalogue" in display["resolution_note"]


def test_the_catalogue_surface_is_a_list_of_objects_not_prose():
    """Machine-parseable: a regex over a sentence rots the first time the
    sentence is reworded, and then the surface silently stops being readable."""
    for entry in _operator_catalogue():
        assert set(entry) >= {"name", "width", "height"}
        assert isinstance(entry["width"], int)
        assert isinstance(entry["height"], int)
        assert not isinstance(entry["width"], bool)


# ---------------------------------------------------------------------------
# 2.4 -- to_dict() must not assert a geometry the argv does not carry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("display_format", DISPLAY_FORMATS, ids=DISPLAY_FORMATS)
@pytest.mark.parametrize("rotation", ROTATIONS, ids=[str(turn) for turn in ROTATIONS])
def test_the_evidence_agrees_with_the_argv_on_every_route_and_turn(
    display_format: str, rotation: int
):
    """2.4: the evidence set and the command line describe one run.

    The expectation is read out of the argv, not out of the decision, so a
    ``to_dict`` that claims a geometry the fragments do not carry goes red.
    """
    decision = decide(
        {
            "width": NON_PINNED_MODE.width,
            "height": NON_PINNED_MODE.height,
            "format": display_format,
            "rotation": rotation,
        }
    )
    fragments = decision.to_qemu_fragments()
    carried = _fw_cfg_geometry(fragments)

    evidence = decision.to_dict()

    if carried is None:
        # No geometry on the command line: the evidence must say so rather
        # than reporting a framebuffer the guest never received.
        assert evidence["framebuffer_in_argv"] is False
        assert "resolution_not_configured" in evidence["route_limitations"]
        if Rotation(rotation).swaps_axes:
            assert "rotation_not_configured" in evidence["route_limitations"]
    else:
        assert evidence["framebuffer_in_argv"] is True
        assert evidence["route_limitations"] == []
        # The geometry the argv carries is exactly the one the evidence
        # reports -- width, height and turn all have to line up.
        assert carried == f"{evidence['framebuffer'][0]}x{evidence['framebuffer'][1]}"
        assert evidence["base"] == [NON_PINNED_MODE.width, NON_PINNED_MODE.height]


def test_the_reported_framebuffer_is_the_one_the_operator_asked_for():
    """``framebuffer`` stays a request record; the marker disambiguates it.

    Renaming or blanking the field would break the readers that legitimately
    need "what was asked for" (``unipad.acceptance.p1``), so the fix is the
    marker next to it rather than a changed field.
    """
    decision = decide(
        {
            "width": NON_PINNED_MODE.width,
            "height": NON_PINNED_MODE.height,
            "format": "virtio-gpu",
            "rotation": 90,
        }
    )

    evidence = decision.to_dict()

    assert evidence["base"] == [2400, 1680]
    assert evidence["framebuffer"] == [1680, 2400]
    assert evidence["rotation_degrees"] == 90
    assert evidence["framebuffer_in_argv"] is False


def test_the_marker_is_absent_from_nothing_a_reader_can_skip():
    """A reader that pulls ``to_dict()["display"]`` cannot miss the marker.

    The marker is a sibling of ``framebuffer`` in the same mapping, so any
    serialisation of the decision carries both or neither.
    """
    evidence = decide({"width": 1376, "height": 1024, "format": "virtio-gpu"}).to_dict()

    assert "framebuffer" in evidence
    assert "framebuffer_in_argv" in evidence
    assert "route_limitations" in evidence


# ---------------------------------------------------------------------------
# CG-6 / N-3 -- decide() is the single reader of the display mapping
# ---------------------------------------------------------------------------


def test_decide_honours_a_rotation_that_exists_only_in_the_mapping():
    """CG-6: ``decide(machine)`` must not drop a configured turn."""
    configured = {"width": 1376, "height": 1024, "format": "ramfb", "rotation": 90}

    assert decide(configured).rotation is Rotation.DEG_90
    assert decide(configured).framebuffer == (1024, 1376)
    assert decide(configured).to_dict()["rotation_degrees"] == 90
    # ... and it reaches the argv, not just the evidence.
    assert _fw_cfg_geometry(decide(configured).to_qemu_fragments()) == "1024x1376"


def test_decide_honours_an_audio_backend_that_exists_only_in_the_mapping():
    """CG-6: same for audio -- a configured ``hda`` emits the device."""
    configured = {"width": 1376, "height": 1024, "format": "ramfb", "audio": "hda"}

    assert decide(configured).audio.backend == "hda"
    assert "-device" in decide(configured).to_qemu_fragments()
    assert "intel-hda" in decide(configured).to_qemu_fragments()


def test_decide_honours_both_keys_from_a_machine_config_object():
    """CG-6: the object form is what the launcher passes, so it must work too."""
    from unipad.models import MachineConfig

    machine = MachineConfig(
        display={
            "width": 1376,
            "height": 1024,
            "format": "ramfb",
            "rotation": 270,
            "audio": "hda",
        }
    )

    decision = decide(machine)

    assert decision.rotation is Rotation.DEG_270
    assert decision.audio.backend == "hda"


def test_an_explicit_request_still_overrides_the_mapping():
    """CG-6: honouring the mapping must not stop an explicit override winning."""
    configured = {"width": 1376, "height": 1024, "format": "ramfb", "rotation": 90, "audio": "hda"}

    assert decide(configured, rotation=180).rotation is Rotation.DEG_180
    assert decide(configured, audio="none").audio.backend == "none"


@pytest.mark.parametrize("turn", ROTATIONS, ids=[str(value) for value in ROTATIONS])
def test_every_turn_in_the_vocabulary_survives_the_mapping(turn: int):
    """The whole closed turn vocabulary round-trips through the mapping."""
    configured = {"width": 1376, "height": 1024, "rotation": turn}

    assert int(decide(configured).rotation) == turn


def test_a_rotation_only_in_the_mapping_is_still_validated():
    """Honouring the mapping must not bypass the closed vocabulary."""
    configured = {"width": 1376, "height": 1024, "rotation": 45}

    with pytest.raises(UniPadError) as excinfo:
        decide(configured)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert excinfo.value.details["supported_rotations"] == list(ROTATIONS)


def test_an_audio_backend_only_in_the_mapping_is_still_validated():
    """Same for the audio vocabulary -- no loosening to make a test pass."""
    configured = {"width": 1376, "height": 1024, "audio": "dsound"}

    with pytest.raises(UniPadError) as excinfo:
        decide(configured)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert excinfo.value.details["supported_audio"] == list(AUDIO_MODES)


def test_the_defaults_are_named_constants_not_call_site_literals():
    """N-3: one spelling of the default, reachable from the module."""
    assert DEFAULT_ROTATION is Rotation.DEG_0
    assert DEFAULT_AUDIO == "none"
    assert DEFAULT_AUDIO in AUDIO_MODES


def test_an_absent_rotation_or_audio_falls_back_to_the_named_default():
    """N-3: the fallback is the constant, and nothing else is invented."""
    bare = {"width": 1376, "height": 1024}

    assert decide(bare).rotation is DEFAULT_ROTATION
    assert decide(bare).audio.backend == DEFAULT_AUDIO
    assert decide().rotation is DEFAULT_ROTATION
    assert decide().audio.backend == DEFAULT_AUDIO


def test_the_shipped_config_agrees_with_the_modules_defaults():
    """Drift guard, the counterpart of the geometry three-way agreement test.

    ``config/machine.json`` carries an *explicit* rotation and audio, which the
    operator is free to change; what must not drift is the vocabulary they are
    expressed in and the geometry the module pins on its own.
    """
    config = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))
    display = config["display"]

    assert display["rotation"] in ROTATIONS
    assert display["audio"] in AUDIO_MODES
    assert (display["width"], display["height"]) == (
        PINNED_DISPLAY["width"],
        PINNED_DISPLAY["height"],
    )
    assert display["format"] == PINNED_DISPLAY["format"]


# ---------------------------------------------------------------------------
# the five reviews, applied to this fix
# ---------------------------------------------------------------------------


def test_the_reverse_case_a_caller_outside_build_args_sees_the_same_truth():
    """Reverse review: a caller that never goes through ``build_args``.

    ``unipad.acceptance.p1`` calls ``decide()`` directly, and any future caller
    will too.  Such a caller now gets the route disclosure without having to
    know the disclosure exists -- which is the property that was missing.
    """
    direct = decide(
        {
            "width": NON_PINNED_MODE.width,
            "height": NON_PINNED_MODE.height,
            "format": "virtio-gpu",
            "rotation": 90,
        }
    )

    # Same object, same answer, no argv knowledge required of the caller.
    assert direct.framebuffer_in_argv is False
    assert direct.route_limitations
    assert direct.to_dict()["route_limitations"]
    # And a caller on the complete route gets an empty disclosure rather than
    # a disclaimer it would have to filter out.
    complete = decide()
    assert complete.route_limitations == ()
    assert complete.to_dict()["route_notes"] == []


def test_the_disclosure_is_derived_not_recomputed():
    """Performance review: nothing is recomputed per launch.

    The marker is a comparison against ``format`` and a mapping lookup, both
    O(1), and the notes are module-level constants rather than strings built
    on every ``to_dict()`` call.
    """
    decision = decide({"width": 1376, "height": 1024, "format": "virtio-gpu"})

    assert decision.route_notes is not None
    # Two calls return equal results, and the notes are the shared constants
    # rather than freshly allocated prose that could drift between calls.
    assert decision.route_notes == decision.route_notes
    assert decision.route_notes == tuple(
        ROUTE_LIMITATION_NOTES[key] for key in decision.route_limitations
    )
