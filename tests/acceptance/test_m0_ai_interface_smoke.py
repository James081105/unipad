"""The AI/Step interface smoke test (PRD 6 M0 pass evidence).

M0's pass evidence is "固定提交、工具、配置；清洁构建产物及哈希；AI 接口冒烟测试",
and PRD 6 names what does *not* count: "README 可读、脚本返回 0、仅下载二进制" --
a readable README, a script that exits 0, and downloaded binaries.  This file is
the third item.  It is a smoke test of the interface an agent or a script drives,
which means it asserts the **contract**, not that any operation succeeded:

* every command the controller advertises emits **exactly one** JSON object on
  stdout (PRD 9.1).  A second document, a traceback or a bare ``print`` makes
  ``json.loads`` on the whole buffer raise, so the discipline is enforced by
  construction rather than by inspection;
* the envelope shape is uniform across successes, blocks and failures -- the
  same keys, the same types, ``ok`` consistent with ``exit_code``;
* every error code is inside the closed taxonomy (``errors.ErrorCode``) or is
  the documented controller-side catch-all, and never a bare class name;
* an unknown command is refused as ``unsupported_command`` with exit 2, not as
  a traceback and not as a silent success;
* no envelope carries an absolute host path, because on this host that path
  embeds the Windows username and PRD 9.2 forbids it reaching any renderer.

Every command is run against an **empty sandbox**, so almost all of them refuse.
That is the point: a smoke test of an interface proves the interface answers
correctly when it cannot do the work, which is the state M0 is actually in.  It
is not evidence that a guest, a display or a disk exists.

Field status: host-side behaviour proved on Windows with the real controller.
"""

from __future__ import annotations

import getpass
import io
import json
from pathlib import Path

import pytest

from unipad.controller import cli
from unipad.errors import ErrorCode

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE_CONFIG = REPO_ROOT / "config" / "machine.json"
SOURCES_LOCK = REPO_ROOT / "config" / "sources.lock.json"
BUDGET_CONFIG = REPO_ROOT / "config" / "budget.json"

#: The envelope keys every outcome must carry (cli.CommandOutcome.to_dict).
ENVELOPE_KEYS = (
    "schema_version",
    "unipad_version",
    "command",
    "status",
    "ok",
    "exit_code",
    "data",
    "error",
    "generated_at",
)

#: The closed status vocabulary.  A status outside this set is a new kind of
#: outcome nobody documented.
STATUSES = frozenset({"ok", "failed", "blocked"})

#: Error codes outside ``ErrorCode`` that the controller is allowed to emit,
#: each with the reason it exists.  ``internal_error`` is a catch-all for
#: unexpected exceptions and must never mask a known fault (cli.py:87-91).
EXTRA_CODES = {"internal_error": "controller-side catch-all for unexpected exceptions"}

#: The minimum arguments each command needs to get past argparse and reach its
#: own logic.  Without these the smoke test would only exercise the argument
#: parser, which is not the interface under test.
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "fetch": ("--run-id", "smoke-run"),
    "collect": ("--run", "smoke-run"),
    "restore": ("--checkpoint", "smoke-checkpoint"),
    "history": ("--all",),
}


def _cli_paths(tmp_path: Path) -> list[str]:
    """Controller options that keep every write inside ``tmp_path``."""
    return [
        "--runs-root",
        str(tmp_path / "runs"),
        "--budget-config",
        str(BUDGET_CONFIG),
        "--budget-usage",
        str(tmp_path / "budget-usage.json"),
        "--sources-lock",
        str(SOURCES_LOCK),
        "--machine-config",
        str(MACHINE_CONFIG),
        "--artifacts-root",
        str(tmp_path / "artifacts"),
    ]


def _run(argv: list[str]) -> tuple[int, dict]:
    """Run the controller and return ``(exit_code, envelope)``.

    ``json.loads`` on the whole buffer is the assertion that the command
    printed **exactly one** JSON object (PRD 9.1).  A second document, a
    traceback, a progress line or a bare print makes this raise.
    """
    buffer = io.StringIO()
    exit_code = cli.main(argv, stream=buffer)
    return exit_code, json.loads(buffer.getvalue())


def _known_codes() -> set[str]:
    """Every error code the taxonomy declares, plus the documented extras."""
    codes = {value for name, value in vars(ErrorCode).items() if not name.startswith("_")}
    codes |= set(EXTRA_CODES)
    return codes


# ---------------------------------------------------------------------------
# every advertised command answers, and answers as one JSON object
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", list(cli.COMMANDS) + list(cli.LIFECYCLE_COMMANDS))
def test_every_command_emits_exactly_one_well_formed_envelope(command, tmp_path):
    """PRD 9.1: the contract holds for all twelve commands, not just ``doctor``.

    The sandbox is empty, so most commands refuse.  What is asserted is that
    they refuse *as the interface*, with the same keys and a machine-readable
    code -- which is the only thing M0 can prove about an interface whose
    backing services do not exist yet.
    """
    argv = [command, *_cli_paths(tmp_path), *REQUIRED_ARGS.get(command, ())]

    exit_code, document = _run(argv)

    # --- exactly one object, with the uniform key set --------------------
    assert set(document) == set(ENVELOPE_KEYS), (
        f"{command} envelope keys drifted: "
        f"{sorted(set(document) ^ set(ENVELOPE_KEYS))}"
    )
    assert document["schema_version"] == cli.ENVELOPE_SCHEMA_VERSION
    assert document["command"] == command
    assert document["unipad_version"]

    # --- status and exit code agree with each other ----------------------
    assert document["status"] in STATUSES, document["status"]
    assert isinstance(document["ok"], bool)
    assert document["ok"] == (exit_code == int(cli.ExitCode.OK)), (
        f"{command}: ok={document['ok']} but exit_code={exit_code}"
    )
    assert document["exit_code"] == exit_code
    assert isinstance(document["data"], dict)

    # --- an error envelope is never present on a success -----------------
    if document["ok"]:
        assert document["error"] is None, f"{command} succeeded but carried an error"
    else:
        assert isinstance(document["error"], dict), f"{command} failed with no error object"

    # --- generated_at is a real timestamp, not a placeholder -------------
    assert isinstance(document["generated_at"], str) and len(document["generated_at"]) >= 19


def test_a_failure_envelope_names_a_code_inside_the_closed_taxonomy(tmp_path):
    """PRD 9.1 / §10: a refusal is machine-readable, and its code is one the
    taxonomy declares.

    The taxonomy is the contract an automation branches on.  A code outside it
    -- a bare class name, a free-text label -- is a branch nobody can write.
    """
    known = _known_codes()

    for command in list(cli.COMMANDS) + list(cli.LIFECYCLE_COMMANDS):
        argv = [command, *_cli_paths(tmp_path), *REQUIRED_ARGS.get(command, ())]
        exit_code, document = _run(argv)

        if document["ok"]:
            continue
        error = document["error"]
        assert isinstance(error, dict), f"{command} failed with no error object"
        code = error.get("code")
        assert isinstance(code, str) and code, f"{command} emitted no error code"
        assert code in known, f"{command} emitted {code!r}, which is outside the taxonomy"
        # A message a human can read, and details a machine can branch on.
        assert isinstance(error.get("message"), str) and error["message"]
        assert isinstance(error.get("details"), dict)


def test_the_two_error_envelopes_agree_about_what_they_carry(tmp_path):
    """Standards R-6: the argparse-shaped refusal and the controller-shaped
    refusal must not disagree about the envelope.

    ``restore`` refuses through argparse (which owns ``details.usage``) and
    ``build`` refuses through the controller's own blocked path.  Both are
    error envelopes, so both must carry the same keys and both must name a code
    inside the taxonomy -- a reader cannot branch on "known" differently
    depending on which layer happened to refuse.
    """
    argparse_shaped = _run(["restore", *_cli_paths(tmp_path), "--checkpoint", "smoke"])[1]
    controller_shaped = _run(["build", *_cli_paths(tmp_path), "--stage", "M4"])[1]

    for label, document in (
        ("argparse-shaped", argparse_shaped),
        ("controller-shaped", controller_shaped),
    ):
        assert not document["ok"], label
        error = document["error"]
        assert set(error) >= {"code", "message", "details"}, label
        assert error["code"] in _known_codes(), label
        assert isinstance(error["details"], dict), label

    # ... and they agree on status, not merely on both being failures.
    assert argparse_shaped["status"] == controller_shaped["status"]


# ---------------------------------------------------------------------------
# an unknown command is a refusal, not a crash and not a success
# ---------------------------------------------------------------------------


def test_an_unknown_command_is_refused_as_unsupported_command(tmp_path):
    """PRD 9.1: anything outside the command list is ``unsupported_command``.

    The two ways to get this wrong are a traceback (the interface dies) and a
    silent exit 0 (the interface lies).  Neither may happen.
    """
    exit_code, document = _run(["definitely-not-a-command", *_cli_paths(tmp_path)])

    assert exit_code == int(cli.ExitCode.USAGE)
    assert document["status"] == "failed"
    assert document["ok"] is False
    assert document["error"]["code"] == ErrorCode.UNSUPPORTED_COMMAND
    assert document["command"] == "definitely-not-a-command"

    # The refusal names what *is* supported, so a caller can self-correct.
    details = document["error"]["details"]
    supported = json.dumps(details)
    for command in cli.COMMANDS:
        assert command in supported, f"the refusal does not name {command!r}"


def test_no_command_at_all_is_a_refusal_not_a_crash(tmp_path):
    """An empty argv must not raise: the interface has to answer something."""
    buffer = io.StringIO()
    try:
        exit_code = cli.main([], stream=buffer)
    except SystemExit as exc:  # argparse's own "no command" path is acceptable
        assert exc.code not in (None, 0)
        return
    document = json.loads(buffer.getvalue())
    assert document["ok"] is False
    assert document["error"]["code"] in _known_codes()


# ---------------------------------------------------------------------------
# the interface never leaks the host account (PRD 9.2)
# ---------------------------------------------------------------------------
#
# This test used to carry a ``strict=True`` xfail whose reason named one leak
# (``doctor``'s ``sys.executable``) and predicted it would be found by the walk
# below.  It was.  The walk then found five more on the same run -- ``doctor``'s
# ``usage.usage_file``, ``verify`` and ``stop``'s ``error.details.runs_root``,
# ``collect``'s ``error.details.path``, ``restore``'s ``error.details.searched``
# and ``history``'s ``data.ledger`` -- because it walks EVERY command's envelope
# rather than the one field that was known to be wrong.  Patching six call sites
# would have fixed six call sites; the fix went into ``_emit``, the single point
# every envelope is serialised through, so the seventh one is caught here too.
#
# The marker is gone rather than left as documentation: a strict xfail that
# XPASSes is reported as a failure, which is the signal to delete it, and this
# is that signal being taken.


def test_no_envelope_carries_the_windows_username(tmp_path):
    """PRD 9.2: an absolute host path embeds the username, and every envelope is
    rendered by something.

    The inventory leak this project already had once (``sys.executable`` reaching
    a committed file) is the reason this walks the whole envelope of every
    command rather than the one field that was wrong: the next leak will not be
    in the same place.  It found a second instance on the first run.
    """
    username = getpass.getuser()
    assert username, "the test cannot prove anything without a username to look for"

    for command in list(cli.COMMANDS) + list(cli.LIFECYCLE_COMMANDS):
        argv = [command, *_cli_paths(tmp_path), *REQUIRED_ARGS.get(command, ())]
        buffer = io.StringIO()
        cli.main(argv, stream=buffer)
        blob = buffer.getvalue()

        assert username not in blob, f"{command} named the Windows account on stdout"
        assert "Users\\" not in blob and "Users/" not in blob, (
            f"{command} emitted an absolute path into a user profile"
        )


# ---------------------------------------------------------------------------
# --help stays reachable without corrupting the JSON contract
# ---------------------------------------------------------------------------


def test_help_is_reachable_and_stays_inside_the_json_contract(tmp_path):
    """``--help`` is a real controller command, not argparse's text dump.

    Measured: ``cli.main(["--help"])`` emits ``command: "<help>"`` as one JSON
    envelope whose ``data.help`` carries the usage text.  That is stricter than
    argparse's own help -- it keeps the one-JSON-object discipline instead of
    breaking it -- so the assertion is that ``--help`` obeys the *same* contract
    as every other command, and that the advertised surface matches the real
    one (standards R-6's other half).
    """
    exit_code, document = _run(["--help", *_cli_paths(tmp_path)])

    assert exit_code == 0
    assert document["command"] == "<help>"
    assert document["status"] == "ok"
    assert document["ok"] is True
    assert set(document) == set(ENVELOPE_KEYS)

    text = document["data"]["help"]
    assert isinstance(text, str) and "usage: unipad" in text
    for command in list(cli.COMMANDS) + list(cli.LIFECYCLE_COMMANDS):
        assert command in text, f"--help does not advertise {command!r}"

    # The epilogue is the contract statement; it must name both groups, or a
    # caller cannot tell which commands are PRD 9.1 and which are P1 (3).
    assert "PRD 9.1 commands" in text
    assert "PRD 3.2 P1 (3)" in text

    # ... and help is the one surface that must not leak the account either.
    assert getpass.getuser() not in json.dumps(document, ensure_ascii=False)
