"""Reading pinned C/C++ source as code, with the comments taken out.

Why this module exists
----------------------
A scan that does not strip comments reports prose as code.  This is not
pedantry and it is not hypothetical: ``tests/acceptance/
test_os_root_chain.py::test_the_locked_display_driver_overrides_only_declared_slots``
extracts every ``name(...) override`` from ``NativeLegacyDisplay.cpp`` and
compares that set against the slot table ``build-native-legacy-display.py``
generates.  Its first version went red on a source that overrode
``['EXPERIMENT']`` -- a name that appears nowhere in the class.  The match was
a comment reading ``// EXPERIMENT (state-document addendum AC).`` followed,
twelve lines later and with no ``;``, ``{`` or ``}`` in between, by a real
``IOReturn supportedFrameInfo(...) override``.  The extraction pattern's
``[^;{}]*`` crosses newlines, so the comment and the signature joined up.

The same class of defect is recorded three more times in this repository, each
one a copy of this function written independently for a different scan:

* ``test_native_virtio_block_records`` -- "a call site inside a comment is not
  a call site"
* ``test_native_virtio_console_records`` -- the driver's fix quotes the OLD
  broken line in a comment so the next reader knows what it replaced, and a
  scan that does not strip comments "goes red on its own documentation, which
  is the same as no test at all"
* ``test_native_screen_interaction_probe`` -- a comment with an odd number of
  double quotes shifted ``_LITERAL``'s pairing for every literal after it, so
  the record "silently stops being found"

Four copies is four chances to fix it in three places.  This module is the one
implementation, and the call sites delegate to it.

What is guaranteed
------------------
* Comment characters are replaced by spaces, never deleted, so byte offsets and
  line numbers survive -- a match position still points at the real line.
* String literals are copied through verbatim, escapes included, so ``"//"``
  inside a literal is not read as a comment.  This is what makes the scan safe
  on sources whose records are made of quotes and escapes.
* An unterminated block comment blanks to end of input rather than raising: a
  source being edited is not a reason to lose a scan.
"""

from __future__ import annotations

__all__ = ["strip_comments"]

_BACKSLASH = "\\"
_QUOTE = '"'


def strip_comments(source: str) -> str:
    """The source with ``//`` and ``/* */`` comments blanked out, in place.

    Literal-aware, because ``"//"`` inside a string is not a comment and these
    sources are made of quotes and escapes.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch != _QUOTE:
            if ch == "/" and i + 1 < n and source[i + 1] == "/":
                while i < n and source[i] != "\n":
                    out.append(" ")
                    i += 1
                continue
            if ch == "/" and i + 1 < n and source[i + 1] == "*":
                end = source.find("*/", i + 2)
                stop = n if end == -1 else end + 2
                # Keep the newlines: a reader matching a position against a
                # line number is reading the real file, not a collapsed copy.
                out.append("".join("\n" if c == "\n" else " " for c in source[i:stop]))
                i = stop
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
        while i < n:
            if source[i] == _BACKSLASH and i + 1 < n:
                out.append(source[i : i + 2])
                i += 2
                continue
            out.append(source[i])
            if source[i] == _QUOTE:
                i += 1
                break
            i += 1
    return "".join(out)
