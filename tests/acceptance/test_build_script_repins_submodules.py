"""``run_driver`` must put the module submodules back after every driver call.

Why this file exists
--------------------
``config/sources.lock.json`` pins ``drivers/NativeMobileDisplay`` one commit
*behind* UniXNU's own gitlink, because the pinned device repository cannot
compile the gitlink's version (measured on run 40187668943: ``only virtual
member functions can be marked 'override'``).  The lock is the authority, so
the checkout has to be moved to the locked commit -- and it has to be moved
**every time the driver touches the submodules**, because every ``unixnu``
subcommand that reaches ``prepare()`` runs ``git submodule update --init
--recursive`` (``cli.py:79-86`` -> ``feeds.py:159-165``), which resets every
module submodule to the gitlink's commit.

Measured on run 40209085545, which is the run that got the whole owner root
chain to ``os_root_status=pass`` and then died in the *second* build pass::

    Rebuild the owned Legacy archive after changing
    drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp

The chain had built the Legacy display archive at the locked ``eb9c949``; the
second pass's ``unixnu build kernel`` reset the checkout to the gitlink's
``c031d882``; and ``build-xnu-4k-baseline.sh:735`` refused to link.  The
divergence was applied in the acquisition step and once more before the chain,
and neither of those could see a reset that happened afterwards.

What is proven here
-------------------
That the re-pin is inside ``run_driver`` -- so it happens for every driver
subcommand, whatever it is, instead of being remembered by each caller -- and
that it delegates to ``unipad.host.source_pins`` rather than carrying a second
implementation of "which commit".  The module's own behaviour (moves a diverging
checkout, leaves an agreeing one, refuses an unfetchable commit) is proven
against real git repositories in ``test_acquire_submodule_divergences.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"
REAL_LOCK = ROOT / "config" / "sources.lock.json"
MODULE = "unipad.host.source_pins"

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH"),
]


def _function(name: str) -> str:
    """One function of ``build-macos.sh``, exactly as the shell reads it."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def test_the_repin_is_inside_run_driver_not_in_its_callers() -> None:
    """The wiring, and the reason it is there rather than at each call site.

    A caller that has to *remember* to re-apply the pin is the same defect one
    level up: the chain step remembered, and the second build pass undid it.
    The call is what carries this; a comment promising it does not.
    """
    run_driver = _function("run_driver")
    assert "pin_module_submodules" in run_driver, (
        "run_driver does not put the module submodules back, so every "
        "`unixnu build`/`fetch` -- which runs `git submodule update --init "
        "--recursive` and resets them to the driver's gitlink -- leaves the "
        "tree at commits the lock does not name. Measured on run 40209085545."
    )
    # ...and it has to come AFTER the driver runs, or there is nothing to
    # repair.  Matching the bare name would find the function definition
    # further up the file, which is the mistake the first draft of this
    # assertion made.
    call = run_driver.index('pin_module_submodules "$step"')
    assert run_driver.index('"${cmd[@]}"') < call, (
        "the module submodules are pinned before the driver runs, so a reset "
        "the driver performs is never repaired"
    )
    assert call < run_driver.rindex("return"), (
        "the module submodules are pinned after run_driver returns, so a "
        "failure of the driver would skip it"
    )


def test_the_repin_delegates_rather_than_reimplementing() -> None:
    """One implementation of "which commit", because two is the defect class.

    The workflow's acquisition step applies the same pins.  If ``build-macos.sh``
    grew its own git-fetch-and-checkout, the two would drift and the second
    place that knows a sha would be this repository's own.
    """
    helper = _function("pin_module_submodules")
    assert MODULE in helper, (
        f"pin_module_submodules does not call {MODULE}, so the pin logic lives "
        "in more than one place"
    )
    # The git operations, not the word: the function is *named*
    # pin_module_submodules, so "submodule" is in it by construction, and a
    # forbidden list that included it failed on the function's own name.
    for forbidden in ("git ", "rev-parse", "FETCH_HEAD", "checkout", "submodule update"):
        assert forbidden not in helper, (
            f"pin_module_submodules contains {forbidden!r}, so it is a second "
            f"implementation of what {MODULE} already does"
        )
    # A pin that cannot be applied is a refusal, not a warning: a build that is
    # not the pinned one must not be allowed to look like a result.
    assert "die " in helper, (
        "a submodule that cannot be moved to the locked commit is not a "
        "refusal, so the build would continue at a commit the lock does not name"
    )


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(directory: Path, message: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    _git(directory, "add", "-A")
    subprocess.run(
        ["git", "-C", str(directory), "commit", "-q", "--allow-empty", "-m", message],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return _git(directory, "rev-parse", "HEAD")


def _workspace(tmp_path: Path) -> tuple[Path, str, str]:
    """A driver checkout whose submodule sits at the *gitlink* commit.

    ``(driver_root, gitlink, locked)``.  Both commits are real commits in a real
    local repository, so the ``git fetch --depth=1 origin <sha>`` the module
    performs resolves without a network.
    """
    driver = tmp_path / "vendor" / "UniXNU"
    (driver / "drivers").mkdir(parents=True)
    _git(driver, "init", "-q")
    (driver / "README.md").write_text("driver checkout\n", encoding="utf-8")
    _commit(driver, "driver")

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / "NativeLegacyDisplay.cpp").write_text("// the gitlink's version\n", encoding="utf-8")
    gitlink = _commit(upstream, "the gitlink's version")
    (upstream / "NativeLegacyDisplay.cpp").write_text("// the locked version\n", encoding="utf-8")
    locked = _commit(upstream, "the locked version")

    # A clone is not a submodule: without .gitmodules and a gitlink entry,
    # `git submodule update --init --recursive` is a NO-OP, and the first draft
    # of this workspace passed its "survives the reset" assertion with the
    # protection removed because there was no reset to survive.  So the
    # submodule is registered the way the driver repository registers it, with a
    # .gitmodules file and a gitlink recording `gitlink`.
    submodule = driver / "drivers" / "NativeMobileDisplay"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(submodule)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(submodule, "checkout", "-q", "--detach", gitlink)
    modules = (
        '[submodule "drivers/NativeMobileDisplay"]\n'
        "\tpath = drivers/NativeMobileDisplay\n"
        f"\turl = {upstream.as_posix()}\n"
    )
    (driver / ".gitmodules").write_text(modules, encoding="utf-8")
    _git(driver, "add", ".gitmodules")
    _commit(driver, "register the module submodule")
    # The upstream is a local directory, and git refuses `file` transport for
    # submodules by default (the CVE-2022-39253 mitigation).  Without this the
    # first `submodule update` that actually has to FETCH dies with
    # "fatal: transport 'file' not allowed" -- which exits 128 and leaves the
    # checkout where it was, so the "survives the reset" assertion would pass
    # with the protection removed.  The real driver's submodules are https; this
    # is what makes a local fixture behave like one.  It has to be set in BOTH
    # repositories, because the fetch runs inside the submodule.
    _git(driver, "config", "protocol.file.allow", "always")
    _git(submodule, "config", "protocol.file.allow", "always")
    _git(
        driver,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{gitlink},drivers/NativeMobileDisplay",
    )
    _commit(driver, "the gitlink records the gitlink's version")
    # Sanity: the reset this test depends on must really be a reset.  Without
    # this the assertion below would pass for the wrong reason.
    _git(driver, "submodule", "update", "--init", "--recursive")
    assert _git(submodule, "rev-parse", "HEAD") == gitlink, (
        "the fixture's submodule is not registered, so `submodule update` "
        "cannot move it and the protection under test is never exercised"
    )
    _git(submodule, "checkout", "-q", "--detach", gitlink)
    return driver, gitlink, locked


def _lock(tmp_path: Path, commit: str, gitlink: str) -> Path:
    payload: dict[str, Any] = (
        json.loads(REAL_LOCK.read_text(encoding="utf-8"))
        if REAL_LOCK.is_file()
        else {"schema_version": 1}
    )
    payload["submodules"] = [
        {
            "path": "drivers/NativeMobileDisplay",
            "commit": commit,
            "gitlink_commit": gitlink,
        }
    ]
    lock = tmp_path / "sources.lock.json"
    lock.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return lock


def test_the_command_the_shell_builds_repairs_a_reset_submodule(tmp_path: Path) -> None:
    """The command itself, run against a checkout the driver has just reset.

    This is the shape of the failure: the driver runs, the submodule is back at
    the gitlink, and the next thing that reads the sources is not the thing that
    reset them.  The command ``pin_module_submodules`` builds is executed here
    as a real process against a real git workspace, with ``PYTHONPATH`` set the
    way ``run_unipad`` sets it -- so what is proven is that the shell's
    invocation works, not that a similar one would.
    """
    driver, gitlink, locked = _workspace(tmp_path)
    submodule = driver / "drivers" / "NativeMobileDisplay"
    assert _git(submodule, "rev-parse", "HEAD") == gitlink

    # What the driver does: reset every module submodule to the gitlink.
    _git(driver, "submodule", "update", "--init", "--recursive")
    assert _git(submodule, "rev-parse", "HEAD") == gitlink

    lock = _lock(tmp_path, locked, gitlink)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE,
            "--lock",
            str(lock),
            "--driver",
            str(driver),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert result.returncode == 0, (
        "the command pin_module_submodules builds did not succeed:\n"
        + (result.stdout + result.stderr)[-2000:]
    )
    assert _git(submodule, "rev-parse", "HEAD") == locked, (
        "the reset submodule was not put back at the locked commit, so the "
        "next build stage reads sources the lock does not name"
    )
    # --quiet means a second run prints nothing and changes nothing.
    again = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE,
            "--lock",
            str(lock),
            "--driver",
            str(driver),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert again.returncode == 0, again.stdout + again.stderr
    assert again.stdout == "", (
        f"an already-pinned tree printed something, so the common path is not "
        f"quiet: {again.stdout!r}"
    )
    assert _git(submodule, "rev-parse", "HEAD") == locked


def test_a_protected_submodule_survives_the_drivers_own_reset(tmp_path: Path) -> None:
    """The half that actually fixes it, against the command doing the reset.

    Re-applying the pin *after* a driver call is not enough, and that was
    measured rather than argued: run 40217489782's log shows the re-pin working
    correctly (``c031d882 -> eb9c949``) at 17:50:36.310, one second AFTER
    ``build-xnu-4k-baseline.sh:735`` had already refused at 17:50:35.288.  The
    reset is the driver's FIRST act (``cli.py:79-86`` -> ``prepare()`` ->
    ``feeds.update_submodules``), so everything the call builds afterwards --
    the archives and the link -- sees the gitlink's commit no matter what runs
    once it returns.

    ``submodule.<path>.update=none`` is the one lever that operates at the right
    moment, because it is read by the very command doing the reset.  This test
    runs that command -- ``git submodule update --init --recursive``, exactly as
    ``feeds.py:159-165`` spells it -- and asserts the checkout does not move.
    """
    driver, gitlink, locked = _workspace(tmp_path)
    submodule = driver / "drivers" / "NativeMobileDisplay"
    lock = _lock(tmp_path, locked, gitlink)

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", MODULE, "--lock", str(lock), "--driver", str(driver)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert result.returncode == 0, (
        "the command pin_module_submodules builds did not succeed:\n"
        + (result.stdout + result.stderr)[-2000:]
    )
    assert _git(submodule, "rev-parse", "HEAD") == locked

    # The driver's own reset, spelled the way feeds.py:164 spells it.
    _git(driver, "submodule", "update", "--init", "--recursive")
    assert _git(submodule, "rev-parse", "HEAD") == locked, (
        "the driver's `submodule update` moved the protected submodule back to "
        f"{gitlink}, so every archive and the link see the gitlink's commit. "
        "Measured on runs 40209085545 and 40217489782 as "
        "build-xnu-4k-baseline.sh:735's refusal to link."
    )

    # ...and it is still protected after a second application, which is what
    # run_driver does on every driver call.
    again = subprocess.run(
        [sys.executable, "-m", MODULE, "--lock", str(lock), "--driver", str(driver)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert again.returncode == 0, again.stdout + again.stderr
    _git(driver, "submodule", "update", "--init", "--recursive")
    assert _git(submodule, "rev-parse", "HEAD") == locked


def test_the_buttons_sink_stub_satisfies_the_reference_the_link_reported() -> None:
    """The stub must define exactly the symbol, with the driver's own signature.

    Measured on run 40222638377, the first run to reach the kernel link at all::

        Undefined symbols for architecture arm64:
          "_native_buttons_set_sink", referenced from:
              NativeHidInput::start(IOService*) in ...(NativeHidInput.late.o)

    ``NativeHidInput.cpp:15`` declares it ``weak_import`` and guards every use
    with ``&symbol``, which is the idiom for a symbol a board may not provide.
    Its sibling ``native_touch_set_sink`` is DEFINED by the device repository's
    package (``TouchState.cpp:23``), so it resolves -- and that contrast is what
    says the problem is the missing definition rather than the attribute.  This
    board has no button driver, so a no-op body is behaviourally identical to
    the symbol being absent.

    The stub's source is extracted from ``build-macos.sh`` rather than retyped,
    so this is about the bytes the runner compiles.  What is checked is the part
    the linker cares about: the name, the arity and the parameter types, because
    a definition with a different signature would not satisfy a C++-mangled
    caller's reference at all.
    """
    hid = VENDOR_DRIVER / "drivers" / "NativeHidInput" / "NativeHidInput.cpp"
    if not hid.is_file():
        pytest.skip("vendor/UniXNU is not present in this checkout")
    source = _stub_source()
    declaration = next(
        line
        for line in hid.read_text(encoding="utf-8").splitlines()
        if "native_buttons_set_sink" in line and "weak_import" in line
    )
    # The driver's own parameter list, so the comparison is against it.
    assert "void (*)(void *, uint32_t), void *" in declaration, declaration

    definitions = [
        line
        for line in source.splitlines()
        if "native_buttons_set_sink" in line and line.rstrip().endswith("{")
    ]
    assert len(definitions) == 1, (
        "the stub defines the sink more than once, or not at all: "
        f"{definitions}"
    )
    assert "void native_buttons_set_sink(" in definitions[0], (
        f"the stub does not define native_buttons_set_sink: {definitions[0]!r}"
    )
    # uint32_t and unsigned int are the same width on arm64, and the C signature
    # the driver declares uses uint32_t; a mismatch here would be a different
    # symbol to the linker, not a compatible one.
    assert "unsigned int" in source or "uint32_t" in source, source
    assert "void *context" in source, source
    # A body that does nothing is the point: the sink is called and nothing
    # happens, which is what "this board has no buttons" means.
    assert "(void)sink" in source and "(void)context" in source, (
        "the stub uses its arguments, so it is not the no-op the driver's "
        "optional-sink contract asks for"
    )


def _stub_source() -> str:
    """The C ``build_buttons_sink_stub`` writes, extracted from the shell."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("cat >\"$source\" <<'STUB'\n") + len("cat >\"$source\" <<'STUB'\n")
    end = text.index("\nSTUB\n", start)
    return text[start:end]


VENDOR_DRIVER = ROOT / "vendor" / "UniXNU"

def test_the_sink_is_named_in_the_generated_config_not_only_in_the_environment() -> None:
    """The interface is the generated .config, and that was measured.

    The first attempt exported XNU_EXTRA_LD_ARCHIVES, which
    build-xnu-4k-baseline.sh:732 does append to the kernel's archives -- and run
    40229046208 built the archive, logged "buttons sink: libButtonsSink.a
    built", and still failed the link on the same symbol.  The config's own
    CONFIG_XNU_EXTRA_LD_ARCHIVES (qemu_defconfig:29 names libLinkStubs.a)
    becomes that environment variable, so the export was overwritten.

    Two things this pins: the archive is APPENDED to the value the board already
    carries -- replacing it would drop libLinkStubs.a and trade one
    undefined-symbol failure for three (_mockfs_mountroot, _mockfs_vfsops,
    _mockfs_vnodeop_opv_desc, measured on run 40067448940) -- and the naming
    happens after `unixnu defconfig` has written the file, because the file
    being edited is the generated one.
    """
    helper = _function("name_buttons_sink_in_config")
    assert "CONFIG_XNU_EXTRA_LD_ARCHIVES=" in helper, (
        "the helper does not name the config variable the board already uses"
    )
    assert "work_dir)/.config" in helper, (
        "the helper edits something other than the generated out/<device>/.config"
    )
    # ...and it appends rather than replaces, which is the whole difference
    # between adding an archive and dropping one.
    appended = 'lines[index] = f'
    assert "line[len(key):]" in helper and appended in helper, (
        "the helper does not append to the value the board already carries, so "
        "it would replace libLinkStubs.a"
    )
    assert "{value} {relative}" in helper, (
        "the appended value does not keep the board's own token, so "
        "libLinkStubs.a would be dropped"
    )
    assert "CONFIG_DIVERGENCE" in helper, (
        "the divergence from the pin is not recorded, so the manifest cannot "
        "say the generated .config was changed"
    )

    step = _function("step_build_kernel")
    defconfig = step.index("run_driver \"$name\" 'defconfig'")
    sink = step.index("name_buttons_sink_in_config")
    assert defconfig < sink, (
        "the button sink is named in the config before `unixnu defconfig` has "
        "written it, so there is no file to edit"
    )
    assert sink < step.index("run_driver \"$name\" 'build' 'kernel'"), (
        "the button sink is named after the kernel build, which is too late"
    )
    # ...and the stub itself is built under the tree, so the config can name it
    # with the board's own {tree} placeholder rather than a second spelling.
    stub = _function("build_buttons_sink_stub")
    assert "tree_dir)/build/buttons-sink" in stub, (
        "the stub is not built under the tree, so the config cannot name it "
        "with the {tree} placeholder qemu_defconfig:29 uses"
    )
    assert "{tree}/build/buttons-sink/libButtonsSink.a" in helper, (
        "the config value does not use the board's {tree} placeholder, so it "
        "is a second spelling of a directory the board already names"
    )

