"""A file whose bytes are hashed must be written byte-exactly.

Why this exists
---------------
MEASURED 2026-11-03.  Every case in ``test_acceptance_registry.py`` was red on
this host with

    UniPadError: evidence file changed since it was recorded
    evidence_drift: acceptance-e2e/HelloUniPad.ipa.sha256

and the acceptance registry could not report a verdict at all.  The cause was
one call:

    (evidence_dir / digest_name).write_text(digest + "\\n", encoding="utf-8")

``Path.write_text`` opens in TEXT mode, and on Windows a lone ``"\\n"`` is
translated to ``"\\r\\n"`` on the way out.  The reference hashes the bytes the
caller meant to write -- 65 of them -- while ``load_bundle`` re-hashes what
landed on disk -- 66.  Measured directly: ``a252c771...`` recorded against
``34507f0e...`` on disk.

It never appeared on the macOS runner, which is the only other place this code
runs, so CI could not see it either.  That is what makes it worth a test rather
than a fix: the defect is invisible on one of the two platforms the code runs
on, and the platform it breaks is the one every verdict on this host depends on.

The rule it establishes: **a file whose bytes are hashed is written with
``write_bytes``, or with ``newline=""`` -- never with a text-mode write that
translates.**  Two checks below hold that rule: one behavioural, one on the
source, and the source one is the one with teeth on Linux, where the behaviour
cannot diverge.

What is NOT checked here
------------------------
* that ``guest_runtime.py``'s ``command.txt`` is byte-exact.  It is written the
  same way and its digest is not recorded, so it is harmless today -- and it is
  the next file to break the moment someone starts verifying it.  Recorded, not
  fixed.
* ``registry.py``'s ``host-inventory.json``, same shape, different
  ``EVIDENCE_DIRNAME`` and no recorded digest.  Same note.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from unipad.acceptance import e2e

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src" / "unipad" / "acceptance" / "e2e.py"

#: The sidecar write, as a source pattern.  ``write_bytes`` is the fixed form; a
#: ``write_text`` carrying ``newline=""`` would also be acceptable and is allowed
#: by the second alternative.
_WRITE = re.compile(r"write_(?:text|bytes)\((?:[^()]|\([^()]*\))*\)")


def _sidecar_write() -> str:
    """The statement that writes ``<sample>.sha256``."""
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index("digest_name = f")
    end = text.index("evidence: list[EvidenceRef]", start)
    return text[start:end]


def test_the_sidecar_is_written_byte_exactly() -> None:
    """The source guard, and the only check with teeth on Linux.

    On Linux ``write_text`` does not translate, so the behavioural test below
    passes either way.  This one reads the source and refuses a text-mode write
    of content whose digest is recorded.
    """
    block = _sidecar_write()
    writes = _WRITE.findall(block)
    assert writes, (
        "the sidecar is no longer written with write_text or write_bytes; the "
        "guard's anchor moved and must be re-derived"
    )
    for write in writes:
        assert "write_bytes" in write or 'newline=""' in write, (
            "the sidecar is written with a text-mode write_text, which on "
            f"Windows turns the recorded digest into a lie: {write.strip()}"
        )


def test_the_recorded_digest_matches_the_bytes_on_disk(tmp_path: Path) -> None:
    """The invariant itself, end to end.

    Runs the real ``_observe_one_sample`` against the real archived sample and
    re-hashes what landed on disk.  This is the check that was failing.
    """
    import tempfile

    from unipad.acceptance.e2e import EVIDENCE_DIRNAME, E2EEnvironment

    sample = REPO / "local-private" / "ipa" / "HelloUniPad.ipa"
    if not sample.is_file():
        import pytest

        pytest.skip("the archived HelloUniPad.ipa is not on this host")

    spec = next(
        s for s in e2e.SAMPLE_SPECS if s.name == "HelloUniPad.ipa"
    )
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td) / "run"
        evidence_dir = run_dir / EVIDENCE_DIRNAME
        evidence_dir.mkdir(parents=True)
        env = E2EEnvironment.create(run_dir, repo_root=REPO)
        facts = e2e._observe_one_sample(spec, env, evidence_dir,
                                        "2026-11-03T00:00:00Z")
        # Read inside the block: the directory is torn down at the end of it,
        # and the first version of this test read the sidecar afterwards and
        # failed with FileNotFoundError rather than with the drift it was
        # looking for.
        refs = {ref.path: ref for ref in facts.evidence}
        sidecar = refs.get(f"{EVIDENCE_DIRNAME}/HelloUniPad.ipa.sha256")
        assert sidecar is not None, (
            f"no sidecar reference in {sorted(refs)}"
        )
        on_disk = (run_dir / sidecar.path).read_bytes()
        recorded = sidecar.sha256
        sample_digest = hashlib.sha256(sample.read_bytes()).hexdigest()

    assert hashlib.sha256(on_disk).hexdigest() == recorded, (
        "the sidecar's bytes on disk do not hash to the digest the reference "
        "records, so load_bundle refuses the bundle as drifted"
    )
    # And the sidecar names the digest of the SAMPLE, not of itself.
    assert on_disk.decode("utf-8").strip() == sample_digest


def test_the_guard_has_teeth(tmp_path: Path) -> None:
    """Restore the text-mode write and the source guard must go red.

    A guard that passes on its own defect is worse than no guard.  The mutation
    is applied to a COPY and the guard is re-run against that copy's text, so
    the repository is never mutated.
    """
    text = SOURCE.read_text(encoding="utf-8")
    mutated = text.replace(
        "(evidence_dir / digest_name).write_bytes(sidecar)",
        '(evidence_dir / digest_name).write_text(digest + "\\n", encoding="utf-8")',
        1,
    )
    assert mutated != text, "the mutation did not apply; the anchor moved"

    block = mutated[mutated.index("digest_name = f"):
                    mutated.index("evidence: list[EvidenceRef]",
                                  mutated.index("digest_name = f"))]
    writes = _WRITE.findall(block)
    assert writes, "the mutated source has no sidecar write to check"
    assert not all("write_bytes" in w or 'newline=""' in w for w in writes), (
        "the byte-exactness guard passed on a text-mode write_text; it has no "
        "teeth"
    )
