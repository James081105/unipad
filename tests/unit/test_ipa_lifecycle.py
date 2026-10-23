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
  rather than being flattened into a neighbouring code;
* an entry is not on the disk until its *name* is either: the file descriptor
  is fsynced, and so is the directory that had to learn the new name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from unipad.errors import ErrorCode
from unipad.ipa import lifecycle as lifecycle_module
from unipad.ipa.lifecycle import (
    FAILURE_CLASSES,
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


#: Lines that are valid JSON but are not a complete entry.  Each one is
#: subscripted by :meth:`LifecycleEntry.from_dict` -- ``data["action"]``,
#: ``data["entry_id"]`` and ``data["bundle_id"]`` -- so a missing key raises
#: ``KeyError``, which is not a ``ValueError`` and therefore escaped the only
#: ``except`` clause ``_load`` had.
INCOMPLETE_LINES: tuple[str, ...] = (
    '{"package_sha256": "abc"}',
    '{"action": "install"}',
    '{"entry_id": "le-00000002", "action": "install"}',
    '{"entry_id": "le-00000003", "bundle_id": "com.example.app"}',
)


@pytest.mark.parametrize("line", INCOMPLETE_LINES)
def test_a_valid_json_line_that_is_not_an_entry_is_counted_not_fatal(ledger, line):
    """A structurally-broken line must cost itself, not the whole ledger.

    ``_load`` caught ``(ValueError, TypeError, json.JSONDecodeError)``.
    ``from_dict`` subscripts three keys with no default, so a line that is
    perfectly valid JSON but missing one of them raised ``KeyError`` straight
    out of the ``InstallLedger(path)`` **constructor** -- the ledger could not
    be opened at all, ``unreadable_lines`` never rose, and ``unipad history``
    died on a host fault instead of reporting a damaged file.

    The sibling store already gets this right: ``CompatibilityMatrix._load``
    catches ``KeyError`` alongside the rest for exactly this reason.  This pins
    the two modules to the same rule, so the difference cannot be read as
    deliberate.
    """
    ledger.record_install(_result())
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    reloaded = InstallLedger(ledger.path)
    assert len(reloaded.entries) == 1, "the good entry must still load"
    assert reloaded.unreadable_lines == 1, (
        f"{line} must be counted as unreadable, not raised out of the constructor"
    )


def _physical_lines(path) -> list[str]:
    """Every non-blank physical line, as the file stores it."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_truncated_tail_does_not_swallow_the_next_entry(ledger):
    """A half-written tail must cost *itself*, never the entry that follows.

    ``_append`` opens the file in append mode.  If the previous writer died
    mid-line the file does not end with a newline, so the next entry is
    concatenated straight onto the corrupt line and both are lost: the reload
    sees one unreadable line instead of one unreadable line *plus* a good
    entry, and ``unreadable_lines`` does not rise, so nothing signals that a
    second entry was swallowed.  That is silent data loss in a module whose
    only job is not to lose data.
    """
    ledger.record_install(_result())
    # a crash mid-write: JSON that stops before the closing brace, no newline
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"entry_id": "le-00000002", "bundle_id": "com.example.app", "act')

    ledger.record_install(_result(package_sha256="b" * 64))

    assert len(_physical_lines(ledger.path)) == 3, (
        "the corrupt tail and the new entry must be separate lines"
    )
    reloaded = InstallLedger(ledger.path)
    assert len(reloaded.entries) == 2
    assert reloaded.entries[1].entry_id == "le-00000002"
    assert reloaded.entries[1].package_sha256 == "b" * 64
    assert reloaded.unreadable_lines == 1


def test_a_complete_line_missing_only_its_newline_is_still_terminated(ledger):
    """The other half of the same crash: the bytes arrived, the newline did not.

    A reader can still parse that line, but the *next* append cannot, so the
    tail has to be closed off before anything is written after it.
    """
    ledger.record_install(_result())
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(ledger.entries[0].to_dict(), ensure_ascii=False))

    ledger.record_install(_result(package_sha256="c" * 64))

    reloaded = InstallLedger(ledger.path)
    assert len(reloaded.entries) == 3
    assert reloaded.unreadable_lines == 0
    assert [e.package_sha256 for e in reloaded.entries] == ["a" * 64, "a" * 64, "c" * 64]


def test_an_unterminated_tail_is_closed_without_rewriting_the_file(ledger):
    """The fix must be additive: an append-only file is never rewritten.

    Repairing the tail by truncating it, or by rewriting the line, would make
    the ledger editable after all -- the exact property this module exists to
    hold.  So the repair may only add bytes, and every byte that was already
    there must still be there, in order.
    """
    ledger.record_install(_result())
    before = ledger.path.read_bytes()

    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"entry_id": "le-00000002", "bund')
    torn = ledger.path.read_bytes()

    ledger.record_install(_result(package_sha256="d" * 64))

    after = ledger.path.read_bytes()
    assert torn in after, "the torn tail must survive byte for byte"
    assert after.startswith(before)
    assert len(after) > len(torn), "only bytes may be added"


def test_an_appended_entry_reaches_the_device_before_the_call_returns(
    ledger, monkeypatch
):
    """Durability: the entry is on the disk when ``record_install`` returns.

    The ledger is the only record that a transition happened.  If the append
    is left in the process's page cache, a crash or a power cut immediately
    after a *successful* install loses the entry: ``unipad history`` answers
    "nothing", and the later ``update`` / ``uninstall`` refuse with
    ``not_installed`` -- the host now disagrees with the guest about what is
    installed, with nothing to reconcile them.
    """
    observed: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        # Whatever the process has handed to the kernel must already be there.
        # A size smaller than the file on disk means flush() did not run
        # before fsync(), so the bytes were never really pushed.
        observed.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)

    ledger.record_install(_result())
    assert len(observed) == 1, "every append must be forced to the device"
    assert observed[0] == ledger.path.stat().st_size, "flush must precede fsync"

    ledger.record_install(_result(package_sha256="b" * 64))
    assert len(observed) == 2
    assert observed[1] == ledger.path.stat().st_size
    assert observed[1] > observed[0], "the second entry grew the file on disk"


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


@pytest.mark.parametrize("stage", ["transferred", "checked", "failed", ""])
def test_a_stage_that_did_not_install_never_registers_the_bundle(ledger, stage):
    """``installed_bundle_ids`` must be gated on stage, exactly like ``installed``.

    The per-bundle gate is pinned by
    ``test_a_failed_install_never_registers_as_installed``; this is its twin
    for the set-wide answer.  Without it a caller reading the set -- the CLI's
    ``history --all`` and any operator script -- would be told a bundle is
    installed because a transfer reached the guest.
    """
    ledger.record_install(_result(stage=stage))
    ledger.record_install(_result(bundle_id="com.example.other", stage="installed"))

    assert ledger.installed_bundle_ids() == ("com.example.other",)


def test_a_plain_reinstall_never_claims_a_predecessor(ledger):
    """A second ``install`` is not an ``update``, so it supersedes nothing.

    Recording the action as ``install`` says "this is a first/repeat install",
    and ``supersedes`` is reserved for the transition that *replaced* a
    predecessor.  Letting a plain install compute one would make every
    re-install look like a replacement, and a reader could not tell a fresh
    install from an update without diffing hashes -- which is the whole reason
    :class:`LifecycleAction` exists.
    """
    ledger.record_install(_result(stage="installed"))
    ledger.record_install(_result(stage="installed", package_sha256="b" * 64))

    assert ledger.entries[1].action == LifecycleAction.INSTALL
    assert ledger.entries[1].supersedes is None
    # ... and the state is still the last install, unchanged by that
    assert ledger.installed("com.example.app").package_sha256 == "b" * 64


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


@pytest.mark.parametrize("dead_stage", ["failed", "transferred", "checked"])
def test_an_update_never_supersedes_an_entry_that_never_installed_anything(
    ledger, dead_stage
):
    """``supersedes`` must name the entry that *holds* the bundle installed.

    A failed install is not an install: :meth:`installed` says so explicitly,
    and it does not evict the previous good one either.  But ``record_install``
    picked its predecessor from the *last* entry for the bundle, so an update
    landing after a failure named that failure as what it replaced -- while
    the good install it actually replaced was skipped.  The two halves of the
    module then disagree about which entry is current, and the supersede chain
    reads install <- failed <- update, which never happened.
    """
    ledger.record_install(_result(stage="installed", package_sha256="a" * 64))
    ledger.record_install(
        _result(stage=dead_stage, error_code=ErrorCode.TRANSFER_FAILED)
    )
    # the good install is still the one that holds the bundle
    assert ledger.installed("com.example.app").entry_id == "le-00000001"

    ledger.record_install(
        _result(stage="installed", package_sha256="b" * 64),
        action=LifecycleAction.UPDATE,
    )

    update = ledger.entries[-1]
    assert update.supersedes == "le-00000001", (
        "the update must supersede the installed entry, not the failure"
    )
    assert update.supersedes != "le-00000002"
    # and the chain is the one that actually happened
    assert [e.entry_id for e in ledger.entries] == [
        "le-00000001",
        "le-00000002",
        "le-00000003",
    ]


def test_a_successful_install_is_the_thing_an_update_supersedes(ledger):
    """The positive twin: after a *successful* install the predecessor is it.

    Without this the test above could be satisfied by never naming a
    predecessor at all, which would break updates entirely.
    """
    ledger.record_install(_result(stage="installed"))
    ledger.record_install(_result(stage="installed"), action=LifecycleAction.UPDATE)

    assert ledger.entries[1].supersedes == "le-00000001"


def test_an_update_after_an_uninstall_claims_no_predecessor(ledger):
    """Nothing is installed, so there is nothing to replace.

    The removal is itself a transition, not the entry an update replaces;
    naming it would make the chain read install <- uninstall <- update.
    """
    ledger.record_install(_result(stage="installed"))
    ledger.record_uninstall(_result(stage="installed"), reason="user_requested")
    ledger.record_install(_result(stage="installed"), action=LifecycleAction.UPDATE)

    assert ledger.entries[-1].supersedes is None


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


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
        ErrorCode.REGISTRATION_FAILED,
        ErrorCode.MISSING_DEPENDENCY,
        ErrorCode.DYLD_FAILED,
    ],
)
def test_a_guest_that_answered_no_is_classified_as_a_refusal(code):
    """The guest's own refusals must not land in ``unknown``.

    ``installer._map_guest_error`` turns the guest's ``signature_rejected`` /
    ``policy_rejected`` / ``untrusted`` / ``rejected`` / ``denied`` /
    ``forbidden`` -- i.e. the most common reason an install does not happen --
    onto ``signature_or_policy_rejected``, and the container, dependency and
    launch failures onto the other three.  All four are codes the guest
    *produces*, so an operator seeing ``unknown`` is told "we don't know" about
    the one failure they can actually act on.  PRD 3.2 P1 (3) asks for 清晰的错误分类.
    """
    assert classify_failure(code) == "guest_refused"


def test_every_class_the_table_returns_is_in_the_closed_vocabulary():
    """The vocabulary is closed, so the table may not invent a class."""
    from unipad.ipa.lifecycle import _CLASS_BY_CODE

    assert set(_CLASS_BY_CODE.values()) <= set(FAILURE_CLASSES)


def test_a_guest_refusal_class_travels_with_the_ledger_entry(ledger):
    """The class is recorded at the time, so it must be the classified one."""
    entry = ledger.record_install(
        _result(
            stage="failed",
            error_code=ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
        )
    )
    assert entry.error_class == "guest_refused"


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


# ---------------------------------------------------------------------------
# the entry is durable, and so is the name that finds it
# ---------------------------------------------------------------------------


def test_a_new_ledger_file_has_its_directory_entry_forced_to_the_device(
    ledger, monkeypatch
):
    """``fsync`` on the file is only half of "the entry is on the disk".

    Creating a file is two operations: the bytes, and the *name* that maps to
    them in the parent directory.  ``fsync`` on the file descriptor flushes the
    first; on a filesystem that does not journal metadata the second can still
    be sitting in a cache, and a power cut then leaves the bytes on the device
    with nothing pointing at them -- the ledger is gone and ``ls`` says so.
    """
    forced: list[Path] = []
    monkeypatch.setattr(
        lifecycle_module,
        "_fsync_directory",
        lambda directory: forced.append(Path(directory)) or True,
    )

    ledger.record_install(_result())

    assert forced == [ledger.path.parent], (
        "the directory that holds a brand-new ledger must be flushed too"
    )


def test_every_directory_the_append_creates_is_forced_to_the_device(
    tmp_path, monkeypatch
):
    """``mkdir(parents=True)`` creates a chain, and each link is a new name.

    Flushing only the leaf would leave the outer directories' entries -- and
    therefore the path to the ledger -- exactly as durable as the page cache.
    """
    forced: list[Path] = []
    monkeypatch.setattr(
        lifecycle_module,
        "_fsync_directory",
        lambda directory: forced.append(Path(directory)) or True,
    )
    deep = tmp_path / "runs" / "lifecycle" / "install-ledger.jsonl"

    InstallLedger(deep).record_install(_result())

    assert forced == [tmp_path / "runs", tmp_path / "runs" / "lifecycle"]


def test_an_append_to_a_file_that_already_exists_does_not_flush_the_directory(
    ledger, monkeypatch
):
    """The name is durable once; paying a directory flush per entry is waste.

    A directory ``fsync`` is a metadata operation on a hot path -- this module
    appends on every install, update and uninstall -- so it belongs exactly
    where a new name appears and nowhere else.
    """
    ledger.record_install(_result())
    forced: list[Path] = []
    monkeypatch.setattr(
        lifecycle_module,
        "_fsync_directory",
        lambda directory: forced.append(Path(directory)) or True,
    )

    ledger.record_install(_result(package_sha256="b" * 64))

    assert forced == []


def test_the_directory_flush_is_a_real_operation_on_this_host(tmp_path):
    """The helper must actually work, not merely be called.

    A durability guarantee that silently degrades to a no-op is worse than
    none: it is a claim in a docstring that the code does not keep.  So the
    helper is asserted to succeed on the platform running the suite -- POSIX
    ``fsync`` on a directory fd, or the Win32 ``FlushFileBuffers`` route.
    """
    assert lifecycle_module._fsync_directory(tmp_path) is True


def test_a_directory_that_cannot_be_flushed_does_not_fail_the_append(
    ledger, monkeypatch
):
    """A ledger on a share that refuses a metadata flush must still record.

    The append itself succeeded and the entry is in the page cache; turning a
    best-effort durability *improvement* into a hard failure would lose a
    transition the host really performed, which is the one thing this module
    must never do.
    """
    def refuse(directory: Path) -> bool:
        raise OSError("the host refused a metadata flush")

    monkeypatch.setattr(lifecycle_module, "_fsync_directory", refuse)

    entry = ledger.record_install(_result())

    assert entry.entry_id == "le-00000001"
    assert InstallLedger(ledger.path).entries[0].entry_id == "le-00000001"


# ---------------------------------------------------------------------------
# the table covers the fault vocabulary PRD section 10 declares
# ---------------------------------------------------------------------------

#: Every fault classification PRD section 10's install-chain table names, in
#: the table's own row order.  These are codes *this project* declared, so an
#: operator seeing ``unknown`` for one of them is being told "we cannot
#: classify this" about something the taxonomy already has a name for.
PRD_10_CODES: tuple[str, ...] = (
    "unsupported_platform",
    "unsupported_arch",
    "invalid_package",
    "transfer_failed",
    "signature_or_policy_rejected",
    "install_service_unavailable",
    "registration_failed",
    "missing_dependency",
    "dyld_failed",
    "render_failed",
    "input_failed",
    "persistence_failed",
)


@pytest.mark.parametrize("code", PRD_10_CODES)
def test_every_code_prd_section_10_names_has_a_class(code):
    assert classify_failure(code) != "unknown", (
        f"{code} is a declared install-chain fault and must be classifiable"
    )


def test_the_post_install_stages_are_the_guest_running_the_app():
    """图形与输入 and 持久化: the guest ran the app and the app misbehaved.

    The install chain is not at fault and the guest did not refuse, so neither
    ``package_rejected`` nor ``guest_refused`` is true.  Leaving them in
    ``unknown`` would lose the one thing an operator can act on -- the install
    worked, the app does not -- behind "we don't know".
    """
    for code in (
        ErrorCode.RENDER_FAILED,
        ErrorCode.INPUT_FAILED,
        ErrorCode.PERSISTENCE_FAILED,
    ):
        assert classify_failure(code) == "guest_runtime_failed"


def test_blocked_is_refused_a_class_on_purpose_and_the_refusal_is_recorded():
    """``blocked`` is not a fault, so it is not given a fault class.

    errors.py defines it as "generic blocked marker: always carries a concrete
    reason in details".  There is no fault to place -- the host stopped before
    any stage -- so picking one of the five classes would be exactly the
    plausible-looking guess this module refuses to make.  The refusal is named
    rather than left as an accident of the default.
    """
    from unipad.ipa.lifecycle import UNCLASSIFIED_BY_DESIGN

    assert classify_failure(ErrorCode.BLOCKED) == "unknown"
    assert ErrorCode.BLOCKED in UNCLASSIFIED_BY_DESIGN
    assert UNCLASSIFIED_BY_DESIGN[ErrorCode.BLOCKED]


def test_every_key_the_table_maps_is_a_declared_error_code():
    """A row for a code nobody declared is a row nobody can reach.

    It also reads as coverage that does not exist: the table looks like it
    handles a transport fault that no producer ever emits.  ``unipad.errors``
    is the closed set of codes (PRD 9.1/10), so the table may not invent one.
    """
    from unipad.ipa.lifecycle import _CLASS_BY_CODE

    declared = {
        value
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    assert set(_CLASS_BY_CODE) <= declared


# ---------------------------------------------------------------------------
# answering two questions about one bundle costs one scan
# ---------------------------------------------------------------------------


def test_a_caller_holding_the_history_is_not_charged_for_a_second_scan(
    ledger, monkeypatch
):
    """``installed`` reduces over ``history``, so asking both costs two scans.

    ``cmd_history`` wants the bundle's entries *and* its current state, which
    is the same question twice at two different depths: at 100 000 entries the
    second scan is another pass over every line to answer something the first
    pass already knew.  Handing the tuple back in makes the second answer free.
    """
    ledger.record_install(_result(stage="installed"))
    held = ledger.history("com.example.app")

    scans: list[str] = []
    real_history = InstallLedger.history

    def counting(self: InstallLedger, bundle_id: str):
        scans.append(bundle_id)
        return real_history(self, bundle_id)

    monkeypatch.setattr(InstallLedger, "history", counting)

    current = ledger.installed("com.example.app", from_history=held)

    assert scans == [], "the caller's own history must not be re-read"
    assert current is not None
    assert current.entry_id == "le-00000001"


def test_the_answer_from_a_held_history_is_the_same_answer(ledger):
    """A shortcut that could disagree with the direct call is a bug, not a win.

    The reduction is the whole definition of ``installed``, so the two routes
    must land on the same entry for every shape of history -- including the
    ones where the answer is ``None``.
    """
    ledger.record_install(_result(stage="installed", package_sha256="a" * 64))
    ledger.record_install(_result(stage="transferred", package_sha256="b" * 64))
    ledger.record_install(
        _result(stage="installed", package_sha256="c" * 64),
        action=LifecycleAction.UPDATE,
    )
    ledger.record_uninstall(_result(stage="installed"), reason="user_requested")
    ledger.record_install(
        _result(stage="installed", package_sha256="d" * 64),
        action=LifecycleAction.UPDATE,
    )

    for bundle_id in ("com.example.app", "com.example.other"):
        direct = ledger.installed(bundle_id)
        held = ledger.installed(bundle_id, from_history=ledger.history(bundle_id))
        assert held == direct
    # and the chain really did move, so the comparison above is not vacuous
    assert ledger.installed("com.example.app").package_sha256 == "d" * 64
    assert ledger.installed("com.example.other") is None


def test_the_direct_route_still_filters_by_bundle(ledger):
    """The shortcut is opt-in: the default behaviour must not move.

    ``from_history`` is a promise about what the tuple contains -- it has to be
    *this* bundle's history, because ``installed`` reduces over whatever it is
    handed.  A caller that cannot keep that promise has the direct route, and
    that route still has to answer for one bundle and not for the file.
    """
    ledger.record_install(_result(bundle_id="com.example.other", stage="installed"))

    assert ledger.installed("com.example.app") is None
    assert ledger.history("com.example.app") == ()
    assert ledger.installed("com.example.other") is not None


# ---------------------------------------------------------------------------
# growth is visible before it is slow
# ---------------------------------------------------------------------------


def test_the_documented_bound_is_the_measured_one():
    """The number in the docstring is a measurement, so it is pinned here.

    ``InstallLedger`` re-parses the whole file on every open -- the honest cost
    of an append-only history that has to answer ``installed()`` in general.
    Measured on this class: ~5.4 us and ~610 bytes per entry, i.e. ~1 s and
    ~80 MB at 100 000 entries.  A bound nobody can check is a bound nobody
    keeps, so the threshold is asserted rather than trusted.
    """
    assert lifecycle_module.LINE_WARN_THRESHOLD == 100_000


def test_a_ledger_that_has_not_reached_the_bound_says_nothing(ledger, monkeypatch):
    """A warning that fires all the time is a warning nobody reads."""
    monkeypatch.setattr(lifecycle_module, "LINE_WARN_THRESHOLD", 2)
    ledger.record_install(_result())
    ledger.record_install(_result(package_sha256="b" * 64))

    assert ledger.growth_warning is None


def test_a_ledger_past_the_documented_bound_warns(ledger, monkeypatch):
    """Growth has to be visible *before* it is a five-second command.

    The cost is linear and there is no index, so the only honest defence is to
    say so while the file is still small enough to do something about it.  The
    warning names the count and the measured cost, and carries no path: it is
    data an operator acts on, not a location (PRD 9.2).
    """
    monkeypatch.setattr(lifecycle_module, "LINE_WARN_THRESHOLD", 2)
    ledger.record_install(_result())
    ledger.record_install(_result(package_sha256="b" * 64))
    ledger.record_install(_result(package_sha256="c" * 64))

    warning = ledger.growth_warning

    assert warning is not None
    assert "3" in warning, "the warning must name how many entries there are"
    assert "100_000" not in warning
    assert str(ledger.path) not in warning
    assert ledger.line_count == 3


def test_the_warning_is_a_property_of_the_loaded_file(ledger, monkeypatch):
    """``history`` is the command that pays for the parse, so it is the reader."""
    monkeypatch.setattr(lifecycle_module, "LINE_WARN_THRESHOLD", 1)
    ledger.record_install(_result())
    ledger.record_install(_result(package_sha256="b" * 64))

    assert InstallLedger(ledger.path).growth_warning is not None
