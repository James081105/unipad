#!/usr/bin/env python3
"""Report whether the console archive builder ran, compiled, and landed in the kernel.

Why this exists
---------------
The UniPad board gained one line -- ``build-arm-driver.py NativeVirtioConsole`` in
``vendor/UniXNU-qemu/board.toml``'s ``[kernel] archives`` -- and one flag,
``CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y`` in ``qemu_root_defconfig``.  Both are in the
pinned tree.  Neither is a measurement: the builder has never been *invoked* on a
macOS runner, so whether ``NativeVirtioConsole.cpp`` compiles at all is UNVERIFIED.

``tests/unit/test_kernel_archive_builder_guard.py`` already distinguishes "the
builder exists" from "the builder is invoked", on the Windows code-stage host, by
reading the pinned trees.  It cannot run a compiler.  This script is the half that
needs one: it is run on a macOS runner, after the builder has been run, and it
reports what happened.

The four outcomes, kept separate on purpose
-------------------------------------------
This project has been burned repeatedly by conflating them, so the verdict is one
of exactly four strings and nothing else:

``compiled``
    The builder ran and the archive exists.  Its size and sha256 are recorded.
``invoked-but-failed``
    The builder ran and did not produce the archive.  The log is the evidence.
``not-invoked``
    board.toml's entry did not cause a run.  This is a wiring bug, and it is
    reported as one rather than as a build failure.
``unmeasured``
    The runner could not check out, could not find the toolchain, could not obtain
    the compile record, and so on.  This is the only outcome that fails the job,
    because "we could not measure" must never be recorded as "it failed".

What this script deliberately does NOT claim
--------------------------------------------
"The archive builds" is not "the console enumerates in the guest" and it is not
"the console can send and receive".  Those are boot measurements.  Every report
this script writes carries that sentence verbatim, so a reader who stops at the
verdict line cannot mistake what was measured.

The silent-wrong-answer hazard
------------------------------
``build-arm-driver.py`` replaces the one argument of the recorded XNU compile
command that equals ``record['file']`` with the driver's own source path.  If the
record it was handed names a file that appears nowhere in ``arguments``, the
substitution never fires and clang is pointed at the XNU source instead -- and the
Mach-O audit at the end of the builder still passes, so the result is a green
message and the wrong archive.  ``source_is_the_driver`` below is the check for
exactly that, and it is computed from ``compile-command.json`` (the builder writes
the command it actually ran) rather than from the builder's exit status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tomllib
from pathlib import Path

#: The sentence every report carries.  Not a disclaimer: the distinction it draws
#: is the one that has cost this project the most, so it is data rather than prose.
NOT_CLAIMED = (
    "The archive builds is NOT the console enumerates in the guest and NOT the "
    "console can send and receive; both are boot measurements."
)

#: The four outcomes.  A verdict outside this set is a bug in this script.
OUTCOMES = ("compiled", "invoked-but-failed", "not-invoked", "unmeasured")

#: What a translation unit looks like on a clang command line.  Used to find the
#: source in the command the builder wrote, because "the argument that is not a
#: flag" is not a test: a clang command has hundreds of those.
SOURCE_SUFFIXES = (
    ".cpp", ".cc", ".cxx", ".c++", ".C", ".c", ".m", ".mm", ".s", ".S",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def board_archives(board: Path) -> tuple[list[list[str]], dict]:
    """(argv per entry, the board's own words) from board.toml.

    Parsed with ``tomllib``, which is what ``scripts/unixnu/kernel.py:56`` reads,
    rather than by scanning lines.  The line scan is what this used to do and it
    was wrong in a way that would have read as success: it stripped the quotes
    before the trailing comma, so every entry came back as
    ``build-arm-driver.py ArmDTPlatform"`` and ``shlex.split`` raised "No closing
    quotation".  A second parser of the board file is also the divergence class
    this project keeps hitting, so the driver's own parser is the only one used.

    The raw text is still read, for one thing tomllib cannot answer: whether the
    component is MENTIONED inside the archives block in a line that is not an
    entry.  A commented-out entry is the failure that looks exactly like success
    in a grep, so it is reported rather than skipped.
    """
    if not board.is_file():
        return [], {"entry_present": False, "reason": f"{board.as_posix()} is absent"}
    try:
        data = tomllib.loads(board.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return [], {"entry_present": False, "reason": f"board.toml does not parse: {error.__class__.__name__}"}
    section = data.get("kernel")
    if not isinstance(section, dict):
        return [], {"entry_present": False, "reason": "board.toml has no [kernel] table"}
    raw_entries = section.get("archives")
    if not isinstance(raw_entries, list) or not all(isinstance(item, str) for item in raw_entries):
        return [], {"entry_present": False, "reason": "[kernel] archives is missing or is not a list of strings"}
    argvs = []
    for item in raw_entries:
        try:
            argvs.append(shlex.split(item))
        except ValueError:
            argvs.append(item.split())
    return argvs, {"entry_count": len(argvs)}


def commented_mention(board: Path, component: str) -> str | None:
    """A line inside [kernel] archives that names the component but is not an entry."""
    if not board.is_file():
        return None
    inside = False
    for line in board.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("archives") and "[" in stripped:
            inside = True
            continue
        if inside:
            if "]" in stripped:
                break
            if component in stripped and not stripped.startswith('"'):
                return stripped
    return None


def board_entry_for_component(argvs: list[list[str]], component: str) -> dict:
    """The entry whose argv names ``component`` after the script name."""
    for number, argv in enumerate(argvs, 1):
        if len(argv) > 1 and component in argv[1:]:
            return {"entry": " ".join(argv), "argv": argv, "index": number}
    return {"entry": None, "argv": None, "index": None}


def invoked_by_driver(driver_log: Path | None, component: str) -> dict:
    """Did the upstream driver's own kernel step run this builder?

    ``scripts/unixnu/kernel.py:63`` logs ``kernel: archive N/M: <entry>`` for every
    entry it runs, and ``util.run(log_file=...)`` sends the builder's stdout to
    ``archive-NN-<script-stem>.log``.  Both are the driver's own output rather than
    a second implementation of "did it run", which is the whole point: a workflow
    that decides for itself whether the builder ran is guessing.
    """
    if driver_log is None or not driver_log.is_file():
        return {"ran": False, "reason": "no driver log", "line": None, "index": None, "of": None}
    text = driver_log.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        r"kernel:\s*archive\s+(\d+)/(\d+):\s*(\S.*?)\s*$", re.MULTILINE
    )
    for match in pattern.finditer(text):
        try:
            argv = shlex.split(match.group(3))
        except ValueError:
            argv = match.group(3).split()
        if component in argv[1:]:
            return {
                "ran": True,
                "reason": "the driver logged the entry it ran",
                "line": match.group(0).strip(),
                "index": int(match.group(1)),
                "of": int(match.group(2)),
            }
    return {"ran": False, "reason": "the driver log has no archive line naming " + component, "line": None, "index": None, "of": None}


def archive_log_for(driver_log_dir: Path | None, index: int | None) -> Path | None:
    """``archive-NN-<stem>.log`` -- named by index, because the stem repeats.

    ``scripts/unixnu/kernel.py:64`` names the per-builder log
    ``archive-{index:02d}-{Path(parts[0]).stem}.log``.  Four entries of the board
    list share the stem ``build-arm-driver``, so the index is the only thing that
    tells them apart; a path built from the component name would find nothing.
    """
    if driver_log_dir is None or index is None:
        return None
    candidate = driver_log_dir / f"archive-{index:02d}-build-arm-driver.log"
    return candidate if candidate.is_file() else None


def read_compile_command(tree: Path, component: str) -> dict:
    """The command the builder actually ran, from the builder's own record.

    ``build-arm-driver.py`` writes ``build/<out>/compile-command.json`` before it
    runs clang, so this is the command as executed rather than as reconstructed.

    The source is identified by its EXTENSION, not by "the first argument that is
    not a flag".  The first revision of this used the latter and read
    ``-I .../.cache/xnu-work/osfmk`` as the source, which made a correct build
    report that it had compiled the wrong translation unit -- measured on run
    41218257835, where the archive carried 59 NativeVirtioConsole symbols and the
    linked kernel carried the same 59.  A clang command has hundreds of non-flag
    arguments (every ``-D`` define is one), so "not a flag" is not a test.
    """
    out_names = {
        "NativeVirtioConsole": "native-virtio-console",
        "NativeUfs": "native-ufs",
    }
    out = out_names.get(component)
    if out is None:
        return {"present": False, "reason": f"no output directory known for {component}"}
    path = tree / "build" / out / "compile-command.json"
    if not path.is_file():
        return {"present": False, "reason": f"{path.as_posix()} is absent", "path": path.as_posix()}
    try:
        command = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"present": False, "reason": f"compile-command.json does not parse: {error.__class__.__name__}"}
    if not isinstance(command, list) or not command:
        return {"present": False, "reason": "compile-command.json is not a non-empty list"}
    sources = [
        value
        for value in command
        if isinstance(value, str) and value.lower().endswith(SOURCE_SUFFIXES)
    ]
    if not sources:
        return {
            "present": True,
            "path": path.as_posix(),
            "source": None,
            "source_basename": None,
            "reason": "no argument of the command ends in a source extension",
        }
    return {
        "present": True,
        "path": path.as_posix(),
        "source": sources[0],
        "source_basename": Path(sources[0]).name,
        "source_candidates": sources,
    }


def object_symbols(tree: Path, component: str, needle: str) -> dict:
    """Does the object the archive was built from carry the driver's own symbols?

    A second, independent check that the right translation unit was compiled: the
    builder's own Mach-O audit only looks at the architecture and the deployment
    target, which a compile of the wrong source would also satisfy.
    """
    out_names = {"NativeVirtioConsole": "native-virtio-console", "NativeUfs": "native-ufs"}
    out = out_names.get(component)
    if out is None:
        return {"checked": False, "reason": f"no output directory known for {component}"}
    obj = tree / "build" / out / f"{component}.o"
    if not obj.is_file():
        return {"checked": False, "reason": f"{obj.as_posix()} is absent"}
    for tool in ("nm", "xcrun"):
        argv = [tool] if tool == "xcrun" else [tool]
        if tool == "xcrun":
            argv += ["nm"]
        argv += ["-g", str(obj)]
        try:
            import subprocess

            result = subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError as error:
            return {"checked": False, "reason": f"cannot run {argv[0]}: {error.__class__.__name__}"}
        if result.returncode == 0:
            names = [line for line in result.stdout.splitlines() if needle in line]
            return {
                "checked": True,
                "tool": " ".join(argv[:2]),
                "matches": len(names),
                "sample": names[:5],
            }
    return {"checked": False, "reason": "no nm on PATH and xcrun nm failed"}


def kernel_symbols(kernel: Path | None, needle: str) -> dict:
    """Did the linked kernel pick the archive up?

    ``build-xnu-4k-baseline.sh`` adds ``-Wl,-force_load,<archive>`` to
    ``KERNEL_EXTRA_ARCHIVES`` when the flag is on, so a kernel linked with the
    console enabled carries the driver's symbols whether anything references them
    or not.  That is what makes this a real measurement of "it landed in the
    kernel" rather than of "the linker was told about it".
    """
    if kernel is None or not kernel.is_file():
        return {"checked": False, "reason": "no linked kernel to inspect"}
    for tool in ("nm",):
        import subprocess

        argv = [tool, "-g", str(kernel)]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError as error:
            return {"checked": False, "reason": f"cannot run nm: {error.__class__.__name__}"}
        if result.returncode != 0:
            return {"checked": False, "reason": f"nm exited {result.returncode}"}
        names = [line for line in result.stdout.splitlines() if needle in line]
        return {
            "checked": True,
            "tool": "nm -g",
            "matches": len(names),
            "sample": names[:5],
            "kernel": kernel.as_posix(),
            "kernel_sha256": sha256_of(kernel),
        }
    return {"checked": False, "reason": "no nm on PATH"}


def link_log_mentions(link_log: Path | None, archive: Path | None) -> dict:
    """Does the kernel link's own log name the archive on its command line?"""
    if link_log is None or archive is None or not link_log.is_file():
        return {"checked": False, "reason": "no link log or no archive path"}
    text = link_log.read_text(encoding="utf-8", errors="replace")
    name = archive.name
    hits = [line.strip() for line in text.splitlines() if name in line]
    return {"checked": True, "mentions": len(hits), "sample": hits[:3]}


def config_flags(config: Path | None) -> dict:
    """The two switches the real path needs, read out of the generated .config.

    ``CONFIG_XNU_BUILD_ARCHIVES`` is the one ``scripts/unixnu/kernel.py:54`` tests
    before it runs any builder at all; without it ``build_archives()`` returns
    immediately and board.toml's entry is inert.  ``CONFIG_XNU_NATIVE_VIRTIO_CONSOLE``
    is the one that adds ``-Wl,-force_load`` to the link.
    """
    wanted = ("CONFIG_XNU_BUILD_ARCHIVES", "CONFIG_XNU_NATIVE_VIRTIO_CONSOLE")
    if config is None or not config.is_file():
        return {"present": False, "values": {}, "missing": list(wanted), "path": None}
    values = {}
    for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.startswith("CONFIG_") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() in wanted:
            values[key.strip()] = value.strip()
    return {
        "present": True,
        "path": config.as_posix(),
        "values": values,
        "missing": [key for key in wanted if values.get(key) != "y"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", required=True)
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--board-toml", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--driver-log", type=Path, default=None)
    parser.add_argument("--driver-log-dir", type=Path, default=None)
    parser.add_argument("--kernel", type=Path, default=None)
    parser.add_argument("--link-log", type=Path, default=None)
    parser.add_argument("--record-provenance", type=Path, default=None)
    parser.add_argument("--invoked-directly", default="false")
    parser.add_argument(
        "--direct-argv",
        default="",
        help="the argv this workflow ran, space-separated. Recorded verbatim; it is a fact about the run, not something to parse.",
    )
    parser.add_argument(
        "--preflight",
        default="",
        help=(
            "comma-separated name=ok|failed pairs for the things that had to be true "
            "before the builder could run (tree assembled, compile record obtained, "
            "headers installed).  A failed one makes the verdict unmeasured, because "
            "'the runner could not set the build up' must never read as 'the driver "
            "does not compile'"
        ),
    )
    parser.add_argument("--elapsed-seconds", default="")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args()

    tree: Path = args.tree
    component = args.component
    problems: list[str] = []
    warnings: list[str] = []

    out_names = {"NativeVirtioConsole": "native-virtio-console", "NativeUfs": "native-ufs"}
    out_dir = out_names.get(component)
    archive = tree / "build" / out_dir / f"lib{component}.a" if out_dir else None

    # ---- 1. is the tree there at all? --------------------------------
    tree_present = tree.is_dir()
    if not tree_present:
        problems.append(f"the assembled tree {tree.as_posix()} is absent")

    # ---- 2. what does board.toml say? ---------------------------------
    argvs, board_meta = board_archives(args.board_toml)
    entry = board_entry_for_component(argvs, component)
    mention = commented_mention(args.board_toml, component)
    board_says = {
        "entry_present": entry["entry"] is not None,
        "entry": entry["entry"],
        "entry_index": entry["index"],
        "commented_out": mention,
        **board_meta,
    }
    if entry["entry"] is None:
        if mention:
            board_says["reason"] = (
                "the only line naming the component inside [kernel] archives is not an "
                f"entry ({mention!r}); a grep would read that as present"
            )
        else:
            board_says["reason"] = f"no [kernel] archives entry names {component}"

    # ---- 3. was it invoked, and by what? ------------------------------
    driver = invoked_by_driver(args.driver_log, component)
    invoked_directly = args.invoked_directly.strip().lower() in ("1", "true", "yes")
    direct_argv = (args.direct_argv or "").strip()
    per_builder_log = archive_log_for(args.driver_log_dir, driver["index"])

    invoked = driver["ran"] or invoked_directly
    if invoked_directly and not driver["ran"]:
        invocation = "workflow-direct"
    elif driver["ran"]:
        invocation = "driver-board-toml"
    else:
        invocation = "none"

    # ---- 4. did it compile? -------------------------------------------
    compiled = bool(archive and archive.is_file() and archive.stat().st_size > 0)
    archive_facts: dict = {"path": archive.as_posix() if archive else None, "exists": compiled}
    if compiled:
        archive_facts["size_bytes"] = archive.stat().st_size
        archive_facts["sha256"] = sha256_of(archive)

    command = read_compile_command(tree, component)
    expected_source = f"drivers/{component}/{component}.cpp"
    source_is_the_driver = bool(
        command.get("present")
        and command.get("source")
        and command["source"].replace("\\", "/").endswith(expected_source)
    )

    # Two independent checks that the right translation unit was compiled, and
    # they are NOT weighted equally.  The symbols are the ground truth: an object
    # carrying the driver's own class symbols is that driver, whatever the command
    # line says.  The source path is supporting evidence that explains it.
    #
    # The first revision made the source path the gate and reported
    # `invoked-but-failed` on a build whose archive and whose linked kernel both
    # carried 59 NativeVirtioConsole symbols (run 41218257835), because it had read
    # an `-I` argument as the source.  A detector that fails a correct build is
    # worse than no detector, so the symbol check is what decides.
    symbols = object_symbols(tree, component, component)
    object_is_the_driver = bool(symbols.get("checked") and symbols.get("matches", 0) > 0)

    if not object_is_the_driver:
        if symbols.get("checked"):
            problems.append(
                f"the object carries no symbol containing {component}; the archive is not this driver"
            )
        elif command.get("present") and not source_is_the_driver:
            # nm was unavailable, so the source path is all there is.  Say which
            # signal was used rather than presenting one as the other.
            problems.append(
                "nm was unavailable, and the compile command's source is "
                f"{command.get('source')!r}, not .../{expected_source}"
            )
        elif command.get("present") and command.get("source") is None:
            problems.append(
                "nm was unavailable and the compile command names no source file; "
                "there is no evidence about which translation unit was compiled"
            )

    # ---- 5. did it land in the kernel? --------------------------------
    flags = config_flags(args.config)
    kernel_facts = kernel_symbols(args.kernel, component)
    link_facts = link_log_mentions(args.link_log, archive)
    if args.kernel is None:
        linked = "not-attempted"
        linked_reason = (
            "no --kernel was given: this run did not link a kernel, and a link is "
            "the only thing that can show the archive landed in it"
        )
    elif not args.kernel.is_file():
        linked = "no"
        linked_reason = "the kernel path was given but no file is there; the link did not finish"
    elif kernel_facts.get("checked") and kernel_facts.get("matches", 0) > 0:
        linked = "yes"
        linked_reason = (
            f"the linked kernel carries {kernel_facts['matches']} symbol(s) naming "
            f"{component}; -Wl,-force_load puts them there whether or not anything "
            "references them"
        )
    elif kernel_facts.get("checked"):
        linked = "no"
        linked_reason = (
            f"the kernel exists but carries no symbol naming {component}: either the "
            "flag was off at link time or the archive was not force-loaded"
        )
    else:
        linked = "unmeasured"
        linked_reason = f"the kernel could not be inspected: {kernel_facts.get('reason')}"

    # ---- 6. the preconditions, and why a failed one is unmeasured ----
    preflight: dict = {}
    for item in (args.preflight or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, state = item.partition("=")
        preflight[name.strip()] = state.strip()
    failed_preflight = sorted(name for name, state in preflight.items() if state != "ok")

    # ---- 7. the verdict ------------------------------------------------
    if failed_preflight:
        # The only place unmeasured is decided.  Everything else here is a
        # measurement with an answer; this is the absence of one.
        verdict = "unmeasured"
        verdict_reason = (
            "the run could not set the build up ("
            + ", ".join(failed_preflight)
            + "), so the builder was never given a chance to fail"
        )
    elif not tree_present:
        verdict = "unmeasured"
        verdict_reason = "the assembled tree is absent, so nothing could be measured"
    elif not invoked:
        verdict = "not-invoked"
        verdict_reason = board_says.get("reason") or "nothing ran the builder"
    elif compiled:
        verdict = "compiled"
        verdict_reason = "the builder ran and the archive is on disk"
    else:
        verdict = "invoked-but-failed"
        verdict_reason = "the builder ran and produced no archive; the log is the evidence"

    if verdict == "compiled" and problems:
        verdict = "invoked-but-failed"
        verdict_reason = "; ".join(problems)

    if verdict not in OUTCOMES:  # pragma: no cover - a bug here is this script's
        raise SystemExit(f"internal error: verdict {verdict!r} is not one of {OUTCOMES}")

    # ---- 8. provenance of the compile record ---------------------------
    provenance = None
    if args.record_provenance is not None and args.record_provenance.is_file():
        try:
            provenance = json.loads(args.record_provenance.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            warnings.append(
                f"the compile-record provenance does not parse ({error.__class__.__name__}); "
                "the record's origin is unknown, not absent"
            )

    elapsed = None
    if args.elapsed_seconds.strip():
        try:
            elapsed = int(float(args.elapsed_seconds))
        except ValueError:
            warnings.append(f"elapsed seconds {args.elapsed_seconds!r} is not a number")


    report = {
        "schema_version": 1,
        "check": "kernel-console-archive",
        "component": component,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "not_claimed": NOT_CLAIMED,
        "tree": {"path": tree.as_posix(), "present": tree_present},
        "board_toml": {
            "path": args.board_toml.as_posix(),
            **board_says,
        },
        "invocation": {
            "mode": invocation,
            "invoked": invoked,
            "by_driver": driver,
            "by_workflow_directly": invoked_directly,
            "direct_argv": direct_argv,
            "per_builder_log": per_builder_log.as_posix() if per_builder_log else None,
        },
        "archive": archive_facts,
        "compile_command": command,
        "source_is_the_driver": source_is_the_driver,
        "object_is_the_driver": object_is_the_driver,
        "object_symbols": symbols,
        "config_flags": flags,
        "linked": linked,
        "linked_reason": linked_reason,
        "kernel": kernel_facts,
        "link_log": link_facts,
        "compile_record_provenance": provenance,
        "preflight": preflight,
        "elapsed_seconds": elapsed,
        "problems": problems,
        "warnings": warnings,
        "status": "fail" if verdict == "unmeasured" else ("fail" if problems else "pass"),
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # ---- 8. the human summary ------------------------------------------
    lines = [
        f"### {component}: `{verdict}`",
        "",
        f"- **verdict**: `{verdict}` -- {verdict_reason}",
        f"- **invoked**: `{invocation}`"
        + (f" (driver log: `{driver['line']}`)" if driver.get("line") else "")
        + (" (this workflow ran the builder itself)" if invocation == "workflow-direct" else ""),
        f"- **compiled**: `{compiled}`",
    ]
    if compiled:
        lines.append(
            f"  - `{archive.name}`: {archive_facts['size_bytes']} bytes, "
            f"sha256 `{archive_facts['sha256']}`"
        )
    lines.append(f"- **source_is_the_driver**: `{source_is_the_driver}`")
    lines.append(f"- **object_is_the_driver**: `{object_is_the_driver}` (the archive's own symbols; this is the check that decides)")
    lines.append(f"- **linked**: `{linked}` -- {linked_reason}")
    if flags.get("present"):
        lines.append(
            "- **config flags**: "
            + ", ".join(f"`{key}={value}`" for key, value in sorted(flags["values"].items()))
            + (f" (missing: {', '.join(flags['missing'])})" if flags["missing"] else "")
        )
    if per_builder_log:
        lines.append(f"- **per-builder log**: `{per_builder_log.as_posix()}`")
    if link_facts.get("checked") and link_facts.get("mentions") == 0 and linked == "yes":
        # Not a problem, and worth saying so: the link log does not name the
        # archive on its command line, so "the linker was told about it" is not
        # measurable from the log.  The symbol check is what measures it.
        lines.append(
            "- **link log**: does not name the archive; the evidence for `linked` is "
            "the kernel's own symbols, not the linker's command line"
        )
    if preflight:
        lines.append(
            "- **preconditions**: "
            + ", ".join(f"`{name}={state}`" for name, state in sorted(preflight.items()))
        )
    if provenance:
        origin = provenance.get("origin") or provenance.get("provenance") or "unknown"
        lines.append(f"- **compile record**: `{origin}`")
        if provenance.get("detail"):
            lines.append(f"  - {provenance['detail']}")
    if elapsed is not None:
        lines.append(f"- **elapsed**: {elapsed} s")
    lines += ["", f"> {NOT_CLAIMED}", ""]
    for warning in warnings:
        lines.append(f"- warning: {warning}")
    for problem in problems:
        lines.append(f"- problem: {problem}")

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"report: {args.report.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
