"""Unit tests for :mod:`unipad.host.logs` (PRD 7.4, 9.2, FR09, 5.2).

Two contracts live here.

**Serial tailing.** PRD 7.4 says serial text is for *locating* a stage, never for
proving one, and that a half-written line is not guest evidence.  So the tailer
must follow appended bytes, buffer partial lines instead of inventing them, and
refuse to pretend a missing log is an empty one.

**The evidence bundle.** PRD 9.2 fixes the minimum set: ``experiment.json``,
``host-inventory.json``, ``artifact-manifest.json``, the actual QEMU arguments,
the serial log, ``verification.json`` (plus guest log, QMP log and timestamped
screenshots when obtainable).  :meth:`EvidenceBundle.build` must refuse to call
an incomplete set complete -- the verifier's "no" is what keeps a green run from
being mistaken for a passing gate.

PRD 9.2 also requires that logs carry no account tokens, signing keys or user
documents, so the tests assert the bundle records *paths and digests*, never
artifact contents, and that the QMP command log carries no arguments.

Nothing here is guest evidence: it proves the host-side collector
(docs/testing-strategy.md D2/D3).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.logs import (
    EvidenceBundle,
    Screenshotter,
    SerialLogTailer,
    read_ppm_header,
    sha256_file,
)
from unipad.models import EvidenceSet
from tests.fakes import fake_qemu as fq


def _ppm(width: int = 8, height: int = 4) -> bytes:
    """A minimal, valid, deterministic binary PPM."""
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return header + bytes((index * 7) % 256 for index in range(width * height * 3))


def _complete_inputs(run_dir: Path) -> dict[str, Path]:
    """Write every required evidence artifact into ``run_dir/inputs``."""
    inputs = run_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    files = {
        "experiment_json": inputs / "experiment.json",
        "host_inventory": inputs / "host-inventory.json",
        "artifact_manifest": inputs / "artifact-manifest.json",
        "serial_log": inputs / "serial.log",
        "screenshot": inputs / "shot.ppm",
    }
    files["experiment_json"].write_text(json.dumps({"experiment_id": "exp-1"}), encoding="utf-8")
    files["host_inventory"].write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    files["artifact_manifest"].write_text(json.dumps({"artifacts": []}), encoding="utf-8")
    files["serial_log"].write_text("M1: UEFI firmware started\n", encoding="utf-8")
    files["screenshot"].write_bytes(_ppm())
    return files


def _fill_required(bundle: EvidenceBundle, files: dict[str, Path]) -> None:
    """Record every item PRD 9.2 requires, so ``build()`` can succeed."""
    bundle.set_experiment_json(files["experiment_json"])
    bundle.set_host_inventory(files["host_inventory"])
    bundle.set_artifact_manifest(files["artifact_manifest"])
    bundle.add_qemu_args(["-accel", "tcg", "-m", "3072"])
    bundle.add_serial_log(files["serial_log"])
    bundle.set_verification({"passed": True, "verifier_id": "unit-simulation"})


# ---------------------------------------------------------------------------
# SerialLogTailer
# ---------------------------------------------------------------------------


def test_tailer_follows_appended_lines(tmp_path):
    """PRD 7.4: appended bytes are delivered as they appear, in order."""
    log = tmp_path / "serial.log"
    log.write_text("first line\n", encoding="utf-8")
    tailer = SerialLogTailer(log, from_end=False)

    assert tailer.read_lines() == ["first line"]

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("second line\n")
    assert tailer.read_lines() == ["second line"]

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("third line\n")
    assert tailer.read_lines() == ["third line"]


def test_tailer_buffers_partial_lines_and_never_invents_them(tmp_path):
    """PRD 7.4: a half-written line is not guest evidence."""
    log = tmp_path / "serial.log"
    log.write_text("complete\n", encoding="utf-8")
    tailer = SerialLogTailer(log, from_end=False)
    assert tailer.read_lines() == ["complete"]

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("half wri")
    assert tailer.read_lines() == []

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("tten\n")
    assert tailer.read_lines() == ["half written"]


def test_tailer_strips_carriage_returns(tmp_path):
    """Windows-host logs must not leak ``\\r`` into the evidence text."""
    log = tmp_path / "serial.log"
    log.write_bytes(b"windows line\r\n")
    tailer = SerialLogTailer(log, from_end=False)

    assert tailer.read_lines() == ["windows line"]


def test_tailer_follow_waits_for_a_log_that_does_not_exist_yet(tmp_path):
    """PRD 7.4: the boot log is the evidence, so ``follow`` waits for it."""
    log = tmp_path / "serial.log"
    tailer = SerialLogTailer(log, from_end=True, poll_interval=0.05)

    def create_later() -> None:
        time.sleep(0.15)
        log.write_text("M1: UEFI firmware started\n", encoding="utf-8")

    writer = threading.Thread(target=create_later, daemon=True)
    writer.start()
    try:
        seen = [line for line in tailer.follow(timeout=3.0)]
    finally:
        writer.join(timeout=3.0)
        tailer.close()

    assert seen == ["M1: UEFI firmware started"]


def test_tailer_follow_yields_lines_as_they_are_appended(tmp_path):
    """PRD 7.4: ``tail -f`` semantics, with a bounded budget and no busy spin."""
    log = tmp_path / "serial.log"
    log.write_text("", encoding="utf-8")
    tailer = SerialLogTailer(log, from_end=False, poll_interval=0.05)

    def append_later() -> None:
        for index in range(3):
            time.sleep(0.08)
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(f"line {index}\n")

    writer = threading.Thread(target=append_later, daemon=True)
    writer.start()
    try:
        seen = [line for line in tailer.follow(timeout=2.0)]
    finally:
        writer.join(timeout=3.0)
        tailer.close()

    assert seen == ["line 0", "line 1", "line 2"]


def test_tailer_from_end_skips_only_what_already_existed(tmp_path):
    """PRD 7.4: constructing a tailer must not drop output produced afterwards."""
    log = tmp_path / "serial.log"
    log.write_text("old line\n", encoding="utf-8")

    tailer = SerialLogTailer(log, from_end=True)  # snapshot taken at construction
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("new line\n")

    assert tailer.read_lines() == ["new line"]


def test_tailer_iter_lines_on_a_missing_log_raises(tmp_path):
    """PRD 9.2: a missing log is not an empty log."""
    tailer = SerialLogTailer(tmp_path / "absent.log", from_end=False)

    with pytest.raises(UniPadError) as excinfo:
        tailer.read_lines()

    assert excinfo.value.code == ErrorCode.ARTIFACT_MISSING


def test_tailer_read_all_ignores_from_end(tmp_path):
    """PRD 9.2: the whole log is available for after-the-fact diagnosis."""
    log = tmp_path / "serial.log"
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert SerialLogTailer(log, from_end=True).read_all() == ["one", "two", "three"]


def test_tailer_restarts_after_truncation(tmp_path):
    """PRD 7.4: a rotated/truncated log is re-read rather than silently skipped."""
    log = tmp_path / "serial.log"
    log.write_text("first line\n", encoding="utf-8")
    tailer = SerialLogTailer(log, from_end=False)
    assert tailer.read_lines() == ["first line"]

    log.write_text("x\n", encoding="utf-8")  # shorter than what we already read
    assert tailer.read_lines() == ["x"]


def test_tailer_context_manager_closes(tmp_path):
    """A stopped tailer must not block a caller forever."""
    log = tmp_path / "serial.log"
    log.write_text("x\n", encoding="utf-8")

    with SerialLogTailer(log, from_end=False) as tailer:
        assert tailer.read_lines() == ["x"]

    assert tailer._closed is True  # noqa: SLF001 - documented lifecycle flag


def test_sha256_file_helper_returns_empty_for_an_unreadable_file(tmp_path):
    """PRD 9.2: a digest is never fabricated for a file that cannot be read."""
    assert sha256_file(tmp_path / "absent.bin") == ""
    target = tmp_path / "x.bin"
    target.write_bytes(b"x")
    assert len(sha256_file(target)) == 64


# ---------------------------------------------------------------------------
# read_ppm_header
# ---------------------------------------------------------------------------


def test_read_ppm_header_parses_a_binary_ppm(tmp_path):
    """PRD 4.2: a screenshot's geometry is read from the file, not assumed."""
    target = tmp_path / "shot.ppm"
    target.write_bytes(_ppm(16, 9))

    header = read_ppm_header(target)

    assert header["width"] == 16
    assert header["height"] == 9
    assert header["maxval"] == 255
    assert header["header_bytes"] == len(b"P6\n16 9\n255\n")


def test_read_ppm_header_rejects_a_non_ppm(tmp_path):
    """PRD 4.2: a file that is not a P6 image is a render failure."""
    target = tmp_path / "shot.ppm"
    target.write_bytes(b"P3\n16 9\n255\nnot binary\n")

    with pytest.raises(UniPadError) as excinfo:
        read_ppm_header(target)

    assert excinfo.value.code == ErrorCode.RENDER_FAILED
    assert "P6" in excinfo.value.message


def test_read_ppm_header_rejects_a_missing_file(tmp_path):
    """PRD 4.2: no file means no screenshot, and that is named."""
    with pytest.raises(UniPadError) as excinfo:
        read_ppm_header(tmp_path / "absent.ppm")

    assert excinfo.value.code == ErrorCode.RENDER_FAILED


# ---------------------------------------------------------------------------
# Screenshotter
# ---------------------------------------------------------------------------


def test_screenshotter_captures_a_verified_ppm_from_qmp(tmp_path):
    """PRD 4.2 / 9.2: a capture is only successful with a real file on disk."""
    with fq.fake_qemu_process() as fake:
        from unipad.host.qmp import QmpClient

        client = QmpClient(port=fake.port, timeout=5.0)
        client.connect()
        try:
            shot = Screenshotter(client, tmp_path).capture("boot")
        finally:
            client.close()

    assert shot["ok"] is True
    assert shot["name"] == "boot.ppm"
    assert shot["bytes"] > 0
    assert shot["width"] == 1112 and shot["height"] == 834
    assert len(shot["sha256"]) == 64
    assert Path(shot["path"]).is_file()


def test_screenshotter_reports_a_dump_with_no_file(tmp_path):
    """PRD 4.2: a QMP ``return`` with no file is a failure, not a screenshot."""

    class _SilentQmp:
        def screendump(self, filename):  # noqa: ANN001, ANN202 - test double
            return {"return": {}, "exists": False, "bytes": None}

    shot = Screenshotter(_SilentQmp(), tmp_path).capture("boot")

    assert shot["ok"] is False
    assert shot["error_code"] == ErrorCode.RENDER_FAILED
    assert "no file" in shot["error_message"]


def test_screenshotter_reports_an_empty_file(tmp_path):
    """PRD 4.2: a zero-byte framebuffer dump proves nothing."""

    class _EmptyQmp:
        def screendump(self, filename):  # noqa: ANN001, ANN202 - test double
            Path(filename).write_bytes(b"")
            return {"return": {}, "exists": True, "bytes": 0}

    shot = Screenshotter(_EmptyQmp(), tmp_path).capture("boot")

    assert shot["ok"] is False
    assert shot["error_code"] == ErrorCode.RENDER_FAILED
    assert "empty" in shot["error_message"]


def test_screenshotter_reports_a_qmp_failure_with_its_code(tmp_path):
    """PRD 9.2: the stable error code reaches the evidence, not prose."""

    class _BrokenQmp:
        def screendump(self, filename):  # noqa: ANN001, ANN202 - test double
            raise UniPadError(ErrorCode.QMP_UNAVAILABLE, "qmp endpoint is not reachable")

    shot = Screenshotter(_BrokenQmp(), tmp_path).capture("boot")

    assert shot["ok"] is False
    assert shot["error_code"] == ErrorCode.QMP_UNAVAILABLE


# ---------------------------------------------------------------------------
# EvidenceBundle: collection
# ---------------------------------------------------------------------------


def test_bundle_collects_every_artifact_and_builds_an_evidence_set(tmp_path):
    """PRD 9.2: the bundle assembles the minimum set and returns it."""
    files = _complete_inputs(tmp_path)
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")
    _fill_required(bundle, files)
    bundle.add_qmp_log('{"QMP": {...}}\n')
    bundle.add_screenshot(files["screenshot"])
    bundle.note("simulation run; no real guest was started")

    evidence = bundle.build()

    assert isinstance(evidence, EvidenceSet)
    assert evidence.run_id == "exp-1"
    assert evidence.check_complete() == []
    assert evidence.qemu_args and Path(evidence.qemu_args).is_file()
    assert evidence.serial_log and Path(evidence.serial_log).is_file()
    assert evidence.verification_json and Path(evidence.verification_json).is_file()
    assert evidence.qmp_log and Path(evidence.qmp_log).is_file()
    assert len(evidence.screenshots) == 1


def test_bundle_copies_the_serial_log_into_the_run_directory(tmp_path):
    """PRD 9.2: the evidence set is self-contained even if archived alone."""
    files = _complete_inputs(tmp_path)
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    recorded = bundle.add_serial_log(files["serial_log"])

    assert recorded == tmp_path / "run" / "serial.log"
    assert recorded.read_text(encoding="utf-8") == files["serial_log"].read_text(encoding="utf-8")


def test_bundle_writes_the_qemu_arguments_one_per_line(tmp_path):
    """PRD 9.2: the actual argv is diffable against a previous run."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")
    args = ["-accel", "tcg", "-cpu", "cortex-a57"]

    target = bundle.add_qemu_args(args)

    assert target.read_text(encoding="utf-8") == "-accel\ntcg\n-cpu\ncortex-a57\n"
    assert bundle.evidence.qemu_args == str(target)


def test_bundle_refuses_to_record_an_empty_argument_list(tmp_path):
    """PRD 9.2: an empty command line is not evidence of a run."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    with pytest.raises(UniPadError) as excinfo:
        bundle.add_qemu_args([])

    assert excinfo.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID


def test_bundle_appends_qmp_log_text(tmp_path):
    """PRD 9.2: the control-channel transcript accumulates across a run."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    bundle.add_qmp_log("greeting\n")
    target = bundle.add_qmp_log("query-status\n")

    assert target.read_text(encoding="utf-8") == "greeting\nquery-status\n"


def test_bundle_records_screenshots_with_their_digests(tmp_path):
    """PRD 9.2: screenshots are timestamped *and* hashed."""
    files = _complete_inputs(tmp_path)
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    target = bundle.add_screenshot(files["screenshot"])

    assert target.parent == tmp_path / "run" / "screenshots"
    assert bundle.evidence.screenshots == [str(target)]
    record = bundle.to_dict()["screenshots"][0]
    assert record["sha256"] == sha256_file(files["screenshot"])
    assert record["bytes"] == files["screenshot"].stat().st_size


def test_bundle_rejects_an_empty_or_missing_screenshot(tmp_path):
    """PRD 9.2: an empty image is a render failure, a missing one is absent."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")
    empty = tmp_path / "empty.ppm"
    empty.write_bytes(b"")

    with pytest.raises(UniPadError) as missing:
        bundle.add_screenshot(tmp_path / "absent.ppm")
    with pytest.raises(UniPadError) as blank:
        bundle.add_screenshot(empty)

    assert missing.value.code == ErrorCode.ARTIFACT_MISSING
    assert blank.value.code == ErrorCode.RENDER_FAILED


@pytest.mark.parametrize(
    "setter", ["set_experiment_json", "set_host_inventory", "set_artifact_manifest"]
)
def test_bundle_rejects_a_missing_required_artifact(tmp_path, setter):
    """PRD 9.2: a required artifact that does not exist cannot be recorded."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    with pytest.raises(UniPadError) as excinfo:
        getattr(bundle, setter)(tmp_path / "absent.json")

    assert excinfo.value.code == ErrorCode.ARTIFACT_MISSING


# ---------------------------------------------------------------------------
# EvidenceBundle: completeness (the verifier's "no")
# ---------------------------------------------------------------------------


def test_check_complete_lists_the_missing_required_items(tmp_path):
    """PRD 9.2: an empty bundle names exactly what is absent."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    assert bundle.missing == list(EvidenceSet.REQUIRED)
    assert bundle.build(strict=False).missing == list(EvidenceSet.REQUIRED)


def test_build_refuses_to_call_an_incomplete_set_complete(tmp_path):
    """PRD 9.2: missing evidence is unverified, and must raise rather than pass."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")
    bundle.add_qemu_args(["-accel", "tcg"])

    with pytest.raises(UniPadError) as excinfo:
        bundle.build()

    error = excinfo.value
    assert error.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert set(error.details["missing"]) == set(EvidenceSet.REQUIRED) - {"qemu_args"}
    assert "experiment_json" in error.details["missing"]


def test_build_still_writes_evidence_json_when_incomplete(tmp_path):
    """PRD 9.2: the reason a run is incomplete is itself recorded."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    with pytest.raises(UniPadError):
        bundle.build(strict=True)

    document = json.loads((tmp_path / "run" / "evidence.json").read_text(encoding="utf-8"))
    assert document["missing"] == list(EvidenceSet.REQUIRED)
    assert document["run_id"] == "exp-1"


def test_build_records_notes_and_screenshot_details(tmp_path):
    """PRD 9.2: provenance notes travel with the evidence set."""
    files = _complete_inputs(tmp_path)
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")
    _fill_required(bundle, files)
    bundle.add_screenshot(files["screenshot"])
    bundle.note("first note")

    bundle.build()
    document = json.loads((tmp_path / "run" / "evidence.json").read_text(encoding="utf-8"))

    assert document["notes"] == ["first note"]
    assert document["screenshots_detail"][0]["name"] == "shot.ppm"
    assert document["run_dir"] == str(tmp_path / "run")


def test_run_id_defaults_to_the_directory_name(tmp_path):
    """PRD 7.3: the run id is what ties the evidence to the experiment."""
    bundle = EvidenceBundle(tmp_path / "runs" / "exp-42")

    assert bundle.run_id == "exp-42"


# ---------------------------------------------------------------------------
# verification.json
# ---------------------------------------------------------------------------


def test_verification_json_is_written_verbatim(tmp_path):
    """PRD 7.4: the bundle never upgrades or rewrites a verdict."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")
    verdict = {"passed": False, "failure_reasons": ["evidence_incomplete"], "verifier_id": "v1"}

    target = bundle.set_verification(verdict)

    assert target == tmp_path / "run" / "verification.json"
    assert json.loads(target.read_text(encoding="utf-8")) == verdict
    assert bundle.evidence.verification_json == str(target)


@pytest.mark.parametrize("bad", [{}, None, "passed", ["passed"]])
def test_set_verification_rejects_a_non_verdict(tmp_path, bad):
    """PRD 7.4: an empty or non-object verdict is not a verdict."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    with pytest.raises(UniPadError) as excinfo:
        bundle.set_verification(bad)  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID


# ---------------------------------------------------------------------------
# nothing secret in the outputs (PRD 9.2)
# ---------------------------------------------------------------------------


def test_evidence_json_records_paths_never_artifact_contents(tmp_path):
    """PRD 9.2: logs must not carry tokens, keys or user documents."""
    secret = "SUPER-SECRET-TOKEN-abc123"
    serial = tmp_path / "serial.log"
    serial.write_text(f"boot ok\npassword={secret}\n", encoding="utf-8")
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    bundle.add_serial_log(serial)
    bundle.build(strict=False)
    document = json.loads((tmp_path / "run" / "evidence.json").read_text(encoding="utf-8"))

    # The path is recorded; the log's contents are not copied into evidence.json.
    assert document["serial_log"] == str(tmp_path / "run" / "serial.log")
    assert secret not in json.dumps(document)
    assert "password=" not in json.dumps(document)


def test_evidence_json_embeds_no_screenshot_pixels(tmp_path):
    """PRD 9.2: only digests are stored, never image bytes."""
    files = _complete_inputs(tmp_path)
    files["screenshot"].write_bytes(_ppm(400, 300))  # ~360 KB of pixels
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    bundle.add_screenshot(files["screenshot"])
    bundle.build(strict=False)
    size = (tmp_path / "run" / "evidence.json").stat().st_size

    assert size < 8192, "evidence.json must not embed screenshot pixels"
    assert "base64" not in (tmp_path / "run" / "evidence.json").read_text(encoding="utf-8")


def test_bundle_does_not_launder_or_invent_a_guest_log(tmp_path):
    """PRD 9.2: an unobtainable guest log stays absent, not fabricated."""
    files = _complete_inputs(tmp_path)
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")
    _fill_required(bundle, files)

    evidence = bundle.build()

    assert evidence.guest_log is None
    assert evidence.missing == []


def test_missing_serial_log_is_not_recorded_as_an_empty_log(tmp_path):
    """PRD 9.2: absent evidence cannot be represented by an empty file."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    with pytest.raises(UniPadError) as excinfo:
        bundle.add_serial_log(tmp_path / "absent.log")

    assert excinfo.value.code == ErrorCode.ARTIFACT_MISSING
    assert not (tmp_path / "run" / "serial.log").exists()


def test_gpu_bridge_log_is_optional_but_must_exist_when_claimed(tmp_path):
    """PRD 9.2: the GPU bridge log is required only when GPU is enabled."""
    bundle = EvidenceBundle(tmp_path / "run", "exp-1")

    with pytest.raises(UniPadError) as excinfo:
        bundle.set_gpu_bridge_log(tmp_path / "absent-bridge.log")

    assert excinfo.value.code == ErrorCode.ARTIFACT_MISSING
