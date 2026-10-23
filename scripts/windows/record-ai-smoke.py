"""Record an actual AI/Step interface smoke-test run as M0 pass evidence.

PRD 6's M0 row asks for three things: pinned commits/tools/config, clean build
artifacts and hashes, and an **AI interface smoke test**.  The column that says
what does *not* count names "a script that exits 0" -- so a smoke test that
exists but was never run and recorded is not evidence, it is a file.

``tests/acceptance/test_m0_ai_interface_smoke.py`` asserts the contract live.
This script produces the *record* of a run against the same contract, which is
what the milestone gate's ``p.ai_interface_smoke`` probe looks for (it globs for
``ai-smoke*.json`` / ``ai-smoke*.log`` under the host view's roots).

The two deliberately share one implementation of the contract: the constants and
the runner are imported from the test module rather than restated here.  A second
copy would drift, and this project has already been bitten by that class often
enough (call-chain 7.4: 18 helper names across 5 PowerShell scripts, 11
diverged; standards R-1: three port validators, three ranges).

What it records, per command: the exit code, the envelope's status/ok, and
whether the envelope obeys the shape the contract requires.  What it refuses to
record: any absolute host path, because on this host such a path embeds the
Windows username and PRD 9.2 forbids it reaching a committed file -- the record
is written under ``artifacts/``, which is gitignored, and is scrubbed anyway.

Exit codes mirror unipad.errors.ExitCode: 0 ok, 1 failure, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from unipad.acceptance.milestones import default_view  # noqa: E402
from unipad.controller import cli  # noqa: E402
from unipad.errors import ErrorCode  # noqa: E402

# The contract under test, imported so there is exactly one spelling of it.
sys.path.insert(0, str(REPO_ROOT / "tests"))
from acceptance.test_m0_ai_interface_smoke import (  # noqa: E402
    ENVELOPE_KEYS,
    REQUIRED_ARGS,
    STATUSES,
    _cli_paths,
    _known_codes,
    _run,
)

RECORD_NAME = "ai-smoke.json"


def _git_commit() -> str:
    """The commit this record was produced against, so it can be re-attributed."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:  # pragma: no cover - git absent is not a smoke-test failure
        return "unknown"


def record(out_dir: Path) -> tuple[Path, dict]:
    """Run every command against an empty sandbox and write the record."""
    commands = list(cli.COMMANDS) + list(cli.LIFECYCLE_COMMANDS)
    known = _known_codes()
    rows = []

    with tempfile.TemporaryDirectory(prefix="unipad-ai-smoke-") as scratch:
        sandbox = Path(scratch)
        for command in commands:
            argv = [command, *_cli_paths(sandbox), *REQUIRED_ARGS.get(command, ())]
            try:
                exit_code, document = _run(argv)
            except Exception as exc:  # a crash is a finding, not a reason to stop
                rows.append(
                    {
                        "command": command,
                        "exit_code": None,
                        "status": None,
                        "ok": None,
                        "envelope_well_formed": False,
                        "code_in_taxonomy": False,
                        "error": f"{type(exc).__name__}",
                    }
                )
                continue

            error = document.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            rows.append(
                {
                    "command": command,
                    "exit_code": exit_code,
                    "status": document.get("status"),
                    "ok": document.get("ok"),
                    "envelope_well_formed": set(document) == set(ENVELOPE_KEYS),
                    "code_in_taxonomy": (
                        code is None if document.get("ok") else code in known
                    ),
                    "status_in_vocabulary": document.get("status") in STATUSES,
                    "code": code,
                }
            )

    payload = {
        "record": RECORD_NAME,
        "what": (
            "An AI/Step interface smoke test actually run and recorded: every "
            "command the controller advertises was driven against an empty "
            "sandbox and its JSON envelope checked against the PRD 9.1 contract."
        ),
        "commit": _git_commit(),
        "python": platform.python_version(),
        "platform": platform.system(),
        "commands_total": len(commands),
        "commands_well_formed": sum(1 for r in rows if r["envelope_well_formed"]),
        "commands_ok_status": sum(1 for r in rows if r.get("status_in_vocabulary")),
        "commands_taxonomy_clean": sum(1 for r in rows if r["code_in_taxonomy"]),
        "commands": rows,
        "note": (
            "Almost every command refuses against an empty sandbox; that is the "
            "point.  A smoke test of an interface proves the interface answers "
            "correctly when it cannot do the work, which is the state M0 is in. "
            "It is not evidence that a guest, a display or a disk exists."
        ),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / RECORD_NAME
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "artifacts" / "m0"),
        help="directory to write ai-smoke.json into (default: artifacts/m0)",
    )
    args = parser.parse_args(argv)

    target, payload = record(Path(args.out))

    # PRD 9.2: the record must not carry the host account, and an absolute path
    # is where it would come from.  Check the written bytes, not the intent.
    blob = target.read_text(encoding="utf-8")
    import getpass

    user = getpass.getuser()
    assert user not in blob, "the record named the Windows account"
    assert "Users\\" not in blob and "Users/" not in blob, (
        "the record carries an absolute path into a user profile"
    )

    print(
        "ai-smoke: {total} commands, {ok} well-formed envelopes, "
        "{tax} taxonomy-clean -> {path}".format(
            total=payload["commands_total"],
            ok=payload["commands_well_formed"],
            tax=payload["commands_taxonomy_clean"],
            path=target,
        )
    )
    return 0 if payload["commands_well_formed"] == payload["commands_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
