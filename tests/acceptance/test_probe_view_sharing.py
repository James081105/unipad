"""One evaluation observes the host once, not once per check.

The measurement behind this
---------------------------
``milestones.HostView`` caches its file index on the instance, and
``milestones._snapshot`` caches its observations on the view too.  Both caches
are per-*instance*, so they only help if the same view is reused.  The probe
adapter in ``probes.py`` called ``milestones.evaluate(check_id)`` with no view,
and ``evaluate`` answers that by building a fresh ``default_view()`` -- an empty
cache -- for every single check.

Measured on this host (Windows 11, CPython 3.12, 2026-10-25) by wrapping
``HostView.files`` and ``_snapshot`` and running one ``AcceptanceRunner.run()``
over the real registry, before the fix:

* **163 s** wall clock, of which **58 s** was filesystem walking;
* **21** snapshots computed (one per milestone check, each rebuilding the view);
* **168** ``HostView.files()`` walks -- **42** walks of
  ``local-private/out/real-boot`` alone, at 1.35 s each, because that root holds
  49,045 files / 3.4 GB of experiment scratch;
* the remaining ~105 s was building and ``fnmatch``-ing a 49,045-entry basename
  index, 42 times over.

After the fix the same run does **4 walks (one per search root) and 1 snapshot**.
Both numbers are reproducible from the wrappers in this file: patch the two
methods, build one run's probes, invoke them all, and count.

None of that work is *wrong* -- it is the same work, forty-two times over.  The
verdict is identical; only the cost is not.  That is why this is a caching test
and not a semantics test: the property being pinned is that one evaluation makes
one observation of the host, and any change that makes it observe more than once
fails here.

Why the counters count misses and not calls
-------------------------------------------
``HostView.files`` and ``_snapshot`` are *called* on every lookup and return the
cached value when they have one, so counting calls counts cache hits too and
reports 21 observations for a run that made one.  Both wrappers below therefore
record whether the cache was cold before delegating, which is what "the host was
observed" actually means.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from unipad.acceptance import milestones, registry


def _milestone_ids(probes: dict) -> list[str]:
    """The check ids whose probe is a milestone probe."""
    return [check_id for check_id in probes if check_id in milestones.CHECKS]


def _run_every_milestone_probe(tmp_path: Path) -> list[str]:
    """Build one run's probes and invoke all of them; return the check ids."""
    inputs = registry.build_inputs(tmp_path / "run")
    milestone_ids = _milestone_ids(inputs.probes)
    assert milestone_ids, "the registry built no milestone probes to share a view"
    for check_id in milestone_ids:
        inputs.probes[check_id]()
    return milestone_ids


def test_one_evaluation_observes_the_host_once(tmp_path: Path) -> None:
    """All of a run's milestone probes share a single host view and snapshot."""
    built: list[milestones.HostView] = []
    observed: list[int] = []
    real_default_view = milestones.default_view
    real_snapshot = milestones._snapshot

    def recording_view() -> milestones.HostView:
        view = real_default_view()
        built.append(view)
        return view

    def counting_snapshot(view: milestones.HostView) -> dict:
        cold = "__snapshot__" not in view._cache
        result = real_snapshot(view)
        if cold:
            observed.append(id(view))
        return result

    original_view, original_snapshot = milestones.default_view, milestones._snapshot
    milestones.default_view = recording_view  # type: ignore[method-assign]
    milestones._snapshot = counting_snapshot  # type: ignore[assignment]
    try:
        milestone_ids = _run_every_milestone_probe(tmp_path)
    finally:
        milestones.default_view = original_view  # type: ignore[method-assign]
        milestones._snapshot = original_snapshot  # type: ignore[assignment]

    assert len(built) == 1, (
        f"{len(built)} host views were built for {len(milestone_ids)} milestone "
        "probes.  A view's file index and snapshot caches live on the instance, "
        "so a view per check re-walks and re-indexes the whole host once per "
        "check -- measured at 42 walks of local-private/out/real-boot and 163 s "
        "for a single evaluation."
    )
    assert len(observed) == 1, (
        f"{len(observed)} host observations for {len(milestone_ids)} milestone "
        "probes.  Each observation walks every search root and indexes every "
        "file it finds, so a per-check view multiplies that by the number of "
        "checks."
    )


@pytest.mark.skipif(sys.platform != "win32", reason="the measured walk is Windows-host specific")
def test_the_shared_view_walks_each_root_once(tmp_path: Path) -> None:
    """The cache is actually used: each search root is walked exactly once.

    ``files()`` caches per root *within* a view, so this is the assertion that
    distinguishes "one view" from "one view that nothing reuses".  A *call* to
    ``files()`` is not a walk -- a cache hit is a call that walks nothing -- so
    only the misses are counted.
    """
    misses: list[str] = []
    real_files = milestones.HostView.files

    def counting(self: milestones.HostView, root: Path) -> tuple[str, ...]:
        cold = str(root) not in self._cache
        result = real_files(self, root)
        if cold:
            misses.append(str(root))
        return result

    original = milestones.HostView.files
    milestones.HostView.files = counting  # type: ignore[method-assign]
    try:
        _run_every_milestone_probe(tmp_path)
    finally:
        milestones.HostView.files = original  # type: ignore[method-assign]

    assert misses, "no root was walked at all; the probes observed nothing"
    counts = {root: misses.count(root) for root in set(misses)}
    repeated = {root: count for root, count in counts.items() if count > 1}
    assert not repeated, (
        f"some search roots were walked more than once: {repeated}.  A repeated "
        "walk means a fresh view was built after the first one had already "
        f"indexed it, i.e. the cache is not being reused ({len(misses)} walks "
        f"for {len(counts)} distinct roots)."
    )
