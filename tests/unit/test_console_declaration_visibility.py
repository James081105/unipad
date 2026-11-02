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
