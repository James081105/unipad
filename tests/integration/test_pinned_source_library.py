"""The shared pinned-source library, exercised against a LOCAL repository.

Why this file exists at all
---------------------------
``scripts/macos/lib/pinned-source.sh`` is now the half of
``bootstrap-mesa-turnip.sh`` and ``bootstrap-metal2vulkan.sh`` that fetches a
pinned commit, proves it is that commit, wipes the working tree and applies a
recorded patch list.  The Mesa bootstrap was runner-verified over three failed
runs (40812750074, 40816437974, 40819797000) before it passed, so every guard
in that logic is a measured failure -- and the metal2vulkan bootstrap has never
run anywhere.  Extracting the shared half is what makes the second bootstrap
cheap; it is also what makes a defect in it cost TWO scripts, one of which
takes an hour of macOS runner time to re-verify.

So the library is exercised here, against a git repository created in a
temporary directory, with no network and no upstream: init, commit, fetch by
sha, apply a locally generated patch, assert the bytes.

What this test proves
---------------------
* a fetch by SHA lands on THAT commit, not on whatever the remote's HEAD is
  (the fixture pins the first of two commits on purpose);
* the checkout's line endings are LF even on a host whose git says
  autocrlf=true -- and the control below measures that the same checkout
  WITHOUT the guard is CRLF here, so the guard is not decorative;
* the tree is reset and cleaned between runs, so a second run at the same pin
  is a function of the pin rather than of whatever the first run left;
* the patch list is applied in the order it is given, and a name the patch
  directory does not hold is a refusal rather than a skipped patch;
* the probe runs BEFORE the patches, which is the only order that makes "the
  pinned files verify" a claim about the pinned inputs;
* a pin the checkout is not at, and a patch that does not apply, are refusals
  that name the reason rather than silent successes.

What this test deliberately does NOT claim
------------------------------------------
No network: a real fetch from github.com or a googlesource mirror cannot be
exercised on this host, and neither can the seven metal2vulkan patches being
applied to the pinned upstream commit -- that needs the runner.  Nor does
anything here touch bison, meson, glslang, llvm-as or cargo, or the workflow
that calls the two bootstraps.

The one claim no test can make is that refactoring
``bootstrap-mesa-turnip.sh`` to use this library preserved its behaviour.  That
rests on the extraction being line-for-line, which is a property of the diff
rather than of the code.  It is recorded in docs/implementation-plan.md as an
extraction, and can only be re-verified by a runner.

Every path this file creates is ASCII.  That is not tidiness: this host's
``TEMP`` is reachable both as ``C:\\Users\\ZHUANZ~1\\...`` and as
``C:\\Users\\ZhuanZ（无密码）\\...``, and the long spelling is what a
non-ASCII-aware native tool sees when the ANSI code page is not UTF-8.  The
temporary root is asserted ASCII rather than assumed, so a host where it is
not fails naming the path instead of failing somewhere inside git.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: The library under test.  Resolved from this file rather than from a relative
#: path: the driver script below is executed with a working directory of its
#: own, so a relative library path would be resolved against that.
LIBRARY = ROOT / "scripts" / "macos" / "lib" / "pinned-source.sh"

#: The build script whose PATCHES list the metal2vulkan bootstrap reads.  A
#: separate check below asserts the extraction agrees with it; that check needs
#: the pinned vendor checkout and skips without it, exactly as
#: tests/integration/test_gpu_addon_blob.py does for its packer.
BRIDGE = ROOT / "vendor" / "UniXNU" / "scripts" / "build-native-metal-bridge.py"

#: The first commit of the fixture, and the tree the patch carries it to.
#: Written as literals rather than generated, so the expected bytes are in this
#: file rather than in whatever `git diff` decides to emit about a repository
#: nobody else has.
FIRST = "alpha\nbeta\ngamma\n"
SECOND = "alpha\nbeta\ngamma\ndelta\n"

#: The patch that turns FIRST into SECOND, in the shape `git apply` expects.
#: It is a literal because a generated one would carry the fixture repository's
#: blob shas, and a patch whose context matches is a patch that applies.
PATCH = """diff --git a/item.txt b/item.txt
--- a/item.txt
+++ b/item.txt
@@ -1,3 +1,4 @@
 alpha
 beta
 gamma
+delta
"""

#: The names the metal2vulkan bootstrap applies, in the order
#: build-native-metal-bridge.py:25-29 names them.  Read back from that file by
#: the test below; this literal is what the extraction is compared against.
EXPECTED_PATCH_NAMES = (
    "0001-optional-constant-output-members.patch",
    "0002-resource-wait-storage-dependencies.patch",
    "0003-cache-device-entry-point-priming.patch",
    "0004-fold-constant-global-field-loads.patch",
    "0005-keep-module-decodes-and-profile-fingerprints.patch",
    "0006-skip-draws-without-a-pipeline.patch",
    "0007-accept-two-plane-rgb-surfaces.patch",
)

#: The directory those names are relative to, as the same assignment spells it.
EXPECTED_PATCH_DIR = "drivers/NativeMetal/bridge/patches"


def _bash() -> str:
    """A bash that can actually tell a parseable script from an unparseable one.

    The instrument is validated before its answer is trusted, for the reason
    tests/acceptance/test_shell_script_syntax.py gives: a bash that answers 0
    to everything (a shim, a stub, a broken MSYS binary) would make every
    assertion in this file vacuous.  Both a must-parse and a must-not-parse
    fragment are put to it, and a bash that cannot separate them is refused
    rather than quietly trusted.
    """
    for name in ("bash", "bash.exe"):
        located = shutil.which(name)
        if located is None:
            continue
        good = subprocess.run([located, "-n", "-c", "true() { :; }"], capture_output=True)
        bad = subprocess.run([located, "-n", "-c", "true() { :;"], capture_output=True)
        if good.returncode == 0 and bad.returncode != 0:
            return located
    pytest.skip("no bash on this host can distinguish a parseable script from an unparseable one")


def _git(cwd: Path, *args: str, isolated: bool = True) -> subprocess.CompletedProcess:
    """One git invocation, with the host's system config out of the way.

    ``isolated`` is the default because a fixture repository whose line endings
    depend on the host's gitconfig is a fixture that measures the host.  It is
    turned OFF by exactly one call -- the control that shows what the host's own
    setting would have produced.
    """
    env = dict(os.environ)
    if isolated:
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        # An empty HOME too, so a ~/.gitconfig on the host cannot reach the
        # fixture.  Pointed at a directory that exists rather than at "".
        env["HOME"] = str(cwd)
    return subprocess.run(["git", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, check=True)


def _fixture(base: Path) -> tuple[Path, str, str]:
    """A two-commit repository, plus the patch from the first commit to the second.

    Two commits rather than one, and the pin is the FIRST: a checkout that
    silently took the remote's HEAD would pass a one-commit fixture and fail a
    real run, because a real remote's HEAD is never the pinned commit.
    """
    origin = base / "origin"
    origin.mkdir(parents=True)
    _git(origin, "-c", "init.defaultBranch=main", "init", "--quiet", ".")
    _git(origin, "config", "user.email", "fixture@example.invalid")
    _git(origin, "config", "user.name", "fixture")
    # LF, written as bytes: Path.write_text on Windows translates \n to \r\n,
    # which would make the fixture's own line endings the thing under test.
    (origin / "item.txt").write_bytes(FIRST.encode("utf-8"))
    _git(origin, "add", "item.txt")
    _git(origin, "commit", "--quiet", "-m", "first")
    first = _git(origin, "rev-parse", "HEAD").stdout.strip()
    (origin / "item.txt").write_bytes(SECOND.encode("utf-8"))
    _git(origin, "add", "item.txt")
    _git(origin, "commit", "--quiet", "-m", "second")
    second = _git(origin, "rev-parse", "HEAD").stdout.strip()
    (base / "patches").mkdir(parents=True)
    (base / "patches" / "one.patch").write_bytes(PATCH.encode("utf-8"))
    return origin, first, second


#: The driver: the two library calls, in the order a bootstrap makes them, with
#: the log/die pair the library expects from its caller.  Placeholders are
#: substituted rather than f-stringed, because the body is full of ${...} and
#: $(...) that an f-string would try to evaluate.
DRIVER = """set -uo pipefail
LIB='@LIB@'
WORK='@WORK@'
COMMIT='@COMMIT@'
PATCHES='@PATCHES@'
PROBE='@PROBE@'
LOG="$WORK/bootstrap.log"
mkdir -p "$(dirname "$LOG")"
: > "$LOG"
log() { printf '%s %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }
die() { log "FAIL $*"; log "---- last 12 lines of $LOG ----"; tail -12 "$LOG" | tee -a "$LOG" >&2; exit 1; }
@PROBE_BODY@
. "$LIB"
SOURCE="$WORK/work"
pinned_source_checkout "$SOURCE" "$WORK/origin" "$COMMIT" "" "$PROBE" "fixture source"
pinned_source_apply_patches "$SOURCE" "$WORK/patches" "$PATCHES" "fixture" "$COMMIT"
log "PASS the library placed and patched the fixture"
"""

#: The two probes, defined in the driver so they can be passed by name -- the
#: library runs "$probe", and a function is a command.  ``probe_pristine``
#: asserts the tree is PRISTINE, which is the only thing that proves the probe
#: runs before the patches: a probe that merely returned 0 would pass either
#: side of them.  ``probe_refuse`` is the one that always refuses, for the test
#: that a refusing probe stops the run.
PROBE_BODY = """probe_pristine() {
    local item="$SOURCE/item.txt"
    if ! grep -q '^alpha$' "$item"; then
        echo "the probe did not find the pinned file at all" >&2
        return 1
    fi
    if grep -q '^delta$' "$item"; then
        echo "the probe ran AFTER the patches were applied" >&2
        return 1
    fi
    return 0
}
probe_refuse() {
    echo "this probe refuses" >&2
    return 1
}
"""


def _run(base: Path, bash: str, commit: str, patches: str, probe: str = "") -> subprocess.CompletedProcess:
    """Write the driver into ``base`` and run it there."""
    driver = base / "driver.sh"
    driver.write_bytes(DRIVER.replace("@LIB@", LIBRARY.as_posix())
                       .replace("@WORK@", base.as_posix())
                       .replace("@COMMIT@", commit)
                       .replace("@PATCHES@", patches)
                       .replace("@PROBE@", probe)
                       .replace("@PROBE_BODY@", PROBE_BODY)
                       .encode("utf-8"))
    # cwd is the fixture root, and it is ASCII.  The driver resolves every path
    # from $WORK, which is absolute, so nothing here depends on the caller's
    # working directory -- the same property the library's own comment demands.
    # The environment is inherited deliberately: the host's git config is what
    # makes the line-ending assertion below mean anything.
    return subprocess.run([bash, driver.name], cwd=str(base), capture_output=True, text=True, timeout=300)


def _force_rmtree(path: Path) -> None:
    """Remove a tree git made read-only, instead of leaving it behind.

    ``git init`` writes its object files read-only, and ``shutil.rmtree``
    refuses them on Windows with a bare PermissionError.  Left unhandled that
    would fail the test asserting something else entirely, and would leave the
    fixture directory on disk -- which on this host is the difference between a
    temporary directory and one of the fifty-odd leftovers that filled F:.
    """
    def _relax(func, target, _exc):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onexc=_relax)


@pytest.fixture()
def fixture() -> Path:
    """The fixture repository, in an ASCII temporary root of our own choosing.

    Not pytest's ``tmp_path``: on this host that is
    ``.../pytest-of-ZhuanZ（无密码）/...``, and the long spelling of the user
    directory is what a native tool sees under a non-UTF-8 ANSI code page.
    ``tempfile.gettempdir()`` answers with the 8.3 spelling here, which is
    ASCII, and the assertion below is what makes that a checked fact rather
    than a lucky one.
    """
    base = Path(tempfile.mkdtemp(prefix="pinned-source-"))
    try:
        assert base.as_posix().isascii(), (
            f"the temporary root {base} is not ASCII; the driver script and the "
            "git invocations below are written to run under any ANSI code page, "
            "and this host's TEMP would have to be pointed at an ASCII path first"
        )
        yield base
    finally:
        _force_rmtree(base)


def test_the_library_places_the_pinned_commit_and_applies_the_patches(fixture: Path) -> None:
    """Fetch by sha, LF line endings, patch applied, probe run first.

    One run, five claims, because the fixture costs two git commits and the
    claims are about one state of it: after ``pinned_source_checkout`` +
    ``pinned_source_apply_patches`` the tree is the pinned commit with exactly
    the patch applied and nothing else changed.
    """
    bash = _bash()
    origin, first, second = _fixture(fixture)
    assert first != second, "the fixture needs two distinct commits"

    done = _run(fixture, bash, first, "one.patch", probe="probe_pristine")
    assert done.returncode == 0, done.stdout + done.stderr

    work = fixture / "work"
    # The pin, not the remote's HEAD: the origin is left on its second commit.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == first
    assert _git(work, "rev-parse", "HEAD^{tree}").stdout.strip() == \
        _git(origin, "rev-parse", f"{first}^{{tree}}").stdout.strip()
    # Exactly the pinned tree plus the patch, and nothing else: `git status`
    # names one modified file, and no untracked file survives.
    assert _git(work, "status", "--porcelain").stdout == " M item.txt\n"
    # The patched bytes, read back as bytes so a CRLF checkout cannot hide
    # behind a text-mode read that normalises it away.
    assert (work / "item.txt").read_bytes() == SECOND.encode("utf-8")
    # The patch count is the library's own log line, and it is asserted rather
    # than inferred: a loop that applied nothing would still leave the file
    # alone, and a reader would have to notice the absence.
    assert "fixture patches: 1 applied" in (fixture / "bootstrap.log").read_text(encoding="utf-8")


def test_the_line_ending_guard_is_load_bearing_on_this_host(fixture: Path) -> None:
    """The same checkout WITHOUT ``core.autocrlf=false`` is CRLF here.

    A guard test that cannot fail is worse than no guard, so the control is run
    rather than asserted from memory.  It is conditional on the host actually
    saying ``autocrlf=true``: on a host that does not, the guard has nothing to
    guard and the control is skipped with the reason printed, rather than
    passing vacuously or failing for an environmental reason.
    """
    host = subprocess.run(["git", "config", "--get", "core.autocrlf"],
                          capture_output=True, text=True).stdout.strip()
    if host != "true":
        pytest.skip(f"this host's git says core.autocrlf={host!r}, so the guard has nothing to prove here")

    bash = _bash()
    _origin, first, _second = _fixture(fixture)

    # The control: init, fetch, checkout -- the library's shape with its one
    # line-ending setting removed.  NOT isolated, so the host's own gitconfig
    # is what applies; that is the whole point of a control.
    control = fixture / "control"
    control.mkdir(parents=True)
    _git(control, "init", "--quiet", ".", isolated=False)
    _git(control, "remote", "add", "origin", (fixture / "origin").as_posix(), isolated=False)
    _git(control, "fetch", "--depth", "1", "origin", first, isolated=False)
    _git(control, "checkout", "--quiet", "--detach", first, isolated=False)
    assert (control / "item.txt").read_bytes() == FIRST.replace("\n", "\r\n").encode("utf-8"), (
        "the control did not produce CRLF, so the line-ending assertion in "
        "test_the_library_places_the_pinned_commit_and_applies_the_patches "
        "is proving nothing on this host"
    )

    # And the library's own tree, from the same fixture, carries no CR at all.
    # Asserted as an absence rather than as an equality with FIRST, because the
    # run above also applies the patch: what is being checked is the line
    # ending, and a file with the patch applied and LF is exactly right.
    done = _run(fixture, bash, first, "one.patch")
    assert done.returncode == 0, done.stdout + done.stderr
    patched = (fixture / "work" / "item.txt").read_bytes()
    assert b"\r" not in patched, f"the library's checkout carries CRLF: {patched!r}"
    assert patched == SECOND.encode("utf-8")


def test_a_second_run_at_the_same_pin_resets_and_reapplies(fixture: Path) -> None:
    """Reset and clean between runs, so the outcome does not depend on history.

    The second run must find the tree already at the pin, say so, and re-apply
    the patch from scratch.  A run that trusted what the previous one left
    behind would apply the patch twice and produce a file with two `delta`
    lines -- and the failure would only appear where a cache is reused, which
    is exactly what a warm ``.cache`` directory is.
    """
    bash = _bash()
    _origin, first, _second = _fixture(fixture)

    assert _run(fixture, bash, first, "one.patch").returncode == 0
    assert (fixture / "work" / "item.txt").read_bytes() == SECOND.encode("utf-8")

    again = _run(fixture, bash, first, "one.patch")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "fixture source already at" in again.stdout
    assert (fixture / "work" / "item.txt").read_bytes() == SECOND.encode("utf-8")
    assert _git(fixture / "work", "status", "--porcelain").stdout == " M item.txt\n"


def test_a_moved_pin_is_refetched_and_a_dirty_tree_names_why(fixture: Path) -> None:
    """The pin is read every run -- and a dirty tree is a refusal that names it.

    One test, two halves, because they are the same pin move seen from two
    starting states, and splitting them would pay for the fixture twice.

    Dirty first.  The library resets the tree AFTER the checkout, not before
    it, so a checkout that already carries the recorded patches -- which is
    what a completed run leaves behind -- refuses a pin move with git's own
    "Your local changes would be overwritten by checkout".  That is a KNOWN
    LIMITATION, pinned here rather than left to be discovered.

    It is not reachable in the workflow: neither bootstrap's cache directory is
    in the workflow's cache path list, so every run starts from a fresh runner
    with an empty ``.cache``.  It IS reachable by hand -- a developer running
    the bootstrap twice on one machine after a pin bump -- and there it fails
    loudly and diagnosably rather than silently building the old source, which
    is the failure mode worth having.

    The two-line fix is a ``reset --hard`` + ``clean`` before the
    ``checkout --detach`` in pinned_source_checkout.  It is deliberately NOT
    applied: bootstrap-mesa-turnip.sh is runner-verified, and the extraction
    that produced this library was required to be behaviour-preserving.  The
    fix only changes a path that currently exits 1, but "only" is a claim that
    costs an hour of macOS runner time to check, so it is recorded instead.

    Then clean, and the same pin move works: the tree is re-fetched, lands on
    the new commit, and refuses at the patch instead -- which is the right
    refusal, because the recorded patch was made against the old tree.  What is
    asserted is that it names BOTH the patch and the commit; a bare nonzero
    exit would leave the reader to work out which of the two halves failed.
    """
    bash = _bash()
    _origin, first, second = _fixture(fixture)
    work = fixture / "work"

    assert _run(fixture, bash, first, "one.patch").returncode == 0
    assert _git(work, "status", "--porcelain").stdout == " M item.txt\n"

    dirty = _run(fixture, bash, second, "one.patch")
    assert dirty.returncode != 0
    assert f"cannot check out {second}" in dirty.stdout + dirty.stderr
    assert "Your local changes" in (fixture / "bootstrap.log").read_text(encoding="utf-8")
    # The tree is left where it was, at the OLD pin, and the run says so.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == first

    # Cleaned from here rather than by the driver, because the driver resets
    # AFTER the checkout and this half is about the checkout itself.
    _git(work, "reset", "--hard", "--quiet", "HEAD")
    _git(work, "clean", "-fdq")

    moved = _run(fixture, bash, second, "one.patch")
    assert moved.returncode != 0
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == second
    assert f"cannot apply one.patch onto fixture {second}" in moved.stdout + moved.stderr
    assert "PASS the library placed" not in moved.stdout

def test_a_missing_patch_is_a_refusal_that_names_it(fixture: Path) -> None:
    """A name the patch directory does not hold is a refusal, not a skipped patch."""
    bash = _bash()
    _origin, first, _second = _fixture(fixture)

    done = _run(fixture, bash, first, "one.patch\nnot-a-real.patch")
    assert done.returncode != 0
    assert "the pin names not-a-real.patch and it is not in" in done.stdout + done.stderr
    # The one that did apply is not rolled back -- the loop dies where it dies,
    # and the tree is left in the state the log describes.  What matters is that
    # the run does not report success.
    assert "PASS the library placed" not in done.stdout


def test_the_probe_runs_before_the_patches_and_can_refuse(fixture: Path) -> None:
    """A probe sees the PRISTINE tree, and a refusing probe stops the run.

    Both halves are the same driver: the probe asserts the tree has no `delta`
    line, so a library that ran it after the patches would be caught by the
    probe's own complaint, and a library that ran it before would not.
    """
    bash = _bash()
    _origin, first, _second = _fixture(fixture)

    seen = _run(fixture, bash, first, "one.patch", probe="probe_pristine")
    assert seen.returncode == 0, seen.stdout + seen.stderr
    assert "the probe ran AFTER" not in seen.stdout + seen.stderr

    # A second, independent run for the refusing probe: the first one left the
    # patch applied, and a run that starts from that tree would be asserting
    # the first run's leftovers rather than its own.
    _force_rmtree(fixture / "work")
    refused = _run(fixture, bash, first, "one.patch", probe="probe_refuse")
    assert refused.returncode != 0
    assert "the fixture source's pinned files do not verify" in refused.stdout + refused.stderr
    assert "PASS the library placed" not in refused.stdout
    # And nothing was patched: the refusal happened before the patch loop.
    assert _git(fixture / "work", "status", "--porcelain").stdout == ""
    assert (fixture / "work" / "item.txt").read_bytes() == FIRST.encode("utf-8")


def test_the_patch_list_is_read_from_the_bridge_build_script() -> None:
    """The metal2vulkan bootstrap reads its patch list, it does not restate it.

    ``build-native-metal-bridge.py:25-29`` is the order the build applies and
    verifies against, so the names and their directory are recovered from that
    assignment's own AST.  This asserts the recovery against the file, which is
    the only way a reordering upstream shows up as a change here rather than as
    a Rust build failure forty minutes later.

    Needs the pinned vendor checkout, and skips naming the exact path when it
    is absent -- the same guard tests/integration/test_gpu_addon_blob.py uses,
    for the same reason: a test that dies on a missing vendor file before any
    of its own assertions run is a test that reddens a clean checkout.
    """
    if not BRIDGE.is_file():
        pytest.skip(
            f"{BRIDGE} is not present: this check needs the pinned vendor "
            "checkout from config/sources.lock.json, prepared by the "
            "'integration (pinned upstream)' job in "
            ".github/workflows/preflight.yml"
        )
    probe = (
        "import ast, sys\n"
        "tree = ast.parse(open(sys.argv[1], encoding='utf-8').read())\n"
        "for node in ast.walk(tree):\n"
        "    if not isinstance(node, ast.Assign):\n"
        "        continue\n"
        "    if not any(isinstance(t, ast.Name) and t.id == 'PATCHES' for t in node.targets):\n"
        "        continue\n"
        "    value = node.value\n"
        "    if not isinstance(value, ast.ListComp) or not isinstance(value.generators[0].iter, ast.List):\n"
        "        continue\n"
        "    folder = ''\n"
        "    inner = value.elt\n"
        "    while isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div):\n"
        "        if isinstance(inner.right, ast.Constant) and isinstance(inner.right.value, str):\n"
        "            folder = inner.right.value if not folder else inner.right.value + '/' + folder\n"
        "        inner = inner.left\n"
        "    print(folder)\n"
        "    for elt in value.generators[0].iter.elts:\n"
        "        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):\n"
        "            print(elt.value)\n"
        "    break\n"
    )
    done = subprocess.run([sys.executable, "-c", probe, str(BRIDGE)],
                          capture_output=True, text=True, check=True)
    lines = done.stdout.splitlines()
    assert lines[0] == EXPECTED_PATCH_DIR, lines[:1]
    assert tuple(lines[1:]) == EXPECTED_PATCH_NAMES, lines[1:]
    # The directory the names are relative to exists in the driver checkout,
    # and the seven files are in it -- otherwise the bootstrap would die on the
    # first name with a message about the wrong thing.
    patch_dir = ROOT / "vendor" / "UniXNU" / EXPECTED_PATCH_DIR
    if patch_dir.is_dir():
        present = sorted(path.name for path in patch_dir.glob("*.patch"))
        assert present == sorted(EXPECTED_PATCH_NAMES), present
