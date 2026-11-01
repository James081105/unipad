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
    """
    milestones = REPO / "src" / "unipad" / "acceptance" / "milestones.py"
    require(milestones)
    gate = milestones.read_text(encoding="utf-8", errors="replace")

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

    for event in sorted(emitted):
        assert f'"{event}"' in gate, (
            f"the driver emits {event} but no probe in milestones.py names it; "
            f"the record can never satisfy anything")
