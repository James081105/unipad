"""Every record the console driver emits must be one line of valid JSON.

Why this exists
---------------
The acceptance gate reads guest records with ``_guest_records``
(``src/unipad/acceptance/milestones.py``): it tries ``json.loads`` on each line,
and when that fails it falls back to splitting the line on the first ``:``.  A
record that is neither is SKIPPED, and a skipped record is indistinguishable
from an absent one.  ``_guest_events`` then keeps only the FIRST record for a
given event (``setdefault``), so a record split across two lines is invisible
even if both halves parse.

That is a silent failure mode with a long feedback loop: the driver compiles
with ``-Werror`` into a kernel, the kernel is linked on a macOS runner, the
boot-inputs bundle is built, downloaded (310 MB, and GitHub's pre-signed URL
expires mid-transfer) and booted before anybody sees a byte.  A malformed
record therefore costs a full build-and-boot cycle to discover, and produces no
error when it does -- just a missing event.

Worse, the specific way these records are built is the part most likely to be
wrong.  They are assembled by the C preprocessor's ADJACENT STRING LITERAL
concatenation, and C concatenates only string literals: ``"a" true "b"`` is a
compile error, not a value.  Two earlier drafts of this exact macro produced
that, and neither would have been caught here, because this host has no C
compiler at all -- CI is the first thing that compiles it.

So this test simulates the preprocessor on the real source and checks the
result, which is the only way to get that answer in under a second.

What it checks
--------------
* every record the driver emits is exactly one line of JSON, ending in one
  newline and containing no other;
* the ``event`` field is the one the gate's probe names;
* every boolean field carries a real JSON boolean, not a string, not a number,
  and not an empty hole;
* no expanded macro body leaves a bare token between two string literals, which
  is the compile error above.

The register reads themselves are NOT checked here.  They cannot be: this test
does not run on the guest.  What it checks is that whatever the registers say
reaches the wire as a well-formed record -- which is the part a build can
verify.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.vendor_paths import require

REPO = Path(__file__).resolve().parents[2]
#: Same file ``build-arm-driver.py`` archives, and the same reason this guard
#: reads it out of ``vendor/UniXNU``: it is the UniXNU pin's
#: ``drivers/NativeVirtioConsole`` submodule, not the device repository.
DRIVER = (
    REPO / "vendor" / "UniXNU" / "drivers" / "NativeVirtioConsole" / "NativeVirtioConsole.cpp"
)

#: The escape scanner.  Built from chr() rather than written as ``"\\"`` so that
#: nothing can collapse it: a bash heredoc turns the two characters backslash-n
#: into a real newline, and that mangled this verifier's ancestor twice.
BACKSLASH = chr(92)
NEWLINE = chr(10)
TAB = chr(9)
ESCAPES = {"n": NEWLINE, "t": TAB, "r": chr(13), "0": chr(0)}

#: The records the gate reads, and the boolean fields each must carry.
#: ``iokit.*`` clears M5's own probes; ``xnu.*`` clears the ``xnu_initialised``
#: blocker (``_xnu_initialisation`` needs all three).
RECORDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("iokit.service_match", "match_record", ()),
    ("iokit.driver_probe", "probe_record", ()),
    ("xnu.page_tables", "pt_on", ("mmu_enabled",)),
    ("xnu.page_tables", "pt_off", ("mmu_enabled",)),
    ("xnu.exception_vectors", "ev_on", ("vectors_installed",)),
    ("xnu.exception_vectors", "ev_off", ("vectors_installed",)),
    ("xnu.interrupts", "ir_tt", ("irqs_unmasked", "timer_configured")),
    ("xnu.interrupts", "ir_tf", ("irqs_unmasked", "timer_configured")),
    ("xnu.interrupts", "ir_ft", ("irqs_unmasked", "timer_configured")),
    ("xnu.interrupts", "ir_ff", ("irqs_unmasked", "timer_configured")),
    # Build 41369829766's isolating experiment.  These are NOT requirements and
    # no probe names them; they exist to answer one binary question -- whether
    # native_console_report(), the free function every kernel-side emitter in
    # this project routes through, delivers at all.  They are listed here so the
    # same one-line-valid-JSON and sizeof-of-an-array machinery covers them, and
    # they come out of both this list and the driver when the measurement lands.
    ("diag.free_fn_from_driver", "free_fn_record", ()),
    ("diag.control_from_driver", "control_record", ()),
    # Round two of the same experiment.  Round one established the FACT and not
    # the cause: the free function's record did not arrive on build 41390547018's
    # matched kernel while the control record on the next line did.  A record
    # that reports only absence cannot say WHY, and three causes have different
    # remedies -- g_console null (fix the publication), send() reporting success
    # with the record still lost (fix the transport, not the entry point), or a
    # third failure nothing has named yet.  So the status is emitted as its own
    # record, selected by an if-chain on the return value.
    ("diag.free_fn_status", "free_fn_ok", ()),
    ("diag.free_fn_status", "free_fn_notopen", ()),
    ("diag.free_fn_status", "free_fn_other", ()),
)


def _literals(fragment: str) -> list[str]:
    """Every C string literal in ``fragment``, with escapes resolved.

    This is the preprocessor's adjacent-literal concatenation, minus the
    concatenation: the caller joins the result.
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


def _bare_outside_literals(fragment: str) -> str:
    """Characters that sit outside every string literal, whitespace dropped.

    A regex cannot answer this: inside a literal, ``\\"`` is a backslash then a
    quote, and the backslash looks bare to ``[^ \\t"]``.  Walking the same way
    ``_literals`` does is the only correct way to ask.
    """
    out: list[str] = []
    i, n = 0, len(fragment)
    while i < n:
        if fragment[i] == '"':
            i += 1
            while i < n and fragment[i] != '"':
                i += 2 if (fragment[i] == BACKSLASH and i + 1 < n) else 1
            i += 1
            continue
        if fragment[i] not in (" ", TAB):
            out.append(fragment[i])
        i += 1
    return "".join(out)


@pytest.fixture(scope="module")
def source() -> str:
    require(DRIVER)
    return DRIVER.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def macros(source: str) -> tuple[str, str]:
    """The two shared macro bodies the records are built from."""
    src = re.search(r"#define\s+NATIVE_CONSOLE_SRC\s+(.+)", source)
    irq = re.search(r"#define\s+NATIVE_CONSOLE_IRQ\s*\(a,b\)\s+(.+)", source)
    assert src, "NATIVE_CONSOLE_SRC is not defined in the driver"
    assert irq, "NATIVE_CONSOLE_IRQ is not defined in the driver"
    return src.group(1), irq.group(1)


def _expand_plain(source: str, name: str, src_macro: str) -> str:
    """Expand ``static char const <name>[] = ...;``."""
    m = re.search(r"static\s+char\s+const\s+" + name + r"\s*\[\s*\]\s*=\s*(.+?);",
                  source, re.S)
    assert m, f"{name} is not declared in the driver"
    return "".join(_literals(m.group(1).replace("NATIVE_CONSOLE_SRC", src_macro)))


#: One instantiation of the interrupt macro, in either spelling.  The quoted
#: form is the correct one; the unquoted form is what a wrong macro produces,
#: and this guard has to be able to read BOTH or it fails on "not declared"
#: instead of on the contract it owns.
_IRQ_CALL = (r"static\s+char\s+const\s+(?P<name>\w+)\s*\[\s*\]\s*=\s*"
             r'NATIVE_CONSOLE_IRQ\s*\(\s*"?(?P<a>\w+)"?\s*,\s*"?(?P<b>\w+)"?\s*\)\s*;')


def _irq_arguments(source: str, name: str) -> tuple[str, str] | None:
    """The two arguments one interrupt combination passes, quoted or not.

    Returns ``None`` when the declaration is absent, so the caller can report
    that rather than crash -- but an ABSENT declaration and a MALFORMED one are
    different failures and must not be conflated.
    """
    for m in re.finditer(_IRQ_CALL, source):
        if m.group("name") == name:
            return m.group("a"), m.group("b")
    return None


def _expand_irq(source: str, name: str, irq_macro: str, src_macro: str) -> str:
    """Expand one of the four interrupt combinations.

    The macro's parameters are STRING LITERALS: the call site passes
    ``"true"``, not ``true``.  With a bare identifier the expansion leaves it
    between two literals, which C refuses to concatenate.
    """
    args = _irq_arguments(source, name)
    assert args is not None, f"{name} is not declared as a NATIVE_CONSOLE_IRQ instantiation"
    a, b = args
    body = irq_macro.replace(" a ", f' "{a}" ').replace(" b ", f' "{b}" ')
    body = body.replace("NATIVE_CONSOLE_SRC", src_macro)
    return "".join(_literals(body))


def _strip_comments(source: str) -> str:
    """The source with ``//`` and ``/* */`` comments blanked out, in place.

    A call site inside a comment is not a call site.  This is not pedantry: the
    driver's fix for the defect below quotes the OLD broken line in a comment so
    the next reader knows what it replaced, and a scan that does not strip
    comments reports that quoted line as a live defect -- the test goes red on
    its own documentation, which is the same as no test at all.

    Literal-aware, because ``"//"`` inside a string is not a comment and the
    records here are made of quotes and escapes.
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

    A regex cannot do this.  The arguments contain nested parentheses and
    commas, so ``[^)]*`` stops at the first inner close paren and silently
    reports a shorter argument than the one the compiler sees -- which is how a
    check ends up green on the code it was written to catch.
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


def _record_lengths(source: str, macros: tuple[str, str]) -> dict[str, int]:
    """Record array name -> the number of bytes its literal occupies.

    This is what ``sizeof(arr) - 1`` evaluates to for a ``char const[]``
    initialised from string literals: the array is one byte longer than the
    text, for the NUL.
    """
    src_macro, irq_macro = macros
    out: dict[str, int] = {}
    for _event, name, _fields in RECORDS:
        if name.startswith("ir_"):
            out[name] = len(_expand_irq(source, name, irq_macro, src_macro))
        else:
            out[name] = len(_expand_plain(source, name, src_macro))
    return out


#: ``sizeof`` on an expression the arrays have decayed through.  On AArch64 a
#: pointer is 8 bytes, so ``sizeof(arr) - 1`` becomes 7 -- and a record sent
#: with length 7 is the seven bytes ``{"event``.
POINTER_BYTES = 8


def test_no_record_is_sent_with_a_length_a_pointer_would_give(source, macros):
    """No record may be sent with a length ``sizeof`` took from a pointer.

    MEASURED 2026-11-01 on a real guest, and it is why this test exists.  The
    two IOKit records arrived complete, but all three XNU records arrived
    truncated to exactly seven bytes: the capture ends with
    ``b'{"event{"event{"event'``.  The driver sends the first two by name and
    the last three through a ternary::

        send(match_record, sizeof(match_record)-1);          // an array  -> right
        NATIVE_CONSOLE_EMIT(NATIVE_CONSOLE_PICK(pt_on,pt_off,mmu_on));

    ``NATIVE_CONSOLE_PICK`` is ``((flag)?(on):(off))``, and the two arms are
    arrays of DIFFERENT lengths (``true`` is four characters, ``false`` five),
    so the conditional operator decays both to ``char const *``.  The result is
    a pointer, and ``sizeof`` of a pointer is 8 on AArch64 -- not the length of
    either record.  Every one of the three records was therefore sent seven
    bytes long.

    A byte-level check on the kernel cannot see this.  The full literals ARE in
    the kernel; only the length argument is wrong, and it is computed at run
    time.  So this is checked on the source, the same way the adjacent-literal
    concatenation is.

    The contract is stated as narrowly as the defect: ``sizeof`` must be
    applied to a plain array name.  That is what makes ``sizeof(arr)-1`` the
    record's own length, and it is the only thing that does.
    """
    lengths = _record_lengths(source, macros)
    assert lengths, "no record arrays found in the driver"

    # Scan the comment-free text.  The fix for this very defect quotes the old
    # broken line in a comment, and a scan over the raw source reports that
    # quotation as a live defect.
    code = _strip_comments(source)

    emit = re.search(r"#define\s+NATIVE_CONSOLE_EMIT\s*\(\s*arr\s*\)\s+(.+)",
                     source)
    assert emit, "NATIVE_CONSOLE_EMIT is not defined in the driver"
    body = emit.group(1).strip()
    assert body == "send(arr,sizeof(arr)-1)", (
        f"the emit macro is {body!r}.  This test models the length sent as "
        f"sizeof(arr)-1 and cannot reason about any other shape, so it must "
        f"fail loudly here rather than model it wrongly.")

    problems: list[str] = []

    for at, arg in _call_arguments(code, "NATIVE_CONSOLE_EMIT"):
        # The macro's own definition line is not a call site.  Skipping it by
        # line rather than by offset matters: without this the test reports
        # "is passed 'arr'" and goes red on its own regex, which is the same as
        # no test at all.
        line_start = source.rfind(NEWLINE, 0, at) + 1
        if source[line_start:at].lstrip().startswith("#define"):
            continue
        ident = arg.strip()
        if re.fullmatch(r"[A-Za-z_]\w*", ident):
            if ident not in lengths:
                problems.append(
                    f"byte {at}: NATIVE_CONSOLE_EMIT is passed {ident!r}, "
                    f"which is not one of the record arrays "
                    f"{sorted(lengths)}")
            continue
        # Not a plain name, so it decays: sizeof gives the pointer size.
        named = [n for n in sorted(lengths) if n in ident]
        wanted = " or ".join(f"{n} ({lengths[n]} bytes)" for n in named)
        problems.append(
            f"byte {at}: NATIVE_CONSOLE_EMIT is passed {ident!r}.  That is "
            f"not an array name, so the arrays in it decay to "
            f"'char const *' and sizeof(arr) is {POINTER_BYTES}, not the "
            f"record's length -- so send() is given {POINTER_BYTES - 1} bytes "
            f"and the record arrives as the first "
            f"{POINTER_BYTES - 1} characters of itself.  It should select "
            f"between {wanted} OUTSIDE the emit, so that sizeof still sees an "
            f"array.")

    for at, arg in _call_arguments(code, "send"):
        line_start = source.rfind(NEWLINE, 0, at) + 1
        if source[line_start:at].lstrip().startswith("#define"):
            continue  # the macro's own body, not a call site
        head, _, tail = arg.partition(",")
        data = head.strip()
        if data not in lengths:
            continue  # not a record; the emitter sends other things too
        want = f"sizeof({data})-1"
        if tail.strip() != want:
            problems.append(
                f"byte {at}: send({data}, {tail.strip()}) -- the length is "
                f"not {want}, so it is not the record's own length")

    assert not problems, (
        "records that would be sent with a length a pointer gives, not the "
        "record's length:" + NEWLINE + NEWLINE.join(problems))


@pytest.mark.parametrize("event,name,_fields", RECORDS,
                         ids=[r[1] for r in RECORDS])
def test_record_is_one_line_of_valid_json(source, macros, event, name, _fields):
    src_macro, irq_macro = macros
    if name.startswith(("ir_",)):
        record = _expand_irq(source, name, irq_macro, src_macro)
    else:
        record = _expand_plain(source, name, src_macro)

    # Exactly one line.  A record split across lines is invisible to the gate
    # even when both halves parse, because _guest_events keeps the first record
    # per event and a partial line is not a record.
    assert record.count(NEWLINE) == 1, (
        f"{name} is {record.count(NEWLINE)} lines, not 1: {record!r}")
    assert record.endswith(NEWLINE), f"{name} does not end in a newline"

    try:
        parsed = json.loads(record.strip())
    except json.JSONDecodeError as exc:
        pytest.fail(f"{name} is not valid JSON ({exc}): {record!r}")

    assert isinstance(parsed, dict), f"{name} is {type(parsed).__name__}, not an object"
    assert parsed.get("event") == event, (
        f"{name} carries event {parsed.get('event')!r}, want {event!r}")


@pytest.mark.parametrize("event,name,fields", RECORDS,
                         ids=[r[1] for r in RECORDS])
def test_boolean_fields_are_real_booleans(source, macros, event, name, fields):
    """A boolean that is a string, a number or a hole is a false statement.

    The earlier draft that wrote ``"vbar_el1":0`` while holding the real value
    in a variable is the reason this exists: the record would have parsed, the
    gate would have accepted it, and it would have been wrong.
    """
    src_macro, irq_macro = macros
    if name.startswith(("ir_",)):
        record = _expand_irq(source, name, irq_macro, src_macro)
    else:
        record = _expand_plain(source, name, src_macro)
    parsed = json.loads(record.strip())

    for field in fields:
        assert field in parsed, f"{name} is missing {field}: {record!r}"
        value = parsed[field]
        assert isinstance(value, bool), (
            f"{name}.{field} is {type(value).__name__} ({value!r}), not a bool")


def test_interrupt_record_carries_both_booleans(source, macros):
    """The interrupt record must carry both facts, not one.

    Emitting them as two records would be simpler, and would be wrong: the gate
    keeps only the FIRST record per event, so the second would be dropped and a
    reader of the log would never see the timer.
    """
    src_macro, irq_macro = macros
    for name in ("ir_tt", "ir_tf", "ir_ft", "ir_ff"):
        record = _expand_irq(source, name, irq_macro, src_macro)
        parsed = json.loads(record.strip())
        assert {"irqs_unmasked", "timer_configured"} <= set(parsed), (
            f"{name} carries only {sorted(parsed)}")


def test_no_expansion_leaves_a_bare_token(source, macros):
    """No expanded body may leave a bare token between two string literals.

    ``":" true ","`` is a compile error, not a value, and it is the failure mode
    this macro is most likely to produce.  CI is the first thing that compiles
    the driver, so without this check the error costs a 40-minute build to find
    and this host has no compiler to find it sooner.

    This test reads the call sites in EITHER spelling on purpose.  A version
    that matched only the quoted form failed with "ir_tt is not declared" when
    the macro was mutated to the unquoted form -- it went red on its own regex
    instead of on the contract, which is the same as no test at all.
    """
    src_macro, irq_macro = macros
    for name in ("ir_tt", "ir_tf", "ir_ft", "ir_ff"):
        args = _irq_arguments(source, name)
        assert args is not None, f"{name} is not declared"
        body = irq_macro.replace(" a ", f' "{args[0]}" ').replace(
            " b ", f' "{args[1]}" ').replace("NATIVE_CONSOLE_SRC", src_macro)
        bare = _bare_outside_literals(body)
        assert not bare, (
            f"{name} expands to a bare token {bare!r} between two string "
            f"literals, which C will not concatenate")


def test_the_gate_still_reads_what_the_driver_emits(source):
    """Every event the driver emits must be one the gate's probes name.

    The direction matters.  Checking that the gate's event names appear in
    ``milestones.py`` is true whether or not the driver emits them, so it cannot
    catch a rename in the driver -- and a renamed event is reported missing,
    which reads exactly like a driver that never ran.

    So the driver is the source of truth here: pull the event names out of its
    own records and require the gate to name each one.

    The one exception is a record that exists to diagnose rather than to satisfy
    a requirement, and it has to be named explicitly rather than inferred: build
    41369829766 measured that ``native_console_report()`` called from kern_exec.c
    delivered nothing while ``send()`` called from inside this class delivered
    five records, and the two diagnostic records below isolate which of those two
    variables is the cause.  A diagnostic is temporary by construction, so it is
    listed here and removed here when the measurement it exists for is taken.
    Inferring "this one is only a diagnostic" from the name would let any
    ungated record exempt itself.
    """
    milestones = REPO / "src" / "unipad" / "acceptance" / "milestones.py"
    require(milestones)
    gate = milestones.read_text(encoding="utf-8", errors="replace")

    #: Events the driver emits to measure the port, not to satisfy a requirement.
    #: Each must carry the reason it exists; an empty set is the normal state.
    DIAGNOSTIC_EVENTS = {
        "diag.free_fn_from_driver",
        "diag.control_from_driver",
        "diag.free_fn_status",
    }

    emitted: set[str] = set()
    # The C source spells the quotes inside a string literal as backslash-quote,
    # so the pattern needs the backslash escaped FOR THE REGEX, not merely
    # present.  ``\\"`` in a pattern means a literal backslash then a quote;
    # ``\"`` means only a quote, and a pattern built that way matches nothing
    # here while looking perfectly correct.
    needle = re.escape(BACKSLASH + '"event' + BACKSLASH + '":'
                       + BACKSLASH + '"') + r"([a-z0-9_.]+)" + re.escape(
                           BACKSLASH + '"')
    for m in re.finditer(needle, source):
        emitted.add(m.group(1))
    assert emitted, "no event names found in the driver's records"

    unexplained = sorted(emitted - DIAGNOSTIC_EVENTS)
    for event in unexplained:
        assert f'"{event}"' in gate, (
            f"the driver emits {event} but no probe in milestones.py names it; "
            f"the record can never satisfy anything")
    # And the allowlist must not become a place to park a real requirement: a
    # diagnostic the gate actually names is a mislabelled requirement.
    for event in sorted(DIAGNOSTIC_EVENTS & emitted):
        assert f'"{event}"' not in gate, (
            f"{event} is listed as a diagnostic but milestones.py names it; "
            f"it is a requirement, not a diagnostic")


# ---------------------------------------------------------------------------
# The report port: a second way on, for code that runs after IOKit matching
# ---------------------------------------------------------------------------

HEADER = DRIVER.parent / "NativeVirtioConsoleTransport.h"

#: The definition's signature, whitespace-insensitive.  Written out rather than
#: matched loosely, because a prototype that disagrees with the definition is a
#: different return type and the caller compiles against the prototype.
REPORT_SIGNATURE = "IOReturnnative_console_report(constvoid*,size_t)"


def _report_body(source: str) -> str:
    """The brace-balanced body of native_console_report()'s DEFINITION.

    ``_call_arguments`` gives the text between the parentheses, which for a
    definition is only the parameter list -- so every assertion about what the
    function DOES has to be made against the braces that follow.  An earlier
    revision of these tests read the parameter list and then asserted
    ``kIOReturnNotOpen`` was in it, which passed for the wrong reason and would
    have passed just as well with the body deleted.
    """
    body = _strip_comments(source)
    hit = re.search(
        r"native_console_report\s*\(\s*const\s+void\s*\*\s*bytes\s*,"
        r"\s*size_t\s+size\s*\)\s*\{", body)
    assert hit is not None, "native_console_report is not defined with this signature"
    depth, i = 1, hit.end()
    while i < len(body) and depth:
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "native_console_report's body is not balanced"
    return body[hit.end():i - 1]


def test_the_report_path_is_a_friend_of_the_class_whose_member_it_calls(source):
    """A free function calling a protected member must be declared a friend.

    send() is protected.  native_console_report() is a free function, so
    reaching it needs a friend declaration inside the class, exactly as
    native_console_send() and native_console_receive() have.

    This is not a style rule and it is not hypothetical.  Run 41313323081
    failed archive 6/11 with

        NativeVirtioConsole.cpp:119:33: error: 'send' is a protected member of
        'NativeVirtioConsole'

    and every source-level guard in this file passed on the commit that caused
    it, because they read the function's SHAPE -- its name, its signature, its
    guard, its ordering -- and none of them read the class's access control.
    The five assertions about this entry point were all green while the driver
    did not compile.

    So the assertion is on the class, not the function: for every free function
    this file guards that calls a protected member, the class must name it as a
    friend.
    """
    body = _strip_comments(source)
    # The BODY, not the parameter list: _call_arguments gives the text between
    # the parentheses, which for a definition is the signature.  The first
    # revision of this test made exactly that mistake and then found no member
    # call to guard.
    definition = _report_body(body)
    assert definition is not None, "native_console_report is not defined"

    # Which protected members does the report path actually call?  Only the
    # ones it names, so a friend grant is required only for those.
    protected = set(re.findall(
        r"^\s*(?:IOReturn|bool|void|IOMemoryMap|IOLock|IOService)\s+"
        r"([A-Za-z_]\w*)\s*\(", body, re.M))
    called = {name for name in protected if re.search(
        r"g_console\s*(?:->|\.)\s*" + re.escape(name) + r"\b", definition)}
    assert called, (
        "the report path calls no member of the console, so this test has "
        "nothing to guard; check whether native_console_report was rewritten"
    )

    friends = set(re.findall(
        r"^\s*friend\s+((?:IOReturn|bool|void)\s+\w+\s*\([^)]*\)\s*);", body, re.M))
    normalized = {"".join(f.split()) for f in friends}
    assert REPORT_SIGNATURE in normalized, (
        f"native_console_report calls the protected member(s) "
        f"{sorted(called)} but the class does not declare it a friend with "
        f"this signature. Found: {sorted(normalized)}. A friend naming a "
        "different parameter list declares a different overload and grants "
        "nothing, so the driver does not compile -- measured on run "
        "41313323081, archive 6/11, with 'send' is a protected member"
    )


def test_the_report_entry_point_is_declared_for_its_callers(source):
    """Declared in the transport header, not only in the .cpp.

    The caller is an XNU patch in bsd/kern/kern_exec.c, compiled as C against a
    force-included header.  A declaration that lives only in the .cpp is one the
    caller cannot see, and the patch would declare its own -- which is how the
    return type drifts.
    """
    require(HEADER)
    header = HEADER.read_text(encoding="utf-8")
    assert "native_console_report" in header, (
        "NativeVirtioConsoleTransport.h does not declare native_console_report; "
        "its caller in bsd/kern/kern_exec.c has no prototype to compile against"
    )
    # Whitespace removed from both sides: the declaration's spacing is the
    # author's, and a signature check that depends on it fails on a reformat.
    assert REPORT_SIGNATURE in "".join(header.split()), (
        "the header's declaration does not match the definition's signature "
        f"({REPORT_SIGNATURE}); the patch's caller compiles against the header"
    )


def test_the_report_path_needs_no_open_client(source):
    """No isOpen() on the report path.

    native_console_send() refuses while isOpen(owner) is false and nothing on
    this board opens the port, so a report path that checked isOpen would be a
    second copy of the function that already could not serve the mount.
    """
    report = _report_body(source)
    assert "isOpen" not in report, (
        "the report path checks isOpen(); nothing on this board opens the port, "
        "so the SystemCryptex mount could never report through it"
    )


def test_the_report_refuses_rather_than_dereferencing(source):
    """kIOReturnNotOpen when there is no live console.

    A caller that runs before start() or after cleanup() must be told, not
    silently dropped and not dereferenced.
    """
    report = _report_body(source)
    assert "kIOReturnNotOpen" in report, (
        "the report path does not return kIOReturnNotOpen when g_console is null"
    )
    assert "g_console?" in report.replace(" ", ""), (
        "the report path does not test g_console before sending through it"
    )
    assert "send(" in report, (
        "the report path does not put the bytes on the port at all"
    )


def test_the_port_is_published_only_after_the_port_is_really_there(source):
    """g_console=this comes after native_console_ready=1 in start().

    configure() allocates both queue buffers, publishes both rings and sets
    online.  Publishing the port before that hands a caller a port whose queue
    does not exist, and send() would lock a null IOLock and panic.
    """
    body = _strip_comments(source)
    ready = body.find("native_console_ready=1")
    publish = body.find("g_console=this")
    assert ready != -1, "start() no longer sets native_console_ready"
    assert publish != -1, "start() no longer publishes g_console"
    assert publish > ready, (
        "g_console is published before native_console_ready is set; a caller "
        "could reach send() before configure() allocated the queue buffers"
    )
    # And it must be inside start(), not at file scope where it would run
    # before probe() has matched anything.
    assert body.find("bool NativeVirtioConsole::start") < publish, (
        "g_console is published outside start()"
    )


def test_the_port_is_withdrawn_when_the_device_is_stopped(source):
    """``g_console`` is withdrawn in ``cleanup()``, and only by its publisher.

    THREE conditions, and each one was a defect or a near-miss:

    1. **Only when the port was stopped.** ``reset()`` reports that, and the
       withdrawal must come after it -- a failed reset leaves the device
       reachable and the pointer must survive it.  The pointer is written under
       the same ``IOLock`` ``send()`` takes, which is what makes the teardown
       race safe: a reporter that read a non-null ``g_console`` just before
       ``cleanup()`` blocks on the lock, and by the time it gets in, ``online``
       is false and ``send()`` refuses without touching freed memory.

    2. **Only by the instance that published it.** ``reset()`` returns true for
       an instance that never started -- ``if(!registers || !ownsDevice)return
       true`` -- so an unguarded withdrawal lets ANY instance's teardown
       unpublish a different, live instance's port.

    3. **Exactly one withdrawal.** The declaration also initialises the pointer,
       and counting it as a withdrawal would make this assertion require zero
       -- i.e. that the port is never published at all.

    Condition 2 is measured, not defensive.  Build 41400938438's capture carried
    ``diag.free_fn_status`` as ``"not_open"`` while the control record on the
    next line arrived -- from inside ``start()``, with ``g_console=this``
    published 143 lines earlier and nothing between the two that assigns it.
    ``g_console`` had been cleared by another instance's teardown, and
    ``native_console_report()`` -- the entry point every kernel-side emitter in
    this project routes through -- had been dropping records silently for
    exactly that reason, on every boot, since the port was first published.
    """
    body = _strip_comments(source)
    cleanup = body.find("void NativeVirtioConsole::cleanup")
    assert cleanup != -1, "cleanup() is gone"
    stop = body.find("bool stopped=reset()", cleanup)
    assert stop != -1, "cleanup() no longer calls reset()"
    withdraw = body.find("g_console = nullptr", cleanup)
    assert withdraw != -1, (
        "cleanup() no longer withdraws g_console.  The pointer is written under "
        "the same IOLock send() takes, which is what makes the teardown race "
        "safe; without the withdrawal a stopped driver leaves a pointer to "
        "freed queue buffers."
    )
    assert withdraw > stop, (
        "g_console is withdrawn before reset() reports whether the port was "
        "stopped; a failed reset leaves the device reachable and the pointer "
        "must survive it"
    )
    guard = body.rfind("g_console == this", 0, withdraw)
    assert guard != -1 and guard > stop, (
        "the withdrawal is not guarded by `g_console == this`, so ANY "
        "instance's cleanup() unpublishes the port -- including an instance "
        "that never started, because reset() returns true when there are no "
        "registers and no device.  Measured as build 41400938438's "
        "diag.free_fn_status == \"not_open\"."
    )
    # The declaration initialises the pointer too, and counting it as a
    # withdrawal would make this assertion require zero -- i.e. that the port is
    # never published at all.  Remove the declaration, then count.
    without_declaration = body.replace(
        "static NativeVirtioConsole *g_console=nullptr;", "")
    assert without_declaration.count("g_console = nullptr") == 1, (
        "g_console is withdrawn in more than one place"
    )
