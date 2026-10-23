"""The two launcher scripts must agree about a controller payload.

``scripts/windows/Start-UniPad.ps1`` and ``scripts/windows/UniPad-Launcher.ps1``
each carry their own copy of ``Get-ControllerVerdict``.  Callchain review 7.3
found that the copy in ``Start-UniPad.ps1`` was missing the single line that
collects the payload's property names, so ``$names`` was ``$null`` inside it,
both ``-contains`` loops were always False, and the function returned
``indeterminate`` for *every* payload -- success included::

    Start-UniPad.ps1   verdict = indeterminate
    UniPad-Launcher    verdict = success
    DIVERGENT: True

The failure was closed, which is why it survived review: the caller's
``indeterminate`` branch printed a warning and exited 0, so nothing ever
claimed a success it had not observed.  What it cost was the success branch at
``Start-UniPad.ps1:562`` being dead code, and ``launcher.cmd start`` never
being able to report success at all.

The defect was **divergence between two copies**, so that is what this file
pins.  Both copies are extracted from their real scripts and driven with the
same payload matrix; the assertion is that they agree *and* that the agreed
answer is the right one, so a shared wrong answer cannot pass by being shared.

Only the verdict function is executed.  Neither script is dot-sourced and no
path reaches QEMU, matching ``test_launcher_script.py``'s rule that the
``Start`` delegation is never invoked for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance

ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = ROOT / "scripts" / "windows" / "Start-UniPad.ps1"
LAUNCHER_SCRIPT = ROOT / "scripts" / "windows" / "UniPad-Launcher.ps1"

FUNCTION = "Get-ControllerVerdict"

#: The payload matrix and the verdict each one must produce.  Both copies have
#: to land on the right answer, not merely on the same answer.
#:
#: ``indeterminate`` is the honest answer for a payload that says nothing this
#: vocabulary understands: the caller must not report success, and must not
#: report failure either.
PAYLOADS: tuple[tuple[dict, str], ...] = (
    ({"ok": True, "status": "ok"}, "success"),
    ({"ok": False, "status": "ok"}, "failure"),
    ({"success": True}, "success"),
    ({"ok": "yes"}, "indeterminate"),          # a non-bool truthy value says nothing
    ({"status": "blocked"}, "failure"),
    ({"status": "started"}, "success"),
    ({"status": "completed"}, "success"),
    # The comparison lowercases the value, so a shouted negative is still a
    # negative: "FAILED" -> "failed", which is in the negative list.
    ({"status": "FAILED"}, "failure"),
    # ... and a shouted positive is still a positive.  Pinned because getting
    # this pair backwards is exactly the kind of off-by-one-inversion that
    # passes a test written from the implementation instead of the contract.
    ({"status": "STARTED"}, "success"),
    ({"status": ""}, "indeterminate"),
    ({"error": {"code": "blocked"}}, "failure"),
    ({"data": {"pid": 1}}, "indeterminate"),    # no key the vocabulary knows
)


def _powershell() -> str:
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    if not exe:
        pytest.skip("powershell.exe is not on PATH")
    return exe


def _extract_function(path: Path, name: str) -> str:
    """Return the source of ``name`` from ``path``, brace-matched.

    Extracting beats dot-sourcing: dot-sourcing would run the script's
    top-level code, and these scripts start a guest.
    """
    text = path.read_text(encoding="utf-8")
    start = text.index(f"function {name} {{")

    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                body = text[start : index + 1]
                break
    else:  # pragma: no cover - a malformed script, not a test failure
        pytest.fail(f"could not brace-match {name} in {path.name}")

    assert body.startswith(f"function {name} {{")
    assert body.endswith("}")
    return body


def _verdicts(tmp_path: Path, payload: dict) -> dict[str, str]:
    """Run both copies of the verdict function against one payload."""
    start_copy = _extract_function(START_SCRIPT, FUNCTION)
    launcher_copy = _extract_function(LAUNCHER_SCRIPT, FUNCTION)

    # Rename the second copy so both can coexist in one session.  The rename is
    # textual and exact, so a script that loses the function fails loudly here
    # rather than silently testing one copy twice.
    renamed = launcher_copy.replace(f"function {FUNCTION} {{", f"function {FUNCTION}_Launcher {{", 1)
    assert renamed != launcher_copy, f"{LAUNCHER_SCRIPT.name} no longer defines {FUNCTION}"

    probe = tmp_path / "verdict-probe.ps1"
    probe.write_text(
        start_copy
        + "\n\n"
        + renamed
        + "\n\n"
        + r"""
# The error predicate the two scripts share.  It is reproduced rather than
# imported because it lives in a different function in each script and the
# point of this test is the verdict logic, not this guard.
function Test-PayloadError {
    param([Parameter(Mandatory = $true)]$Payload)
    if ($null -eq $Payload) { return $true }
    if ($Payload -is [string]) { return $true }
    $n = @($Payload.PSObject.Properties.Name)
    if (-not ($n -contains 'error')) { return $false }
    return $null -ne $Payload.error
}

$payload = '%s' | ConvertFrom-Json
[ordered]@{
    start    = %s -Payload $payload
    launcher = %s_Launcher -Payload $payload
} | ConvertTo-Json -Compress
"""
        % (json.dumps(payload), FUNCTION, FUNCTION),
        encoding="utf-8",
    )

    result = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr[:400]}"
    return json.loads(result.stdout)


@pytest.mark.parametrize(("payload", "expected"), PAYLOADS, ids=range(len(PAYLOADS)))
def test_both_launcher_copies_reach_the_right_verdict(tmp_path, payload, expected):
    """The verdict is right in both copies -- not merely the same in both."""
    verdicts = _verdicts(tmp_path, payload)

    assert verdicts["start"] == expected, (
        f"Start-UniPad.ps1 said {verdicts['start']!r} for {payload}, expected {expected!r}"
    )
    assert verdicts["launcher"] == expected, (
        f"UniPad-Launcher.ps1 said {verdicts['launcher']!r} for {payload}, "
        f"expected {expected!r}"
    )


def test_the_two_copies_never_disagree(tmp_path):
    """The defect class itself: two copies drifting apart on the same payload.

    Every payload in the matrix is fed to both copies and the answers compared.
    A copy that goes silent -- returns ``indeterminate`` for everything, which
    is exactly what the missing ``$names`` assignment did -- fails here even if
    the other copy happens to be right, because they no longer agree.
    """
    disagreements = []
    for index, (payload, _expected) in enumerate(PAYLOADS):
        verdicts = _verdicts(tmp_path, payload)
        if verdicts["start"] != verdicts["launcher"]:
            disagreements.append((index, payload, verdicts))

    assert not disagreements, "the launcher copies disagree: " + repr(disagreements)


def test_a_silent_copy_is_caught_not_just_a_divergent_one(tmp_path):
    """A copy that answers ``indeterminate`` for *everything* is a defect even
    if the other copy agrees with it by being equally silent.

    This is the regression guard for the specific bug: with ``$names`` unset,
    both loops short-circuit and the only reachable answers are ``failure``
    (from the error predicate) and ``indeterminate``.  A payload that must read
    as ``success`` therefore catches it.
    """
    silent = [i for i, (p, e) in enumerate(PAYLOADS) if e == "success"]
    assert silent, "the matrix must contain success cases or this proves nothing"

    for index in silent:
        payload, expected = PAYLOADS[index]
        verdicts = _verdicts(tmp_path, payload)
        assert verdicts["start"] == "success", (
            f"Start-UniPad.ps1 cannot report success for {payload}: "
            f"it said {verdicts['start']!r}.  This is the shape of the "
            "missing-$names bug -- check that Get-ControllerVerdict collects "
            "the payload's property names before the -contains loops."
        )
