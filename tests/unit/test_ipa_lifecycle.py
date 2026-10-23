"""Install lifecycle: uninstall, update, history, clear error classification.

PRD 3.2 P1 (3) asks for 应用卸载、更新、安装历史和清晰的错误分类.  All four are
host-side decisions about *state*, which means they can be implemented and
proved without a guest: what the host knows is what it recorded, and a wrong
answer here is a wrong answer about the user's device, not about our emulation.

The design that follows from that:

* the ledger is append-only.  A history that can be edited is not a history;
* an uninstall and an update are *transitions*, not commands -- each one is a
  new entry that names the entry it supersedes, so "what happened" survives
  "what is true now";
* every entry carries a classified error, and the classification is a closed
  vocabulary.  An error the taxonomy cannot name is reported as unknown
  rather than being flattened into a neighbouring code.
"""

from __future__ import annotations

import json

import pytest

from unipad.errors import ErrorCode
from unipad.ipa.lifecycle import (
    InstallLedger,
    LifecycleAction,
    classify_failure,
)
from unipad.models import InstallResult


def _result(**kwargs) -> InstallResult:
    base = {"package_sha256": "a" * 64, "bundle_id": "com.example.app"}
    base.update(kwargs)
    return InstallResult(**base)


@pytest.fixture
def ledger(tmp_path) -> InstallLedger:
    return InstallLedger(tmp_path / "install-history.jsonl")


# ---------------------------------------------------------------------------
# the ledger is append-only
# ---------------------------------------------------------------------------


def test_a_new_ledger_is_empty_and_has_no_file(ledger):
    assert ledger.entries == []
    assert not ledger.path.exists()


def test_recording_an_install_appends_rather_than_replaces(ledger):
    ledger.record_install(_result())
    ledger.record_install(_result(bundle_id="com.example.other"))

    assert [e.bundle_id for e in ledger.entries] == [
        "com.example.app",
        "com.example.other",
    ]


def test_the_ledger_survives_a_reload(ledger):
    """A history is only a history if it outlives the process that wrote it."""
    ledger.record_install(_result())
    ledger.record_install(
        _result(stage="failed", error_code=ErrorCode.INVALID_PACKAGE)
    )

    reloaded = InstallLedger(ledger.path)
    assert len(reloaded.entries) == 2
    assert reloaded.entries[1].error_code == ErrorCode.INVALID_PACKAGE


def test_a_corrupt_line_does_not_destroy_the_history(ledger):
    """One unreadable line is reported; the rest still loads.

    A truncated write is the realistic failure, and dropping the whole file
    would lose every entry that was successfully recorded.
    """
    ledger.record_install(_result())
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"bundle_id": "truncated"')

    reloaded = InstallLedger(ledger.path)
    assert len(reloaded.entries) == 1
    assert reloaded.unreadable_lines == 1


# ---------------------------------------------------------------------------
# installed state is derived, never stored
# ---------------------------------------------------------------------------


def test_installed_reports_only_what_the_last_entry_says(ledger):
    ledger.record_install(_result(stage="installed"))
    ledger.record_install(_result(stage="installed", package_sha256="b" * 64))

    state = ledger.installed("com.example.app")
    assert state is not None
    assert state.package_sha256 == "b" * 64


def test_an_uninstalled_bundle_is_not_installed(ledger):
    ledger.record_install(_result(stage="installed"))
    ledger.record_uninstall(_result(stage="installed"), reason="user_requested")

    assert ledger.installed("com.example.app") is None
    assert ledger.installed_bundle_ids() == ()


def test_a_failed_install_never_registers_as_installed(ledger):
    """``failed`` is terminal, so it is the last word about that attempt."""
    ledger.record_install(_result(stage="installed"))
    ledger.record_install(
        _result(
            stage="failed",
            error_code=ErrorCode.INVALID_PACKAGE,
            package_sha256="c" * 64,
        )
    )

    state = ledger.installed("com.example.app")
    assert state is not None
    # the *previous* good install still stands; the failure did not evict it
    assert state.package_sha256 == "a" * 64


def test_a_bundle_that_was_never_seen_is_absent_not_erroring(ledger):
    assert ledger.installed("com.nope") is None


# ---------------------------------------------------------------------------
# uninstall and update are transitions
# ---------------------------------------------------------------------------


def test_an_uninstall_records_a_transition_that_names_what_it_superseded(ledger):
    ledger.record_install(_result(stage="installed"))
    ledger.record_uninstall(_result(stage="installed"), reason="user_requested")

    uninstall = ledger.entries[-1]
    assert uninstall.action == LifecycleAction.UNINSTALL
    assert uninstall.reason == "user_requested"
    assert uninstall.supersedes is not None
    assert uninstall.supersedes == ledger.entries[0].entry_id


def test_uninstalling_something_not_installed_is_refused_not_invented(ledger):
    """Removing an app that is not there must not fabricate a removal."""
    ledger.record_install(_result(stage="installed"))
    ledger.record_uninstall(_result(stage="installed"), reason="user_requested")

    with pytest.raises(ValueError, match="not installed"):
        ledger.record_uninstall(_result(stage="installed"), reason="again")


def test_an_update_keeps_both_versions_in_the_history(ledger):
    """An update is not an overwrite: the old version stays visible."""
    ledger.record_install(_result(stage="installed", package_sha256="a" * 64))
    ledger.record_install(
        _result(stage="installed", package_sha256="b" * 64),
        action=LifecycleAction.UPDATE,
    )

    assert len(ledger.entries) == 2
    assert ledger.entries[1].action == LifecycleAction.UPDATE
    assert ledger.entries[1].supersedes == ledger.entries[0].entry_id
    # and the current state is the new one
    assert ledger.installed("com.example.app").package_sha256 == "b" * 64


def test_an_update_without_a_previous_install_is_a_plain_install(ledger):
    ledger.record_install(_result(stage="installed"), action=LifecycleAction.UPDATE)
    # nothing to supersede, so it keeps the action but claims no predecessor
    assert ledger.entries[0].action == LifecycleAction.UPDATE
    assert ledger.entries[0].supersedes is None


# ---------------------------------------------------------------------------
# error classification is a closed vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        (ErrorCode.INVALID_PACKAGE, "package_rejected"),
        (ErrorCode.UNSUPPORTED_PLATFORM, "package_rejected"),
        (ErrorCode.INSTALL_SERVICE_UNAVAILABLE, "service_unavailable"),
        (ErrorCode.IMAGE_HASH_MISMATCH, "integrity_failed"),
        (ErrorCode.TRANSFER_FAILED, "transfer_failed"),
    ],
)
def test_a_known_error_code_maps_to_a_stable_class(code, expected):
    assert classify_failure(code) == expected


def test_an_unknown_error_code_is_reported_as_unknown_not_guessed():
    """Flattening an unrecognised code into a neighbour hides the real fault."""
    assert classify_failure("some_error_we_have_never_seen") == "unknown"
    assert classify_failure(None) == "unknown"


def test_a_classified_failure_never_contains_the_payload(ledger):
    """PRD 9.2: no secrets in any output, including error details."""
    entry = ledger.record_install(
        _result(
            stage="failed",
            error_code=ErrorCode.INVALID_PACKAGE,
            error_message="token=sk-live-abc123 secret material",
        )
    )
    assert "sk-live-abc123" not in json.dumps(entry.to_dict())


# ---------------------------------------------------------------------------
# the history is queryable
# ---------------------------------------------------------------------------


def test_history_returns_entries_for_one_bundle_in_order(ledger):
    ledger.record_install(_result(stage="installed"))
    ledger.record_install(_result(bundle_id="com.example.other", stage="installed"))
    ledger.record_uninstall(_result(stage="installed"), reason="user_requested")

    history = ledger.history("com.example.app")
    assert [e.action for e in history] == [
        LifecycleAction.INSTALL,
        LifecycleAction.UNINSTALL,
    ]


def test_history_for_an_unknown_bundle_is_empty(ledger):
    assert ledger.history("com.nope") == ()


# ---------------------------------------------------------------------------
# the ledger is not editable through its own accessor
# ---------------------------------------------------------------------------


def test_entries_is_a_copy_not_the_live_list(ledger):
    """A history that a caller can rewrite is not a history."""
    ledger.record_install(_result(stage="installed"))

    handed_out = ledger.entries
    handed_out.clear()

    assert len(ledger.entries) == 1
    assert ledger.installed("com.example.app") is not None


def test_a_second_install_cannot_be_reverted_by_mutating_the_first(ledger):
    """Appending through the accessor would forge an entry."""
    ledger.record_install(_result(stage="installed"))
    ledger.entries.append(
        __import__(
            "unipad.ipa.lifecycle", fromlist=["LifecycleEntry"]
        ).LifecycleEntry(
            entry_id="forged",
            bundle_id="com.example.app",
            action=LifecycleAction.UNINSTALL,
            supersedes="le-00000001",
        )
    )

    # the forged entry never reached the file, so a reload does not see it
    reloaded = InstallLedger(ledger.path)
    assert len(reloaded.entries) == 1
    assert reloaded.installed("com.example.app") is not None


def test_the_error_class_is_recorded_not_recomputed_later(ledger):
    """A class is a judgement made at the time, so it is stored.

    Recomputing it on read would let a taxonomy change silently rewrite the
    classification of past failures.
    """
    entry = ledger.record_install(
        _result(stage="failed", error_code=ErrorCode.INVALID_PACKAGE)
    )
    assert entry.error_class == "package_rejected"

    reloaded = InstallLedger(ledger.path)
    assert reloaded.entries[0].error_class == "package_rejected"


def test_the_serialised_entry_carries_no_free_text_error(ledger):
    """PRD 9.2: the on-disk record is what leaks, so it must carry no prose.

    A guest-supplied ``error_message`` is where a token, a path or a user
    document would end up.  The class and the code are what an operator needs;
    the raw text is not, so it is never written.
    """
    ledger.record_install(
        _result(
            stage="failed",
            error_code=ErrorCode.INVALID_PACKAGE,
            error_message="token=sk-live-abc123 C:/Users/private/doc.pdf",
        )
    )

    on_disk = ledger.path.read_text(encoding="utf-8")
    assert "sk-live-abc123" not in on_disk
    assert "private/doc.pdf" not in on_disk
    # ... and the classification still made it, so the entry is not useless
    assert "package_rejected" in on_disk
