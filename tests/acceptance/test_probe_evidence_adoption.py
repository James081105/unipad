"""A run's evidence bundle holds repository content, not another agent's scratch.

The measured defect behind this
------------------------------
On 2026-10-24/25 a teammate scratch directory ``.tmp-bt/`` grew to 1.2 GB inside
this repository.  It was untracked and it was not in ``.gitignore`` either, so
nothing claimed it -- and ``_adopt`` claimed it anyway.  Two milestone probes
named a 283-character path from it, ``destination.parent.mkdir`` raised
``FileNotFoundError [WinError 3]`` inside the probe, the runner caught the
exception and turned the check into ``probe_error:M1``, and the run's verdict
went from BLOCKED (correct on a host with no guest) to FAIL.  Acceptance tests
that had nothing to do with the scratch directory went red for every agent on
the host.

The directory has since been moved out to ``F:/unipad-tmp/bt-moved``, so the
incident is not reproducible.  The class is: **any untracked directory with deep
enough paths** reproduces it, which is why these tests build their own in a
throwaway git repository rather than reaching for the one that was moved.

What is pinned, and why each direction matters
----------------------------------------------
* Repository content is still **adopted and digested**.  Adoption is what makes a
  PASS constructible for a p1 check (``record.py:591-597``), so a guard that
  refused more than it had to would be the same defect wearing a different hat:
  a green check reported red.
* A scratch directory is **refused, and the refusal is recorded** -- a stub on
  disk naming the path and the reason, and a reference carrying no digest.  A
  reference that vanished without a reason would hide the fact that a check named
  something the run could not vouch for.
* A refused reference **cannot carry a PASS**.  The stub could be hashed; it is
  deliberately not, because a note explaining why an artefact was not adopted is
  not the artefact.
* The path-length backstop fires **without raising**.  This is the case the
  ownership rule cannot see: a *claimed* path that is still too deep to copy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from unipad.acceptance import probes
from unipad.acceptance.probes import _adopt, _p1_evidence, _repo_content
from unipad.acceptance.record import Verdict, VerdictKind
from unipad.errors import UniPadError

#: The ownership rule is git's answer; without git the answer does not exist and
#: the test has nothing to assert.
_GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(_GIT is None, reason="git is not on PATH")

#: The scratch path used throughout.  Deep on purpose: the original was 195
#: characters of repo-relative path, which is what pushed the *destination* past
#: the platform limit.
SCRATCH = ".tmp-scratch/a/b/c/serial-boot.log"


class _Evidence:
    """A ``p1.Evidence`` stand-in: a path, a kind and a scope, and nothing else."""

    def __init__(self, path: str, kind: str = "probe_log") -> None:
        self.path = path
        self.kind = kind
        self.scope = "simulation"


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=str(root), check=True, capture_output=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real git repository holding one of every kind of path a run names.

    ``src/pkg/module.py`` is tracked, ``artifacts/`` is ignored,
    ``host-inventory.json`` is at the root and untracked, and ``.tmp-scratch/``
    is untracked and unignored.  Those four are the four answers
    :meth:`RepoContent.owns` has to give, and the last one is the class.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("artifacts/\n", encoding="utf-8")
    module = root / "src" / "pkg"
    module.mkdir(parents=True)
    (module / "module.py").write_text("# a module the checks cite\n", encoding="utf-8")
    artifact = root / "artifacts" / "m0"
    artifact.mkdir(parents=True)
    (artifact / "ai-smoke.json").write_text('{"ok": true}\n', encoding="utf-8")
    (root / "host-inventory.json").write_text('{"host": "x"}\n', encoding="utf-8")
    scratch = root / ".tmp-scratch" / "a" / "b" / "c"
    scratch.mkdir(parents=True)
    (scratch / "serial-boot.log").write_text("Shell>\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "src/pkg/module.py")
    return root


@pytest.fixture()
def evidence_dir(tmp_path: Path) -> Path:
    """A run's evidence directory, as the runner would have made it."""
    directory = tmp_path / "run" / "acceptance"
    directory.mkdir(parents=True)
    return directory


def _stubs(directory: Path) -> list[Path]:
    return sorted((directory / "absent").glob("*"))


# ---------------------------------------------------------------------------
# the ownership rule
# ---------------------------------------------------------------------------
def test_repository_content_is_adopted_and_digested(repo, evidence_dir) -> None:
    """The guard's first duty is to stay out of the way.

    Every path here is one a real evaluation names on this host: a tracked
    module, an ignored artefact directory, and a file written at the repository
    root by ``unipad.host.inventory``.  If any of them stops being adopted, a
    check that used to pass stops being able to.
    """
    content = _repo_content(repo)
    for relative in (
        "src/pkg/module.py",
        "artifacts/m0/ai-smoke.json",
        "host-inventory.json",
    ):
        adopted = _adopt(relative, repo, evidence_dir, content)
        assert adopted is not None, relative
        assert adopted.sha256, relative
        assert (evidence_dir / relative).is_file(), relative


def test_an_untracked_and_unignored_directory_is_scratch(repo) -> None:
    """The rule is a property of the repository's own books, not of a name."""
    content = _repo_content(repo)
    assert content.known, "the fixture repository must answer"
    assert not content.owns(SCRATCH)
    assert not content.owns(".tmp-bt/anything/at/all.log")
    # A directory named for the incident is not special-cased: any name behaves
    # the same way, which is the point.
    assert not content.owns("scratch/deep/enough/to/matter.log")
    assert content.owns("src/pkg/module.py")
    assert content.owns("artifacts/m0/ai-smoke.json")
    assert content.owns("host-inventory.json")


def test_a_repository_that_cannot_answer_claims_everything(repo) -> None:
    """Degradation claims rather than refuses, and says which it is doing.

    A gate that cannot tell content from scratch must not start refusing
    content: the only thing a refusal costs is a PASS that can no longer be
    constructed.  ``known`` is the flag that says the rule was not applied, so a
    reader can tell "nothing was scratch" from "nobody looked".
    """
    blind = probes.RepoContent(repo, frozenset(), frozenset(), known=False)
    assert blind.owns(SCRATCH)
    assert not _repo_content(repo / "nowhere").known


# ---------------------------------------------------------------------------
# the refusal, and what it costs
# ---------------------------------------------------------------------------
def test_a_scratch_path_is_refused_and_the_refusal_is_recorded(
    repo, evidence_dir
) -> None:
    """The file is not copied, and the bundle still knows it was named."""
    content = _repo_content(repo)
    refused = _adopt(SCRATCH, repo, evidence_dir, content)

    assert refused is not None, "a refusal must be reported, not dropped"
    assert refused.sha256 is None, "a note recording a refusal is not the artefact"
    reason = refused.digest_reason or ""
    assert "not adopted" in reason, reason
    assert SCRATCH in reason, reason

    # Nothing was copied, so the pollution half of the class is closed too.
    assert not (evidence_dir / SCRATCH).exists()
    assert not (evidence_dir / ".tmp-scratch").exists()

    # ... and the stub that carries the reason is on disk, which is what lets the
    # bundle reload instead of aborting with 'evidence file is missing'.
    stubs = _stubs(evidence_dir)
    assert stubs, "the refusal was not recorded anywhere on disk"
    assert any(SCRATCH in stub.read_text(encoding="utf-8") for stub in stubs)


def test_a_scratch_reference_cannot_carry_a_pass(repo, evidence_dir) -> None:
    """Adoption is what makes a PASS constructible; a refusal is not adoption.

    ``Verdict`` refuses a PASS whose evidence cannot be re-verified
    (``record.py:591-597``).  If the refusal came back digested -- because the
    stub is on disk and could be hashed -- that rule would be satisfied by a note,
    and a check whose only evidence is scratch would be able to pass on nothing.
    """
    content = _repo_content(repo)
    refs = _p1_evidence([_Evidence(SCRATCH)], repo, evidence_dir, content)
    assert len(refs) == 1
    assert refs[0].sha256 is None
    with pytest.raises(UniPadError):
        Verdict.pass_(refs)


def test_repository_evidence_still_carries_a_pass(repo, evidence_dir) -> None:
    """The other half of the same rule, asserted in the same test file."""
    content = _repo_content(repo)
    refs = _p1_evidence(
        [_Evidence("src/pkg/module.py")], repo, evidence_dir, content
    )
    assert refs[0].sha256
    # Does not raise: this is the PASS the guard must not cost.
    Verdict.pass_(refs)


def test_a_refusal_keeps_the_producers_kind(repo, evidence_dir) -> None:
    """``_legal_kind`` still applies, so a refusal is not a new validation hole."""
    content = _repo_content(repo)
    item = _Evidence(SCRATCH, "trace_log")
    refused = _adopt(SCRATCH, repo, evidence_dir, content, item)
    assert refused is not None
    assert refused.kind == "probe_log", refused.kind


# ---------------------------------------------------------------------------
# the length backstop
# ---------------------------------------------------------------------------
def test_a_destination_past_the_platform_limit_is_refused_not_raised(
    repo, evidence_dir, monkeypatch
) -> None:
    """A claimed path that is still too deep to copy is recorded, not crashed on.

    The constant is patched rather than driven to its real value because the
    limit is a property of the host: on Windows this CPython build creates a
    247-character path and fails at 248, so a test that built a 283-character
    source would have to be able to *create* one, which the same limit forbids.
    What is under test is the branch, not the number.
    """
    monkeypatch.setattr(probes, "_MAX_DESTINATION", 40)
    content = _repo_content(repo)
    refused = _adopt("src/pkg/module.py", repo, evidence_dir, content)

    assert refused is not None, "too deep is a refusal, not a crash"
    assert refused.sha256 is None
    reason = refused.digest_reason or ""
    assert "characters" in reason, reason
    assert not (evidence_dir / "src").exists()


def test_a_destination_at_the_platform_limit_is_still_adopted(
    repo, evidence_dir, monkeypatch
) -> None:
    """The backstop has a boundary and it is inclusive, in both directions.

    Patching the constant to the destination's own length is what makes this a
    boundary test rather than a restatement of the guard: at exactly the limit
    the file is adopted, one character under the limit it is refused.  A guard
    written ``>=`` would pass the first half and fail the second.
    """
    content = _repo_content(repo)
    destination = evidence_dir / "src/pkg/module.py"
    limit = len(str(destination))

    monkeypatch.setattr(probes, "_MAX_DESTINATION", limit)
    assert _adopt("src/pkg/module.py", repo, evidence_dir, content) is not None
    assert destination.is_file()

    monkeypatch.setattr(probes, "_MAX_DESTINATION", limit - 1)
    refused = _adopt("artifacts/m0/ai-smoke.json", repo, evidence_dir, content)
    assert refused is not None and refused.sha256 is None
    assert not (evidence_dir / "artifacts").exists()


# ---------------------------------------------------------------------------
# the calls the probes make
# ---------------------------------------------------------------------------
def test_the_milestone_probe_survives_a_scratch_directory(
    repo, evidence_dir, monkeypatch
) -> None:
    """The path that actually went red: ``milestone_probe``'s adoption loop.

    ``M1`` and ``WP02`` named a path under the scratch directory and raised
    inside the probe, which the runner turned into ``probe_error:M1``.  This
    drives the same loop over a verdict whose evidence is all scratch, and
    asserts it returns a verdict instead of raising.
    """
    from unipad.acceptance.milestones import default_view
    from unipad.acceptance.record import EvidenceRef

    content = _repo_content(repo)
    # ``milestones._ref`` always builds its references with a digest, so this is
    # the shape the adapter is really handed -- scratch included.
    scratch_ref = EvidenceRef(
        path=SCRATCH,
        kind="serial_log",
        sha256="0" * 64,
    )
    view = default_view()

    class _Result:
        verdict = Verdict.fail(("blocked_by:fixture",), evidence=(scratch_ref,))

    def evaluate(check_id: str, view: Any = None, snapshot: Any = None) -> Any:
        return _Result()

    monkeypatch.setattr(probes.milestones, "evaluate", evaluate)
    probe = probes.milestone_probe("M1", repo, evidence_dir, view, content)
    verdict = probe()

    assert verdict.kind == VerdictKind.FAIL, verdict
    assert verdict.evidence, "the scratch reference must survive the adapter"
    assert verdict.evidence[0].sha256 is None
    assert "not adopted" in (verdict.evidence[0].digest_reason or "")
    assert not (evidence_dir / SCRATCH).exists()
    assert _stubs(evidence_dir), "the refusal was not recorded on disk"
