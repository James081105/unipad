"""One record, one event, and no second event under the same prefix.

Why this exists
---------------
``milestones.py``'s probe ``p.virtio_blk_read_back`` asks for a guest event
literally named ``virtio_blk.read_back``.  Nothing in either pinned tree emitted
any ``virtio_blk.*`` event at all, so the probe could only ever report missing.

Emitting it is the easy half.  The half that has cost this repository real boots
is everything *around* the emission:

* a record that is not one line of valid JSON is SKIPPED by
  ``_guest_records``, and a skipped record is indistinguishable from an absent
  one -- the gate says "missing" and the reader concludes the guest never read
  the disk;
* a record sent with ``sizeof`` taken from a *pointer* arrives seven bytes long
  behind a green build.  Measured on a real guest: three XNU records arrived as
  ``b'{"event{"event{"event'``;
* a second ``virtio_blk.*`` event is not "extra context", it is a loaded gun.
  ``_device_enumeration_without_readback`` fires when any event under that prefix
  other than ``virtio_blk.read_back`` is observed and the read-back probe has not
  passed.  Emitting ``virtio_blk.enumerated`` alongside the read-back therefore
  fails the release verdict the moment the read-back record does not arrive --
  the veto is dormant today only because zero ``virtio_blk.*`` emitters exist;
* a *weak* reference to the console's report entry point resolves to null and
  silently drops every record behind it.  That is the most expensive lesson in
  this repository and it is recorded at length in
  ``vendor/UniXNU/patches/xnu/offline-owner-systemos-mount.patch``.

So this file simulates the C preprocessor on the real source -- adjacent string
literal concatenation included -- and checks the result, which is the only way
to get these answers in under a second on a host with no C compiler.

Where the driver source is read from
------------------------------------
``vendor/`` is git-excluded (``.gitignore``:39), so the pinned checkout is not
part of the repository and a clean checkout has none of it.  A change to a
pinned driver therefore cannot be committed at its real path, so this change is
committed under ``vendor-patches/`` at the same relative path and the guard
reads the pinned tree once it carries the change and the committed copy until
then.  ``test_the_pin_carries_the_emitter`` is the one test that notices the
difference.  When neither exists the guard skips, the same honest skip
``test_native_virtio_console_records.py`` uses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.vendor_paths import require

REPO = Path(__file__).resolve().parents[2]

#: The pinned checkout's copy, which is what a kernel is actually built from.
PINNED = REPO / "vendor" / "UniXNU" / "drivers" / "NativeVirtioBlock" / "NativeVirtioBlock.cpp"
#: This change, at the same relative path, until the pin carries it.
PENDING = REPO / "vendor-patches" / "UniXNU" / "drivers" / "NativeVirtioBlock" / "NativeVirtioBlock.cpp"

#: The escape scanner.  Built from chr() rather than written as ``"\\"`` so that
#: nothing can collapse it: a bash heredoc turns the two characters backslash-n
#: into a real newline, and that mangled this verifier's ancestor twice.
BACKSLASH = chr(92)
NEWLINE = chr(10)
TAB = chr(9)
ESCAPES = {"n": NEWLINE, "t": TAB, "r": chr(13), "0": chr(0)}

#: The one event the gate names, and the only one allowed under the prefix.
READ_BACK = "virtio_blk.read_back"

#: The prefix that arms ``_device_enumeration_without_readback``.  Every other
#: name under it is a veto; only this one is not.
PREFIX = "virtio_blk."

#: The record's fields.  Written out rather than read back from the record, so a
#: test that merely echoes whatever the source says cannot pass: the source has
#: to agree with this list, and a field invented at the call site fails here.
RECORD_FIELDS = ("event", "driver", "verified", "source")

#: The anchors that say a driver source carries this change.  A pin that has not
#: been moved yet has none of them, and the guard must not go red on that --
#: ``test_the_pin_carries_the_emitter`` is the precise signal for it, and it
#: says what to do.  Fifteen guards failing with "read_back_record is not
#: declared" say much less.
ANCHORS = (
    'extern "C" IOReturn native_console_report(const void *, size_t);',
    "read_back_record",
    "readBackReported",
)


def _carries(path: Path) -> bool:
    """Whether ``path`` holds the emitter this file guards."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return all(anchor in text for anchor in ANCHORS)


# ---------------------------------------------------------------------------
# A small preprocessor simulator
# ---------------------------------------------------------------------------


def _literals(fragment: str) -> list[str]:
    """Every C string literal in ``fragment``, with escapes resolved.

    This is the preprocessor's adjacent-literal concatenation, minus the
    concatenation: the caller joins the result.  A record assembled from two
    literals is one string to the compiler and must be one string here too.
    """
    out: list[str] = []
    i, n = 0, len(fragment)
    while i < n:
        if fragment[i] != '"':
            i += 1
            continue
        i += 1
        buf: list[str] = []
        while i < n and fragment[i] != '"':
            if fragment[i] == BACKSLASH and i + 1 < n:
                buf.append(ESCAPES.get(fragment[i + 1], fragment[i + 1]))
                i += 2
            else:
                buf.append(fragment[i])
                i += 1
        i += 1
        out.append("".join(buf))
    return out


def _strip_comments(source: str) -> str:
    """The source with ``//`` and ``/* */`` comments blanked out, in place.

    A call site inside a comment is not a call site, and an event name inside a
    comment is not an emitted event.  Literal-aware, because ``"//"`` inside a
    string is not a comment and these records are made of quotes and escapes.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch != '"':
            if ch == "/" and i + 1 < n and source[i + 1] == "/":
                while i < n and source[i] != NEWLINE:
                    out.append(" ")
                    i += 1
                continue
            if ch == "/" and i + 1 < n and source[i + 1] == "*":
                end = source.find("*/", i + 2)
                stop = n if end == -1 else end + 2
                out.append(" " * (stop - i))
                i = stop
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
        while i < n:
            if source[i] == BACKSLASH and i + 1 < n:
                out.append(source[i:i + 2])
                i += 2
                continue
            out.append(source[i])
            if source[i] == '"':
                i += 1
                break
            i += 1
    return "".join(out)


def _call_arguments(source: str, opener: str) -> list[tuple[int, str]]:
    """Every call to ``opener(`` and its argument text, parentheses balanced.

    A regex cannot do this: the arguments contain commas, so ``[^)]*`` stops at
    the first inner close paren and silently reports a shorter argument than the
    one the compiler sees.
    """
    out: list[tuple[int, str]] = []
    for m in re.finditer(re.escape(opener) + r"\s*\(", source):
        depth, i, start = 1, m.end(), m.end()
        while i < len(source) and depth:
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
            i += 1
        if depth == 0:
            out.append((m.start(), source[start:i - 1]))
    return out


def _brace_body(source: str, header: str, start: int = 0) -> str:
    """The brace-balanced body that follows ``header`` at or after ``start``.

    ``_call_arguments`` gives the text between the parentheses, which for a
    *definition* is only the parameter list -- so every assertion about what a
    function does has to be made against the braces that follow.
    """
    hit = re.search(header, source[start:])
    if hit is None:
        return ""
    depth, i = 1, start + hit.end()
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[start + hit.end():i - 1] if depth == 0 else ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _driver() -> Path:
    """The driver source under test.

    The pinned checkout's copy once it carries the change, and this
    repository's committed copy until then.  Preferring the copy that carries
    the emitter is deliberate: a pin that has not been moved yet must not turn
    fifteen guards red with "read_back_record is not declared" -- that says
    much less than the one test whose whole job is to notice the pin has not
    moved.  When NEITHER copy carries it, the pinned one is returned so the
    guards report the real tree rather than passing on a file nobody builds.
    """
    present = [path for path in (PINNED, PENDING) if path.is_file()]
    if not present:
        # A test that cannot read its subject has measured nothing.  Skipping
        # says that; passing would report a contract nobody looked at.
        pytest.skip(
            f"neither {PINNED.relative_to(REPO).as_posix()} nor "
            f"{PENDING.relative_to(REPO).as_posix()} is present.  vendor/ is "
            "git-excluded, so a clean checkout has neither."
        )
    for candidate in present:
        if _carries(candidate):
            return candidate
    return present[0]


@pytest.fixture(scope="module")
def source() -> str:
    path = _driver()
    require(path)
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def code(source: str) -> str:
    """The source with comments blanked, so a comment is never a call site."""
    return _strip_comments(source)


@pytest.fixture(scope="module")
def record(source: str) -> str:
    """The read-back record as the compiler would concatenate it."""
    m = re.search(r"static\s+char\s+const\s+read_back_record\s*\[\s*\]\s*=\s*(.+?);",
                  source, re.S)
    assert m, "read_back_record is not declared in the driver"
    return "".join(_literals(m.group(1)))


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_the_record_is_one_line_of_valid_json(record: str):
    """One line, ending in one newline, parsing as a JSON object.

    The gate tries ``json.loads`` on each line and, when that fails, falls back
    to splitting on the first ``:``.  A record that is neither is SKIPPED, and a
    skipped record is indistinguishable from an absent one.  ``_guest_events``
    then keeps only the FIRST record per event, so a record split across two
    lines is invisible even when both halves parse.
    """
    assert record.count(NEWLINE) == 1, (
        f"the record is {record.count(NEWLINE)} lines, not 1: {record!r}")
    assert record.endswith(NEWLINE), (
        f"the record does not end in a newline: {record!r}")

    try:
        parsed = json.loads(record.strip())
    except json.JSONDecodeError as exc:
        pytest.fail(f"the record is not valid JSON ({exc}): {record!r}")

    assert isinstance(parsed, dict), (
        f"the record is {type(parsed).__name__}, not an object: {record!r}")


def test_the_event_name_is_exactly_the_one_the_gate_asks_for(record: str):
    """``virtio_blk.read_back``, spelled that way and no other way.

    A near miss -- ``virtio_blk.readback``, ``virtio_blk.read_back_complete`` --
    is reported missing, which reads exactly like a driver that never ran.
    """
    parsed = json.loads(record.strip())
    assert parsed.get("event") == READ_BACK, (
        f"the record carries event {parsed.get('event')!r}, want {READ_BACK!r}")


def test_the_record_names_only_fields_the_code_holds(record: str):
    """Every field is one this function holds, and none is invented.

    The mount emitter's two defects both compiled clean and read on the host as
    "the guest never mounted": a dropped key, and a macro whose value came out
    unquoted.  Both are a record claiming something the code does not hold.  So
    the key set is written out here and the record has to agree with it.
    """
    parsed = json.loads(record.strip())
    assert tuple(sorted(parsed)) == tuple(sorted(RECORD_FIELDS)), (
        f"the record carries {sorted(parsed)}, want {sorted(RECORD_FIELDS)}.  A "
        "field the emitter does not hold is a false statement in the evidence "
        "the gate reads; a missing one is a fact the reader never sees.")
    assert parsed["verified"] is True, (
        f"verified is {parsed['verified']!r}, not a real JSON boolean.  A "
        "string, a number or a hole is a false statement.")
    assert parsed["driver"] == "NativeVirtioBlock", (
        f"driver is {parsed['driver']!r}; the emitter is NativeVirtioBlock")


def test_the_boolean_is_a_real_boolean_not_a_string_or_a_number(record: str):
    """``true``, not ``"true"`` and not ``1``.

    The console driver's earlier draft wrote ``"vbar_el1":0`` into a literal
    while holding the real value in a variable: it parsed, the gate accepted it,
    and it was wrong.
    """
    parsed = json.loads(record.strip())
    value = parsed["verified"]
    assert isinstance(value, bool), (
        f"verified is {type(value).__name__} ({value!r}), not a bool")


# ---------------------------------------------------------------------------
# The veto: one event, and only one
# ---------------------------------------------------------------------------


def _fork_files() -> list[Path]:
    """Every file in the driver fork, pinned copy and pending copy both.

    A second ``virtio_blk.*`` emitter does not have to be in the .cpp -- a
    header the driver includes, or a record in a sibling file, arms the same
    veto.  So the scan is over the fork, not over the file under test.
    """
    seen: dict[str, Path] = {}
    for directory in (PINNED.parent, PENDING.parent):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                seen[str(path.resolve())] = path
    return [seen[key] for key in sorted(seen)]


def test_no_second_virtio_blk_event_name_exists_anywhere_in_the_fork():
    """THE ANTI-VETO TEST.  No event under ``virtio_blk.`` but the one.

    ``_device_enumeration_without_readback`` (milestones.py:1719) fires when any
    event starting with ``virtio_blk.`` other than ``virtio_blk.read_back`` is
    observed and the read-back probe has not passed.  Adding
    ``virtio_blk.enumerated`` or ``virtio_blk.probe`` "for context" therefore
    arms a veto that fails the release verdict the moment the read-back record
    does not arrive -- which is precisely the failure this whole change exists
    to prevent.  The veto is dormant today only because zero ``virtio_blk.*``
    emitters exist in either pinned tree.

    The scan is over the whole fork, with comments stripped: an event name
    quoted in a comment is not an emitted event, and a scan that does not strip
    comments goes red on its own documentation.
    """
    # The fork is a pinned checkout this host acquires separately, and a clean
    # checkout (every CI run, and `git archive HEAD`) has none.  Without it there
    # is nothing to scan, and the honest answer is "not verified here" -- which is
    # what `require` records.  Measured: on run 41497905806 this test failed with
    # "no driver fork files to scan" on a checkout that had never acquired one,
    # which is a red stage for a reason no code change can fix.
    require(PINNED)
    files = _fork_files()
    assert files, "no driver fork files to scan"
    offenders: list[str] = []
    for path in files:
        text = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for name in sorted(set(re.findall(re.escape(PREFIX) + r"[a-z0-9_]+", text))):
            if name != READ_BACK:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        "these event names sit under the virtio_blk. prefix and are not "
        f"{READ_BACK!r}: " + ", ".join(offenders) + ".  Emitting any of them "
        "arms _device_enumeration_without_readback, which fails the release "
        "verdict whenever the read-back record does not arrive.  One event; "
        "that is the whole job.")


def test_the_read_back_is_the_only_event_the_fork_emits(record: str, source: str):
    """The fork emits this event and no other guest event at all.

    Stated separately from the anti-veto test so the two cannot be conflated:
    "no forbidden name" passes vacuously when nothing is emitted, and "exactly
    one name" is the property that says the emitter is really there.
    """
    names = set()
    # The C source spells the quotes inside a string literal as backslash-quote,
    # so the pattern needs the backslash escaped FOR THE REGEX, not merely
    # present.  ``\\"`` in a pattern means a literal backslash then a quote;
    # ``\"`` means only a quote, and a pattern built that way matches nothing
    # here while looking perfectly correct.
    needle = (re.escape(BACKSLASH + '"event' + BACKSLASH + '":' + BACKSLASH + '"')
              + r"([a-z0-9_.]+)" + re.escape(BACKSLASH + '"'))
    for m in re.finditer(needle, _strip_comments(source)):
        names.add(m.group(1))
    assert names == {READ_BACK}, (
        f"the fork's records name {sorted(names)}, want [{READ_BACK}]")


# ---------------------------------------------------------------------------
# The emitter is a real gate, not an unconditional send
# ---------------------------------------------------------------------------


def test_the_emitter_is_gated_on_the_read_having_verified(code: str):
    """The emission is inside an ``if`` that tests the read's own result.

    An unconditional send would put "the guest read known content back" on the
    wire for a read that failed -- or for one that never happened.  The gate is
    the read's return value plus the byte count, both of which this function
    holds.
    """
    m = re.search(
        r"if\s*\(\s*!readBackReported\s*&&\s*result\s*==\s*kIOReturnSuccess"
        r"\s*&&\s*actual\s*==\s*count\s*\*\s*512\s*\)", code)
    assert m, (
        "the emission is not gated on "
        "'!readBackReported && result == kIOReturnSuccess && actual == count * "
        "512'.  A record emitted outside that gate is a statement about a read "
        "that may have failed.")


def test_the_gate_means_what_it_says(code: str):
    """``kIOReturnSuccess`` from readSectors really means verified.

    This is what makes the gate above load-bearing rather than decorative:
    readSectors() takes an early ``return kIOReturnNotPrivileged`` the moment a
    single vr_verify_block() call does not return VR_OK, and only reaches its
    success return after the loop has covered every block in the span.  If that
    early return were ever removed, the gate would pass on unverified DMA.
    """
    body = _brace_body(code, r"IOReturn\s+NativeVirtioBlock::readSectors\s*\([^)]*\)\s*\{")
    assert body, "readSectors is not defined with a brace body"
    assert "vr_verify_block" in body, (
        "readSectors no longer calls vr_verify_block, so its success return no "
        "longer means the bytes matched the manifest's digest table")
    assert re.search(r"vr_verify_block[\s\S]{0,400}?return\s+kIOReturnNotPrivileged", body), (
        "readSectors no longer returns kIOReturnNotPrivileged on a failed "
        "vr_verify_block; a success return would then not mean the read-back "
        "was verified, and the emitter's gate would be decoration")


def test_the_record_is_emitted_once(code: str):
    """The once-flag is both tested and set, and set before the send.

    The gate keeps only the FIRST record per event, so a second one buys
    nothing; and the driver performs thousands of verified reads during a boot.
    Setting the flag *after* the call would let a re-entrant read emit a second
    record while the first is still in flight.
    """
    emission = code.find("native_console_report(read_back_record")
    assert emission != -1, "the record is never emitted"
    guard = code.rfind("if (", 0, emission)
    assert guard != -1, "the emission is not inside an if"
    body = _brace_body(code, re.escape("if ("), guard)
    assert body, "the emission's guard has no brace body"
    assert guard < emission < guard + len("if (") + len(body) + 2, (
        "the emission is not inside the if that precedes it")
    flat = "".join(body.split())
    assert "readBackReported=true;" in flat, (
        "the once-flag is not set inside the emission's own guard, so a second "
        "verified read emits a second record")
    # The record's own send, not any other call that happens to sit in the body.
    send = "native_console_report(read_back_record"
    assert send in flat, "the record is not sent inside its own guard"
    assert flat.index("readBackReported=true;") < flat.index(send), (
        "readBackReported is set after the send, so a re-entrant read can emit "
        "a second record while the first is still in flight")


def test_the_length_is_the_arrays_own_length_not_a_pointers(code: str):
    """``sizeof(read_back_record) - 1``, and the argument is the array itself.

    MEASURED 2026-11-01 on a real guest, and it is why this test exists.  Three
    XNU records arrived truncated to exactly seven bytes: the driver sent two by
    name and three through a ternary whose arms were arrays of different
    lengths, so the conditional operator decayed both to ``char const *`` and
    ``sizeof`` measured the pointer -- 8 on AArch64.  The build was green, the
    literals were byte-level in the kernel, and only the wire showed it.

    So the contract is stated as narrowly as the defect: the length must be
    ``sizeof(<plain array name>) - 1``.  That is the only thing that makes it
    the record's own length.
    """
    calls = _call_arguments(code, "native_console_report")
    record_calls = [(at, arg) for at, arg in calls if "read_back_record" in arg]
    assert record_calls, "native_console_report is never called with the record"

    for at, arg in record_calls:
        data, _, length = arg.partition(",")
        assert data.strip() == "read_back_record", (
            f"byte {at}: native_console_report is passed {data.strip()!r}, not "
            "the array itself.  Anything else -- an index, an offset, a "
            "ternary -- decays the array to a pointer.")
        wanted = "sizeof(read_back_record)-1"
        assert "".join(length.split()) == wanted, (
            f"byte {at}: the length is {length.strip()!r}, not {wanted}.  "
            "sizeof of a pointer is 8 on AArch64, so a length taken from one "
            f"sends {8 - 1} bytes of a {119}-byte record.")


# ---------------------------------------------------------------------------
# The reference to the console's report entry point
# ---------------------------------------------------------------------------

HEADER = REPO / "vendor" / "UniXNU" / "drivers" / "NativeVirtioConsole" / "NativeVirtioConsoleTransport.h"

#: The signature, whitespace-insensitive.  Written out rather than matched
#: loosely, because a prototype that disagrees with the definition is a
#: different return type and the caller compiles against the prototype.
REPORT_SIGNATURE = "IOReturnnative_console_report(constvoid*,size_t)"


def test_the_reference_is_strong_and_matches_the_header(code: str):
    """Strong ``extern``, and byte-for-byte the header's declaration.

    Two rules, each from a failed build in this repository:

    * NEVER ``weak_import``.  A weak reference does not oblige the linker to
      keep the definition, so the symbol the call site tests for can simply be
      absent and every record behind it silently never arrives -- measured on
      build 41330622788, where an ungated record did not arrive on a boot that
      ran to t=1530 s while five records from the same port on the same boot
      arrived complete.
    * Match the header exactly.  A kext cannot declare the same ``extern "C"``
      function twice with different linkage, and this driver's compile command
      carries no ``-I drivers/NativeVirtioConsole`` (build-arm-driver.py adds
      that only for NativeOfflineGPU), so the header cannot be included and the
      signature has to be written out.  Written out wrong, it is a different
      overload and the caller compiles against the wrong prototype.
    """
    declarations = list(re.finditer(
        r'extern\s+"C"\s+([A-Za-z_]\w*)\s+native_console_report\s*\(([^)]*)\)', code))
    assert declarations, (
        "native_console_report is not declared in the driver, so the emitter "
        "has no prototype to compile against")
    for m in declarations:
        return_type, params = m.group(1), m.group(2)
        # Everything up to the semicolon: a linkage attribute would sit there.
        tail = code[m.end():code.find(";", m.end())]
        assert "weak" not in tail, (
            f"native_console_report is declared with {tail.strip()!r}.  A weak "
            "reference resolves to null and silently drops every record behind "
            "it; the mount emitter's own note records that measurement.")
        normalized = "".join(
            (return_type + "native_console_report(" + params + ")").split())
        assert normalized == REPORT_SIGNATURE, (
            f"the declaration is {normalized!r}, want {REPORT_SIGNATURE!r}")

    require(HEADER)
    header = HEADER.read_text(encoding="utf-8")
    assert REPORT_SIGNATURE in "".join(header.split()), (
        "NativeVirtioConsoleTransport.h does not declare native_console_report "
        f"with {REPORT_SIGNATURE!r}; the driver's written-out signature has "
        "drifted from the definition the caller compiles against")


def test_nothing_stands_in_for_the_report_entry_point():
    """No weak stub answers this reference with a no-op.

    ``package/LinkStubs.c`` exists precisely to answer hooks from components a
    defconfig leaves out, and every definition in it is ``__attribute__((weak))``
    so a linked component replaces it.  A stub for ``native_console_report``
    would make the strong reference resolve to a function that puts nothing on
    the wire -- a record that never arrives with no link error to say so.
    """
    stubs = REPO / "vendor" / "UniXNU-qemu" / "package" / "LinkStubs.c"
    require(stubs)
    text = stubs.read_text(encoding="utf-8", errors="replace")
    assert "native_console_report" not in text, (
        "LinkStubs.c defines native_console_report, so the driver's strong "
        "reference can be answered by a no-op and the record would never "
        "arrive")


# ---------------------------------------------------------------------------
# Where the emission sits
# ---------------------------------------------------------------------------


def test_the_emission_is_on_the_read_path_not_the_start_path(code: str):
    """Emitted from doAsyncReadWrite, never from start() or readSectors().

    start() performs the same verified read -- readSectors(0, 512) before
    registerService() -- and the site was originally mapped there.  It is
    reachable, and that is the problem: start() runs during IOKit matching, and
    this driver's start() and NativeVirtioConsole::start() (which publishes the
    port the record needs) are siblings in the same device-tree matching
    cascade.  Nothing in either source says which runs first, and a record
    emitted before the console is up is not delayed -- it is dropped, because
    native_console_report() returns kIOReturnNotOpen while no console is live.

    doAsyncReadWrite() is invoked only by a client of the nub start()'s
    registerService() published, so it cannot run before start() has returned.
    The emission therefore does not depend on the two start()s' order.
    """
    emission = code.find("native_console_report(read_back_record")
    assert emission != -1, "the record is never emitted"
    for name, pattern in (
        ("start", r"bool\s+NativeVirtioBlock::start\s*\([^)]*\)\s*\{"),
        ("readSectors", r"IOReturn\s+NativeVirtioBlock::readSectors\s*\([^)]*\)\s*\{"),
        ("probe", r"IOService\s*\*\s*NativeVirtioBlock::probe\s*\([^)]*\)\s*\{"),
    ):
        hit = re.search(pattern, code)
        assert hit, f"{name} is not defined"
        body = _brace_body(code, pattern)
        assert "native_console_report" not in body, (
            f"the record is emitted from {name}(), which runs during IOKit "
            "matching -- before the console driver's start() may have published "
            "the port, and a record emitted then is dropped rather than delayed")


def test_the_emission_is_after_the_lock_is_released(code: str):
    """Outside ``IOLockLock(gate)``/``IOLockUnlock(gate)``.

    readSectors() runs under this driver's gate, and
    ``native_console_report() -> send()`` takes the console's own IOLock and
    polls the virtqueue with 2 s timeouts.  Emitting inside readSectors() would
    hold this driver's gate across that, serialising every block read behind one
    record.
    """
    emission = code.find("native_console_report(read_back_record")
    unlock = code.rfind("IOLockUnlock(gate)", 0, emission)
    assert unlock != -1 and unlock < emission, (
        "the emission is not after an IOLockUnlock(gate), so it holds this "
        "driver's gate across the console's send()")
    lock = code.rfind("IOLockLock(gate)", 0, unlock)
    assert lock != -1, "the IOLockUnlock found is not this driver's"
    assert not (lock < emission < unlock), (
        "the emission sits between IOLockLock(gate) and IOLockUnlock(gate)")


def test_the_emission_is_inside_the_verification_guard(code: str):
    """Both the reference and the emission are behind NATIVE_VERIFIED_BLOCK.

    The read-back proof exists only in verified mode: without it, readSectors()
    is readRawSectors() and nothing compares the bytes against the manifest.  A
    build without the flag must not reference the console at all, or it gains a
    link dependency on an archive it may not have.
    """
    reference = code.find('extern "C" IOReturn native_console_report')
    assert reference != -1, "the reference is gone"
    emission = code.find("native_console_report(read_back_record")
    assert emission != -1, "the emission is gone"
    guard = "#if defined(NATIVE_VERIFIED_BLOCK) && NATIVE_VERIFIED_BLOCK"
    for at, what in ((reference, "the reference"), (emission, "the emission")):
        opened = code.rfind(guard, 0, at)
        assert opened != -1, f"{what} is not inside a NATIVE_VERIFIED_BLOCK guard"
        closed = code.find("#endif", opened)
        assert closed != -1 and closed > at, (
            f"{what} is not closed by the guard's #endif")


# ---------------------------------------------------------------------------
# The gate still reads what the driver emits
# ---------------------------------------------------------------------------


def test_the_gate_names_the_event_the_driver_emits():
    """milestones.py's probe names exactly this event.

    The direction matters.  Checking that the driver's event name appears in
    milestones.py is true whether or not the driver emits it, so it cannot catch
    a rename -- and a renamed event is reported missing, which reads exactly
    like a driver that never ran.  So the driver is the source of truth: pull
    the name out of its record and require the gate to name it.
    """
    milestones = REPO / "src" / "unipad" / "acceptance" / "milestones.py"
    require(milestones)
    gate = milestones.read_text(encoding="utf-8", errors="replace")
    assert f'"{READ_BACK}"' in gate, (
        f"no probe in milestones.py names {READ_BACK!r}; the record can never "
        "satisfy anything")
    # And the veto this record has to keep dormant must still be there, or the
    # anti-veto test above is guarding a predicate that no longer exists.
    assert "_device_enumeration_without_readback" in gate, (
        "the veto predicate is gone from milestones.py; the anti-veto test in "
        "this file would then be guarding nothing")


def test_the_veto_excludes_the_read_back_and_only_it():
    """The veto's own carve-out is the one event this driver emits.

    ``_device_enumeration_without_readback`` ignores ``virtio_blk.read_back``
    and nothing else.  If that carve-out were widened, a second event would stop
    arming the veto and the anti-veto test's premise would quietly evaporate.
    """
    milestones = REPO / "src" / "unipad" / "acceptance" / "milestones.py"
    require(milestones)
    gate = milestones.read_text(encoding="utf-8", errors="replace")
    assert f'event != "{READ_BACK}"' in gate, (
        f"the veto no longer carves out exactly {READ_BACK!r}; the anti-veto "
        "property this file guards is no longer the one the gate enforces")


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


def test_the_pin_carries_the_emitter():
    """The pinned checkout has the emitter, once the pin is moved.

    ``vendor/`` is git-excluded, so this skips on a clean checkout and fails
    where the pin is present but does not carry the change -- which is the
    honest answer until the main agent applies it and moves the gitlink.  It is
    what stops the pending copy under ``vendor-patches/`` from going stale: a
    pin that loses the emitter turns this red rather than leaving the guard
    reading a file nobody builds.
    """
    if not PINNED.is_file():
        pytest.skip(
            f"{PINNED.relative_to(REPO).as_posix()} is not present.  vendor/ is "
            "git-excluded, so a clean checkout has no pinned tree to check.")
    text = PINNED.read_text(encoding="utf-8", errors="replace")
    for anchor in (
        'extern "C" IOReturn native_console_report(const void *, size_t);',
        "read_back_record",
        "readBackReported",
    ):
        assert anchor in text, (
            f"the pinned NativeVirtioBlock.cpp does not contain {anchor!r}.  "
            "The change under vendor-patches/ has not been applied to the pin, "
            "so no kernel built from it emits the record.")
