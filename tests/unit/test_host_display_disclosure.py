"""The route-disclosure docstring describes a code path that exists.

``unipad.host.display`` decides the display/audio/rotation block and then hands
the launcher the argv pieces (:meth:`DisplayDecision.to_qemu_fragments`).  On
the ``virtio-gpu`` route the validated geometry never reaches the guest, so the
module also carries a *disclosure* of that drop --
:attr:`DisplayDecision.framebuffer_in_argv`,
:attr:`DisplayDecision.route_limitations` and
:attr:`DisplayDecision.route_notes` -- in machine-readable and prose form.

The module docstring used to say both reach the operator "through
:meth:`DisplayDecision.to_dict` and through ``config/machine.json``
``display.resolution_note``".  Only the second half was true: no consumer
anywhere reads the disclosure keys back out of a display decision, so the
machine-readable form of the drop existed inside this module and nowhere else.
``config/machine.json``'s own ``resolution_note`` already says so in as many
words -- "no run path calls ``DisplayDecision.to_dict()``, and ``cmd_start``
builds ``launch.json`` from ``LaunchResult.to_dict()``, which carries no display
block" -- and calls a promise nothing keeps "the same defect this note was
written to correct".  A docstring is the same kind of promise.

What these tests pin is the agreement between the prose and the code:

* the launcher, which is the run path that consumes a display decision, never
  asks it for its machine-readable view;
* the module docstring does not claim the disclosure reaches an operator through
  that view while nothing consumes it -- and stops asserting anything the moment
  a consumer appears, so the test tracks the truth instead of freezing a gap;
* the disclosure itself is *not* weakened by correcting the prose -- the closed
  vocabulary, the prose notes, the machine-readable view and the operator-facing
  config note all stay, and the e2e route tests in
  ``tests/unit/test_host_display_route.py`` still parse the expected geometry
  out of the assembled argv.

Precision note: ``DisplayDecision.to_dict()`` *is* called once in the product,
at ``unipad.acceptance.p1``'s own display check -- and it reads
``['framebuffer']``, the geometry, not the disclosure.  So the claim that was
false is specifically about **the disclosure reaching the operator** through it,
and that is what is pinned here rather than the weaker "nothing calls to_dict".
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from unipad.host import display
from unipad.host.qemu import QemuLauncher

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

#: The module that owns the disclosure.  Every one of these names is expected to
#: appear here and *only* here until something consumes them.
DISCLOSURE_KEYS = ("framebuffer_in_argv", "route_limitations", "route_notes")

#: The factories that return a :class:`DisplayDecision`.  ``unipad.host.qemu``
#: imports the second one under an alias, which is why both spellings are listed.
_DECISION_FACTORIES = frozenset(
    {
        "unipad.host.display.decide",
        "decide_display",
        "display.decide",
        "decide",
    }
)


def _dotted(node: ast.AST) -> str:
    """Return ``a.b.c`` for an attribute/name chain, ``""`` for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _disclosure_consumers() -> dict[str, set[str]]:
    """Map each disclosure key to the modules that name it.

    Read out of the source rather than out of a runtime profile, so a consumer
    added on a path no test exercises is still found.  A consumer is any module
    that *names* the key -- as an attribute, a subscript or a string literal --
    because all three are how a caller would read it back.

    This is a fact-finder, not a pin: the docstring test below conditions on it
    rather than requiring it to stay empty, because the honest end state is a
    module that consumes the keys *and* a docstring that says so.
    """
    found: dict[str, set[str]] = {key: set() for key in DISCLOSURE_KEYS}
    for path in sorted(SRC.rglob("*.py")):
        if path == SRC / "unipad" / "host" / "display.py":
            continue
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(REPO_ROOT).as_posix()
        for key in DISCLOSURE_KEYS:
            if key in text:
                found[key].add(relative)
    return found


def _module_docstring() -> str:
    """Return the ``unipad.host.display`` module docstring."""
    source = (SRC / "unipad" / "host" / "display.py").read_text(encoding="utf-8")
    return ast.get_docstring(ast.parse(source)) or ""


def _virtio_decision() -> display.DisplayDecision:
    """A decision on the route that drops the geometry it validated."""
    return display.decide(
        {
            "width": 1376,
            "height": 1024,
            "format": "virtio-gpu",
            "rotation": 90,
        }
    )


# ---------------------------------------------------------------------------
# the fact the docstring has to agree with
# ---------------------------------------------------------------------------


def test_no_run_path_asks_the_decision_for_its_machine_readable_view(tmp_path: Path) -> None:
    """The launcher builds the argv without ever calling ``to_dict()``.

    Driven through the real :class:`~unipad.host.qemu.QemuLauncher` with the
    shipped ``config/machine.json``, because ``build_args`` is the run path that
    consumes a display decision: it splices
    :meth:`DisplayDecision.to_qemu_fragments` into the argv and nothing else.
    A spy on ``to_dict`` records whether anything asks for the evidence view on
    the way.
    """
    from unipad.models import MachineConfig

    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    system = tmp_path / "system.img"
    system.write_bytes(b"system")

    calls: list[display.DisplayDecision] = []
    real = display.DisplayDecision.to_dict

    def spy(self: display.DisplayDecision) -> dict:
        calls.append(self)
        return real(self)

    display.DisplayDecision.to_dict = spy  # type: ignore[method-assign]
    try:
        raw = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))
        config = (
            MachineConfig.from_dict(raw)
            if hasattr(MachineConfig, "from_dict")
            else MachineConfig(**raw)
        )
        launcher = QemuLauncher(config, qmp_host="127.0.0.1")
        argv = launcher.build_args(
            firmware,
            system,
            None,
            tmp_path,
            4444,
            headless=True,
            serial_log=tmp_path / "serial.log",
        )
    finally:
        display.DisplayDecision.to_dict = real  # type: ignore[method-assign]

    # The display block really was built and really reached the argv...
    assert any("ramfb" in str(arg) for arg in argv), argv
    # ...and nothing asked the decision for its evidence view on the way.
    assert calls == [], "a run path now calls DisplayDecision.to_dict(); update the docstring"


# ---------------------------------------------------------------------------
# the docstring
# ---------------------------------------------------------------------------


def test_the_module_docstring_does_not_promise_a_path_that_is_not_called() -> None:
    """callchain finding 4: the prose and the code say the same thing.

    The module docstring used to claim the route disclosure reaches the operator
    "through :meth:`DisplayDecision.to_dict`".  No module outside this one names
    those keys, so the claim described a code path that does not exist -- a false
    promise about where a PRD 9.2 evidence fact surfaces.
    """
    docstring = _module_docstring()

    if any(_disclosure_consumers().values()):
        pytest.skip(
            "a module now consumes the disclosure keys; the docstring may promise "
            "them, and this test stops second-guessing it"
        )

    # A window rather than a sentence split: the prose names
    # ``config/machine.json`` and ``DisplayDecision.to_dict``, and both contain
    # the full stop a naive split would break on.
    for match in re.finditer(r"to_dict", docstring):
        window = docstring[max(0, match.start() - 160) : match.end() + 160]
        assert "operator" not in window, (
            "the module docstring claims the disclosure reaches the operator "
            "through to_dict(), but no module names the disclosure keys: %r"
            % docstring[max(0, match.start() - 80) : match.end() + 80].strip()
        )

    # The operator-facing surface that *is* real is still named, so the
    # correction does not quietly become a second gap.
    assert "config/machine.json" in docstring
    assert "resolution_note" in docstring


def test_the_config_note_is_the_surface_the_docstring_names() -> None:
    """The note the docstring points at really does state the limitation.

    Correcting the docstring to name ``config/machine.json`` is only honest if
    that note carries the disclosure in operator-facing words.
    """
    config = json.loads((REPO_ROOT / "config" / "machine.json").read_text(encoding="utf-8"))
    display_section = config["display"]

    assert "resolution_note" in display_section
    assert "rotation_note" in display_section
    for note in (display_section["resolution_note"], display_section["rotation_note"]):
        assert "virtio-gpu" in note
        assert "does NOT reach the guest" in note


# ---------------------------------------------------------------------------
# the disclosure itself is not weakened by correcting the prose
# ---------------------------------------------------------------------------


def test_the_disclosure_itself_is_not_weakened() -> None:
    """Correcting the prose must not cost the operator the disclosure."""
    decision = _virtio_decision()

    assert decision.framebuffer_in_argv is False
    assert decision.route_limitations == (
        "resolution_not_configured",
        "rotation_not_configured",
    )
    assert all(note.strip() for note in decision.route_notes)
    assert "ADR-0004" in decision.route_notes[0]

    evidence = decision.to_dict()
    assert evidence["framebuffer_in_argv"] is False
    assert evidence["route_limitations"] == [
        "resolution_not_configured",
        "rotation_not_configured",
    ]
    assert evidence["route_notes"]
    assert json.loads(json.dumps(evidence))["framebuffer_in_argv"] is False


def test_a_complete_route_still_reports_no_limitations() -> None:
    """An empty disclosure where nothing was dropped, or it is noise."""
    decision = display.decide({"width": 1376, "height": 1024, "format": "ramfb", "rotation": 0})

    assert decision.framebuffer_in_argv is True
    assert decision.route_limitations == ()
    assert decision.route_notes == ()
    assert decision.to_dict()["route_limitations"] == []
    assert decision.to_dict()["route_notes"] == []
