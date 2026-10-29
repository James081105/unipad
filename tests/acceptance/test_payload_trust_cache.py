"""The payload stage must hand the driver the owner root chain's two outputs.

Why this file exists
--------------------
``unixnu build payload`` takes ``--trust-cache`` and ``--data-ramdisk``, and
``scripts/macos/build-macos.sh`` passed neither.  That is not a small omission,
and the reason is written in the pinned device repository itself, in the chain's
own module docstring (``vendor/UniXNU-qemu/scripts/build-os-root.py:26-27``)::

    build-original-system-trust.py: the original static and System trust caches
    as one bundle, the payload's --trust-cache. Without it AMFI refuses to run
    launchd (exit reason namespace 9, EXEC, code 8, SECURITY_POLICY).

and the same script's LAST line (``build-os-root.py:174``) is literally the
command it says to run next::

    unixnu build payload ... --trust-cache {trust} --data-ramdisk {seed}

So an ESP built without them is a volume that boots a kernel AMFI will not let
exec ``launchd``.  Every acceptance criterion downstream of "launchd is PID 1"
sits behind those two flags, and neither was passed anywhere in the workflow.

The shape this bug takes is the reason it survived: the second build pass
rebuilds the ESP around the kernel that can finally mount a root volume, so the
missing flags make the build look *more* finished while making the guest *less*
able to start.  A gap that hides inside apparent progress is the one worth a
test.

What is proven here
-------------------
Two halves, because there are two places that could drop the flags and only one
of them is the script:

1. **The script.**  The region of ``step_build_payload`` that builds argv is
   *executed* in a real ``bash`` with a stubbed ``run_driver``, and the arguments
   the stub receives are asserted.  Not a substring: the defect this repo kept
   hitting (fact 54c, run 40238050858) is that a substring assertion still passes
   after the code behind it is broken.  Executing the region is what makes the
   teeth real.
2. **The workflow.**  The second build pass -- the only pass that runs after the
   chain and so the only one that *has* these files -- is read out of the YAML
   and its shell is executed far enough to show the two flags reaching
   ``build-macos.sh``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "build-macos.yml"

#: The chain's own final instruction, quoted so this file cannot drift from the
#: thing it is checking.  ``build-os-root.py:174``.
CHAIN_COMMAND = (
    "unixnu build payload ... --trust-cache {trust} --data-ramdisk {seed}"
)
#: What the trust cache is for, in the device repository's own words.
#: ``build-os-root.py:26-27``.
AMFI_REASON = "Without it AMFI refuses to run launchd"

SECOND_PASS = "Run macOS build again (qemu_root_defconfig, PRD 8.1 root chain)"

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(
        shutil.which("bash") is None, reason="bash is required to run the script's own code"
    ),
]


def _bash() -> str:
    """A bash that actually starts, or a named skip.

    The same helper ``test_os_root_chain.py`` needs, for the same measured
    reason: on this host ``shutil.which("bash")`` can resolve to
    ``C:\\Windows\\System32\\bash.exe``, the WSL launcher, whose distribution
    cannot start -- so every invocation dies before the script under test sees a
    byte of input.  Resolving a name is not the same as running it, and a test
    that skipped the distinction would report "the build script is broken" when
    nothing about the build script was ever exercised.
    """
    for candidate in ("bash", "bash.exe"):
        located = shutil.which(candidate)
        if not located:
            continue
        try:
            probe = subprocess.run(
                [located, "--version"], capture_output=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            continue
        if probe.returncode == 0:
            return located
    pytest.skip(
        "no runnable bash on this host: every bash on PATH is a launcher whose "
        "distribution cannot start. These tests execute build-macos.sh's own "
        "code and need a working shell."
    )


def _lines() -> list[str]:
    return SCRIPT.read_text(encoding="utf-8").splitlines()


def _payload_argv_region() -> str:
    """The part of ``step_build_payload`` that builds argv and calls the driver.

    From ``local -a uefi_args=()`` -- the first thing the region needs that is
    not a variable set earlier in the function -- through the ``fi`` that closes
    the ``if ! ( ... run_driver ... )``.  Everything in between is argument
    construction and one driver call, which is exactly what is under test, and
    nothing in it depends on the parts of the function that need a macOS host
    (the tool checks, the seed derivation, the QEMU install).

    The end is found by walking forward from the ``run_driver`` line to the next
    ``fi`` at the function's own indentation.  Anchoring on the driver call
    rather than on a fixed line number is what keeps this from silently
    extracting the wrong block when the function grows.
    """
    lines = _lines()
    start = next(
        i for i, line in enumerate(lines) if line.strip() == "local -a uefi_args=()"
    )
    call = next(
        i
        for i in range(start, len(lines))
        if "run_driver" in lines[i] and "'build' 'payload'" in lines[i]
    )
    end = next(i for i in range(call, len(lines)) if lines[i] == "    fi")
    region = "\n".join(lines[start : end + 1])
    assert "root_args" in region, (
        "the extracted region does not mention root_args, so this test is not "
        "looking at the code that forwards the chain's outputs"
    )
    return region


#: Everything the extracted region touches that the surrounding function would
#: have set.  Spelled out rather than stubbed implicitly, so a variable the
#: region starts using is a NameError here rather than a silent empty string.
#:
#: The three variables the region reads from the caller come in from the
#: environment, in DOUBLE quotes.  Single quotes would be the obvious spelling
#: and they are wrong: ``X='${X:-}'`` assigns the seven characters ``${X:-}``,
#: and every assertion downstream then compares against a literal instead of a
#: path -- a harness that fails for a reason that has nothing to do with the
#: script, which is the sixth time this session a guard has gone red on its own
#: defect.
HARNESS_HEAD = """\
set -euo pipefail
name='build-payload'
DEVICE_DIR='/device'
UNIXNU_OUT='/out'
kernel='/kernel/kernel.macho'
seed='0123456789abcdef'
PLATFORM_SEED_SHA256='feedface'
OUT_DIR='/out'
TARGET='tcg'
UEFI_DIR="${UEFI_DIR:-}"
TRUST_CACHE_PATH="${TRUST_CACHE_PATH:-}"
DATA_RAMDISK_PATH="${DATA_RAMDISK_PATH:-}"
log() { printf 'LOG %s\\n' "$*"; }
ledger_add() { printf 'LEDGER %s\\n' "$*"; }
record_artifact() { :; }
verify_payload_image() { return 0; }
work_dir() { printf '/out/qemu'; }
shown_path() { printf '%s' "$1"; }
adt_qemu_path() { printf '/opt/homebrew/bin/qemu-system-aarch64'; }
run_driver() { printf 'ARG %s\\n' "$@"; }
payload_invocation() {
"""


def _run_region(trust: str = "", ramdisk: str = "", addon: str = "") -> tuple[int, str]:
    """Execute the argv-building region and return ``(status, output)``.

    A real ``bash``, because the region is shell: the empty-array expansions
    (``${root_args+"${root_args[@]}"}``) and the arithmetic ``(( root_count == 2
    ))`` are the things most likely to be wrong, and neither survives being read
    as text.  ``set -euo pipefail`` matches the script's own (line 164), so an
    unbound variable is an error here too -- which is how the bash-3.2
    ``${#arr[@]}`` trap would have been caught.

    The environment is INHERITED rather than replaced.  The first draft set
    ``PATH=/usr/bin:/bin`` -- which is right on a macOS runner and wrong here,
    where bash is an MSYS build that needs its own prefix on PATH to find
    ``printf``.  The result was a shell that died before reading a byte, and the
    failure read as a defect in the script.  Only the three variables the region
    reads are supplied.
    """
    script = (
        HARNESS_HEAD
        + textwrap.indent(_payload_argv_region(), "    ")
        + "\n}\npayload_invocation\n"
    )
    environment = dict(os.environ)
    environment.update(
        {"UEFI_DIR": "", "TRUST_CACHE_PATH": trust, "DATA_RAMDISK_PATH": ramdisk,
         "GPU_ADDON_PATH": addon}
    )
    result = subprocess.run(
        [_bash(), "-s"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=60,
    )
    return result.returncode, result.stdout + result.stderr


def test_the_stage_forwards_both_of_the_chains_outputs() -> None:
    """The whole point: the driver receives the two files the chain made.

    ``--trust-cache`` first, because that is the order the chain's own command
    spells them in and the order a reader of the driver's argv.log expects.
    """
    status, output = _run_region(
        trust="/tree/build/original-static-system-trust/original-static-system.bundle",
        ramdisk="/device/.cache/firmware/owner-data/owner-data-1g.dbr",
        addon="/tree/build/adreno-metal-plugin/gpu-addon.ngpu",
    )
    assert status == 0, output
    arguments = [line[4:] for line in output.splitlines() if line.startswith("ARG ")]
    assert arguments[:5] == [
        "build-payload",
        "build",
        "payload",
        "--kernel",
        "/kernel/kernel.macho",
    ], arguments
    assert "--trust-cache" in arguments, arguments
    assert "/tree/build/original-static-system-trust/original-static-system.bundle" in arguments, (
        arguments
    )
    assert "--data-ramdisk" in arguments, arguments
    assert "/device/.cache/firmware/owner-data/owner-data-1g.dbr" in arguments, arguments
    # The addon travels with them but is not one of them: it is forwarded with its
    # own value, and it does not take part in the chain's ordering assertion
    # below, because folding it into root_count would make that sentence about
    # something else.
    assert "--gpu-addon" in arguments, arguments
    assert "/tree/build/adreno-metal-plugin/gpu-addon.ngpu" in arguments, arguments
    # ...and in the chain's own order, so the argv.log reads like the command
    # build-os-root.py:174 prints.
    assert arguments.index("--trust-cache") < arguments.index("--data-ramdisk"), arguments


def test_a_pass_with_no_chain_sends_neither_and_says_so() -> None:
    """The first build pass, which runs before the chain exists.

    This is the state the flags must tolerate, and it is why they are optional
    arguments rather than required ones.  What must NOT happen is the flags being
    passed empty: ``--trust-cache ''`` would put a misleading argument in the
    driver's argv.log and would make ``ctx.option()`` fall through to the
    environment, which is the same trap ``--uefi`` already documents.

    The log line is asserted too, because a silent absence on the FIRST pass and
    a silent absence on the SECOND pass are indistinguishable in the build
    report -- and only the second one is the defect.
    """
    status, output = _run_region()
    assert status == 0, output
    arguments = [line[4:] for line in output.splitlines() if line.startswith("ARG ")]
    assert "--trust-cache" not in arguments, arguments
    assert "--data-ramdisk" not in arguments, arguments
    assert "--gpu-addon" not in arguments, arguments
    assert "AMFI will refuse launchd" in output, output
    # The addon's absence is logged for the same reason the chain's is: a silent
    # absence on the first pass and a silent absence on the second are the same
    # bytes, and only the second is the defect.  Its consequence is different and
    # is said in its own words rather than folded into the chain's sentence.
    assert "no GPU addon" in output, output


def _region_code(region: str) -> str:
    """The region with its full-line comments removed.

    Needed because the region *explains* the very idiom this guard forbids::

        # A counter, not ${#root_args[@]}: macOS ships /bin/bash 3.2 ...

    -- so a naive ``"${#root_args[@]}" not in region`` matches the comment that
    warns about it and reports a defect that is not there.  That is the same
    false-positive class as a guard whose message contains the forbidden word:
    red on its own text, not on the contract.  Only lines whose first non-space
    character is ``#`` are dropped, and the region has no ``#`` inside a string
    literal, so nothing real is removed with them.
    """
    return "\n".join(
        line for line in region.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_region_counts_into_a_variable_rather_than_reading_the_array_length() -> None:
    """The one assertion here that is a source check, and why it cannot be more.

    macOS ships ``/bin/bash`` 3.2 -- which this script says in three places of
    its own (lines 265, 2342, 3367) and which is why every other empty-array
    expansion in it uses the ``${arr+"${arr[@]}"}`` form.  On that bash,
    ``${#arr[@]}`` on an empty array under ``set -u`` is an unbound-variable
    error, so the count has to go into a variable.

    **This test cannot execute that.**  It runs on the host's bash, which is
    5.x, where the idiom works -- so the check is "the region does not spell the
    array length", read as source.  Driven through the 对质: reverting
    ``root_count`` to ``${#root_args[@]}`` leaves this test RED here only because
    of the source assertion, not because the harness reproduced bash 3.2.  A test
    that claimed to have caught it by running would be lying about what it
    measured, which is the exact failure mode the audit file exists to record.

    What IS executed, and what this test is really guarding, is the case that
    matters on every bash: with neither file supplied the region must run
    cleanly under ``set -u``.  That is
    ``test_a_pass_with_no_chain_sends_neither_and_says_so``, and it is the one
    that would catch a genuine unbound-variable error.
    """
    code = _region_code(_payload_argv_region())
    assert "${#root_args[@]}" not in code, (
        "the region reads the array length, which is the form macOS's bash 3.2 "
        "cannot do under set -u; count into a variable instead (the script says "
        "so at lines 265, 2342 and 3367)"
    )
    assert "root_count" in code, (
        "the region no longer counts the chain's outputs, so the log line that "
        "distinguishes 'both' from 'neither' has nothing to count"
    )


def test_the_workflows_second_pass_supplies_the_two_files() -> None:
    """The workflow half: the only pass that HAS the files must pass them.

    The chain runs between the two build passes, so only the second one can name
    these paths.  The step's own shell is extracted from the YAML and executed
    with the two files present, with ``build-macos.sh`` replaced by a stub that
    prints its argv -- so what is asserted is what the script would actually
    receive, not what the YAML happens to contain.
    """
    step = _second_pass_shell()
    assert "--trust-cache" in step, (
        "the second build pass does not pass --trust-cache, so the ESP it "
        "rebuilds cannot exec launchd (build-os-root.py:26-27)"
    )
    assert "--data-ramdisk" in step, (
        "the second build pass does not pass --data-ramdisk, so the Data volume "
        "the chain packed is not in the ESP"
    )

    status, output = _run_second_pass(step, trust=True, ramdisk=True)
    assert status == 0, output
    assert "--trust-cache" in output, output
    assert "original-static-system.bundle" in output, output
    assert "--data-ramdisk" in output, output
    assert "owner-data-1g.dbr" in output, output


def test_the_second_pass_says_so_when_the_chain_produced_nothing(tmp_path: Path) -> None:
    """A missing file is a warning, not a silently shorter argv.

    The step is gated on ``os_root_status == 'pass'``, so this should not happen
    -- but a glob that expands to nothing and a flag that is quietly omitted
    produce the same dead ESP, and only one of them says anything.  The warning
    is the difference.
    """
    step = _second_pass_shell()
    status, output = _run_second_pass(step, trust=False, ramdisk=False)
    assert status == 0, output
    assert "::warning::" in output, output
    assert "--trust-cache" not in output.split("build-macos.sh", 1)[-1], output


def _workflow() -> Any:
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover - depends on the environment
        pytest.skip("PyYAML is required to parse build-macos.yml")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _second_pass_shell() -> str:
    for step in _workflow()["jobs"]["build"]["steps"]:
        if step.get("name") == SECOND_PASS:
            return step["run"]
    raise AssertionError(f"{WORKFLOW.name} has no step named {SECOND_PASS!r}")


def _run_second_pass(step: str, *, trust: bool, ramdisk: bool) -> tuple[int, str]:
    """Execute the second pass's shell far enough to see the argv it builds.

    The step's own ``bash scripts/macos/build-macos.sh`` is replaced by a stub on
    PATH that prints its arguments, and ``tee`` is stubbed too because the real
    one needs the log file the step never creates here.  Everything before the
    invocation -- the glob for the tree, the two ``[ -f ... ]`` checks -- is the
    step's own code and is what is under test.

    The step's shell is run VERBATIM.  The first draft rewrote the two paths
    inside it, which quietly turned the test into a test of the rewrite: the
    thing most likely to be wrong is the path the step spells, and replacing it
    hides exactly that.  So the fixture is laid down at the step's own paths
    (``build-output/unixnu-out/<target>/tree`` and
    ``vendor/UniXNU-qemu/.cache/firmware/owner-data/``) and the step is executed
    as the runner would execute it, from the workspace root, with only the
    ``env:`` block supplied.

    Paths are RELATIVE and the subprocess runs with ``cwd`` at the workspace
    root, because that is what the step assumes -- and because a Windows
    absolute path handed to an MSYS bash has its backslashes read as escapes,
    which made the first draft's glob resolve to nothing and the warning fire
    for the wrong reason.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="unipad-second-pass-") as temporary:
        bindir = Path(temporary) / "bin"
        bindir.mkdir()
        out_root = Path("build-output/unixnu-out/qemu")
        tree = out_root / "tree"
        (Path(temporary) / tree / "build" / "original-static-system-trust").mkdir(parents=True)
        bundle = (
            Path(temporary)
            / tree
            / "build"
            / "original-static-system-trust"
            / "original-static-system.bundle"
        )
        bundle.write_bytes(b"bundle")
        device = Path("vendor/UniXNU-qemu/.cache/firmware/owner-data")
        (Path(temporary) / device).mkdir(parents=True)
        (Path(temporary) / device / "owner-data-1g.dbr").write_bytes(b"seed")
        if not trust:
            bundle.unlink()
        if not ramdisk:
            (Path(temporary) / device / "owner-data-1g.dbr").unlink()

        # The stub goes at the path the step names, not on PATH: the step runs
        # `bash scripts/macos/build-macos.sh`, so a PATH shim is never consulted
        # and the step dies with "No such file or directory" before a single
        # argument is built.  That is how the first draft of this harness failed,
        # and the failure looked like a missing script rather than a stub in the
        # wrong place.
        stub = Path(temporary) / "scripts" / "macos"
        stub.mkdir(parents=True)
        (stub / "build-macos.sh").write_text(
            '#!/bin/sh\nprintf "BUILDMACOS %s\\n" "$@"\n', encoding="utf-8"
        )
        (stub / "build-macos.sh").chmod(0o755)
        # `tee` IS a PATH lookup, because the step pipes into it.
        (bindir / "tee").write_text('#!/bin/sh\ncat\n', encoding="utf-8")
        (bindir / "tee").chmod(0o755)

        body = step.replace('"$EXPERIMENT_ID"', "root-chain-9").replace('"$STAGE"', "M3")
        environment = dict(os.environ)
        environment["PATH"] = f"{bindir}{os.pathsep}" + environment.get("PATH", "")
        environment["GITHUB_OUTPUT"] = "github-output"
        environment["GITHUB_WORKSPACE"] = temporary
        # The step's own `env:` block, the way the runner supplies it.  The step
        # runs under `set -euo pipefail`, so a variable it reads and this harness
        # does not set is an unbound-variable error -- which is how the first
        # draft of this harness failed, with "BUILD_OUTPUT_DIR: unbound
        # variable", reading as a defect in the step.
        environment.update(
            {
                "EXPERIMENT_ID": "root-chain-9",
                "STAGE": "M3",
                "PAYLOAD_TARGET": "tcg",
                "BUILD_OUTPUT_DIR": "build-output",
                "BUILD_PAYLOAD": "1",
                "KERNELCACHE_PATH": "ipsw-work/kernelcache.macho",
                "DEVICETREE_PATH": "ipsw-work/devicetree",
                "BUILD_LOG": "macos-build-root.log",
            }
        )
        result = subprocess.run(
            [_bash(), "-s"],
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            cwd=temporary,
            timeout=60,
        )
        return result.returncode, result.stdout + result.stderr


def test_the_chain_still_asks_for_both() -> None:
    """The two flags this file is about are the chain's own instruction.

    A guard on the pinned device repository, so that if the chain ever stops
    asking for them this file's premise is re-examined rather than quietly
    rotting.  ``build-os-root.py:174`` prints that command; if it stops, this
    test says so instead of the workflow carrying flags nothing asked for.
    """
    chain = (
        ROOT / "vendor" / "UniXNU-qemu" / "scripts" / "build-os-root.py"
    )
    if not chain.is_file():  # pragma: no cover - the pin is always fetched
        pytest.skip("the device repository is not on disk")
    text = chain.read_text(encoding="utf-8")
    assert CHAIN_COMMAND in text, (
        "build-os-root.py no longer prints the command this test checks the "
        "workflow against; the chain's own instruction has changed"
    )
    assert AMFI_REASON in text, (
        "build-os-root.py no longer records why the trust cache matters; the "
        "reason this file exists has to come from somewhere"
    )
