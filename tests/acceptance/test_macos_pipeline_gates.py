"""The macOS pipeline's gates, run for real -- the four the mutation review left open.

Why this file exists
--------------------
``rev-mutation`` applied 15 mutations to the delivered macOS build pipeline and
reported six survivors: the whole suite stayed green while the behaviour was
broken.  Five of those six are closed here.  A test that still passes after the
code is broken is not evidence, so each one of these tests was written by
breaking the thing it names and watching an earlier attempt stay green.

The five, and the mutation each one catches:

1. ``scripts/macos/build-macos.sh`` refuses an ``http(s)`` ``--ipsw`` with
   ``network_refused``.  Deleting the whole guard changed nothing observable:
   the file-existence check further down still refused the URL, as
   ``config_invalid``.  A caller reading the machine-readable code therefore
   could not tell "this script will not touch the network" from "you typo'd a
   path", and PRD 8.1's no-network policy had no witness.

2. ``.github/workflows/build-macos.yml``'s staging step denies a filename that
   says it is a full system image or private material -- even when the role
   table or an ``artifact-roles.json`` would otherwise allowlist it.  Removing
   the pattern check left every staging test passing, because ``kernel.dmg``
   is a *kernel* by name and a *system image* by extension, and the name table
   alone was happy to upload it.  PRD 8.2 step 4 is default-deny: an
   unrecognised file is excluded and recorded, never uploaded.

3. The workflow's "Validate inputs against sources.lock.json" step fails the
   job when a **frozen** lock's commits disagree with the dispatch inputs.  No
   test had ever loaded that step's Python at all -- the file's own
   ``_step_python`` helper only knew the acquisition step by name -- so the
   frozen branch could be deleted silently.

4. The script's STATUS header claimed "the refusals and the manifest shape are
   proven ... by tests/acceptance/test_p0p1_gate_main.py".  Nothing in the
   suite reads ``stage-result.json`` or ``artifact-manifest.json``.  The header
   now names exactly which test proves what and says plainly what is left
   unproven, and two tests below keep it honest.

5. The second build pass decides ``root_kernel=true`` by grepping its own log
   for a literal that only the kernel stage emits -- and it reaches it through a
   shell variable, so the literal appears in the text of NEITHER file and
   ``grep -rn`` cannot see the coupling at all.  Rewording the log line, or the
   workflow's pattern, left every run reporting the wrong verdict beside a
   ``::warning::`` while the kernel was built correctly.  The test runs the real
   script both ways and asks the workflow's own pattern of each pass's log, so
   it holds in both directions and carries no literal of its own to drift.

6. The material delivery upload named two files by hand -- the seed's ciphertext
   and the manifest -- while ``material_manifest --verify`` refuses a root
   missing any of the six roles the manifest names.  A run that built everything
   correctly would deliver a seed the host could not verify, because the kernel,
   the device tree, the trust bundle, the boot config and the donor kernelcache
   were left in the public artifact the download never asked for.  The upload
   now ships the whole ``build-output/private/`` directory and the test asserts
   both halves of that: the path is the directory and not a list of names, and
   the staging loop is manifest-driven so a role added later travels without a
   second list to remember.

What is still unproven here
---------------------------
Everything that needs a macOS host: the Apple toolchain, a real clone from
github.com, and the contents of the two files this script writes.  The tests
here exercise the refusals, not the build.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# The helpers this file reuses rather than reinvents.  test_build_script_gates
# owns the "run the real script under a shim python3" machinery -- the script
# resolves its interpreter with `command -v python3` at load time and ignores a
# PY environment variable, so PATH is the only way to hand it one -- and
# test_p0p1_gate_main owns the "extract a step's Python verbatim from the YAML"
# machinery, and test_os_root_chain owns the workspace in which the owner root
# chain is either on disk or not.  All three are imported, not copied: a second
# copy of any of them is the divergence class this repository keeps hitting.
from acceptance.test_build_script_gates import (
    _error_code,
    _error_message,
    _run,
    _write_lock,
)
from acceptance.test_os_root_chain import (
    _bash,
    _python_on_path,
    _scratch,
)
from acceptance.test_p0p1_gate_main import (
    LOCK_PATH,
    STAGING_STEP,
    _step,
    _step_python,
)

pytestmark = pytest.mark.acceptance

ROOT = Path(__file__).resolve().parents[2]

#: A mutation harness's leftover marker: the word immediately after a ``#``, in
#: the upper case the harness used.  Deliberately NOT case-insensitive and
#: deliberately NOT a bare word search -- see the docstring of
#: :func:`test_no_mutation_markers_survive_in_the_delivered_pipeline` for the
#: prose that a bare search flagged and why that had to be fixed rather than
#: the prose deleted.
_MUTATION_MARKER = re.compile(r"#\s*(?:MUTATION|MUTANT)\b")
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "build-macos.yml"

#: The step whose frozen-lock branch had no witness at all.
INPUTS_STEP = "Validate inputs against sources.lock.json"

#: A 40-hex value that is deliberately NOT the repository's own pin.
OTHER_COMMIT = "a" * 40


# ---------------------------------------------------------------------------
# 1. The script refuses to fetch an IPSW over the network
# ---------------------------------------------------------------------------


def test_a_url_ipsw_is_refused_as_network_access(tmp_path: Path) -> None:
    """``--ipsw https://...`` must be ``network_refused``, not any other code.

    The driver's own default for ``board.toml [ipsw]`` is an https URL fetched
    with HTTP range requests, so this script's whole no-network policy
    (PRD 8.1) rests on refusing one.  Deleting the guard does not delete the
    refusal -- the file-existence check below it still rejects the URL, as
    ``config_invalid`` -- so a caller cannot tell a policy refusal from a typo.
    Asserting the *code* and the *message* is what makes the guard load-bearing.
    """
    lock = _write_lock(tmp_path, frozen=False)
    result = _run(tmp_path, lock, "--ipsw", "https://example.invalid/system.ipsw")

    assert _error_code(result) == "network_refused", (
        "a URL --ipsw is not refused as network access, so the no-network "
        f"policy has no witness:\n{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no network access" in _error_message(result), _error_message(result)
    # The refusal must name the value it refused, or an operator cannot tell
    # which of several file inputs was the URL.
    assert "example.invalid" in _error_message(result), _error_message(result)


def test_a_local_ipsw_file_is_not_refused_as_network_access(tmp_path: Path) -> None:
    """The guard is about URLs, not about the option.

    Without this the refusal could be satisfied by refusing every ``--ipsw``,
    which would break the devicetree stage that legitimately decodes a local
    IPSW.  A local file must get past this guard and stop somewhere later (the
    Apple tools are genuinely absent on this host).
    """
    local = tmp_path / "system.ipsw"
    local.write_bytes(b"not really an IPSW\n")
    lock = _write_lock(tmp_path, frozen=False)
    result = _run(tmp_path, lock, "--ipsw", local.as_posix())

    assert _error_code(result) != "network_refused", result.stdout + result.stderr
    assert "no network access" not in _error_message(result), _error_message(result)


# ---------------------------------------------------------------------------
# 2. The staging step's default-deny for system-image / private filenames
# ---------------------------------------------------------------------------


def _build_output(tmp_path: Path, *names: str) -> Path:
    """A build-output directory holding the named files, none of them empty."""
    out = tmp_path / "build-output"
    out.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"payload bytes\n")
    return out


def _run_staging_step(tmp_path: Path) -> int:
    """Execute the staging step's own Python, exactly as the runner does.

    In-process rather than through ``subprocess``: the step is a script with
    top-level code and no ``main()``, and this host's interpreter cannot be
    spawned reliably (``sys.executable`` resolves to a wrapper whose child dies
    with 0xC0000142 under fork pressure, which made this test flaky for reasons
    that had nothing to do with the code under test).  ``os.environ`` is swapped
    for the step's inputs and restored whatever happens, so the step reads the
    same environment a runner would give it and nothing leaks into the next test.
    """
    env = {
        "BUILD_OUTPUT_DIR": (tmp_path / "build-output").as_posix(),
        "STAGE_DIR": (tmp_path / "staged").as_posix(),
        "BUILD_LOG": (tmp_path / "macos-build.log").as_posix(),
        "REPORT_DIR": (tmp_path / "build").as_posix(),
        "EXPERIMENT_ID": "coverage-gap",
        "STAGE": "M0",
        "PAYLOAD_TARGET": "tcg",
        "UNIXNU_COMMIT": OTHER_COMMIT,
        "DEVICE_COMMIT": "b" * 40,
        "RUN_ID": "1",
        "RUN_ATTEMPT": "1",
        "MAX_FILE_BYTES": "2147483648",
    }
    (tmp_path / "macos-build.log").write_bytes(b"build log\n")
    return _execute_step(STAGING_STEP, env)


def _execute_step(name: str, env: dict[str, str]) -> int:
    """Run a workflow step's extracted Python with ``env`` and return its code.

    The environment is *replaced*, not merged: a step must not be able to read a
    value the runner would not have given it.  ``PATH`` (and ``SystemRoot`` on
    Windows, without which nothing execs) is carried over because a runner has
    one and the input-validation step really does run ``git rev-parse``.
    """
    source = _step_python(name)
    saved = os.environ.copy()
    inherited = {
        key: value
        for key, value in saved.items()
        if key in ("PATH", "Path", "SystemRoot", "TEMP", "TMP")
    }
    try:
        os.environ.clear()
        os.environ.update(inherited)
        os.environ.update(env)
        code = compile(source, f"{name} (build-macos.yml heredoc)", "exec")
        try:
            exec(code, {"__name__": "unipad_workflow_step_under_test"})  # noqa: S102
        except SystemExit as stop:
            return int(stop.code) if stop.code is not None else 0
        return 0
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _staging_report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "build" / "staging-report.json").read_text(encoding="utf-8"))


def _staged_manifest(tmp_path: Path) -> dict:
    path = tmp_path / "staged" / "artifact-manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_system_image_filename_is_excluded_and_recorded_never_staged(tmp_path: Path) -> None:
    """PRD 8.2 step 4: a full system image is never uploaded, even named as a kernel.

    ``kernel.dmg`` is the case the name table alone cannot see: it contains
    "kernel", so ``role_from_table`` allowlists it, and it is a disk image, so
    it is a whole system.  Removing the pattern check left this staged and
    hashed with no complaint anywhere, which is the survivor this closes.
    """
    _build_output(tmp_path, "kernel.macho", "kernel.dmg", "firmware.p12")
    result = _run_staging_step(tmp_path)

    assert result == 0, (
        "the staging step failed on a build-output directory that has one "
        f"allowlisted payload; exit={result}"
    )
    excluded = {item["path"]: item for item in _staging_report(tmp_path)["excluded"]}
    for name in ("kernel.dmg", "firmware.p12"):
        assert name in excluded, (
            f"{name} was not excluded: a system-image or private-data filename "
            f"reached the upload set\nexcluded={sorted(excluded)}"
        )
        assert "system-image/private-data" in excluded[name]["reason"], excluded[name]
        assert not (tmp_path / "staged" / name).exists(), f"{name} was staged"
    # The denial is recorded in the manifest too, so it is auditable after the
    # run rather than only visible in the step's log.
    manifest_excluded = {item["path"] for item in _staged_manifest(tmp_path)["excluded"]}
    assert {"kernel.dmg", "firmware.p12"} <= manifest_excluded, manifest_excluded
    # ...and the build still delivered what it was allowed to.
    staged = {item["name"] for item in _staged_manifest(tmp_path)["artifacts"]}
    assert "kernel.macho" in staged, staged
    assert not ({"kernel.dmg", "firmware.p12"} & staged), staged


def test_an_allowlisted_role_cannot_smuggle_a_system_image_past_the_name_table(
    tmp_path: Path,
) -> None:
    """Default-deny is the last word, not the first.

    ``artifact-roles.json`` is the build script's own way of saying what a file
    is, and it is read *before* the name table.  A role of "firmware" for
    ``firmware.img`` therefore passes every role check and is still a whole
    disk image.  If the pattern check only ran when the role was unknown, this
    file would be uploaded -- which is exactly the bypass the check exists to
    close.
    """
    _build_output(tmp_path, "kernel.macho", "firmware.img")
    roles = tmp_path / "build-output" / "artifact-roles.json"
    roles.write_text(json.dumps({"firmware.img": "firmware"}), encoding="utf-8")
    result = _run_staging_step(tmp_path)

    assert result == 0, (
        "the staging step failed with an artifact-roles.json present; "
        f"exit={result}"
    )
    excluded = {item["path"]: item for item in _staging_report(tmp_path)["excluded"]}
    assert "firmware.img" in excluded, (
        f"an explicitly allowlisted role smuggled a disk image past the name "
        f"table\nexcluded={sorted(excluded)}"
    )
    assert excluded["firmware.img"]["role"] == "firmware", excluded["firmware.img"]
    assert not (tmp_path / "staged" / "firmware.img").exists()


def test_an_unrecognised_filename_is_excluded_and_recorded(tmp_path: Path) -> None:
    """The other half of default-deny: a file nobody named is never uploaded.

    This is the branch that was already covered, asserted here so that the
    pattern check above cannot be "fixed" by deleting the unknown-role refusal
    instead: both must hold at once.
    """
    _build_output(tmp_path, "kernel.macho", "mystery.bin")
    result = _run_staging_step(tmp_path)

    assert result == 0, f"the staging step failed on an ordinary build output; exit={result}"
    excluded = {item["path"]: item for item in _staging_report(tmp_path)["excluded"]}
    assert "mystery.bin" in excluded, sorted(excluded)
    assert "allowlist" in excluded["mystery.bin"]["reason"], excluded["mystery.bin"]
    assert not (tmp_path / "staged" / "mystery.bin").exists()


# ---------------------------------------------------------------------------
# 3. The frozen-lock commit mismatch is a hard failure
# ---------------------------------------------------------------------------


def _lock_from_repository() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def _run_inputs_step(tmp_path: Path, lock: dict) -> int:
    """Execute the workflow's own input-validation Python against ``lock``."""
    lock_path = tmp_path / "sources.lock.json"
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    env = {
        "LOCK_PATH": lock_path.as_posix(),
        "REPORT_DIR": (tmp_path / "build").as_posix(),
        "EXPERIMENT_ID": "coverage-gap",
        # The dispatch request: a commit that is NOT the one the lock pins.
        "UNIXNU_COMMIT": OTHER_COMMIT,
        "DEVICE_COMMIT": lock["device_repo"]["commit"],
        "STAGE": "M0",
        "SYSTEM_BUILD": "",
        "PAYLOAD_TARGET": "tcg",
    }
    return _execute_step(INPUTS_STEP, env)


def _inputs_report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "build" / "input-validation.json").read_text(encoding="utf-8"))


def test_a_frozen_lock_that_disagrees_with_the_request_is_a_hard_failure(tmp_path: Path) -> None:
    """A frozen lock is the authority; a request that contradicts it must fail.

    ``frozen`` is a *declaration* that the pins are final, and this step is the
    only place the workflow enforces what that declaration means.  No test had
    ever executed this step's Python, and none had ever built a lock with
    ``frozen: true``, so the whole branch -- the comparison and the
    "frozen but a commit is null" refusal -- could be deleted without a single
    test noticing.
    """
    lock = _lock_from_repository()
    lock["frozen"] = True
    lock["unixnu"]["commit"] = "c" * 40
    result = _run_inputs_step(tmp_path, lock)

    assert result == 1, (
        "a frozen lock whose unixnu.commit disagrees with the dispatch input "
        f"did not fail the job; exit={result}"
    )
    report = _inputs_report(tmp_path)
    assert report["status"] == "fail", report
    assert report["lock"]["frozen"] is True, report["lock"]
    assert any(
        "unixnu_commit" in problem and "sources.lock.json" in problem
        for problem in report["problems"]
    ), report["problems"]


def test_a_frozen_lock_with_a_null_commit_is_a_hard_failure(tmp_path: Path) -> None:
    """The second half of the frozen branch: a frozen lock with no pin is a lie."""
    lock = _lock_from_repository()
    lock["frozen"] = True
    lock["unixnu"]["commit"] = None
    result = _run_inputs_step(tmp_path, lock)

    assert result == 1, (
        "a frozen lock with a null commit did not fail the job; "
        f"exit={result}"
    )
    report = _inputs_report(tmp_path)
    assert any("null" in problem for problem in report["problems"]), report["problems"]


def test_the_same_disagreement_in_an_unfrozen_lock_is_only_a_warning(tmp_path: Path) -> None:
    """The frozen flag is what makes the mismatch fatal -- and nothing else.

    Without this the test above could be satisfied by a step that fails on any
    commit mismatch, which would break the only path to a first build: while
    the lock is unfrozen the dispatch inputs ARE the freeze candidates, and
    refusing them is the deadlock
    ``test_build_script_gates.py`` measured on the shell side.
    """
    lock = _lock_from_repository()
    lock["frozen"] = False
    result = _run_inputs_step(tmp_path, lock)

    assert result == 0, (
        "an unfrozen lock whose commit disagrees with the dispatch input was "
        f"refused, which is the deadlock the shell side already measured; "
        f"exit={result}"
    )
    report = _inputs_report(tmp_path)
    assert report["status"] == "pass", report
    assert any("NOT FROZEN" in warning for warning in report["warnings"]), report["warnings"]


# ---------------------------------------------------------------------------
# 4. The script's STATUS header must not claim more than the suite proves
# ---------------------------------------------------------------------------


def _script_header() -> str:
    """The comment block above ``set -euo pipefail``: everything but the code."""
    text = SCRIPT.read_text(encoding="utf-8")
    head, separator, _rest = text.partition("set -euo pipefail")
    assert separator, "build-macos.sh no longer contains `set -euo pipefail`"
    return head


def _status_section() -> str:
    header = _script_header()
    start = header.find("# STATUS")
    assert start != -1, "the STATUS section was removed from build-macos.sh's header"
    return header[start:]


def _cited_test_files() -> set[str]:
    """Every ``tests/...`` path the STATUS section names, punctuation stripped."""
    return {
        token.rstrip(".,;:)")
        for token in _status_section().split()
        if token.startswith("tests/")
    }


def test_the_header_cites_test_files_that_exist() -> None:
    """A header that points at a test file which was renamed or deleted is a
    lie no run will ever contradict -- the file is prose, so nothing checks it
    until someone reads it and believes it."""
    missing = sorted(path for path in _cited_test_files() if not (ROOT / path).is_file())
    assert not missing, (
        "the STATUS section cites test files that do not exist: "
        f"{missing}. Either the file moved -- fix the citation -- or the claim "
        "was never true -- remove it."
    )


def test_the_header_does_not_claim_the_manifest_is_proven() -> None:
    """The lie this round started from.

    The old STATUS line read "the argument construction, the refusals and the
    manifest shape are proven ... by tests/acceptance/test_p0p1_gate_main.py".
    Nothing in the suite reads ``stage-result.json`` or
    ``artifact-manifest.json`` -- the workflow's staging step builds its own
    manifest from the build output, and the script's own writer is never read
    back by anything.  A header that overclaims is worse than one that
    underclaims, because it is the reason nobody looks.
    """
    status = _status_section()
    assert "manifest shape" not in status, (
        "the STATUS section still claims the manifest shape is proven; no test "
        "reads stage-result.json or artifact-manifest.json"
    )


def test_the_header_states_what_the_suite_cannot_prove() -> None:
    """The honest half must be present, not merely the false half absent."""
    status = _status_section().lower()
    for claim in ("xcodebuild", "artifact-manifest.json"):
        assert claim.lower() in status, (
            f"the STATUS section no longer names {claim!r}: an operator reading "
            "it could believe this host proved something it cannot"
        )


def test_the_header_names_the_tests_that_prove_what_it_claims() -> None:
    """Each proof claim points at a real file, and the set is not a guess.

    The list below is what the suite actually exercises for this script and for
    the workflow steps that call it.  A new test that proves something belongs
    here; a removed one must be deleted from the header too, and this test
    fails if the header and the suite drift apart in either direction.
    """
    cited = _cited_test_files()
    expected = {
        "tests/acceptance/test_build_script_gates.py",
        "tests/acceptance/test_macos_pipeline_gates.py",
        "tests/acceptance/test_p0p1_gate_main.py",
        "tests/acceptance/test_payload_font_header.py",
        "tests/acceptance/test_shell_script_syntax.py",
        "tests/acceptance/test_workflow_python_syntax.py",
    }
    assert cited == expected, (
        "the STATUS section's proof list and the suite disagree.\n"
        f"cited but absent: {sorted(cited - expected)}\n"
        f"exists but uncited: {sorted(expected - cited)}\n"
        "Update scripts/macos/build-macos.sh's STATUS section, or this test, "
        "but do not leave one naming a file that does not prove anything."
    )


# ---------------------------------------------------------------------------
# 5. The tree carries no mutation markers
# ---------------------------------------------------------------------------


def test_no_mutation_markers_survive_in_the_delivered_pipeline(tmp_path: Path) -> None:
    """A ``# MUTATION`` comment is a harness artefact, never a shipped one.

    The mutation reviewer reported that the live ``build-macos.yml`` had been
    edited in place with a ``# MUTATION: default-deny ... removed`` marker left
    above the hole it made.  That is a copy-based-harness violation: the tree
    claims a rule exists that does not, and the only thing distinguishing the
    delivered workflow from a broken one is a comment nobody greps for.  This
    test makes the leftover loud instead of invisible.

    Why the pattern is what it is
    -----------------------------
    It first matched the bare word ``MUTATION`` anywhere on a line, which flagged
    ``# THE FOUR DEFECTS THIS SECTION WAS WRITTEN AGAINST, AND THE MUTATION EACH
    ONE`` -- English prose about the project's own 对质 rule.  A guard that cries
    wolf on a comment explaining mutation testing gets deleted or ignored, and
    then the real marker ships.  So it now requires the marker *convention*: the
    word immediately after a ``#``, in the upper case the harness used.  That
    matches both shapes actually left in this tree (``# MUTATION: <what was
    removed>`` and a trailing ``# MUTATION``) and neither ``# mutation proof:``
    nor the prose above.

    The self-test below is the load-bearing half: narrowing a pattern is exactly
    how a guard stops working, so this asserts the narrowed form still catches a
    real marker rather than trusting that it does.
    """
    offenders: list[str] = []
    # This file names the marker it forbids, so it cannot scan itself; every
    # other file under the delivered tree can and must be clean.
    scanned = (
        ROOT / ".github",
        ROOT / "scripts",
        ROOT / "src",
        ROOT / "tests",
    )
    for directory in scanned:
        if not directory.is_dir():  # pragma: no cover - the repo always ships these
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path == Path(__file__):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - unreadable file is a different problem
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if _MUTATION_MARKER.search(line):
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    assert not offenders, (
        "mutation markers were left in the tree: "
        f"{offenders}. A mutation harness must work on a copy; a marker in the "
        "delivered file means the mutation was never reverted."
    )


@pytest.mark.parametrize(
    ("line", "is_a_marker"),
    [
        # The two shapes actually left in this tree.
        ("# MUTATION: default-deny for system-image/private-data filenames removed", True),
        ("        if False:  # MUTATION", True),
        ("    reason = None  # MUTANT", True),
        # Prose that must NOT be flagged, or the guard gets ignored.
        ("# THE FOUR DEFECTS THIS SECTION WAS WRITTEN AGAINST, AND THE MUTATION EACH", False),
        ("# mutation proof: reverted the fix; the test failed. Restored.", False),
        ("# Mutation proof: two of three tests killed the mutant.", False),
        ("    # The mutation reviewer reported that the live yml had been", False),
        ('    "a mutation harness must work on a copy"', False),
        ("    mutation_count = 0", False),
    ],
)
def test_the_marker_pattern_separates_a_marker_from_prose(line: str, is_a_marker: bool) -> None:
    """The narrowed pattern still catches a marker and still spares English.

    Parametrised over both directions on purpose: a pattern narrowed until it
    matches nothing is the failure mode of fixing a false positive, and it would
    otherwise be invisible.
    """
    assert bool(_MUTATION_MARKER.search(line)) is is_a_marker, line


def test_the_guard_would_catch_a_real_leftover(tmp_path: Path) -> None:
    """End-to-end: plant the exact marker that was really left, and be caught.

    The pattern test above proves the regex; this proves the *scan* reaches a
    file and reports it.  A guard whose glob silently stopped covering ``tests/``
    would pass both other tests.
    """
    planted = tmp_path / "delivered.py"
    planted.write_text(
        "x = 1\n# MUTATION: the guard below was deleted\ny = 2\n", encoding="utf-8"
    )
    found = [
        f"{path.name}:{lineno}"
        for path in (planted,)
        for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1)
        if _MUTATION_MARKER.search(line)
    ]
    assert found == ["delivered.py:2"], found


# ---------------------------------------------------------------------------
# 5. The second pass's root_kernel verdict and the kernel stage's log line
# ---------------------------------------------------------------------------

#: The step that decides, out of the second pass's own build log, whether the
#: kernel it built came from ``qemu_root_defconfig``.
ROOT_BUILD_STEP = "Run macOS build again (qemu_root_defconfig, PRD 8.1 root chain)"


def _root_kernel_pattern() -> str:
    """The pattern that step greps its build log for, read out of the workflow.

    Read rather than restated.  A literal in this file would be a third spelling
    of one fact -- the shape this whole file exists to police, and the shape
    ``step_build_payload`` polices for ``FONT_RELATIVE`` against the lock's
    ``uefi_sheng.font_header.path``.
    """
    run = _step(ROOT_BUILD_STEP)["run"]
    patterns = re.findall(r"""grep -q ['"]([^'"]+)['"]""", run)
    assert len(patterns) == 1, (patterns, run)
    return patterns[0]


def _root_pass(tmp_path: Path, *, chain: bool) -> subprocess.CompletedProcess:
    """The second build pass, run for real, with or without the owner root chain.

    ``test_os_root_chain`` owns the workspace -- which three files
    ``root_chain_present()`` checks, and where they sit relative to ``tree_dir()``
    -- and importing it rather than restating it is what stops this test drifting
    away from that one.  The single addition is ``--min-free-gb 0``: the script's
    ``DEFAULT_MIN_FREE_GB=8`` gate measures the host's free disk, not the
    behaviour under test, and on a host with ~5 GiB free it refuses the whole run
    before a stage is planned -- which would make this test fail everywhere and
    prove nothing about the coupling.
    """
    lock, device, out = _scratch(tmp_path, chain=chain)
    return subprocess.run(
        [
            _bash(),
            SCRIPT.as_posix(),
            "--stage",
            "M2",
            "--experiment",
            "root-kernel-grep",
            "--lock",
            lock.as_posix(),
            "--out",
            out.as_posix(),
            "--unixnu",
            (tmp_path / "ws" / "unixnu").as_posix(),
            "--device",
            device.as_posix(),
            "--min-free-gb",
            "0",
        ],
        capture_output=True,
        text=True,
        env=_python_on_path(tmp_path),
        cwd=str(tmp_path),
        timeout=600,
    )


def _defconfig_lines(text: str) -> list[str]:
    """The kernel stage's own account of which defconfig it chose, and why."""
    return [line for line in text.splitlines() if "kernel:" in line and "defconfig" in line]


def _grep_q(pattern: str, text: str) -> int:
    """``grep -q PATTERN`` over TEXT: the question the workflow asks of its log."""
    located = shutil.which("grep")
    if located is None:  # pragma: no cover - a host with bash has grep
        pytest.skip("no grep on this host, so the workflow's own test cannot be run")
    return subprocess.run([located, "-q", "--", pattern], input=text, text=True).returncode


def test_the_root_kernel_grep_and_the_kernel_stage_agree_in_both_directions(
    tmp_path: Path,
) -> None:
    """``root_kernel=true`` is only as good as the log line it is read from.

    That output is what tells a reader of the job whether the second pass built
    ``qemu_root_defconfig`` -- the kernel that mounts the owner-signed OS volume
    -- or quietly fell back to ``qemu_defconfig`` and stopped at "Failed to mount
    root device".  It is decided by grepping the pass's own log for a string only
    the kernel stage emits, so the two files share one fact with no common
    spelling: the workflow holds the literal, the script holds a shell variable
    that interpolates it.  ``grep -rn`` for the literal in the script finds
    nothing at all, so nothing in the repository can see the coupling.

    Reword the log line and every run reports ``root_kernel=false`` beside a
    ``::warning::`` while the kernel is built correctly; change the workflow's
    literal and the same happens in reverse.  Both directions are pinned here by
    running the real script both ways -- the chain on disk and off it -- and
    asking the workflow's own pattern of each run's log.  There is deliberately
    no literal in this test: the pattern comes from the workflow and the log from
    the script, so the assertion is the comparison rather than a third copy of
    the string.
    """
    pattern = _root_kernel_pattern()

    with_chain = _root_pass(tmp_path / "with-chain", chain=True)
    assert _defconfig_lines(with_chain.stdout), (
        "the kernel stage never chose a defconfig, so this test proves nothing "
        "about the coupling: " + with_chain.stdout
    )
    without_chain = _root_pass(tmp_path / "without-chain", chain=False)
    assert _defconfig_lines(without_chain.stdout), (
        "the kernel stage never chose a defconfig, so this test proves nothing "
        "about the coupling: " + without_chain.stdout
    )

    # The chain on disk: the stage switches to qemu_root_defconfig and says so,
    # and the workflow's grep must find it.  Were the stage never reached, both
    # runs would answer 1 and this is the assertion that fails -- so a green here
    # cannot be a vacuous one.
    assert _grep_q(pattern, with_chain.stdout) == 0, (
        f"the workflow greps {pattern!r} but the root-chain pass did not log it: "
        f"{_defconfig_lines(with_chain.stdout)}"
    )

    # ...and no chain: the board's own defconfig, which must NOT satisfy the same
    # grep.  This is the half that catches a log line naming the root defconfig
    # unconditionally, which would report a root kernel that was never built.
    assert _grep_q(pattern, without_chain.stdout) == 1, (
        f"the workflow greps {pattern!r} and a pass with no owner root chain "
        f"matched it anyway: {_defconfig_lines(without_chain.stdout)}"
    )


# ---------------------------------------------------------------------------
# 6. The material delivery ships the whole directory the manifest names
# ---------------------------------------------------------------------------


def test_the_material_delivery_ships_the_directory_not_a_list_of_names() -> None:
    """The delivery is a matched build, so it must arrive whole.

    This is the sixth survivor, and it was found by reading the two ends against
    each other rather than by a mutation.  The material manifest names six roles
    -- the kernel, the device tree, the trust bundle, the boot config, the donor
    kernelcache and the Data seed -- and ``material_manifest --verify`` refuses a
    root that is missing any of them except the two board-pinned IPSW members
    PRD 12 deletes.  The upload step named two of them by hand: the ciphertext
    and the manifest.  So the host would download the seed and its index and be
    told the kernel was absent, on a run that had built everything correctly --
    the same shape as the path bug in implementation-plan fact 89, where a
    consumer made an assumption about a producer's output instead of measuring
    it, and it cost the run its whole delivery.

    Shipping the directory is what closes it, and it closes it structurally
    rather than by enumeration: the staging loop is manifest-driven (it iterates
    ``manifest["artifacts"]``), so a role added later travels without anyone
    remembering a second list.  Both halves are asserted here, because either
    one alone leaves the other free to drift -- an upload of a directory nobody
    stages into is as empty as a stage nobody uploads.
    """
    step = _step("Deliver the encrypted material (private channel)")
    path = step["with"]["path"]
    assert isinstance(path, str), path
    # The directory, normalised.  A trailing slash is a spelling, not a contract.
    assert path.strip().rstrip("/") == "build-output/private", path
    # ...and NOT a list of names.  This is the assertion that fails on the
    # delivered version of this step, which listed the ciphertext and the
    # manifest and nothing else.
    assert "owner-data-1g.dbr.enc" not in path, path
    assert "material-manifest.json" not in path, path

    # The other half: the stage that fills the directory stages EVERY role the
    # manifest names, and it learns the roles from the manifest rather than from
    # a list of its own.
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'delivery="$(material_delivery_dir)"' in script, (
        "the material manifest stage no longer resolves its delivery directory "
        "through material_delivery_dir(), so this test's other half has nothing "
        "to check against"
    )
    assert 'cp "$path" "$delivery/$leaf"' in script, (
        "the staging loop no longer copies into the delivery directory"
    )
    assert 'for role, entry in sorted(manifest["artifacts"].items())' in script, (
        "the staging loop no longer iterates the manifest's own roles, so a role "
        "added to the manifest would not be staged into the delivery"
    )
    # And the directory it resolves to is the one that is shipped.
    assert 'material_delivery_dir() { printf \'%s\' "$OUT_DIR/private"; }' in script, (
        "material_delivery_dir() no longer names $OUT_DIR/private, so the stage "
        "and the upload would disagree about where the delivery is"
    )


def test_every_apple_derived_member_is_encrypted_not_only_the_seed() -> None:
    """The delivery is uploaded from a PUBLIC repo, so every Apple member is ciphertext.

    ``actions/upload-artifact`` on a public repository produces a publicly
    downloadable artifact -- ``gh repo view`` answers ``PUBLIC`` for this one --
    so "controlled private channel" is not a property the delivery step has.  The
    stage used to encrypt exactly one member: the Data seed.  The donor
    kernelcache is the *original Apple binary* board.toml [kernel.prelink] pins,
    and the trust bundle is the static and System trust caches the chain lifted
    out of the same IPSW.  Both are Apple material under PRD 12 and both would
    have travelled in the clear.

    The built artifacts are deliberately NOT encrypted, and that is asserted
    too: a delivery nobody can read without the key is not a verifiable
    delivery, which is the entire reason the manifest exists.
    """
    script = (ROOT / "scripts" / "macos" / "build-macos.sh").read_text(encoding="utf-8")
    start = script.find("local -a apple_derived=")
    assert start != -1, (
        "step_material_manifest no longer declares an apple_derived list, so "
        "either the encryption was rewritten or it encrypts a fixed set again; "
        "this file has to be rewritten with it rather than left asserting the "
        "shape of code that is gone"
    )
    block = script[start:script.index("return 0", start)]

    # (1) every Apple-derived member is in the list.
    for member in ("owner-data-1g.dbr", "kernelcache.decompressed",
                   "original-static-system.bundle"):
        assert f"'{member}'" in block, f"{member} is Apple-derived and is not encrypted"
    # (2) ...and the built artifacts are not, so the delivery stays readable.
    for built in ("kernel.macho", "kernel.macho.adt", "unixnu.toml"):
        assert f"'{built}'" not in block, (
            f"{built} is this project's own output; encrypting it would make the "
            "delivery unreadable without the key"
        )
    # (3) the list is iterated, not one file hardcoded: a member added later is
    #     encrypted because it is in the list, and the loop proves there is a
    #     loop rather than a second copy of the cipher invocation.
    assert 'for member in "${apple_derived[@]}"' in block, block
    assert block.count("-aes-256-gcm") == 1, (
        "the ciphertext is produced by more than one invocation, so the list is "
        "not what decides what gets encrypted"
    )
    # (3b) ...and it is invoked through the resolved OpenSSL 3 keg, not through
    #      bare `openssl`.  macOS's /usr/bin/openssl is LibreSSL, whose `enc` has
    #      no GCM, so `openssl enc -aes-256-gcm` fails there -- measured on run
    #      40392771419, whose delivery encryption failed for exactly that reason
    #      and left no line in the step log.  Asserting the variable rather than
    #      the word is what keeps a future edit from putting the bare command
    #      back: `bash -n` cannot tell the two apart and neither can a reviewer
    #      skimming for "openssl".
    assert '"$ossl" enc -aes-256-gcm' in block, block
    assert not re.search(r"(?<![$\w])openssl\s+enc\s", block), (
        "the encryption calls bare `openssl`, which on macOS is LibreSSL and has "
        "no GCM; use the keg openssl_bin() resolves"
    )
    # The failure is logged, not only recorded: ledger_add appends to an array
    # and prints nothing, so a refusal that lands only in the build report is one
    # the operator reading the step log cannot see.
    assert "could NOT be encrypted by" in block, block
    assert "${encrypt_error:-<no output>}" in block, block
    assert 'ossl="$(openssl_bin)"' in block, block
    # (4) the plaintext is deleted as it is encrypted, per member.
    assert 'rm -f "$delivery/$member"' in block, block
    # (5) no key is not a silent pass: the stage says so and uploads nothing.
    assert "MATERIAL_DELIVERY_KEY is unset" in block, block

    # (6) the other half: the runner's removal step deletes every one of them,
    #     not only the seed.  On a run WITH the key the stage already deleted
    #     each plaintext, so this is the belt over those braces.
    removal = _step("Remove the unencrypted material from the runner")["run"]
    for member in ("owner-data-1g.dbr", "kernelcache.decompressed",
                   "original-static-system.bundle"):
        assert member in removal, (
            f"the removal step no longer deletes {member}, so an unencrypted "
            "Apple-derived member would survive the job on a persistent runner"
        )


def test_the_delivery_directory_is_excluded_from_the_public_artifact() -> None:
    """The private channel is only private if the public one excludes it.

    The same directory is uploaded twice on a successful run -- once through the
    delivery step and once, if the staging step did not exclude it, through the
    public build artifact.  The exclusion is the pattern list, and PRD 12 does
    not get to rest on the two uploads happening to be ordered.
    """
    step = _step(STAGING_STEP)
    patterns = _step_python(STAGING_STEP)
    assert '"private/*"' in patterns, (
        "the staging step's default-deny patterns no longer exclude the "
        "delivery directory: " + patterns[:400]
    )
    assert '"*.enc"' in patterns, (
        "the staging step's default-deny patterns no longer exclude the "
        "ciphertext: " + patterns[:400]
    )
    # The step still exists and still has the patterns it claims to.
    assert step["name"] == STAGING_STEP, step
