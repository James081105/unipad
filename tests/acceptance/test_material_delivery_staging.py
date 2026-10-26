"""``build-macos.sh``'s material delivery staging, pinned against a real run.

Why this file exists
--------------------
``step_material_manifest`` stages a *flat* delivery and then verifies it, and the
staging half used to find each file by searching for its leaf name across six
roots:

```bash
for path in "$(tree_dir)/$leaf" "$(work_dir)/$leaf" \
            "$DEVICE_DIR/.cache/firmware/owner-data/$leaf" \
            "$DEVICE_DIR/.cache/firmware/target-volumes/$leaf" \
            "$OUT_DIR/payload-esp/EFI/UniXNU/$leaf" "$kernelcache"; do
    [[ -f "$path" ]] || continue
    cp "$path" "$delivery/$leaf"
    break
done
```

That loop cannot find three of the eight roles that carry bytes, because none of
the six candidates is a directory those files sit in.  Measured on run
40369137221, whose own boot inputs list names all three real paths:

* the kernel is at ``<work>/kernel/kernel.macho``, so the candidate
  ``<work>/kernel.macho`` is a path no build writes;
* the device tree is at ``<esp>/EFI/UniXNU/Payloads/kernel.macho.adt``, so the
  candidate ``<esp>/EFI/UniXNU/kernel.macho.adt`` is missing the ``Payloads/``
  segment -- ``unixnu/scripts/unixnu/payload.py:149`` writes
  ``payloads / 'kernel.macho.adt'``;
* the trust bundle is at
  ``<tree>/build/original-static-system-trust/original-static-system.bundle``
  (``build-original-system-trust.py:54-56``), so the candidate
  ``<tree>/original-static-system.bundle`` is missing two segments.

The last candidate is the part that makes this more than a missing file.  It is
``$kernelcache`` **unconditionally**, so a role the loop could not find was
handed the donor Apple kernelcache under its own name -- and the verify that
follows then refused it as ``artifact-hash-mismatch``, which reads as "the file
changed" rather than "the file was never copied".  A wrong file under a right
name is the failure ``RULE_LEAF_COLLISION`` was written to prevent in the
manifest, reappearing one stage later in the shell.

What this file pins
-------------------
The three paths above, spelled the way the script spells them; the
``kernelcache`` arm being selected by role rather than by being last in a list;
the payload's device tree taking precedence over the devicetree stage's, in the
same order ``BuildTree.devicetree_candidates`` uses; and an unresolvable source
being a *refusal* rather than a skip, because a delivery missing a file the
build claims to have produced must fail with the role named.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"

pytestmark = pytest.mark.acceptance


def _script() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text, f"{SCRIPT} is empty"
    return text


def _staging_block(text: str) -> str:
    """The delivery-staging half of ``step_material_manifest``.

    Bounded by the ``case`` that names each role's source and the verify that
    follows it, so an assertion here cannot be satisfied by a comment elsewhere
    in a 4,000-line script.
    """
    start = text.find('case "$role" in')
    assert start != -1, (
        "step_material_manifest no longer names each role's source with a case; "
        "if the staging half was rewritten, this file has to be rewritten with "
        "it rather than left asserting the shape of code that is gone"
    )
    end = text.index("--verify", start)
    block = text[start:end]
    assert "kernel.macho" in block, block
    return block


def test_the_delivery_stages_each_role_from_the_path_the_build_wrote(tmp_path: Path) -> None:
    """The three roles the leaf search could not find are now named outright.

    Two-sided.  Each path must be present, so the staging half can copy it; and
    the six-candidate search that missed them must be gone, so the assertion
    cannot pass against a loop that happens to also contain these strings.
    """
    text = _script()
    block = _staging_block(text)

    # (1) present, spelled the way the run's own log spells them.
    for wanted in (
        "$(work_dir)/kernel/kernel.macho",
        "$(tree_dir)/build/original-static-system-trust/original-static-system.bundle",
        "$OUT_DIR/payload-esp/EFI/UniXNU/unixnu.toml",
    ):
        assert wanted in block, f"the staging half does not name {wanted}"
    # The device tree's ``Payloads/`` segment is the one the old candidate
    # lacked.  It is not spelled in this block -- the block names the variable
    # ``step_build_payload`` set -- so the spelling is pinned where it is set,
    # and the arm that consumes it is pinned by the next test.
    assert 'PAYLOAD_ADT_PATH="$esp/EFI/UniXNU/Payloads/kernel.macho.adt"' in text, (
        "step_build_payload does not set PAYLOAD_ADT_PATH to the ESP's Payloads/"
    )

    # (2) the search is gone.  Each of its six candidates is a path no build
    # writes for the role it was meant to serve, and the last one is the donor
    # kernelcache acting as a catch-all.
    for gone in (
        '"$(tree_dir)/$leaf"',
        '"$(work_dir)/$leaf"',
        '"$DEVICE_DIR/.cache/firmware/owner-data/$leaf"',
        '"$DEVICE_DIR/.cache/firmware/target-volumes/$leaf"',
        '"$OUT_DIR/payload-esp/EFI/UniXNU/$leaf"',
    ):
        assert gone not in text, f"the leaf-name search still carries {gone}"
    assert not re.search(r'for path in [^;]*"\$kernelcache"', text), (
        "the donor kernelcache is still a catch-all candidate, so a role the "
        "loop cannot find is handed it under its own name"
    )


def test_the_kernelcache_is_selected_by_role_not_by_being_last(tmp_path: Path) -> None:
    """``kernelcache)`` is an arm of the ``case``, not the end of a list.

    A role-based arm means adding a role cannot silently inherit the previous
    role's file, which is what a positional list does.
    """
    block = _staging_block(_script())
    assert re.search(r"^\s*kernelcache\)\s*$", block, re.MULTILINE), block
    assert 'path="$kernelcache"' in block, block
    # Every role that carries bytes has an arm, so an unmapped role is refused
    # by the ``*)`` arm rather than falling through to whatever is last.
    for role in ("kernel", "devicetree", "trust_cache", "data_seed",
                 "boot_config", "kernelcache"):
        assert re.search(rf"^\s*{role}\)\s*$", block, re.MULTILINE), (
            f"role '{role}' has no arm in the staging case"
        )


def test_the_payloads_device_tree_wins_over_the_devicetree_stages(tmp_path: Path) -> None:
    """The tree the guest boots is staged, and the other one is the fallback.

    ``BuildTree.devicetree_candidates`` resolves in this order and the shell has
    to agree with it, or the delivery carries a tree the module did not
    describe.  ``PAYLOAD_ADT_PATH`` is set by ``step_build_payload`` and is
    empty on a pass that did not build one, hence the default expansion.
    """
    block = _staging_block(_script())
    arm = block[block.index("devicetree)"):block.index("trust_cache)")]
    assert "${PAYLOAD_ADT_PATH:-}" in arm, arm
    assert "$(work_dir)/kernel/kernel.macho.adt" in arm, arm
    # The order is the payload's first: the fallback is what runs when the
    # payload's is empty, not the other way round.
    assert arm.index("${PAYLOAD_ADT_PATH:-}") < arm.index(
        "$(work_dir)/kernel/kernel.macho.adt"
    ), arm


def test_a_role_with_no_source_is_refused_rather_than_skipped(tmp_path: Path) -> None:
    """A delivery missing a file the build claims to have produced is a failure.

    The old loop ``break``-ed out of a search that matched nothing and moved on,
    so the omission surfaced one step later as a hash mismatch on a file that
    was never there.  Refusing here names the role instead.
    """
    block = _staging_block(_script())
    assert 'path=\'\'' in block or 'path=""' in block, (
        "the catch-all arm does not clear the path, so an unknown role keeps "
        "whatever the previous iteration set"
    )
    assert "cannot be staged into the delivery" in block, block
    assert "is not on disk" in block, block
    # Both are refusals, not ledger notes: the stage returns non-zero.
    assert block.count("return 1") >= 2, block
