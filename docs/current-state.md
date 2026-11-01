# Current state — task, evidence, next actions, resume commands

**This file is the working state document.** It is updated as work proceeds, and
it is the first thing to read after a context compaction or a session restart.
The numbered facts behind every claim here live in
[`implementation-plan.md`](implementation-plan.md); this file says where things
stand and what to run next, not why.

Last updated: 2026-11-01 (mount reporter revision). Main repo at `5778bd4`.

---

## The one-paragraph summary

The virtio console channel now works in the guest→host direction, measured end
to end: the console driver binds to the agent channel's transport
(`native_console_ready=1`, `native_console_mmio=0xa003e00`), the announcement is
compiled into a real kernel, and the host received it on the wire. Frame
acquisition also works — one `pmemsave` reads the guest's framebuffer and all
480,000 pixels are byte-identical to QMP `screendump` — and the frame→window
path is now written and mutation-verified. What is not yet done is the
guest-side *consumer*: `NativeOfflineGPU`, the only code that reads the console
port, has never been linked into a kernel because `XNU_NATIVE_OFFLINE_GPU`
defaults to 0.

**Where the XNU records stand — and the defect that was hiding behind them:**
pinned at `c3c9ce2`/`2bff001`, and run 41281382063 concluded **success**, so all
ten records survive `-Werror` on the macOS runner (fact 225). Fact 227 then
proved them byte-level: the ESP kernel's sha256 equals `payload.json`'s
`payload_kernel_sha256`, and all five literals are in it at the offsets and
counts the source predicts.

**Then the guest disagreed, and that is fact 228.** The two IOKit records
arrived complete; all three XNU records arrived **seven bytes** each — the
capture ends with the literal bytes `{"event{"event{"event`. The record was
selected by a ternary passed into the emit macro, the two arms are arrays of
*different* lengths (`true` is 4 chars, `false` is 5), so the conditional
operator decayed both to `char const *` and `sizeof(arr)` measured the
**pointer**, 8 on AArch64. `sizeof(arr)-1` was 7.

**The build was green and the kernel grep was green.** The literals really are
in the kernel. Only the length argument was wrong, and it is computed at run
time — so the first instrument that could see the defect was the wire. The
reusable rule: *"the literal is in the kernel" is not "the record is sent
whole"*.

Fixed by selecting in an `if` so every arm names an array, with
`NATIVE_CONSOLE_PICK` deleted outright. Driver `a7b7a03` / UniXNU `8a223b2`,
both read back from api.github.com. Build **run 41296027165** concluded
**success** (34/34 steps), the capture landed 1492 bytes with all five records
complete, and the gate now reads **M4 PASS and M5 PASS** — fact 230, the first
time either has passed.

**What that did NOT buy, stated plainly:** clearing `xnu_initialised` moved
M6/M7/M8 to UNVERIFIED (blocker gone, evidence still absent), M9/M9a to
**FAIL on a PRD veto** (`m9.veto.homescreen_screenshot_only`,
`m9a.veto.wallpaper_only` — the gate refuses a screenshot as interaction
evidence), M9b to BLOCKED on a *different* blocker
(`system_volume_mounted`), and left M10/M10a/M11/M12 BLOCKED on
`guest_install_service`. Two passes, five different kinds of not-passed.

---

## What is measured and holds

| # | Claim | Evidence |
|---|---|---|
| 1 | The console driver **binds** to the agent channel's transport | fact 211 — `native_console_ready=1`, `native_console_mmio=0xa003e00` (the exact expected base), `native_console_failures=0`, on a guest that really booted |
| 2 | The announcement **is in a built kernel** | bundle13 ESP kernel, sha256 `dca5b3bb…` = `payload.json`'s `payload_kernel_sha256`; the literal at file offset 242876, NUL-terminated; bundle12 has 0 occurrences, bundle13 has 1 |
| 3 | The announcement **arrives on the wire** | fact 217 — 978 bytes, literal at byte 924, immediately after the bootloader's `ID_AA64PFR0_EL1` line, i.e. at the hand-off |
| 4 | The driver's own counters **agree** with what the host saw | fact 218 — `native_console_tx_requests=1`, `native_console_tx_bytes=54`, and 54 is exactly the announcement's length |
| 5 | The host **acquires a real frame** with one `pmemsave` | fact 213 — 1,920,000 bytes in 0.02 s from guest-physical `0x1BC7A0000`; 480,000/480,000 pixels byte-identical to `screendump` in BGRA |
| 6 | The host **can write guest memory** via gdbstub + PhyMemMode | fact 214 — baseline `m` answers `E14`; after `Qqemu.PhyMemMode:1` the same address returns the bytes `pmemsave` holds; an `M` write of `deadbeef` is read back by `pmemsave` |
| 7 | The frame→window path **is written and tested** | fact 216 — `framebuffer.py` + `surface_runner.py`; 4 and 7 mutants respectively, all killed |
| 8 | The **red main is fixed** | commit `7414bad`; preflight run 41262805198 green |
| 9 | The **pin chain is closed** | fact 215 — unipad `a4648c9` → UniXNU `04466512` → VirtioMmioConsole `61dc680`; the announcement is at lines 221-222 read back from GitHub |
| 10 | The **IOKit records reach the wire on a real guest** | fact 223 — `local-private/boot40/iokit-records/guest.log`, 1187 bytes, both records at lines 17-18, parsed with the gate's own criterion and scoped ONSITE |
| 11 | The **gap to M4+M5 is exactly the three XNU records** | fact 224 — the gate's own verdicts: M4 `UNVERIFIED` with `missing` = the three XNU requirements and nothing else; M5 `BLOCKED` by `xnu_initialised` with `missing=()` (its two IOKit requirements already pass, witnesses at `guest.log:17`/`:18`, both `onsite`) |
| 12 | The **pin chain for the XNU records closes on GitHub** | `51b7e04`'s lock → UniXNU `2bff001` (parent `29ed7e9`) → its `drivers/NativeVirtioConsole` gitlink is `c3c9ce2` (parent `d1ebb35`), and the lock's submodule entry agrees with the gitlink, so there is no divergence. All four SHAs read back from api.github.com |
| 13 | **The driver compiles under `-Werror` with all ten records** | fact 225 — run 41281382063, conclusion **success**, 34/34 steps: step 7 pin cross-check, step 14 macOS build, step 17 owner root chain, step 21 the second macOS build. This host has no C compiler, so step 14 is the first real compiler to see the macro, and it is the direct proof the adjacent-string-literal concatenation is right — the same macro that was wrong three times |
| 14 | The **M4/M5 baseline is unchanged since fact 224** | re-measured just now on `default_view()`: M4 `UNVERIFIED`, `missing=('m4.page_tables','m4.exception_state','m4.interrupt_state')` and nothing else; M5 `BLOCKED` by `xnu_initialised`, `missing=()` with both IOKit requirements satisfied and their witnesses still at `guest.log:17`/`:18`, `scope='onsite'` |
| 15 | **`xnu_initialised` gates SEVEN milestones, not two — fact 224 undercounted** | fact 226 — `_XNU_BLOCKER` has seven use sites in `milestones.py` (lines 2336/2367/2412/2457/2498/2543/2593), landing on M5, M6, M7, M8, M9, M9a and M9b. Evaluated M3-M12 in full: each of those seven is `BLOCKED` with `unmet_blockers` containing only `xnu_initialised` (M9b also `system_volume_mounted`). So the three records pay once and move **eight** milestones — M4 completes, seven unblock |
| 16 | **All five records are in the bytes of the kernel the guest boots** | fact 227 — artifact `13074462047`'s ESP kernel, 125,480,960 bytes, sha256 `359eba3f…` **equal** to `payload.json`'s `payload_kernel_sha256`; `iokit.service_match` at 242932 and `iokit.driver_probe` at 243047 (1 each), `xnu.page_tables` at 243159 (1), `xnu.exception_vectors` at 243244 (**2**), `xnu.interrupts` at 243439 (**4**). The instrument refuses to report unless the two hashes agree. The counts were predicted from the source first: two static strings per fact, and four DAIF × CNTFRQ combinations because the gate keeps only the first record per event. **Corrected by fact 228: this proves the literals are in the kernel, NOT that they are sent whole** |
| 17 | **The kernel was obtained without transferring 310 MB** | fact 227 — the member is 78,650,816 bytes DEFLATED over `[2,354,995, 81,005,811)`, a range the partial straight download already covered, so extraction moved `fetched 0 bytes`. The extractor was validated offline first against `bundle14.zip` (a complete zip whose ESP kernel is already on disk), reproducing that known kernel byte for byte; its first live run **refused**, correctly, because it had been handed one artifact's bytes against another's central directory |
| 18 | **M4 and M5 PASS — the first time either has** | fact 230 — gate on `default_view()`: M4 `PASS` with `satisfied:m4.page_tables/m4.exception_state/m4.interrupt_state/m4.failure_forensics`, M5 `PASS` with `satisfied:m5.service_matching/m5.driver_probe`. Witnesses the gate itself cites: `local-private/boot41/xnu-records/guest.log:20:xnu.page_tables`, `:21:xnu.exception_vectors`, `:22:xnu.interrupts`, all `kind='guest_log'`, `scope='onsite'`. Capture 1492 bytes, sha256 `7fba8ed0…` |
| 19 | **The fixed kernel is a different kernel from the defective one** | fact 230 — ESP kernel sha256 moved `359eba3f…` → `227fc03b…`, both equal to their own `payload.json`, and all five record literals sit at the **same** offsets and counts in both (242932/243047/243159/243244×2/243439×4), because the fix changed control flow and not the strings |
| 20 | **The `xnu_initialised` records were sent seven bytes long — fact 228** | driver `a7b7a03` / UniXNU `8a223b2` / run 41296027165 — the two IOKit records arrived complete and all three XNU records arrived as the 7 bytes `{"event`, because the record was selected by a ternary whose two arms are arrays of *different* lengths, so the conditional operator decayed both to `char const *` and `sizeof(arr)` measured the pointer. The build was green and a byte-level grep of the kernel was green: the literals really are in the image. Only the length argument was wrong, and it is computed at run time |


## What is NOT done, and why

| # | Gap | Why it is still open |
|---|---|---|
| A | **No guest-side consumer in the kernel.** `NativeOfflineGPU` is the only code that reads the console port, and it is source-only | fact 219 — `XNU_NATIVE_OFFLINE_GPU` defaults to 0 in `build-xnu-4k-baseline.sh:306`; `board.toml`'s `[kernel]` section has only `defconfig` and `archives`; all ten `native_gpu_broker_*` symbols are absent from the kernel |
| B | **No guest agent for install/launch/persist.** The host's `protocol.py` has all six ops; nothing in the guest reads them | `grep -rln native_console_receive` over `vendor/UniXNU/` returns only `NativeOfflineGPU.cpp` and the console driver itself |
| C | **The host→guest arrival question is unmeasured** | fact 218 — the RX counters count pulls, not arrivals; the arrival round read the wrong register offsets (the queue rings are at 0x80/0x90/0xa0, not 0x30-0x44) |
| D | **No real IPA has been installed, launched or persisted** | Blocked by A and B |
| E | **No SpringBoard interaction evidence** | Blocked by D |
| F | **No audio, network, launcher or performance acceptance** | Not started |
| G | **M4 and M5 now PASS; the rest of the XNU chain is a different problem** | fact 230 — the three XNU records are on the wire whole and M4/M5 read PASS. What remains is NOT this chain: M6/M7/M8 need their own guest evidence, M9/M9a need real interaction evidence (a screenshot is vetoed by name), M9b needs `system_volume_mounted`, and M10/M10a/M11/M12 need `guest_install_service` |
| H | **CLOSED — the three XNU records moved M4 and M5 to PASS** | fact 230 — fact 224 said nothing else stood between the records and M4+M5, and that held. Fact 226 widened the claim to eight milestones, and fact 230 shows what that widening was worth: **two PASS, three UNVERIFIED, two FAIL-on-veto, one BLOCKED on a different blocker**. The eight-milestone figure was right about the lever and wrong if read as eight passes |

## Active work

**M4 and M5 PASS.** That is the headline and it is worth being precise about
what produced it: three XNU records on the agent channel, complete, from a
kernel whose sha256 ties to its own `payload.json`, with witnesses the gate
itself cites at `guest.log:20/:21/:22` all scoped `onsite` (fact 230).

**And it took a defect to get there.** The records were in the kernel the whole
time — a byte-level grep found all five, the build was green — and they were
still sent seven bytes each, because the emit macro was handed a ternary and
`sizeof` measured the pointer instead of the array (fact 228). The reusable
rule is in the instrument lessons: *"the literal is in the kernel" is not "the
record is sent whole."*  A green kernel check is a necessary result, never a
sufficient one.

**What "unblocked" actually bought, since it is easy to over-read:** two PASS,
three UNVERIFIED (M6/M7/M8 — blocker gone, evidence still absent), two FAIL on
a PRD veto (M9/M9a — a screenshot is refused by name), one still BLOCKED on a
*different* blocker (M9b → `system_volume_mounted`), and four still BLOCKED on
`guest_install_service` (M10/M10a/M11/M12).

**The next real blocker is `system_volume_mounted`, and it is not a driver
fix.** The gate wants a writable system volume in the guest; this host has no
APFS read/write implementation. `guest_install_service` (gap B) is independent
of it and can run in parallel.

| Level | Repo | Branch | Commit |
|---|---|---|---|
| driver fork | `James081105/VirtioMmioConsole` | `unipad/console-announcement` | `ddeb315` |
| UniXNU fork | `James081105/UniXNU` | `unipad/gpu-addon-ngpu-dir` | `c0c6c4d` |
| main repo | `James081105/unipad` | `main` | `5778bd4` |

Build **run 41313323081** was dispatched 2026-11-01 on `5778bd4`, experiment
`exp-2026-11-01-mount-report`, stage M7, payload target tcg. It is the first
build of the mount reporter; see "What is unverified" below.

### What this revision changed and why the M9b blocker's own words were wrong

The `system_volume_mounted` blocker's reason text is an APFS narrative — "this
host has no APFS read/write implementation, so the 6.08 GB os-root raw image
cannot be produced" — but **that is not the condition the gate checks.** The
blocker's `satisfied_by` is `system_volume_mount`, which is
`_onsite_ok(snapshot, "p.system_mount")`, which is the guest event
`system.mount`. So the blocker is cleared by putting a record on the guest
event channel, not by implementing APFS. The APFS sentence describes a real
limitation of this host and is not what gates the milestone.

The emitter added in this revision clears **three** requirements with two
records: M7's `m7.mount_relations` (`system.mount`), M7's
`m7.volume_identity` (`system.volume_identity`), and the M9b blocker
(`system.mount` again).

### What is unverified (do not read the commits as evidence)

The emitter is CODE COMPLETE and guard-tested on Windows. It has not been
built, and nothing has reached the guest channel. In particular:

- Whether the two static strings survive the kernel link at all is unmeasured.
- The previous pin on this same driver lost three records to a
  `sizeof`-of-a-decayed-pointer defect that was invisible to the build, to a
  byte-level grep of the kernel, and to every source-level guard. The same
  class of defect is possible here. Only the wire answers.
- `dyld.cache_read` — M7's third requirement — is **not** covered by this
  emitter and is the remaining piece of M7.

## Next actions, in order

1. **Read run 41313323081's boot-inputs bundle and re-capture the guest.**
   `local-private/tmp/root-mount-capture.py` pulls the bundle; the capture is
   what tells us whether `system.mount` reached the channel whole or not at
   all. Then re-read the gate. This is the same cycle that produced M4/M5.
2. **`dyld.cache_read` — M7's third and last requirement.** Not covered by the
   emitter in this revision. It is the only thing between M7 and a pass, given
   the other two records land.
3. **`guest_install_service` (M10/M10a/M11/M12) — gap B, and independent.** No
   guest agent reads the host's six install/launch/persist ops. This can be
   pushed in parallel and does not wait on anything above.
4. **M9/M9a need real interaction evidence, and the veto names what will not
   count.** `m9.veto.homescreen_screenshot_only` and
   `m9a.veto.wallpaper_only` mean a screenshot — however good — can never
   satisfy them. What they want is input reaching the guest and the guest
   responding.
4. **M6/M7/M8 are UNVERIFIED with their blockers gone.** They need their own
   guest evidence: `m6.known_content_read_back`, `m7.volume_identity`,
   `m7.mount_relations`, `m7.dyld_cache_read`, `m8.original_launchd`,
   `m8.later_daemons`. Several of these fall out of step 1 for free.
5. **The broker, if the render path is next.** That is the Part A + Part B +
   Part C change fact 221 describes, and it is a device-repo re-pin.
6. **Not started at all:** audio, network, launcher, performance acceptance
   (gap F).

### What just finished, for the record

Build run 41296027165 concluded success (34/34 steps) on `3b6bbd4`. The bundle
was staged by `local-private/tmp/stage-bundle-from-run.py 41296027165
local-private/tmp/bundle16` — one command, nine members, every one verified
against both `payload.json` and the artifact's own `manifest.json`. The capture
wrote 1492 bytes to `local-private/boot41/xnu-records/guest.log` (sha256
`7fba8ed0…`) with all five records complete, and the gate reads M4 PASS and M5
PASS. The defective capture is kept beside it as
`capture-defect-228-seven-byte-records.log`, deliberately named so it matches
none of the gate's `_GUEST_LOG_GLOBS` — it is evidence, not input.

## Resume commands

Everything below runs from `F:\iosvirtual`. `bundle13` is the boot-inputs bundle
whose kernel carries the announcement; it is already extracted at
`local-private/tmp/bundle13`.

```bash
# Byte-level proof the records are in the kernel the guest boots.
python -u local-private/tmp/verify-records-in-kernel.py local-private/tmp/bundle15-check

# Pull ONE member out of the artifact by byte range (no 310 MB download).
python -u local-private/tmp/extract-one-member.py local-private/tmp/bundle15.zip \
  "payload-esp/EFI/UniXNU/Payloads/kernel.macho" local-private/tmp/b15-kernel.macho

# ...and validate the extractor offline against a known answer first.
python -u local-private/tmp/validate-extractor.py

# Re-read the gate after a capture lands (M3..M12, with the blockers).
python -u local-private/tmp/reread-gate.py

# Stage a whole verified bundle from a build run, in one command.
python -u local-private/tmp/stage-bundle-from-run.py 41296027165 local-private/tmp/bundle16

# The XNU records, on the committed (LF) blob -- that is what CI compiles.
git -C vendor/UniXNU/drivers/NativeVirtioConsole show   c3c9ce2:NativeVirtioConsole.cpp > local-private/tmp/committed-driver.cpp
python local-private/tmp/verify-xnu-records.py

# Re-run the announcement probe (proves guest->host on a fresh guest).
cd F:/iosvirtual
rm -rf local-private/boot40/announce
timeout 1500 python -u local-private/tmp/announce-probe.py local-private/tmp/bundle13

# Re-run the RX counter probe (proves the counters count pulls, not arrivals).
rm -rf local-private/boot40/rx-probe
timeout 1500 python -u local-private/tmp/rx-probe.py local-private/tmp/bundle13

# Re-run the frame acquisition probe (proves pmemsave reaches the framebuffer).
rm -rf local-private/boot40/framebuffer-acquire
timeout  900 python -u local-private/tmp/framebuffer-acquire.py local-private/tmp/bundle13

# The full unit suite, with a real summary (the pipe eats it otherwise).
timeout  900 python -m pytest tests/unit -q --no-header --tb=no -p no:randomly \
  > local-private/tmp/unit-suite.txt 2>&1; tail -2 local-private/tmp/unit-suite.txt

# Regenerate the coverage matrix (docs/coverage-matrix.md is its output).
python scripts/windows/build-coverage-table.py --output local-private/tmp/coverage-new.md
```

## Instrument lessons that cost time here

Recorded so the next run does not repeat them:

- **A missing ESP member is silent and fatal.** `scratch-data.img` and
  `kernel.macho.trustcache` are both absent from a fresh `boot-inputs` bundle by
  design; without them the guest sits in the UNINU boot menu with `qemu=running`
  and the channel connected. Facts 206 and 207.
- **`qemu=running` plus a connected channel is never proof the kernel ran.**
  Read the serial log for `Exiting boot services`. Fact 207.
- **The GDB remote protocol's `m`/`M` length field is hexadecimal.** Sending
  `,32` asks for 0x32 = 50 bytes, which made a correct comparison report False.
  Fact 214.
- **A probe address that is all zero makes "agrees with pmemsave" true by
  construction.** Facts 208 and 209.
- **`cpu_ttep` is still zero at the hand-off.** The bootloader's hand-off line
  precedes the kernel installing its own page tables. Wait on `cpu_ttep`, not on
  a flat budget. Fact 219.
- **The virtio-mmio queue rings are at `base+0x80/0x90/0xa0`**, not
  `base+0x30..0x44` (those are QueueSel/QueueNum/QueueSize/QueueReady). Fact 219.
- **The kernel holds the RESOLVED string, so grep it with plain quotes.** The C
  source spells the quotes inside a string literal as backslash-quote
  (``\"event\":\"xnu.page_tables\"``); the compiler resolves those escapes.
  A needle built the way the source spells it reported all five records ABSENT
  in a kernel that carries two of them. Validated against a known answer first:
  bundle14's ESP kernel (sha256 `620c1acf…`, equal to `payload.json`) has
  `iokit.service_match` at byte 242932 and `iokit.driver_probe` at 243047, one
  occurrence each, and the three XNU records absent — which is correct, because
  bundle14 was built from `d1ebb35`, before `c3c9ce2` added them.
  `local-private/tmp/verify-records-in-kernel.py` is that instrument.
- **"The literal is in the kernel" is NOT "the record is sent whole."**
  Measured 2026-11-01, and it is the most expensive lesson here. A byte-level
  grep of the ESP kernel found all five record literals with the right counts,
  the sha256 tied to `payload.json`, the build was green under `-Werror` -- and
  the three XNU records still arrived seven bytes each on the wire, because the
  length argument was `sizeof` of a decayed pointer. The image cannot answer
  this question; only the capture can. So a green kernel check is a NECESSARY
  result, never a sufficient one.
- **`sizeof` on a ternary over arrays measures the pointer, not the array.**
  The two arms must be the same length for the conditional operator to keep an
  array type, and `true`/`false` are not. Passing such an expression to a macro
  that does `sizeof(arr)-1` silently sends `sizeof(char const*)-1` = 7 on
  AArch64. Select in an `if` so every arm names an array.
- **A guard test must not report its own documentation as a defect.** The new
  records test quotes the old broken line in a comment so the next reader knows
  what it replaced, and a raw scan reported that quotation as a live call site.
  `_strip_comments()` (literal-aware, because the records are made of quotes and
  escapes) is what separates them. A test that goes red on its own prose is
  worse than no test.
- **A submodule's worktree is CRLF while its commits are LF.**
  `vendor/UniXNU/drivers/NativeVirtioConsole` has `core.autocrlf=true`, so
  `git ls-files --eol` reports `i/lf w/crlf`: the committed blob is LF and the
  checkout on disk is CRLF, and git normalizes on commit. So a byte-for-byte
  diff between the worktree and `git show <sha>:<file>` reports every line as
  different when the content is identical. Verify against the committed blob
  (`git show <sha>:<file>`), because that is what CI compiles.
- **GitHub artifact pre-signed URLs expire mid-download and curl exits 0 on the
  truncated result.** Use `local-private/tmp/fetch-artifact.py`, which resumes
  with a fresh URL per attempt. Measured three times.
- **A 310 MB artifact does not have to be downloaded to read one member of it.**
  The pre-signed URL honours Range (measured), the zip's central directory gives
  every member's exact compressed byte range, and deflate inflates locally with
  `zlib.decompress(data, -15)`. `local-private/tmp/extract-one-member.py` pulls a
  single member; the ESP kernel cost **zero** extra bytes because the partial
  download already covered its stream. **Validate the extractor offline against a
  complete zip whose answer is already on disk before trusting it on the
  network** — and read a refusal as the guard working: its first live run refused
  because it was handed one artifact's bytes with another artifact's central
  directory, and the two place the same member at different offsets.
- **The proxy at `127.0.0.1:7897` cannot reach `*.blob.core.windows.net`.**
  `curl -v` shows the CONNECT tunnel establish and the TLS handshake inside it
  fail; `-k` does not help; plain HTTP returns 502. It works for
  `api.github.com`, `github.com` and `objects.githubusercontent.com`. So a
  suggested proxy is not automatically usable — test it against the actual host
  before planning around it.

## Facts established 2026-11-01 (mount reporter revision)

- **A blocker's reason text is not the blocker's condition.** The
  `system_volume_mounted` blocker reads as an APFS problem and is actually a
  missing guest event. Read the `satisfied_by` and follow it to the predicate
  before planning work against the prose.
- **A macro that expands to a string literal contributes its CONTENT to
  adjacent-literal concatenation, not its quotes.** `"\"volume_uuid\":"`
  followed by a macro holding `8846FE78-...` yields `"volume_uuid":8846FE78-`
  — an unquoted JSON value. The quotes must be adjacent literals of their own.
  Both this and a dropped key compiled clean, survived a grep for the event
  name, and read on the host as "the guest never mounted".
- **`sizeof`-style escaping bugs are invisible to the compiler and to a grep.**
  The previous pin lost three records to `sizeof` of a decayed pointer; this
  revision's two defects were of the same family. Build a parser for the
  artifact the toolchain will actually produce — here, expanding the macros and
  `json.loads`-ing the result — before writing the guard test.
- **`_guest_events` merges across files with `setdefault`.** Only the FIRST
  record per event name is kept, so a second emission of the same event is
  silently dropped. Relevant to any emitter that reports more than one instance
  of a thing.
- **`weak_import` is the established idiom for an XNU patch referencing a
  separately-switched driver archive.** `zzy-native-gpu-addon-mount.patch:76`
  is the precedent; the console archive is behind
  `CONFIG_XNU_NATIVE_VIRTIO_CONSOLE`. A weak import plus a null check at the
  call site means the emitter links green with the symbol absent — which is why
  the driver commit and the emitter commit are pinned together, since the build
  cannot tell the two apart.
- **`PERSONALITY_CFLAGS` only reach one translation unit.** They land in
  `KernelConfigTables.cpo_CFLAGS_ADD`; per-object flags for `kern_exec.o` come
  from `build-xnu-4k-baseline.sh:466`'s `kern_exec.o_CFLAGS_ADD=...`. So an XNU
  patch cannot test `CONFIG_XNU_NATIVE_VIRTIO_CONSOLE` from `kern_exec.c`.
- **`OWNER_UNION_SYSTEMOS` defaults to 0** (`build-xnu-4k-baseline.sh:438`), so
  the `#else` arm is live and the mountpoint is `/private/preboot`. A guard test
  reading the FIRST of two `#define XNU_OWNER_CACHE_MOUNTPOINT` arms measures
  the wrong one; take the last.
- **A hunk count and a hunk start are two different defects.** Growing a `+`
  side without updating `@@ -a,b +c,d @@`'s `d` fails `git apply`; shifting
  later hunks' new-side starts fails the unreachable check. Three pre-existing
  tests went red on one edit for exactly these two reasons, and the diagnosis
  was a before/after hunk-header comparison, not a guess.
- **`_call_arguments()` returns only the text BETWEEN the parentheses.** For a
  function definition that is the parameter list, not the body. Assertions
  about what a function DOES need a brace-balanced body extractor.
- **`/f/unipad-tmp` had grown to 45 GB of named scratch across 670 entries**,
  leaving F: at 6.7 GB free — which is not enough for a 310 MB boot-inputs
  bundle plus its extraction. Reclaimed to 30 GB by deleting the stale
  `pytest-*` basetemp dirs, never `shell/` and never `pytest-of-*`.

### Correction recorded after the first commit of this revision

The mountpoint assertions in `tests/unit/test_pinned_patches_apply.py` were
wrong for a whole revision and nothing went red. They took whichever
`#define XNU_OWNER_CACHE_MOUNTPOINT` the patch carried LAST — the `#else`
arm, `/private/preboot` — on the reasoning that `build-xnu-4k-baseline.sh:438`
defaults `XNU_OWNER_UNION_SYSTEMOS` to 0 and nothing overrides it.
**`qemu_root_defconfig:36` does override it**, and that config is the one that
matters: `build-macos.sh:2590-2596` selects it for the second pass, and
`build-macos.sh:2930-2940` says in those words that the second pass's kernel is
the one the ESP carries. So the booting kernel is built with the union arm, the
mount overlays `/`, and the record was being checked against a mount point this
board never uses.

The emitted record was never wrong — the macro resolves at compile time either
way, so it says `/` and the mount is at `/`. What was wrong is that nothing
checked the record against the build. The fix derives the expected mount point
from the defconfig, and three new assertions cover the arm structure, the
defconfig going silent, and each arm naming its own branch's mount point.

**The generalisable lesson, and it is the third time in this project:** an
assertion that reads its expectation from the same source as the value under
test agrees with itself and measures nothing. The `uuid` mutation taught it
first (the test substituted its own UUID), the mountpoint taught it second,
and the fix is the same both times — derive the expectation from something the
value does not control.

### Byte-level verification of the mount reporter's premises (before the build lands)

Measured on the kernel the guest actually boots —
`local-private/boot42/ring-boot/esp/EFI/UniXNU/Payloads/kernel.macho`,
125,480,960 bytes, the same size fact 227 recorded — with Python's
`bytes.count()`, not `grep -c`, because `grep -c` on a binary counts LINES and
reported `system.mount` as present when it is absent:

| string | count | what it settles |
|---|---|---|
| `8846FE78-2910-4297-AF97-D59BB6F8FC16` | **1** @ 808230 | the functional mount's `const char *volume = XNU_OWNER_CACHE_VOLUME_UUID` really is compiled into the booting kernel |
| `DYLD_SHARED_CACHE_DIR` | **0** | the non-union arm is NOT live, so the mount point is `/` |
| `system.mount` | **0** | this kernel predates the emitter — the honest baseline |
| `native_console_report` | **0** | ditto; the symbol is not linked into this kernel |

The `DYLD_SHARED_CACHE_DIR` result is the decisive one for the mountpoint
question. That literal is compiled **only** inside
`#if !defined(XNU_OWNER_UNION_SYSTEMOS) || !XNU_OWNER_UNION_SYSTEMOS`, so its
absence from the booting kernel is a byte-level proof that the union arm is the
live one and the mount overlays `/`. The config evidence
(`qemu_root_defconfig:36`) and the byte evidence now agree.

And the mount the emitter reports is identifiable in the guest's own ring, at
`ring-after.log:323`:

```
[   35.314745]: apfs_log_op_with_proc:3091: disk4s1 mounting volume
  DawnUpdate21H461.J120J121J207J208SystemCryptex, requested by: init (pid 1);
  parent: kernel_task (pid 0)
[   35.316584]: handle_mount:817: disk4s1 vol-uuid: 8846FE78-2910-4297-AF97-D59BB6F8FC16
```

`requested by: init (pid 1); parent: kernel_task (pid 0)` is `bsdinit_task()`,
which is exactly where the patch puts `native_owner_cache_prepare()`. So the
log line IS the mount the record reports, and the vol-uuid it names is the one
the record carries.

**The ordering risk is closed, and it was a real risk.** If
`native_owner_cache_prepare()` ran before `NativeVirtioConsole::start()`, then
`g_console` would still be null, `native_console_report` would return
`kIOReturnNotOpen`, and both records would be dropped with a green build — the
same silent failure shape as fact 228. It does not: the console's own records
sit at `guest.log:19-23`, immediately after the EFI hand-off's "Exiting boot
services", and the mount is 35 seconds later.

### Run 41313323081 FAILED, and the failure is the point

Build `exp-2026-11-01-mount-report`, stage M7, target tcg, dispatched on
`5778bd4`. Steps 1-13 all succeeded — including step 7's pin cross-check, which
means the driver fork commit, the UniXNU gitlink and the lock's two pins all
agreed and the runner fetched exactly what was pinned. Step 14, the first
`build-macos.sh` pass, failed at archive 6/11:

```
drivers/NativeVirtioConsole/NativeVirtioConsole.cpp:119:33: error: 'send' is a
protected member of 'NativeVirtioConsole'
  119 |     return g_console?g_console->send(bytes,size):kIOReturnNotOpen;
NativeVirtioConsole.cpp:57:14: note: declared protected here
   57 |     IOReturn send(const void *,size_t);
```

`send()` is protected. `native_console_report()` is a free function, so it
cannot call it; `native_console_send()` and `native_console_receive()` can,
because lines 37-38 declare them friends of the class. The fix is the same one
line for the new entry point.

**What this failure proves about the guard tests, and it is the important
part.** Five assertions guarded `native_console_report` — declared for its
callers, needs no open client, refuses rather than dereferencing, published
only after the port is really there, withdrawn when the device is stopped. All
five passed. All five had been mutation-checked against five separate defects,
all killed. And the driver did not compile.

They read the function's SHAPE. None of them read the class's ACCESS CONTROL,
which is the one thing a free function reaching a protected member depends on.
This is fact 228's lesson in a second costume: *a source-level guard tests the
source, and a compile error is not in the source.*

The new assertion is deliberately on the class, not the function: for every
protected member the report path actually calls, the class must declare
`native_console_report` a friend **with this signature**. Signature, not name —
a friend naming a different parameter list declares a different overload and
grants nothing, which the `friend-misspelled` mutation confirmed by surviving
the first revision of the assertion.

Failure evidence is preserved at
`local-private/ci-evidence/run-41313323081-step14-failure.log` and
`...-archive-06.log` (gitignored, per PRD 12).

**Steps 15-21 did not run.** So this build tells us nothing about whether the
emitter's records reach the guest channel. That question is still open, and
the next run is what answers it.

### The guest-event evidence path has no production caller — found while mapping M10

`AgentChannelTransport.listen()` (`src/unipad/guest/agent_channel.py:271`) is the
only thing that reads the virtio-serial port and writes a `guest.log`. **It has
no caller anywhere in `src/`.** The production path is
`GuestObserver.kernel_log()` (`src/unipad/host/guestobserve.py:863`), which reads
`msgbufp`'s ring buffer through QMP and writes `guest-kernel.log`
(`host/unixnu_boot.py:2719`, `:2976`). That file contains no virtio-serial bytes.

So every guest event this project has ever satisfied a requirement with — the two
IOKit records that carry M4/M5, and the three XNU records behind them — reached a
`guest.log` through a **manual, out-of-tree capture script**, not through the
product. `guest-kernel.log` matches `_GUEST_LOG_GLOBS`'s `**/*guest*.log`, so the
gate reads it; it is simply the wrong bytes.

Two consequences, and both are load-bearing:

1. **Re-reading the gate is not enough after a build.** The boot-inputs bundle
   must be pulled, the guest booted, the console port drained by the capture
   script, and the resulting `guest.log` left where the gate can see it. A green
   build plus a green preflight says nothing about any milestone.
2. **This is a product gap, not a measurement shortcut.** M10's
   `install.registered` and every later guest event would hit the same wall. The
   fix belongs in `unixnu_boot.py`, not in another capture script, and it is
   worth doing before the next emitter is written rather than after.

### The guest channel's log now has a name the gate reads

`unixnu_boot.py` wrote the agent chardev to `agent.log`. QEMU's `logfile=`
captures everything the guest writes to the virtio-serial port, which is
where every guest event record goes — so the records were on disk after
every boot, under a name `_GUEST_LOG_GLOBS` does not match.

The evidence that this mattered is not theoretical. The capture that
satisfied M4/M5 came from a manual out-of-tree script calling
`AgentChannelTransport.listen()`, and `listen()` has no production caller
anywhere in `src/`. Meanwhile
`local-private/boot42/ring-boot/agent.log` already held all five records,
unread, at 1476 bytes.

Renamed to `guest-channel.log`, which matches the gate's existing
`**/guest-*.log` and `**/*guest*.log` — so no acceptance criterion moves and
no new glob is admitted. The new test asserts against `_GUEST_LOG_GLOBS`
rather than against a string the test happens to know, and was verified by
mutating the name back: red on `agent.log`, green on `guest-channel.log`.

**Inherited constraint, restated because it has the same failure shape as a
missing channel:** QEMU refuses the entire chardev when the logfile path
contains characters it cannot create, and this host's `%TEMP%` has a
full-width parenthesis in it (implementation-plan.md's fact 203 measured
`Could not create '.../agent.log': Invalid argument`). The symptom is
`connect refused`, identical to "no boot wants this channel". The rename
does not touch the directory, so it is not newly exposed.

### Run 41316953324 SUCCEEDED — 37/37 steps, both kernel passes

Build `exp-2026-11-01-mount-report-fix`, stage M7, target tcg, dispatched on
`573697c`. The one-line `friend` fix from run 41313323081's failure was enough.

Both `build-macos.sh` passes ran clean:

| pass | config | ESP kernel | sha256 |
|---|---|---|---|
| first (`macos-build.log`) | `qemu_defconfig` | 51,859,456 B | `6082be80…` |
| second (`macos-build-root.log`) | `qemu_root_defconfig` | 125,480,960 B | `e836fd58…` |

`stages: ran=9 not_run=0 failed=0 skipped=2` on the second pass.

**The second pass's kernel is the one the guest boots**, and its size,
125,480,960 bytes, is byte-for-byte the size of the kernel already measured
locally at `local-private/boot42/ring-boot/esp/EFI/UniXNU/Payloads/kernel.macho`
— the kernel whose ring showed the SystemCryptex mount at t=35.314745 and whose
`DYLD_SHARED_CACHE_DIR` count is 0. That closes the loop between the config
evidence, the byte evidence and the build.

**What this proves and what it does not.** It proves the tree compiles and
links under `-Werror` with the emitter in it. It does not prove the records
reach the guest channel — that was the previous pin's whole lesson (fact 228:
the build was green, the kernel grep was green, and three records arrived seven
bytes long). The boot-inputs bundle is downloading; the capture is what answers.

**A separate blocker surfaced while regenerating the coverage matrix, and it
gates this work.** `docs/coverage-matrix.md` could not be regenerated at all:
`_adopt` in `src/unipad/acceptance/probes.py` rebuilt each `EvidenceRef` from
scratch and hardcoded `scope=SIMULATION`, so `write_bundle` refused every
on-site PASS — M4 measured `scopes=['simulation']` against
`required_scope=onsite`. Fixing that exposed a second dropped field
(`witness`), which raised "on-site evidence requires a witness only the target
could produce" and turned M4 into `probe_error:M4` → FAIL. Both are fixed and
guard-tested.

The regenerated matrix reveals the chain this work actually has to clear:

    M6 (virtio_blk.read_back)  ->  M7 (mount records)  ->  M8
                                    \-> M9b (system_volume_mounted)

So `system.mount` + `system.volume_identity` satisfy M7's two evidence
requirements, but M7 stays BLOCKED until **M6's `virtio_blk.read_back` event
also lands** — and M9b needs M7 *and* the mount. That is a requirement the
previous revision of the plan did not name.
