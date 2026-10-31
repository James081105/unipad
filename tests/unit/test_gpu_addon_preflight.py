"""The GPU addon chain's preflight must fail in itself, naming the tool.

Why this exists
---------------
The chain is seven rings and the expensive ones compile for minutes.  A tool
that is missing is discovered by whichever ring reaches it first, and the
message the operator gets is a compiler error about a header, not a sentence
about a formula.  That already happened once in this chain's design:
``vulkan_smoke.c:9`` includes ``<vulkan/vulkan.h>`` and
``check-turnip-darwin-host.py:55`` compiles it with ``-I
/opt/homebrew/include``, but ``brew install glslang`` -- what the Mesa bootstrap
installs -- does not put the Vulkan headers there.  Without the preflight that
surfaces as a compile error on the ring's fifth file, after four ArmSmmu4K
objects and the IOKit shim have already been built.

So the contract is: the script exits non-zero, its message names a tool, and no
ring's log was written.  This is checked by running the real script on a PATH
that has the shell's own utilities and Python but no Xcode tools, which is what
this Windows host happens to provide.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "macos" / "build-gpu-addon.sh"
DRIVER = REPO / "vendor" / "UniXNU"


def _driver_file(relative: str) -> str:
    """Read a file out of the pinned driver checkout, or skip naming the path.

    ``tests/unit`` runs on a clean checkout where ``vendor/`` is empty, so every
    read through it needs an existence check beside it -- that is what
    ``test_clean_checkout_selfsufficiency.py`` enforces, and the same shape
    ``tests/integration/test_gpu_addon_blob.py``'s ``_packer()`` uses.  A skip
    that names the missing path is the honest outcome; a FileNotFoundError about
    a checkout that was never prepared is not.
    """
    path = DRIVER / relative
    if not path.is_file():
        pytest.skip(
            f"{path} is not present: this stage needs the pinned vendor "
            "checkout from config/sources.lock.json"
        )
    return path.read_text(encoding="utf-8")

# The messages the preflight is allowed to die with.  Each names the tool and
# the ring that needs it.  A message outside this set means the script got past
# the preflight and died somewhere expensive.
#
# The last one used to be spelled ``"no F:/iosvirtual/vendor/UniXNU"`` -- the
# literal this host's repository path produces.  That is a marker which matches
# on exactly one machine: measured 2026-10-31, a clean checkout elsewhere dies
# with ``no <that path>/vendor/UniXNU; ring 0 (the bootstraps) runs first`` and
# the test went red on a refusal that was working correctly.  The tail of the
# message is the part that identifies the refusal; the prefix is a path.
PREFLIGHT_MARKERS = (
    "cannot install shaderc",
    "is absent;",
    "does not answer",
    "still does not compile",
    "is installed but",
    "no python interpreter",
    "the translator checkout is",
    "bootstrap-metal2vulkan.sh has not run",
    "vendor/UniXNU; ring 0 (the bootstraps) runs first",
)


def _bash() -> str:
    """Git Bash's own executable, by absolute path.

    The bare name is ambiguous on Windows 11: ``System32\\bash.exe`` is the WSL
    launcher, and if it wins the script runs under WSL and dies with a
    mount-disk error that has nothing to do with the preflight.  Resolving it
    here and invoking the absolute path removes that ambiguity.
    """
    found = shutil.which("bash")
    assert found is not None, "no bash on PATH; this test cannot run"
    assert Path(found).name.lower() in ("bash.exe", "bash"), found
    return found


def _shell_path() -> str:
    """A PATH with the shell's utilities and Python, and nothing that builds iOS."""
    # The shell's own bin directory comes first, so nothing shadows it.
    parts = [str(Path(_bash()).parent)]
    for tool in ("git", "python", "tee", "tail", "date", "mkdir", "tr", "wc", "grep", "cat"):
        found = shutil.which(tool)
        if found:
            parts.append(str(Path(found).parent))
    # System32 carries the Python launcher and the basic CRT tools Git Bash
    # shells out to; without it the script cannot even start.
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    for sub in ("System32",):
        candidate = Path(windir) / sub
        if candidate.is_dir():
            parts.append(str(candidate))
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return os.pathsep.join(seen)


@pytest.mark.skipif(
    shutil.which("xcrun") is not None,
    reason="a host with xcrun has the tools the preflight provisions; the preflight's "
           "refusal path only exists where they are missing",
)
def test_the_preflight_refuses_before_any_ring_runs(tmp_path):
    """Exit non-zero, name a tool, and leave no ring log behind."""
    env = dict(os.environ)
    env["PATH"] = _shell_path()
    # The chain writes its logs wherever this says; a tmp path keeps the test
    # from depending on the repository's build-output directory.
    env["ADDON_LOG_DIR"] = str(tmp_path / "logs")

    result = subprocess.run(
        [_bash(), str(SCRIPT), REPO.as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
        cwd=str(tmp_path),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"the chain reported success on a host with no build tools: {combined!r}"
    )
    assert any(marker in combined for marker in PREFLIGHT_MARKERS), (
        f"the failure did not name a tool the preflight provisions; it got past "
        f"the preflight and died somewhere expensive. Output: {combined[-2000:]!r}"
    )
    # The one thing that must NOT exist: a ring's log.  Its presence means the
    # preflight let the chain through.
    ring_logs = list((tmp_path / "logs").glob("addon-*.log"))
    assert not ring_logs, (
        f"a ring ran before the preflight finished: {[p.name for p in ring_logs]}"
    )


def test_the_preflight_provisions_the_shader_tools():
    """`glslc` is not in the `glslang` formula, and ring 2a calls it directly.

    gen-turnip-smoke-shaders.py:15-16 runs `glslc` and `spirv-val` by name.
    Homebrew's glslang provides glslangValidator and the spirv-* tools; glslc
    comes from shaderc.  The Mesa bootstrap installs glslang, so without this
    the chain fails on its first shader compile -- which is inside ring 2, after
    the ArmSmmu4K objects and the IOKit shim have been built.

    Asserted on the code, not the word, for the reason the test above records:
    a guard that prose can satisfy is not a guard.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert "brew install shaderc" in script, (
        "build-gpu-addon.sh no longer installs shaderc; glslc (which "
        "gen-turnip-smoke-shaders.py:15 calls by name) is not in the glslang "
        "formula the Mesa bootstrap installs"
    )
    assert "glslc --version" in script and "spirv-val --version" in script, (
        "the preflight no longer asks glslc and spirv-val to answer; a tool "
        "that is on PATH but does not run is the same failure as an absent one"
    )
    generators = _driver_file("scripts/gen-turnip-smoke-shaders.py")
    assert "'glslc'" in generators and "'spirv-val'" in generators, (
        "gen-turnip-smoke-shaders.py no longer calls glslc and spirv-val by "
        "name; the preflight is provisioning tools nothing uses"
    )


def _stub_bin(tmp_path: Path) -> Path:
    """A directory of stubs that make every tool the preflight probes answer.

    Why stubs at all: the refusal test above stops at the FIRST missing tool,
    which on this host is ``glslc`` -- so it never reaches the cargo check, the
    toolchain reader, or the translator pin.  That is exactly how the
    use-before-definition bug on run 40928765061 survived a green suite: the
    script died on ``$BRIDGE`` thirty-eight lines below its definition, at a
    point no test had ever executed.

    A guard that only ever runs as far as its host allows is a guard with a
    host-shaped hole in it.  These stubs close the hole: with every tool
    answering, the preflight runs to its end and the test can assert it reached
    the rings rather than dying halfway through provisioning.
    """
    bindir = tmp_path / "stubbin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name, body in {
        # Each answers the question the preflight asks and nothing else, so a
        # stub that stops matching shows up as a failure rather than as a pass.
        "glslc": "#!/bin/sh\nexit 0\n",
        "spirv-val": "#!/bin/sh\nexit 0\n",
        "brew": "#!/bin/sh\nexit 0\n",
        "codesign": "#!/bin/sh\nexit 0\n",
        "strip": "#!/bin/sh\nexit 0\n",
        "cargo": "#!/bin/sh\necho 'cargo 1.98.0 (stub)'\nexit 0\n",
        "rustup": "#!/bin/sh\necho aarch64-apple-ios\nexit 0\n",
        "llvm-as": "#!/bin/sh\nexit 0\n",
        # xcrun is how the SDK, the compilers and the Vulkan-header proof are
        # reached; a stub that succeeds is what "the toolchain is present" means
        # to this preflight.
        "xcrun": "#!/bin/sh\nexit 0\n",
    }.items():
        path = bindir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return bindir


def test_the_preflight_runs_past_the_toolchain_check_when_every_tool_answers(tmp_path):
    """Teeth for the coverage gap: the whole preflight, not just its first refusal.

    Run 40928765061 died at ``$BRIDGE`` -- used thirty-eight lines above its own
    definition -- and every preflight test stayed green, because on this host the
    preflight never got past ``glslc``.  This test supplies stubs for every tool
    so it does, and asserts the log reaches the toolchain line, which is the last
    thing before the checks that need a real translator checkout.

    The toolchain line is the right landmark: it sits immediately after the
    ``$BRIDGE`` read, so a use-before-definition anywhere in the cargo block is
    reached here and nowhere else.
    """
    if not (REPO / "vendor" / "UniXNU" / "scripts" / "build-native-metal-bridge.py").is_file():
        pytest.skip("the pinned vendor checkout is absent; the preflight cannot run")
    stubs = _stub_bin(tmp_path)
    logs = tmp_path / "logs"

    env = dict(os.environ)
    env["PATH"] = f"{stubs}{os.pathsep}{_shell_path()}"
    env["ADDON_LOG_DIR"] = str(logs)
    result = subprocess.run(
        [_bash(), str(SCRIPT), REPO.as_posix()],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=600, cwd=str(tmp_path),
    )
    combined = result.stdout + result.stderr
    log_text = logs / "build-gpu-addon.log"
    written = log_text.read_text(encoding="utf-8", errors="replace") if log_text.is_file() else ""

    # The bug this test exists for.  A use-before-definition is not a lint nit:
    # under `set -u` it is a preflight that dies halfway through provisioning and
    # leaves a log that stops mid-sentence, which is what run 40928765061's did.
    assert "unbound variable" not in combined, (
        f"the preflight died on an unbound variable; a variable is used before "
        f"it is assigned. Output: {combined[-1500:]!r}"
    )
    assert "for toolchain 1.98.0" in written, (
        f"the preflight stopped before the toolchain check; it got only as far "
        f"as its host's missing tools, which is the hole run 40928765061 fell "
        f"through. Output: {combined[-1500:]!r}"
    )
    assert "preflight: llvm-as is" in written, (
        f"the preflight stopped before the llvm-as check. Output: {combined[-1500:]!r}"
    )
    # And no ring ran: reaching the rings means the preflight let something
    # through that it should have refused.
    assert not list(logs.glob("addon-*.log")), (
        f"a ring ran; the preflight let the chain through: "
        f"{[p.name for p in logs.glob('addon-*.log')]}"
    )


def test_the_rust_toolchain_pin_is_read_from_the_bridge_not_spelled_twice():
    """Teeth: the preflight queried the wrong toolchain and reported a false negative.

    MEASURED on run 40921780876.  This check asked
    ``rustup target list --installed`` -- no ``+toolchain`` -- which answers for
    rustup's DEFAULT toolchain, while the bridge builds with the pinned one.  On
    that runner bootstrap-metal2vulkan.sh had just installed ``aarch64-apple-ios``
    into the pinned toolchain (its own log reads
    ``rustup targets: aarch64-apple-darwin aarch64-apple-ios``) and this line
    still logged "aarch64-apple-ios is not in `rustup target list --installed`".
    A negative answer from an instrument pointed at the wrong subject is worse
    than no answer, because it is believed.

    So the pin is read from the bridge script -- the same one it builds with --
    and both the cargo check and the target check use it.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert "rustup \"+$CARGO_TOOLCHAIN\" target list --installed" in script, (
        "the target check no longer names the pinned toolchain; it would answer "
        "for rustup's default, which on the measured runner was not the pinned "
        "one, and report a target as missing while it was installed"
    )
    assert "cargo \"+$CARGO_TOOLCHAIN\" --version" in script, (
        "the cargo check no longer uses the toolchain pin read from the bridge"
    )
    # The pin must be READ, not spelled: a second copy of `1.98.0` in this script
    # is a second place for it to drift from build-native-metal-bridge.py:82.
    #
    # Proved by running the reader this script uses against the real bridge, so
    # the assertion is about what the reader extracts rather than about how its
    # source is spelled -- and by checking the pin is absent from the CODE, with
    # the prose stripped first.  That second half is not decoration: this
    # assertion's first two versions were both satisfied by another occurrence of
    # the same string (a log line, then a comment), which is the third time in
    # this file an assertion has been fooled by prose.
    pin = _toolchain_pin()
    code = _code_only(script)
    assert f"+{pin}" not in code, (
        f"the toolchain pin +{pin} is spelled in this script's code as well as "
        f"read from the bridge; build-native-metal-bridge.py is the one place it "
        f"should live"
    )
    assert 'cargo "+$CARGO_TOOLCHAIN" --version' in code, (
        "the cargo check no longer uses the toolchain pin read from the bridge"
    )
    assert f"+{pin}" in _driver_file("scripts/build-native-metal-bridge.py"), (
        f"the reader extracted {pin!r} but the bridge does not invoke "
        f"cargo +{pin}; the reader is aimed at the wrong line"
    )


def _code_only(text: str) -> str:
    """The script with its full-line comments removed.

    Naive on purpose: it drops lines whose first non-blank character is ``#``,
    which is where this script's prose lives.  A ``#`` inside a quoted string on
    a code line would survive, and that is the right direction -- the point is to
    check the pin is not spelled in CODE, not to parse shell.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _toolchain_pin() -> str:
    """Run the reader this script uses, against the real bridge script."""
    import re

    bridge = DRIVER / "scripts" / "build-native-metal-bridge.py"
    if not bridge.is_file():
        pytest.skip(f"{bridge} is not present; the pinned vendor checkout is absent")
    text = bridge.read_text(encoding="utf-8")
    found = re.search(r"'cargo', '\+([0-9][0-9.]*)'", text)
    if not found:
        pytest.skip("the bridge no longer spells a cargo +<version> toolchain pin")
    assert found.group(1), "the reader extracted an empty toolchain pin"
    return found.group(1)


def test_every_ring_reports_how_long_it_took():
    """The job's ``timeout-minutes`` is an estimate until a run produces numbers.

    ``build-macos.yml`` sizes the build job in minutes, and a chain that
    overruns it fails at the job boundary -- which names no ring.  The way out
    is for each ring to say how long it took, so the first run that reaches them
    replaces the estimate with a measurement.  Asserted on the source because
    the timing itself only exists on a macOS runner.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'log "PASS $name in $((ended - started))s' in script, (
        "the ring helper no longer logs how long a ring took; the job's "
        "timeout-minutes could not be corrected from a run's own evidence"
    )
    assert 'started="$(date +%s)"' in script and 'ended="$(date +%s)"' in script, (
        "the ring helper no longer brackets each ring with timestamps"
    )
    # The failure line must carry the duration too: a ring that ran for fifty
    # minutes and then failed is a different problem from one that failed in
    # five seconds, and the log is where that distinction is read.
    assert 'log "FAIL $name after $((ended - started))s' in script, (
        "a failed ring no longer reports how long it ran before failing"
    )
    """The one gap that was found by reading the include rather than by running.

    Recorded as a source assertion because it cannot be executed here: the
    include and the `-I` flag are the evidence, and a future edit that drops the
    Vulkan-header check would otherwise look harmless.
    """
    smoke = _driver_file("drivers/NativeAdreno/tests/hostsim/vulkan_smoke.c")
    assert "#include <vulkan/vulkan.h>" in smoke, (
        "vulkan_smoke.c no longer includes <vulkan/vulkan.h>; the preflight's "
        "vulkan-headers check may now be unnecessary"
    )
    check = _driver_file("scripts/check-turnip-darwin-host.py")
    assert "'/opt/homebrew/include'" in check, (
        "check-turnip-darwin-host.py no longer compiles against "
        "/opt/homebrew/include; the preflight's header proof is aimed at the "
        "wrong path"
    )
    script = SCRIPT.read_text(encoding="utf-8")
    # Asserted on the CODE, not on the word.  The first version of this test
    # checked `"vulkan-headers" in script` and the mutation that deleted the
    # whole check left it green, because the comment above the check spells the
    # formula name too.  A guard that prose can satisfy is not a guard.
    assert "brew install vulkan-headers" in script, (
        "build-gpu-addon.sh no longer installs vulkan-headers; the header gap "
        "this chain was diagnosed with is back"
    )
    # The proof's own compile line.  Spelled without the closing quote because
    # the script's printf carries the rest of the translation unit after the
    # include; `printf '#include <vulkan/vulkan.h>` is still code-specific --
    # the comment above it writes the header bare, so prose cannot satisfy it.
    assert "printf '#include <vulkan/vulkan.h>" in script, (
        "build-gpu-addon.sh no longer compiles a translation unit against "
        "/opt/homebrew/include to prove the headers are usable; a header that "
        "is present but unusable is the same failure as an absent one"
    )
    assert "xcrun clang -x c -I /opt/homebrew/include" in script, (
        "the preflight no longer compiles the proof TU against "
        "/opt/homebrew/include; a header that is present but unusable is the "
        "same failure as an absent one"
    )
