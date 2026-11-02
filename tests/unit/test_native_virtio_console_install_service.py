"""The install dispatcher's one record, and the couplings that keep it honest.

Why this exists
---------------
``guest_install_service`` is named by FOUR milestones -- M10, M10a, M11 and M12
(``src/unipad/acceptance/milestones.py:2649``, ``:2698``, ``:2753``, ``:2804``)
-- and it is cleared by exactly one onsite guest event, ``install.registered``
(``:1636-1638``).  Until now nothing in the guest emitted it: the string occurs
zero times, measured byte-level, in the kernel the guest boots.

That is a lot of leverage riding on one record, and the record is built the way
this board's other guest records are built -- by the C preprocessor's ADJACENT
STRING LITERAL concatenation, in a kernel patch, compiled on a macOS runner
nobody here can reach.  Two drafts of the mount emitter carried the literal
``system.mount`` and produced a record that parsed as nothing; both compiled
clean.  A malformed record therefore costs a full build-and-boot cycle to
discover and produces no error when it does -- just a missing event, which reads
exactly like a guest that never did the thing.

So the checks that CAN be made here are made here, on the source, in under a
second.  What is checked is what a build can verify: the record's shape, the
event's name, the couplings that make the record TRUE, and the two invariants
that are easy to break and invisible until a boot.

What is NOT checked, and cannot be
-----------------------------------
* that a host write reaches the guest's RX virtqueue at all.  That is the one
  load-bearing unknown; fact 218's counters count pulls by a user client and
  none exists, and the arrival round read the wrong ring offsets.  Nothing in
  the repository measures it correctly.
* that the kernel compiles.  This host has no C compiler; CI is the first thing
  that compiles the driver and the patch.
* that the record arrives on a real boot.

Those three are recorded as UNVERIFIED in the commit that adds this file, not
papered over here.

The two invariants worth naming, because both are one edit away from silent
-------------------------------------------------------------------------
1. **The dispatcher and the GPU broker cannot share the RX byte stream.**
   ``NativeOfflineGPU::readExact()``
   (``vendor/UniXNU/drivers/NativeMetal/NativeOfflineGPU.cpp:34-44``) drains RX
   in a lock-step loop and treats ANY short read as ``kIOReturnIOError``;
   ``exchange()`` then sets ``online=false`` at ``:75``, permanently.  A second
   consumer's line buffering would take bytes the broker was counting on.  So
   the dispatcher is compiled only where the broker is not, and that is asserted
   below against the build script rather than left to a comment -- a guard
   naming a macro the translation unit never receives is true whether or not
   the switch is set, which is the shape of defect that drops a record behind a
   green build.

2. **Liveness is a return status, never a symbol null check.**  A
   ``__attribute__((weak_import))`` reference does not oblige the linker to keep
   the definition: MEASURED on build 41330622788, one resolved to null and
   dropped two records behind a green build with both records byte-level in the
   booting kernel, and the ``if (native_console_report)`` guard made that
   absence look deliberate.  The reference here is a strong ``extern``, so a
   missing archive fails the link naming the symbol.
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
HEADER = DRIVER.parent / "NativeVirtioConsoleTransport.h"

#: The patch that holds the dispatcher.  A NEW file rather than an edit to an
#: existing one: ``patches/xnu/*.patch`` is auto-discovered by
#: ``build-xnu-4k-baseline.sh``, so a new file needs no registration, and it
#: carries no risk of breaking the context a following patch depends on.  Two
#: builds failed at ``kern_exec.c:7389`` from an edit to a patch nobody meant to
#: touch, because ``zzz-boot-progress.patch`` needs a ten-line block that
#: ``offline-owner-systemos-mount.patch`` adds.
PATCH = REPO / "vendor" / "UniXNU" / "patches" / "xnu" / "native-install-service-dispatcher.patch"

#: The defconfig whose kernel the guest actually boots.  build-macos.sh:2590-2596
#: builds ``qemu_defconfig`` on the first pass and ``qemu_root_defconfig`` on
#: the second, and the second pass's kernel is the one the ESP carries.
BOOTING_KERNEL_DEFCONFIG = (
    REPO / "vendor" / "UniXNU-qemu" / "configs" / "qemu_root_defconfig"
)

#: The script that decides which macros reach which translation unit.
BUILD_SCRIPT = REPO / "vendor" / "UniXNU" / "scripts" / "build-xnu-4k-baseline.sh"

MILESTONES = REPO / "src" / "unipad" / "acceptance" / "milestones.py"
BOOT = REPO / "src" / "unipad" / "host" / "unixnu_boot.py"

#: The event, and the one probe that names it.
EVENT = "install.registered"

#: The escape scanner.  Built from chr() rather than written as ``"\\"`` so that
#: nothing can collapse it: a bash heredoc turns the two characters backslash-n
#: into a real newline, and that mangled this verifier's ancestor twice.
BACKSLASH = chr(92)
NEWLINE = chr(10)


# ---------------------------------------------------------------------------
# Reading the patch
# ---------------------------------------------------------------------------

def _patch_text() -> str:
    require(PATCH)
    return PATCH.read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """Added lines that are code, not prose.

    In a unified diff every added line starts with ``+``, so a comment
    explaining the emitter is a ``+`` line too -- and "the record is emitted"
    would then be satisfied by the explanation of the record.  Strip the
    marker, strip whitespace, and drop anything whose first character makes it
    a comment.
    """
    out = []
    for line in text.splitlines():
        if not line.startswith("+"):
            continue
        body = line[1:].lstrip()
        if body[:1] in ("*", "/"):
            continue
        out.append(body)
    return out


def _added_source(text: str) -> str:
    """The patch's ``+`` side as C source, with comments blanked in place.

    Context lines are kept: what makes a hunk's position meaningful is which
    function's tail it sits in, and that is carried by the context on both
    sides.  Blanking rather than deleting keeps every offset meaningful.

    Literal-aware, and CHAR-literal aware too.  The second half is not
    pedantry: the reader's newline test is ``line[end] != '\\n'``, and a walker
    that does not know a char literal is a string will read the quote inside it
    as the start of a string literal and swallow everything up to the next
    quote.  The result is a "body is not balanced" failure on code that is
    perfectly balanced -- the test going red on its own instrument, which is
    the same as no test at all.
    """
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            body = line[1:]
        elif line.startswith(("--- ", "@@ ", "diff ")):
            continue
        else:
            body = line
        out.append(body)
    joined = NEWLINE.join(out)
    return _strip_c_comments(joined)


def _strip_c_comments(source: str) -> str:
    """``/* */`` and ``//`` comments replaced by spaces, literals preserved."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch == "'":
            # A char literal.  Consume it whole, escapes included, so the quote
            # in '\\n' or '"' cannot be mistaken for a string delimiter.
            out.append(ch)
            i += 1
            while i < n:
                if source[i] == BACKSLASH and i + 1 < n:
                    out.append(source[i:i + 2])
                    i += 2
                    continue
                out.append(source[i])
                if source[i] == "'":
                    i += 1
                    break
                i += 1
            continue
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


def _skip_literal(source: str, i: int) -> int:
    """The index just past the string or char literal starting at ``i``.

    Shared by the comment stripper and the brace matcher, because the two must
    agree about where a literal ends.  They did not once: the stripper preserved
    literals (correctly -- their contents are what the JSON tests read) and the
    brace matcher then counted the ``{`` inside ``frame[0] != '{'`` as a brace,
    so the dispatcher's body looked unbalanced and the test went red on its own
    instrument.
    """
    quote = source[i]
    i += 1
    n = len(source)
    while i < n:
        if source[i] == BACKSLASH and i + 1 < n:
            i += 2
            continue
        if source[i] == quote:
            return i + 1
        i += 1
    return n


def _body_after(code: str, signature: str) -> str:
    """The brace-balanced body of the function whose signature contains ``signature``.

    ``_call_arguments`` gives the text between the parentheses, which for a
    definition is only the parameter list -- so every assertion about what a
    function DOES has to be made against the braces that follow.  An earlier
    revision of this shape read the parameter list and asserted
    ``kIOReturnNotOpen`` was in it, which passed for the wrong reason.

    Literal-aware, for the reason :func:`_skip_literal` gives.
    """
    at = code.find(signature)
    assert at != -1, f"no function with signature {signature!r} in the patch's + side"
    opening = code.find("{", at)
    assert opening != -1, f"{signature!r} has no body"
    depth, i = 1, opening + 1
    n = len(code)
    while i < n and depth:
        ch = code[i]
        if ch in ('"', "'"):
            i = _skip_literal(code, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{signature!r}'s body is not balanced"
    return code[opening + 1:i - 1]


def _record_literal(text: str) -> str:
    """The record's adjacent string literals, joined the way C concatenates."""
    hit = re.search(r"static char const native_install_record\[\] =\s*(.*?);", text, re.S)
    assert hit is not None, "native_install_record is not defined in the patch"
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', hit.group(1))
    assert literals, "native_install_record holds no string literal"
    return "".join(literals)


def _record_payload(text: str) -> dict:
    """Expand the record's adjacent literals and parse the result as JSON.

    The backslash-quote and backslash-n are still two characters until they are
    decoded, which is what the C source carries at this point; requiring a real
    newline here would be requiring something that is only true one line later.
    """
    joined = _record_literal(text)
    assert joined.endswith(BACKSLASH + "n"), (
        "native_install_record does not end with a newline escape; an "
        "unterminated record runs into the next line the host parses"
    )
    decoded = joined.replace(BACKSLASH + '"', '"').replace(BACKSLASH + "n", NEWLINE)
    assert decoded.endswith(NEWLINE), "the record does not decode to a trailing newline"
    return json.loads(decoded)


#: The guard, exactly as the patch spells it.  Written out rather than matched
#: loosely, because the coupling is the whole point and a guard that differs in
#: any way is a different coupling.
GUARD = (
    "#if defined(XNU_OWNER_ROOT_TRUST) && XNU_OWNER_ROOT_TRUST && "
    "!(defined(XNU_NATIVE_OFFLINE_GPU) && XNU_NATIVE_OFFLINE_GPU)"
)

#: The definition's signature, whitespace-insensitive, and the header's
#: declaration must match it: a prototype that disagrees with the definition is
#: a different return type and the caller compiles against the prototype.
POLL_SIGNATURE = "IOReturnnative_console_poll(void*,size_t*)"


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def test_the_record_is_one_line_of_valid_json():
    """The record parses as JSON, and is exactly one line of it.

    Asserted on the PARSED record, not on the presence of the event name: two
    drafts of the mount emitter carried the literal ``system.mount`` and
    produced a record that parsed as nothing, and both defects compiled clean.
    """
    text = _patch_text()
    joined = _record_literal(text)
    assert joined.count(BACKSLASH + "n") == 1, (
        f"the record carries {joined.count(BACKSLASH + 'n')} newline escapes, "
        f"not 1: {joined!r}.  The host's _guest_records splits the guest log by "
        f"line and parses each one, so a record split across two lines is "
        f"invisible even when both halves parse."
    )
    payload = _record_payload(text)
    assert isinstance(payload, dict), f"the record is {type(payload).__name__}, not an object"


def test_the_record_names_exactly_install_registered():
    """The event name is the one the gate's probe names, spelled exactly.

    A renamed event is reported missing by the probe, which reads exactly like
    a guest that never ran.  The direction matters: checking that the name
    appears somewhere in the patch is true whether or not the RECORD carries
    it, so the assertion is on the parsed record's own field.
    """
    text = _patch_text()
    payload = _record_payload(text)
    assert payload.get("event") == EVENT, (
        f"the record names {payload.get('event')!r}, not {EVENT!r}.  The probe "
        f"for it (milestones.py:1528) then reports the event missing, which "
        f"reads as the guest never registering the install."
    )


def test_the_records_only_route_to_disk_is_the_port_the_gate_reads():
    """The record would be scoped ONSITE, because of where it is written.

    ``_onsite_ok`` requires an EvidenceRef with ``scope == ONSITE``, which
    ``_guest_probe`` sets only when the record's path is not a simulation path
    (``milestones.py:1450-1451``, markers at ``:291``).  The path is decided by
    the SINK, not by the record: this board's guest events reach disk through
    the console port, whose capture QEMU names ``guest-channel.log``
    (``unixnu_boot.py:1874-1881``).  That name matches ``_GUEST_LOG_GLOBS`` and
    carries no simulation marker, so a record written there is ONSITE.

    All three halves are asserted, because each can change independently: the
    emitter could be repointed at another sink, the capture could be renamed to
    something the gate does not glob (``agent.log`` was, and the records sat on
    disk unread after every boot), or the gate's globs could move.

    DIAGNOSTIC RECORDS ARE ALLOWED, AND NAMED EXPLICITLY.  Two of them exist to
    answer why ``install.registered`` never arrives: a host put fifty
    well-formed frames on the channel (MEASURED, 50 successful sendall calls,
    zero errors) and the record did not come back, with ``diag.free_fn_status``
    reporting ``success``.  That leaves three causes with different remedies --
    the thread never started, ``native_console_poll()`` never returns data, or
    the recogniser rejects the frame -- and nothing outside the guest can tell
    them apart.  One record at the top of the thread and one on the first
    non-empty poll split the first two.

    They are listed by name rather than inferred from a prefix, because
    inferring "this one is only a diagnostic" from the name would let any
    record exempt itself from the one-write rule.
    """
    text = _patch_text()
    code = _code_lines(text)

    #: Records that measure the route rather than satisfy a requirement.
    #: No probe names any of them, and they come out when the question closes.
    DIAGNOSTIC_RECORDS = ("diag.install_thread_started", "diag.install_rx_bytes")
    #: The C variable each diagnostic record is built into.  The WRITE line
    #: names the variable, not the event, so matching on the event name alone
    #: matches nothing and every diagnostic write reads as unexplained.
    DIAGNOSTIC_VARS = ("thread_started_record", "rx_bytes_record",
                      "install_start_ok", "install_start_failed")

    # The declaration names the symbol once and is not a write.  Without
    # this filter, adding the declaration build 41376479360 demanded made
    # this test count it as a second write of the same event -- a test going
    # red on the fix for the defect it was written to catch.
    emitters = [line for line in code
                if "native_console_report(" in line
                and not line.startswith("extern")]
    assert emitters, "the patch never hands a record to the console port"
    requirement_writes = [
        line for line in emitters
        if "native_install_record" in line]
    diagnostic_writes = [
        line for line in emitters
        if any(name in line for name in DIAGNOSTIC_VARS)]
    assert requirement_writes, (
        "the patch no longer writes install.registered itself, so the one "
        "record that clears a four-milestone blocker is gone")
    assert len(requirement_writes) == 1, (
        f"install.registered is written {len(requirement_writes)} times, not "
        f"once: {requirement_writes}")
    unexplained = [line for line in emitters
                   if line not in requirement_writes
                   and line not in diagnostic_writes]
    assert not unexplained, (
        f"the patch writes to the port with something other than the record "
        f"or a named diagnostic: {unexplained}.  A record that reaches disk by "
        f"another route is not the record this test can see."
    )
    # No other sink.  printf/kprintf would put the record in the kernel ring,
    # which the gate does not read, and would look like success locally.  The
    # scan is over ADDED CODE lines only, and it looks for a call rather than a
    # substring: this patch's own context carries bsd_init_kprintf(), and a
    # substring test for "kprintf(" matches it and goes red on the patch's
    # pretext -- the test failing on the very lines it is meant to read past.
    for banned in ("printf", "kprintf", "IOLog", "os_log"):
        hits = [
            line for line in code
            if re.search(r"(?<![A-Za-z0-9_])" + banned + r"\s*\(", line)
        ]
        assert not hits, (
            f"the patch calls {banned}(): {hits}.  The kernel ring is not a "
            f"guest log the acceptance gate reads, so a record that lands only "
            f"there would read as success locally and be invisible to the gate."
        )

    require(BOOT)
    boot = BOOT.read_text(encoding="utf-8")
    assert "guest-channel.log" in boot, (
        "unixnu_boot.py no longer names the agent chardev's capture "
        "guest-channel.log, so which file the console port's bytes land in is "
        "unknown and this record's scope cannot be established"
    )

    require(MILESTONES)
    gate = MILESTONES.read_text(encoding="utf-8")
    globs = re.search(r"_GUEST_LOG_GLOBS = \((.*?)\)", gate, re.S)
    assert globs, "_GUEST_LOG_GLOBS is not defined in milestones.py"
    patterns = re.findall(r'"([^"]*)"', globs.group(1))
    assert patterns, "_GUEST_LOG_GLOBS carries no pattern"

    def _matches(name: str, pattern: str) -> bool:
        regex = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?")
        regex = regex.replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", ".")
        return re.fullmatch(regex, name) is not None

    assert any(_matches("guest-channel.log", pattern) for pattern in patterns), (
        f"guest-channel.log matches none of _GUEST_LOG_GLOBS {patterns}; the "
        f"records would be on disk after every boot and the gate could not see "
        f"them -- which is exactly what happened when the file was called "
        f"agent.log"
    )
    markers = re.search(r"_SIMULATION_MARKERS = \((.*?)\)", gate, re.S)
    assert markers, "_SIMULATION_MARKERS is not defined in milestones.py"
    for marker in re.findall(r'"([^"]*)"', markers.group(1)):
        assert marker not in "guest-channel.log", (
            f"the capture's name carries the simulation marker {marker!r}, so "
            f"every record in it is simulation-scoped and can never satisfy an "
            f"onsite requirement"
        )


def test_the_gate_names_the_event_in_an_onsite_guest_probe():
    """The event is one an ONSITE guest probe requires, not just a string.

    A record that satisfies no probe is a fact nobody reads.  Asserted by
    finding the probe that names the event and checking the probe factory's own
    scope, rather than by checking that the string appears anywhere in the
    gate -- which is true of a comment.
    """
    require(MILESTONES)
    gate = MILESTONES.read_text(encoding="utf-8")
    hit = re.search(
        r'_guest_probe\(\s*"p\.install_registered"\s*,\s*"[^"]*"\s*,\s*\(([^)]*)\)', gate
    )
    assert hit, (
        "no _guest_probe named p.install_registered in milestones.py; the "
        "install.registered record would satisfy nothing"
    )
    assert f'"{EVENT}"' in hit.group(1), (
        f"the probe names {hit.group(1)!r}, not {EVENT!r}"
    )
    assert "_ONSITE = EvidenceScope.ONSITE" in gate, (
        "milestones.py no longer defines _ONSITE as EvidenceScope.ONSITE, so "
        "the claim that these requirements are onsite cannot be checked here"
    )


# ---------------------------------------------------------------------------
# The emitter is gated on the install having been registered
# ---------------------------------------------------------------------------

def test_the_emitter_is_gated_on_the_install_having_been_registered():
    """Emitted only after a well-formed install.request has been accepted.

    An emitter that fired unconditionally would claim a registration on a boot
    where no request ever arrived, which is the false statement this
    repository's record discipline exists to prevent.  So the record is emitted
    from inside the function that validates the request, AFTER the flag that
    says the registration was recorded, and never from the reader thread's
    entry.

    All three halves matter on their own: the emission before the flag would
    claim a registration the code then refuses; the emission from the thread's
    entry would fire on every boot regardless; and a missing early return would
    make the validation decorative.
    """
    text = _patch_text()
    code = _added_source(text)

    flag = "native_install_registered = 1;"
    emit = "native_console_report(native_install_record,"
    dispatcher = _body_after(code, "native_install_service(const char *frame, size_t length)")
    reader = _body_after(code, "native_install_service_thread(void *param")

    assert flag in dispatcher, (
        "native_install_service() never records that the install was "
        "registered, so the flag the emitter depends on is never set"
    )
    assert emit in dispatcher, (
        "the record is not emitted from native_install_service(), so it is not "
        "emitted by the code that accepted the install request"
    )
    assert dispatcher.index(emit) > dispatcher.index(flag), (
        "the record is emitted BEFORE native_install_registered is set; the "
        "record would then claim a registration the code has not recorded"
    )
    assert emit not in reader, (
        "the record is emitted from the reader thread's own body, so it fires "
        "on every boot whether or not an install request ever arrived"
    )
    # The validation is real, and it is checked by NAME rather than by a count.
    # A count of "at least four" passed a mutation that deleted the digest check
    # outright, because four of the five guards were still there -- the test
    # going green on a validator that no longer required the digest.  Naming the
    # five guards makes a deleted one a failure that says which.
    early = dispatcher[:dispatcher.index(flag)]
    assert early.count("return;") >= 5, (
        f"native_install_service() takes only {early.count('return;')} early "
        f"returns before it records the registration.  The request must be "
        f"refused for: already registered, not a JSON object, the wrong type, "
        f"no package name, and no digest."
    )
    for marker, why in (
        ("native_install_registered)", "an install that was already registered"),
        ("frame[0] != '{'", "a line that is not a JSON object"),
        ("native_install_type_key", "a frame whose type is not install.request"),
        ("native_install_name_key", "a frame that names no package"),
        ("native_install_digest_key", "a frame that names no digest"),
    ):
        assert marker in early, (
            f"native_install_service() no longer refuses {why}: the guard on "
            f"{marker!r} is gone from the code that runs before the "
            f"registration is recorded."
        )


def test_the_recogniser_matches_the_frame_the_hosts_own_encoder_produces():
    """The three keys are substrings of a real install.request, byte for byte.

    The recogniser is a set of literal byte sequences, so it is only as good as
    its agreement with the host's encoder.  Asserted against the encoder itself
    rather than against a hand-written example: ``Message.to_json()`` uses
    ``sort_keys=True`` and ``separators=(",",":")``, so there is no space
    anywhere in a frame on this wire, and a recogniser that expected one would
    match nothing while looking perfectly correct.

    A hand-written fixture would agree with whatever the patch says.  Building
    the frame with the host's own protocol module cannot.
    """
    from unipad.guest.protocol import (
        MSG_INSTALL_REQUEST,
        InstallRequest,
        Message,
        new_message_id,
    )

    text = _patch_text()
    code = _added_source(text)

    request = InstallRequest(
        package_name="HelloUniPad",
        package_sha256="a" * 64,
        size_bytes=4096,
        declared_min_os="17.0",
        signature_mode="adhoc",
    )
    frame = Message(
        type=MSG_INSTALL_REQUEST, id=new_message_id(), payload=request.to_dict()
    ).to_json()
    assert " " not in frame, (
        f"the host's own encoder produced a frame with a space in it, so the "
        f"separators this recogniser assumes are no longer what the host sends: "
        f"{frame!r}"
    )

    for name in ("native_install_type_key", "native_install_name_key",
                 "native_install_digest_key"):
        # The C source line is `static char const NAME[] = "...";` where the
        # inner quotes are escaped, so the literal's CONTENT is everything
        # between the first quote after the `=` and the last quote before the
        # semicolon.  Greedy on purpose: the content contains no unescaped
        # quote, so the first and last quote on the line are the delimiters.
        hit = re.search(
            r"static char const " + name + r"\[\] = \"(.*)\";", code
        )
        assert hit, f"{name} is not defined in the patch's + side"
        decoded = hit.group(1).replace(BACKSLASH + '"', '"')
        assert decoded in frame, (
            f"{name} is {decoded!r}, which is not a substring of the frame the "
            f"host's own encoder produces.  The dispatcher would then never "
            f"recognise an install request and would emit nothing, while every "
            f"source-level check passed.\n  frame: {frame!r}"
        )


def test_the_dispatcher_does_not_answer_the_host():
    """No install.response, no install.progress, and no other sink.

    This is not an omission to be filled in later -- every available answer is
    either a fabricated success or a veto, and adding one makes the milestone
    WORSE:

    * ``install.response`` with stage installed/registered must carry a
      ``bundle_id`` (``GuestClient._reject_fabricated_success``,
      ``client.py:478-505``) and this dispatcher installs nothing;
    * ``install.response`` with stage received/checked/transferred lands in the
      host's ``install.json`` (``controller/cli.py:2316``) and arms the
      ``copy_only_install`` veto (``milestones.py:1766-1769``), which FAILS M10
      even with ``install.registered`` on the wire;
    * ``install.progress`` is read by ``AgentChannelTransport.request`` as THE
      reply (``agent_channel.py:271``) and then rejected for its type.

    So the guard is that none of them appears, and that the port is written to
    exactly once with exactly the record.
    """
    text = _patch_text()
    code = _code_lines(text)
    for banned in ("install.response", "install.progress", "challenge.response",
                   "status.response", "launch.response", "persist.response",
                   "MSG_INSTALL_RESPONSE"):
        # CODE lines only.  This patch's own comment explains at length why
        # there is no reply, and it names all three reply types while doing so
        # -- a scan over the whole text would be satisfied by the explanation
        # of the thing it is checking for, which is the fourth time that shape
        # has bitten this project.
        hits = [line for line in code if banned in line]
        assert not hits, (
            f"the patch now carries {banned} in a code line: {hits}.  A reply "
            f"is not free: see this test's docstring for the three ways one of "
            f"them turns a cleared blocker into a failed milestone."
        )
    writes = [line for line in code
              if "native_console_report(" in line
              and not line.startswith("extern")]
    #: Records that measure the route rather than satisfy a requirement.  No
    #: probe names any of them, and they come out when the question closes.
    DIAGNOSTIC_RECORDS = ("diag.install_thread_started", "diag.install_rx_bytes")
    #: The C variable each diagnostic record is built into.  The WRITE line
    #: names the variable, not the event, so matching on the event name alone
    #: matches nothing and every diagnostic write reads as unexplained.
    DIAGNOSTIC_VARS = ("thread_started_record", "rx_bytes_record",
                      "install_start_ok", "install_start_failed")
    requirement_writes = [line for line in writes
                          if "native_install_record" in line]
    assert len(requirement_writes) == 1, (
        f"install.registered is written {len(requirement_writes)} times, not "
        f"once: {requirement_writes}.  A second write of the same event is "
        f"silently dropped by the host (_guest_events uses setdefault), so a "
        f"reader of the log would never see it.")
    unexplained = [line for line in writes
                   if line not in requirement_writes
                   and not any(name in line for name in DIAGNOSTIC_VARS)]
    assert not unexplained, (
        f"the port is written with something other than install.registered or "
        f"a named diagnostic: {unexplained}.  A write of a DIFFERENT event arms "
        f"one of the vetoes -- see this test's docstring for the three ways a "
        f"reply turns a cleared blocker into a failed milestone."
    )


# ---------------------------------------------------------------------------
# The reference and the length
# ---------------------------------------------------------------------------

def test_the_declaration_is_not_inside_a_conditional_its_callers_are_outside():
    """The ``extern`` sits at unconditional file scope, above the guard.

    Build 41359711528 failed at ``kern_exec.c:8120: error: call to undeclared
    function 'native_console_report'``.  The caller sat OUTSIDE
    ``#if defined(XNU_OWNER_CACHE_MOUNT)`` and the ``extern`` declaring it sat
    INSIDE, and the build log names the configuration: "kernel: no owner root
    chain on disk, so building qemu_defconfig" -- the first pass, which does not
    define that macro for this translation unit.

    That is the inverse of the defect this file's neighbours guard, where the
    macro gated the EMITTER and it was silently not compiled.  Same root cause
    both times -- a caller and its declaration behind different conditionals --
    and neither is visible to any source-level check, because nothing on this
    host preprocesses.  So the rule is asserted here as a position: the
    declaration must come BEFORE the guard whose region holds the callers, and
    it must be at file scope.

    The patch carries the guard TWICE -- once around the thread start inside
    ``bsdinit_task()``, once around the definitions -- so "before the first
    guard" is the wrong test and "before the LAST one" is the right one: the
    last guard opens the region that contains every call site.  An earlier
    revision of this test compared against the first guard and went red on a
    patch that was correct, which is the same as no test at all.

    A declaration with no definition is free: the linker resolves a symbol only
    when something references it, so an unconditional ``extern`` costs nothing
    when the dispatcher is compiled out, and it cannot be separated from a
    caller somebody adds later.
    """
    text = _patch_text()
    code = _added_source(text)

    declaration = code.find("extern int native_console_poll(void *, size_t *);")
    assert declaration != -1, (
        "the patch does not declare native_console_poll; its caller in "
        "bsd_init.c would be a call to an undeclared function"
    )
    # The LAST guard is the one whose region holds the call sites.
    last_guard = code.rfind(GUARD)
    assert last_guard != -1, f"the patch no longer carries the guard {GUARD!r}"
    assert declaration < last_guard, (
        "the extern declaring native_console_poll sits INSIDE the guarded region "
        "that holds its callers.  A caller added outside that guard would then "
        "be a call to an undeclared function on a configuration nobody was "
        "editing -- measured as build 41359711528, kern_exec.c:8120, on the "
        "first pass."
    )
    # And it is at FILE scope: column zero, no indentation.  A declaration
    # nested inside a function body is in scope for that function only.
    assert code[declaration - 1] == NEWLINE, (
        "the extern is not at the start of a line, so it is not at file scope"
    )
    # And declared exactly once.  A second declaration is harmless to the
    # compiler but it is how the two of them drift apart.
    assert code.count("extern int native_console_poll(void *, size_t *);") == 1, (
        "native_console_poll is declared more than once in the patch"
    )
    # Every call site really is inside that region, which is what makes
    # "before the last guard" the right comparison rather than a coincidence.
    assert code.find("native_console_poll(", last_guard) != -1, (
        "no call site sits inside the guarded region, so the comparison above "
        "is measuring nothing"
    )


def test_every_function_the_call_block_uses_is_declared_before_it():
    """The call block sits above the definitions, so it needs forward declarations.

    The dispatcher's definitions live AFTER ``bsdinit_task()`` -- that placement
    is deliberate, because inserting them between ``bsd_init()``'s closing brace
    and ``bsdinit_task()`` would break the context ``offline-owner-root-binding-audit.patch``
    needs (its second hunk's pretext ends at ``void bsdinit_task(void)``).  The
    consequence is that the thread function must be forward-declared inside the
    guarded call block, or the call is a call to an undeclared function.

    That is not hypothetical and it is not theoretical: the first revision of
    this patch had no forward declaration, and nothing here caught it -- the
    patch applied, the counts matched, and the only thing that would have
    found it is a compiler this host does not have.  Build 41359711528 failed at
    ``kern_exec.c:8120: error: call to undeclared function`` for the same
    reason, and neither defect is visible to any source-level check.

    AND THIS TEST HAS NOW BEEN WRONG ABOUT WHERE THE DECLARATION GOES TWICE.

    Round one demanded it INSIDE the call block, immediately above the call.
    Build 41376479360's second pass -- the first compiler to see the dispatcher
    -- rejected exactly that:

        bsd_init.c:1309:2: error: function declared in block scope cannot have
                          'static' storage class

    Round two demanded FILE scope, on the reasoning that a block-scope
    declaration cannot carry `static` and dropping the keyword then fails
    against the definition with "static declaration follows non-static
    declaration".  Build 41382433090 rejected that too:

        bsd_init.c:1325:27: error: use of undeclared identifier
                           'native_install_service_thread'

    The file-scope declaration sat at applied line 1377 -- AFTER the call at
    1325, because the whole added block begins INSIDE ``bsdinit_task()``
    (pristine line 1096, five lines into a function opening at 1073) and the
    definitions were deliberately placed after it.  And there is no file-scope
    position that is both before the call and free of another patch's context:
    the region between ``bsd_init()``'s closing brace and ``bsdinit_task()`` is
    claimed by ``offline-owner-root-binding-audit.patch``'s second hunk, whose
    pretext ends at ``void bsdinit_task(void)``.

    Round three -- what is here now -- is block scope WITHOUT `static`.  The
    keyword is what is illegal in block scope, not the declaration, and the
    definition drops `static` to match.  The cost is one symbol with external
    linkage in the kernel's table: namespace pollution rather than a correctness
    problem, and the only option that does not fight another patch for the same
    lines.

    A test written from reasoning about C rather than from a compiler demanded
    something the compiler refuses -- twice -- the same shape as the inter-patch
    guard that passed while the build failed.  What the compiler actually
    settles is the scope, and it settles it as a consequence of a placement
    constraint this file cannot see, so the only durable form of this test is
    the one that names all three: the declaration exists, it is in block scope
    because that is the only position before the call, and it does not carry
    `static`.

    It also checked only the thread function.  The declaration that actually
    failed to compile was ``native_console_report``, and this test's own name
    says "every function the call block uses".  So it walks them all.
    """
    text = _patch_text()
    code = _added_source(text)
    lines = _code_lines(text)

    call = code.find("kernel_thread_start(native_install_service_thread")
    assert call != -1, "the patch never starts the install service"

    # The thread's forward declaration: block scope, NO `static`, and ABOVE THE
    # CALL.  All three, because each one was a build failure:
    #
    #   absent                      -> 41359711528, call to undeclared function
    #   block scope WITH `static`   -> 41376479360, block scope cannot be static
    #   file scope (after the call) -> 41382433090, undeclared identifier
    #
    # Indentation is read from the RAW diff lines, because _code_lines strips
    # it -- and stripped, a file-scope declaration and a block-scope one are
    # the same string.  That is precisely the distinction the last two builds
    # turned on, so it cannot be read through a helper that erases it.
    #
    # The anchor is deliberately LOOSE -- any added line whose stripped text
    # ends with the thread's signature-and-semicolon -- so that each defect
    # below fails on the assertion that names it.  Matching the exact final
    # string instead makes all three collapse into "no declaration at all",
    # which is a lie on a tree where one plainly exists and sends the next
    # reader looking in the wrong place.
    signature = "native_install_service_thread(void *, wait_result_t);"
    raw = [ln for ln in text.splitlines() if ln.startswith("+")]
    decl_raw = [ln for ln in raw if ln[1:].rstrip().endswith(signature)]
    assert decl_raw, (
        "the call block starts native_install_service_thread without declaring "
        "it.  The definitions live below bsdinit_task(), so this is a call to "
        "an undeclared function -- measured as build 41359711528 at "
        "kern_exec.c:8120 and again as 41382433090 at bsd_init.c:1325, and "
        "invisible to every check in this file that does not look for the "
        "declaration."
    )
    assert len(decl_raw) == 1, f"declared {len(decl_raw)} times: {decl_raw}"
    line = decl_raw[0]
    assert "static" not in line, (
        f"the block-scope declaration carries `static`: {line!r}.  Build "
        f"41376479360 failed with 'function declared in block scope cannot "
        f"have static storage class' at bsd_init.c:1309."
    )
    assert line[1:2].isspace(), (
        f"the forward declaration is NOT indented, so it is at file scope: "
        f"{line!r} -- which puts it after the call.  Build 41382433090 failed "
        f"with 'use of undeclared identifier native_install_service_thread' at "
        f"bsd_init.c:1325 for exactly that reason.  Block scope is not a style "
        f"choice here: it is the only position that is before the call, because "
        f"the file-scope region above it is claimed by "
        f"offline-owner-root-binding-audit.patch's second hunk."
    )
    assert line[1:].strip() == "void " + signature, (
        f"the declaration's type is not the expected `void`: {line!r}"
    )
    declaration = line[1:].strip()
    assert declaration in lines, "the declaration is not in the added code"
    decl_at = lines.index(declaration)
    assert decl_at < call, (
        "the forward declaration sits after the call, which is the same as "
        "having none"
    )
    definition = next(i for i, ln in enumerate(lines)
                      if ln.startswith("native_install_service_thread("))
    assert decl_at < definition, (
        "the forward declaration sits after the definition, which is the same "
        "as having none"
    )
    # The pair must agree on linkage.  Round two's failure was the mismatch:
    # "static declaration follows non-static declaration".  The declaration is
    # non-static, so the definition must be too.  The sibling function
    # native_install_service keeps `static` -- that one is correct, because it
    # has no block-scope declaration to agree with.
    def_lines = lines[max(0, definition - 2):definition]
    assert not any(ln == "static void" for ln in def_lines), (
        f"the definition still spells `static`, so it disagrees with the "
        f"non-static forward declaration: {def_lines!r}.  Build 41376479360 "
        f"failed with 'static declaration follows non-static declaration' at "
        f"bsd_init.c:1649."
    )

    # Every function the added code CALLS must be declared somewhere in it.  The
    # first revision of this test named only the thread and missed
    # native_console_report, which is the one that failed to compile.
    #
    # Block-scope declarations count too: they start with a type, not with
    # `extern` or `static`, and the thread's is exactly such a line.  Without
    # this the test would report the declaration it just required as missing.
    #
    # The matcher is a declaration SHAPE, not a substring test: a type, a name,
    # a parameter list, a semicolon, end of line.  A CALL has the same shape
    # minus the type, and counting calls as declarations would let a missing
    # declaration hide behind the very call that needs it -- the test passing
    # because of the defect it exists to catch.
    declared = {m.group(1)
                for ln in lines
                for m in [re.match(r"^[A-Za-z_]\w*(?:[\w \t]+\**)?\s+"
                                   r"(\w+)\s*\(.*\)\s*;\s*$", ln)]
                if m}
    called = set()
    for ln in lines:
        if ln.startswith(("extern ", "static ", "#")):
            continue
        for name in ("kernel_thread_start", "native_console_poll",
                     "native_console_report", "native_install_service_thread",
                     "native_install_recognise"):
            if name + "(" in ln:
                called.add(name)
    missing = sorted(called - declared - {"kernel_thread_start"})
    assert not missing, (
        f"the patch calls {missing} without declaring it in this patch.  A "
        f"declaration the patch does not carry is a call to an undeclared "
        f"function -- build 41376479360 failed at bsd_init.c:1633 for exactly "
        f"that reason, and no source-level check sees it."
    )


def test_the_console_reference_is_strong_not_weak():
    """``native_console_poll`` must be a STRONG reference.

    This is the whole reason build 41330622788 existed, and the defect it
    caught is the most expensive kind: silent, and invisible to every
    source-level check.  The reference used to be
    ``__attribute__((weak_import))`` on the reasoning that the console archive
    is behind its own build switch.  It does not work: a weak reference does
    not oblige the linker to keep the definition, so the symbol the call site
    tests for is simply absent, and the null check makes that absence look
    deliberate.

    A strong reference makes the definition load-bearing: if the archive is
    ever missing while this code is compiled, the link fails naming the symbol.
    """
    text = _patch_text()
    code = _code_lines(text)
    declarations = [
        line for line in code
        if line.startswith("extern") and "native_console_poll" in line
    ]
    assert len(declarations) == 1, (
        f"expected exactly one declaration of native_console_poll, found "
        f"{len(declarations)}: {declarations}"
    )
    assert "weak_import" not in declarations[0], (
        "native_console_poll is weak-imported. Measured on build 41330622788: "
        "the symbol then resolves to null and the records are dropped "
        "silently, with the build green and the records byte-level present in "
        "the kernel. Use a strong extern."
    )
    assert declarations[0] == "extern int native_console_poll(void *, size_t *);", (
        f"the declaration is not the expected strong extern: {declarations[0]!r}"
    )


def test_no_null_check_decides_whether_a_record_is_emitted():
    """No ``if (native_console_...)`` anywhere in the patch.

    A guard that tests whether an optional dependency is present is
    indistinguishable, from the source, from a guard hiding that the dependency
    was never linked.  The two have opposite remedies.  Liveness is answered by
    the return status instead: ``native_console_poll()`` returns
    ``kIOReturnNotOpen`` while no console is live, which the reader waits out.

    The mount emitter's guard is a DEAD branch that a test deliberately keeps
    (``test_pinned_patches_apply.py:924-944``).  This patch does not carry one
    at all, and that difference is the point: the new code must not grow the
    pattern, whatever the older patch does with it.
    """
    text = _patch_text()
    code = _code_lines(text)
    # Every mention of the symbol in code must be a CALL, and nothing else.
    # Checking for the `if (...)` spelling alone is not enough: the same defect
    # written as a conditional operator -- `native_console_poll ? ... : ...` --
    # survived the first revision of this test, and it is the same defect.  A
    # guard that tests whether an optional dependency is present is
    # indistinguishable, from the source, from a guard hiding that the
    # dependency was never linked.  The two have opposite remedies.
    for line in code:
        if "native_console_poll" not in line and "native_console_report" not in line:
            continue
        if line.startswith("extern"):
            continue                    # the declaration, which names it once
        # A symbol name inside a STRING LITERAL is text, not code.  The
        # diagnostics below name the entry point in their own "source" field,
        # and without this skip the test reads that as a use of the symbol as a
        # value -- the same shape as the kprintf scan matching this patch's own
        # pretext, and the fourth time that shape has bitten this project.
        #
        # The scan is ESCAPE-AWARE, because the parity of raw quote characters
        # is the wrong test here: a C string holding an escaped quote reads
        # `"\"source\":\"XNU.native_console_poll\"...`, and by the time the
        # symbol is reached the count of `"` characters is EVEN even though the
        # scanner is inside the literal.  The first version of this skip used
        # that parity and did not skip anything.
        for symbol in ("native_console_poll", "native_console_report"):
            if symbol not in line:
                continue
            inside = False
            escaped = False
            for index, char in enumerate(line):
                if escaped:
                    escaped = False
                elif char == BACKSLASH:
                    escaped = True
                elif char == '"':
                    inside = not inside
                elif not inside and line.startswith(symbol, index):
                    after = line[index + len(symbol):].lstrip()
                    assert after.startswith("("), (
                        f"the patch uses {symbol} as a value rather than "
                        f"calling it: {line!r}.  A null check or a conditional "
                        f"on the symbol would make a linker that dropped the "
                        f"definition look like a deliberate design decision.  "
                        f"Test the RETURN STATUS instead: "
                        f"native_console_poll() returns kIOReturnNotOpen while "
                        f"no console is live, which the reader waits out."
                    )


def test_the_record_is_an_array_whose_sizeof_is_of_the_array():
    """``sizeof(record) - 1`` on an ARRAY, never on a decayed pointer.

    This is the defect that cost the previous driver pin three records on a
    real guest: the record was selected by a ternary whose two arms were arrays
    of different lengths, the conditional operator decayed both to ``char *``,
    and ``sizeof`` became 8.  The literals were in the kernel the whole time
    and the build was green, so neither a build nor a byte-level grep of the
    image could see it.  All three XNU records arrived seven bytes long and the
    capture ended with the literal bytes ``b'{"event{"event{"event'``.

    Both halves are asserted: the declaration must be the array form, and the
    call must pass that array directly rather than an expression that could
    select between two of them.
    """
    text = _patch_text()
    code = _code_lines(text)

    declarations = [
        line for line in code if line.startswith("static char const native_install_record")
    ]
    assert len(declarations) == 1, (
        f"expected exactly one declaration of native_install_record, found "
        f"{len(declarations)}: {declarations}"
    )
    assert declarations[0].endswith("[] ="), (
        f"native_install_record is not declared as an array: "
        f"{declarations[0]!r}.  sizeof on a pointer is 8 on arm64, which is the "
        f"seven-byte record."
    )
    assert "*" not in declarations[0], (
        f"native_install_record is declared through a pointer: "
        f"{declarations[0]!r}"
    )
    assert any(
        line == "native_console_report(native_install_record, sizeof(native_install_record) - 1);"
        for line in code
    ), (
        "the record is not handed to the port as sizeof(native_install_record) "
        "- 1; any other length expression is the eight-byte record"
    )
    # No conditional operator anywhere near the emitter: the ternary that
    # decayed two arrays to one pointer is the shape being excluded.
    start = next(i for i, line in enumerate(code)
                 if "native_console_report(native_install_record" in line)
    block = code[max(0, start - 8):start + 2]
    assert not any("?" in line for line in block), (
        f"the emitter block carries a conditional operator: {block}"
    )


# ---------------------------------------------------------------------------
# The switch coupling: the dispatcher is not compiled where the GPU broker is
# ---------------------------------------------------------------------------

def test_the_dispatcher_is_disabled_by_the_switch_that_enables_the_gpu():
    """The guard names the GPU switch, and the build really passes it here.

    This is a design invariant, not a detail.  ``NativeOfflineGPU::readExact()``
    (``NativeOfflineGPU.cpp:34-44``) drains RX in a lock-step loop and treats
    ANY short read as ``kIOReturnIOError``; ``exchange()`` then sets
    ``online=false`` at ``:75``, permanently, because a partial stream cannot be
    reused.  Two consumers of one RX byte stream cannot coexist: the
    dispatcher's line buffering would take bytes the broker was counting on and
    take the broker offline for the rest of the boot.

    ``NativeOfflineGPU`` is absent from the kernel four ways over today, so
    there is no competing consumer and this is safe NOW.  The moment the broker
    is revived it is not, and the guard is what keeps that revival from also
    silencing the broker.

    The half that makes this a real test rather than decoration: the macro the
    guard names must actually reach this translation unit.  A guard naming a
    macro the file never receives is true whether or not the switch is set --
    the shape of defect that drops a record behind a green build.
    """
    text = _patch_text()
    assert text.count(GUARD) == 2, (
        f"the guard appears {text.count(GUARD)} times, not 2 (once for the "
        f"thread start inside bsdinit_task(), once for the definitions).  A "
        f"guard that differs from\n  {GUARD}\nis a different coupling."
    )
    assert "XNU_NATIVE_OFFLINE_GPU" in GUARD, "the guard does not name the GPU switch"

    require(BUILD_SCRIPT)
    build = BUILD_SCRIPT.read_text(encoding="utf-8")
    # Both macros must be on THIS translation unit's CFLAGS.  That is the whole
    # reason the dispatcher lives in bsd_init.c rather than in kern_exec.c:
    # kern_exec.o receives XNU_OWNER_CACHE_MOUNT, and bsd_init.o is the object
    # that receives XNU_NATIVE_OFFLINE_GPU.
    hit = re.search(r'EXTRA_MAKE_ARGS\+=\("bsd_init\.o_CFLAGS_ADD=([^"]*)"', build)
    assert hit, "build-xnu-4k-baseline.sh no longer passes bsd_init.o_CFLAGS_ADD"
    flags = hit.group(1)
    assert "-DXNU_NATIVE_OFFLINE_GPU=$NATIVE_OFFLINE_GPU" in flags, (
        "bsd_init.o no longer receives -DXNU_NATIVE_OFFLINE_GPU, so the GPU "
        "half of this guard is VACUOUS: it would be true whether or not the "
        "broker were enabled, which is the shape of defect that drops a record "
        "behind a green build.  Either move the dispatcher to a translation "
        "unit that receives the macro, or make the build pass it here."
    )
    assert "-DXNU_OWNER_ROOT_TRUST=$OWNER_ROOT_TRUST" in flags, (
        "bsd_init.o no longer receives -DXNU_OWNER_ROOT_TRUST, so the console "
        "half of this guard is vacuous too"
    )
    # And the macro must be DERIVED from the switch, not hardcoded.
    assert "NATIVE_OFFLINE_GPU=${XNU_NATIVE_OFFLINE_GPU:-0}" in build, (
        "build-xnu-4k-baseline.sh no longer derives NATIVE_OFFLINE_GPU from "
        "XNU_NATIVE_OFFLINE_GPU, so what enables the broker is no longer "
        "measurable from here"
    )

    # Today: the broker is off and the port is on.  Both halves of the claim
    # "this is safe now" are read out of the config rather than asserted from
    # memory.
    require(BOOTING_KERNEL_DEFCONFIG)
    config = BOOTING_KERNEL_DEFCONFIG.read_text(encoding="utf-8")
    assert "CONFIG_XNU_NATIVE_OFFLINE_GPU=y" not in config, (
        "qemu_root_defconfig now sets CONFIG_XNU_NATIVE_OFFLINE_GPU, so the "
        "broker and this dispatcher would both be compiled into the kernel the "
        "guest boots and would share one RX byte stream.  The coupling this "
        "test exists for has been reached."
    )
    assert "CONFIG_XNU_OWNER_ROOT_TRUST=y" in config, (
        "qemu_root_defconfig no longer sets CONFIG_XNU_OWNER_ROOT_TRUST, so the "
        "dispatcher would not be compiled into the kernel the guest boots and "
        "the record would never be emitted"
    )
    assert "CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y" in config, (
        "qemu_root_defconfig no longer sets CONFIG_XNU_NATIVE_VIRTIO_CONSOLE, "
        "so the archive that defines native_console_poll is not on the kernel "
        "link and this code would fail to link"
    )


def test_the_console_half_of_the_guard_is_the_switch_that_links_the_archive():
    """``XNU_OWNER_ROOT_TRUST`` is set exactly where the console is.

    A strong reference makes the definition load-bearing, which is the loud
    failure and the correct one -- but it means the gate has to be a switch
    that actually puts the archive on the link.  ``XNU_OWNER_ROOT_TRUST`` is
    that switch by configuration rather than by macro: both are set only by
    ``qemu_root_defconfig``, which is the config whose kernel the guest boots.
    The first-pass ``qemu_defconfig`` sets neither, so the first pass compiles
    this out instead of failing to link on an undefined symbol.
    """
    root = REPO / "vendor" / "UniXNU-qemu" / "configs" / "qemu_root_defconfig"
    first = REPO / "vendor" / "UniXNU-qemu" / "configs" / "qemu_defconfig"
    require(root, first)
    booting = root.read_text(encoding="utf-8")
    assert "CONFIG_XNU_OWNER_ROOT_TRUST=y" in booting
    assert "CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y" in booting

    first_pass = first.read_text(encoding="utf-8")
    assert "CONFIG_XNU_OWNER_ROOT_TRUST=y" not in first_pass, (
        "qemu_defconfig now sets CONFIG_XNU_OWNER_ROOT_TRUST, so the first "
        "kernel pass compiles the dispatcher too -- and the first pass does "
        "not link the console archive, so it would fail on an undefined "
        "symbol rather than compiling this out"
    )
    assert "CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y" not in first_pass, (
        "qemu_defconfig now sets CONFIG_XNU_NATIVE_VIRTIO_CONSOLE, so the two "
        "switches are no longer distinguishable and this assertion's premise "
        "has to be re-derived"
    )


# ---------------------------------------------------------------------------
# The patch is well-formed and applies
# ---------------------------------------------------------------------------

def test_the_install_patch_is_in_the_set_at_all():
    """Coverage that looks total and is not, in the other direction.

    Every assertion below is about one patch.  If that patch stops being walked
    -- renamed, moved out of ``patches/xnu``, or excluded by a glob -- every one
    of them still passes, because each looks its own file up by name and skips
    when it is absent.  The count and the target file are asserted here so that
    a patch nobody walks is a red test rather than a dozen green ones.
    """
    # The guard comes FIRST, before the directory walk.  A clean checkout has no
    # vendor/ at all and tests/unit runs against exactly such a checkout, so a
    # walk written ahead of its guard is a collection error there -- the defect
    # that made 2026-10-31's clean-checkout run lose thirty tests.
    require(PATCH)
    patches = sorted((REPO / "vendor" / "UniXNU" / "patches").rglob("*.patch"))
    names = {p.name for p in patches}
    assert PATCH.name in names, (
        f"{PATCH.name} is not being walked; the assertions about it would each "
        f"skip and read as not-verified-here"
    )
    text = _patch_text()
    assert "--- a/bsd/kern/bsd_init.c" in text and "+++ b/bsd/kern/bsd_init.c" in text, (
        "the dispatcher no longer targets bsd/kern/bsd_init.c, so the "
        "bsd_init.o_CFLAGS_ADD pairing this patch depends on is about the wrong "
        "file"
    )


def test_the_install_patch_applies_to_its_own_minus_side():
    """``git apply`` itself, plus the header check, for this patch alone.

    The whole-file tests elsewhere already do both for every patch.  This one
    exists because those tests walk a directory: a patch that is added, applies,
    and is never named by any other assertion here would pass them and be
    measured by nothing.  It is also the only place this patch's OWN hunk
    headers are checked for reachability, which is the defect that cost run
    40981242194 a patch whose every count was correct.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_patch_hunks", REPO / "scripts" / "shared" / "check_patch_hunks.py",
    )
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    problems = checker.check(PATCH)
    assert not problems, (
        f"{len(problems)} problem(s) in this patch's hunks:\n" + "\n".join(problems)
    )

    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "shared" / "verify_patch_applies.py"),
         str(PATCH)],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )
    assert proc.returncode == 0 and "exit: 0" in proc.stdout, (
        f"the patch does not apply to its own reconstructed minus side:\n"
        f"{proc.stdout}{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# The driver entry point the dispatcher calls
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def source() -> str:
    require(DRIVER)
    return DRIVER.read_text(encoding="utf-8", errors="replace")


def _poll_body(source: str) -> str:
    """The brace-balanced body of native_console_poll()'s DEFINITION."""
    body = _strip_c_comments(source)
    hit = re.search(
        r"native_console_poll\s*\(\s*void\s*\*\s*bytes\s*,\s*size_t\s*\*\s*size\s*\)\s*\{",
        body,
    )
    assert hit is not None, "native_console_poll is not defined with this signature"
    depth, i = 1, hit.end()
    while i < len(body) and depth:
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "native_console_poll's body is not balanced"
    return body[hit.end():i - 1]


def test_the_poll_entry_point_is_declared_for_its_callers(source):
    """Declared in the transport header, with the definition's exact signature.

    The caller is a kernel patch in bsd/kern/bsd_init.c, compiled as C.  A
    declaration that lives only in the .cpp is one the caller cannot see, and
    the patch would declare its own -- which is how the return type drifts, and
    a drifted return type is a different function.
    """
    require(HEADER)
    header = HEADER.read_text(encoding="utf-8")
    assert "native_console_poll" in header, (
        "NativeVirtioConsoleTransport.h does not declare native_console_poll; "
        "its caller in bsd/kern/bsd_init.c has no prototype to compile against"
    )
    # Whitespace removed from both sides: the declaration's spacing is the
    # author's, and a signature check that depends on it fails on a reformat.
    assert POLL_SIGNATURE in "".join(header.split()), (
        "the header's declaration does not match the definition's signature "
        f"({POLL_SIGNATURE}); the patch's caller compiles against the header"
    )
    # And the definition agrees with itself.
    definition = _poll_body(source)
    assert definition is not None, "native_console_poll is not defined"
    assert "native_console_poll" in "".join(_strip_c_comments(source).split()), (
        "the driver no longer defines native_console_poll"
    )


def test_the_poll_path_is_a_friend_of_the_class_whose_member_it_calls(source):
    """A free function calling a protected member must be declared a friend.

    ``receive()`` is protected, exactly as ``send()`` is.  Run 41313323081
    failed archive 6/11 with ``'send' is a protected member of
    'NativeVirtioConsole'`` and every source-level guard in this repository
    passed on the commit that caused it, because they read the function's SHAPE
    -- its name, its signature, its guard, its ordering -- and none of them read
    the class's access control.  So the assertion is on the class, not the
    function.
    """
    body = _strip_c_comments(source)
    definition = _poll_body(body)
    protected = set(re.findall(
        r"^\s*(?:IOReturn|bool|void|IOMemoryMap|IOLock|IOService)\s+"
        r"([A-Za-z_]\w*)\s*\(", body, re.M))
    called = {name for name in protected if re.search(
        r"g_console\s*(?:->|\.)\s*" + re.escape(name) + r"\b", definition)}
    assert called, (
        "the poll path calls no member of the console, so this test has nothing "
        "to guard; check whether native_console_poll was rewritten"
    )
    friends = set(re.findall(
        r"^\s*friend\s+((?:IOReturn|bool|void)\s+\w+\s*\([^)]*\)\s*);", body, re.M))
    normalized = {"".join(f.split()) for f in friends}
    assert POLL_SIGNATURE in normalized, (
        f"native_console_poll calls the protected member(s) {sorted(called)} "
        f"but the class does not declare it a friend with this signature. "
        f"Found: {sorted(normalized)}.  A friend naming a different parameter "
        f"list declares a different overload and grants nothing, so the driver "
        f"does not compile -- measured on run 41313323081, archive 6/11, with "
        f"'send' is a protected member."
    )


def test_the_poll_path_needs_no_open_client(source):
    """No ``isOpen()`` on the poll path.

    ``native_console_receive()`` refuses while ``isOpen(owner)`` is false and
    nothing on this board opens the port -- the only opener in the pinned tree
    is ``NativeOfflineGPU.cpp:132``, and that driver is not in the kernel four
    ways over.  A poll path that checked ``isOpen`` would be a second copy of
    the function that already could not serve a kernel-side consumer, and a
    kernel thread cannot become the owner anyway: ``IOService::open()``
    dereferences ``forClient->reserved->uvars``.
    """
    poll = _poll_body(source)
    assert "isOpen" not in poll, (
        "the poll path checks isOpen(); nothing on this board opens the port, "
        "so no guest-side consumer could ever read a byte through it"
    )


def test_the_poll_refuses_rather_than_dereferencing(source):
    """``kIOReturnNotOpen`` when there is no live console, and no bytes dropped.

    A caller that runs before ``start()`` or after ``cleanup()`` must be told,
    not silently handed zero bytes and not dereferenced.  This is what makes
    the return status the liveness test: ``kIOReturnNotOpen`` is how "the port
    is not there yet" is spelled, and it is distinguishable from "the symbol
    was never linked" precisely because the reference is strong.
    """
    poll = _poll_body(source)
    assert "kIOReturnNotOpen" in poll, (
        "the poll path does not return kIOReturnNotOpen when g_console is null"
    )
    assert "g_console?" in poll.replace(" ", ""), (
        "the poll path does not test g_console before receiving through it"
    )
    assert "receive(" in poll, (
        "the poll path does not put the bytes on the caller's buffer at all"
    )


def test_the_poll_entry_point_is_a_byte_transport_and_nothing_more(source):
    """The header keeps its stated contract: no message or GPU semantics.

    The transport header's first two lines are ``// Generic exclusive-owner
    kernel byte transport. No message/GPU semantics.``  Putting install-JSON
    knowledge in it would break that contract, and the discrimination would be
    unsound besides -- the GPU broker's frames are binary with a magic, so "not
    GPU magic therefore install" would silently reclassify every future
    protocol.  The JSON lives in the patch, and the header must not grow it.
    """
    require(HEADER)
    header = HEADER.read_text(encoding="utf-8")
    lines = header.splitlines()
    assert lines[1].strip() == (
        "// Generic exclusive-owner kernel byte transport. No message/GPU semantics."
    ), (
        f"the transport header's contract line has changed to {lines[1]!r}; "
        f"this component's stated contract is the reason the dispatcher lives "
        f"in an XNU patch instead of in the driver"
    )
    # What is banned is PROTOCOL knowledge, not the words.  The contract line
    # itself says "No message/GPU semantics" and the report entry point's own
    # comment names the mount it reports, so a word list would be satisfied by
    # the disclaimer it is meant to protect.
    declarations = header.split("//:", 1)
    assert len(declarations) == 2, (
        "the transport header no longer separates the bare declarations from "
        "the commented ones, so the region this test reads cannot be located"
    )
    for banned in ("install", "package_name", "package_sha256", "install.request",
                   BACKSLASH + '"event' + BACKSLASH + '":'):
        assert banned not in declarations[1], (
            f"the header's declarations now mention {banned!r}.  The transport "
            f"is a byte transport; putting install-JSON knowledge in it breaks "
            f"the component's stated contract, and the discrimination would be "
            f"unsound besides -- the GPU broker's frames are binary with a "
            f"magic, so 'not GPU magic therefore install' would silently "
            f"reclassify every future protocol."
        )


def test_the_workflow_that_can_verify_the_dispatcher_is_named():
    """``build-kernel-console`` cannot verify the dispatcher, and this says so.

    Build 41388754631 concluded **success** on the decl-order fix, and it
    verified nothing about the dispatcher.  Both of its jobs select
    ``qemu_defconfig``:

        python3 "$VENDOR_DIR/UniXNU/unixnu" defconfig qemu_defconfig ...

    ``qemu_defconfig`` does not set ``CONFIG_XNU_OWNER_ROOT_TRUST``, so the
    dispatcher's guard is false and the whole block is compiled out.  The kernel
    compiled and linked, the driver archive is on disk, and none of it touched
    the dispatcher.  Every one of the four defects so far came from the SECOND
    pass, ``qemu_root_defconfig``, which lives in ``build-macos``.

    **A green build only verifies the configuration it actually selected.**  The
    tests above already pin which defconfig enables the guard; what was missing
    is which WORKFLOW builds it, and that is the one that has to be dispatched.
    Reading a ``build-kernel-console`` success as "the dispatcher compiles" is
    the same shape as reading a passing guard test as "the constraint holds":
    the instrument never exercised the thing.

    The dispatcher has still never been compiled by any compiler.
    """
    console_wf = REPO / ".github" / "workflows" / "build-kernel-console.yml"
    macos_wf = REPO / ".github" / "workflows" / "build-macos.yml"
    require(console_wf, macos_wf)

    console = console_wf.read_text(encoding="utf-8")
    assert "defconfig qemu_root_defconfig" not in console, (
        "build-kernel-console now builds qemu_root_defconfig.  That workflow "
        "does not force-load the console archive on the link "
        "(-Wl,-force_load,<archive> is qemu_root_defconfig's, per its own "
        "comment), so a dispatcher compiled here would fail to link rather "
        "than being compiled out -- and the workflow's own stated reason for "
        "using qemu_defconfig no longer applies."
    )
    assert "defconfig qemu_defconfig" in console, (
        "build-kernel-console no longer builds qemu_defconfig, so the assertion "
        "above is measuring nothing"
    )

    macos = macos_wf.read_text(encoding="utf-8")
    assert "qemu_root_defconfig" in macos, (
        "build-macos no longer mentions qemu_root_defconfig, so NO workflow "
        "builds the config that compiles the dispatcher -- which means the "
        "dispatcher is compiled by nobody and install.registered can never be "
        "emitted, however correct its source is."
    )
