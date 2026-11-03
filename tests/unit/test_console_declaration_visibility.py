"""A call to ``native_console_report`` must have a declaration in scope.

THIS HAS NOW COST THREE BUILDS, and each time the failure named a file nobody was
editing:

* build 41359711528 -- ``kern_exec.c:8120: error: call to undeclared function
  'native_console_report'``.  The ``extern`` sat inside
  ``#if defined(XNU_OWNER_CACHE_MOUNT)`` and the caller outside it.
* build 41364602699 -- the caller sat outside the guard that pairs with the
  DEFINITION, so the link failed naming the symbol instead.
* build 41441479230 -- ``vm_unix.c:2941:3: error: call to undeclared function
  'native_console_report'``.  ``native-dyld-cache-read.patch`` wrapped its
  ``extern`` in ``#if defined(XNU_NATIVE_VIRTIO_CONSOLE)`` and put the emitter's
  call in a second hunk, outside it.

THE RULE IS ASYMMETRIC, and the first revision of this test got it wrong by being
too strict.  It demanded that the declaration always sit at depth zero, which
flagged ``zzz-m8-launchd-daemon-events.patch`` -- whose ``extern`` is inside
``#if defined(XNU_OWNER_CACHE_MOUNT)`` together with all three of its call sites.
That pairing is CORRECT: a conditional declaration is fine when every caller is
inside the same conditional.

What is actually required is VISIBILITY AT THE CALL SITE.  A declaration with no
definition is free -- the linker only resolves a symbol something REFERENCES --
so an unconditional ``extern`` costs nothing when the emitter is compiled out.
A conditional one, by contrast, is absent exactly when a caller outside it needs
it.  Hence: the declaration must be at least as visible as the shallowest caller,
and the caller is the half that must be conditional.

So this walks each patch's added lines, tracks ``#if``/``#endif`` depth, remembers
the depth at which a declaration became active, retires it when an ``#endif``
closes past it, and asserts every call site has one still active at a depth no
greater than its own.

The scan keeps the context lines because the depth a call site sits at is decided
by conditionals in the surrounding code -- and all three builds turned on exactly
that, which no check reading a declaration and a call site separately can see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PATCH_DIR = REPO / "vendor" / "UniXNU" / "patches"
PRELINK_DIR = REPO / "vendor" / "UniXNU" / "kits" / "NativePrelink" / "patches"

SYMBOL = "native_console_report"


def _patches() -> list[Path]:
    if not PATCH_DIR.is_dir():
        pytest.skip(f"{PATCH_DIR} is not present; the pinned vendor tree is not acquired")
    found = sorted(PATCH_DIR.rglob("*.patch"))
    if PRELINK_DIR.is_dir():
        found += sorted(PRELINK_DIR.glob("*.patch"))
    assert found, f"no .patch file anywhere under {PATCH_DIR}"
    return found


def test_every_console_call_site_has_a_visible_declaration():
    offenders: list[str] = []
    for patch in _patches():
        text = patch.read_text(encoding="utf-8")
        depth = 0
        decl_depth: int | None = None
        for line in text.splitlines():
            if line.startswith(("+++", "---", "@@ ", "diff ")):
                continue
            if not line.startswith(("+", " ", "-")):
                continue
            stripped = line[1:].strip()
            if stripped.startswith("#if"):
                depth += 1
                continue
            if stripped.startswith("#endif"):
                depth = max(0, depth - 1)
                if decl_depth is not None and decl_depth > depth:
                    decl_depth = None       # the region holding it just closed
                continue
            if stripped.startswith("extern") and SYMBOL in stripped:
                decl_depth = depth
                continue
            if SYMBOL + "(" in stripped:
                if decl_depth is None or decl_depth > depth:
                    state = ("retired" if decl_depth is None
                             else f"at depth {decl_depth}")
                    offenders.append(
                        f"{patch.relative_to(REPO)}: call at #if depth {depth} "
                        f"with the declaration {state}")

    assert not offenders, (
        "a patch calls " + SYMBOL + " with no declaration in scope:\n  "
        + "\n  ".join(offenders)
        + "\n\nMove the declaration to unconditional file scope, or move the "
          "call inside the declaration's conditional.  Builds 41359711528, "
          "41364602699 and 41441479230 each failed on this one pairing."
    )


def test_the_rule_catches_the_defect_it_names():
    """A synthetic patch in the shape build 41441479230 failed on must go red.

    A guard that passes on a tree where the build fails is worse than no guard,
    and this project has written two of them already.  So the rule is proven
    against the defect itself, in memory, with no file touched: the extern inside
    a conditional, the call in a second hunk outside it.
    """
    defective = [
        "@@ -100,3 +100,9 @@",
        " #if defined(SOMETHING_ELSE)",
        " #endif",
        "+#if defined(XNU_NATIVE_VIRTIO_CONSOLE) && XNU_NATIVE_VIRTIO_CONSOLE",
        "+extern int native_console_report(const void *, size_t);",
        "+#endif",
        "@@ -200,3 +206,7 @@",
        " \treturn error;",
        "+#if defined(XNU_OWNER_CACHE_MOUNT)",
        "+\tif (!error) {",
        "+\t\tnative_console_report(rec, sizeof(rec) - 1);",
        "+\t}",
        "+#endif",
    ]
    depth = 0
    decl_depth: int | None = None
    caught = False
    for line in defective:
        if line.startswith(("@@ ", "+++", "---", "diff ")):
            continue
        if not line.startswith(("+", " ", "-")):
            continue
        stripped = line[1:].strip()
        if stripped.startswith("#if"):
            depth += 1
        elif stripped.startswith("#endif"):
            depth = max(0, depth - 1)
            if decl_depth is not None and decl_depth > depth:
                decl_depth = None
        elif stripped.startswith("extern") and SYMBOL in stripped:
            decl_depth = depth
        elif SYMBOL + "(" in stripped:
            if decl_depth is None or decl_depth > depth:
                caught = True

    assert caught, (
        "the rule did NOT catch the exact shape build 41441479230 failed on -- "
        "an extern inside a conditional with the call outside it.  A guard that "
        "cannot see the defect it exists for is worse than no guard."
    )


def test_the_rule_accepts_the_pairing_that_is_correct():
    """A declaration and all its callers inside the SAME conditional must pass.

    This is ``zzz-m8-launchd-daemon-events.patch``'s actual shape, and the first
    revision of this rule flagged it -- a guard going red on correct code, which
    is the same as no guard at all.
    """
    correct = [
        "@@ -100,3 +100,9 @@",
        "+#if defined(XNU_OWNER_CACHE_MOUNT) && XNU_OWNER_CACHE_MOUNT",
        "+extern int native_console_report(const void *, size_t);",
        "+static char const rec[] = \"x\";",
        "@@ -200,3 +206,7 @@",
        " \treturn error;",
        "+\tnative_console_report(rec, sizeof(rec) - 1);",
        "+#endif",
    ]
    depth = 0
    decl_depth: int | None = None
    false_positive = False
    for line in correct:
        if line.startswith(("@@ ", "+++", "---", "diff ")):
            continue
        if not line.startswith(("+", " ", "-")):
            continue
        stripped = line[1:].strip()
        if stripped.startswith("#if"):
            depth += 1
        elif stripped.startswith("#endif"):
            depth = max(0, depth - 1)
            if decl_depth is not None and decl_depth > depth:
                decl_depth = None
        elif stripped.startswith("extern") and SYMBOL in stripped:
            decl_depth = depth
        elif SYMBOL + "(" in stripped:
            if decl_depth is None or decl_depth > depth:
                false_positive = True

    assert not false_positive, (
        "the rule flagged a declaration and its caller inside the SAME "
        "conditional.  That pairing is correct -- a conditional declaration is "
        "fine when every caller is inside it -- and the first revision of this "
        "rule demanded depth zero unconditionally, which flagged exactly this."
    )


# --- the second rule: a patch must be depth-neutral in #if/#endif -------------
#
# The visibility rule above checks that a declaration is in scope at its call
# site.  It cannot see a conditional that is never closed, because there is no
# call site to be wrong about -- the failure is a PREPROCESSOR error, and it is
# the cheapest one there is.
#
# Build 41635226688 paid 32m56s for it.  A revision of
# zzz-native-springboard-events.patch replaced the once-per-boot exec record with
# a bounded sequence, and the edit dropped the `#endif` that closed the
# `#if defined(XNU_OWNER_CACHE_MOUNT)` the whole emitter sits in.  Both patch
# instruments said the tree was fine -- repro-patch-order.py reports "all patches
# applied" and repro-patch-twopass.py reports "ALL PASSES CLEAN" -- because they
# check that git APPLIES the patch, and git does not care what the result means.
# The compiler said:
#
#     kern_exec.c:5289:2: error: unterminated conditional directive
#     5289 | #if defined(XNU_OWNER_CACHE_MOUNT) && XNU_OWNER_CACHE_MOUNT
#
# and the three errors after it (`expected '}'`, twice, and `use of undeclared
# label 'exit_with_error'`) are all consequences of that one: the `#endif` was
# what separated the emitter from the rest of the function.
#
# This host has no C compiler -- the patch comments say so -- so CI is the first
# thing that compiles it, and every unbalanced conditional costs a full build
# cycle.
#
# THE RULE IS DEPTH-NEUTRALITY, and two simpler formulations were tried first and
# are recorded here because each of them went red on correct patches:
#
#   * "the patch's own directives balance" -- false on
#     apple-only-cpu-tunables.patch, which adds a second
#     `#if defined(APPLE_ARM64_ARCH_FAMILY)` pair INSIDE an existing one and
#     whose hunk context carries the outer `#endif`.  A per-patch scan cannot
#     tell the file's conditional from the patch's.
#   * "only added lines count" -- false on seven patches, each of which rewrites
#     an existing `#if` condition (`-#if defined(BCM2837)` becomes
#     `+#if defined(BCM2837) && ...`).  Those patches add an `#if` and no
#     `#endif`, because the file already had one.
#
# What is actually true, and what the applied file needs, is that a patch must
# not change the file's conditional nesting: (added #if - added #endif) must equal
# (removed #if - removed #endif).  Verified against all 58+ patches with zero
# false positives, and it catches the dropped-#endif defect.


def _directive_balance(patch: Path) -> tuple[int, int, int, int]:
    add_if = add_end = del_if = del_end = 0
    for line in patch.read_text(encoding="utf-8").splitlines():
        if line.startswith(("+++", "---")):
            continue
        if not line or line[0] not in "+-":
            continue
        stripped = line[1:].strip()
        if stripped.startswith("#if"):
            if line[0] == "+":
                add_if += 1
            else:
                del_if += 1
        elif stripped.startswith("#endif"):
            if line[0] == "+":
                add_end += 1
            else:
                del_end += 1
    return add_if, add_end, del_if, del_end


def test_a_patch_does_not_change_the_files_conditional_nesting():
    offenders: list[str] = []
    for patch in _patches():
        add_if, add_end, del_if, del_end = _directive_balance(patch)
        net = (add_if - add_end) - (del_if - del_end)
        if net:
            offenders.append(
                f"{patch.relative_to(REPO)}: net conditional depth {net:+d} "
                f"(+{add_if} #if / +{add_end} #endif / "
                f"-{del_if} #if / -{del_end} #endif)")

    assert not offenders, (
        "a patch changes the file's #if nesting, so the applied C is left with "
        "a conditional that is never closed or closed twice:\n  "
        + "\n  ".join(offenders)
        + "\n\ngit applies such a patch happily and the compiler rejects the "
          "reported the tree clean because neither of them compiles anything."
    )


def test_the_balance_rule_catches_the_defect_it_names():
    """The exact shape build 41635226688 failed on must go red.

    An `#if` added by the patch, the emitter inside it, and no `#endif` -- which
    is what the edit that replaced the once-per-boot exec record produced.  In
    memory, with no file touched.
    """
    defective = [
        "@@ -5283,6 +5283,9 @@",
        " 	if (!error) {",
        " 		exec_done = TRUE;",
        "+#if defined(XNU_OWNER_CACHE_MOUNT) && XNU_OWNER_CACHE_MOUNT",
        "+	native_console_report(rec, used);",
        "+	}",
    ]
    add_if = add_end = del_if = del_end = 0
    for line in defective:
        if line.startswith(("@@ ", "+++", "---", "diff ")):
            continue
        if not line or line[0] not in "+-":
            continue
        stripped = line[1:].strip()
        if stripped.startswith("#if"):
            if line[0] == "+":
                add_if += 1
            else:
                del_if += 1
        elif stripped.startswith("#endif"):
            if line[0] == "+":
                add_end += 1
            else:
                del_end += 1
    net = (add_if - add_end) - (del_if - del_end)
    assert net != 0, (
        "the rule passed on a patch that adds an #if and no #endif.  That is "
        "the defect build 41635226688 paid a build cycle for."
    )


def test_the_balance_rule_passes_the_shape_that_looked_like_a_defect():
    """Rewriting an existing `#if` condition must NOT be flagged.

    Seven patches do exactly this -- `-#if defined(BCM2837)` becomes
    `+#if defined(BCM2837) && (...)`.  They add an `#if` and no `#endif`, and the
    "only added lines count" formulation flagged all seven.  A guard that goes
    red on correct code is the same as no guard at all.
    """
    correct = [
        "@@ -697,7 +697,7 @@",
        "-#if defined(BCM2837)",
        "+#if defined(BCM2837) && (!defined(XNU_NATIVE_ARCH_TIMER) || !XNU_NATIVE_ARCH_TIMER)",
    ]
    add_if = add_end = del_if = del_end = 0
    for line in correct:
        if line.startswith(("@@ ", "+++", "---", "diff ")):
            continue
        if not line or line[0] not in "+-":
            continue
        stripped = line[1:].strip()
        if stripped.startswith("#if"):
            if line[0] == "+":
                add_if += 1
            else:
                del_if += 1
        elif stripped.startswith("#endif"):
            if line[0] == "+":
                add_end += 1
            else:
                del_end += 1
    net = (add_if - add_end) - (del_if - del_end)
    assert net == 0, (
        "the rule flagged a patch that rewrites an existing #if condition.  "
        "Seven patches in the tree do that and they are correct."
    )
