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

# The messages the preflight is allowed to die with.  Each names the tool and
# the ring that needs it.  A message outside this set means the script got past
# the preflight and died somewhere expensive.
PREFLIGHT_MARKERS = (
    "cannot install shaderc",
    "is absent;",
    "does not answer",
    "still does not compile",
    "is installed but",
    "no python interpreter",
    "the translator checkout is",
    "bootstrap-metal2vulkan.sh has not run",
    "no F:/iosvirtual/vendor/UniXNU",
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
    generators = (
        REPO / "vendor" / "UniXNU" / "scripts" / "gen-turnip-smoke-shaders.py"
    ).read_text(encoding="utf-8")
    assert "'glslc'" in generators and "'spirv-val'" in generators, (
        "gen-turnip-smoke-shaders.py no longer calls glslc and spirv-val by "
        "name; the preflight is provisioning tools nothing uses"
    )


def test_the_preflight_names_the_vulkan_header_gap():
    """The one gap that was found by reading the include rather than by running.

    Recorded as a source assertion because it cannot be executed here: the
    include and the `-I` flag are the evidence, and a future edit that drops the
    Vulkan-header check would otherwise look harmless.
    """
    sim = REPO / "vendor" / "UniXNU" / "drivers" / "NativeAdreno" / "tests" / "hostsim"
    smoke = (sim / "vulkan_smoke.c").read_text(encoding="utf-8")
    assert "#include <vulkan/vulkan.h>" in smoke, (
        "vulkan_smoke.c no longer includes <vulkan/vulkan.h>; the preflight's "
        "vulkan-headers check may now be unnecessary"
    )
    check = (REPO / "vendor" / "UniXNU" / "scripts" / "check-turnip-darwin-host.py").read_text(encoding="utf-8")
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
