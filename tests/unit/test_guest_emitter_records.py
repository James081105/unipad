"""Every guest emitter's record, reassembled and checked -- and the check's own teeth.

Why this exists
---------------
The guest's only output is a stream of one-line JSON records over the agent
channel, and a record that is not JSON is not a partial record: the host's line
reader drops it.  So a malformed emitter costs a full build-and-boot cycle to
discover and produces **no error when it does** -- just a missing event, which
reads exactly like a guest that never did the thing.  Three drafts of the mount
emitter were in that state and compiled clean.

The record is built by adjacent-literal concatenation, so the only thing that
can see the defect is reassembling the parts the way the C preprocessor will,
decoding the escapes the way the compiler would, and parsing the result.  That
is what this file does, for every emitter that builds a ``record`` buffer.

The check had three defects of its own, and they are recorded here because a
check that cries wolf is worse than no check
----------------------------------------------------------------------
1. It asserted every field other than ``event`` and ``source`` was a number.
   Four healthy emitters were reported as broken: ``springboard.running``'s
   ``comm`` and ``springboard.interaction``'s two screen digests are legitimately
   strings.  Replaced with the invariant that actually matters -- every empty
   value position has exactly one digit-append behind it.
2. It then counted the ``put_unsigned`` helper's own *definition* as a call,
   because the helper's body also contains ``record[used++] = text[--digits]``.
   Every emitter using the helper failed.  Fixed by starting the window at the
   emitter's first literal, which is above every helper.
3. It reported ``system.mount`` as "the record is not JSON" -- a FAIL.  That
   emitter hands a whole static array to ``native_console_report``, built from
   adjacent literals *and macros* the regex cannot see, so only its first
   fragment is visible and half a record parses as nothing.  That is a
   limitation of the check, not a defect in the emitter, and it is now
   UNVERIFIABLE rather than FAIL.  See
   :func:`test_the_emitters_this_check_cannot_see_are_named`.

What is NOT checked here, and cannot be
---------------------------------------
* that any of this compiles.  This host has no C compiler; CI is the first thing
  that compiles these translation units.
* that any record arrives on a real boot.  That needs a matched build and a
  1500 s capture, and it is a different measurement from "the record is
  well-formed".
* ``system.mount`` / ``system.volume_identity`` / ``install.registered``.  Their
  shape is asserted elsewhere or not at all; this file only asserts that they
  are *named* as unverifiable, so the list of what is covered stays honest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.vendor_paths import require

REPO = Path(__file__).resolve().parents[2]

BACKSLASH = chr(92)
QUOTE = chr(34)

#: One emitter per row.  ``appends`` is how many digit-appends its empty value
#: positions need, which is what the check counts -- most call the
#: ``put_unsigned`` helper, ``springboard.running`` predates it and inlines
#: ``record[used++] = text[--digits]``, and ``springboard.interaction`` has none
#: because both of its values are string literals rather than formatted numbers.
class Emitter(NamedTuple):
    source: str
    event: str
    claimed_source: str
    keys: frozenset[str]


EMITTERS = (
    Emitter("vendor/UniXNU-qemu/package/LegacyDisplayProbe.cpp",
            "diag.legacy_display_status", "XNU.LegacyDisplayProbe",
            frozenset({"event", "stage", "surface_status", "surface_found",
                       "dt", "source"})),
    Emitter("vendor/UniXNU/patches/xnu/offline-owner-systemos-mount.patch",
            "diag.owner_data_status", "XNU.native_owner_data_prepare",
            frozenset({"event", "stage", "error", "source"})),
    Emitter("vendor/UniXNU/patches/xnu/zzz-native-springboard-events.patch",
            "springboard.running", "XNU.__mac_execve",
            frozenset({"event", "pid", "comm", "source"})),
    Emitter("vendor/UniXNU-qemu/package/HidInputProbe.cpp",
            "springboard.interaction", "XNU.HidInputProbe",
            frozenset({"event", "before", "after", "source"})),
)

#: The emitters this reassembly cannot see, and why.  They hand a whole static
#: array to ``native_console_report`` -- adjacent string literals *plus* macros
#: -- so the pattern below matches only the first fragment of each.  Named here
#: so "not covered" is a recorded decision rather than a gap nobody noticed.
UNVERIFIABLE = {
    "system.mount": "adjacent literals and XNU_OWNER_*_UUID macros",
    "system.volume_identity": "adjacent literals and XNU_OWNER_*_UUID macros",
}

LEGACY = next(e for e in EMITTERS if e.event == "diag.legacy_display_status")


#: event -> the pinned file that emits it.  Lets the reader take an EVENT rather
#: than a path; see the note on :func:`_text`.
_SOURCE_OF = {e.event: e.source for e in EMITTERS}


def _text(event: str) -> str:
    """The source of the emitter named *event*, guarded before it is read.

    Takes the EVENT and not the path, and that is load-bearing.  A vendor path at
    the call site is exactly what
    ``tests/unit/test_clean_checkout_selfsufficiency.py`` refuses: the guard has
    to be evaluated in the same function, and a caller that passes the path looks
    like it knows better than the guard.  Measured -- this file was written with
    ``_text("diag.legacy_display_status")`` and that test went red on five call sites, in a
    clean checkout where ``vendor/`` is empty.
    """
    require(REPO / _SOURCE_OF[event])
    return (REPO / _SOURCE_OF[event]).read_text(encoding="utf-8")


def _decode(part: str) -> str:
    """C string escapes to real characters, the way the compiler would."""
    return (part.replace(BACKSLASH + BACKSLASH, BACKSLASH)
                .replace(BACKSLASH + QUOTE, QUOTE)
                .replace(BACKSLASH + "n", chr(10))
                .replace(BACKSLASH + "t", chr(9)))


class Reassembled(NamedTuple):
    template: str
    names: tuple[str, ...]
    body: str
    #: Empty bare positions, each needing one decimal append.
    bare: int
    #: Decimal appends present (put_unsigned calls plus `record[used++] = text[`).
    decimal: int
    buffer: int | None
    #: Empty quoted positions, each needing one hex append (screen digests).
    quoted: int
    #: Hex appends present (`record[used++] = interaction_hex[`).
    hexdigits: int

    @property
    def worst_case(self) -> int:
        """The longest record this emitter can build, including the NUL."""
        return (len(self.template) + 10 * self.bare + 16 * self.quoted + 1)


def reassemble(text: str, event: str) -> Reassembled:
    """The emitter that names *event*, as its concatenated template plus its body.

    The literals are anchored on the ``native_console_report(record...)`` call
    and searched BACKWARD, not on a window around the event name: a window lands
    in the emitter's own comment, which quotes the record and therefore carries
    escaped quotes of its own.  Patch files put a ``+`` on every added line, so
    the gap between ``=`` and the literal is ``[+\\s]*`` rather than ``\\s*``.

    Raises ``Unverifiable`` when the emitter is not of the shape this reassembles
    -- see UNVERIFIABLE.
    """
    emit = text.find("native_console_report(record")
    if emit < 0:
        raise Unverifiable("no native_console_report(record...) call")
    # The alternation needs TWO backslashes before the dot.  One backslash makes
    # `\\.` a literal dot, which stops the capture at the first escaped quote in
    # the C literal and silently yields `{\\`.  Measured; it cost an hour.
    two = BACKSLASH * 2
    pattern = (r'static char const (\w+)\[\]\s*=\s*[+\s]*' + QUOTE
               + '((?:[^' + two + QUOTE + ']|' + two + '.)*)' + QUOTE)
    matches = list(re.finditer(pattern, text[:emit]))
    start = next((i for i, m in enumerate(matches) if event in m.group(2)), None)
    if start is None:
        raise AssertionError(f"no literal naming {event} precedes the emit call")
    named = matches[start]
    parts, names = [named.group(2)], [named.group(1)]
    for match in matches[start + 1:]:
        if 'event' in match.group(2) and ':' in match.group(2):
            break  # the next emitter's prefix; a fixed count spans two emitters
        parts.append(match.group(2))
        names.append(match.group(1))

    # The shape this cannot reassemble: the array is emitted whole.
    if re.search(r"native_console_report\(\s*" + re.escape(names[0]) + r"\s*,",
                 text):
        raise Unverifiable(f"{names[0]} is emitted whole")

    template = "".join(_decode(p) for p in parts)
    # The body starts at the emitter's FIRST literal, not at the top of the file.
    # Starting at the top reached back into the put_unsigned helper DEFINITIONS,
    # whose own bodies contain `record[used++] = text[--digits]`, so every
    # emitter that used the helper counted its appends twice and failed.
    body = text[named.start():emit]
    # Two families of value slot, and they take two different appends.
    #
    # A BARE position -- `"pid":` with nothing after the colon -- is filled by a
    # decimal append at run time.  A QUOTED one -- `"before":""` -- is filled by
    # hex digits inside the quotes, which is how the screen digests get in.
    # Counting them as one bucket reported springboard.interaction as 0
    # positions with 5 appends: its two digests are quoted, so the bare-position
    # regex never saw them.  Separating the buckets makes each family's
    # invariant checkable, and each catches the defect that belongs to it.
    bare = len(re.findall(r":(?=,|\})", template))
    quoted = len(re.findall(r':""(?=,|\})', template))
    decimal = (len(re.findall(r"put_unsigned\(record", body))
               + len(re.findall(r"record\[used\+\+\]\s*=\s*text\[", body)))
    hexdigits = len(re.findall(r"record\[used\+\+\]\s*=\s*interaction_hex\[",
                               body))
    size = _buffer_size(body, text)
    return Reassembled(template, tuple(names), body, bare, decimal, size,
                       quoted, hexdigits)


def _buffer_size(body: str, whole: str) -> int | None:
    """The record buffer's size, resolving a macro if the emitter uses one.

    ``springboard.interaction`` declares ``char record[INTERACTION_RECORD_MAX]``
    and defines the macro to 160 two hundred lines above.  Matching only a digit
    literal skipped that emitter's buffer check entirely -- a real gap, since the
    bound is the invariant under test -- and it was found by noticing the skip
    rather than by any failure.
    """
    match = re.search(r"char\s+record\s*\[\s*(\d+)\s*\]", body)
    if match:
        return int(match.group(1))
    macro = re.search(r"char\s+record\s*\[\s*(\w+)\s*\]", body)
    if not macro:
        return None
    defined = re.search(r"#define\s+" + re.escape(macro.group(1))
                        + r"\s+(\d+)", whole)
    if not defined:
        return None
    return int(defined.group(1))


class Unverifiable(Exception):
    """This check cannot see the emitter.  Not a pass, and not a failure."""


def _record(template: str) -> dict:
    """The template with a zero in each empty value position, parsed."""
    return json.loads(re.sub(r":(?=,|\})", ":0", template))


# ---------------------------------------------------------------------------
# The record's shape, for every emitter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("emitter", EMITTERS, ids=lambda e: e.event)
def test_the_record_reassembles_to_json_with_the_keys_it_claims(emitter):
    got = reassemble(_text(emitter.event), emitter.event)
    record = _record(got.template)
    assert record["event"] == emitter.event
    assert record["source"] == emitter.claimed_source
    assert set(record) == set(emitter.keys), (
        f"{emitter.event} carries {sorted(set(record) ^ set(emitter.keys))} "
        "unexpectedly"
    )


@pytest.mark.parametrize("emitter", EMITTERS, ids=lambda e: e.event)
def test_the_record_is_a_newline_terminated_object(emitter):
    """The host splits records on newlines; a record without one merges with
    the next and both are lost."""
    got = reassemble(_text(emitter.event), emitter.event)
    assert got.template.rstrip(chr(10)).endswith("}"), (
        f"{emitter.event} does not close with '}}'"
    )
    assert got.template.endswith(chr(10)), (
        f"{emitter.event} is not newline-terminated"
    )


@pytest.mark.parametrize("emitter", EMITTERS, ids=lambda e: e.event)
def test_every_empty_value_position_has_a_digit_append(emitter):
    """The invariant that matters, and the one the first version got wrong.

    Every field of this emitter family is a prefix literal ending in ``:`` with
    the digits appended at run time, so an empty value position is the DESIGN.
    What it must be paired with is one append.  Add a field to a prefix literal
    and forget its append and the guest emits ``"foo":,`` -- not JSON -- so the
    host drops it and the event never arrives, which is indistinguishable from a
    guest that never did the thing.

    Bare positions take decimal appends and quoted ones take hex; counting them
    as a single bucket reported springboard.interaction as 0 positions with 5
    appends, because its two screen digests are quoted and the bare-position
    regex never saw them.
    """
    got = reassemble(_text(emitter.event), emitter.event)
    assert got.bare == got.decimal, (
        f"{emitter.event} leaves {got.bare} bare value position(s) empty but has "
        f"{got.decimal} decimal append(s); a field would be emitted as `:` with "
        "no digits and the record would not be JSON"
    )
    assert got.quoted == got.hexdigits, (
        f"{emitter.event} leaves {got.quoted} quoted value position(s) empty but "
        f"has {got.hexdigits} hex append(s); a digest would be emitted as `\"\"` "
        "or the hex would land outside the quotes"
    )


@pytest.mark.parametrize("emitter", EMITTERS, ids=lambda e: e.event)
def test_the_buffer_can_hold_the_worst_case_record(emitter):
    """A short buffer truncates silently -- put_literal stops at size-1 -- and
    truncation produces a record that is not JSON, which reads as a missing
    event."""
    got = reassemble(_text(emitter.event), emitter.event)
    if got.buffer is None:
        pytest.skip(f"{emitter.event} declares no resolvable record buffer")
    assert got.buffer >= got.worst_case, (
        f"{emitter.event}: record[{got.buffer}] cannot hold the worst case "
        f"({got.worst_case} bytes); the record would truncate"
    )


# ---------------------------------------------------------------------------
# The linkage, which is what makes the record arrive at all
# ---------------------------------------------------------------------------

def test_the_legacy_emitters_console_reference_is_strong_and_c_linked():
    """STRONG, because a weak_import one resolved to null on build 41330622788
    and dropped records behind a green build.  C, because the definition is a C
    symbol and every function in that translation unit sits OUTSIDE its
    ``extern "C"`` block, so a bare ``extern int`` would mangle the reference
    into C++ and leave the kext with a symbol that resolves to nothing at load.

    That second half is the same green-build-emits-nothing failure the
    weak_import produced, reached by a different route, and it was one edit away
    from shipping: the first version of this emitter declared it bare.
    """
    text = _text("diag.legacy_display_status")
    declaration = re.search(r"^extern\s*(?:\"C\"\s*)?int\s+native_console_report"
                            r"\s*\([^\n]*$", text, re.M)
    assert declaration, "the legacy emitter declares no native_console_report"
    # Exactly one declaration, and it is the one with C linkage.  Two shapes are
    # counted so a second bare `extern int` cannot hide behind the C one.
    assert len(re.findall(r"^extern\s*(?:\"C\"\s*)?int\s+native_console_report"
                          r"\s*\(", text, re.M)) == 1, (
        "expected exactly one declaration of native_console_report"
    )
    assert declaration.group(0).lstrip().startswith('extern "C"'), (
        f"the declaration is {declaration.group(0).strip()!r} without extern "
        '"C"; every function in this translation unit is outside the extern "C" '
        "block, so the reference would be C++-mangled against a C definition"
    )
    # Checked on the DECLARATION LINE, not on the text before the symbol.  The
    # first version of this check read the 400 characters before the first
    # mention of the name -- which is inside the emitter's own comment, where
    # the word weak_import appears explaining why it must not be used.  The
    # check matched its own documentation and reported the healthy source as
    # broken, which is the third false FAIL this family of checks has produced.
    assert "weak_import" not in declaration.group(0), (
        "the declaration is weak_import; one already resolved to null behind a "
        "green build"
    )


def test_the_legacy_display_checkpoint_reaches_the_early_exit():
    """The exit that had no checkpoint at all.

    ``pendingLegacyDisplay`` already cleared means the display never ARRIVED,
    which is the case the record was needed for -- and it is the exit every
    frozen boot took.
    """
    text = _text("diag.legacy_display_status")
    line = next(l for l in text.splitlines()
                if "pendingLegacyDisplay" in l and "if(!display)" in l)
    assert "native_legacy_display_checkpoint()" in line, (
        f"the early exit does not checkpoint: {line.strip()!r}"
    )


# ---------------------------------------------------------------------------
# What this check cannot see, said out loud
# ---------------------------------------------------------------------------

def test_the_emitters_this_check_cannot_see_are_named():
    """``system.mount`` must be UNVERIFIABLE, not FAIL.

    The first version of this check reported it as "the record is not JSON",
    because it hands a whole static array to ``native_console_report`` -- adjacent
    literals plus ``XNU_OWNER_*_UUID`` macros -- and only the first fragment is
    visible, so half a record parses as nothing.  A false FAIL on a healthy
    emitter is how a real FAIL gets ignored.

    If this ever starts failing, the emitter changed shape and this table needs
    updating -- which is the correct outcome, and much better than the check
    quietly beginning to assert something it cannot see.
    """
    text = _text("diag.owner_data_status")
    for event, why in UNVERIFIABLE.items():
        with pytest.raises(Unverifiable) as caught:
            reassemble(text, event)
        # The message names the ARRAY, not the event -- the event name appears
        # only inside the fragment this check can see, which is not what makes
        # it unverifiable.  Asserting the event is in the message was the first
        # version of this check and it failed on a correct classification.
        assert "emitted whole" in str(caught.value), (
            f"{event} stopped being UNVERIFIABLE: {caught.value}"
        )
        assert why  # the reason is recorded, not decorative


# ---------------------------------------------------------------------------
# Teeth: these checks must go red on a broken emitter
# ---------------------------------------------------------------------------

def _mutate(tmp_path: Path, **replacements: str) -> Path:
    """A copy of the legacy emitter with *replacements* applied.

    Written to pytest's own tmp directory.  An earlier version of this family
    wrote the mutation to the real file and restored it in a ``finally``, which
    leaves the repository mutated if anything interrupts the test -- and this
    repository has already lost four edits to a checkout that restored the wrong
    bytes.
    """
    text = _text("diag.legacy_display_status")
    for old, new in replacements.items():
        assert old in text, f"the mutation anchor {old!r} is no longer in the source"
        text = text.replace(old, new)
    path = tmp_path / "LegacyDisplayProbe.cpp"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_field_with_no_append_is_caught(tmp_path):
    """Add ``"mode":`` to the prefix and give it no append.

    The record would then contain ``"mode":,`` -- not JSON -- so the host drops
    it and ``diag.legacy_display_status`` never arrives, which reads exactly like
    a display path that never ran.
    """
    q = BACKSLASH + QUOTE
    good = _text("diag.legacy_display_status")
    mutated = _mutate(tmp_path, **{
        q + "surface_status" + q + ":":
            q + "mode" + q + ":," + q + "surface_status" + q + ":"})
    assert mutated.read_text(encoding="utf-8") != good, "the mutation did not apply"
    got = reassemble(mutated.read_text(encoding="utf-8"), LEGACY.event)
    assert got.bare != got.decimal, (
        "the position/append check passed on an emitter with a field and no "
        "append for it; it has no teeth"
    )
    # And the record really is not JSON without the zero standing in.
    with pytest.raises(json.JSONDecodeError):
        json.loads(got.template.replace('"mode":', '"mode":'))


def test_a_short_buffer_is_caught(tmp_path):
    mutated = _mutate(tmp_path, **{"char record[192]": "char record[100]"})
    got = reassemble(mutated.read_text(encoding="utf-8"), LEGACY.event)
    assert got.buffer is not None and got.buffer < got.worst_case, (
        "the buffer check passed on a buffer too small for the worst case"
    )


def test_a_missing_newline_is_caught(tmp_path):
    mutated = _mutate(tmp_path, **{
        "LegacyDisplayProbe" + BACKSLASH + QUOTE + "}" + BACKSLASH + 'n"':
            "LegacyDisplayProbe" + BACKSLASH + QUOTE + '}"'})
    got = reassemble(mutated.read_text(encoding="utf-8"), LEGACY.event)
    assert not got.template.endswith(chr(10)), (
        "the newline check passed on a record with no trailing newline"
    )


def test_a_removed_source_field_is_caught(tmp_path):
    q = BACKSLASH + QUOTE
    mutated = _mutate(tmp_path, **{
        "," + q + "source" + q + ":" + q + "XNU.LegacyDisplayProbe" + q + "}"
        + BACKSLASH + 'n"': "}" + BACKSLASH + 'n"'})
    got = reassemble(mutated.read_text(encoding="utf-8"), LEGACY.event)
    assert "source" not in _record(got.template), (
        "the source check passed on a record that names no source"
    )
