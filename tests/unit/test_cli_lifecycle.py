"""CLI surface for the install lifecycle (PRD 3.2 P1 (3) on top of PRD 9.1).

PRD 3.2 P1 asks for 应用卸载、更新、安装历史和清晰的错误分类.  The library half
of that lives in :mod:`unipad.ipa.lifecycle`; this file proves the
*controller* half: the three commands that let an operator ask those questions
of a real run root and get one machine-readable object back.

What is being defended here:

* **The ledger is a production path, not a test path.**  It lives under the runs
  root, so the same command that records an install is the command a later
  ``history`` reads, and a test that writes to ``tmp_path`` never reaches the
  developer's own ledger.
* **A missing bundle is a true answer, not an error.**  ``history --bundle x``
  on a bundle nothing ever recorded returns an empty history with exit 0.  A
  question with a known answer must not be reported as a failure.
* **Refusal is a structured envelope, never a traceback.**  Uninstalling
  something that is not installed, or updating something that was never
  installed, is a state mismatch: the command says so with a stable code and a
  concrete reason, and the ledger is left untouched.
* **A stage that never happened records nothing.**  A blocked install and a
  refused update happen before the transfer, so they never touched a bundle and
  there is no transition to record.  What they must never do is leave the
  operator with nothing, so the log the envelope names exists and says which
  probe failed -- that is the difference between "records nothing" and "left no
  trace", and it is asserted here rather than assumed.
* **A package defect is a package defect.**  An IPA with no
  ``CFBundleIdentifier`` is refused by the precheck with ``invalid_package``,
  and that check runs before the require-installed one, so ``update`` reports
  the defect instead of blaming the install state.
* **A transfer is not an install.**  PRD 9.1 line 391: a successful file
  transfer must not return ``installed``.  The lifecycle entry inherits whatever
  stage the chain actually reached, so a ``transferred`` entry never makes the
  bundle present.
* **No secrets (PRD 9.2).**  :meth:`LifecycleEntry.to_dict` omits
  ``error_message`` on purpose; the CLI asserts that omission rather than
  trusting it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from unipad import __version__
from unipad.controller import cli
from unipad.errors import ErrorCode, ExitCode, UniPadError
from unipad.ipa import lifecycle as lifecycle_module
from unipad.ipa.lifecycle import InstallLedger, LifecycleAction
from unipad.models import InstallResult
from tests.fixtures.ipa_builder import DEFAULT_BUNDLE_ID, build_ipa

#: The nine commands of PRD 9.1 -- unchanged by this module.
PRD_9_1_COMMANDS: frozenset[str] = frozenset(
    {
        "doctor",
        "build",
        "fetch",
        "start",
        "install",
        "verify",
        "stop",
        "collect",
        "restore",
    }
)

#: The three commands PRD 3.2 P1 (3) adds on top of the nine.
LIFECYCLE_COMMANDS: frozenset[str] = frozenset({"history", "uninstall", "update"})

#: The one command PRD 3.2 P1 (2) adds on top of those.  It is a separate
#: surface for the same reason the lifecycle commands are: the closed set this
#: test pins must stay exactly the declared surfaces, so a new surface has to
#: be *added* to it rather than quietly absorbed into ``COMMANDS``.
MATRIX_COMMANDS: frozenset[str] = frozenset({"matrix"})

#: The envelope keys every command must carry (PRD 9.1).
ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "unipad_version",
        "command",
        "status",
        "ok",
        "exit_code",
        "data",
        "error",
        "generated_at",
    }
)

#: The keys of one ledger entry as the CLI must render it.  ``error_message``
#: is absent by contract (PRD 9.2).
ENTRY_KEYS: frozenset[str] = frozenset(
    {
        "entry_id",
        "bundle_id",
        "action",
        "package_sha256",
        "stage",
        "recorded_at",
        "supersedes",
        "reason",
        "error_code",
        "error_class",
        "trust_mode",
    }
)

BUDGET_CONFIG: dict[str, object] = {
    "schema_version": 1,
    "per_hypothesis_max_experiments": 3,
    "same_failure_signature_max": 3,
    "max_actions_minutes_per_week": 0,
    "max_model_tokens_per_session": None,
    "max_local_run_minutes_per_experiment": 45,
}


class CliResult:
    """One CLI invocation: exit code, parsed envelope and raw stdout."""

    def __init__(self, exit_code: int, document: dict[str, Any], raw: str) -> None:
        self.exit_code = exit_code
        self.document = document
        self.raw = raw

    @property
    def status(self) -> str:
        return str(self.document["status"])

    @property
    def error_code(self) -> str | None:
        error = self.document.get("error")
        return None if not error else str(error["code"])

    @property
    def data(self) -> dict[str, Any]:
        return dict(self.document.get("data") or {})

    @property
    def details(self) -> dict[str, Any]:
        error = self.document.get("error") or {}
        return dict(error.get("details") or {})


def _decode_one_object(text: str) -> dict[str, Any]:
    """Parse ``text`` and assert it holds exactly one JSON document.

    PRD 9.1: exactly one JSON object per command.  ``raw_decode`` proves the
    remainder after the first document is whitespace only, so a traceback or a
    child process writing to stdout fails the test.
    """
    assert text.strip(), "the CLI must print a JSON object"
    document, end = json.JSONDecoder().raw_decode(text.lstrip())
    assert text.lstrip()[end:].strip() == "", "stdout must contain exactly one JSON object"
    assert isinstance(document, dict)
    return document


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    """A tmp_path run root plus the controller-side config files."""
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps(BUDGET_CONFIG), encoding="utf-8")
    machine = tmp_path / "machine.json"
    machine.write_text(
        json.dumps({"name": "ipad-tcg", "accel": "tcg", "cores": 4, "memory_mb": 3072}),
        encoding="utf-8",
    )
    lock = tmp_path / "sources.lock.json"
    lock.write_text(json.dumps({"frozen": False}), encoding="utf-8")
    return {
        "root": tmp_path,
        "runs": runs,
        "budget": budget,
        "machine": machine,
        "lock": lock,
    }


def run_cli(argv: list[str], workspace: dict[str, Path]) -> CliResult:
    """Invoke ``cli.main`` with the workspace paths and capture stdout."""
    full = [
        "--runs-root",
        str(workspace["runs"]),
        "--budget-config",
        str(workspace["budget"]),
        "--machine-config",
        str(workspace["machine"]),
        "--sources-lock",
        str(workspace["lock"]),
        "--artifacts-root",
        str(workspace["root"] / "artifacts"),
        *argv,
    ]
    buffer = io.StringIO()
    exit_code = cli.main(full, buffer)
    return CliResult(exit_code, _decode_one_object(buffer.getvalue()), buffer.getvalue())


def run_dir_of(workspace: dict[str, Path], result: CliResult, *, named: str) -> Path:
    """The run directory this invocation created, found without the envelope.

    ``_emit`` redacts every string in the envelope (PRD 9.2: an absolute path
    under the runs root embeds the Windows account name), so
    ``Path(result.data["run_dir"])`` and ``Path(result.data["log_path"])`` are
    no longer paths -- they are ``<redacted:user_path>/...``.  That redaction is
    the correct behaviour and must not be undone here, which means a test that
    wants to read the evidence has to find the directory the way an operator
    would: by looking for the file the envelope names, under the runs root.

    ``named`` is the file the envelope claims to have written.  It is asserted to
    still be named there, redacted or not, *and* used as the search key.  Without
    the naming assertion the test would pass even if the envelope stopped naming
    the file at all -- a silent regression this file is specifically guarding
    against, because "the envelope names this file, so it must exist" is the
    contract under test.

    The search is by ``named`` rather than "the only directory here" because the
    runs root is shared: ``InstallLedger`` keeps its own ``lifecycle/`` directory
    beside the run directories, so "exactly one directory" is not true and
    picking the newest would be a guess.  Raises when the file is not found in
    exactly one directory, so a command that writes its evidence elsewhere fails
    here instead of having the test quietly read some other run's evidence.
    """
    # The envelope must still NAME the file it wrote, redacted or not.
    assert Path(result.data["log_path"]).name == named, result.data["log_path"]
    assert named in result.data["log_path"], result.data["log_path"]
    everything = sorted(path for path in workspace["runs"].iterdir() if path.is_dir())
    created = [path for path in everything if (path / named).is_file()]
    assert len(created) == 1, (
        f"expected exactly one run directory under {workspace['runs']} holding "
        f"{named}, found {[p.name for p in created]} among "
        f"{[p.name for p in everything]}: the test cannot tell which run's "
        "evidence it is inspecting"
    )
    return created[0]


def envelope_names(redacted: str, expected: Path) -> None:
    """Assert a redacted envelope string still names the file ``expected`` is.

    The overridden ``--ledger`` / ``--matrix`` paths are ``tmp_path``-rooted, so
    after redaction the envelope carries ``<redacted:absolute_path>/...`` and
    comparing it to ``expected.as_posix()`` would fail on the redaction rather
    than on a defect.  What survives redaction -- and what an operator actually
    relies on -- is the *final component*: the envelope names the same file the
    override named.  The caller asserts separately that the file the override
    names was really written; together the two prove the override landed, which
    is what the un-redacted equality used to prove.
    """
    assert Path(redacted).name == expected.name, (redacted, expected)
    assert expected.name in redacted, (redacted, expected.name)


def default_ledger_path(workspace: dict[str, Path]) -> Path:
    """Where the CLI is contracted to keep the ledger for this workspace."""
    return workspace["runs"] / "lifecycle" / "install-ledger.jsonl"


def seed_installed(
    workspace: dict[str, Path],
    bundle_id: str = DEFAULT_BUNDLE_ID,
    *,
    stage: str = "installed",
    sha256: str = "a" * 64,
) -> InstallLedger:
    """Write one install entry for ``bundle_id`` into the default ledger."""
    ledger = InstallLedger(default_ledger_path(workspace))
    ledger.record_install(
        InstallResult(package_sha256=sha256, bundle_id=bundle_id, stage=stage),
    )
    return InstallLedger(default_ledger_path(workspace))


class _FakeInstaller:
    """Minimal stand-in for :class:`unipad.ipa.installer.IpaInstaller`.

    It returns exactly the scripted :class:`InstallResult`, so a test decides
    which stage the chain "reached" without pretending a guest exists.
    """

    #: The result every call returns.
    scripted: InstallResult = InstallResult(
        package_sha256="", bundle_id=DEFAULT_BUNDLE_ID, stage="installed"
    )

    def __init__(self, **kwargs: Any) -> None:  # noqa: D107 - signature mirror
        self.kwargs = kwargs

    def install(
        self, path: Any, *, name: Any = None, precheck: Any = None, **kwargs: Any
    ) -> InstallResult:
        return self.scripted


@pytest.fixture()
def completing_chain(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make the install chain complete at a scripted stage, with no guest.

    Only the two capabilities that need a real guest are replaced; the precheck
    stays the real one so a rejected package still fails for the real reason.
    """
    real = cli._resolve_capability

    def fake(name: str) -> Any:
        if name == "guest_channel":
            return lambda: object()
        if name == "ipa_install":
            return _FakeInstaller
        return real(name)

    monkeypatch.setattr(cli, "_resolve_capability", fake)
    return _FakeInstaller


# ---------------------------------------------------------------------------
# command registration
# ---------------------------------------------------------------------------


def test_the_prd_9_1_command_list_is_unchanged() -> None:
    """The nine documented commands stay exactly nine (PRD 9.1)."""
    assert set(cli.COMMANDS) == PRD_9_1_COMMANDS
    assert set(cli._COMMAND_TABLE) == PRD_9_1_COMMANDS  # noqa: SLF001 - contract


def test_the_three_lifecycle_commands_are_registered_separately() -> None:
    """PRD 3.2 P1 (3) adds commands; it does not rewrite the PRD 9.1 list.

    The "the total is exactly the declared surfaces" claim moved to
    :func:`test_the_matrix_command_is_registered_apart_from_the_other_surfaces`,
    which states it as the three-way union -- a stronger form of what the third
    assertion here used to say, because it now has to name every surface.
    """
    assert set(cli.LIFECYCLE_COMMANDS) == LIFECYCLE_COMMANDS
    assert set(cli._LIFECYCLE_COMMAND_TABLE) == LIFECYCLE_COMMANDS  # noqa: SLF001
    # The nine are still all accepted: neither later surface dropped one.
    assert PRD_9_1_COMMANDS <= set(cli.SUPPORTED_COMMANDS)


def test_the_matrix_command_is_registered_apart_from_the_other_surfaces() -> None:
    """PRD 3.2 P1 (2) adds one command; it rewrites neither of the other lists.

    ``SUPPORTED_COMMANDS`` is the exit-2 guard and the ``known_commands`` an
    unknown-command envelope reports, so the matrix surface has to be *in* it --
    without that, ``unipad matrix`` is refused as ``unsupported_command`` before
    the parser ever sees it.  It is still pinned as its own surface rather than
    folded into ``COMMANDS``, because the closed set stays exactly the declared
    surfaces: the equality is against a named union, so a surface that appears
    without being declared here fails.
    """
    assert set(cli.MATRIX_COMMANDS) == MATRIX_COMMANDS
    assert set(cli._MATRIX_COMMAND_TABLE) == MATRIX_COMMANDS  # noqa: SLF001
    assert (
        set(cli.SUPPORTED_COMMANDS) == PRD_9_1_COMMANDS | LIFECYCLE_COMMANDS | MATRIX_COMMANDS
    )


def test_the_ledger_is_a_probed_capability_not_a_hard_import() -> None:
    """A ``unipad.ipa`` that cannot import must read as ``blocked``, not a crash.

    Every sibling package the controller uses is resolved through
    ``_CAPABILITIES`` for this reason; the ledger is not an exception.
    """
    assert cli._CAPABILITIES["install_ledger"] == (  # noqa: SLF001 - contract
        ("unipad.ipa.lifecycle", "InstallLedger"),
    )
    assert cli._resolve_capability("install_ledger") is not None
    # ... and the probe list is what a blocked envelope would name.
    assert cli._probed("install_ledger") == ["unipad.ipa.lifecycle.InstallLedger"]  # noqa: SLF001


def test_main_accepts_the_lifecycle_commands(workspace: dict[str, Path]) -> None:
    """An unknown command is exit 2; a lifecycle command is not unknown."""
    unknown = run_cli(["frobnicate"], workspace)
    assert unknown.exit_code == int(ExitCode.USAGE)
    assert unknown.error_code == ErrorCode.UNSUPPORTED_COMMAND

    for command in sorted(LIFECYCLE_COMMANDS):
        argv = [command]
        if command == "uninstall":
            argv.append(DEFAULT_BUNDLE_ID)
        if command == "update":
            argv.append(str(workspace["root"] / "absent.ipa"))
        result = run_cli(argv, workspace)
        assert result.document["command"] == command
        assert result.error_code != ErrorCode.UNSUPPORTED_COMMAND


def test_every_lifecycle_command_emits_the_prd_9_1_envelope(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """One JSON object, the same nine keys, whatever the outcome."""
    invocations = [
        ["history", "--all"],
        ["history", "--bundle", DEFAULT_BUNDLE_ID],
        ["uninstall", DEFAULT_BUNDLE_ID],
        ["update", str(ipa)],
    ]
    for argv in invocations:
        result = run_cli(argv, workspace)
        assert set(result.document) == ENVELOPE_KEYS, argv
        assert result.document["unipad_version"] == __version__
        assert result.document["schema_version"] == cli.ENVELOPE_SCHEMA_VERSION
        assert isinstance(result.document["data"], dict)
        assert result.document["exit_code"] == result.exit_code
        assert result.document["ok"] is (result.exit_code == int(ExitCode.OK))


@pytest.fixture()
def ipa(workspace: dict[str, Path]) -> Path:
    """A real, acceptable Device IPA built by the fixture builder."""
    return build_ipa(workspace["root"] / "HelloUniPad.ipa")


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_of_an_unknown_bundle_is_an_empty_true_answer(
    workspace: dict[str, Path],
) -> None:
    """Nothing recorded is an answer, not a failure (exit 0)."""
    result = run_cli(["history", "--bundle", "com.unipad.never-installed"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.status == "ok"
    assert result.document["error"] is None
    assert result.data["bundle_id"] == "com.unipad.never-installed"
    assert result.data["entries"] == []
    assert result.data["entry_count"] == 0
    assert result.data["unreadable_lines"] == 0
    assert result.data["installed"] is False
    # The ledger was never created by a read.
    assert not default_ledger_path(workspace).exists()


def test_history_reports_every_entry_of_one_bundle_oldest_first(
    workspace: dict[str, Path],
) -> None:
    ledger = seed_installed(workspace)
    ledger.record_install(
        InstallResult(
            package_sha256="b" * 64,
            bundle_id=DEFAULT_BUNDLE_ID,
            stage="installed",
            trust_mode="developer",
        ),
        action=LifecycleAction.UPDATE,
    )
    ledger.record_uninstall(
        InstallResult(package_sha256="b" * 64, bundle_id=DEFAULT_BUNDLE_ID, stage="installed")
    )
    InstallLedger(default_ledger_path(workspace))  # dropped: history is re-read from disk

    result = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)

    assert result.exit_code == int(ExitCode.OK)
    entries = result.data["entries"]
    assert [entry["action"] for entry in entries] == ["install", "update", "uninstall"]
    assert [entry["entry_id"] for entry in entries] == [
        "le-00000001",
        "le-00000002",
        "le-00000003",
    ]
    # The transition names the entry it replaced, so "what happened" survives.
    assert entries[1]["supersedes"] == "le-00000001"
    assert entries[2]["supersedes"] == "le-00000002"
    assert entries[2]["stage"] == "removed"
    assert result.data["entry_count"] == 3
    # Derived, never stored: the tail says the bundle is gone.
    assert result.data["installed"] is False
    assert result.data["current_stage"] is None


def test_history_all_reports_the_derived_installed_set(workspace: dict[str, Path]) -> None:
    seed_installed(workspace, "com.unipad.one")
    seed_installed(workspace, "com.unipad.two")
    seed_installed(workspace, "com.unipad.three").record_uninstall(
        InstallResult(package_sha256="a" * 64, bundle_id="com.unipad.three", stage="installed")
    )

    result = run_cli(["history", "--all"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["all_bundles"] is True
    assert result.data["entry_count"] == 4
    assert result.data["installed_bundle_ids"] == ["com.unipad.one", "com.unipad.two"]


def test_a_corrupt_ledger_line_is_reported_and_the_history_survives(
    workspace: dict[str, Path],
) -> None:
    """A truncated tail costs one entry, never the whole history."""
    ledger_path = default_ledger_path(workspace)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    good = seed_installed(workspace)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
        handle.write(json.dumps({"entry_id": "x", "action": "bogus_action"}) + "\n")

    result = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["unreadable_lines"] == 2
    assert result.data["entry_count"] == 1
    assert result.data["entries"][0]["entry_id"] == good.entries[0].entry_id


def test_history_requires_exactly_one_of_bundle_or_all(workspace: dict[str, Path]) -> None:
    """Neither selector is ambiguous; both is a contradiction.  Exit 2 (PRD 9.1)."""
    neither = run_cli(["history"], workspace)
    assert neither.exit_code == int(ExitCode.USAGE)
    assert neither.error_code == ErrorCode.CONFIG_INVALID

    both = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID, "--all"], workspace)
    assert both.exit_code == int(ExitCode.USAGE)
    assert both.error_code == ErrorCode.CONFIG_INVALID
    assert neither.document["error"] is not None


def test_a_blank_bundle_selector_does_not_widen_to_every_bundle(
    workspace: dict[str, Path],
) -> None:
    """A blank ``--bundle`` must not silently answer for the whole host."""
    seed_installed(workspace, "com.unipad.one")

    result = run_cli(["history", "--bundle", "   "], workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID
    assert result.data == {}


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_of_an_installed_bundle_records_the_transition(
    workspace: dict[str, Path],
) -> None:
    seeded = seed_installed(workspace)

    result = run_cli(["uninstall", DEFAULT_BUNDLE_ID], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.status == "ok"
    assert result.document["error"] is None
    entry = result.data["entry"]
    assert entry["action"] == "uninstall"
    assert entry["stage"] == "removed"
    assert entry["supersedes"] == seeded.entries[0].entry_id
    assert entry["package_sha256"] == "a" * 64
    assert result.data["installed"] is False
    # The envelope must agree with the entry beside it.  The installed set used
    # to be captured *before* the removal and emitted after it, so a success
    # reported ``installed: false`` and ``installed_bundle_ids: [this id]`` in
    # the same object -- and the next ``uninstall`` of that id was then refused
    # for a reason the previous answer denied.
    assert DEFAULT_BUNDLE_ID not in result.data["installed_bundle_ids"]

    # The transition is durable and the derived state follows it.
    reloaded = InstallLedger(default_ledger_path(workspace))
    assert [e.action for e in reloaded.entries] == [
        LifecycleAction.INSTALL,
        LifecycleAction.UNINSTALL,
    ]
    assert reloaded.installed(DEFAULT_BUNDLE_ID) is None
    assert reloaded.installed_bundle_ids() == ()


def test_uninstall_refuses_a_bundle_that_is_not_installed(
    workspace: dict[str, Path],
) -> None:
    """Refusal is a structured envelope; the ledger is not touched."""
    result = run_cli(["uninstall", DEFAULT_BUNDLE_ID], workspace)

    assert result.exit_code != int(ExitCode.OK)
    assert result.status in {"blocked", "failed"}
    assert result.document["ok"] is False
    assert result.details["blocked_reason"] == "not_installed"
    assert result.details["bundle_id"] == DEFAULT_BUNDLE_ID
    assert result.details["installed_bundle_ids"] == []
    # No traceback, no second document on stdout.
    assert "Traceback" not in result.raw
    assert result.document["error"]["message"]
    # Refusing must not fabricate a removal.
    assert not default_ledger_path(workspace).exists()


def test_uninstall_twice_is_refused_the_second_time(workspace: dict[str, Path]) -> None:
    seed_installed(workspace)
    first = run_cli(["uninstall", DEFAULT_BUNDLE_ID], workspace)
    second = run_cli(["uninstall", DEFAULT_BUNDLE_ID], workspace)

    assert first.exit_code == int(ExitCode.OK)
    assert second.exit_code != int(ExitCode.OK)
    assert second.details["blocked_reason"] == "not_installed"
    # Exactly one removal was recorded, not two.
    assert len(InstallLedger(default_ledger_path(workspace)).entries) == 2


def test_uninstall_of_a_bundle_recorded_only_as_a_failure_is_refused(
    workspace: dict[str, Path],
) -> None:
    """A failed install is not an install, so there is nothing to remove."""
    ledger = InstallLedger(default_ledger_path(workspace))
    ledger.record_install(
        InstallResult(
            package_sha256="a" * 64,
            bundle_id=DEFAULT_BUNDLE_ID,
            stage="failed",
            error_code=ErrorCode.INVALID_PACKAGE,
        )
    )

    result = run_cli(["uninstall", DEFAULT_BUNDLE_ID], workspace)

    assert result.exit_code != int(ExitCode.OK)
    assert result.details["blocked_reason"] == "not_installed"
    assert len(InstallLedger(default_ledger_path(workspace)).entries) == 1


def test_uninstall_requires_a_non_empty_bundle_id(workspace: dict[str, Path]) -> None:
    result = run_cli(["uninstall", "   "], workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_of_a_bundle_that_was_never_installed_is_refused(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """An update is a transition; with no predecessor the command says so.

    It must refuse *before* asking for a guest channel: reaching the channel
    would mean transferring a package the operator believes replaces something
    that is not there.
    """
    result = run_cli(["update", str(ipa)], workspace)

    assert result.exit_code != int(ExitCode.OK)
    assert result.details["blocked_reason"] == "not_installed"
    assert result.details["installed_bundle_ids"] == []
    assert not default_ledger_path(workspace).exists()


def test_update_of_an_installed_bundle_records_a_superseding_update(
    workspace: dict[str, Path], ipa: Path, completing_chain: Any
) -> None:
    completing_chain.scripted = InstallResult(
        package_sha256="",
        bundle_id=DEFAULT_BUNDLE_ID,
        stage="installed",
        trust_mode="developer",
    )
    seeded = seed_installed(workspace)

    result = run_cli(["update", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["stage"] == "installed"
    lifecycle = result.data["lifecycle"]
    assert lifecycle["action"] == "update"
    assert lifecycle["supersedes"] == seeded.entries[0].entry_id
    assert lifecycle["bundle_id"] == DEFAULT_BUNDLE_ID
    assert lifecycle["unreadable_lines"] == 0

    reloaded = InstallLedger(default_ledger_path(workspace))
    assert [e.action for e in reloaded.entries] == [
        LifecycleAction.INSTALL,
        LifecycleAction.UPDATE,
    ]
    assert reloaded.installed(DEFAULT_BUNDLE_ID) is not None
    assert reloaded.installed(DEFAULT_BUNDLE_ID).entry_id == "le-00000002"


def test_a_successful_transfer_never_reports_installed(
    workspace: dict[str, Path], ipa: Path, completing_chain: Any
) -> None:
    """PRD 9.1: a verified file transfer must not return ``installed``."""
    completing_chain.scripted = InstallResult(
        package_sha256="", bundle_id=DEFAULT_BUNDLE_ID, stage="transferred"
    )
    seed_installed(workspace)

    result = run_cli(["update", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.data["stage"] == "transferred"
    assert result.data["stage"] not in ("installed", "registered")
    # The entry records the stage that was reached, so the bundle stays absent.
    entry = result.data["lifecycle"]
    assert entry["stage"] == "transferred"
    assert InstallLedger(default_ledger_path(workspace)).installed(DEFAULT_BUNDLE_ID).entry_id == (
        "le-00000001"
    )


def test_a_failed_update_keeps_the_previous_install_present(
    workspace: dict[str, Path], ipa: Path, completing_chain: Any
) -> None:
    """A failed replacement did not touch the installed version (no eviction)."""
    completing_chain.scripted = InstallResult(
        package_sha256="",
        bundle_id=DEFAULT_BUNDLE_ID,
        stage="failed",
        error_code=ErrorCode.TRANSFER_FAILED,
    )
    seed_installed(workspace)

    result = run_cli(["update", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.data["error_code"] == ErrorCode.TRANSFER_FAILED
    reloaded = InstallLedger(default_ledger_path(workspace))
    assert reloaded.installed(DEFAULT_BUNDLE_ID) is not None
    assert [e.action for e in reloaded.entries] == [
        LifecycleAction.INSTALL,
        LifecycleAction.UPDATE,
    ]
    assert reloaded.entries[1].error_class == "transfer_failed"


# ---------------------------------------------------------------------------
# the install chain feeds the ledger
# ---------------------------------------------------------------------------


def test_install_feeds_the_ledger_that_history_reads(
    workspace: dict[str, Path], ipa: Path, completing_chain: Any
) -> None:
    """Without this the whole surface is dead code: nothing would ever record."""
    completing_chain.scripted = InstallResult(
        package_sha256="", bundle_id=DEFAULT_BUNDLE_ID, stage="installed", trust_mode="developer"
    )

    install = run_cli(["install", str(ipa)], workspace)
    assert install.exit_code == int(ExitCode.OK)

    history = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)
    assert history.data["entry_count"] == 1
    assert history.data["entries"][0]["action"] == "install"
    assert history.data["installed"] is True
    assert history.data["current_stage"] == "installed"
    assert history.data["entries"][0]["trust_mode"] == "developer"

    # ... and the uninstall that follows therefore succeeds.
    uninstall = run_cli(["uninstall", DEFAULT_BUNDLE_ID], workspace)
    assert uninstall.exit_code == int(ExitCode.OK)


def test_an_installer_failure_is_recorded_with_its_failure_class(
    workspace: dict[str, Path], ipa: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure raised *inside* the installer is a history entry too.

    The installer path is a different ``_finish_install`` call site from the
    precheck one, and the two have drifted before; the ledger must be written
    from both or a guest-side failure would vanish from the history.
    """
    from unipad.errors import UniPadError

    class _RaisingInstaller(_FakeInstaller):
        def install(self, path: Any, **kwargs: Any) -> InstallResult:
            raise UniPadError(ErrorCode.TRANSFER_FAILED, "the guest dropped the link")

    real = cli._resolve_capability

    def fake(name: str) -> Any:
        if name == "guest_channel":
            return lambda: object()
        if name == "ipa_install":
            return _RaisingInstaller
        return real(name)

    monkeypatch.setattr(cli, "_resolve_capability", fake)

    install = run_cli(["install", str(ipa)], workspace)

    assert install.exit_code == int(ExitCode.FAILURE)
    assert install.data["error_code"] == ErrorCode.TRANSFER_FAILED
    assert install.data["lifecycle"]["error_class"] == "transfer_failed"

    history = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)
    entry = history.data["entries"][0]
    assert entry["stage"] == "failed"
    assert entry["error_code"] == ErrorCode.TRANSFER_FAILED
    assert entry["error_class"] == "transfer_failed"
    assert history.data["installed"] is False


def test_a_precheck_rejection_is_recorded_with_its_failure_class(
    workspace: dict[str, Path],
) -> None:
    """清晰的错误分类: the class travels with the entry, from the CLI."""
    package = build_ipa(workspace["root"] / "Sim.ipa", corrupt="simulator")

    install = run_cli(["install", str(package)], workspace)
    assert install.exit_code == int(ExitCode.FAILURE)

    history = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)
    entry = history.data["entries"][0]
    assert entry["action"] == "install"
    assert entry["stage"] == "failed"
    assert entry["error_code"] in (
        ErrorCode.UNSUPPORTED_ARCH,
        ErrorCode.UNSUPPORTED_PLATFORM,
        ErrorCode.INVALID_PACKAGE,
    )
    assert entry["error_class"] == "package_rejected"
    assert history.data["installed"] is False


def test_a_blocked_install_records_nothing(workspace: dict[str, Path], ipa: Path) -> None:
    """No guest channel means the install never happened; no entry is forged."""
    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    # ``unavailable`` and not ``missing``: the channel is modelled on this host,
    # so the truthful reason is that no guest is reachable.  See
    # tests/unit/test_cli.py::test_a_guest_channel_the_controller_does_not_model_is_missing
    # for the other reason and the only case that should produce it.
    assert result.details["blocked_reason"] == "guest_channel_unavailable"
    assert not default_ledger_path(workspace).exists()
    history = run_cli(["history", "--all"], workspace)
    assert history.data["entry_count"] == 0


# ---------------------------------------------------------------------------
# PRD 9.2: nothing sensitive in the output
# ---------------------------------------------------------------------------


def test_no_entry_ever_carries_an_error_message(workspace: dict[str, Path]) -> None:
    ledger = InstallLedger(default_ledger_path(workspace))
    ledger.record_install(
        InstallResult(
            package_sha256="a" * 64,
            bundle_id=DEFAULT_BUNDLE_ID,
            stage="failed",
            error_code=ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
            error_message="a guest message that must never be printed",
        )
    )

    result = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)

    entry = result.data["entries"][0]
    assert set(entry) == ENTRY_KEYS
    assert "error_message" not in entry
    assert "a guest message" not in json.dumps(result.document)
    assert entry["error_code"] == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert entry["error_class"] == "service_unavailable"


def test_the_ledger_path_is_reported_and_overridable(workspace: dict[str, Path]) -> None:
    seed_installed(workspace)

    default = run_cli(["history", "--all"], workspace)
    # The envelope's ledger field is redacted (PRD 9.2), so it cannot be compared
    # to the path this test controls as a string.  What must survive redaction is
    # the naming: the envelope names the ledger the command is contracted to read,
    # by its final component, and that file is the one that really exists.
    envelope_names(default.data["ledger"], default_ledger_path(workspace))
    assert default_ledger_path(workspace).is_file()

    custom = workspace["root"] / "elsewhere" / "ledger.jsonl"
    result = run_cli(["history", "--all", "--ledger", str(custom)], workspace)
    envelope_names(result.data["ledger"], custom)
    assert result.data["entry_count"] == 0
    assert result.data["unreadable_lines"] == 0
    # The override is honoured for writes too.
    seeded = InstallLedger(custom)
    seeded.record_install(
        InstallResult(package_sha256="c" * 64, bundle_id="com.unipad.custom", stage="installed")
    )
    again = run_cli(
        ["history", "--bundle", "com.unipad.custom", "--ledger", str(custom)], workspace
    )
    assert again.data["entry_count"] == 1
    # The override really is where the entry went: it is readable back through the
    # path the test passed in, which is only true if the write landed there and not
    # in the default ledger.
    envelope_names(again.data["ledger"], custom)
    assert custom.is_file()
    assert InstallLedger(custom).installed("com.unipad.custom") is not None
    # The default ledger is untouched by the override.
    assert len(InstallLedger(default_ledger_path(workspace)).entries) == 1


def test_a_ledger_path_that_is_not_a_file_is_refused_not_read_as_empty(
    workspace: dict[str, Path],
) -> None:
    """A directory at the ledger path must not answer "nothing is installed".

    ``InstallLedger`` tests ``is_file()``, so without this guard a directory
    would read as an empty ledger -- a wrong answer about the device dressed up
    as a true one.
    """
    bogus = default_ledger_path(workspace)
    bogus.mkdir(parents=True, exist_ok=True)

    history = run_cli(["history", "--all"], workspace)
    assert history.exit_code == int(ExitCode.BLOCKED)
    assert history.details["blocked_reason"] == "ledger_not_a_file"
    assert history.data == {}

    uninstall = run_cli(["uninstall", DEFAULT_BUNDLE_ID], workspace)
    assert uninstall.exit_code == int(ExitCode.BLOCKED)
    assert uninstall.details["blocked_reason"] == "ledger_not_a_file"


def test_uninstall_into_an_overridden_ledger(workspace: dict[str, Path]) -> None:
    custom = workspace["root"] / "elsewhere" / "ledger.jsonl"
    InstallLedger(custom).record_install(
        InstallResult(package_sha256="d" * 64, bundle_id="com.unipad.custom", stage="installed")
    )

    result = run_cli(
        ["uninstall", "com.unipad.custom", "--ledger", str(custom)], workspace
    )

    assert result.exit_code == int(ExitCode.OK)
    # The ledger field is redacted (PRD 9.2), so the override is proven two ways
    # instead of by string equality: the envelope names the same final component
    # the test passed in, and the transition the uninstall recorded is readable
    # back through that same override path.
    envelope_names(result.data["ledger"], custom)
    assert InstallLedger(custom).installed("com.unipad.custom") is None
    assert not default_ledger_path(workspace).exists()


# ---------------------------------------------------------------------------
# the ledger's own integrity, through the production path
# ---------------------------------------------------------------------------


def test_a_torn_tail_does_not_swallow_the_next_recorded_transition(
    workspace: dict[str, Path], ipa: Path, completing_chain: Any
) -> None:
    """A half-written line must cost itself, never the entry after it.

    This is the production path: the CLI opens the real ledger, appends, and a
    later ``history`` reads it back.  If the tail is not terminated before the
    append, the update is concatenated onto the corrupt line and *both* are
    lost -- and because that is one unreadable line, nothing rises to say a
    second entry went with it.  The operator would see a bundle installed that
    the update never touched.
    """
    completing_chain.scripted = InstallResult(
        package_sha256="", bundle_id=DEFAULT_BUNDLE_ID, stage="installed"
    )
    seed_installed(workspace)
    ledger_path = default_ledger_path(workspace)

    # a writer that died mid-line
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write('{"entry_id": "le-00000002", "bundle_id": "' + DEFAULT_BUNDLE_ID + '", "act')

    update = run_cli(["update", str(ipa)], workspace)
    assert update.exit_code == int(ExitCode.OK)

    lines = [
        line
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 3, "the torn tail and the update must be separate lines"

    history = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)
    assert history.data["entry_count"] == 2
    assert [e["action"] for e in history.data["entries"]] == ["install", "update"]
    assert history.data["entries"][1]["supersedes"] == "le-00000001"
    assert history.data["installed"] is True


def test_a_guest_refusal_is_reported_as_a_refusal_not_as_unknown(
    workspace: dict[str, Path], ipa: Path, completing_chain: Any
) -> None:
    """清晰的错误分类: the guest's most common refusal must be actionable.

    ``installer._map_guest_error`` turns ``signature_rejected`` /
    ``policy_rejected`` / ``untrusted`` / ``rejected`` / ``denied`` /
    ``forbidden`` onto ``signature_or_policy_rejected``.  Reporting that as
    ``unknown`` tells an operator "we cannot classify this" about the one
    failure they can actually act on.
    """
    completing_chain.scripted = InstallResult(
        package_sha256="",
        bundle_id=DEFAULT_BUNDLE_ID,
        stage="failed",
        error_code=ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    )

    install = run_cli(["install", str(ipa)], workspace)

    assert install.exit_code == int(ExitCode.FAILURE)
    assert install.data["lifecycle"]["error_code"] == (
        ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    )
    assert install.data["lifecycle"]["error_class"] == "guest_refused"

    history = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)
    assert history.data["entries"][0]["error_class"] == "guest_refused"
    assert history.data["installed"] is False


def test_an_update_supersedes_the_installed_entry_not_a_failed_one(
    workspace: dict[str, Path], ipa: Path, completing_chain: Any
) -> None:
    """A failed attempt in between must not become the thing an update replaced.

    The seeded install is still what holds the bundle, so it -- not the failed
    entry -- is what the update supersedes.  Naming the failure would put the
    supersede chain at odds with ``installed`` in the same file.
    """
    completing_chain.scripted = InstallResult(
        package_sha256="",
        bundle_id=DEFAULT_BUNDLE_ID,
        stage="failed",
        error_code=ErrorCode.TRANSFER_FAILED,
    )
    seed_installed(workspace)

    failed = run_cli(["update", str(ipa)], workspace)
    assert failed.exit_code == int(ExitCode.FAILURE)

    completing_chain.scripted = InstallResult(
        package_sha256="", bundle_id=DEFAULT_BUNDLE_ID, stage="installed"
    )
    good = run_cli(["update", str(ipa)], workspace)
    assert good.exit_code == int(ExitCode.OK)

    entries = InstallLedger(default_ledger_path(workspace)).entries
    assert [e.action for e in entries] == [
        LifecycleAction.INSTALL,
        LifecycleAction.UPDATE,
        LifecycleAction.UPDATE,
    ]
    assert entries[1].supersedes == "le-00000001"
    assert entries[2].supersedes == "le-00000001"


# ---------------------------------------------------------------------------
# a stage that never happened records nothing -- the decision, pinned
# ---------------------------------------------------------------------------


def test_a_blocked_install_records_nothing_but_leaves_its_reason_in_the_log(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """No guest channel means no stage ran, so the ledger gains no entry.

    That is the rule, not an oversight: the ledger records *transitions*, and a
    refusal that happened before the transfer never touched any bundle.  What
    it must never do is leave the operator with nothing -- so the log the
    envelope names exists and says which probe failed, and ``history`` answers
    "nothing" as the true answer it is rather than as a fault.
    """
    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.details["blocked_reason"] == "guest_channel_unavailable"
    assert not default_ledger_path(workspace).exists()

    history = run_cli(["history", "--all"], workspace)
    assert history.exit_code == int(ExitCode.OK)
    assert history.data["entry_count"] == 0
    assert history.data["installed_bundle_ids"] == []

    log_path = run_dir_of(workspace, result, named="install.log") / "install.log"
    assert log_path.is_file(), "the envelope names this file, so it must exist"
    logged = log_path.read_text(encoding="utf-8")
    assert "install requested" in logged
    assert "guest_channel" in logged, "the refusal must say which probe failed"


def test_a_refused_update_records_nothing_but_leaves_its_reason_in_the_log(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """The same rule on the ``not_installed`` refusal (reverse-review m4).

    ``unipad update`` refuses before the transfer, so there is no transition to
    record.  The refusal is not lost either: it is in the log the envelope
    names, with the bundle id that could not be matched.
    """
    result = run_cli(["update", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.details["blocked_reason"] == "not_installed"
    assert not default_ledger_path(workspace).exists()

    history = run_cli(["history", "--all"], workspace)
    assert history.data["entry_count"] == 0

    log_path = run_dir_of(workspace, result, named="install.log") / "install.log"
    assert log_path.is_file()
    logged = log_path.read_text(encoding="utf-8")
    assert "refused=not_installed" in logged
    assert DEFAULT_BUNDLE_ID in logged


def test_a_refused_update_leaves_an_existing_history_untouched(
    workspace: dict[str, Path],
) -> None:
    """The refusal must not append a marker either.

    A history is not a log of everything the CLI was asked to do; it is a
    record of what happened to the bundles.  An entry for an update that never
    ran would be indistinguishable from an update that did.
    """
    seeded = seed_installed(workspace)
    stranger = build_ipa(
        workspace["root"] / "Stranger.ipa", bundle_id="com.unipad.stranger"
    )

    result = run_cli(["update", str(stranger)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.details["blocked_reason"] == "not_installed"
    assert result.details["installed_bundle_ids"] == [DEFAULT_BUNDLE_ID]

    reloaded = InstallLedger(default_ledger_path(workspace))
    assert [e.entry_id for e in reloaded.entries] == [seeded.entries[0].entry_id]
    assert reloaded.installed(DEFAULT_BUNDLE_ID) is not None
    assert reloaded.installed("com.unipad.stranger") is None


# ---------------------------------------------------------------------------
# reverse m5: a package with no CFBundleIdentifier (already refused upstream)
# ---------------------------------------------------------------------------


def test_a_bundle_id_less_ipa_is_refused_as_a_package_defect_not_a_state_mismatch(
    workspace: dict[str, Path],
) -> None:
    """``update`` must not report a package defect as "nothing is installed".

    The precheck refuses an IPA whose Info.plist has no ``CFBundleIdentifier``
    with ``invalid_package``/``missing_bundle_id``, and it runs *before* the
    require-installed check.  So the operator is told the package is defective
    -- the thing they can fix -- instead of being told to use ``unipad install``
    for a first install, which would send them to reinstall a package that can
    never be installed.  Pinned because the order of those two checks is the
    whole difference between the two messages.
    """
    package = build_ipa(workspace["root"] / "NoIdentifier.ipa", bundle_id="")

    for command in ("install", "update"):
        result = run_cli([command, str(package)], workspace)

        assert result.exit_code == int(ExitCode.FAILURE), command
        assert result.error_code == ErrorCode.INVALID_PACKAGE, command
        assert result.details.get("blocked_reason") is None, command
        assert "not recorded as installed" not in result.raw, command
        assert result.data["stage"] == "failed", command


def test_a_package_that_declares_no_bundle_id_is_recorded_as_a_failure(
    workspace: dict[str, Path],
) -> None:
    """The attempt is history; the empty key is what the package declares.

    An unparseable package has no bundle id, so the entry is stored under the
    empty string -- which is the truth about the package, not a placeholder
    invented to make it look addressable.  It is a *failure*, so nothing can
    ever derive presence from it: ``installed_bundle_ids`` stays empty and the
    operator is never told something is installed because a broken package was
    offered.  ``history --all`` still shows the attempt, with the package hash
    that identifies it.
    """
    package = build_ipa(workspace["root"] / "NoIdentifier.ipa", bundle_id="")

    run_cli(["install", str(package)], workspace)

    ledger = InstallLedger(default_ledger_path(workspace))
    assert [e.bundle_id for e in ledger.entries] == [""]
    assert ledger.entries[0].stage == "failed"
    assert ledger.entries[0].error_class == "package_rejected"
    assert ledger.entries[0].package_sha256

    assert ledger.installed("") is None
    assert ledger.installed_bundle_ids() == ()

    history = run_cli(["history", "--all"], workspace)
    assert history.data["entry_count"] == 1
    assert history.data["installed_bundle_ids"] == []


# ---------------------------------------------------------------------------
# the ledger lands before the run record
#
# This was pinned as an expected failure while ``_finish_install`` wrote
# ``install.json`` before the ledger append (callchain-5).  The ordering is in
# this module's file, so it is fixed here rather than deferred; the other half of
# that finding -- ``models.write_json`` needing an atomic rename plus fsync --
# is in ``src/unipad/models.py`` and is not this module's to fix.
# ---------------------------------------------------------------------------


def test_the_ledger_lands_before_the_run_record(
    workspace: dict[str, Path],
    ipa: Path,
    completing_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run record is evidence; the ledger is the state.  Evidence second."""
    order: list[str] = []
    real_write_json = cli.write_json

    def recording_write_json(path: Any, data: Any) -> None:
        if Path(path).name == "install.json":
            order.append("install.json")
        real_write_json(path, data)

    monkeypatch.setattr(cli, "write_json", recording_write_json)

    real_record = InstallLedger.record_install

    def recording_record(self: InstallLedger, *args: Any, **kwargs: Any):
        order.append("ledger")
        return real_record(self, *args, **kwargs)

    monkeypatch.setattr(InstallLedger, "record_install", recording_record)

    completing_chain.scripted = InstallResult(
        package_sha256="", bundle_id=DEFAULT_BUNDLE_ID, stage="installed"
    )
    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert order == ["ledger", "install.json"]


def test_history_of_one_bundle_scans_the_ledger_once(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_installed(workspace)
    scans: list[str] = []
    real_history = InstallLedger.history

    def counting(self: InstallLedger, bundle_id: str):
        scans.append(bundle_id)
        return real_history(self, bundle_id)

    monkeypatch.setattr(InstallLedger, "history", counting)

    result = run_cli(["history", "--bundle", DEFAULT_BUNDLE_ID], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert len(scans) == 1, "one bundle, one question, one scan"


def test_history_surfaces_the_growth_warning_past_the_documented_bound(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is crossed, not reached: ``growth_warning`` fires on ``>``.

    Two entries against a bound of one, because the ledger's own comparison is
    ``len(entries) <= LINE_WARN_THRESHOLD`` -- a ledger holding exactly the bound
    is still inside it.  Seeding one entry against a bound of one therefore
    proved nothing: the warning was unreachable however the envelope behaved,
    and the pin would have kept failing after the fix landed.
    """
    monkeypatch.setattr(lifecycle_module, "LINE_WARN_THRESHOLD", 1)
    seed_installed(workspace)
    seed_installed(workspace, bundle_id="com.unipad.second")

    result = run_cli(["history", "--all"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    warning = result.data.get("ledger_growth_warning")
    assert warning, "a ledger past the documented bound must say so"
    # The count is the ledger's, not a placeholder.
    assert "2 entries" in warning
    # A warning, never a refusal: the command still answers.
    assert result.data["entry_count"] == 2


def test_history_reports_no_growth_warning_inside_the_documented_bound(
    workspace: dict[str, Path],
) -> None:
    """The key is *absent*, not empty and not ``None``.

    ``history --all`` on a small ledger must not grow a
    ``ledger_growth_warning`` key at all: a caller testing for the key's
    presence is then answering "is this ledger past the bound" without having
    to know what the bound is, and ``None`` would make that test ambiguous with
    a warning that happens to be falsy.
    """
    seed_installed(workspace)

    result = run_cli(["history", "--all"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert "ledger_growth_warning" not in result.data


# ---------------------------------------------------------------------------
# the transfer stage publishes the package source (ADR-0008)
# ---------------------------------------------------------------------------

#: The five keys an install request carried before ADR-0008.
LEGACY_REQUEST_KEYS = {
    "package_name",
    "package_sha256",
    "size_bytes",
    "declared_min_os",
    "signature_mode",
}


class _RecordingChannel:
    """A guest channel that remembers what the installer asked it to do.

    ``_FakeInstaller`` never touches the channel, so it cannot show whether the
    install request names the package source.  This one records both calls the
    real installer makes.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.requests: list[dict[str, Any]] = []

    def send_package(self, name: str, data: bytes) -> None:
        self.sent.append((name, data))

    def request_install(self, meta: Mapping[str, Any]) -> Any:
        self.requests.append(dict(meta))
        return None


class _ChannelExercisingInstaller:
    """A stand-in installer that actually drives the guest channel."""

    scripted = InstallResult(package_sha256="", bundle_id=DEFAULT_BUNDLE_ID, stage="installed")

    #: The most recently constructed instance, so a test can read its channel.
    last: Any = None

    def __init__(self, **kwargs: Any) -> None:  # noqa: D107 - signature mirror
        self.kwargs = kwargs
        type(self).last = self

    def install(
        self, path: Any, *, name: Any = None, precheck: Any = None, **kwargs: Any
    ) -> InstallResult:
        channel = self.kwargs["channel"]
        channel.send_package("HelloUniPad.ipa", b"IPA-BYTES")
        channel.request_install(
            {
                "package_name": "HelloUniPad.ipa",
                "package_sha256": "0" * 64,
                "size_bytes": len(b"IPA-BYTES"),
                "declared_min_os": "17.0",
                "signature_mode": "self",
            }
        )
        return type(self).scripted


@pytest.fixture()
def channel_chain(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The install chain with a recording channel and an installer that uses it."""
    channel = _RecordingChannel()
    real = cli._resolve_capability

    def fake(name: str) -> Any:
        if name == "guest_channel":
            return lambda: channel
        if name == "ipa_install":
            return _ChannelExercisingInstaller
        return real(name)

    monkeypatch.setattr(cli, "_resolve_capability", fake)
    return channel, _ChannelExercisingInstaller


def _transfer_window(tmp_path: Path, **overrides: Any) -> Any:
    """A transfer window for a package sitting in ``tmp_path``."""
    fields: dict[str, Any] = dict(
        command="install",
        ipa_path=tmp_path / "HelloUniPad.ipa",
        transport=cli.TRANSPORT_HTTPS,
        package_sha256="0" * 64,
        bundle_id=None,
        log_path=tmp_path / "install.log",
        log_lines=[],
        persist_log=lambda: None,
    )
    fields.update(overrides)
    return cli._TransferWindow(**fields)  # noqa: SLF001 - the unit under test


def _assert_port_is_free(port: int) -> None:
    """Prove nothing is listening on ``port`` any more.

    The probe binds *without* ``SO_REUSEADDR``, and that is not an accident.  On
    Windows a socket carrying ``SO_REUSEADDR`` is allowed to bind a port that
    another ``SO_REUSEADDR`` socket already holds -- and ``SO_REUSEADDR`` is
    exactly what ``http.server.HTTPServer`` sets -- so a reuse-enabled probe
    reports a stopped server as free and this test would pass with the port
    still held.  A cross-check on the connect side closes the other way out.
    """
    import socket

    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            probe.listen(1)
        except OSError as exc:
            raise AssertionError(f"port {port} is still held: {exc}") from exc

    with socket.socket() as dial:
        dial.settimeout(1.0)
        assert dial.connect_ex(("127.0.0.1", port)) != 0, f"port {port} still answers"


def test_the_default_transport_is_channel_and_publishes_nothing(
    workspace: dict[str, Path], ipa: Path, channel_chain: Any
) -> None:
    """The default route is unchanged: no service, no extra request fields.

    ADR-0008 keeps ``channel`` as the default because this host has no guest, so
    the ``https`` path cannot be exercised end to end.  A transport that was
    wrong would surface as "the package is bad" rather than "the transport is
    wrong", which is the failure mode the default exists to avoid.
    """
    channel, installer = channel_chain

    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["transport"] == "channel"
    # No service ran, so there is no service evidence -- an absent key, not an
    # empty one: "nothing listened" and "something listened and said nothing"
    # are different facts.
    assert "transfer" not in result.data
    # ... and the channel is the object the capability returned, unwrapped.
    assert installer.last.kwargs["channel"] is channel
    assert len(channel.requests) == 1
    assert set(channel.requests[0]) == LEGACY_REQUEST_KEYS


def test_the_https_transport_publishes_a_service_and_names_it_in_the_request(
    workspace: dict[str, Path], ipa: Path, channel_chain: Any
) -> None:
    """The transfer stage really starts the service and really records it."""
    channel, installer = channel_chain

    result = run_cli(["install", "--transport", "https", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["transport"] == "https"
    transfer = result.data["transfer"]
    assert transfer["transport"] == "https"
    assert transfer["bind_host"] == "127.0.0.1"
    assert transfer["port"] > 0
    assert transfer["guest_url"] == f"https://10.0.2.2:{transfer['port']}/index.json"
    # The service listening is a host fact; the guest reaching it is not one
    # this host can observe, so the evidence must not claim it was verified.
    assert transfer["guest_fetch_verified"] is False
    assert transfer["guest_fetch_status"] == "unverified_no_guest_implementation"

    # The request the installer sent names the source the window published.
    assert len(channel.requests) == 1
    request = channel.requests[0]
    assert request["package_url"] == transfer["guest_url"]
    assert request["cert_fingerprint"] == transfer["certificate_fingerprint"]
    assert set(request) == LEGACY_REQUEST_KEYS | {"package_url", "cert_fingerprint"}
    # ... and the bytes still went over the channel: the service is a source
    # record, not a second byte path.
    assert channel.sent == [("HelloUniPad.ipa", b"IPA-BYTES")]
    assert installer.last.kwargs["channel"] is not channel
    assert installer.last.kwargs["channel"].inner is channel


def test_the_https_route_reaches_a_real_guest_client(
    workspace: dict[str, Path], ipa: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """callchain finding 2: the https route reaches the channel it really has.

    ``_PackageSourceChannel`` promises "everything else is forwarded untouched
    -- including ``send_package``".  The ``guest_channel`` capability resolves to
    ``unipad.guest.create_install_channel``, which returns a
    :class:`~unipad.guest.client.GuestClient` -- and that object's surface is
    ``install_package`` / ``challenge`` / ``status``.  It has neither
    ``send_package`` nor ``request_install``: the guest package splits the byte
    channel from the JSON-Lines control channel (PRD 9.1) and
    :class:`unipad.ipa.installer.GuestClientChannel` is the adapter that joins
    them.

    The wrapper declares ``send_package`` itself, so
    :func:`unipad.ipa.installer._adapt_channel` -- whose rule is "a client that
    is not already a channel" -- no longer recognised the object underneath and
    never adapted it.  The wrapper then forwarded ``send_package`` and
    ``request_install`` to an object with neither, so every https install on a
    host that actually ships the channel raised ``AttributeError`` instead of
    reporting a stage.

    The https test above never caught this because its fixture channel is a
    ``_RecordingChannel`` that *does* declare both methods -- a shape no
    production capability returns.  This one drives the real capability object
    through the real command, so what is under test is the line in ``_import_ipa``
    that builds the wrapper.
    """
    from unipad.guest import GuestClient
    from unipad.guest.protocol import MSG_INSTALL_RESPONSE

    class _Transport:
        """The client's message transport: records what the guest was asked."""

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def request(self, message: dict) -> dict:
            self.calls.append(dict(message))
            reply = {
                "type": MSG_INSTALL_RESPONSE,
                "id": message["id"],
                "payload": {"stage": "registered", "bundle_id": DEFAULT_BUNDLE_ID},
            }
            return reply

    transport = _Transport()
    real = cli._resolve_capability

    def fake(name: str) -> Any:
        if name == "guest_channel":
            return lambda: GuestClient(transport, attempts=1, backoff_s=0.0)
        if name == "ipa_install":
            return _ChannelExercisingInstaller
        return real(name)

    monkeypatch.setattr(cli, "_resolve_capability", fake)

    result = run_cli(["install", "--transport", "https", str(ipa)], workspace)

    # The chain ran: the stand-in installer drives the channel, the channel
    # drives the client, the client puts one message on the transport.  An
    # ``AttributeError`` on the way never gets that far.
    assert result.exit_code == int(ExitCode.OK), result.document
    assert len(transport.calls) == 1
    assert transport.calls[0]["type"] == "install.request"
    payload = transport.calls[0]["payload"]
    # ...and the request names the package source the window published, which is
    # the only reason the wrapper exists (ADR-0008).
    assert payload["package_url"].startswith("https://10.0.2.2:")
    assert payload["cert_fingerprint"]
    assert payload["package_sha256"]


def _assert_port_answers(port: int) -> None:
    """Prove something *is* listening on ``port``: the evidence is not aspirational."""
    import socket

    with socket.socket() as dial:
        dial.settimeout(2.0)
        assert dial.connect_ex(("127.0.0.1", port)) == 0, f"port {port} does not answer"


def test_the_transfer_window_always_releases_the_port(tmp_path: Path) -> None:
    """The port is free again after the window, whatever the body did.

    ADR-0008 puts the service in the transfer stage precisely because that
    window is bounded: a service that outlived the transfer would be a daemon
    thread on a process that is about to exit, holding a port nobody released.
    """
    window = _transfer_window(tmp_path)
    port = 0

    with pytest.raises(RuntimeError, match="the installer body blew up"):
        with cli._transfer_window(window):  # noqa: SLF001 - the unit under test
            port = window.evidence["port"]
            assert port > 0
            # The recorded port is the one that actually answers, not a number
            # the caller hoped for.
            _assert_port_answers(port)
            raise RuntimeError("the installer body blew up")

    # Released on the way out, exception included.
    _assert_port_is_free(port)


def test_a_failure_after_the_bind_still_releases_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window that never opened must not leave its port behind.

    ``start`` binds and *then* records the evidence.  If anything in that second
    half raises, the ``with`` body never runs -- so a context manager that only
    releases on the way out of the body would leak the socket into a process
    that is about to report a failure.  The port is the resource a *second*
    import trips on, which is the same "address in use" this stage refuses to
    paper over.
    """
    from unipad.host import httpsvc

    bound_ports: list[int] = []

    def refuse_evidence(server: Any, **kwargs: Any) -> dict[str, Any]:
        bound_ports.append(server.port)
        raise RuntimeError("the evidence step blew up")

    monkeypatch.setattr(httpsvc, "service_evidence", refuse_evidence)
    window = _transfer_window(tmp_path)

    with pytest.raises(RuntimeError, match="the evidence step blew up"):
        with cli._transfer_window(window):  # noqa: SLF001 - the unit under test
            raise AssertionError("the window must never open once start has failed")

    assert bound_ports, "the failure has to happen after the socket was bound"
    _assert_port_is_free(bound_ports[0])


def test_a_second_start_refuses_rather_than_orphaning_the_port(tmp_path: Path) -> None:
    """Starting twice must not leak the first socket.

    The failure this prevents is quiet: the second server becomes the recorded
    one, the first keeps the port, and nothing is left holding a reference that
    could stop it.  From the outside that looks exactly like the "port already
    in use" this stage is supposed to refuse for a real reason.
    """
    window = _transfer_window(tmp_path)
    window.start()
    port = window.evidence["port"]
    try:
        with pytest.raises(UniPadError) as excinfo:
            window.start()
        assert excinfo.value.code == ErrorCode.BLOCKED
        assert excinfo.value.details["blocked_reason"] == "package_service_already_running"
        # The first server is untouched and is still the one the evidence names.
        assert window.evidence["port"] == port
        _assert_port_answers(port)
    finally:
        window.stop()

    _assert_port_is_free(port)


def test_a_bound_port_is_refused_rather_than_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0008 decision 4: in use is a refusal, never a silent second port.

    A retry would publish a port the caller never asked for while the recorded
    URL still named the first one, so the evidence would describe a service
    that did not listen.  This test binds the port for real and counts the bind
    attempts.
    """
    import socket

    from unipad.host import httpsvc

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        monkeypatch.setattr(cli, "_package_service_port", lambda: port)

        attempts: list[int] = []
        real_server = httpsvc.PackageServer

        class Counting(real_server):  # type: ignore[misc,valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                attempts.append(kwargs.get("port", -1))
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpsvc, "PackageServer", Counting)

        window = _transfer_window(tmp_path)
        with pytest.raises(UniPadError) as excinfo:
            window.start()

    # Exactly one attempt, on the port that was asked for.
    assert attempts == [port]
    assert excinfo.value.code == ErrorCode.BLOCKED
    assert excinfo.value.details["blocked_reason"] == "package_service_unavailable"
    # A service that did not listen is a host condition, not a transfer
    # failure: "transfer_failed" would tell the operator the bytes broke.
    assert excinfo.value.details["service_error"] == ErrorCode.TRANSFER_FAILED


def test_a_service_that_did_not_listen_is_a_refusal_that_leaves_its_log(
    workspace: dict[str, Path],
    ipa: Path,
    channel_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal reaches the operator as a refusal, and leaves install.log."""
    import socket

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        monkeypatch.setattr(cli, "_package_service_port", lambda: occupied.getsockname()[1])

        result = run_cli(["install", "--transport", "https", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.error_code == ErrorCode.BLOCKED
    assert result.details["blocked_reason"] == "package_service_unavailable"
    logs = sorted((workspace["runs"]).rglob("install.log"))
    assert logs, "every other refusal in this chain leaves install.log behind"
    assert "package_service=" in logs[0].read_text(encoding="utf-8")


def test_the_caller_never_relaxes_the_service_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loopback and TLS stay the defaults, and nothing here argues otherwise.

    The check is on the *arguments*: the only way to reach a plaintext or
    non-loopback PackageServer is to pass ``allow_plaintext`` or
    ``allow_non_loopback`` by name, and this caller passes neither.
    """
    from unipad.host import httpsvc

    seen_args: list[Any] = []
    seen_kwargs: dict[str, Any] = {}
    real_server = httpsvc.PackageServer

    class Recording(real_server):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen_args.extend(args)
            seen_kwargs.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpsvc, "PackageServer", Recording)

    window = _transfer_window(tmp_path)
    with cli._transfer_window(window):  # noqa: SLF001 - the unit under test
        pass

    # Nothing is passed positionally, and the only keyword arguments are the
    # package directory and the port: no host, no scheme, no opt-in.
    assert seen_args == []
    assert set(seen_kwargs) == {"root", "port"}
    assert seen_kwargs["root"] == tmp_path
    assert seen_kwargs["port"] == httpsvc.DEFAULT_GUEST_PORT
    assert window.evidence["bind_host"] == "127.0.0.1"
    assert window.evidence["transport"] == "https"


def test_the_run_evidence_records_the_service_that_listened(
    workspace: dict[str, Path], ipa: Path, channel_chain: Any
) -> None:
    """install.json and the envelope both name the URL and the fingerprint."""
    result = run_cli(["install", "--transport", "https", str(ipa)], workspace)
    assert result.exit_code == int(ExitCode.OK)

    # The run directory is resolved the way an operator would find it rather than
    # through ``result.data["run_dir"]``, which redaction turned into a string
    # that is not a path (PRD 9.2).  ``run_dir_of`` still proves the envelope
    # names install.log, which is the half of the contract redaction must not
    # break: an operator reading the envelope must be able to tell which file in
    # which run holds the evidence.
    run_dir = run_dir_of(workspace, result, named="install.log")
    record = json.loads((run_dir / "install.json").read_text(encoding="utf-8"))
    assert record["transport"] == "https"
    assert record["transfer"]["guest_url"] == result.data["transfer"]["guest_url"]
    assert (
        record["transfer"]["certificate_fingerprint"]
        == result.data["transfer"]["certificate_fingerprint"]
    )

    log = (run_dir / "install.log").read_text(encoding="utf-8")
    assert "package_service=listening" in log
    assert f"port={result.data['transfer']['port']}" in log
    assert f"fingerprint={result.data['transfer']['certificate_fingerprint']}" in log
    assert "transport=https" in log


def test_the_channel_route_records_no_service_evidence_in_the_run_record(
    workspace: dict[str, Path], ipa: Path, channel_chain: Any
) -> None:
    """The default route's run record is the one it always was."""
    result = run_cli(["install", str(ipa)], workspace)
    assert result.exit_code == int(ExitCode.OK)

    # Resolved through the runs root, not the redacted ``run_dir`` string; see
    # the https test above for why the envelope cannot be parsed back into a path.
    run_dir = run_dir_of(workspace, result, named="install.log")
    record = json.loads((run_dir / "install.json").read_text(encoding="utf-8"))
    assert record["transport"] == "channel"
    assert "transfer" not in record
    assert "package_service" not in (run_dir / "install.log").read_text(encoding="utf-8")


def test_a_refusal_names_the_transport_it_was_publishing_on(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """ADR-0008: the gate must report the transport that was actually used.

    A refusal that never reached ``_finish_install`` used to report no transport
    at all, so an operator reading the envelope could not tell which route the
    install had asked for -- and the ``package_service_unavailable`` refusal in
    particular names a *service* without saying why one was being started.
    """
    plain = run_cli(["install", str(ipa)], workspace)

    assert plain.exit_code == int(ExitCode.BLOCKED)
    assert plain.details["blocked_reason"] == "guest_channel_unavailable"
    assert plain.details["data"]["transport"] == "channel"


def test_the_package_service_refusal_names_the_https_route(
    workspace: dict[str, Path],
    ipa: Path,
    channel_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal about the service also names the route that wanted one.

    Without it the envelope reports a service failure and leaves the reader to
    infer that ``--transport https`` was in play -- the same "wrong transport
    looks like a bad package" confusion the ``channel`` default exists to avoid.
    """
    import socket

    channel, installer = channel_chain

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        monkeypatch.setattr(cli, "_package_service_port", lambda: occupied.getsockname()[1])

        served = run_cli(["install", "--transport", "https", str(ipa)], workspace)

    assert served.exit_code == int(ExitCode.BLOCKED)
    assert served.details["blocked_reason"] == "package_service_unavailable"
    assert served.details["data"]["transport"] == "https"


def test_an_unknown_transport_is_a_usage_error_not_a_silent_default() -> None:
    """A transport the operator believes they asked for is the one they get.

    ``argparse`` already restricts the surface; this is the same check for a
    caller that reaches ``_import_ipa`` without the parser, where falling back
    to the default would quietly publish over a route nobody chose.
    """
    import argparse

    with pytest.raises(UniPadError) as excinfo:
        cli._transfer_transport(  # noqa: SLF001 - the unit under test
            argparse.Namespace(transport="carrier-pigeon"), command="install"
        )
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert excinfo.value.details["transport"] == "carrier-pigeon"
    assert excinfo.value.details["known"] == ["channel", "https"]

    # ... and an absent attribute still means the documented default.
    assert (
        cli._transfer_transport(argparse.Namespace(), command="install")  # noqa: SLF001
        == cli.TRANSPORT_CHANNEL
    )
