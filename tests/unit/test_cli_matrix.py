"""CLI surface for the compatibility matrix (PRD 3.2 P1 (2), docs/PRD.md:87).

PRD 3.2 P1 asks for 更多自有或获授权的 IPA 兼容性测试.  The library half of that lives in
:mod:`unipad.ipa.matrix` -- the three-part key, the closed vocabularies, the
authorisation gate, the append-only JSONL store.  This file proves the
*controller* half: the subcommand that lets an operator ask the matrix what it
holds, and append an observation to it, and get one machine-readable object back.

What is being defended here:

* **The matrix has a writer in the product, not only in a test.**  Before this
  surface existed the store was read by ``doctor`` and by nothing else, so an
  observation could only ever be produced by a probe writing into a temporary
  directory.  ``unipad matrix record`` is the path an operator takes, and the
  tests below drive that path against a real file under the runs root.
* **The verdict is derived, never chosen.**  The command runs the same precheck
  the install chain runs and lets :meth:`MatrixEntry.from_precheck` decide.  On a
  host with no iPadOS guest that derivation can only ever produce ``untested`` or
  ``rejected`` -- never ``installs-only`` or ``installs-and-runs`` -- so no flag
  combination turns a host-side check into a compatibility claim.
* **The authorisation gate holds through the CLI.**  ``not-authorised`` is
  refused by the module and the command reports the refusal as a structured
  ``blocked`` envelope, having appended nothing.  A gate that only holds when the
  caller is a test is not a gate.
* **The three-part key is not optional.**  A missing part is a usage error, not a
  coarser key: ``MatrixKey`` refuses a blank part, and the command refuses to
  build one rather than substituting a default.
* **A question with a known answer is not a failure.**  An empty matrix, and a
  bundle nothing recorded, both return exit 0 -- the operator asked what the
  matrix holds and the honest answer is "nothing".
* **Skipped lines are split by cause.**  A corrupt line is damage to repair and a
  refused line is a line this module's own gate turned away; summing them would
  hide the second inside the first, and the second is the security signal.
* **No secrets (PRD 9.2).**  ``package_path`` is a host absolute path, so it is
  written to the file and withheld from stdout; the matrix location is relative
  to the runs root for the same reason ``doctor`` withholds it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from unipad import __version__
from unipad.acceptance import p1
from unipad.controller import cli
from unipad.errors import ErrorCode, ExitCode
from unipad.ipa import matrix as matrix_module
from unipad.ipa.matrix import (
    POSITIVE_VERDICTS,
    Authorisation,
    CompatibilityMatrix,
    MatrixEntry,
    MatrixKey,
    TrustCategory,
    Verdict,
)
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

#: The three commands PRD 3.2 P1 (3) adds.
LIFECYCLE_COMMANDS: frozenset[str] = frozenset({"history", "uninstall", "update"})

#: The one command PRD 3.2 P1 (2) adds.
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

#: A key every test can build.  The three parts are the PRD 10 triple.
KEY_PARTS: tuple[str, str, str] = ("1.0.0", "21H461", "tcg-ramfb")

BUDGET_CONFIG: dict[str, object] = {
    "schema_version": 1,
    "per_hypothesis_max_experiments": 3,
    "same_failure_signature_max": 3,
    "max_actions_minutes_per_week": 0,
    "max_model_tokens_per_session": None,
    "max_local_run_minutes_per_experiment": 45,
}

#: Repository root: tests/unit/test_cli_matrix.py -> repo.
REPO_ROOT = Path(__file__).resolve().parents[2]


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
    """Parse ``text`` and assert it holds exactly one JSON document."""
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


def default_matrix_path(workspace: dict[str, Path]) -> Path:
    """The matrix the CLI writes when the operator names no other."""
    return workspace["runs"] / "lifecycle" / "compatibility-matrix.jsonl"


def matrix_lines(path: Path) -> list[str]:
    """The non-blank lines of a matrix file, or ``[]`` when there is none."""
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def ipa(workspace: dict[str, Path]) -> Path:
    """A real, acceptable Device IPA built by the fixture builder."""
    return build_ipa(workspace["root"] / "HelloUniPad.ipa")


def _record_argv(
    ipa: Path,
    *,
    authorisation: str = str(Authorisation.OWN_SOURCE),
    trust_category: str = str(TrustCategory.DEVELOPMENT_SIGNED),
    key: tuple[str, str, str] = KEY_PARTS,
) -> list[str]:
    """The ``matrix record`` invocation every record test starts from."""
    return [
        "matrix",
        "record",
        str(ipa),
        "--package-version",
        key[0],
        "--system-build",
        key[1],
        "--run-config",
        key[2],
        "--authorisation",
        authorisation,
        "--trust-category",
        trust_category,
    ]


# ---------------------------------------------------------------------------
# command registration
# ---------------------------------------------------------------------------


def test_the_matrix_command_is_registered_and_wired_to_its_handler() -> None:
    """``matrix`` is a real subcommand, not a name in a table.

    Parsed through the production parser: a subparser entry with no handler, or a
    handler with no subparser, would leave ``unipad matrix`` answering
    ``unsupported_command`` while every list in the module said otherwise.
    """
    parser = cli.build_parser()
    args = parser.parse_args(["matrix"])
    assert args.command == "matrix"
    assert args.handler is cli.cmd_matrix
    # The read action is what an operator gets who says nothing.
    assert args.action == "report"

    recorded = parser.parse_args([*_record_argv(Path("HelloUniPad.ipa"))])
    assert recorded.action == "record"
    assert recorded.handler is cli.cmd_matrix


def test_the_matrix_command_is_registered_apart_from_the_other_two_tables() -> None:
    """PRD 3.2 P1 (2) adds a command; it rewrites neither PRD list."""
    assert set(cli.MATRIX_COMMANDS) == MATRIX_COMMANDS
    assert set(cli._MATRIX_COMMAND_TABLE) == MATRIX_COMMANDS  # noqa: SLF001 - contract
    assert set(cli.COMMANDS) == PRD_9_1_COMMANDS
    assert set(cli.LIFECYCLE_COMMANDS) == LIFECYCLE_COMMANDS
    assert set(cli.SUPPORTED_COMMANDS) == PRD_9_1_COMMANDS | LIFECYCLE_COMMANDS | MATRIX_COMMANDS


def test_the_matrix_is_a_probed_capability_not_a_hard_import() -> None:
    """A ``unipad.ipa`` that cannot import must read as ``blocked``, not a crash.

    The controller deliberately keeps ``unipad.ipa`` out of its import graph and
    resolves every entry point through :data:`_CAPABILITIES`; the matrix, the
    package opener and the package precheck are not exceptions.
    """
    assert cli._CAPABILITIES["compatibility_matrix"] == (  # noqa: SLF001 - contract
        ("unipad.ipa.matrix", "CompatibilityMatrix"),
    )
    assert cli._CAPABILITIES["ipa_open"] == (("unipad.ipa.package", "open_ipa"),)  # noqa: SLF001
    assert cli._CAPABILITIES["ipa_precheck_package"] == (  # noqa: SLF001
        ("unipad.ipa.precheck", "run_precheck_package"),
    )
    for name in ("compatibility_matrix", "ipa_open", "ipa_precheck_package"):
        assert cli._resolve_capability(name) is not None, name
        assert cli._probed(name), name


def test_main_accepts_the_matrix_command(workspace: dict[str, Path]) -> None:
    """An unknown command is exit 2; ``matrix`` is not unknown."""
    unknown = run_cli(["frobnicate"], workspace)
    assert unknown.exit_code == int(ExitCode.USAGE)
    assert unknown.error_code == ErrorCode.UNSUPPORTED_COMMAND

    for argv in (["matrix"], ["matrix", "record", "absent.ipa"]):
        result = run_cli(argv, workspace)
        assert result.document["command"] == "matrix", argv
        assert result.error_code != ErrorCode.UNSUPPORTED_COMMAND


def test_both_matrix_actions_emit_the_prd_9_1_envelope(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """One JSON object, the same nine keys, whatever the outcome."""
    invocations = [
        ["matrix"],
        ["matrix", "report"],
        ["matrix", "report", "--bundle", DEFAULT_BUNDLE_ID],
        _record_argv(ipa),
        ["matrix", "record", "absent.ipa"],
    ]
    for argv in invocations:
        result = run_cli(argv, workspace)
        assert set(result.document) == ENVELOPE_KEYS, argv
        assert result.document["unipad_version"] == __version__
        assert result.document["schema_version"] == cli.ENVELOPE_SCHEMA_VERSION
        assert isinstance(result.document["data"], dict)
        assert result.document["exit_code"] == result.exit_code
        assert result.document["ok"] is (result.exit_code == int(ExitCode.OK))


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_of_an_absent_matrix_is_an_empty_true_answer(
    workspace: dict[str, Path],
) -> None:
    """Nothing recorded is an answer, not a failure (exit 0)."""
    result = run_cli(["matrix"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.status == "ok"
    assert result.document["error"] is None
    assert result.data["matrix_present"] is False
    assert result.data["entry_count"] == 0
    assert result.data["entries"] == []
    assert result.data["unreadable_lines"] == 0
    # A read never grows the file it read.
    assert not default_matrix_path(workspace).exists()


def test_report_lists_the_current_entries_and_counts_every_verdict(
    workspace: dict[str, Path],
) -> None:
    """``counts`` names every verdict, zero included, and sums to the entries."""
    path = default_matrix_path(workspace)
    store = CompatibilityMatrix(path)
    for sha in ("a" * 64, "b" * 64):
        store.record(
            MatrixEntry(
                key=MatrixKey(*KEY_PARTS),
                authorisation=Authorisation.OWN_SOURCE,
                trust_category=TrustCategory.DEVELOPMENT_SIGNED,
                bundle_id=DEFAULT_BUNDLE_ID,
                package_sha256=sha,
                verdict=Verdict.UNTESTED,
            )
        )
    store.record(
        MatrixEntry(
            key=MatrixKey("2.0.0", *KEY_PARTS[1:]),
            authorisation=Authorisation.OWN_SOURCE,
            trust_category=TrustCategory.DEVELOPMENT_SIGNED,
            bundle_id="com.unipad.notes",
            package_sha256="c" * 64,
            verdict=Verdict.UNTESTED,
        )
    )

    result = run_cli(["matrix"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["matrix_present"] is True
    assert result.data["entry_count"] == 2, "two keys, so two current entries"
    assert result.data["observation_count"] == 3, "every line is an observation"
    # The whole matrix, so the tally is the module's own.
    assert result.data["counts"] == {str(v): n for v, n in store.counts().items()}
    assert sum(result.data["counts"].values()) == result.data["entry_count"]
    assert result.data["counts"]["installs-and-runs"] == 0


def test_report_of_one_bundle_narrows_to_that_bundle(workspace: dict[str, Path]) -> None:
    path = default_matrix_path(workspace)
    store = CompatibilityMatrix(path)
    for bundle in (DEFAULT_BUNDLE_ID, "com.unipad.notes"):
        store.record(
            MatrixEntry(
                key=MatrixKey("1.0.0", *KEY_PARTS[1:]),
                authorisation=Authorisation.OWN_SOURCE,
                trust_category=TrustCategory.DEVELOPMENT_SIGNED,
                bundle_id=bundle,
                package_sha256="a" * 64,
                verdict=Verdict.UNTESTED,
            )
        )

    result = run_cli(["matrix", "report", "--bundle", "com.unipad.notes"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["selector"]["kind"] == "bundle"
    assert result.data["selector"]["bundle_id"] == "com.unipad.notes"
    assert result.data["entry_count"] == 1
    assert [entry["bundle_id"] for entry in result.data["entries"]] == ["com.unipad.notes"]


def test_report_of_one_key_with_history_reports_every_observation(
    workspace: dict[str, Path],
) -> None:
    """``--history`` answers "what was ever observed", not only "what is current"."""
    path = default_matrix_path(workspace)
    store = CompatibilityMatrix(path)
    for sha in ("a" * 64, "b" * 64):
        store.record(
            MatrixEntry(
                key=MatrixKey(*KEY_PARTS),
                authorisation=Authorisation.OWN_SOURCE,
                trust_category=TrustCategory.DEVELOPMENT_SIGNED,
                bundle_id=DEFAULT_BUNDLE_ID,
                package_sha256=sha,
                verdict=Verdict.UNTESTED,
            )
        )

    without = run_cli(["matrix", "report", "--key", *KEY_PARTS], workspace)
    assert without.data["entry_count"] == 1
    assert "observations" not in without.data

    with_history = run_cli(["matrix", "report", "--key", *KEY_PARTS, "--history"], workspace)
    assert with_history.data["entry_count"] == 1
    assert with_history.data["observation_count"] == 2
    assert [entry["package_sha256"] for entry in with_history.data["observations"]] == [
        "a" * 64,
        "b" * 64,
    ]


def test_report_of_several_keys_with_history_keeps_first_seen_order(
    workspace: dict[str, Path],
) -> None:
    """``--history`` over many keys is ordered, and no key is answered twice.

    This is the path where the report has to de-duplicate the keys it is about
    to read: ``entries`` holds one current entry per key, and the same key can
    appear under a bundle filter too.  A de-duplication that loses order makes
    the answer depend on dict iteration, and one that keeps duplicates reports
    the same observation twice -- both of which an operator reads as the matrix
    having recorded something it did not.
    """
    path = default_matrix_path(workspace)
    store = CompatibilityMatrix(path)
    keys = [
        MatrixKey(*KEY_PARTS),
        MatrixKey("2.0.0", KEY_PARTS[1], KEY_PARTS[2]),
        MatrixKey(*KEY_PARTS),
    ]
    for index, key in enumerate(keys):
        store.record(
            MatrixEntry(
                key=key,
                authorisation=Authorisation.OWN_SOURCE,
                trust_category=TrustCategory.DEVELOPMENT_SIGNED,
                bundle_id=DEFAULT_BUNDLE_ID,
                package_sha256=f"{index:064x}",
                verdict=Verdict.UNTESTED,
            )
        )

    result = run_cli(["matrix", "report", "--history"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["entry_count"] == 2
    assert result.data["observation_count"] == 3
    # First-seen order: the first key's two observations, then the second key's.
    assert [entry["package_sha256"] for entry in result.data["observations"]] == [
        f"{0:064x}",
        f"{2:064x}",
        f"{1:064x}",
    ]


def test_the_report_key_set_is_a_hash_membership_not_a_linear_scan(
    workspace: dict[str, Path],
    monkeypatch,
) -> None:
    """De-duplicating the reported keys costs O(k), not O(k^2).

    ``if entry.key not in keys`` over a list compares every new key against every
    key already collected, so a matrix that has been appended to for a while --
    exactly the matrix this command exists to report on -- pays a quadratic scan
    before it reads a single observation.  A set or ``dict.fromkeys`` asks the
    hash table instead, and the answer is the same, which is why nothing but a
    count of the comparisons can tell the two apart.

    The count is taken by wrapping ``MatrixKey.__eq__``: a linear scan reaches it
    once per pair, a hash membership only on a hash collision.  The bound below is
    generous on purpose -- what it has to reject is the quadratic shape, not a
    particular implementation of the linear one.
    """
    distinct = 200
    store = CompatibilityMatrix(default_matrix_path(workspace))
    for index in range(distinct):
        store.record(
            MatrixEntry(
                key=MatrixKey(f"{index}.0.0", *KEY_PARTS[1:]),
                authorisation=Authorisation.OWN_SOURCE,
                trust_category=TrustCategory.DEVELOPMENT_SIGNED,
                bundle_id=DEFAULT_BUNDLE_ID,
                package_sha256=f"{index:064x}",
                verdict=Verdict.UNTESTED,
            )
        )

    real_eq = matrix_module.MatrixKey.__eq__
    comparisons = 0

    def counting_eq(self, other):
        nonlocal comparisons
        comparisons += 1
        return real_eq(self, other)

    monkeypatch.setattr(matrix_module.MatrixKey, "__eq__", counting_eq)

    result = run_cli(["matrix", "report", "--history"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["entry_count"] == distinct
    assert result.data["observation_count"] == distinct
    # k comparisons would be one per key; k^2/2 for the list form is ~19,900 here.
    assert comparisons <= 4 * distinct, (
        f"{comparisons} key comparisons for {distinct} keys: the report is "
        "de-duplicating with a linear scan, not a hash membership"
    )


def test_the_report_of_one_bundle_carries_every_keys_history(
    workspace: dict[str, Path],
) -> None:
    """A bundle filter narrows by app, not by observation.

    A bundle id is not part of the key, so one bundle can hold several keys and
    each of them can hold several observations.  Narrowing to the bundle must
    still answer "what was ever observed for this app" with all of it: a filter
    that reported only the current entry per key would tell an operator the
    rebuild that fixed (or broke) a version had never happened.
    """
    path = default_matrix_path(workspace)
    store = CompatibilityMatrix(path)
    for version in ("1.0.0", "2.0.0"):
        for sample in ("a" * 64, "b" * 64):
            store.record(
                MatrixEntry(
                    key=MatrixKey(version, *KEY_PARTS[1:]),
                    authorisation=Authorisation.OWN_SOURCE,
                    trust_category=TrustCategory.DEVELOPMENT_SIGNED,
                    bundle_id=DEFAULT_BUNDLE_ID,
                    package_sha256=sample,
                    verdict=Verdict.UNTESTED,
                )
            )
    store.record(
        MatrixEntry(
            key=MatrixKey("9.9.9", *KEY_PARTS[1:]),
            authorisation=Authorisation.OWN_SOURCE,
            trust_category=TrustCategory.DEVELOPMENT_SIGNED,
            bundle_id="com.someone.else",
            package_sha256="c" * 64,
            verdict=Verdict.UNTESTED,
        )
    )

    result = run_cli(["matrix", "report", "--bundle", DEFAULT_BUNDLE_ID, "--history"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["entry_count"] == 2
    assert result.data["observation_count"] == 4
    assert [entry["package_sha256"] for entry in result.data["observations"]] == [
        "a" * 64,
        "b" * 64,
        "a" * 64,
        "b" * 64,
    ]


def test_report_splits_the_lines_it_could_not_read_by_cause(workspace: dict[str, Path]) -> None:
    """A refused line is a security signal and must not read as disk damage."""
    path = default_matrix_path(workspace)
    store = CompatibilityMatrix(path)
    store.record(
        MatrixEntry(
            key=MatrixKey(*KEY_PARTS),
            authorisation=Authorisation.OWN_SOURCE,
            trust_category=TrustCategory.DEVELOPMENT_SIGNED,
            bundle_id=DEFAULT_BUNDLE_ID,
            package_sha256="a" * 64,
            verdict=Verdict.UNTESTED,
        )
    )
    with path.open("a", encoding="utf-8") as handle:
        # Damage: parses as JSON, is not an entry's shape.
        handle.write(json.dumps({"package_version": "1.0.0"}) + "\n")
        # Refused: a well-formed entry the authorisation gate turns away.
        handle.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "key": {
                        "package_version": "9.9.9",
                        "system_build": KEY_PARTS[1],
                        "run_config": KEY_PARTS[2],
                    },
                    "authorisation": "not-authorised",
                    "trust_category": "development-signed",
                    "bundle_id": DEFAULT_BUNDLE_ID,
                    "package_sha256": "d" * 64,
                    "verdict": "untested",
                }
            )
            + "\n"
        )

    result = run_cli(["matrix"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["unreadable_lines"] == 2
    assert result.data["corrupt_lines"] == 1
    assert result.data["refused_lines"] == 1
    # The one entry the loader did accept is still reported.
    assert result.data["entry_count"] == 1


def test_a_matrix_path_that_is_not_a_file_is_refused_not_read_as_empty(
    workspace: dict[str, Path],
) -> None:
    """A directory at the matrix path must not answer "nothing is recorded".

    ``CompatibilityMatrix`` tests ``is_file()``, so without this guard a directory
    would read as an empty matrix -- a wrong answer about the evidence dressed up
    as a true one.
    """
    bogus = default_matrix_path(workspace)
    bogus.mkdir(parents=True, exist_ok=True)

    report = run_cli(["matrix"], workspace)
    assert report.exit_code == int(ExitCode.BLOCKED)
    assert report.details["blocked_reason"] == "matrix_not_a_file"
    assert report.data == {}

    record = run_cli(_record_argv(workspace["root"] / "absent.ipa"), workspace)
    assert record.exit_code == int(ExitCode.BLOCKED)
    assert record.details["blocked_reason"] == "matrix_not_a_file"


def test_the_matrix_path_is_reported_and_overridable(workspace: dict[str, Path]) -> None:
    path = default_matrix_path(workspace)
    CompatibilityMatrix(path).record(
        MatrixEntry(
            key=MatrixKey(*KEY_PARTS),
            authorisation=Authorisation.OWN_SOURCE,
            trust_category=TrustCategory.DEVELOPMENT_SIGNED,
            bundle_id=DEFAULT_BUNDLE_ID,
            package_sha256="a" * 64,
            verdict=Verdict.UNTESTED,
        )
    )

    default = run_cli(["matrix"], workspace)
    assert default.data["matrix"] == "lifecycle/compatibility-matrix.jsonl"
    assert default.data["matrix_overridden"] is False

    custom = workspace["root"] / "elsewhere" / "matrix.jsonl"
    result = run_cli(["matrix", "--matrix", str(custom)], workspace)
    # Outside the runs root, so only the final component is named -- the same
    # redaction ``doctor`` applies to this store (PRD 9.2).
    assert result.data["matrix"] == custom.name
    assert result.data["matrix_overridden"] is True
    assert result.data["entry_count"] == 0
    assert result.data["matrix_present"] is False


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


def test_record_appends_the_verdict_the_precheck_derived(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """The core of the surface: an observation goes in through the product.

    Everything reported is read off the package and the precheck the install
    chain itself runs -- the CLI derives nothing and invents nothing.  On a host
    with no guest the derived verdict can only be ``untested``.
    """
    result = run_cli(_record_argv(ipa), workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.status == "ok"
    assert result.document["error"] is None
    assert result.data["verdict"] == str(Verdict.UNTESTED)
    assert result.data["bundle_id"] == DEFAULT_BUNDLE_ID
    assert result.data["app_name"]
    assert len(result.data["package_sha256"]) == 64
    assert result.data["key"] == {
        "package_version": KEY_PARTS[0],
        "system_build": KEY_PARTS[1],
        "run_config": KEY_PARTS[2],
    }
    assert result.data["authorisation"] == str(Authorisation.OWN_SOURCE)
    assert result.data["trust_category"] == str(TrustCategory.DEVELOPMENT_SIGNED)
    assert result.data["install_stage"] == "", "no guest, so no stage was reached"
    assert result.data["signing_mode"]
    # The two conditions a host can read off the package are recorded, and the
    # one that needs an install attempt is not.
    assert set(result.data["established_conditions"]) == {"drm-free", "arm64-present"}

    path = default_matrix_path(workspace)
    lines = matrix_lines(path)
    assert len(lines) == 1, "exactly one observation was appended"
    stored = CompatibilityMatrix(path)
    assert len(stored.entries) == 1
    assert stored.unreadable_lines == 0
    entry = stored.entries[0]
    assert entry.bundle_id == DEFAULT_BUNDLE_ID
    assert entry.package_sha256 == result.data["package_sha256"]
    assert entry.verdict is Verdict.UNTESTED
    assert entry.install_stage == ""
    # The file is the record, so it carries what stdout withholds.
    assert entry.package_path


def test_record_reports_the_observation_a_second_invocation_can_read_back(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """The write path and the read path are the same store."""
    recorded = run_cli(_record_argv(ipa), workspace)
    assert recorded.exit_code == int(ExitCode.OK)

    report = run_cli(["matrix", "report", "--bundle", DEFAULT_BUNDLE_ID], workspace)
    assert report.data["entry_count"] == 1
    entry = report.data["entries"][0]
    assert entry["package_sha256"] == recorded.data["package_sha256"]
    assert entry["verdict"] == str(Verdict.UNTESTED)
    assert entry["key"] == recorded.data["key"]


def test_record_derives_rejected_from_a_precheck_fault(
    workspace: dict[str, Path], tmp_path: Path
) -> None:
    """A package the precheck refuses is recorded as refused, with its reason."""
    broken = build_ipa(workspace["root"] / "Simulator.ipa", corrupt="simulator")

    result = run_cli(_record_argv(broken), workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["verdict"] == str(Verdict.REJECTED)
    assert result.data["error_code"], "a rejection names the stable code that fired"
    assert result.data["reason"], "a rejection names which rule fired"
    # The conditions are still the ones the host could read off the package: a
    # simulator slice is not ARM64, so ``arm64-present`` is absent, and the
    # rejection does not suppress what *was* established.
    assert "arm64-present" not in result.data["established_conditions"]
    stored = CompatibilityMatrix(default_matrix_path(workspace))
    assert stored.entries[0].verdict is Verdict.REJECTED


def test_no_invocation_of_record_can_produce_a_positive_verdict(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """``installs-only`` / ``installs-and-runs`` need a guest, by construction.

    The command exposes no verdict flag, so the only way a positive verdict could
    appear here is a derivation that claims a stage the host never reached.  This
    is the assertion that would catch it.
    """
    result = run_cli(_record_argv(ipa), workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["verdict"] not in {str(v) for v in POSITIVE_VERDICTS}
    stored = CompatibilityMatrix(default_matrix_path(workspace))
    assert all(entry.verdict not in POSITIVE_VERDICTS for entry in stored.observations)


def test_record_refuses_a_package_that_is_not_authorised(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """PRD 12: 公开可得不等于已获得再分发许可.  The gate holds through the CLI."""
    result = run_cli(
        _record_argv(ipa, authorisation=str(Authorisation.NOT_AUTHORISED)), workspace
    )

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.error_code == ErrorCode.BLOCKED
    assert result.details["reason"] == "not_authorised"
    # Nothing was appended: a refused observation is not an observation.
    assert matrix_lines(default_matrix_path(workspace)) == []


def test_record_refuses_a_second_trust_category_under_one_key(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """PRD 5.1: two support categories may not share one key (PRD 10)."""
    first = run_cli(_record_argv(ipa), workspace)
    assert first.exit_code == int(ExitCode.OK)

    second = run_cli(
        _record_argv(ipa, trust_category=str(TrustCategory.OWNER_TRUSTED)), workspace
    )

    assert second.exit_code == int(ExitCode.USAGE)
    assert second.error_code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert second.details["reason"] == "trust_category_conflict"
    # The conflict is reported, and the first observation is still the only line.
    assert len(matrix_lines(default_matrix_path(workspace))) == 1
    stored = CompatibilityMatrix(default_matrix_path(workspace))
    assert stored.entries[0].trust_category is TrustCategory.DEVELOPMENT_SIGNED


def test_record_refuses_an_authorisation_outside_the_closed_vocabulary(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """A near-miss is not rounded to a neighbour (PRD 5.1)."""
    result = run_cli(_record_argv(ipa, authorisation="probably-fine"), workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID
    assert "probably-fine" in json.dumps(result.document)
    assert "own-source" in json.dumps(result.document)
    assert matrix_lines(default_matrix_path(workspace)) == []


def test_record_refuses_a_trust_category_outside_the_closed_vocabulary(
    workspace: dict[str, Path], ipa: Path
) -> None:
    result = run_cli(_record_argv(ipa, trust_category="app-store"), workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID
    assert matrix_lines(default_matrix_path(workspace)) == []


def test_record_requires_all_three_parts_of_the_key(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """A blank part is not a coarser key; it is an unnamed key (PRD 10)."""
    argv = _record_argv(ipa)
    # Drop --run-config and its value.
    trimmed = argv[: argv.index("--run-config")] + argv[argv.index("--run-config") + 2 :]

    result = run_cli(trimmed, workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID
    assert result.details["command"] == "matrix"
    assert matrix_lines(default_matrix_path(workspace)) == []


def test_record_refuses_a_blank_key_part_rather_than_widening_the_key(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """``--run-config ""`` is a usage error, not "any configuration"."""
    result = run_cli(_record_argv(ipa, key=(KEY_PARTS[0], KEY_PARTS[1], "   ")), workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID
    assert matrix_lines(default_matrix_path(workspace)) == []


def test_record_refuses_an_ipa_that_is_not_there(workspace: dict[str, Path]) -> None:
    """The same code and exit the install chain gives for a missing package."""
    result = run_cli(_record_argv(workspace["root"] / "absent.ipa"), workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.ARTIFACT_MISSING
    # The name is reported, never the path (PRD 9.2).
    assert result.details["path"] == "absent.ipa"
    assert matrix_lines(default_matrix_path(workspace)) == []


def test_record_requires_an_ipa_and_report_takes_none(workspace: dict[str, Path]) -> None:
    missing_ipa = run_cli(["matrix", "record"], workspace)
    assert missing_ipa.exit_code == int(ExitCode.USAGE)
    assert missing_ipa.error_code == ErrorCode.CONFIG_INVALID

    with_ipa = run_cli(["matrix", "report", "HelloUniPad.ipa"], workspace)
    assert with_ipa.exit_code == int(ExitCode.USAGE)
    assert with_ipa.error_code == ErrorCode.CONFIG_INVALID


def test_record_on_a_broken_ipa_reports_the_name_and_not_the_path(
    workspace: dict[str, Path],
) -> None:
    """PRD 9.2: a structurally broken IPA must not leak the operator's path.

    ``open_ipa`` refuses a non-zip with ``path=<the path it was handed>``, and
    that path is absolute -- on this machine it embeds the Windows username.
    The install chain never has this exposure because ``run_precheck`` catches
    the same error and keeps only its code, reason and message; ``record`` calls
    ``open_ipa`` directly, so the redaction is this surface's to do.

    Asserted on the *decoded* detail, not on the raw stdout: JSON escapes a
    backslash, so a substring check for the Windows path form reads a leak as
    clean.  That is exactly the mistake this test exists to stop being made
    again.
    """
    broken = workspace["root"] / "not-an-ipa.ipa"
    broken.write_bytes(b"this is not a zip archive")

    result = run_cli(_record_argv(broken), workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.INVALID_PACKAGE
    # The stable reason survives: what an operator acts on is not withheld.
    assert result.details["reason"] == "not_a_zip"
    # The location does not.
    assert result.details["path"] == broken.name
    assert str(workspace["root"]) not in json.dumps(result.document)
    assert str(broken) not in json.dumps(result.document)
    assert broken.as_posix() not in json.dumps(result.document)
    # ...and nothing was recorded against a package that could not be read.
    assert matrix_lines(default_matrix_path(workspace)) == []


def test_record_into_an_overridden_matrix(workspace: dict[str, Path], ipa: Path) -> None:
    custom = workspace["root"] / "elsewhere" / "matrix.jsonl"
    result = run_cli([*_record_argv(ipa), "--matrix", str(custom)], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["matrix"] == custom.name
    assert result.data["matrix_overridden"] is True
    assert len(matrix_lines(custom)) == 1
    # The default location is untouched, so the override really was one.
    assert not default_matrix_path(workspace).exists()


# ---------------------------------------------------------------------------
# PRD 9.2
# ---------------------------------------------------------------------------


def test_no_matrix_envelope_ever_carries_a_host_path(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """``package_path`` and the runs root are the two things stdout must withhold.

    ``package_path`` is an absolute host path that embeds the Windows username,
    so it is written to the file -- where the record belongs -- and dropped from
    the envelope.  The matrix location is relative to the runs root, exactly as
    ``doctor`` reports it, for the same reason.
    """
    recorded = run_cli(_record_argv(ipa), workspace)
    assert recorded.exit_code == int(ExitCode.OK)
    assert "package_path" not in json.dumps(recorded.document)
    assert str(workspace["root"]) not in json.dumps(recorded.document)

    reported = run_cli(["matrix"], workspace)
    assert str(workspace["root"]) not in json.dumps(reported.document)
    assert "package_path" not in json.dumps(reported.document)


# ---------------------------------------------------------------------------
# P1 (2)'s 实现状态 field is derived, not declared
# ---------------------------------------------------------------------------


def test_p1_2_reports_code_complete_while_the_code_surface_is_complete() -> None:
    """The four-field table must not read PARTIAL for code that is written.

    CLAUDE.md 代码阶段与现场阶段分离: what is missing for this bullet is an
    archived IPA we own or are licensed to redistribute, and a guest to run one
    on.  Both are recorded in ``blocker`` and in the onsite field, and neither is
    unfinished code.
    """
    declared, wired = p1._cli_subcommands(REPO_ROOT)  # noqa: SLF001 - the code surface
    assert "matrix" in declared, declared
    assert "matrix" in wired, wired

    check = p1.check_p1_2_compatibility(REPO_ROOT)
    assert check.implementation == "CODE COMPLETE"
    # ...and the field gaps are still reported as field gaps.
    assert check.onsite_status == "BLOCKED"
    assert check.blocker
    assert check.verdict != p1.PASS


def test_p1_2_stops_reporting_code_complete_when_the_cli_surface_regresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove the subcommand and the field must fall back to PARTIAL.

    This is what makes the derived field load-bearing rather than a second
    hardcoded string: a regression in the code surface is visible in the
    four-field table, not only in the ``cli_surface`` observation.
    """
    monkeypatch.setattr(p1, "_cli_subcommands", lambda root: ((), ()))

    check = p1.check_p1_2_compatibility(REPO_ROOT)

    assert check.implementation == "PARTIAL"
    observation = next(o for o in check.observations if o.label == "cli_surface")
    assert observation.ok is False


def test_p1_2_stops_reporting_code_complete_when_the_matrix_module_regresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unimportable matrix is not finished code either."""
    monkeypatch.setattr(p1, "_import", lambda dotted: (None, "probe: unavailable"))

    check = p1.check_p1_2_compatibility(REPO_ROOT)

    assert check.implementation == "PARTIAL"
    assert check.verdict == p1.FAIL


def test_p1_2_keeps_the_field_gaps_reported_as_field_gaps() -> None:
    """The gate is not weakened to buy the derived field.

    ``archived_ipas`` stays ``False`` and ``recorded_observations`` stays a
    finding: neither is a code gap, and neither may be flipped to make the
    four-field table read better.
    """
    check = p1.check_p1_2_compatibility(REPO_ROOT)
    labels = {observation.label: observation for observation in check.observations}

    assert labels["archived_ipas"].ok is False
    assert "recorded observations: 0" in labels["recorded_observations"].detail
    assert "no_archived_device_ipa" in check.verdict_reasons
    assert "installs_and_runs_requires_a_real_ipados_guest" in check.verdict_reasons


def test_the_matrix_module_itself_is_unchanged_by_the_cli_surface() -> None:
    """The writer is a new caller of the module, not a new rule in it.

    The gates below are the ones PRD 5.1 / 10 / 12 put in the module; the CLI
    must not have become a way around any of them.
    """
    assert matrix_module.AUTHORISATIONS == (
        Authorisation.OWN_SOURCE,
        Authorisation.LICENSED_WITH_EVIDENCE,
        Authorisation.NOT_AUTHORISED,
    )
    assert matrix_module.TRUST_CATEGORIES == (
        TrustCategory.UNSIGNED,
        TrustCategory.DEVELOPMENT_SIGNED,
        TrustCategory.OWNER_TRUSTED,
        TrustCategory.GUEST_REPORTED,
    )
    assert "compatible" not in {str(v) for v in matrix_module.VERDICTS}
    assert "works" not in {str(v) for v in matrix_module.VERDICTS}
    assert matrix_module.POSITIVE_VERDICTS == (Verdict.INSTALLS_AND_RUNS, Verdict.INSTALLS_ONLY)
