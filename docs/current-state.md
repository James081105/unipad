# Current state — task, evidence, next actions, resume commands

**This file is the working state document.** It is updated as work proceeds, and
it is the first thing to read after a context compaction or a session restart.
The numbered facts behind every claim here live in
[`implementation-plan.md`](implementation-plan.md); this file says where things
stand and what to run next, not why.

Last updated: 2026-11-02 (inter-patch constraint corrected; the console
declaration moved out of the mount's guard; `dyld.cache_read`,
`virtio_blk.read_back` and the M8 pair all CODE COMPLETE but unpinned, pending
the drain measurement). Main repo at `3bf6fe9`.

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

1. **Read build 41364602699** (pin `8e492f5`, the declaration fix). It is the
   one that should finally compile AND run the diagnostic. Fetch the boot inputs
   with `python -u local-private/tmp/fetch-boot-inputs-run.py 41364602699` and run
   `UNIPAD_RUN_ID=41364602699 python -u local-private/tmp/root-mount-capture.py`,
   whose `REQUIRED_EVENTS` already includes `system.mount` and
   `system.volume_identity`. Then read the gate with
   `python -u local-private/tmp/read-gate.py`. **Look for
   `diag.load_init_program_entered` first** — that record, not the mount records,
   is what this build was dispatched to measure.
   - **It arrives** → the port works from `kern_exec.c`, and the question moves
     to why the prepare call below it does not run.
   - **It does not** → the port is unreachable from this translation unit and the
     emitter needs a different route entirely.
   - **The guest hangs, or the log is far shorter than t=1500 s** → that is the
     DRAIN. `send()` is a synchronous round trip with a two-second deadline, so
     measure how many records arrived before it stalled rather than treating it
     as a boot failure. This is the risk the three unpinned emitters carry.
2. **`dyld.cache_read` — M7's third requirement. IN FLIGHT** (worktree branch
   `dyld-cache-read-emitter`). Mapped site: `cs_native4k_prepare()` at
   `bsd/kern/ubc_subr.c:5765`, the read at `:5818`, `error = 0` at `:5858`.
   `native-4k-signature-hash-cache.patch` already touches that file. Known
   caveat: the function has no path knowledge, so the record cannot name a
   directory — emit only what the function holds.
3. **M6's `virtio_blk.read_back`. IN FLIGHT** (worktree branch
   `virtio-blk-readback-emitter`). Mapped site: `vr_verify_block`'s loop at
   `NativeVirtioBlock.cpp:435-444`, content verified against the owner-signed
   manifest's per-block digest table. Needs a NEW driver fork. **Emit that one
   event and nothing else under the `virtio_blk.` prefix** — the
   `device_enumeration_without_readback` veto at
   `milestones.py:1719` is dormant today (zero `virtio_blk.*` emitters exist) and
   any second event arms it. Ordering risk: the block driver's `start()` may run
   before the console's, and `if (native_console_report)` is not an acceptable
   answer to that.
4. **`guest_install_service` (M10/M10a/M11/M12) — gap B, and independent.** No
   guest agent reads the host's six install/launch/persist ops. This can be
   pushed in parallel and does not wait on anything above. The host half is
   complete and `native_console_receive` works, but the port is exclusive-owner
   (`NativeOfflineGPU.cpp:132` is the only `open()`) and there is no dispatcher.
5. **M9/M9a need real interaction evidence, and the veto names what will not
   count.** `m9.veto.homescreen_screenshot_only` and
   `m9a.veto.wallpaper_only` mean a screenshot — however good — can never
   satisfy them. What they want is input reaching the guest and the guest
   responding.
6. **M6/M7/M8 are UNVERIFIED with their blockers gone.** They need their own
   guest evidence: `m6.known_content_read_back`, `m7.volume_identity`,
   `m7.mount_relations`, `m7.dyld_cache_read`, `m8.original_launchd`,
   `m8.later_daemons`. Several of these fall out of step 1 for free.
7. **The broker, if the render path is next.** That is the Part A + Part B +
   Part C change fact 221 describes, and it is a device-repo re-pin.
8. **Not started at all:** audio, network, launcher, performance acceptance
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
# The current run: 41364602699 (pin 8e492f5, the declaration fix). The scripts
# are parameterised, but not the same way: the fetcher takes the run id as
# argv[1], the capture reads UNIPAD_RUN_ID from the environment and defaults its
# bundle to local-private/tmp/bundle-<run-id>.
python -u local-private/tmp/fetch-boot-inputs-run.py 41364602699
UNIPAD_RUN_ID=41364602699 python -u local-private/tmp/root-mount-capture.py
python -u local-private/tmp/read-gate.py

# Prove the patch SET applies, in the runner's own order, against the real XNU
# source.  This is the only validated instrument for the inter-patch class of
# defect; it needs local-private/upstream/xnu, so it cannot run in CI.
rm -rf local-private/tmp/xnu-work-repro local-private/tmp/staged-patches
python -u local-private/tmp/repro-patch-order.py

# Byte-level proof the records are in the kernel the guest boots.
python -u local-private/tmp/verify-records-in-kernel.py local-private/tmp/bundle15-check

# Pull ONE member out of the artifact by byte range (no 310 MB download).
python -u local-private/tmp/extract-one-member.py local-private/tmp/bundle15.zip \
  "payload-esp/EFI/UniXNU/Payloads/kernel.macho" local-private/tmp/b15-kernel.macho

# ...and validate the extractor offline against a known answer first.
python -u local-private/tmp/validate-extractor.py

# Read the gate (M0..M12 and WP00..WP04, with the blockers and the vetoes).
python -u local-private/tmp/read-gate.py

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

- **A per-unit check that reconstructs the PRISTINE input cannot see a constraint
  that exists only BETWEEN units.** Measured 2026-11-02. Every pinned patch is
  verified against its own reconstructed minus side, which is the pristine file,
  so `zzz-boot-progress.patch`'s hunk — whose context is three lines
  `offline-owner-systemos-mount.patch` ADDS — is never exercised against the
  patched file. Build 41346094752 failed at `kern_exec.c:7389` naming a patch
  nobody edited. The check was not wrong; it answered a narrower question than
  the build asks. **When a failure names a file you did not touch, that is the
  signature.** The fix is to assert the cross-unit constraint at the site of the
  edit: `test_this_patch_keeps_the_lines_the_next_patch_uses_as_context`.

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

### The records are in the kernel and the port works — and they still did not arrive

This is the most important measurement of the revision, and it is a
non-result. Run 41316953324's boot-inputs bundle was pulled (310,344,859
bytes), its `payload.img` digest `a9ff378c…` matched `macos-build-root.log:125`'s
own record, and the ESP kernel extracted from it hashed to
`e836fd58…` — the digest the build log records. So the bytes measured are the
bytes the build produced.

**Byte-level, the emitter is there and it is right:**

| string | count | offset |
|---|---|---|
| `"event":"system.mount",…}` + `\n` | 1 | 113341, 187 bytes |
| `"event":"system.volume_identity",…}` + `\n` | 1 | 113530, 149 bytes |
| `"mountpoint":"/"` | 1 | inside record 1 |
| `8846FE78-2910-4297-AF97-D59BB6F8FC16` | 3 | 113394, 113578, 808582 |
| `DYLD_SHARED_CACHE_DIR` | **0** | — |
| `native_console_report` | 1 | 90716496 (exported symbol table) |

`"mountpoint":"/"` is the union arm, confirmed in the booting kernel — the
mountpoint fix is not just config-consistent, it is what the guest will say.

**And the capture came back with neither record.** 1455 bytes, five events —
the two IOKit records and the three XNU ones — and `system.mount` /
`system.volume_identity` sourced from NOWHERE. The ring, read on the same boot
through the production `_guest_log_probe` path, says why this is not a timing
problem:

```
[   64.077490] disk3s1 mounting volume …SystemCryptex, requested by: init (pid 1)
[   64.080551] disk3s1 vol-uuid: 8846FE78-2910-4297-AF97-D59BB6F8FC16
[   64.109681] disk3s1 mount-complete volume …SystemCryptex
[   71.809687] load_init_program: attempting to load /usr/appleinternal/sbin/launchd.development
```

The mount succeeded, carrying the exact vol-uuid the record names, and
`load_init_program` ran afterwards.

**So the elimination is short.** The emitter's gate is
`if (!error && native_console_report)`. The call site is
`if (cache_error) panic(...)` and the ring shows no panic, so `error` was 0.
Which leaves `native_console_report` resolving to null in `kern_exec.o` — and
that is invisible to every source-level check here, because the symbol *is*
exported (it sits in the kernel's string table between `native_console_receive`
and `native_console_rx_bytes`). A weak reference either binds or it does not,
and nothing in the source says which. Only the wire answers.

Build 41330622788 carries an ungated diagnostic record at the function's first
statement. If it arrives, the symbol binds and the gate failed; if it does not,
the weak import is not binding and the emitter needs a different way onto the
port. **Do not read the previous build as "the emitter is broken" — it is "one
of two causes, and the diagnostic separates them".**

**A second, unrelated finding: the acceptance suite has been red.** 10 tests in
`tests/acceptance/` fail, and they fail on `HEAD~1` too — before any change this
session. They encode "this host has achieved nothing" (`test_no_check_passes_on_this_host`,
`test_no_onsite_evidence_exists_on_this_host`,
`test_the_release_verdict_is_blocked_on_this_host`, and seven more), which stopped
being true when M4/M5 passed. `test_guest_dependent_milestones_are_blocked_on_this_host`'s
own docstring prescribes the remedy: *"If a real UniXNU kernel and a guest log
appear, this test is supposed to fail: the field record changed and must be
re-recorded deliberately rather than quietly."* The earlier "the suite is green"
reading was `tests/unit` only; `tests/acceptance` had not been run.

### The weak reference resolved to null — measured, not theorised

Build 41330622788 carried an ungated diagnostic record at
`native_owner_cache_prepare()`'s first statement, asking one binary question.
It ran the full 1500 s of channel draining, and the ring's own last timestamp
at loop end was **t=1530.11 s** — the guest ran twenty-four times past that
function — while five records from the same port on the same boot arrived
complete.

The diagnostic did not arrive.

That closes the elimination the previous revision opened. The emitter's gate
is `if (!error && native_console_report)`; the call site is
`if (cache_error) panic(...)` and the ring shows no panic, so `error` was 0.
What is left is that the weak reference resolved to null: **a `weak_import`
does not oblige the linker to keep the definition**, so the symbol the call
site tested for was simply absent.

And the null check is what made this expensive. `if (native_console_report)`
read as a deliberate design decision about a switchable archive. It was in
fact the only reason a silent failure looked like a guarded one — the guard
and the defect were the same line.

**The fix is one word.** The reference is now a strong `extern`. That makes
the definition load-bearing: if the archive is ever missing while this code is
compiled, the link fails naming the symbol instead of dropping both records
with a green build. The switch pairing makes it safe — `XNU_OWNER_CACHE_MOUNT`
is set only by `qemu_root_defconfig:33`, which also sets
`CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y` at `:47`, and that is what puts
`libNativeVirtioConsole.a` on the kernel link with `-Wl,-force_load`
(`build-xnu-4k-baseline.sh:666-670`). A config with the mount on and the
console off does not exist in this tree.

The null check stays, as a dead branch rather than a call through zero.

**The reusable lesson, and it generalises past this patch.** A guard that
tests whether an optional dependency is present is indistinguishable, from
the source, from a guard that is hiding the fact that the dependency was
never linked. The two have opposite remedies — one wants the guard kept, the
other wants it deleted — and no amount of reading the source tells them
apart. What tells them apart is emitting something through the path and
watching whether it arrives, which is what the diagnostic did and what the
previous three builds could not.

**Also closed this session: the acceptance suite was red.** Ten tests in
`tests/acceptance/` failed, on the parent commit too — pre-existing, not
regressions. They encoded "this host has achieved nothing", which stopped
being true when M4/M5/WP04 passed. `tests/unit` had been run and was green;
`tests/acceptance` had never been run, which is how a red suite went
unmeasured for two milestones. Each was rewritten to the invariant that is
true today and still catches the original defect — see the commit message for
the per-test mapping. One of the ten was not a stale premise at all:
`cross_check` reported `vendor/UniXNU` at `3919f6f` where the lock pins
`ef7d305`, and the pin was not in the clone's object store. That was an
environment action, not a test change.

Build 41338826948 is the one that answers whether the strong reference
delivers.

### Correction: the strong reference did not deliver, and the diagnostic was wrong

Build 41338826948 ran 37/37 steps with the strong `extern`. The records were
byte-level in the booting kernel, the guest ran to **t=1514 s**, and the channel
carried the same five records and neither mount record.

So the previous revision's conclusion was wrong, and the reason is the useful
part. The diagnostic sat at `native_owner_cache_prepare()`'s first statement —
and **a function that is never called fails that test exactly as surely as a
symbol that resolves to null does.** The two causes are indistinguishable from
inside the function. That is why making the reference strong changed nothing:
the code was not running at all.

The strong reference is kept. It is the right change regardless — a missing
archive should be a link error, not a silent null — but it was not the fix, and
recording it as the fix would have sent the next revision hunting a linker
problem that does not exist.

The new diagnostic sits at the top of `load_init_program()`, before the prepare
calls and before the `init_programs[]` loop whose `"attempting to load"` line the
kernel ring prints at **t=71.8**. That is a site the ring *proves* executes,
which is the property the last two diagnostics lacked.

Two outcomes, and they point in different directions:
- **It arrives** → the port works from `kern_exec.c`, and the question moves to
  why the prepare call below it does not run (a `goto`, an `#if` arm, or the
  function being called from somewhere the ring's mount is not attributable to).
- **It does not arrive** → the port is unreachable from this translation unit
  and the emitter needs a different route entirely — most likely the
  `native_console_send(provider, owner, …)` entry point, or a driver-side
  emitter rather than a kernel-side one.

**The generalisable lesson, and it is the third of this shape.** An instrument
placed inside the thing it is testing cannot distinguish "the thing is broken"
from "the thing never ran". Both diagnostics so far were inside
`native_owner_cache_prepare()`. The one that answers has to sit somewhere the
evidence already proves executes — and for this patch that place is the ring's
own `load_init_program:` line.

Also worth recording: the escape in the new literal was mangled by the heredoc
that inserted it (the documented lesson, third time), splitting a C string
across two source lines. That also stopped the hunk checker at a line that was
neither context nor added, so one cause produced two red instruments.

---

## 2026-11-02 — the build dispatched to measure the diagnostic failed on a patch nobody edited

Build **41346094752** was dispatched to answer the question the previous
revision left open. It never got to the guest: it failed at
`build-xnu-4k-baseline.sh:44` with

```
error: patch failed: bsd/kern/kern_exec.c:7389
error: bsd/kern/kern_exec.c: patch does not apply
```

Line 7389 is `zzz-boot-progress.patch`'s hunk. **Nobody edited that patch.**

### The cause is a dependency between two patches that no local check can see

`zzz-boot-progress.patch:200-205` carries a hunk whose *context* is

```c
(void) mach_vm_allocate_kernel(map, &scratch_addr, ...)
user_addr_t init_envp = USER_ADDR_NULL;
#if defined(XNU_OWNER_CACHE_MOUNT) && XNU_OWNER_CACHE_MOUNT
int cache_error = native_owner_cache_prepare(...)
```

and **three of those four lines are lines `offline-owner-systemos-mount.patch`
ADDS.** So the patch set is order-dependent in a way that
`test_every_patch_applies_to_its_own_minus_side` structurally cannot catch: that
test reconstructs each patch's minus side, which is the *pristine* file, and
applies it there. The pair is never exercised together locally.

The previous revision inserted the diagnostic between
`mach_vm_allocate_kernel` and `user_addr_t init_envp`. That kept its own patch
self-consistent — the hunk counts were right, the patch applied to its own minus
side, every unit test passed — and broke the patch after it.

**The generalisable lesson, and it is the fourth of this shape.** A per-unit
check that reconstructs the *pristine* input cannot see a constraint that only
exists *between* units. The check is not wrong; it is answering a narrower
question than the one the build asks. When a failure names a file you did not
touch, that is the signature.

The fix (`94a131e`) moves the diagnostic below `user_addr_t init_envp`. The
adjacency is restored and the diagnostic stays where the measurement needs it —
above the prepare calls, above the `init_programs[]` loop whose
`"attempting to load"` line the ring prints at **t=71.8**.

`test_this_patch_keeps_the_lines_the_next_patch_uses_as_context` now asserts
the adjacency directly, with the failing build named in the message. It was
mutation-checked by putting the comment back above the shared line: red on that
exact state, with that message, green again after the restore.

### Also recorded: a submodule pointer that looks like an edit and is not

`vendor/UniXNU`'s working tree shows `drivers/NativeMobileDisplay` as modified:
gitlink `c031d88`, checkout `eb9c949`. That is **intended and documented** — the
lock pins `eb9c949` because the gitlink's `c031d88` cannot compile
(`only virtual member functions can be marked 'override'`, measured on run
40187668943), and `run_driver` re-pins to the lock before every driver call
because every `unixnu` subcommand that reaches `prepare()` runs
`git submodule update --init --recursive` and resets the checkout to the gitlink.
The lock's `commit_note` has recorded this across four revisions. It was left
dirty again, and the commit was path-scoped so it stays that way.

### State

- Pin: `unixnu.commit` = `94a131e` (fifteenth revision), driver pin unchanged at `2f9a89a`.
- Main repo: `7bc0da9`. Fork: `James081105/UniXNU` branch `unipad/gpu-addon-ngpu-dir` at `94a131e`.
- **Build 41350610831** dispatched at `7bc0da9`, stage M7, payload tcg.
- Status: CODE COMPLETE, compile UNVERIFIED, wire UNVERIFIED. This build tells us
  the tree compiles and whether the diagnostic arrives; nothing more.
- The two outcomes and what each would mean are unchanged from the section above.

---

## 2026-11-02 — the guest is 49 real daemons ahead of what the gate knows

The gate says M8 has "zero evidence". The guest ring from build 41338826948
(`local-private/boot-mount-41338826948/root-mount/kernel-ring.log`, guest ran to
**t=1514.6 s**) carries **49 distinct real iPadOS userspace processes**, PIDs 26
through 128:

```
/usr/libexec/runningboardd[26]              /usr/libexec/studentd[31]
/usr/libexec/routined[32]                   /System/Library/CoreServices/powerd.bundle/powerd[40]
/usr/sbin/WirelessRadioManagerd[42]         .../BiomeStreams.framework/Support/biomed[43]
/usr/libexec/nfcd[54]                       .../IDS.framework/identityservicesd.app/identityservicesd[55]
.../CoreDuetContext.framework/Resources/contextstored[57]
/usr/libexec/backboardd[58]                 /usr/libexec/sharingd[61]
/usr/libexec/locationd[63]                  .../IMCore.framework/imagent.app/imagent[64]
/usr/libexec/dasd[68]                       .../PassKitCore.framework/passd[71]
/usr/libexec/lockdownd[72]                  /usr/libexec/PerfPowerServices[74]
... plus 29 more, up to /System/Library/CoreServices/osanalyticshelper[126]
```

and at t=1085.075614:

```
MobileGestaltHel[95] triggered unnest of range 0x1f7800000->0x1f7a00000 of DYLD shared region
```

**`backboardd[58]` is the daemon that launches SpringBoard.** It is running;
SpringBoard has not started yet.

### Why the gate cannot see this, and why that is correct

`_guest_records` (`milestones.py:1362`) scans only files matching
`_GUEST_LOG_GLOBS` (`**/guest.log`, `**/guest-*.log`, `**/*guest*.log`,
`**/guest.json`, `**/guest-*.json`), and its docstring is explicit: *"Only guest
channels count. A serial log is not a guest channel (PRD 7.4)"*. The ring is
`kernel-ring.log`, which matches none of those globs.

**This is the right design and it must not be weakened.** The ring is the
CROSS-CHECK that a record is true, not a substitute for one. What is missing is
not guest progress — it is a channel.

### The sites, in real source

The pinned XNU source is public (`apple-oss-distributions/xnu`, ref
`xnu-10063.141.1`, per `vendor/UniXNU/feeds.toml:10`) and is now cloned at
`local-private/upstream/xnu`. The existing mount patch's line numbers were
verified against it and match exactly, so this is the tree the runner builds.

- **`launchd.running`** — `bsd/kern/kern_exec.c:7142`, `load_init_program()`. The
  `init_programs[]` loop at :7187 unconditionally contains `/sbin/launchd`, and
  its `if (!error) { return; }` branch is exactly "the unmodified launchd binary
  is running as PID 1". A record emitted *before* `load_init_program_at_path`
  would claim a launchd that then failed to load, and the `panic()` three lines
  below is what happens when every entry fails — so the emitter goes inside the
  success branch.
- **`daemon.spawned`** — `bsd/kern/kern_exec.c:5165`, `__mac_execve()`, with
  `exec_done = TRUE;` at :5284 inside `if (!error)`. Every daemon spawn reaches
  it, because launchd uses `posix_spawn`, which lands here.

### The trap this creates, and it is the same shape as the virtio_blk one

`mockfs_pid1_as_launchd` (`milestones.py:1739`) fires when `p.launchd_running` is
OK **and** `p.daemon_spawned` is not. M8 is UNVERIFIED today; emitting
`launchd.running` alone would turn it **FAIL** and make the release verdict
worse. **The two records must land together or not at all.** Same lesson as
`device_enumeration_without_readback`, second occurrence.

A second constraint: `_guest_events` (`milestones.py:1422`) merges with
`setdefault`, so only the FIRST record per event name reaches the gate. That is
the gate's behaviour and must not be worked around by inventing event names —
that would be gaming it. Emit one `daemon.spawned` per successful exec; the gate
reads the first and the full ordered sequence stays in the guest log as
evidence.

### State

- M8 emitters: in flight (worktree branch `m8-launchd-daemon-emitters`).
- M7's third requirement, `dyld.cache_read`: in flight. The ring's DYLD
  shared-region unnest is a better-corroborated site than `cs_native4k_prepare()`,
  which is XNU's own native-4k signature hash cache and is a different thing from
  a process reading the dyld shared cache. The agent has been told.
- M6's `virtio_blk.read_back`: in flight.
- `guest_install_service` (M10/M10a/M11/M12): mapping in flight.
- Build 41350610831 (the inter-patch fix): running.

---

## 2026-11-02 — the second correction, and two emitters whose mapped sites were wrong

### The inter-patch constraint is a ten-line block, not one adjacency

Build **41350610831** failed with the *identical* error as 41346094752 —
`patch failed: bsd/kern/kern_exec.c:7389` — on a pin whose patch contained
the previous fix. So the previous fix was wrong, and **the guard written with
it was wrong too**: it asserted that `mach_vm_allocate_kernel` is followed by
`user_addr_t init_envp`, which was TRUE on the failing tree. A guard that
passes while the build fails is worse than no guard.

`zzz-boot-progress.patch:200`'s pretext is **ten lines**, nine of which this
patch ADDS (`mach_vm_allocate_kernel`, `user_addr_t init_envp`, the whole
CACHE_MOUNT block, the whole DATA_MOUNT block, `#if CONFIG_MEMORYSTATUS`).
`git apply` requires that block contiguous. The fix (`8b57770`) moves the
diagnostic ABOVE `mach_vm_allocate_kernel` — the only position that is both
before the prepare calls and outside the block.

**Proven locally rather than argued.** The pinned XNU source is public
(`apple-oss-distributions/xnu`, ref `xnu-10063.141.1` per
`vendor/UniXNU/feeds.toml:10`) and is now cloned at
`local-private/upstream/xnu`. `local-private/tmp/repro-patch-order.py` replays
the runner's own order — three named patches reversed first, then
`patches/xnu/*.patch` and `kits/NativePrelink/patches/*.patch` shell-globbed
and therefore ALPHABETICAL — with real `git apply`. It failed on
`zzz-boot-progress.patch` at `kern_exec.c:7389` before the change and takes
all 56 patches after. **That script is the only validated instrument for this
class of bug, and it needs the source, so it cannot run in CI.**

A general source-independent cross-patch checker was attempted and
**DELETED**. It reported 18 splits on a tree where real `git apply` takes all
56, because anchoring on lines like `#endif` and `}` matches in the wrong
place. An instrument that is red on a green tree is worse than none — this
repository's own rule, applied to itself. The CI-side guard is therefore
precise rather than general: it asserts the ten-line block as a literal list,
mutation-checked by putting the diagnostic back where it failed.

### The gate reads FAIL, and the reason is the vetoes working correctly

`local-private/tmp/read-gate.py` (there is no `reread-gate.py`; a previous
revision of this document named one that does not exist):

```
release verdict: FAIL
reasons: blocking_fail:M9, blocking_fail:M9a
M4 PASS   M5 PASS   WP04 PASS
M9  FAIL  refused:m9.veto.homescreen_screenshot_only  只展示主屏幕截图
M9a FAIL  refused:m9a.veto.wallpaper_only             只显示桌面壁纸
M10/M10a/M11/M12 BLOCKED  blocked_by:guest_install_service
M9b BLOCKED  blocked_by:system_volume_mounted
WP00 BLOCKED frozen_source_baseline   WP01 BLOCKED frozen_build_inputs
M0 M1 M2 M3 M6 M7 M8 WP02 WP03 UNVERIFIED
```

`_homescreen_screenshot_only` is `p.screendump AND NOT
p.springboard_interaction`, and `_wallpaper_only` is `p.screendump AND NOT
p.uikit_first_frame`. Screenshots exist and interaction evidence does not, so
the vetoes fire and the verdict is FAIL. **That is the gate doing its job** —
it refuses a screenshot as interaction evidence, which is exactly PRD 215/216.

### The guest is 49 real daemons ahead of what the gate knows

The ring from build 41338826948 (guest to t=1514.6 s) carries **49 distinct
real iPadOS userspace processes**, PIDs 26 through 128 —
`runningboardd[26]`, `backboardd[58]` (the daemon that launches SpringBoard),
`lockdownd[72]`, `audiomxd[107]`, `identityservicesd[55]` and ~44 more — and at
t=1085.075614:

```
MobileGestaltHel[95] triggered unnest of range 0x1f7800000->0x1f7a00000 of DYLD shared region
```

`_guest_records` (`milestones.py:1362`) scans only `_GUEST_LOG_GLOBS`
(`**/guest.log`, `**/guest-*.log`, `**/*guest*.log`, `**/guest.json`,
`**/guest-*.json`) and its docstring is explicit: *"Only guest channels count.
A serial log is not a guest channel (PRD 7.4)"*. **That is correct and must not
be weakened.** The ring is the cross-check that a record is true, not a
substitute for one.

### Two mapped sites were wrong, and both emitters moved

| emitter | mapped site | why it was wrong | where it went |
|---|---|---|---|
| `dyld.cache_read` | `cs_native4k_prepare()` @ `ubc_subr.c:5765` | authenticates the signed region of an ARBITRARY code-signed vnode — every 16K-hashed Mach-O on the board. The record would be false for most calls | `_shared_region_map_and_slide()` @ `vm_unix.c`, where `vm_shared_region_map_file()` has just mapped the shared cache into the process's shared region |
| `virtio_blk.read_back` | `vr_verify_block()`'s loop @ `NativeVirtioBlock.cpp:435-444` | reachable from `start()` (so it can precede the console's), runs PER BLOCK (thousands of records), and has no success variable | `doAsyncReadWrite()`, gated on `result == kIOReturnSuccess && actual == count * 512`, after `IOLockUnlock` |

The dyld emitter also needs a **load-bearing vendor edit**:
`build-xnu-4k-baseline.sh` must add `-DXNU_NATIVE_VIRTIO_CONSOLE` to
`vm_unix.o_CFLAGS_ADD`, or the macro is never defined for that translation unit
and the emitter is silently not compiled. The gate is
`XNU_NATIVE_VIRTIO_CONSOLE` rather than `XNU_NATIVE_CS_HASH_CACHE` because the
former is the switch that force-loads `libNativeVirtioConsole.a`.

Both emitters also sit behind a **veto pairing**: M8's
`mockfs_pid1_as_launchd` fires when `p.launchd_running` is OK and
`p.daemon_spawned` is not, so those two must land together or M8 goes from
UNVERIFIED to FAIL. Same shape as `device_enumeration_without_readback`.

### State

- Build **41359711528** dispatched at `a4d40fb`, pin `8b57770` — the build that
  answers whether the diagnostic arrives.
- Main repo at `2e0c593`. UniXNU fork `unipad/gpu-addon-ngpu-dir` at `a549b9a`
  (mount fix + dyld emitter).
- **BLOCKED: the block driver's fork is not pushable from this host.**
  `James081105/VirtioMmioBlock` exists — the API created it and reports it
  public with push permission — but `git` says "Repository not found" for it
  while the same command succeeds on `James081105/VirtioMmioConsole`. An
  explicit URL fails where the named remote succeeds on the console fork;
  neither works on the block fork; overriding the credential helper with the
  gh token does not change it. Recorded rather than guessed at. The driver
  commit is safe in its own repository on branch `unipad/virtio-blk-readback`.
- `guest_install_service` mapped: `local-private/tmp/install-service-map.md`,
  939 lines. Five host→guest request types (not six), the RX path IS reachable
  from kernel code, `NativeOfflineGPU` can never start (its patch calls a
  function defined nowhere), and the recommended design is one new free
  function `native_console_poll()` symmetric to `native_console_report`.

---

## 2026-11-02 — the patches apply, and then the declaration was behind the wrong guard

Build **41359711528** (pin `8b57770`) got **4.5 minutes into the XNU compile**
before failing. That is the answer to the question the previous two builds could
not reach: **the inter-patch fix works.** The patches apply, and both kernel
passes start.

It then failed with:

```
bsd/kern/kern_exec.c:8120:2: error: call to undeclared function
'native_console_report'; ISO C99 and later do not support implicit function
declarations [-Wimplicit-function-declaration]
 8120 |         native_console_report(diag_record, sizeof(diag_record) - 1);
```

Line 8120 is the `load_init_program` DIAGNOSTIC — the record whose entire
purpose is to ask whether this port is reachable from `kern_exec.c` at all. It
is emitted **outside** the `#if defined(XNU_OWNER_CACHE_MOUNT)` block, and the
`extern` declaring `native_console_report` was **inside** it. The build log
names the configuration: *"kernel: no owner root chain on disk, so building
qemu_defconfig"* — the first pass, which does not define that macro for this
translation unit. So the call compiled and the declaration did not.

### The generalisable lesson, and it is the fifth of this shape

This is the **exact inverse** of the defect the dyld emitter was warned about.
There the macro gated the EMITTER, so it was silently not compiled — a no-op
rather than a failure. Here the macro gates the DECLARATION, so the caller is
compiled and the build fails. **Same root cause both times: a caller and its
declaration behind different conditionals.** The first is invisible until a
boot; the second is invisible until a compile; and neither is visible to any
source-level check, because nothing on this host preprocesses.

`8e492f5` moves the declaration above the guard, unconditionally, with both
callers — the emitter inside the conditional and the diagnostic outside it —
seeing one declaration whatever the configuration.

### What is deliberately NOT in that commit

The three new emitter patches sitting in the working tree. The M8 agent
measured the reason:

> `send()` is a synchronous round trip with a two-second deadline, and a
> descriptor the device never consumes stays pending — so every `send()` after
> the first timeout pays another two seconds. On a boot with hundreds of execs
> that is a hang.

Measured volume: the ring's retained window names **49 distinct PIDs (26–128)**
and PIDs 1–25 happened before it, so **≥75 successful execs on a boot that
stops before SpringBoard**, several hundred on a full boot. The agent looked
for measured evidence of the stall, **disproved its own first theory** (the
6.35 s gap between the Data mount at t=58.524 and `load_init_program` at
t=64.879 sits *after* the mount records are emitted, so it is not console
timeouts), and added no speculative backoff. **The drain is the first thing to
measure on the next boot.**

### M8: both events land, and the veto is respected

`zzz-m8-launchd-daemon-events.patch` — 3 hunks, 161 added lines, 11 guard
tests, 13 mutations all red, no survivors.

- **`launchd.running`** inside `load_init_program()`'s `init_programs[]` loop's
  `if (!error)` branch (post-patch line 8344). The `panic()` four lines below is
  the all-entries-failed path, so the branch is success-only.
- **`daemon.spawned`** inside `__mac_execve()`, at `exec_done = TRUE` (post-patch
  5411) — the kernel's own "the exec occurred" flag.
- `test_both_m8_events_are_in_the_same_patch_set` asserts the pair travels
  together, and that stripping the daemon hunk makes a set carrying only
  `launchd.running` — so a discriminator that always answers "both" cannot pass.
- **`DEVELOPMENT` is set; `DEBUG` is not** — settled by measurement, not by
  reading a config: the ring reads `attempting to load
  /usr/appleinternal/sbin/launchd.development` at t=64.879, then
  `attempting to load /sbin/launchd` at t=65.008, then `[1(launchd)]` at t=66.579.
  The `.debug` entry was never attempted, so `DEBUG` is 0.
- **`imgp->ip_ndp` is freed by the time site 2 runs** — `exec_activate_image()`
  calls `nameidone()` before returning. Reading it would be a use-after-free
  that looks like a working record. The name comes from `p->p_comm`.
- The first `daemon.spawned` on a real boot will be **launchd itself (pid 1)**,
  because `load_init_program_at_path()` ends in `execve()`.

### A CRLF risk, checked and cleared

The M8 agent flagged that `offline-owner-systemos-mount.patch` is CRLF in the
Windows worktree (418 CRLF, 0 bare LF) while the other 52 patches are LF, and
that if that reached the commit the runner's `git apply` would refuse it.
Checked: the committed blob is **LF** (`git ls-files --eol` reports
`i/lf w/crlf attr/text eol=lf`). The working tree is CRLF because of
`core.autocrlf`; git normalises on commit. The runner gets clean LF.

### State

- Build **41364602699** dispatched at `3bf6fe9`, pin `8e492f5` — the build that
  should compile and should finally run the diagnostic.
- Main repo at `3bf6fe9`. Fork branch at `8e492f5` (`8b57770` → `8e492f5`).
- `a549b9a` (the dyld emitter) was force-pushed off the branch and is **not
  pinned**; its patch is preserved at
  `local-private/tmp/parked-patches/native-dyld-cache-read.patch`.
- Three emitters CODE COMPLETE but unpinned: `dyld.cache_read`,
  `virtio_blk.read_back`, and the M8 pair. `guest_install_service` mapping is
  done and its emitter is in flight.

---

## 2026-11-02 — the suite is green, and the block fork is a hard blocker

**`tests/unit`: 3404 passed, 10 skipped, 0 failed** (exit 0). Read from the dot
output rather than the summary line, which the Bash tool eats — the documented
lesson, applied again.

**The block driver's fork is a hard blocker, not a propagation delay.**
`James081505/VirtioMmioBlock` was created through the API and is reported
public with push permission (`"fork":true`, parent `UniXNU/VirtioMmioBlock`).
Every one of these fails against it with `remote: Repository not found`:

- `git ls-remote <url> HEAD` and `git push <url>` — from the driver repo's cwd,
  from `/tmp`, and from `vendor/UniXNU`.
- `git ls-remote fork HEAD` via the named remote, which SUCCEEDS on
  `James081505/VirtioMmioConsole` from its own repo.
- `git -c credential.https://github.com.helper='!gh auth git-credential'` —
  overriding the credential helper with the gh token changes nothing.
- `gh repo view James081505/VirtioMmioBlock` and `gh api repos/James081505/…`
  both 404, while `gh api` on `UniXNU/…` repos works.

And the decisive asymmetry: **the explicit URL fails where the named remote
succeeds**, on the console fork, from the console repo's own cwd. So the
credential is resolved per-remote-name, and the block fork has no such entry.
Retried over ~75 minutes; not a delay.

The emitter is safe: its commit is in `vendor/UniXNU/drivers/NativeVirtioBlock`
on branch `unipad/virtio-blk-readback` (`3d05351`), and the 18 guard tests are
committed in the main repo. What is missing is only the push and the two pin
bumps. **Recorded as a blocker rather than guessed at** — the next session
should either push from a host whose credential can see the fork, or change the
`.gitmodules` URL for `drivers/NativeVirtioBlock` to a fork that resolves.

---

## 2026-11-02 — three corrections in a row, each exposing the next

The chain, because each step's failure named the next step's defect:

1. **41346094752** — patch failed at `kern_exec.c:7389`. The diagnostic sat
   between two lines of `zzz-boot-progress.patch`'s context.
2. **41350610831** — the *identical* error, on a pin containing the fix. The
   constraint was the whole **ten-line** block, not the one adjacency the first
   fix kept. The guard written with that fix asserted the adjacency — which was
   TRUE on the failing tree — so it passed while the build failed.
3. **41359711528** — the patches applied, the build got **4.5 minutes into the
   compile**, then `kern_exec.c:8120: error: call to undeclared function
   'native_console_report'`. The diagnostic is emitted *outside*
   `#if defined(XNU_OWNER_CACHE_MOUNT)`; the `extern` was *inside* it.
4. **41364602699** — the compile passed, the build reached the **LINK**, and
   failed with `"_native_console_report", referenced from: _load_init_program in
   kern_exec.o` / `ld: symbol(s) not found`. That is the strong extern doing its
   job. The archive is force-loaded only when `NATIVE_VIRTIO_CONSOLE=1`, and the
   switch that is both passed to this translation unit *and* paired with the
   archive is `XNU_OWNER_CACHE_MOUNT`.
5. **41369829766** — dispatched.

### The lesson that generalises, and it is the sixth of this shape

**A caller and its definition must share one guard.** Step 3 was the macro
gating the *declaration*; step 4 was the caller outside the guard that pairs with
the *definition*. Both are the same rule one layer apart, and **neither is visible
to any source-level check, because nothing on this host preprocesses or links.**

### And there is no single position satisfying both constraints

The diagnostic must be inside `XNU_OWNER_CACHE_MOUNT` to link. `zzz-boot-progress`
needs ten lines contiguous, nine of which this patch adds, with the guard's region
in the middle. Moving the guard above the whole hunk — the previous revision —
satisfied the first and broke the second, and
`test_this_patch_keeps_the_lines_the_next_patch_uses_as_context` **caught it red**.
The guard works; the placement was wrong.

`ebd8769` gives the diagnostic its **own** self-contained `#if` block above
`mach_vm_allocate_kernel` and restores the emitter's guard between `init_envp`
and the prepare call. Verified by replaying the runner's own patch order with
real `git apply` against the cloned XNU source: **all 58 patches apply.**

This costs nothing in diagnostic power, and that is worth stating exactly: the
diagnostic fires at the **top** of `load_init_program()`, before the prepare
calls; the emitter fires at the **end** of `native_owner_cache_prepare()` gated on
`!error`. Different points, different gates. Diagnostic arrives and emitter does
not → the prepare call did not run, or `error` was non-zero. Neither arrives → the
port is unreachable from `kern_exec.c`.

A small one, twice: the added comment paragraph carried its own `*/` while the
comment's original close stayed, so the comment had **two** closes — in two
consecutive revisions. Caught both times by *looking at the file* rather than
trusting the script's exit code.

---

## 2026-11-02 — the install service lands, and it answers the drain question by construction

`install.registered` is the single event that clears `_INSTALL_SERVICE_BLOCKER`
(`milestones.py:1983-1991`), which is named by **four** milestones — M10, M10a,
M11, M12. Nothing in either pinned tree produced it, so all four could only ever
read BLOCKED.

**The dispatcher lives in `bsd/kern/bsd_init.c`, not `kern_exec.c`, for a measured
reason**: `build-xnu-4k-baseline.sh:329` passes `-DXNU_NATIVE_OFFLINE_GPU` to
`bsd_init.o` and **not** to `kern_exec.o`. A `kern_exec.c` home would make the GPU
half of the switch coupling vacuously true — a guard that reads as protective and
protects nothing. That is the same shape as the caller/declaration mismatch three
revisions up, one level out.

**The driver half** is `native_console_poll(void *, size_t *)` in the console fork
(`5693c68`), the RX twin of `native_console_report()`. It needs the same friend
grant `receive()` needs, for the same measured reason `send()` needed it on run
41313323081. The header's first two lines are unchanged and it carries no
install-JSON knowledge — it is a byte transport, and the protocol lives in the
patch.

**ONE RECORD PER BOOT, and that bound is structural, not an unmeasured rate
limit.** `native_install_registered` is never cleared and the port is written
exactly once, so the worst this can add to a stalling TX drain is **one**
two-second deadline — against the daemon emitter's measured **≥75** sends per
boot. That is what makes it safe to pin while the M8 and dyld emitters are not.

**The dispatcher is disabled by the same switch that enables the GPU**
(`XNU_OWNER_ROOT_TRUST && !XNU_NATIVE_OFFLINE_GPU`), because the moment the GPU
broker is revived the two share one RX byte stream under a lock-step protocol and
`readExact` treats any short read as `kIOReturnIOError`. The coupling is asserted
by a test that checks the guard appears exactly twice **and** that the build
script really passes both macros — the second half is what makes it real rather
than decoration.

**It deliberately does not answer the host.** The map's recommended
reply-through-`native_console_report` is a **trap**: every available reply either
fabricates a success or arms the `copy_only_install` veto, so replying would trade
one blocker for a worse one. A test asserts none of the three reply types appears.

21 guard tests, **26 mutations, every one killed**. Two of them were real defects
the tests found before a build: the patch called `native_console_poll` without
declaring it, and the call block started the reader thread without
forward-declaring it.

### State

- Main repo at `cbb82aa`. Fork `unipad/gpu-addon-ngpu-dir` at `3ff2f7f`; console
  fork `unipad/console-announcement` at `5693c68`.
- Lock: `unixnu.commit` = `3ff2f7fa`, `submodules[drivers/NativeVirtioConsole]` =
  `5693c68d`. Both bumped together, because a patch calling a symbol the gitlink
  does not carry fails the link naming it.
- `repro-patch-order.py`: **all 58 patches apply**, the install patch at position
  21, contributing zero unreachable hunks.
- Build **41369829766** (the own-guard fix) is still running.
- Three emitters remain CODE COMPLETE but **unpinned**, all for the drain reason:
  `dyld.cache_read`, `virtio_blk.read_back` (also blocked on its fork), and the M8
  `launchd.running`/`daemon.spawned` pair.

---

## 2026-11-02 — the first fully green build, and the diagnostic is in the booting kernel

Build **41369829766** concluded **success**, 34/34 steps. That is the first build
to get past all three of the failures the previous three revisions hit in
sequence: the patch application, the compile, and the link.

The boot inputs are downloaded and verified against the bundle's **own**
`payload.json` — `payload_sha256: c38b7ed6bef94111ab8f5935d4769fdc0c65344df7ee5329d959006228a671b7`,
which is exactly what the 310,343,023-byte download hashes to. The fetcher's
hardcoded expectation (`62587de9…`) is from an old build and is simply stale; the
bundle's own manifest is the authority, as it was the last time this came up.

**Byte-level, in the kernel the guest boots** (ESP `kernel.macho`, 125,480,960
bytes, sha256 `7cac83e5…` = `payload.json`'s `payload_kernel_sha256`):

| literal | count |
|---|---|
| `diag.load_init_program_entered` | **1** |
| `XNU.load_init_program` | 1 |
| `system.mount` | 1 |
| `system.volume_identity` | 1 |
| `XNU_OWNER_CACHE_MOUNT` | 0 — correct, it is a macro and is compiled away |

So all four records are in the image. **The wire is the only instrument left**, and
the capture is running.

`REQUIRED_EVENTS` in `root-mount-capture.py` now includes
`diag.load_init_program_entered`, because that record — not the mount records — is
what this build was dispatched to measure, and a capture that ended early would
otherwise report "missing system.mount" and say nothing about the question actually
asked.

---

## 2026-11-02 — the first fully green build, and the measurement that changes the route

Build **41369829766** concluded **success**, 34/34 steps — the first build to get
past the patch application, the compile and the link, which the previous three
revisions hit in sequence.

**And the diagnostic did not arrive.**

The capture ran the guest to **t=1527.8 s** (1500 s of draining), wrote 1476 bytes,
and found:

```
events found: iokit.driver_probe, iokit.service_match, xnu.exception_vectors,
               xnu.interrupts, xnu.page_tables
missing:      system.mount, system.volume_identity, diag.load_init_program_entered
emitter_missing_from_kernel: []      <- every record IS in the ESP kernel
```

The records are byte-level in the kernel the guest boots (ESP `kernel.macho`,
125,480,960 bytes, sha256 `7cac83e5…` = `payload.json`'s `payload_kernel_sha256`),
and `offline owner SystemOS mount failed` — a string that sits inside the *same*
`#if defined(XNU_OWNER_CACHE_MOUNT)` as the diagnostic — is present, which proves
the macro was 1 and the block was compiled. The link resolved. IOKit matching had
finished by **t=63.1** (ring: `Got boot device =
IOService:/ArmDTPlatform/virtio-mmio-31@2003E00/NativeVirtioBlock/…`), and
`g_console` is published in `start()`, so it was set eight seconds before
`load_init_program` ran at t=71.8. Ordering is not the cause.

### Two variables changed at once, and that is the defect in the conclusion

The five records that arrived go through **`this->send()` directly**, from inside
the class. The diagnostic goes through **`native_console_report()`**, the free
function, from BSD kernel code. So the entry point changed *and* the caller's
context changed — and **nothing in this repository had ever proven the free
function delivers at all.** Every record that ever satisfied a requirement went
through `send()` from inside `NativeVirtioConsole`.

`native_console_report()` is `g_console?g_console->send(bytes,size):kIOReturnNotOpen`
— a null `g_console` drops silently, which is the same shape as the `weak_import`
defect this project already spent a month on.

### The experiment that isolates it (build 41376479360, dispatched)

Two records added to the console driver, at the same point in the boot as the five
that are known to work:

- `diag.free_fn_from_driver` — emitted through `native_console_report()`.
- `diag.control_from_driver` — emitted through `send()`, the way the others go.

| result | meaning |
|---|---|
| free_fn absent, control present | the free function is broken, and **every** kernel-side emitter — the M8 pair, the dyld emitter, the install dispatcher's reply — is routed through a dead path |
| both present | the free function works, and the defect is specific to `kern_exec.c`: `g_console` null by then, or `send()`'s synchronous `complete()` wait behaving differently outside the driver's context |

`send()` ends with `complete(1,&consumed)` — a synchronous wait for the device to
consume the descriptor, which is the two-second deadline the M8 agent measured.

Both records are added to the test's `RECORDS` list so the existing
one-line-valid-JSON and `sizeof`-of-an-array machinery covers them, and to an
explicit `DIAGNOSTIC_EVENTS` allowlist in
`test_the_gate_still_reads_what_the_driver_emits` — **named explicitly rather than
inferred**, because inferring "this one is only a diagnostic" from the name would
let any ungated record exempt itself. That test also asserts the reverse: a
diagnostic the gate actually names is a mislabelled requirement.

### State

- Main repo at `1ceae14`. Fork `unipad/gpu-addon-ngpu-dir` at `1625c84`; console
  fork `unipad/console-announcement` at `d25974c`.
- Lock: `unixnu.commit` = `1625c84c`, `submodules[drivers/NativeVirtioConsole]` =
  `d25974c3`.
- Gate unchanged: 3 PASS (M4, M5, WP04), 9 UNVERIFIED, 2 FAIL (M9/M9a on the PRD
  vetoes), 7 BLOCKED.
- `tests/unit`: 3404 passed, 10 skipped, 0 failed.

---

## 2026-11-02 — a trap in the boot inputs, and three more compile errors

**The boot inputs are collected even when the second kernel pass fails, and they
then carry the FIRST-pass kernel.** Build 41376479360 failed at step 21, yet
steps 26–27 still collected and uploaded the boot inputs. Its ESP
`kernel.macho` is **51,859,456 bytes** — the first-pass size — against the
125,480,960 bytes the second pass produces, and it contains **zero** occurrences
of `diag.free_fn_from_driver`, `diag.control_from_driver`,
`diag.load_init_program_entered` and `install.registered`. A capture against
those inputs would boot a kernel without the experiment and report the records
"missing" — which reads exactly like the measurement the experiment exists to
take. **Check the kernel's size and the record counts before trusting a capture.**

### Three compile errors in the install dispatcher, found by the second pass

41376479360's first pass compiled and linked — so the isolating experiment for
`native_console_report()` is sound — but its **second** pass,
`qemu_root_defconfig`, sets `XNU_OWNER_ROOT_TRUST=1` and is the **first compiler
to see the dispatcher at all**:

```
bsd_init.c:1309:2: error: function declared in block scope cannot have 'static' storage class
bsd_init.c:1633:2: error: call to undeclared function 'native_console_report'
bsd_init.c:1649:1: error: static declaration of 'native_install_service_thread' follows non-static declaration
```

The first and third are one defect: the forward declaration sat inside
`bsd_do_post()`'s body, where `static` is illegal, and dropping the keyword alone
then fails against the definition. It now sits at **file scope**, inside the same
`#if` as the definition. The second is the caller/declaration rule one layer out
— the patch declared `native_console_poll` and called `native_console_report`;
clang named the fix itself (*"did you mean 'native_console_poll'?"*).

### Two tests were wrong, and both are fixed rather than deleted

`test_every_function_the_call_block_uses_is_declared_before_it` **required the
forward declaration inside the call block** — which is exactly what the compiler
rejects. A test written from reasoning about C rather than from a compiler
demanded something C refuses: the same shape as the inter-patch guard that passed
while the build failed. It now requires file scope, read from the **raw diff
lines**, because `_code_lines` strips indentation and stripped, a file-scope and
a block-scope declaration are the same string — which is precisely the
distinction this turned on.

It also named only the thread function. The declaration that actually failed to
compile was `native_console_report`, and the test's own name says "every function
the call block uses". It now walks them all.

The two write-counting tests needed the `extern` exclusion the sibling test at
the null-check guard already had: without it, adding the declaration made them
count it as a second write of the same event — **a test going red on the fix for
the defect it was written to catch.**

### State

- Main repo at `9618b86`. Fork at `d645589`. Lock: `unixnu.commit` = `d6455896`,
  `submodules[drivers/NativeVirtioConsole]` = `d25974c3` (unchanged — this
  revision changes only the XNU patch).
- Build **41382433090** dispatched: the isolating experiment **and** the install
  fix together. One build answers both questions.
- 78 guard tests green across the console, install and patch suites.

## 2026-11-02 — the declaration goes at block scope, and the block fork was a typo

**Build 41382433090's second pass got past the three errors 41376479360 found and
stopped on a fourth:**

```
bsd_init.c:1325:27: error: use of undeclared identifier 'native_install_service_thread'
```

The file-scope declaration that fixed the *previous* round sits at applied line
**1377 -- after the call at 1325**. The whole added block begins **inside
`bsdinit_task()`** (pristine line 1096, five lines into a function opening at
1073) and the definitions were deliberately placed below it. And there is no
file-scope position that is both before the call and free of another patch's
context: the region between `bsd_init()`'s closing brace and `bsdinit_task()` is
claimed by `offline-owner-root-binding-audit.patch`'s second hunk, whose pretext
ends at `void bsdinit_task(void)`.

The fix drops `static` from the pair and puts the declaration at **block scope,
above the call**. The keyword is what is illegal in block scope, not the
declaration, and the definition drops it to match. The cost is one symbol with
external linkage in the kernel's table -- namespace pollution rather than a
correctness problem, and the only option that does not fight another patch for
the same lines.

### The declaration test has now been wrong about where the declaration goes TWICE

Round one demanded block scope **with** `static` (41376479360 rejected it). Round
two demanded **file** scope (41382433090 rejected that). Both were written from
reasoning about C rather than from a compiler -- the same shape as the inter-patch
guard that passed while the build failed. Both rounds are now recorded in the
test's docstring.

The durable form names all three constraints the compiler actually settles: the
declaration **exists**, it is at **block scope** because that is the only position
before the call, and it does **not** carry `static`.

### It is verified by a five-mutation teeth check, not by agreement

Remove it / add `static` / unindent it / put `static` on the definition / move it
after the call. **Each mutation must fail on the assertion that names its build
failure.** The first version of that check matched the exact final declaration
string, which made three of the five collapse into *"no declaration at all"* -- a
lie on a tree where one plainly exists, and it would have sent the next reader
looking in the wrong place. The anchor is now deliberately loose and the
properties are asserted one at a time.

Two bugs in the check itself were found and fixed: `DECL.lstrip("+")` strips the
leading `+` too, so the mutated line left the diff entirely and the test failed
for *absent* rather than for *file scope*; and the `declared` set was originally a
substring test, which counts a **call** as a declaration and lets a missing
declaration hide behind the call that needs it. It is now a declaration-**shape**
matcher: a type, a name, a parameter list, a semicolon.

### The M6 blocker was never permissions -- it was a transposed digit

`git push` to the block fork said *"Repository not found"* for ~75 minutes across
retries, URL changes and credential-helper overrides. The remote read
`https://github.com/James081505/VirtioMmioBlock.git`; the authenticated account
is **`James081105`**. A `gh api` call to the account's own repo list showed
`James081105/VirtioMmioBlock` sitting there with `push=true`, last pushed
2026-10-31.

**The rule this time:** when a push to a repo you created says *"not found"*,
query the account's own repo list before concluding anything about permissions.
"Not found" from GitHub means *does not exist **or** you cannot see it* -- the two
are indistinguishable from the client, and one of them is a typo.

The console driver's remote was checked for the same defect and is correct. The
read-back commit `3d05351` is now pushed to `unipad/virtio-blk-readback` on the
fork. **M6's `virtio_blk.read_back` is therefore no longer blocked on
pushability** -- it remains unpinned by choice, so one build measures one thing.

### State

- Main repo at `267f866`. UniXNU fork `unipad/gpu-addon-ngpu-dir` at `0906997`
  (two commits: the decl-order fix, then a one-line cosmetic follow-up removing a
  blank line it left between the variable and the declaration).
- Console fork unchanged at `d25974c` -- nothing in this revision touches it.
- Lock: `unixnu.commit` = `0906997`, `submodules[drivers/NativeVirtioConsole]` =
  `d25974c3` (unchanged).
- Verified by replaying the runner's patch order against the **committed** tree:
  all 58 patches apply, and the applied `bsd_init.c` carries the declaration at
  1325, the call at 1326, and the definition at 1643 with no `static` on it.
- `tests/unit`: 3441 collected, exit code 0 (3431 passed / 10 skipped, unchanged
  -- this revision edits the install test rather than adding one). The
  declaration test is 5/5 on the teeth check.
- Build **41388754631** dispatched on `267f866`: the install fix **and** the
  isolating experiment, one build. **Check the ESP kernel's byte size before
  trusting any capture from it** -- the boot inputs are uploaded even when the
  second kernel pass fails, and they then carry the FIRST-pass kernel
  (51,859,456 bytes) rather than the second pass's (125,480,960).

## 2026-11-02 — a green build that verified nothing, and the workflow that can

**Build 41388754631 concluded success, and it does NOT verify the install
dispatcher.** Both of its jobs select `qemu_defconfig`:

```
python3 "$VENDOR_DIR/UniXNU/unixnu" defconfig qemu_defconfig ...
== configuration qemu_defconfig -> .../unixnu-out/qemu/.config
```

`qemu_defconfig` does not set `XNU_OWNER_ROOT_TRUST`, so the dispatcher's guard
`XNU_OWNER_ROOT_TRUST && !XNU_NATIVE_OFFLINE_GPU` is **false and the whole block
is compiled out**. The kernel compiled and linked (`LD
mach.development.bcm2837.unstripped.noctf`, `LDFILELIST bsd`, zero error lines)
and the driver archive is on disk — all true, and all orthogonal to the
dispatcher.

**The compiler diagnostics confirm which file layout it used, but they cannot
confirm the macro.** The reported `bsd_init.c` line numbers (241, 242, 260, ...
1295, 1860) match the WITH-install source exactly — 1295 is the `-Wformat`
`panic("bsd_do_post: ...")`, 1860 is the `-Wcast-function-type-strict`
`STARTUP(TUNABLES, ...)`. That proves the compiler read this file version; it
does **not** prove the `#if` was true, because the inactive lines still occupy
those line numbers.

### The rule, and it is the seventh of this shape

**A green build only verifies the configuration it actually selected.** The four
dispatcher defects were all found by the SECOND pass, `qemu_root_defconfig`,
which lives in `build-macos` — not in `build-kernel-console`. Reading a
`build-kernel-console` success as "the dispatcher compiles" is the same error as
reading a passing guard test as "the constraint holds": the instrument never
exercised the thing.

The install dispatcher has therefore **still never been compiled by any
compiler**. Every error so far came from the one configuration that includes it.

Build **41390547018** dispatched on `ad5bd1f` with
`unixnu_commit=09069974` and `stage=M10` — this is the first
`qemu_root_defconfig` build since `9618b868`, which is where the
`bsd_init.c:1325` failure came from. That is the build that answers it.

## 2026-11-02 — M6's blocker was one of three, and the other two were already solved

The fork push was only the FIRST of M6's blockers, and it turned out to be the
only one that was actually broken.  The rest of the chain was already in place,
which is worth recording because it changes the estimate from "a multi-step
integration" to "a pin bump".

`qemu_root_defconfig` already carries all three:

```
CONFIG_XNU_NATIVE_AES=y
CONFIG_XNU_NATIVE_VIRTIO_BLOCK=y
CONFIG_XNU_NATIVE_VIRTIO_BLOCK_ARCHIVE="{tree}/build/native-virtio-block-verified-bundle/libNativeVirtioBlock.a"
```

`build-xnu-4k-baseline.sh:170-176` refuses `XNU_NATIVE_VIRTIO_BLOCK=1` unless
`XNU_NATIVE_AES=1` -- so the AES dependency that looked like extra work is
already satisfied by the booting config, not something to add.

The archive path is built by
`vendor/UniXNU-qemu/scripts/build-arm-driver.py:33-37`, which routes
`--verified_root_bundle` to `build/native-virtio-block-verified-bundle` and
passes `-DNATIVE_VERIFIED_BLOCK=1` at `:72`.  So the emitter's own guard
`#if defined(NATIVE_VERIFIED_BLOCK) && NATIVE_VERIFIED_BLOCK` is satisfied on
exactly the path the booting config names -- not on a path nothing takes.

**And the emitter is bounded the same way the install service is.** `3d05351`
carries `bool readBackReported` as a class member and sets it BEFORE the call, so
the cost is one record per driver instance on the first successful verified read
-- against the "thousands of verified reads during a boot" its own comment names.
That is the structural bound that makes the install service safe to pin while M8
is not, and M6 now has it too.

The record itself is a static string, not `snprintf`, with `sizeof` applied to the
ARRAY and a comment naming the fact-228 ternary defect.  One line of valid JSON,
event name exactly `virtio_blk.read_back`.

So M6's remaining work is the pin bump and nothing else:
`submodules[drivers/NativeVirtioBlock]` `98909eb` -> `3d05351`.

## 2026-11-02 — the free function is DEAD, and that re-routes every kernel-side emitter

**Measured, not inferred.** Build 41390547018 produced the matched kernel, its
sha256 `73dbc9598885` verified against `payload.json`'s `payload_kernel_sha256`,
and all three experiment literals byte-level in it at **one occurrence each**.
The boot captured **1531 bytes** and ended with:

```
{"event":"iokit.service_match",...}
{"event":"iokit.driver_probe",...}
{"event":"xnu.page_tables",...}
{"event":"xnu.exception_vectors",...}
{"event":"xnu.interrupts",...}
{"event":"diag.control_from_driver","source":"NativeVirtioConsole.start"}
```

`diag.free_fn_from_driver` is **absent**. The two calls are adjacent lines in
`start()` -- line 432 the free function, line 433 the member -- with
`g_console=this` published 143 lines earlier at :289, after five successful
`send()` calls.

**An independent replication, which is what makes this a fact rather than an
anecdote:** the mount patch's own `native_console_report()` records
(`system.mount` and the identity record, called from a *different translation
unit* entirely) have **never arrived in any capture, across every boot this
project has taken**. Zero occurrences in every guest log on disk. Meanwhile
every `send()`-based record lands.

So: two translation units, multiple boots, the free function delivering nothing.

### What is ruled out, and how

* **A competing definition.** No patch defines the symbol -- every occurrence in
  `patches/` is a call. The driver's archive carries exactly one
  `native_console_report` and no mangled variant (`_Z21native_console_report`
  occurs zero times), so the friend declaration at
  `NativeVirtioConsole.cpp:46` did not fork a second C++-linkage symbol.
* **`send()` behaving differently between two adjacent calls.** It has no state
  that could differ: `online` is true, `gate` is held by nobody, and the control
  record on the next line goes through the identical path.
* **The host dropping it.** The capture is one 1531-byte chunk parsed line by
  line; the control record immediately after the free-function call is present.

### What this re-routes

**Every kernel-side emitter in this project routes through this dead path**: M6's
`virtio_blk.read_back`, the M8 `launchd.running`/`daemon.spawned` pair, M7's
`dyld.cache_read`, the install dispatcher's `install.registered`, and the mount
emitter's records. None of them can deliver until this is fixed, however correct
their source is.

**So the drain risk was never the binding constraint on M7 and M8.**
Both were held back as "CODE COMPLETE but unpinned, pending the drain
measurement", which read as a prudence about a hang. The real reason they could
not have delivered is a dead entry point. And **M6 must NOT be pinned now**: its
integration is complete (the config, the archive path, the macro and the
one-record bound are all already in place) and its only remaining step is the
pin bump, but pinning it would put a second emitter on a dead path and measure
nothing. Sequencing: fix the free function, then pin M6, M7 and M8 in one build
and measure all three.

**And it does NOT touch what already passes.** M4 and M5 pass on
`iokit.service_match`, `iokit.driver_probe`, `xnu.page_tables`,
`xnu.exception_vectors` and `xnu.interrupts` -- all five emitted by the driver's
own `send()`. The gate's current PASSes stand.

### Round two, dispatched, and why round one could not answer it

A record that reports only **absence** cannot distinguish the three surviving
causes, and they have different remedies:

| status | meaning | remedy |
|---|---|---|
| `not_open` | `g_console` is null, so the ternary drops silently | publish it earlier, or find what clears it |
| `success` | `send()` reports success and the record is still lost | the loss is **downstream of `send()`** -- no entry-point fix will help |
| `other` | a third failure nothing has named | worth a third round |

So the driver now emits the **return status** as its own record, selected by an
if-chain so every arm still names an array and `sizeof(arr)-1` stays the record's
own length. The length-checking test caught the first draft of it for writing
`sizeof(X) - 1` with spaces, where the driver's style and that test both require
`sizeof(X)-1` -- a test going red on the fix for the defect it was written to
catch, in the good sense.

Build **41400938438** dispatched on `120ee05`, console fork at `d2bdce2`.

## 2026-11-02 — the root cause: ANY instance's teardown withdrew the live port

**Round two answered it.** Build 41400938438's capture (1662 bytes, kernel sha256
`a93e59781d8c`, verified against `payload.json`) carried:

```
{"event":"diag.free_fn_status","status":"not_open"}
{"event":"diag.control_from_driver","source":"NativeVirtioConsole.start"}
```

`kIOReturnNotOpen` is the ternary's FALSE arm in
`native_console_report()` -- `g_console ? g_console->send(...) :
kIOReturnNotOpen`. `send()` itself returns only BadArgument, NotReady or Success,
so the pointer was null. Published 143 lines earlier, at `:289`, with nothing
between the two that assigns it.

**And `reset()` is why:**

```cpp
bool NativeVirtioConsole::reset() {
    online=false;if(ownsDevice)native_console_ready=0;
    if(!registers || !ownsDevice)return true;     // :183 -- TRUE for a
                                                  //     never-started instance
```

`cleanup()` cleared `g_console` whenever `reset()` returned true, and **never
checked `g_console == this`**. So an instance that was merely constructed and then
stopped withdrew the port belonging to a different, live instance. The live
instance's own `this->send()` kept working -- that path never reads the pointer --
which is exactly why five records landed and the sixth did not, on adjacent lines.

The fix is `if (g_console == this)`. **Only the publisher may unpublish.**

### The blast radius, and it is larger than the diagnostic

`native_console_report()` is the entry point **every kernel-side emitter in this
project routes through**, and it has been dropping records silently **since the
port was first published, on every boot**. This is the same reason the mount
patch's `native_console_report()` records have never arrived in any capture, across
every boot taken. Two independent translation units, multiple boots, one cause.

It also explains a measurement that had been read as a *context* problem: the
`kern_exec.c` diagnostic that "did not arrive from BSD kernel code" was never a
context failure at all. The entry point was dead by then, for this reason.

### The test was checking a weaker property than the bug required

`test_the_port_is_withdrawn_when_the_device_is_stopped` required that
`g_console=nullptr` appear in `cleanup()` and follow `reset()`. Both still hold
after the fix, and **neither was sufficient** -- the guard it was missing is the
one that would have caught this. It now requires `g_console == this`, and removing
the guard turns it red.

Build **41407044961** dispatched on `8a3e5cd`, console fork at `3ca09c9`. Its
capture should show `diag.free_fn_status` as `success` and
`diag.free_fn_from_driver` arriving for the first time.

## 2026-11-02 — the fix is measured: `diag.free_fn_from_driver` arrives for the first time

**Build 41407044961, capture 1675 bytes, kernel sha256 `f9f2cc5c5c17` verified
against `payload.json`:**

```
{"event":"iokit.service_match",...}
{"event":"iokit.driver_probe",...}
{"event":"xnu.page_tables",...}
{"event":"xnu.exception_vectors",...}
{"event":"xnu.interrupts",...}
{"event":"diag.free_fn_from_driver","source":"NativeVirtioConsole.start"}
{"event":"diag.free_fn_status","status":"success"}
{"event":"diag.control_from_driver","source":"NativeVirtioConsole.start"}
```

**`diag.free_fn_from_driver` has NEVER appeared in any capture before this boot.**
The publisher guard at `cleanup()` — `if (g_console == this)` — is what delivered
it, and `diag.free_fn_status` reports `success`, which is `send()`'s own verdict
rather than an inference from the record's presence.

So `native_console_report()` works. **Every kernel-side emitter in this project is
now viable**: M6's `virtio_blk.read_back`, the M8 `launchd.running`/`daemon.spawned`
pair, M7's `dyld.cache_read`, the install dispatcher's `install.registered`, and
the mount emitter's records.

`install.registered` is still absent, and that is expected rather than a failure:
the dispatcher emits it only after a host install frame reaches the guest's RX
virtqueue, and no host client exists that sends one.

**One defect in the instrument, recorded because it produced a wrong sentence on a
right measurement.** The capture script's conclusion branch treated
`status == "success"` as *"the record is still absent, so the loss is downstream"*
— correct for round two, where a success with an absent record would have meant
exactly that. Round three got a success WITH the record present, so the script
printed that sentence anyway. The raw data is unambiguous; the prose was written
for a state that did not occur. **A conclusion template that does not enumerate
the state it is actually in will describe the wrong one.**

### And a correction to what "unpinned" meant for M7 and M8

Both patches were described as **"CODE COMPLETE but unpinned, pending the drain
measurement"**. The mechanical fact is stronger and simpler: **they were never in
the build at all.**

`vendor/UniXNU/patches/xnu/native-dyld-cache-read.patch` and
`zzz-m8-launchd-daemon-events.patch` exist in the working tree and are
byte-identical to their copies under `local-private/tmp/parked-patches/`, but
`git status` in the UniXNU submodule reports both as `??` -- untracked. The pinned
commit `0906997` carries 54 tracked patches and neither of these. `patches/xnu/
*.patch` is globbed from the assembled tree, so an untracked file in a working
tree the build never sees is applied by nobody.

That is why their records were absent from every kernel measured: not compiled
out, not switched off, **not compiled**. The drain risk was a real reason to be
careful, but it was not what was holding them back.

The rule this time: **"CODE COMPLETE but unpinned" has two very different
meanings** -- a file that is applied and switched off by a guard, and a file that
is not in the pinned commit at all. They look identical in a state document and
they have different remedies. `git ls-tree HEAD` on the directory settles it in
one command.

### What is now unblocked, and in what order

M6, M7 and M8 are all CODE COMPLETE and all route through the entry point that now
works. Two things stand between here and measuring them:

1. **M7 and M8 carry defects that predate this fix** and must be corrected first —
   both have `if (native_console_report)` null checks, which is the exact shape of
   the defect that dropped records behind a green build on 41330622788, and M8's
   `daemon.spawned` emits with a computed length `(size_t)at` rather than
   `sizeof(array)-1`.
2. **The drain question is now the only open risk for M8**, whose emitter fires per
   successful exec (measured at ≥75 per boot) against the install service's
   structural bound of one.

The diagnostic records stay in the driver until M6/M7/M8 have been measured — they
are the instrument that just earned its keep, and they come out of both lists
after.

## 2026-11-02 — M6 pinned and measured INCONCLUSIVE, because the capture window was too short

**Build 41416745307, kernel sha256 verified, `virtio_blk.read_back` byte-level in it
at one occurrence.** The boot captured 1675 bytes and the record did **not**
arrive — with `diag.free_fn_status` reporting `success`, so the entry point works
and the emitter simply never ran.

**That is an inconclusive measurement, not a negative one, and the reason is the
instrument.** Every capture this project has taken listens for **300 seconds**
after the kernel handoff. The block driver's emitter sits behind
`result == kIOReturnSuccess && actual == count * 512`, and `result` is success only
after `readSectors()`'s `vr_verify_block()` loop has compared the SHA-256 of every
4096-byte block in the DMA span against the owner-signed manifest. That happens
when the guest actually reads known content — which implementation-plan fact 98
puts late: the same configuration saw 2 processes at t=32 s, 2 at t=170 s, and 116
at t=700 s wall clock, with SpringBoard exec'd at guest t=423 s.

**So a 300-second window cannot see any record that depends on the userland being
up.** That is not specific to M6: `install.registered` needs a host install frame,
M7's `dyld.cache_read` is per-process, M8's pair is per-exec — every one of them
fires later than the window reaches. The three records that DID arrive in every
capture (the IOKit pair and the three `xnu.*`) are all emitted from
`NativeVirtioConsole::start()`, which is early.

### The long window changed nothing, so the window was not the constraint

A 1500-second capture against the same bundle produced **1656 bytes and the same
eight records**. Extending the window 5x did not add a single event. QEMU had
burned 2142 CPU-seconds over that run and was still executing, so the guest is not
hung -- it is running and not reaching M6's emit point.

So the honest M6 verdict is:

> `virtio_blk.read_back` is byte-level in the sha256-verified booting kernel, the
> console entry point it routes through is proven working, and the record did not
> arrive in a 1500-second window on an actively-executing guest. **The emitter's
> condition never held.**

That is UNMEASURED, not FAIL, and the distinction matters: nothing here says the
driver is wrong, only that this boot did not exercise it. The emitter sits behind
`result == kIOReturnSuccess && actual == count * 512`, and `result` is success only
after `readSectors()`'s `vr_verify_block()` loop has compared every 4096-byte
block against the owner-signed manifest. Whether the guest's filesystem reads
reach `doAsyncReadWrite` at all, and whether they reach it with a span that
satisfies the condition, is the next question -- and it needs a diagnostic at the
top of `doAsyncReadWrite`, not another capture window.

**The generalisable lesson, and it is the eighth of this shape: an instrument
limitation and a negative result look identical from the outside.** The 300-second
window produced "no late records"; so did the 1500-second one. The first reading
was "the window is too short" and the second disproved it. Extending the window
was the right diagnostic precisely BECAUSE it could have failed -- and it did, so
the window was exonerated rather than assumed.

An install-frame capture is running against the same kernel, which asks a
different question the same boot can answer.

## 2026-11-02 — the host write IS delivered; the unknown is now guest-side

**Measured.** A host client put **50 well-formed install frames** on the channel
-- 50 successful `sendall` calls, zero errors, across a 1500-second window -- and
`install.registered` did not arrive.

Two things were wrong with the first attempt, and both are worth recording
because each produced a misleading sentence:

1. **`request()` was the wrong API.** It sends the line correctly and then blocks
   reading a reply the dispatcher deliberately never sends; `_read_line()` returns
   the EDK2/DarwinBoot chatter already sitting in the channel, so it raises
   *"the agent channel returned a line that is not JSON"*. That reads as a failed
   write. It is not one -- `sendall()` had already succeeded. The transport's own
   `listen()` docstring records the same trap ("a caller that sends
   `status.request` first and reads second collects those 1015 bytes as its
   reply"). The frame now goes out on the socket directly.
2. **One send was not enough to conclude anything.** The dispatcher's reader is a
   kernel thread that polls `native_console_poll()`, which returns
   `kIOReturnNotOpen` until the console driver publishes the port. A frame sent
   before that is either dropped or sits in the queue, and nothing outside the
   guest can tell which. The frame is now re-sent every 20 s, and the
   dispatcher's own `if (native_install_registered) return;` makes duplicates
   harmless.

So the host's write path works and the unknown has moved inside the guest. Three
causes remain, with different remedies: the thread never started,
`native_console_poll()` never returns data, or the recogniser rejects the frame.
**Two diagnostics split the first two** -- one at the top of the thread, one on
the first non-empty poll -- and build **41441479230** carries them.

### M7 and M8 are bounded, un-nulled, and finally tracked

**They were never in the build at all.** Both files existed in the working tree
and were byte-identical to their copies under `local-private/tmp/parked-patches/`,
but `git status` reported them **untracked** and the pinned commit carried
neither. `patches/xnu/*.patch` is globbed from the assembled tree, so an
untracked file in a working tree the build never sees is applied by nobody. The
drain risk was a real reason to be careful; it was not what was holding them
back.

**Both are now bounded to one record per boot**, by a file-scope flag set BEFORE
the emit and never cleared -- the same structural bound the install dispatcher
has. M7's site is per-process ("dyld calls this path in every process that uses
the shared cache"); M8's `daemon.spawned` fires per exec, measured at ≥75 per
boot. `launchd.running` needed no flag: `load_init_program()` runs once per boot
and the loop returns on the first entry that loads.

**Both null checks are gone.** `if (native_console_report)` is the exact shape
build 41330622788 paid for. M7's `!error` stays because it is about the event's
truth, not the dependency's.

**Three tests were wrong and are fixed rather than deleted.** Two required the
null check on the reasoning that "a strong reference that somehow resolved to null
would otherwise be a call through zero" -- which is the reasoning 41330622788
disproved. The third anchored on the old gate text. The install test's one-write
rule now names the diagnostic variables explicitly, because inferring "this one is
only a diagnostic" from a prefix would let any record exempt itself; and its
null-check scan became escape-aware, because the first version tested the parity
of raw quote characters -- which is EVEN inside a C string holding an escaped
quote, so it skipped nothing.

## 2026-11-02 — build 41441479230 failed, and the guard test earned its keep

**It named a file nobody was editing:**

```
vm_unix.c:2941:3: error: call to undeclared function 'native_console_report'
```

Three defects, all one shape.

**1. M7's extern was inside the guard its call was outside.** The declaration sat
in `#if defined(XNU_NATIVE_VIRTIO_CONSOLE)` and the emitter's call in a second
hunk, outside it. This is the third build on this one placement -- 41359711528
found it in `kern_exec.c:8120`, 41364602699 found the caller/definition variant,
and this one found it in `vm_unix.c:2941`.

**The rule is asymmetric and that is the whole point: the declaration must be
unconditional, the call may be conditional.** A declaration with no definition is
free -- the linker only resolves a symbol something REFERENCES -- so an
unconditional `extern` costs nothing when the emitter is compiled out. A
conditional one is absent exactly when a caller outside it needs it.

**2. M7's emitter was dead code, and its own comment claimed otherwise.** The
patch said `vm_unix.o` receives the macro from `build-xnu-4k-baseline.sh:484`'s
"companion edit". Line 484 passes only `-DXNU_OWNER_CACHE_CARRIER`. So the emitter
would have compiled to nothing even with the declaration fixed -- a false "CODE
COMPLETE". The console macro now rides on that line, and the owner-cache-carrier
stamp is extended to carry it so a change forces `vm_unix.o` to be rebuilt rather
than reusing a stale object. (It rides on the SAME line rather than a second
`EXTRA_MAKE_ARGS` entry because make takes the last `vm_unix.o_CFLAGS_ADD`, and a
second entry would silently discard the carrier one.)

**3. M8 had the same defect, and the new guard test found it before any build
could.** M8's extern was inside the FIRST of its three `#if` blocks and the
`launchd.running` emitter is in the THIRD. It compiled only because
`offline-owner-systemos-mount.patch` happens to carry its own file-scope
declaration -- a coupling nobody asked for and nothing asserts.

### The guard test, and why it is proven three ways

`tests/unit/test_console_declaration_visibility.py` walks every patch's added
lines tracking `#if`/`#endif` depth, remembers the depth at which a declaration
became active, retires it when an `#endif` closes past it, and asserts every call
site has one still active at a depth no greater than its own.

It is proven against the real patches, against the exact shape this build failed
on (which MUST go red), and against the pairing that is CORRECT -- a declaration
and its callers inside the same conditional, which the rule's first revision
wrongly flagged by demanding depth zero unconditionally. **A guard that goes red
on correct code is the same as no guard at all**, and this one nearly did.

Two existing tests were wrong and are fixed rather than deleted: one required the
extern INSIDE the console guard, on the reasoning this build disproved; the other
anchored on the old gate text.

**That is now three tests in this change set written from reasoning about C
rather than from a compiler**, each of which would have let the defect through or
blocked the fix. The pattern is worth naming: the reasoning was *plausible* in
every case ("a call through zero would be worse", "declared whether or not the
archive is on the link"), and it was wrong every time.

Verified by replaying the runner's patch order against the committed tree: all 58
patches apply, and both `kern_exec.c` and `vm_unix.c` now report **zero** call
sites without a visible declaration. Build **41451414922** dispatched on `eece438`.

## 2026-11-02 — M8 is held back: it breaks the SECOND kernel pass

**Build 41451414922 fixed the `vm_unix.c` compile error and then failed on
something the single-pass patch repro could not see:**

```
error: patch failed: bsd/kern/kern_exec.c:183
error: bsd/kern/kern_exec.c: patch does not apply
build-xnu-4k-baseline.sh: line 44: git -C .cache/xnu-work apply --check "$PATCH" failed
```

`build-xnu-4k-baseline.sh` applies `patches/xnu/*.patch` **TWICE** -- once in the
bootstrap pass (`XNU_BOOTSTRAP=1`, which only has to leave the compile records
and `link.filelist` behind) and again after the archive builders. Its loop decides
whether to re-apply with:

```bash
if ! git apply --reverse --check "$PATCH"; then
    git apply --check "$PATCH"; git apply "$PATCH"
fi
```

**That test is not sound.** It assumes a patch that is applied can be reversed.
M8's third hunk inserts inside `load_init_program()`'s `if (!error) {` block --
which is *also* the trailing context of
`offline-owner-systemos-mount.patch`'s `@@ -7186,7 +7520,7 @@` hunk. With M8
applied, the mount patch can no longer be reversed; the loop then tries to apply
it again and fails at the include line its first hunk already changed.

### Isolated by experiment, not by reasoning

`repro-patch-order.py` replays **one** pass and reported "all patches applied" --
it is blind to this, which is how it reached a runner cycle. A new instrument,
`local-private/tmp/repro-patch-twopass.py`, replays both:

| patches present | second pass |
|---|---|
| M7 + M8 | **fails** on the mount patch |
| M7 only | clean |
| M8 only | **fails** |

That names M8 and not M7, and not the extern fix build 41451414922 was dispatched
to verify. **The instrument, not the reasoning, is what attributed this** -- and
the single-pass instrument would have said the tree was fine.

### What ships and what waits

**M8 is removed from the pinned tree.** Its file is preserved under
`local-private/tmp/parked-patches/`, and
`test_the_m8_patch_is_in_the_pinned_tree_at_all` now **XFAILs** with the reason,
the reproduction command, and the two things that would unblock it: move the
emitter to a site that does not split the mount patch's context, or make the
build script's already-applied test stop depending on reversibility. A test that
demands a file be present must say why it is absent, or the absence reads as an
oversight on the next pass.

**M7's extern fix and the `vm_unix.o` CFLAGS edit stay** -- M7 alone passes both
passes, and those two changes are exactly what build 41441479230's compile error
demanded.

**M6, M7 and the install diagnostics are unaffected** and are measured in build
**41461647339**, dispatched on `9e96302`.

## 2026-11-02 — M7 compiles for the first time; the install thread does not run

**Build 41461647339 concluded success, step 21 included.** Its kernel (sha256
verified against `payload.json`) carries, at one occurrence each:

```
dyld.cache_read                1     <- M7, compiled for the first time
virtio_blk.read_back           1     <- M6
install.registered             1     <- the install dispatcher
diag.install_thread_started    1     <- the new thread diagnostic
diag.install_rx_bytes          1     <- the new RX diagnostic
diag.free_fn_status            3
launchd.running                0     <- M8, correctly held back
```

The capture (1654 bytes, 1500-second window) put **50 install frames on the
channel with zero send errors**, and `diag.free_fn_status` arrived as `success`.
None of `dyld.cache_read`, `virtio_blk.read_back` or `install.registered` arrived
-- and **neither did `diag.install_thread_started`**.

### What that last absence means

`diag.install_thread_started` is the FIRST statement of
`native_install_service_thread()`, emitted through the same
`native_console_report()` that delivered `diag.free_fn_status` three lines later
in the same capture. So the entry point works and the record is byte-level in the
kernel. **The thread is not reaching its first statement.**

The `kernel_thread_start(native_install_service_thread, ...)` call is in the
applied `bsd_init.c` at :1325, inside the `XNU_OWNER_ROOT_TRUST && !GPU` guard
whose macro is provably set (the record is in the kernel). So the code is compiled
and the call is there. Whether `kernel_thread_start` returns non-KERN_SUCCESS, or
the thread is created and dies before its first statement, is the next question --
and it is a narrower one than "does the host write reach the guest", which the 50
clean sends have now answered: **it does.**

### The other two are UNMEASURED, and the reason is the window

M7's site is per-process and M6's sits behind a SHA-256 verification of every
4096-byte block against the owner-signed manifest. Both happen later in the boot
than 1500 seconds reaches -- the same instrument limitation the 5x window
extension exonerated rather than confirmed. **Compile VERIFIED, wire UNMEASURED**
for both, and the distinction is the whole point: nothing here says either emitter
is wrong.

### Where the four-milestone blocker stands

M10, M10a, M11 and M12 stay BLOCKED. The dispatcher compiles, the record is in the
booting kernel, the host write is measured working, and the console entry point is
measured working. What is left is that the reader thread does not run.

---

# Coverage table — every PRD requirement, its evidence, and its gap

Read this before reusing any conclusion below it. Each row's "latest evidence" is
the most recent MEASUREMENT, not the most recent code change, and the two are
different columns on purpose.

## The gate, as it stands (measured 2026-11-03, `read-gate.py`)

| gate | verdict | what is missing |
|---|---|---|
| M0 | UNVERIFIED | clean-build artifacts |
| M1 | UNVERIFIED | guest display evidence |
| M2 | UNVERIFIED | config read, boot entry, load evidence |
| M3 | UNVERIFIED | input validation, load info, handoff location |
| **M4** | **PASS** | — |
| **M5** | **PASS** | — |
| M6 | UNVERIFIED | `virtio_blk.read_back` on the wire |
| M7 | UNVERIFIED | volume identity, mount relations, `dyld.cache_read` |
| M8 | UNVERIFIED | `launchd.running`, `daemon.spawned` (patch held back) |
| M9 | **FAIL** | veto: screenshot is not interaction evidence |
| M9a | **FAIL** | veto: wallpaper is not a UIKit frame |
| M9b | BLOCKED | no writable system volume |
| **M10–M12** | **BLOCKED** | the install reader thread does not run |
| WP00 | BLOCKED | `sources.lock.json` declares `frozen=false` |
| WP01 | BLOCKED | unfrozen lock; no Windows cross-build |
| WP02 | UNVERIFIED | run directory, control port |
| WP03 | UNVERIFIED | UniXNU run evidence, DarwinBoot handoff |
| **WP04** | **PASS** | — |

Release verdict: **FAIL**, on `blocking_fail:M9, blocking_fail:M9a`.

## The four states, and which one each row is in

The PRD forbids conflating these. Every claim below carries one.

| requirement | code | compiles | on the wire | notes |
|---|---|---|---|---|
| M4 `xnu.*` | yes | yes | **PASS** | 3 records, every boot |
| M5 `iokit.*` | yes | yes | **PASS** | 2 records, every boot |
| M6 `virtio_blk.read_back` | yes | **yes** | UNMEASURED | site is later than the capture window |
| M7 `dyld.cache_read` | yes | **yes** | UNMEASURED | per-process site; window too short |
| M8 `launchd.running` / `daemon.spawned` | yes | **no** | — | patch held back, see below |
| M10–M12 `install.registered` | yes | **yes** | BLOCKED | reader thread does not run |
| M9 `springboard.running` | **no** | — | — | no emitter exists |
| M9 `springboard.interaction` | **no** | — | — | needs a guest userspace console client |
| M13 network | **no** | — | — | no emitter, no probe evidence |
| M14 GPU/Metal | **no** | — | — | `XNU_NATIVE_OFFLINE_GPU` defaults to 0 |
| M15 Metal IPA | **no** | — | — | downstream of M14 |

## The three gaps that are architectural, not incremental

1. **The guest userspace has no console client.** Every record that has ever
   arrived was emitted by the kernel or by `NativeVirtioConsole` itself. The guest
   has an `IOUserClient` for the port (`NativeVirtioConsoleClient`) and nothing
   opens it. `springboard.interaction`, `uikit.*`, `data.*` and `install.trust`
   all need a userspace agent that does not exist.
2. **`XNU_NATIVE_OFFLINE_GPU` defaults to 0**, so `NativeOfflineGPU` -- the only
   code that reads the console port from userspace's side of the broker -- has
   never been linked into a kernel.
3. **The capture window is 1500 s and the interesting sites are later than that.**
   M6's emitter sits behind a SHA-256 verification of every 4096-byte block; M7's
   is per-process. Extending the window 5x added nothing, so the window is
   exonerated rather than assumed -- which means the gap is the window's LENGTH,
   not its existence.

## What is deliberately NOT claimed

* No IPA has been installed, launched or interacted with. No `.ipa` is in any
  tree.
* No audio, network, launcher or performance measurement exists. The gate has no
  gate for them; the PRD's M13–M15 rows are "not default-blocking for the first
  release unless already a P0 dependency".
* M9/M9a FAIL on the PRD's own vetoes. That is the gate working, not a defect.
* `sources.lock.json` is `frozen=false`. WP00 and half of WP01 are blocked on
  that, and freezing it is a delivery decision, not a code one.

---

# Interval 2026-11-03 (second): M9 is architectural, and the first half of it is now code

## The M8 failure is reproduced, attributed, and controlled

`repro-patch-twopass.py` was BROKEN when I picked it up: `main()` never called
`one_pass()`, so it printed "both passes clean" unconditionally.  An instrument
that always agrees is not evidence.  Fixed to run three passes, and with M8
restored it reproduces build 41451414922 exactly:

    pass 1: applied 58, skipped 0
    *** pass 2: FIRST FAILURE: offline-owner-systemos-mount.patch
    error: patch failed: bsd/kern/kern_exec.c:183

Control, M8 removed: `pass 1: applied 57`, then three clean passes.  So the
coupling is real, it is M8's, and it is the SECOND pass that catches it -- which
is why the single-pass instrument was blind to it.

## M9 is not blocked on a build. It is blocked on an architecture decision.

The PRD veto (`milestones.py:1745-1752`) refuses a homescreen screenshot plus a
mouse click.  Both records it wants must come from a guest channel, and the
guest userspace is Apple's real code and cannot be modified to emit anything.
So the question was: what can the KERNEL measure?

Both halves exist, and this is the first interval that establishes it with
file:line evidence rather than by assumption:

* **the screen.**  `NativeGOPPresenter::start()`
  (`vendor/UniXNU/drivers/NativeMobileDisplay/NativeGOPPresenter.cpp:82-98`) maps
  the scanout writable and cross-checks the mapping against `PE_current_console()`
  -- same `v_baseAddr`, `v_width`, `v_height`, `v_rowBytes`, depth 32, no rotate.
  It also exports `native_gop_ready` and `native_gop_virtual` as `extern "C"`
  globals (`:14`, `:96-97`).  A digest over that mapping is a measurement.
* **the input.**  `NativeHidInput` reaches `handleReport()` from `touchSend()`
  (`:188`), `buttonsAction()` (`:204`) and `tick()` (`:244`), on a 1-second timer
  armed at `:290`.  The attach is default-off in
  `vendor/UniXNU-qemu/package/HidInputProbe.cpp:147` and the staged kernel args
  turn it on -- `offline-hid-input=1 offline-hid-home=1 sheng-touch-hid=1`,
  asserted by `tests/unit/test_staged_kernel_args_provenance.py`.

Digest, deliver, digest again, report only if they differ.  That is a measurement
of "mouse input changed the guest interface", which is the sentence the milestone
actually asks for.

## Two corrections to my own earlier reads

* **`native_hid_input_observed` IS wired.**  I reported it undefined earlier this
  interval.  The grep that produced that was backgrounded mid-run and I read its
  truncated output as a negative.  It is called in five places
  (`NativeHidInput.cpp:190,206,236,240,246`) and DEFINED at
  `vendor/UniXNU-qemu/package/HidInputProbe.cpp:141`.  Same failure shape as the
  QMP `pmemsave` episode: an instrument limitation read as a result.
* **`NativeHidInput` and `NativeMobileDisplay` are separate pinned submodules**
  (`config/sources.lock.json`: `28f9b463b48e` and `eb9c94916482`), not part of the
  `unixnu.commit` pin.  Editing either is its own pin bump.  The interaction
  emitter therefore goes in the DEVICE repo
  (`vendor/UniXNU-qemu`, pinned as `device_repo.commit = efa03c981efc`), where
  `HidInputProbe.cpp` already owns the input hook and already includes
  `<pexpert/pexpert.h>` -- so it can take the digest from `PE_current_console()`
  without touching a single driver.

## What landed

* `vendor/UniXNU/patches/xnu/zzz-native-springboard-events.patch`, pushed as
  `cfeef91` on `unipad/gpu-addon-ngpu-dir`.  Emits `springboard.running` from
  `__mac_execve()`'s `if (!error)` block, where `p` is the switched proc and
  `p_comm` has just been written by `set_proc_name()`.  THE EXTERN IS
  BLOCK-SCOPED: a file-scope declaration would need a second hunk in the include
  block, and that is precisely M8's failure.  Keeping it beside its only caller
  adds no hunk near `kern_exec.c:183` at all, and three passes stay clean.
* `tests/unit/test_native_screen_interaction_probe.py`, committed `7e44d6b`.
  Six tests fail -- neither emitter exists.  That is the contract, not a broken
  test.  The teeth check (`test_the_rule_catches_the_defect_it_names`) PASSES,
  and writing it caught a real bug in the rule itself: the first version matched
  only `!=`/`<`/`>`, which rejected the honest `before == after -> return` gate.
  A test that only ever fails is not evidence it can pass.

## Still unmeasured, and not claimed

* No kernel carrying `zzz-native-springboard-events.patch` has compiled.  CI is
  the first thing that compiles it; this host has no C compiler.
* No kernel carrying it has booted.  `springboard.running` is code COMPLETE and
  wire UNMEASURED.
* The interaction emitter is not written.  It is the half the veto actually turns
  on: `homescreen_screenshot_only` fires on the ABSENCE of
  `springboard.interaction`, so `springboard.running` alone moves M9 from FAIL to
  still-FAIL.

---

# Interval 2026-11-03 (third): a predicted build failure, confirmed line for line

## The prediction

`test_console_declaration_visibility.py` reported, BEFORE build 41497938605
finished:

    native-install-service-dispatcher.patch: call at #if depth 1 with the
    declaration retired          (x2)

Traced it by hand: the install patch's first hunk opens
`#if defined(XNU_OWNER_ROOT_TRUST) && ...`, calls `native_console_report` twice
inside it for `diag.install_thread_start`, CLOSES it, and only then declares the
symbol at unconditional file scope.  The comment above that declaration read
"declared at UNCONDITIONAL FILE SCOPE -- above the guard below, not inside it" --
and that is true of the SECOND guard.  The first guard came earlier.

Checked it could not be rescued by a header: `native_console_report` is declared
only in `.c` files' own externs, never in a header, and the pristine
`bsd_init.c` has no declaration.

## The confirmation

Build 41497938605 failed at step 21, and `kernel.log` (from the build-logs
artifact, because the step log does not echo it) says:

    bsd/kern/bsd_init.c:1348:5: error: call to undeclared function
    'native_console_report'; ISO C99 and later do not support implicit function
    declarations [-Wimplicit-function-declaration]
    bsd/kern/bsd_init.c:1351:5: error: (same)

The predicted error, at the predicted lines, for the predicted reason.

## One wrong read along the way, recorded because it is the recurring shape

I first read a backgrounded `grep`'s truncated output and reported
`native_hid_input_observed` as "declared and called 5 times, defined nowhere".
It is defined, at `vendor/UniXNU-qemu/package/HidInputProbe.cpp:141`.  Same
failure shape as the QMP `pmemsave` episode: an instrument limitation read as a
result.  The declaration-order analysis, by contrast, was checked against the
applied tree and against the test before the build finished, and it held.

## What was fixed, and verified by two instruments

`vendor/UniXNU/patches/xnu/native-install-service-dispatcher.patch` -- the two
externs moved above the FIRST guard, the hunk header recounted from the body
rather than typed.  Verified:

* `test_console_declaration_visibility.py` -- 3 passed, was 1 failed
* `repro-patch-twopass.py` -- three passes clean, 58 applied on pass 1
* the three complete record literals still assemble to JSON (the two
  `diag.install_thread_start` records and `install.registered`)
* the applied tree shows the declaration at line 1313, before the calls at 1362
  and 1365

AN INSTRUMENT BUG WORTH REMEMBERING: `repro-patch-twopass.py`'s `stage()` reads
`git show HEAD:<path>` and falls back to the working tree, so my first
"three passes clean" run validated the OLD patch -- HEAD had not moved yet.  The
declaration still sat after the calls in that work tree.  Re-run after the commit,
it is correct.  An instrument that reads a different source than you think it
does agrees with you either way.

## A red commit, caught and fixed

`ee8bf29` (the pin bump) was pushed with two test files still holding
pre-compaction edits in the working tree.  Measured by stashing: HEAD's versions
fail three tests against the patch they cover.  Fixed in `31a0afc`, pushed, tree
clean.  The lesson is the one the project already records -- an uncommitted
working tree is not a smaller problem than a wrong commit, it is the same problem
one push later.

## Dispatched

Build **41506272367**, `exp-2026-11-03-decl-fix-and-springboard-M10`, stage M10,
target tcg, `unixnu_commit=2f574a96`, `device_commit=efa03c98`.  It carries the
declaration fix AND the `diag.install_thread_start` diagnostic, so it is the
first build that can answer whether `kernel_thread_start` succeeds.  It also
carries `springboard.running`, which will be the first compile of that emitter.

## Still unmeasured

* `diag.install_thread_start`'s status.  The build it was dispatched for never
  compiled the kernel, so the question is still open.
* `springboard.running` on the wire.
* `springboard.interaction` is not written.  It is the half the M9 veto actually
  turns on: `homescreen_screenshot_only` fires on the ABSENCE of
  `springboard.interaction`, so `springboard.running` alone moves M9 from FAIL to
  still-FAIL.

---

# Interval 2026-11-03 (fourth): M9 is code-complete on both halves

## What landed

`springboard.interaction`, the record the PRD veto actually turns on, in
`vendor/UniXNU-qemu/package/HidInputProbe.cpp`, pushed as `068f440` on
`unipad/qemu-metal-bridge-stubs` and pinned as `device_repo.commit`.

The design, and why it is not the obvious one:

* **digest, deliver, digest again, report only if the two differ.**  The veto
  (`milestones.py:1745-1752`) refuses a homescreen screenshot plus a mouse click,
  so the record cannot mean "input was delivered".  An emitter that fires when the
  report reaches the HID stack would clear `p.springboard_interaction` AND clear
  the veto, and the release verdict would improve while the guest stood still.
* **the digest reads `PE_current_console()` plus the presenter's exported
  `native_gop_ready` / `native_gop_virtual` / `native_gop_physical`.**  It does not
  reach into `NativeMobileDisplay`'s private `IOMemoryMap`, and it cross-checks
  `native_gop_physical == video.v_baseAddr` -- the presenter's own proof that the
  mapping being read is the one it validated.
* **armed on events 4, 5 and 6 only** -- the reports actually delivered.  0, 1, 2,
  3 and 7 are probe lifecycle and liveness; arming on those would make the record
  about this driver's own bookkeeping.
* **compared on the timer's own 1-second cadence** (`tick()` at
  `NativeHidInput.cpp:233` calls `native_hid_live_checkpoint()`), which is long
  enough for SpringBoard to have drawn and short enough that the guest has not
  moved on.
* **FNV-1a, eight bytes at a time** where the row allows it, because a
  3048x2032x32 frame is ~24 MB and a byte-wise pass on the work loop's timer
  thread is long enough to delay the next input report.  The tail is byte-wise
  because `v_rowBytes` is only guaranteed to be a multiple of 4.

## The linkage question, answered BEFORE writing the code

The risk was that `HidInputProbe.o` is force-loaded in a configuration whose link
does not carry the console archive, which would make a strong
`extern native_console_report` a link error.  Established from the sources rather
than assumed:

| fact | evidence |
|---|---|
| `HidInputProbe.o` is on the kernel link | `build-native-legacy-display.py:252-254` puts it in `libNativeLegacyDisplay.late.a` |
| that archive is force-loaded | `build-xnu-4k-baseline.sh:625-627` |
| the console archive is force-loaded separately | `build-xnu-4k-baseline.sh:672-677`, gated on `NATIVE_VIRTIO_CONSOLE = 1` |
| the legacy archive is gated on `NATIVE_LEGACY_DISPLAY = 1` | `build-xnu-4k-baseline.sh:625-627` |
| only `qemu_root_defconfig` sets `NATIVE_LEGACY_DISPLAY` | `configs/qemu_root_defconfig:39` |
| that same config sets `NATIVE_VIRTIO_CONSOLE` | `configs/qemu_root_defconfig:47` |
| `qemu_defconfig` sets NEITHER | `configs/qemu_defconfig:8` says it is left out |

So there is no configuration in which the reference is unresolved.  That is what
allows it to be STRONG, and a strong reference is what makes a missing archive
fail the link naming the symbol instead of silently dropping the record -- the
defect that cost build 41330622788 two records behind a green build.

`package/HidInputProbe.cpp` is the source of truth even though the build script
names `kits/qemu/HidInputProbe.cpp`: `board.toml:11` -- `kit = "qemu"  #
package/ of this repository appears as kits/qemu in the build tree`.

## Three of my own wrong assumptions, corrected in the tests

Each was a wrong guess about the architecture, not a defect in the code, and each
would have left a test asserting something false:

1. **`_vendor_tree()` scanned only `vendor/UniXNU`** and reported "no emitter
   exists" against a file in the device repository that has one.  Same class of
   mistake as the escaped-literal one.
2. **The coupling test demanded `NativeHidInput.cpp` emit the record**, on the
   theory that the input driver is where input is delivered.  True of the
   delivery; the emitter belongs one level up, in the file that owns the hook.
3. **The digest test looked for the presenter's private `IOMemoryMap`.**  The
   digest reads its exported globals plus `PE_current_console()`, which is the
   better idiom and needs no driver change.

The escaped-literal decoding now covers C sources as well as patches, for the
same reason it was added for patches.

## What is NOT claimed

* **No changed-byte count in the record.**  Computing one means holding a second
  copy of a ~24 MB frame.  The docstring claimed one before it was written; that
  is corrected rather than left standing.
* **A digest changing does not prove the user's intent was honoured.**  A clock
  redraw would also change it.  The record carries BOTH digests so a reader can
  see which two states were compared.
* **Compile UNVERIFIED, wire UNMEASURED.**  This host has no C compiler.  No
  kernel carrying either emitter has compiled.
* **M9 is still FAIL at the gate.**  Code-complete is not passing: the veto fires
  on wire evidence, not on source.

## Housekeeping

`local-private/tmp` held 30 GB against 14 GB free on F:.  Reclaimed 11 GB by name
(`build-logs-413*`, `bundle-*`, `bundle1x`) -- never `shell/`, never
`pytest-of-*`.  F: is now at 25 GB free.

## In flight

Build **41506272367** (`exp-2026-11-03-decl-fix-and-springboard-M10`) carries the
declaration fix and `springboard.running` and the install diagnostic, but NOT the
interaction emitter -- it was dispatched before the device pin moved.  It is the
build that answers whether `kernel_thread_start` succeeds.  A further build is
needed afterwards to compile the interaction emitter at all.

---

# Interval 2026-11-03 (fifth): three builds, one pairing, and the middle fix was wrong

## The shape of the mistake

The install patch calls `native_console_report` in **two functions**, and they need
declarations at **two different scopes**:

| call site | function | scope needed |
|---|---|---|
| `diag.install_thread_start` (x2) | `bsd_init()` | visible inside `bsd_init()` |
| `install.registered`, `diag.install_thread_started`, `diag.install_rx_bytes`, `native_console_poll` | `native_install_service_thread()` | file scope |

**Build 41497938605** failed on the first group -- `bsd_init.c:1348` and `:1351` --
because the only declaration sat at file scope *after* `bsd_init()`'s closing
brace.  A file-scope declaration is not visible inside a function compiled before
it; it is visible in the functions that follow, which is exactly why the thread
never had a problem.

**My first fix moved that pair up to just before the first guard**, on the theory
that "before the guard" meant "outside the function".  It does not: **the first
guard is INSIDE `bsd_init()`**, so the pair landed inside `bsd_init()` and the
thread lost its declaration.  **Build 41506272367** then failed on the second
group -- `bsd_init.c:1665`, `:1699`, and `:1717` (the last of those
`native_console_poll`).

**So it is an ADD, not a MOVE.**  The file-scope pair stays where it was, correct
for the thread; the two calls inside `bsd_init()` get a block-scoped `extern`
beside them, the same pattern `zzz-native-springboard-events.patch` uses for
`kern_exec.c`.  A declaration with no definition is free -- the linker only
resolves a symbol something REFERENCES -- so the duplicate costs nothing.

## What actually answered the question

**The applied tree, not the patch text.**  The patch text made the first fix look
correct: the declaration sat before the guard, the guard sat before the call, and
the declaration-visibility test agreed.  It was only by applying every patch and
reading the resulting `bsd_init.c` that the scope error became visible -- the
externs were at line 1312, inside a function whose closing brace was at 1372,
while the calls that needed them were at 1665+.

That is the third build failure on this one pairing, and the transferable lesson
is not "move the declaration".  It is: **a patch that calls a symbol from two
functions at two scopes needs a declaration at each, and the applied tree is what
answers the question.**

## Verified, in the order that matters

1. `test_console_declaration_visibility.py` -- passes
2. `repro-patch-twopass.py` -- three passes clean, 58 applied on pass 1
3. **the applied tree** -- block-scoped extern at 1368 precedes the two diag calls
   at 1374/1377; `bsd_init()`'s `}` precedes the file-scope pair at 1419-1420; the
   pair precedes `native_install_service_thread()` at 1695 and all four of its
   calls at 1679, 1713, 1731, 1739
4. the three complete record literals still assemble to JSON

## Also this interval

* The full `tests/unit` suite passes: exit 0, zero FAILED/ERROR, 100% reached.
* `local-private/tmp` held 30 GB against **14 GB free** on F:.  Reclaimed 11 GB by
  name (`build-logs-413*`, `bundle-*`, `bundle1x`) -- never `shell/`, never
  `pytest-of-*`.  F: is now at 25 GB free.
* A push to the device repo's `main` was rejected because `main` is the upstream
  tracking branch (`97b75b1`); the working branch is
  `unipad/qemu-metal-bridge-stubs`.  Corrected, and `main` is untouched.

## Dispatched

Build **41512218642**, `exp-2026-11-03-m9-both-halves-M10`, stage M10, target tcg,
`unixnu_commit=757e46b1`, `device_commit=068f440a`.  It is the first build that
carries BOTH M9 emitters and the install diagnostic, and therefore the first that
can answer three open questions at once: does the kernel compile with both, does
`kernel_thread_start` succeed, and do the two M9 records arrive on the wire.

## Still unmeasured

* `diag.install_thread_start`'s status -- the question the last three builds were
  sent to answer, still open because none of them compiled the kernel.
* `springboard.running` and `springboard.interaction` on the wire.
* M9 remains FAIL at the gate until both records arrive from a guest channel.

---

# Interval 2026-11-03 (sixth): the M9 capture needs no new code, and has two instruments

## The path, end to end, all of it already in the repository

The question this interval answered was "if the kernel compiles, how does
`springboard.interaction` actually get produced?"  The answer is that nothing has
to be written:

1. **QEMU carries the input devices.**  `src/unipad/host/unixnu_boot.py:1985` adds
   `virtio-tablet-device` and `virtio-keyboard-device` whenever the boot has a root
   image -- which is the `qemu_root_defconfig` build that was dispatched.
2. **The guest has a driver for them.**  `package/VirtioInput.cpp` reaches
   `DRIVER_OK` ("QEMU routes its input here"), maps evdev Home (102) and F1 (59) as
   keys and ABS X/Y plus BTN_LEFT/BTN_TOUCH as touch, and on `EV_SYN` with the
   button down calls `publish()` -- which writes the SAME published finger set
   (`native_touch_seq/count/xs/ys/ids`) that the hardware touch driver writes.
3. **`publish()` reaches my emitter.**  `native_touch_published()` ->
   `NativeHidInput::touchNotify` -> `touchSend()` (`NativeHidInput.cpp:188`) ->
   `handleReport()` -> `native_hid_input_observed(6, result)` ->
   **`interaction_arm()`**.
4. **The compare runs on the timer's own cadence.**  `tick()` calls
   `native_hid_live_checkpoint()` as its FIRST statement, before any early return,
   so the 1-second compare cannot be skipped.  That is where
   **`interaction_compare()`** sits.
5. **The host measures the same thing, independently.**
   `guest_runtime.py:378-395` (`reach_springboard`, PRD 5.2 case 2) sends Home over
   QMP, takes `case2-before.ppm` and `case2-after.ppm`, and compares their sha256.
   `e2e.py:1160-1179` fails the case with `case2_springboard_not_operable` /
   "mouse input did not change the guest framebuffer" when they are equal, citing
   PRD 7.4 line 294 -- "a screenshot that does not change proves nothing about
   input".

So there are THREE instruments on one fact, and they can disagree usefully:

| host screendump sha256 | guest screen digest | what it means |
|---|---|---|
| changes | changes | the record is confirmed by two independent measurements |
| changes | does not | my emitter has a defect -- most likely `native_gop_ready` is 0, i.e. the presenter never came online, so `screen_digest()` returns unreadable and the arm never fires |
| does not change | does not | the input did not reach the guest at all, which is an earlier failure than either emitter |
| does not change | changes | one of the two is reading the wrong memory |

The third row is the one worth naming, because "the record is absent" looks
identical from outside for all four rows.  If it happens, the next thing to add is
a one-shot diagnostic at the arm site reporting whether the digest was readable --
not a guess about which row it was.

## What this means for the gate

`_guest_events` (`milestones.py:1408-1424`) keeps only the FIRST record per event
name (`setdefault`), so my four-record bound is more than enough.  The records
travel the identical route as the ones that already pass -- M4 `xnu.*` and M5
`iokit.*` are both PASS -- through `native_console_report` into a file matching
`_GUEST_LOG_GLOBS`.  `_guest_records` (`:1362`) is explicit that a serial log is
NOT a guest channel (PRD 7.4), which is why the emitter goes through the console
port and not the kernel ring.

## Live in flight

Build **41512218642** (`exp-2026-11-03-m9-both-halves-M10`) is the first that
carries both M9 emitters AND the install diagnostic.  Three questions at once:

* does the kernel compile with both (the question the last three builds died on)?
* does `kernel_thread_start` succeed -- `diag.install_thread_start` reports `ok`
  or `failed`?
* do `springboard.running` and `springboard.interaction` arrive?

The live-HID mailbox path (`vendor/UniXNU/scripts/qemu-entry/live-hid.py`) was
checked and set aside: it resolves kernel symbols with `xcrun nm`, which is
macOS-only, and this host is Windows.  The virtio-input route above needs no such
tooling, so it is the one to use.

---

# Interval 2026-11-03 (seventh): the five reviews found four defects, one of them mine

## What the reviews found in my own emitter

Ran the project's five reviews against `springboard.interaction` and four of them
found something real.  All four are fixed in `0efab237`.

**反向审查 -- a memory-ordering hole.**  `NativeGOPPresenter::start()` publishes
`native_gop_physical` and `native_gop_virtual` and THEN raises `native_gop_ready`
(`NativeGOPPresenter.cpp:96-97`), with no barrier on the writer's side and no way
for `HidInputProbe.cpp` to add one there.  On weakly-ordered ARM a reader can
observe `ready=1` with a stale virtual address.  The read side now takes a `dmb
ish` after seeing the flag and before reading the data; the existing
`native_gop_physical == video.v_baseAddr` cross-check is the second line of
defence, because a stale address almost certainly comes with a stale physical one
and the check fails closed rather than hashing the wrong memory.

**性能审查 -- an unbounded work-loop cost.**  Each arm hashes the whole ~24 MB
frame, and a held key delivers a report every 100 ms, so ten reports inside one
compare window were ten hashes on the work loop's timer thread.  `interaction_arm()`
now returns early if already armed, bounding it to at most two hashes per second --
one arm and one compare -- the same order as the registry walk the checkpoint
already does every second.

**规范审查 -- `memcpy` for one aligned copy.**  Replaced with a direct volatile
64-bit load, so the translation unit has no reason to pull in `<string.h>` for it.
ARM64 handles the unaligned tail through the byte loop.

**对质 -- and this one caught a defect I introduced WHILE fixing the others.**  The
first revision of the bounded-copy rewrite carried a `parts[]` loop that copied all
three literals up front and then copied `middle` and `suffix` again after the hex,
producing `{"event":...,"before":"","after":"<hex><middle><hex><suffix>` -- a record
that compiles clean and parses as nothing.  **The exact defect two drafts of the
mount emitter had.**  Caught by reassembling the literals the way the preprocessor
will and parsing the result (`local-private/tmp/check-interaction-emitter.py`),
which is the only thing that can see it.

That last one is the reason this record exists: the emitter's record was ALREADY
VALID when the previous revision was pinned, and the defect appeared in the act of
making the copies safer.  A change that improves one property can break another,
and the only detector is the one that reassembles and parses.

## A red push, and what caused it

The previous revision was committed and pushed with two failing tests.  The cause
was not the tests:

    python -m pytest tests/... -q 2>&1 | tail -2

A pipeline's exit status is the LAST command's -- `tail`'s -- which is 0 whether or
not pytest failed.  The tests were correct; the invocation was not, and the
invocation is what I trusted.  Fixed by redirecting to a file and echoing `$?` on
its own line, and saved to memory as
[[pipe-to-tail-masks-the-exit-code]].  It is the third time this interval an
instrument has told me what I wanted to hear.

## A second instrument defect, found by the same review

`_LITERAL` pairs quote characters and has no idea what a comment is.  A comment
carrying an odd number of double quotes therefore shifts the pairing for every
literal after it, and the record silently stops being found.  **This is not
hypothetical: it happened the moment I added a code comment quoting a malformed
record to explain what NOT to write.**  The explanation disabled the check that
would have caught it.  Comments are now stripped before the scan, while the raw
text is still returned for the structural checks.

## Dispatched

Build **41514522108**, `exp-2026-11-03-m9-final-M10`, stage M10, target tcg,
`unixnu_commit=757e46b1`, `device_commit=0efab237`.  Build 41512218642 was
cancelled at step 13 to carry the reviewed emitter instead.

## Still unmeasured

* Whether the kernel compiles with both emitters -- the question three builds have
  died on, and the one this build is primarily sent to answer.
* `diag.install_thread_start`'s status.
* `springboard.running` and `springboard.interaction` on the wire.
* M9 remains FAIL at the gate until both records arrive from a guest channel.

---

# Interval 2026-11-03 (eighth): the M9a/M11/M12 sample gap is closed on this host

## What was missing, and why it mattered

`observe_samples` (`e2e.py:775-790`) reports a sample as **BLOCKED** with
`sample_ipa_not_found:<name>` when it is absent -- "never a pass, and never a
silent omission (PRD 5.1, appendix A line 480)".  `SAMPLE_SPECS` names two:

| sample | used by |
|---|---|
| `HelloUniPad.ipa` | case 3 -- import and record the input SHA-256 (M9a, M11, M12) |
| `UniPadNotes.ipa` | case 7 -- the persistence test that rewrites and reads back the container marker |

Neither existed under any sample root.  The only copies on disk were pytest
fixtures in `.tmp-bt` temp directories, which are not sample roots and are not
archived.

## Closed, without a third-party IPA

`tests/fixtures/ipa_builder.py` builds both samples ON THE HOST -- `hello_ipa()`
and `notes_ipa()` at `:258` and `:267`, plus `valid_device_ipa()` at `:254` for a
"complete, acceptable iOS Device IPA (arm64 slice, clear binary)".  Built into
`local-private/ipa`, which is a `DEFAULT_SAMPLE_ROOT` (`e2e.py:547`) and is
gitignored, so no Apple or third-party material enters the tree and the
"unauthorized third-party IPAs must never enter the compatibility matrix"
constraint is satisfied by construction -- these are our own artifacts.

Verified after building:

    HelloUniPad.ipa:  896 bytes, 3 members, zip integrity OK
      Payload/HelloUniPad.app/{Info.plist, HelloUniPad, PkgInfo}
    UniPadNotes.ipa: 1108 bytes, 4 members, zip integrity OK
      Payload/UniPadNotes.app/{..., Documents/container-data.txt}

and `_locate_sample` now finds both under `local-private\ipa`.

## What this does and does not unblock

**Does**: the `sample_ipa_not_found` BLOCKED reason is gone for case 3 and case 7.
A capture that reaches those cases will now get past the sample gate and fail (or
pass) on the guest-side question instead.

**Does not**: M9a still needs `uikit.first_frame` from a guest channel, which needs
the app to be installed and launched, which needs M10-M12's install path to work
first.  The sample is a prerequisite, not the milestone.  And `e2e.py:1200` is
explicit that `installed_before` "asks the guest what it already has, and the
guest-side status transport is the piece that does not exist yet" -- so case 3
returns a FAILED observation naming that, rather than a pass with it guessed.

## In flight

Build **41514522108** (`exp-2026-11-03-m9-final-M10`) is queued for a runner.  It
carries the both-scopes declaration fix, `springboard.running`, and the reviewed
`springboard.interaction` emitter.

---

# Interval 2026-11-03 (ninth): M10-M12 and M14-M15 cannot share a kernel

## The mutual exclusion, measured from the sources

The install patch's guard is
(`native-install-service-dispatcher.patch:8` and `:129`):

    #if defined(XNU_OWNER_ROOT_TRUST) && XNU_OWNER_ROOT_TRUST && \
        !(defined(XNU_NATIVE_OFFLINE_GPU) && XNU_NATIVE_OFFLINE_GPU)

and the GPU broker's is (`offline-native-gpu-broker.patch:7`):

    #if (DEVELOPMENT || DEBUG) && defined(XNU_NATIVE_OFFLINE_GPU) && XNU_NATIVE_OFFLINE_GPU

So the two are exact complements:

| `XNU_NATIVE_OFFLINE_GPU` | install dispatcher | GPU broker |
|---|---|---|
| 0 (the default) | compiled | compiled out |
| 1 | compiled out | compiled |

`build-xnu-4k-baseline.sh:306` reads it as
`NATIVE_OFFLINE_GPU=${XNU_NATIVE_OFFLINE_GPU:-0}`, and **neither defconfig nor
`scripts/macos/build-macos.sh` sets it**, so every build so far has been the
install configuration.  That is the right default for the current critical path,
and it means the build in flight does carry the install dispatcher.

The reason is the one the install test file records as its first invariant:
`NativeOfflineGPU::readExact()` drains RX in a lock-step loop and treats ANY short
read as `kIOReturnIOError`, so a second consumer's line buffering would take bytes
the broker was counting on.  The exclusion is deliberate, not an oversight.

**Consequence for planning: M10-M12 and M14/M15 need at least two builds, and the
GPU one cannot be the same kernel that proves install.**

## The GPU build path does not exist yet

`.github/workflows/build-macos.yml` exposes exactly six inputs --
`experiment_id`, `unixnu_commit`, `device_commit`, `stage`, `system_build`,
`payload_target`.  **There is no input for `XNU_NATIVE_OFFLINE_GPU`**, and nothing
in the host script exports it either.  So a GPU configuration cannot be requested
from the workflow at all today; it needs one of:

1. a new workflow input plumbed through to the environment the build script reads, or
2. a defconfig in `vendor/UniXNU-qemu/configs/` that sets it, pinned by a
   `device_repo.commit` bump.

Either is a code change, and until one lands M14 and M15 are BLOCKED on the build
path rather than on the driver.  Recorded as such rather than as "not started",
because the distinction changes what the next action is.

## Why the CI is not settling, and what I changed about my own behaviour

`preflight.yml:37-39` sets `concurrency: group unipad-preflight-${{github.ref}},
cancel-in-progress: true`.  Every push to `main` therefore CANCELS the preflight
already running for `main`.  I pushed eight times in quick succession, so the
preflight was cancelled and restarted eight times and never completed once --
run 41514599784 finished as `cancelled` for exactly that reason.

`build-macos.yml:72-74` uses a different group keyed on `inputs.experiment_id`
with `cancel-in-progress: false`, so the build is not cancelled by the pushes --
but it is queued behind them for the runner.

The fix is behavioural, not a workflow change: stop pushing to `main` until the
in-flight preflight completes, so there is a green CI result to point at.  "Necessary
CI passes" is a completion requirement, and a perpetually-cancelled preflight is
not a passing one.

## In flight

* `41515022936` -- preflight for `2f4828d`, in progress.
* `41514522108` -- build-macos, `exp-2026-11-03-m9-final-M10`, queued.

---

# Interval 2026-11-03 (tenth): the preflight is green for the first time

## What was wrong

`vendor/` is gitignored and untracked (`.gitignore:39`), and the preflight job does
a plain `actions/checkout` with no acquisition step -- so its unit stage runs
against a checkout that has **no vendor tree at all**.  Three tests failed there:

    test_clean_checkout_selfsufficiency.py::test_the_guarded_modules_skip_
        rather_than_error_when_the_vendor_root_is_absent
    test_native_virtio_block_records.py::test_no_second_virtio_blk_event_name_
        exists_anywhere_in_the_fork
    test_native_virtio_console_install_service.py::test_the_install_patch_
        applies_to_its_own_minus_side

Two of them read `vendor/UniXNU` directly and failed with
`"no driver fork files to scan"` and a missing-file error.  The third failed as the
**cascade**: it asserts that the guarded modules skip, so one unguarded read turns
into two red stages.

## Attribution, done before fixing

Run `41497905806` is commit **`c1d8782d`** -- the commit this session started from
-- and it has **exactly the same three failures**.  So this is pre-existing, not
caused by anything here.  Fixed anyway, because "necessary CI passes" is a
completion requirement and a stage that has never been green cannot be cited as
one.

## The fix, verified in both directions

Both tests now call `tests.vendor_paths.require`, which skips when the owning
checkout is absent and fails when it is present and a file is not -- the split the
module's docstring already describes, and the one the 2026-10-24 defect it records
was about.

    with the vendor tree present:                2 passed
    with UNIPAD_VENDOR_ROOT pointing nowhere:    2 skipped, naming why

The skip message is the honest one -- "not verified here" -- and it records that
`tests/integration` is where the check actually runs, because that CI job refuses
to read green when everything in it skips.  A skip is not a pass; it is the
truthful answer to a question this checkout cannot ask.

**Result: preflight run `41520170003` completed `success`.**  First green preflight
of the session.

## A false lead worth recording

My first attempt to add the install test's guard was rejected by my own script with
"the guard is already there".  It was wrong: the check was
`if "require(PATCH)" in text`, and that string already appears in **two other
tests** in the same file (lines 118 and 1234), so it matched whether or not the
target test was guarded.  The site-identifying check has to be the anchor, not a
substring that occurs elsewhere.  Same family as the declaration-visibility false
positive earlier: a check that matches the wrong thing agrees with you either way.

## The build was cancelled, not failed

`41514522108` reported `failure` at the run level, but the job shows
**`cancelled` with 0 steps** -- it never got a runner.  It sat `queued` behind the
preflight runs my own rapid pushes kept starting, and was cancelled before
starting.  Not a build failure, and no kernel.log exists to read.

Re-dispatched as **`41521813113`**, same pins (`unixnu=757e46b1`,
`device=0efab237`).

## Also recorded this interval

**M10-M12 and M14-M15 cannot share a kernel.**  The install patch's guard
(`native-install-service-dispatcher.patch:8`, `:129`) is the exact complement of
the GPU broker's (`offline-native-gpu-broker.patch:7`):

    #if defined(XNU_OWNER_ROOT_TRUST) && XNU_OWNER_ROOT_TRUST && \
        !(defined(XNU_NATIVE_OFFLINE_GPU) && XNU_NATIVE_OFFLINE_GPU)
    #if (DEVELOPMENT || DEBUG) && defined(XNU_NATIVE_OFFLINE_GPU) && XNU_NATIVE_OFFLINE_GPU

`build-xnu-4k-baseline.sh:306` defaults it to 0 and **neither defconfig nor
`scripts/macos/build-macos.sh` sets it**, so every build so far has been the
install configuration.  The GPU milestone therefore needs a second build, and
**that build path does not exist**: `build-macos.yml` exposes six inputs and none
of them is `XNU_NATIVE_OFFLINE_GPU`.  That makes M14/M15 blocked on the build path
rather than on the driver, which is a different next action.

## In flight

* `41521813113` -- build-macos, `exp-2026-11-03-m9-final-M10`, re-dispatched.
* `41520170003` -- preflight, **success**.

---

# Interval 2026-11-03 (eleventh): the GPU configuration can now be requested

## What was missing

M14/M15 were recorded as "not started", which was the wrong label.  The right one
is **blocked on the build path**: the configuration could not be requested at all.

`XNU_NATIVE_OFFLINE_GPU` is read directly by
`vendor/UniXNU/scripts/build-xnu-4k-baseline.sh:306`
(`NATIVE_OFFLINE_GPU=${XNU_NATIVE_OFFLINE_GPU:-0}`), and:

* neither `qemu_defconfig` nor `qemu_root_defconfig` sets it,
* `scripts/macos/build-macos.sh` never exports it,
* `.github/workflows/build-macos.yml` exposed no input for it.

So every build so far has been `XNU_NATIVE_OFFLINE_GPU=0` -- the install
configuration -- and there was no lever to ask for the other one.

## What landed

A `gpu_broker` input (choice 0/1, default 0), plumbed as `XNU_NATIVE_OFFLINE_GPU`
into **both** build steps.  There are two -- "Run macOS build" and "Run macOS build
again (qemu_root_defconfig, PRD 8.1 root chain)" -- and the second is the one that
builds the root chain, so it needed the variable at least as much as the first.

**The plumbing works, and the reason is worth recording**: `unixnu/kernel.py:79`
starts from `dict(os.environ)` and then `env.update(xnu_env(ctx))`.  `xnu_env`
builds `XNU_*` from the defconfig's `CONFIG_XNU_*`, and the defconfig does not set
this one -- so the inherited value survives.  If a defconfig ever DOES set it, the
defconfig wins, which is the documented precedence and is now in the comment so the
next person does not have to re-derive it.

**`gpu_broker=1` raises a warning, not a problem, and the asymmetry is
deliberate.**  The vendor script already refuses the pairing outright
(`build-xnu-4k-baseline.sh:311-313`: "The private offline GPU broker requires the
actual byte transport"), and the validate step cannot see whether the root chain
is on disk -- that is decided at run time by `build-macos.sh`, not by the lock.  A
validation rule that fails runs which are actually fine is worse than no rule.

## What this does and does not do

**Does**: makes the GPU configuration buildable at all.

**Does not**: pass M14 or M15.  And it cannot be the same kernel that proves
M10-M12 -- with the broker on, the install dispatcher is compiled out, so
`install.registered` cannot be emitted.  Two builds minimum, and the GPU one
cannot be cited as install evidence.

Verified: the YAML parses, the input list is as expected, both build steps carry
the variable, and `tests/unit -k 'workflow or build_macos or dispatch or inputs'`
passes.

## In flight

* `41521813113` -- build-macos, `exp-2026-11-03-m9-final-M10`, at the GPU addon
  chain step.
* preflight -- green at `715d25c`; the pushes since then each start a new one.

---

# Interval 2026-11-03 (twelfth): the kernel compiles, the console route is
# MEASURED, and the blocker is the root volume -- not the emitters

## Build 41521813113 succeeded

First kernel to compile with the both-scopes declaration fix, `springboard.running`,
the reviewed `springboard.interaction` emitter, and the install dispatcher's
call-site diagnostic.  Three builds died on the declaration pairing; this one did
not.

Verified before booting:

* `payload-esp/EFI/UniXNU/Payloads/kernel.macho` sha256 =
  `9a930370f56ee7dff81dfaa4ad6e74e2317283b80a2e1df6397157010852d49c`, equal to
  `payload.json`'s `payload_kernel_sha256`.
* every record literal is byte-level in that kernel:
  `springboard.running` x1, `springboard.interaction` x1,
  `diag.install_thread_start` x3, `install.registered` x1,
  `diag.install_thread_started` x1, `diag.install_rx_bytes` x1.

## The console route is MEASURED on a real guest

Booted it.  Eight records arrived, and the last three are the control:

    {"event":"diag.free_fn_from_driver","source":"NativeVirtioConsole.start"}
    {"event":"diag.free_fn_status","status":"success"}
    {"event":"diag.control_from_driver","source":"NativeVirtioConsole.start"}

plus `xnu.page_tables`, `xnu.exception_vectors`, `xnu.interrupts`,
`iokit.service_match`, `iokit.driver_probe` -- all emitted from
`NativeVirtioConsole.start()`.

**So `native_console_report()` reaches the host from a real boot.**  That was the
premise under every emitter in this project and it had never been measured end to
end on a guest carrying them.  It now is.

## And then the boot stopped

Nothing after the console driver's start.  `diag.install_thread_start` did not
arrive, and neither did `install.registered`, `diag.install_thread_started` or
`diag.install_rx_bytes`.

**The cause is not the code.**  The install block sits in `bsd_init()` immediately
before `load_init_program(p)`, and the guest never got there.

## The blocker, measured rather than inferred

`owner-data-material-41521813113`'s `material-manifest.json`:

    os_root_image    bytes=           0  044-76040-038.dmg
    systemos_image   bytes=           0  044-76034-038.dmg
    volume_uuid      bytes=           0  (empty)
    data_seed        bytes=     1499280  owner-data-1g.dbr
    trust_cache      bytes=       61356  original-static-system.bundle

**The OS root and SystemOS volumes are ZERO BYTES.**  They were never produced.
The boot inputs carry only the kernel, devicetree, DarwinBoot, a 4.8 MB
`scratch-data.img` Data ramdisk, and the trust cache -- so the guest has no root
volume to mount and stops, which is exactly what the build script predicts:
"the guest either mounts the owner-signed OS volume as its root or stops at
'Failed to mount root device'".

This is the same fact `_VOLUME_BLOCKER` records
(`milestones.py:1972-1981`): "this host has no APFS read/write implementation, so
the 6.08 GB os-root raw image cannot be produced in the state the guest would
see".  It is now MEASURED rather than merely asserted -- the manifest says 0 bytes.

## What this reframes

**M9, M9a, M9b and M10-M12 are all blocked on the root volume, not on their
emitters.**  Every one of them needs a guest that gets past root mount:

| record | needs |
|---|---|
| `springboard.running` | SpringBoard to exec, which needs launchd, which needs a root |
| `springboard.interaction` | a screen and input, which needs a booted userspace |
| `data.written` / `data.shutdown` | a writable Data volume and a running guest |
| `install.registered` | the install block in `bsd_init()`, past root mount |
| `diag.install_thread_start` | the same |

So the emitters being code-complete is necessary but not close to sufficient, and
the next action is the root volume, not another emitter.  That is a different
problem from the one the last three builds were fighting.

## What is NOT claimed

* `diag.install_thread_start`'s status is still UNMEASURED.  The record did not
  arrive, and the reason is the boot stopping early -- not the answer the
  diagnostic was built to give.
* No SpringBoard, no IPA, no interaction, no persistence.
* M9 remains FAIL at the gate; M9a and M9b remain blocked.
* The APFS read/write gap is not closed by anything here.

## In flight

* `41521813113` -- build-macos, **success**.
* preflight -- green.
* A further capture cannot answer the install question until the boot carries a
  root volume; the material manifest is the evidence for that, not a guess.

---

# CORRECTION, same interval: the root volumes ARE produced.  I had it backwards.

## What I recorded, and why it was wrong

The previous section says the OS root and SystemOS volumes are 0 bytes and "were
never produced", and reads that as confirming `_VOLUME_BLOCKER`'s "no APFS
read/write implementation".  **That is wrong, and it is the kind of wrong that
would have sent the next three builds at a problem that does not exist.**

The material manifest's 0 bytes is the **deletion**, not a failure.

## The measurement

From run 41521813113's own log, the chain step:

    2026-11-02T21:14:05Z chain: placed 044-76034-038.dmg (3303014400 bytes)
                                        at vendor/UniXNU-qemu/.cache/firmware/target-volumes
    2026-11-02T21:14:05Z chain: placed 044-76040-038.dmg (5157406069 bytes)
                                        at vendor/UniXNU-qemu/.cache/firmware/target-volumes

3,303,014,400 + 5,157,406,069 = 8.46 GB, matching the script's own "7.88 GiB of
volumes".  They are **moved, not copied** ("7.88 GiB renamed within one filesystem
is free, and a copy would need 7.88 GiB that is not there").

And then step 19, "Remove the root chain's disk volumes", runs:

    rm -f "$volumes"/*.dmg
    echo "removed the OS and SystemOS volumes from $volumes (Apple firmware material, PRD ...)"

**The deletion is deliberate and correct** -- Apple firmware material must not
survive the job (PRD 12).  The manifest is collected after it, so it honestly
records 0 bytes.

## So what actually stopped the boot

Not a capability gap.  **A packaging gap**: the boot inputs are collected after the
volumes are deleted, so they carry the kernel, devicetree, DarwinBoot, a 4.8 MB
`scratch-data.img` Data ramdisk and the trust cache -- and no root volume.  The
guest therefore has nothing to mount and stops, which is exactly what
`build-macos.sh` predicts: "the guest either mounts the owner-signed OS volume as
its root or stops at 'Failed to mount root device'".

## What this changes about the plan

The critical path is **not** "implement APFS read/write".  It is "get the volumes
that are already being produced, correctly, to the boot".  Three candidate routes,
in the order I would try them:

1. **Deliver them through the channel that already exists.**  Step 29 is "Deliver
   the encrypted material (private channel)".  The `owner-data-material` artifact
   carries `kernelcache.decompressed.enc`, so the encrypted-delivery mechanism
   works; it simply is not being asked to carry the volumes.
2. **Have the boot-inputs collection run before step 19.**  The volumes exist at
   21:14 and are deleted at 21:16; the boot inputs are collected later.  This is a
   step-ordering change in the workflow, and it is the cheapest of the three --
   but it puts 8.46 GB of Apple firmware material into a GitHub artifact, which
   PRD 12 may not permit.  That has to be checked before doing it.
3. **Re-extract them locally for the capture.**  `extract-target-volume.py` reads
   the IPSW, and this host has the IPSW pinned under `local-private/`.  The
   volumes are reproducible from it, so a capture does not need the runner's copy
   at all -- only the IPSW, which is already local.

Route 3 is the one that does not touch the workflow or PRD 12, and it is the one
to try first for a capture.  Routes 1 and 2 are for the deliverable.

## What is still true, and what is not

**Still true:**
* Build 41521813113 succeeded -- first kernel to compile with both emitters.
* The payload kernel's sha256 matches `payload.json`; every record literal is
  byte-level in it.
* `diag.free_fn_status` reports `success` on a real boot, so
  `native_console_report()` reaches the host.  MEASURED.
* `diag.install_thread_start` did not arrive, because the boot stopped before
  `bsd_init()`'s install block.
* M9, M9a, M9b and M10-M12 all need a guest past root mount.

**Not true, and withdrawn:**
* "the OS root and SystemOS volumes are 0 bytes / were never produced".
* "the blocker is the APFS read/write implementation".

`_VOLUME_BLOCKER`'s wording may still be right about M9b's *writable Data volume*
-- that is a different question from whether the read-only OS volumes can be
produced, and this measurement says nothing about it.  It must be re-measured, not
inherited from a conclusion I have just withdrawn.

---

# Interval 2026-11-03 (thirteenth): route 3 is running -- the volumes are being
# decoded to raw

## What is in flight

`local-private/tmp/decode-udif-volumes.py` is decoding both UDIF volumes to raw
disk images QEMU can attach, one at a time, deleting each UDIF only after its raw
image passes the GPT check.

Measured progress on the OS volume: 328/3814 chunks in 244 s, so ~47 minutes for
it and ~30 more for SystemOS.  The raw image is 6,077,580,800 bytes, pre-allocated
at full size because `udif.py:520-528` records that NTFS does not honour a sparse
request from `open("wb")` or `truncate` -- "a 256 MiB all-zero-fill image costs
256 MiB of free space", and an earlier version of that docstring claimed
otherwise and was wrong.

Disk: F: is at 13 GB free with the OS raw pre-allocated.  The arithmetic works
only because each UDIF is deleted as soon as its raw image passes the GPT check:
13 GB now, +5.16 GB when the OS UDIF goes, -~4 GB for the SystemOS raw, +3.3 GB
when its UDIF goes.

## Why this is the right route

The volumes the runner produced are byte-identical to what this host can
re-extract from the local IPSW:

    os_root_image    local sha256 == manifest sha256   3f99b859...
    systemos_image   local sha256 == manifest sha256   b4908cde...

So a capture does not need the runner's copy at all -- only the IPSW, which is
already local at `local-private/ipsw/iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw`
(6.84 GB).  That is route 3, and it touches neither the workflow nor PRD 12.

## The stale caveat this run overturns

`unixnu_boot.build_argv:1682-1696` warns that attaching a root volume "needs a
kernel carrying `CONFIG_XNU_NATIVE_VIRTIO_BLOCK`; without one the device is
attached and never enumerated at all, which is the state of every kernel on this
host (measured 2026-10-25)".

**That measurement is now out of date and must not be inherited.**  The kernel
build 41521813113 produced sets `CONFIG_XNU_NATIVE_VIRTIO_BLOCK=y`
(`qemu_root_defconfig:29`), names the verified bundle archive at `:50`, and
carries `NativeVirtioBlock` 66 times plus `virtio_blk.read_back` once --
verified byte-level in the payload kernel.  So this run is the first on this host
for which the root volume can actually be enumerated.

## What the capture will measure, and in what order

With the volumes attached, `build_argv` also adds `virtio-tablet-device` and
`virtio-keyboard-device` (`unixnu_boot.py:1985`), which is what makes the input
path exist at all.  So this one boot can answer, in order of how early they fire:

1. **`diag.install_thread_start`** -- `bsd_init()`, before launchd.  The question
   three builds were sent to answer.
2. **`install.registered`** -- after a host install frame reaches the RX queue.
3. **`springboard.running`** -- if SpringBoard execs at all.
4. **`springboard.interaction`** -- the guest's screen digest around a Home press,
   with the host's QMP screendump sha256 as an independent twin.

Records 1 and 2 need only a root mount.  Records 3 and 4 need a booted userspace,
which needs the root mount AND the display AND the input path.  Each is a
strictly later failure than the last, so a capture that stops early still tells
us something.

## Not claimed

* The decode has not finished.  No boot with the volumes has run.
* Nothing here says the guest reaches SpringBoard.  The display path
  (`offline-legacy-*` boot args) and the AMFI/PMGR chain are separate gates that
  have never been passed on this host.
* M9 remains FAIL; M9a and M9b remain blocked.

---

# Interval 2026-11-03 (fourteenth): install.registered ARRIVED.  The load-bearing
# unknown the install test file names is closed.

## The answer to the question three builds died trying to answer

    {"event": "diag.install_thread_start", "status": "ok"}

**`kernel_thread_start` SUCCEEDED.**  Builds 41497938605 and 41506272367 were sent
to answer this and neither compiled the kernel; 41521813113 did, and this capture
read the answer off a real guest.

## The whole chain, measured end to end

Six records, all byte-level in `local-private/boot51/install-thread-start/guest.log`
(2144 bytes):

    {"event":"virtio_blk.read_back","driver":"NativeVirtioBlock","verified":true,
     "source":"NativeVirtioBlock.readSectors"}
    {"event":"diag.install_thread_start","status":"ok"}
    {"event":"diag.load_init_program_entered","source":"XNU.load_init_program"}
    {"event":"diag.install_thread_started","source":"XNU.bsdinit_task"}
    {"event":"diag.install_rx_bytes","source":"XNU.native_console_poll"}
    {"event":"install.registered","source":"XNU.native_install_service"}

Read in order, that is the entire path the project had been unable to measure:

1. the root volume was enumerated and read back by `NativeVirtioBlock` -- **M6**,
   previously UNMEASURED;
2. `kernel_thread_start` returned `KERN_SUCCESS`;
3. the thread's FIRST statement ran (`install_thread_started`), so it did not die
   before its body;
4. `native_console_poll()` returned data (`install_rx_bytes`), so the RX path
   delivers;
5. the dispatcher recognised the host's install frame and emitted
   **`install.registered`** -- the one event that clears `_INSTALL_SERVICE_BLOCKER`,
   named by M10, M10a, M11 and M12.

The install test file calls (4) "the one load-bearing unknown... Nothing in the
repository measures it correctly".  It is now measured.

## The host-side twin, independently

    screendump before: 8833260ecd372e44f0c0bc52260880f7a31905b1b6ffcba50cb25709aedae4a0
    screendump after:  ed8f1baeca1ffc20ad9d69d84ebcfc46ee4cb4f472b09567b5738ec48c44028c
    the screen changed:  True

A QMP screendump, a Home press over the virtio keyboard, another screendump, and
the two sha256 differ.  That is the same fact `springboard.interaction` measures
from inside the guest, from the other side -- so the input path reaches the guest
AND the screen responds to it.  `input devices attached: True`, because
`build_argv` only adds `virtio-tablet-device` and `virtio-keyboard-device` when a
root image is present (`unixnu_boot.py:1985`).

## What made it possible, and it was not a code change

The root volume.  The runner produces it and then deletes it (step 19, PRD 12), so
the boot inputs carry none and the first capture stopped after the console driver's
start.  This run re-extracted it from the local IPSW:

* `044-76040-038.dmg` sha256 == the material manifest's `os_root_image.sha256`
  (`3f99b859...`), so the local copy is byte-identical to the runner's;
* it is a UDIF container, and `build_argv` refuses one outright -- "QEMU's
  format=raw would hand the guest the compressed container, which publishes no
  IOMedia" -- so it had to be decoded;
* the decode took 4679 s at the ~2 MB/s the module documents, and produced
  6,077,580,800 bytes whose GPT check passed with both CRCs OK;
* the SystemOS DMG turned out NOT to be a UDIF ("does not end in a UDIF 'koly'
  trailer"), so it is still un-decoded and was not attached.

## The one thing this run did NOT do

No `springboard.running` and no `springboard.interaction`.  With no cryptex the
boot panics at `offline owner SystemOS mount failed: 80`
(`offline-owner-systemos-mount.patch:419-420`), which is inside
`load_init_program()` -- AFTER the install block, which is why the install records
still arrived, and BEFORE launchd, which is why the M9 records did not.

So the ordering is the reason this capture could answer the install question
without the second volume, and it is also the reason it cannot answer M9.

## What is now measured that was not before

| fact | status |
|---|---|
| the console route reaches the host from a real boot | **MEASURED** (`diag.free_fn_status: success`) |
| `kernel_thread_start` succeeds | **MEASURED** (`status: ok`) |
| the reader thread's first statement runs | **MEASURED** (`diag.install_thread_started`) |
| `native_console_poll()` returns RX data | **MEASURED** (`diag.install_rx_bytes`) |
| a host write reaches the guest's RX virtqueue and is recognised | **MEASURED** (`install.registered`) |
| the root volume is enumerated and read back | **MEASURED** (`virtio_blk.read_back`, `verified: true`) |
| input reaches the guest and the screen responds | **MEASURED** (screendump sha256 changed) |
| `springboard.running` / `springboard.interaction` | still UNMEASURED -- needs the cryptex |

## Not claimed

* No SpringBoard, no IPA, no interaction *recorded by the guest*, no persistence.
* M9 remains FAIL at the gate: the veto fires on the absence of
  `springboard.interaction`, and neither M9 record arrived.
* The SystemOS volume is still undecoded, so the next capture cannot reach
  launchd until it is.
* This is one boot.  It has not been repeated, and a single successful run is not
  a stable measurement.
\n
---

# Interval 2026-11-03 (fifteenth): `system.mount` arrived.  So did a withdrawal.

## What this run measured that the last one could not

The boot reached the SystemOS mount.  Two records, byte-level in
`local-private/boot53/springboard-boot/guest.log`:

    {"event":"system.mount","mounted":true,"volume_uuid":"8846FE78-...","filesystem":"apfs"}
    {"event":"system.volume_identity","volume_uuid":"8846FE78-...","filesystem":"apfs"}

That clears **M7** (`p.system_mount`, `p.system_volume_identity`) and satisfies
the `system_volume_mounted` half of the M9b blocker, which the patch says the
same event clears.  With the volumes attached, `build_argv` also adds
`virtio-tablet-device` and `virtio-keyboard-device` (`unixnu_boot.py:1985`), so
the input path exists for the first time on this host.

## The caveat that has to travel with that record

**The `volume_uuid` in those two records is a compile-time constant.**  It is
`XNU_OWNER_CACHE_VOLUME_UUID`, which defaults to the literal
`8846FE78-2910-4297-AF97-D59BB6F8FC16`
(`offline-owner-systemos-mount.patch:98-99`) and is concatenated straight into
the record.  It is **not** read from the device.

So the record proves `mount()` returned 0 and that `cache_authorized` held.  It
does not prove *which* device was mounted.  Anyone citing it as evidence that the
right SystemOS volume mounted would be over-reading it, and the emitter's own
comment invites exactly that reading by saying "the mount is complete and
authorized by this point".  It is true of the mount call and silent about the
device.

## WITHDRAWN: the host-side twin as evidence that input reaches the guest

Interval 14 recorded:

    the screen changed:  True

and read it as "the input path reaches the guest AND the screen responds to it".
**That is withdrawn.  The measurement cannot support the claim**, because of two
defects in my own capture:

1. **Home was pressed at T+0**, immediately after the kernel hand-off, when the
   framebuffer still showed the UniXNU boot menu.  There was no SpringBoard to
   respond to it.
2. **The "after" shot was taken at the end of the whole 900 s budget.**  Two
   sha256 values 15 minutes apart differ on *any* boot that progresses, so
   `changed: true` measures the boot, not the key press.

The guest-channel half of that interval stands -- `install.registered` is real
and is the answer to the question the stop hook named.  The input half does not,
and it was reported in the same table as if it did.  That is the defect: not a
wrong number, a right number attached to the wrong claim.

## What the framebuffer actually shows, and what that rules out

`local-private/boot53/install-thread-start-boot/shots/after.ppm`, converted and
read: the **UniXNU boot menu**, not a panic screen.  Coloured top bars,
"DARWINBOOT: inputs validated; exiting boot services", the kernel/entry/args/video
banner, "Exiting boot services. A green bar across the top means the kernel entry
code ran.", the highlighted entry "QEMU virt: source 4K XNU", and
"drivers 1 loaded / 0 failed".

`unixnu_boot.py` records that the panic screen is **the only kernel output on
this frame buffer**.  So a panic would have replaced the menu, and it did not.
**The kernel did not panic.**  The boot reached `system.volume_identity` and the
kernel kept running.

## The one structural gap this run exposed

Everything after `system.volume_identity` is invisible:

| step | code | observable? |
|---|---|---|
| the Data mount | `native_owner_data_prepare()` | **no** -- the function emits nothing; every failure is `goto done` |
| a Data mount failure | `panic("offline scratch Data mount failed: %d")` | **no** -- `panic()` writes to XNU's printf console |
| the launchd load loop | `printf("attempting to load %s")` | **no** -- printf only |
| SpringBoard exec | `springboard.running` | yes, but gated on `p_comm == "SpringBoard"` |

And there is no way to read XNU's printf console from the host:

* the kernel console is **not on the serial port** (`unixnu_boot.py:468-469`,
  measured); `serial.log` ends at the hand-off and the kernel adds nothing;
* there is **no serial console patch** in `vendor/UniXNU/patches/xnu/`;
* there is **no embedded panic log**: the ADT has no `embedded-panic-log-size`,
  so `gPanicBase` stays 0 and the panic text is unreachable from the host.

So a hang and a panic look identical from outside, and both look identical to a
boot that is merely slow.  **This is the diagnostic gap on the critical path**,
and it is the thing to close before spending another 70-minute boot on a guess.

## The timing evidence, and what it does and does not say

All 2601 bytes of the capture arrived within the first ~60 s (two reads:
`+924`, then `+1677`), and then **nothing arrived for the remaining 540 s**
while the host sent 27 more install frames.

That is consistent with a TCG userspace boot that needs longer than 900 s, and
`springboard.running` is gated on `p_comm == "SpringBoard"`
(`zzz-native-springboard-events.patch:47`), so the whole chain from launchd
through to SpringBoard's exec has to finish first.  It is equally consistent with
a hang in the unobservable region above.  **This run cannot separate those two**,
and the previous interval's "the guest either mounts ... or stops" framing did
not separate them either.

## One risk, recorded and NOT concluded

`XNU_OWNER_DATA_VOLUME_UUID` is supplied from
`$DEVICE_DIR/.cache/firmware/owner-data/seed.json`
(`build-macos.sh:2211`, `:2661`).  **This host has no
`.cache/firmware/owner-data/` directory at all**, so a local build would export an
empty value -- which passes the `#ifndef` guard (it is defined, as `""`) and then
makes `native_ram_block_volume_seed("")` return 0, i.e. `ENOENT`, i.e.
`panic("offline scratch Data mount failed: 2")`.

Separately, `vendor/UniXNU/scripts/build-cpu-desktop-kernel.py:7` builds with
`18AB5557-DC2D-469B-8758-855E60F5B0DF`, while
`local-private/firmware/verify-data-mount.py:117-123` records that the last
measured `seed.json` UUID was `18AF0FFB-77A7-4878-B999-70DD8B4076DC` and calls
the discrepancy "an open item on the runner side".

**Neither literal is in the kernel this run booted** (scanned: 19 UUID literals,
neither value among them), so the runner's build used a third value that I have
not located, and I have not read the Data seed's own UUID.

**This is a risk, not a finding.**  I do not know that the Data mount failed, and
the boot-53 kernel came from the runner where `seed.json` did exist.  What I know
is that the failure mode is real, documented, and completely unobservable.

## Next action, in order

1. **A 3600 s capture with a corrected Home twin** --
   `local-private/tmp/capture-springboard-boot.py`.  Home every 600 s from
   T+600, each press bracketed by a screendump immediately before and one 5 s
   after; a screendump every 120 s for a screen timeline.  The budget is 4x the
   last one because the timing evidence is consistent with a slow boot.
2. **If that still produces no `springboard.running`**, the next step is NOT
   another boot.  It is to close the observability gap: emit a record at every
   stage of `native_owner_data_prepare()` (which stage, which error) and at each
   launchd attempt, so the next run says where it stopped instead of stopping
   silently.

## Not claimed

* No SpringBoard, no IPA, no interaction recorded by the guest, no persistence.
* M9 remains FAIL: neither M9 record arrived.
* M7 is measured **with the constant-UUID caveat above**, not as proof of device
  identity.
* This is one boot with the volumes.  It has not been repeated.
* The 3600 s capture is running; its result is not in this record.
\n
---

# Interval 2026-11-03 (sixteenth): `dyld.cache_read` arrived.  So did a correction
# about the input path.

## The furthest the boot has ever gone

    {"event":"dyld.cache_read","source":"XNU._shared_region_map_and_slide"}

arrived at **t=914 s** of a 3538 s run, 853 s after the last of the install
records.  That is the furthest any boot on this host has reached.

`native-dyld-cache-read.patch:41` bounds the emitter to **one record per boot**
on purpose -- the site is per-process, and the transport is synchronous with a
2000 ms deadline per record.  So one record proves **at least one** userspace
process exec'd and dyld ran in it.  It does not say how many, and it does not say
which.  What it does say is that `load_init_program()` got past the launchd
attempts, that `native_owner_cache_prepare()`'s
`DYLD_SHARED_CACHE_DIR` environment reached a real process, and that the SystemOS
union mount is serving the shared cache to it.

That is the first evidence of userspace execution on this host.

## The timeline, which is the part that matters

    t=   15 s   +945 bytes    (xnu.*, iokit.*, virtio_blk.read_back)
    t=   46 s   +1339 bytes   (diag.install_thread_start, install.registered)
    t=   61 s   +338 bytes    (system.mount, system.volume_identity)
    t=  121 s   the screen stops changing, permanently
    t=  914 s   +72 bytes     (dyld.cache_read)
    t= 3538 s   the budget ends

Everything the kernel has to say about itself arrives in the first 61 s.  Then
853 s of nothing, one record, and 2624 s of nothing.

## The screen never advanced, and now that is measured rather than inferred

30 screendumps, 120 s apart, hashed and deleted.  **Two distinct images**: the
first at t=0 (`2e2910ab...`, mid-boot) and the second from t=121 s onward
(`8833260e...`).  The second is the same image for the remaining 3417 s.

So the framebuffer settled onto the UniXNU boot menu by t=121 s and never moved
again, while the guest channel delivered records at t=914 s.  The kernel was
executing userspace code with a display that showed a boot menu.  This is the
same gap `unixnu_boot.py` records ("XNU's video console draws nothing here
despite -v") now with a timeline attached: it is not that the console draws late,
it is that it never draws at all on this path.

## No SpringBoard

`springboard.running` and `springboard.interaction` did not arrive in 3538 s.
The emitter is byte-level in the booting kernel (count 1 each), so the absence is
a real absence and not a missing emitter.  M9 remains FAIL.

## CORRECTION to interval 15: the input path has never been measured

Interval 15 withdrew the host-side twin and gave the reason as timing -- Home
pressed at T+0, the "after" shot taken at the end of the budget.  **That is not
the reason, and the real one is worse.**

    Home press failed: UniPadError: qmp client is already connected

on **all six presses** of this run, and on the one press of boot 53
(`"input_error": "UniPadError: qmp client is already connected"`, which I read
past).  `QmpClient.__enter__` calls `connect()`
(`src/unipad/host/qmp.py:283-285`), and the capture called `connect()` again
inside the `with` block.  The second connect raises, so **no key was ever sent to
any guest on this host.**

So:

* the input path is UNMEASURED, not "measured with a timing defect";
* `input devices attached: True` proves the argv carries
  `virtio-tablet-device` and `virtio-keyboard-device`, and nothing more;
* every earlier claim that a Home press reached the guest is withdrawn.  There
  was no such measurement.

The fix is one line -- drop the redundant `connect()` -- and it is applied.  The
next capture measures it for the first time.

## The observability gap is closed in source

`native_owner_data_prepare()` emitted nothing, so a Data mount failure, a hang in
the launchd loop and a slow boot were indistinguishable from the host.  It now
emits:

    {"event":"diag.owner_data_status","stage":N,"error":E,
     "source":"XNU.native_owner_data_prepare"}

at its single `done:` label, which every path in the function reaches.
`native_owner_data_stage` was already maintained by all of them, so the record
costs nothing to produce and turns "the guest went quiet" into "the guest stopped
at stage N": 0 the seed was not found or its geometry was rejected, 1 the copyout
of the mount arguments failed, 2 `mount()` failed, 3 `vnode_lookup` failed, 4 the
mount verified.

**This is source only.  It is not compiled and not measured** -- this host has no
C compiler, so CI is the first thing that compiles it and a matched kernel build
is the first thing that can emit it.

## Three defects my own edit caused, and what caught each

Recorded because each one was invisible to the tool I reached for first.

1. **A hunk header that disagreed with its body.**  I added 85 lines to hunk 1 and
   left `+332` in the header.  `git apply` rejects the file as "corrupt patch at
   line 431" -- a message that reads like file damage.  Caught by
   `local-private/tmp/check-hunk-headers.py`, now
   `test_every_hunk_header_matches_the_body_it_has`.
2. **The later hunks' new-side starts then pointed inside hunk 1's output.**
   `zzy-native-gpu-addon-mount.patch` failed at `kern_exec.c:7193`, because its
   six-line context block is exactly `char native_owner_data_device_path[64];` /
   blank / `static int` / `native_owner_data_prepare(...)` / `{` -- and I had put
   28 helper lines in that gap.  Fixed by moving the helpers above the globals.
3. **A duplicate `extern`.**  My emitter carried a block-scoped
   `native_console_report` on the reasoning that
   `native-install-service-dispatcher.patch` needed one at both scopes.  That
   patch needed it because ITS caller sits above ITS declaration; here the
   file-scope declaration is 888 lines above the function and outside every
   guard, so the copy was redundant.  Caught by
   `test_the_console_reference_is_strong_not_weak`, which asserts exactly one
   declaration.  **That test was right and my comment justifying the duplicate
   was wrong**, so the comment was corrected rather than the test weakened.

## And the reason (1) was not caught by the tool I trusted

`repro-patch-twopass.py` staged patches with `git show HEAD:<path>` -- the
**committed** bytes.  I edited the patch, ran it, and it printed "ALL PASSES
CLEAN" about a patch that did not contain my edit.  A green result from an
instrument that is not looking at the change under test is worse than no result.

It now stages from the working tree and prints a note when the two differ.  This
is the third time this session that an instrument has had to be validated before
its answer could be used, and the first time the instrument was my own.

## A known defect fell out as a side effect

`test_the_unreachable_hunk_starts_are_exactly_the_known_set` recorded
`("offline-owner-systemos-mount.patch", 346)` as an unreachable hunk start, with a
comment saying the offset was "deliberately left alone".  Recomputing the
new-side starts as `old_start + the running (new-old) delta` -- the relation a
unified diff actually has -- made the patch internally consistent, and the entry
disappeared.  The patch still applies in all three idempotency passes.

So that offset was not a design decision, it was an uncorrected defect that
`git apply` happened to tolerate.  The entry is removed and the correction
recorded in its place, rather than renumbered -- renumbering it would have
preserved the wrongness.

## Not claimed

* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* `dyld.cache_read` proves at least one userspace process exec'd.  It does not
  prove launchd completed, and it does not name the process.
* The input path is UNMEASURED.  The one-line fix is applied and not yet run.
* `diag.owner_data_status` is source only -- not compiled, not booted.
* This is one 3538 s boot.  It has not been repeated.
\n
---

# Interval 2026-11-03 (seventeenth): the Home press works now, and it changed
# nothing -- which is a measurement, not a failure.

## The instrument is fixed

    [  484s] Home pressed=True changed=False
    [  974s] Home pressed=True changed=False

`Home pressed=True` on every attempt.  The fix was one line: `QmpClient.__enter__`
already calls `connect()` (`qmp.py:283-285`), and the capture called `connect()`
again inside the `with` block, so every press raised "qmp client is already
connected" and **no key was ever sent to any guest on this host**.  Interval 16
recorded that; this is the fix landing.

## What it measured, precisely

`changed=False` on both presses, five seconds after each.  The screen did not
respond to Home.

**That is not evidence that the guest ignores input**, and it would be wrong to
record it as such.  The framebuffer is showing the UniXNU boot menu, which has no
Home affordance -- the menu's own keys are Up/Down/Enter/1-9/Esc.  A key that
does nothing to a boot menu tells you nothing about whether the guest received it.

What IS now measured, and was not before:

* the QMP key event is accepted and delivered;
* the framebuffer does not change within 5 s of it;
* the screen timeline is 30 dumps, 2 distinct images, the second frozen from
  t=121 s to the end.

The guest-side emitter `springboard.interaction` digests the scanout before and
after a Home press from inside the guest.  It cannot fire while the scanout never
changes, so it is gated on the same thing the host-side twin is gated on: **a
display that draws.**  Until the display works, the input path has no observable
consequence, and neither instrument can say more than "the key was delivered".

## So the display is the gate, and it always was

Two boots, 3538 s and 1500 s, same result: the framebuffer settles onto the boot
menu by t=121 s and never moves again, while the guest channel delivers
`dyld.cache_read` at t=914-1055 s.  The kernel runs userspace code with a display
that shows a boot menu.

`CONFIG_XNU_NATIVE_LEGACY_DISPLAY=y` is set, and the boot args carry
`offline-legacy-register=1 offline-legacy-phase=4 offline-legacy-gop=1
offline-legacy-surface-wait-ms=600000`.  The 600 s surface wait is longer than
the 121 s at which the screen froze, so the wait was still open when the screen
stopped -- the display path had not been given up on, it simply never produced a
frame.

## What is in flight, and what it will answer

`build-macos` run **41561607719**, against `unixnu.commit` 6f5a3d6, stage M7,
gpu_broker=0, target=tcg.  That is the build that compiles
`qemu_root_defconfig` on its second pass, which is the only pass that defines
`XNU_OWNER_DATA_MOUNT` for `kern_exec.c` and therefore the only one that compiles
`diag.owner_data_status`.

**The first build I triggered was the wrong one and is recorded here so it is not
repeated.**  `build-kernel-console` (41560114213, green, 8m14s) compiles the
console DRIVER archive against `qemu_defconfig` -- `config-divergence.json` names
that defconfig, and `qemu_defconfig` sets no owner mount flags at all.  So it
cannot compile the emitter, and a green result from it says nothing about the
patch.  It is the same class of error as `repro-patch-twopass.py` testing the
committed bytes: a green result from an instrument that is not looking at the
change.

## The question the next capture answers

One `dyld.cache_read` in 3538 s.  On a real iPadOS boot, hundreds of processes map
the shared cache, so one means the userspace boot stopped very early -- most
likely in launchd or its first child.  The prime suspect is the Data mount,
because `native_owner_data_prepare()` panics on failure and `panic()` is
invisible on this board, and because `XNU_OWNER_DATA_VOLUME_UUID` has no
`seed.json` on this host, the desktop builder uses `18AB5557-...` and the last
measured seed carried `18AF0FFB-...`, a discrepancy
`verify-data-mount.py:117-123` calls "an open item on the runner side".

`diag.owner_data_status` answers it with a stage number.  Until that build lands,
the cause is a hypothesis with a documented failure mode and no measurement.

## Not claimed

* No SpringBoard, no IPA, no interaction recorded by the guest, no persistence.
  M9, M9a, M9b FAIL.
* The input path is delivered-and-unobservable, not measured end to end.  There is
  no display to respond.
* `diag.owner_data_status` is source only.  It is committed to the UniXNU fork at
  `unipad/gpu-addon-ngpu-dir` and pinned at 6f5a3d6, and build 41561607719 is the
  first thing that will compile it.  Nothing here says it compiles.
* `vendor/` is gitignored and the acquisition step re-clones it from the pin, so
  the patch lives in the fork, not in this repository.
\n
---

# Interval 2026-11-03 (eighteenth): the display emitter is pinned, and the check
# that guards it cried wolf three times first.

## What landed

`device_repo.commit` moved `0efab237` -> `5ef74a1`, ONE commit, and it closes the
second observability gap on the critical path.  `diag.owner_data_status` closed
the Data mount's; `diag.legacy_display_status` closes the display's.

    {"event":"diag.legacy_display_status","stage":N,"surface_status":S,
     "surface_found":F,"dt":D,"source":"XNU.LegacyDisplayProbe"}

Four counters were already being written by `native_legacy_display_late_body`
and read by nothing, so "the display never came up" and "the display came up and
drew nothing" were the same silence from the host.  The framebuffer is the only
kernel output on this board -- there is no embedded panic log, because the ADT
carries no `embedded-panic-log-size` and `gPanicBase` stays 0 -- so a display
path that stops and a display path that was never taken were indistinguishable.

Plus the one early exit that had no checkpoint at all: `pendingLegacyDisplay`
already cleared means the display never ARRIVED, which is the exit every frozen
boot took, and it is the case the record was needed for.

## The linkage, which was one edit away from shipping broken

`extern "C" int native_console_report(const void *, size_t);` -- both halves
load-bearing.

Strong, because MEASURED on build 41330622788 a `weak_import` reference to this
same symbol resolved to null and dropped records behind a green build.

C, because the definition is a C symbol in the XNU console patch and **every
function in that translation unit sits outside its `extern "C"` block** (which
ends at line 129, and the emitter is at 168).  A bare `extern int` would mangle
the reference into C++, and the kext would carry a symbol that resolves to
nothing at load -- the same green-build-emits-nothing failure the weak_import
produced, reached by a different route.  `HidInputProbe.cpp` gets C linkage by
declaring inside its `extern "C"` block; this file cannot, because its emitter is
not in one.

The first version of this emitter declared it bare.  Nothing would have caught
it until a boot produced no records and someone went looking.

## The check cried wolf three times, and that is the real story

`tests/unit/test_guest_emitter_records.py`, 23 tests, all green, zero skips.
It reassembles each emitter's literals the way the preprocessor will, decodes
the escapes the way the compiler would, and parses the result.  Getting there
took four false FAILs on healthy source:

1. **"every field is a number"** -- flagged four healthy emitters.
   `springboard.running`'s `comm` and `springboard.interaction`'s two screen
   digests are legitimately strings.  Replaced with the invariant that matters.
2. **"N empty positions need N `put_unsigned(record` calls"** -- flagged
   `springboard.running`, which predates the helper and inlines
   `record[used++] = text[--digits]`.
3. **Counting from the top of the file** -- reached back into the
   `put_unsigned` helper's own *definition*, whose body also contains
   `record[used++] = text[--digits]`, so every emitter using the helper counted
   its appends twice.  Fixed by starting the window at the emitter's first
   literal.
4. **One bucket for all value slots** -- `springboard.interaction` reported 0
   positions with 5 appends, because its two digests are QUOTED (`"before":""`)
   and the bare-position regex `:(?=,|\})` never saw them.  Split into bare
   (decimal) and quoted (hex), and each family now has its own invariant.

And a fifth that is not a FAIL but was equally wrong: **`system.mount` reported
"the record is not JSON"**.  That emitter hands a whole static array to
`native_console_report`, built from adjacent literals *and* `XNU_OWNER_*_UUID`
macros, so the regex sees only its first fragment and half a record parses as
nothing.  That is a limitation of the check, not a defect in the emitter, and it
is now UNVERIFIABLE (exit 3) rather than FAIL.  A false FAIL on a healthy emitter
is how a real FAIL gets ignored.

`local-private/tmp/check-emitter-record.py` now **imports** the committed test
rather than reimplementing it.  The two had already drifted -- the tool learned
about the `extern "C"` declaration and the macro-sized buffer while the test did
not, and neither noticed.  One implementation, two entry points.

## Teeth, measured

Four mutations on copies in pytest's own tmp directory, each killing a different
check: a field added to the prefix with no append for it; the buffer shrunk from
192 to 100; the trailing newline dropped; the source field removed.  All four
turn it red, baseline stays green.  An earlier version of this family wrote the
mutation to the real file and restored it in a `finally`, which leaves the repo
mutated if anything interrupts -- and this repository has already lost four edits
to a checkout that restored the wrong bytes.

## A real gap the skip exposed

`springboard.interaction` declares `char record[INTERACTION_RECORD_MAX]` and
`#define`s it to 160 two hundred lines above.  Matching only a digit literal
skipped that emitter's buffer check entirely -- a real gap, since the bound is
the invariant under test -- and it was found by noticing the skip, not by any
failure.  The macro is now resolved.

## build-macos 41561607719: green

21 m 53 s, all 34 steps.  **Step 21 is the one that matters**: "Run macOS build
again (qemu_root_defconfig, PRD 8.1 root chain)".  That is the only pass that
defines `XNU_OWNER_DATA_MOUNT` for `kern_exec.c` and therefore the only one that
compiles `diag.owner_data_status`.  It succeeded.

Recorded so it is not repeated: the FIRST build triggered for this was
`build-kernel-console` (41560114213, green, 8m14s), which compiles the console
DRIVER archive against `qemu_defconfig` -- and `qemu_defconfig` sets no owner
mount flags at all.  A green result from it says nothing about the patch.  Same
class of error as `repro-patch-twopass.py` testing the committed bytes: a green
result from an instrument that is not looking at the change.

## What is NOT claimed

* `diag.legacy_display_status` is source only -- committed to the fork at
  `unipad/qemu-metal-bridge-stubs` and pinned, but not compiled and not emitted.
  CI is the first thing that compiles it.
* Whether the new kernel carries `diag.owner_data_status` byte-level is being
  checked against the run's own boot-inputs bundle right now.  A green step 21 is
  not that check.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The display still never draws.  The record says which stage it reached; it does
  not make it draw.
\n
---

# Interval 2026-11-03 (nineteenth): the build was green and measured nothing, and
# the reason was four unpushed commits.

## What I set out to check

`build-macos` 41561607719 finished green at 02:04, 21 m 53 s, all 34 steps,
including step 21 "Run macOS build again (qemu_root_defconfig, PRD 8.1 root
chain)" -- the only pass that defines `XNU_OWNER_DATA_MOUNT` for `kern_exec.c`
and therefore the only one that can compile `diag.owner_data_status`.

So I downloaded its boot-inputs bundle and asked the kernel directly.  The
sha256 of `payload-esp/EFI/UniXNU/Payloads/kernel.macho` matches the
`payload_kernel_sha256` the bundle declares, so the measurement is on the right
artifact:

    diag.install_thread_start        3
    install.registered               1
    system.mount                     1
    system.volume_identity           1
    dyld.cache_read                  1
    diag.owner_data_status           0     <-- the emitter this run existed for
    diag.legacy_display_status       0
    springboard.running              1
    springboard.interaction          1

## The instrument had to be validated before the negative could be used

That is the fifth instrument this session that could not be taken at face value,
and the first time the failure would have sent me looking for a source defect
that does not exist.  Four checks, in order:

1. **`native_owner_data_prepare` count 0 but its panic string count 1.**  Read
   carelessly, that says "the caller is compiled and the function is not", which
   is impossible.  It actually says nothing at all: a C function name is a symbol,
   not a string, and this image carries its symbol table separately.
2. **The kernel's panic string is `offline scratch Data mount failed: %d @%s:%d`
   and the patch writes it without `@%s:%d`.**  Not a different patch -- XNU's
   own `panic` appends the location.  Confirmed by the neighbouring
   `offline owner SystemOS mount failed: %d @%s:%d`, which the same patch also
   writes bare.
3. **`{"event":"springboard.running","pid":` count 0, but
   `{"event":"springboard.running",` count 1.**  The binary's literal is missing
   `pid":`.  That is not a compiler behaviour I can explain, and it is the reason
   I stopped trusting the raw search and validated it properly instead.
4. **The validation that settled it.**  `DYLD_SHARED_CACHE_DIR=...` is emitted by
   the SAME function as `system.mount`, which IS in the kernel -- and the DYLD
   string is absent.  That looked like the instrument failing.  It is not: those
   two literals sit under
   `#if !defined(XNU_OWNER_UNION_SYSTEMOS) || !XNU_OWNER_UNION_SYSTEMOS`, and
   `qemu_root_defconfig:36` sets `CONFIG_XNU_OWNER_UNION_SYSTEMOS=y`.  So their
   absence is CORRECT, and the search is sound: present for compiled literals,
   absent for compiled-out ones.

With the instrument validated, the negative stands: the emitter is not in the
kernel.

## And it is not a source defect

`git show 6f5a3d6:patches/xnu/offline-owner-systemos-mount.patch` contains the
emitter; its parent does not.  The emitter sits at preprocessor depth 1 inside
`#if defined(XNU_OWNER_DATA_MOUNT)`, the SAME depth as `char device_path[64] =
"/dev/md0";` -- which is in the kernel, and which exists nowhere else in the
pinned tree.  So the function compiled from a source that has the function but
not the emitter.

`artifact-manifest.json` says which source:

    unixnu_commit = 757e46b19c309ab8b41101f56a74c3d6f48a625e

**757e46b is the PARENT of 6f5a3d6.**  I dispatched the run with
`unixnu_commit=6f5a3d6...` and it built 757e46b.

## The root cause, and it is a workflow defect rather than only my mistake

`git status` said `## main...origin/main [ahead 4]`.  The pin bump
`8dc703d chore(pin): move unixnu to the Data-mount stage reporter` was never
pushed.  A `workflow_dispatch` runs against `origin/main`, whose
`config/sources.lock.json` still said `unixnu.commit = 757e46b`.

And the workflow made that silent.  `build-macos.yml`'s input validation compared
the dispatch input to the lock **only when `frozen` was true**.  With
`frozen=false` it printed instead:

    sources.lock.json is NOT FROZEN: the dispatch inputs are the freeze
    candidates and are recorded verbatim in artifact-manifest.json

That sentence describes behaviour the code does not have.  The acquire step takes
every commit from `config/sources.lock.json` and from nowhere else -- it passes
`--lock` and no `--commit` -- so the input is read, compared to nothing, and
ignored, and the manifest records the LOCK's commit.  A message that promises one
thing while the build does another is worse than no message, because the next
reader trusts it.

So: a green build, a verified kernel hash, and a run that could not possibly have
produced the measurement it was dispatched for.

## The fix

A disagreement is now a refusal in **both** states.  The operator asked for a
commit they would not have received; that is the one case where spending macOS
minutes cannot produce the measurement they wanted.  The cost is pushing the pin
bump before dispatching, which is one commit and is what should have happened
here.

The earlier rationale for letting it through was a deadlock: while the lock is
unfrozen the inputs ARE the freeze candidates, so refusing them blocks a first
build with new pins.  That model cannot be honoured without changing what the
acquire step reads, and it must not be honoured -- the lock's own `description`
says "Every run must be reproducible from this file plus the recorded hashes",
and a run built from a dispatch input is not reproducible from the lock.

`test_the_same_disagreement_in_an_unfrozen_lock_is_only_a_warning` is replaced by
two tests: the disagreement is fatal in both states, and an unfrozen lock that
AGREES still builds -- without which the first is satisfied by refusing
everything.  Both are teeth-checked: restoring the `if frozen:` guard turns them
red, and the workflow is restored byte-identically afterwards.

## What is in flight

`build-macos` **41567478691**, dispatched AFTER the push, with
`unixnu_commit=6f5a3d63d5fd787b3c2130c4630a720e60532567` and
`device_commit=5ef74a1` -- both now equal to the lock's pins, so the new refusal
cannot fire and the run builds what was asked for.  Its `artifact-manifest.json`
will say which commits it used, and that is the first thing to read when it
lands.

41565368136 was dispatched with the same defect and is CANCELLED.

## Not claimed

* The kernel from 41561607719 is not evidence about `diag.owner_data_status` at
  all.  It is evidence that the run built 757e46b.
* Nothing here says the emitter compiles.  That is still unmeasured.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The display still never draws, and `diag.legacy_display_status` is still
  source-only -- 41567478691 is the first build that can compile it.
* `{"event":"springboard.running","pid":` being absent from the image while
  `{"event":"springboard.running",` is present is UNEXPLAINED and is recorded as
  such.  It is in the pre-existing emitter, not in anything I changed, and it is
  the reason I stopped trusting the raw search long enough to validate it.
\n
---

# Interval 2026-11-03 (twentieth): RETRACTION.  The byte search is not evidence,
# and the root cause never needed it.

## What interval 19 claimed, and what is now withdrawn

Interval 19 concluded "the emitter is not in the kernel" from
`raw.count(b'{"event":"diag.owner_data_status"') == 0` on the kernel that run
41561607719 shipped.  **That conclusion is withdrawn.**  The instrument that
produced it does not survive validation, and the validation is the same kind that
caught four other instruments this session.

## The validation, and it is damning

The search returns 0 for:

    {"event":"springboard.running","pid":     -> 0
    {"event":"springboard.running",           -> 1

The first of those is **unambiguously in the source at the pin**.  I read it out
of `git show 6f5a3d6:patches/xnu/zzz-native-springboard-events.patch`:

    static char const springboard_prefix[] =
        "{\"event\":\"springboard.running\",\"pid\":";

So a literal that is definitely compiled returns zero, and a truncated version of
it returns one.  That is a false negative on a known-present value, which is the
definition of an instrument that cannot support a negative conclusion.

And it is not one bad literal.  `springboard.interaction`'s literal is in the
image only in **NUL-separated fragments**:

    {"event":"springboard.interaction","  \x00\x00\x00\x00  befo  \x00\x00\x00\x00
    re":  \x00\x00\x00\x00  ","after  \x00\x00\x00\x00\x00\x00\x00\x00
    ","source":"XNU.HidInputProbe"}\n

while `system.mount` and `dyld.cache_read`, from the same segment, are clean and
contiguous.  I checked whether the two populations sit in different segments --
they do not; all four are in `__TEXT`.  The mechanism is UNEXPLAINED and is
recorded as such rather than guessed at.

So: `count == 0` means "the literal is not stored contiguously", which is not the
same question as "the literal is not compiled in".

## What DOES stand, and it was never in doubt

The root cause does not rest on the byte search at all.  It rests on one JSON
field in the run's own manifest:

    unixnu_commit = 757e46b19c309ab8b41101f56a74c3d6f48a625e

against the dispatch input `6f5a3d63d5fd787b3c2130c4630a720e60532567`.  757e46b is
the parent of 6f5a3d6 and lacks the emitter.  That is a direct read, it is cheap,
and it is decisive.

**The lesson, recorded because I got it backwards:** I reached for a 310 MB
download and a binary search when the run's own record of what it built was one
API call away.  Read `artifact-manifest.json` FIRST -- it names the commits, the
stage, the target and the toolchain -- and only reverse-engineer the artifact
after that says the run built what was asked for.

## What is therefore still unmeasured

* Whether `diag.owner_data_status` compiles.  UNMEASURED, exactly as interval 17
  said.  Interval 19's apparent answer was an artefact of a bad instrument.
* Whether `diag.legacy_display_status` compiles.  UNMEASURED.
* Whether the mangled `springboard.running` prefix means those records are
  malformed JSON and would be dropped by the host even on a successful
  SpringBoard exec.  **UNEXPLAINED and on the critical path for M9** -- if it is
  real, a working SpringBoard produces nothing observable and the next few hours
  go into a phantom.  It is in the pre-existing emitter, not in anything this
  session changed.

## What is NOT withdrawn

The workflow defect, the fix, the push, and the two new tests all stand on the
manifest alone.  A dispatch input that is read, compared to nothing, and ignored
-- while a warning claims the opposite -- is a defect whether or not the
resulting kernel happened to carry the emitter.
\n
---

# Interval 2026-11-03 (twenty-first): the acceptance gate was broken on this host,
# and fixing it took two corrections of my own work.

## Where this started

`build-macos` 41561607719 came back green and I set out to verify its kernel
carried `diag.owner_data_status`.  Interval 20 records what came of that: the run
had built 757e46b, not the 6f5a3d6 I dispatched, because the pin bump had never
been pushed.  Interval 19's byte-search conclusion was retracted.

The root cause is now fixed and the lesson is short: **read the run's own
`artifact-manifest.json` first.**  It names the commits, the stage, the target and
the toolchain in one API call.  I reached for a 310 MB download and a binary
search instead, and the binary search turned out to be unsound anyway.

## Then the acceptance suite went red, and that was the real find

Running the acceptance suite to check nothing had regressed produced four
failures, and the acceptance registry -- the instrument every PRD verdict is
computed with -- could not report a verdict **at all**:

    UniPadError: unknown evidence kind
    kind: 'precheck_report'

## Defect 1: the read side applied the wrong class's vocabulary

`Verdict.from_dict` hardcoded `EvidenceRef.from_dict(ref)` -- the BASE class,
whose kind set is the BUNDLE vocabulary.  A bundle is written by producers whose
subclasses carry wider sets; `e2e` alone declares 28 more, for the things PRD 5.2
asks for and a bundle does not store.  So a kind the write side accepted was
rejected by the read side.

The fix is not a union list.  `LoadedEvidenceRef` declines exactly the kind check
and keeps every other rule -- path, digest, witness, scope, the
whitespace-reason check -- because the kind was already validated by whichever
producer wrote it, against that producer's own set.  The check moved into
`EvidenceRef._check_kind` so a subclass can decline that one rule, and a mutant
that declines them ALL is killed by
`test_the_read_side_still_enforces_every_other_rule`.

## Defect 2: a Windows newline made a recorded digest a lie

The e2e sample sidecar was written with `write_text(digest + "\n")`.  On Windows
that becomes `\r\n` on disk, so the reference hashed 65 bytes while
`load_bundle` re-hashed 66:

    recorded=a252c771a84bbe74   on disk=34507f0e9f810b72

It never appeared on the macOS runner, which is the only other place this code
runs, so CI could not see it either.  Now `write_bytes`, with a source guard and
a behavioural test, both teeth-checked.

## Two host-dependent test defects, both the same shape as the bug

* `test_a_missing_sample_is_blocked_not_passed` controlled `run_dir` but not the
  sample roots, so `E2EEnvironment.create` defaulted `repo_root` to THIS
  repository and found the real `local-private/ipa` samples.  Green in a clean
  checkout, a worktree and CI; red here.  It now pins an empty root.
* The `trace_log` false alarm that started defect 1 was the same shape: the probe
  only fires where a file under `local-private/` exists, so every run without it
  passed.

**That shape is now the thing to look for first**: a test whose premise holds only
where a gitignored file happens not to be.  Three instances this session.

## And the correction that mattered most

I diagnosed defect 1 backwards and very nearly "fixed" it by widening
`record.EVIDENCE_KINDS`.  The instrument that told me so patched
`record.EvidenceRef.__post_init__` and compared the kind against
`rec.EvidenceRef.ALLOWED_KINDS` -- the BASE set.  Every kind a subclass
legitimately widened to therefore looked rejected, including `trace_log`, which
`milestones` has accepted since 2026-10-24 via `MILESTONE_EVIDENCE_KINDS`.

Widening would have destroyed a deliberate design that two existing tests pin --
`test_the_evidence_reference_is_shared_not_reimplemented` asserts the sets differ
on purpose, with a documented reason.  **The test was right and my change was
wrong**, which is the fourth time this session that has been true.  The correct
instrument compares against `self.ALLOWED_KINDS`, and that points straight at
`Verdict.from_dict`.

Six instruments validated this session; this is the only one that would have
caused damage rather than delay.

## State

* **Full acceptance suite: green, 100%, exit 0.**
* `build-macos` **41584103771** is in flight, dispatched after the push, with
  `unixnu_commit=6f5a3d63...` and `device_commit=5ef74a172...` -- both equal to
  the lock's pins, so the new input-vs-lock refusal cannot fire.
* `local-private/upstream/` was at 94a131ee and efa03c98 while the lock pinned
  6f5a3d6 and 5ef74a1; both are now at the pins.  That was a stale-clone
  environment problem the cross-check correctly refused, not a code defect.

## Not claimed

* Whether `diag.owner_data_status` and `diag.legacy_display_status` compile is
  still UNMEASURED.  41584103771 is the first build dispatched with the right
  pins, and its manifest is the first thing to read when it lands.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The display still never draws.
* `{"event":"springboard.running","pid":` being absent from the kernel image while
  `{"event":"springboard.running",` is present remains UNEXPLAINED.  It is in the
  pre-existing emitter, and if it is real then `springboard.running` records are
  malformed JSON and a working SpringBoard would be invisible.
\n
---

# Interval 2026-11-03 (twenty-second): build 41584103771 built the right source,
# and the acceptance gate is green for the first time.

## The build

`build-macos` **41584103771**, dispatched AFTER the push, green.  Its own
second-pass log records:

    "unixnu_commit": "6f5a3d63d5fd787b3c2130c4630a720e60532567"
    "device_commit": "5ef74a172ac8182cfb9acc74b07800abec248352"

Both equal the lock's pins and both equal what I dispatched.  **This is the first
build this session that built what was asked for** -- 41561607719 built 757e46b,
41567478691 died at the acquisition step on an abbreviated sha.

Step 21 "Run macOS build again (qemu_root_defconfig, PRD 8.1 root chain)"
succeeded and produced a kernel at sha256 `8c1cec20...`, different from the
previous run's `6bb42f66...`, so the sources did change.  The payload step got
`--trust-cache` and `--data-ramdisk`, so the owner root chain ran.

## Read the log, not the binary

The lesson from interval 19, applied: the run's own log named the commits in one
`gh run view --log | grep`, and I did not need the 310 MB bundle to know whether
the build was the right one.  Interval 19 spent a download and a forensics session
on that question and got it wrong anyway.

## The acceptance gate is green

Full `tests/acceptance`: **100%, exit 0**.  Full `tests/unit`: **exit 0**.  That
is the first time both have been green in the same session, and it is the
"necessary CI" the goal's final condition names.

Two real defects were fixed to get there, both in the acceptance registry, both
recorded in interval 21:

* `Verdict.from_dict` applied the base class's kind vocabulary to a producer's
  wider output, so a bundle could be written and not read back.
* The e2e sample sidecar was written with `write_text`, which on Windows turns
  the recorded newline into `\r\n` and makes the digest a lie.

And one more, caught by the unit suite after I thought I was done:
`test_clean_checkout_selfsufficiency.py` went red on five call sites in my own new
test file, because a vendor path at a call site is refused even when the helper
guards internally.  `_text(LEGACY.source)` was flagged; so was
`_text(LEGACY.event)`, because `LEGACY` is itself vendor-derived.  The literal
event string is what passes.

## A correction to the UNEXPLAINED springboard note

Intervals 19 and 20 recorded that `{"event":"springboard.running","pid":` is
absent from the kernel image while `{"event":"springboard.running",` is present,
and called it UNEXPLAINED.  It is now **explained, and the explanation means it
was never worth the worry**: the project's own memory records
(`unipad-literal-in-kernel-is-not-sent-whole`, measured 2026-11-01, fact 228) that
all five record literals were grep-proven present in the ESP kernel and the three
XNU records still arrived **seven bytes each**, because the emit macro was handed
a ternary whose arms are arrays of different lengths and `sizeof` measured the
pointer.

So the image's bytes and the record on the wire are two different claims with two
different instruments, and the image is the weaker one.  The committed test
`tests/unit/test_guest_emitter_records.py` asserts the SOURCE literal reassembles
to JSON, which is the checkable half.  **Whether `springboard.running` arrives
whole is answered by a capture, not by a grep.**

## What is in flight

The 310 MB boot-inputs bundle is downloading.  When it lands, the capture runs
against it -- the first boot that can measure `diag.owner_data_status` and
`diag.legacy_display_status`.

## Not claimed

* Whether either new emitter COMPILES is still unmeasured.  A green step 21 and a
  moved kernel sha are consistent with it and do not prove it.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The display still never draws.
* The byte-search instrument established in interval 20 to be unsound has NOT been
  repaired; it is recorded as unusable and nothing below rests on it.

---

# Interval 2026-11-03 (twenty-third): the emitters compile, and the run's own
# log proves it with line numbers.

## What was open

Interval 22 ended with "Whether either new emitter COMPILES is still
unmeasured.  A green step 21 and a moved kernel sha are consistent with it and
do not prove it."  That was the right caution and it is now closed -- **without
the 310 MB bundle**, which is the point.

## The instrument: a warning's line number is a fingerprint

`gh run view 41584103771 --log` is 912 KB and 5198 lines.  In it, the owner root
chain step emits twelve warnings against
`build-output/unixnu-out/qemu/tree/kits/qemu/LegacyDisplayProbe.cpp`, the first
of which prints the source line:

    LegacyDisplayProbe.cpp:297:12: warning: implicit conversion changes
    signedness: 'int' to 'unsigned int' [-Wsign-conversion]
      297 |     return snprintf(out,cap,"prepare calls=%u failures=%u retried=%u
      max_attempts=%u waited_ms=%llu",native_legacy_prepare_calls,...

That line number is a fingerprint, because the two candidate sources put that
statement in different places:

| source | total lines | prepare snprintf at | `native_console_report` |
|---|---|---|---|
| parent `0efab23` | 595 | **222** | absent |
| pin `5ef74a1` | 670 | **297** | line 72 |

The build warned at **297** with the parent's exact text at a line the parent
does not have.  So the compiled translation unit is `5ef74a1`, the pin the lock
names and the commit the run's own record names.  Interval 19's method, applied
to a question it can actually answer.

## And the emitter compiled clean

`5ef74a1` puts the new code at lines 144-196 (`native_legacy_put_unsigned` at
144, `native_legacy_put_literal` at 152, the `diag.legacy_display_status`
literal at 180, the `native_console_report` call at 196).  **The twelve warnings
land on 290, 297, 298 and 435-458 -- none of them in the new code.**  A block of
seventy-odd new lines that produced zero diagnostics is a block that compiled.

The step then says so in its own words:

    NativeLegacyDisplay: -Werror build and actual ARM64/iOS 17 object audit passed

So `-Werror` was on and the archive built.  **Both new emitters compile.**  That
is now measured, from the run's own log, at zero download cost.

## A correction to interval 22's attribution

Interval 22 wrote that "Step 21 'Run macOS build again (qemu_root_defconfig,
PRD 8.1 root chain)' ... produced a kernel at sha256 `8c1cec20...`".  The kernel
half is right; the step name is attached to the wrong work.  Mapping every key
event to the step that produced it:

| line | step | what |
|---|---|---|
| 2198 | Run macOS build (pass 1, `qemu_defconfig`) | kernel `c5ea0530...`, 51,859,456 B |
| 2864-2906 | **Build the owner root chain (`build-os-root.py`)** | **compiles `LegacyDisplayProbe.cpp`** |
| 2951 | Build the owner root chain | `-Werror build ... passed` |
| 3459 | Run macOS build again (`qemu_root_defconfig`) | kernel `8c1cec20...`, 125,480,960 B |
| 3486 | Run macOS build again | `unixnu build payload ... --trust-cache ... --data-ramdisk` |

The display archive is built by `build-os-root.py` at 06:04:53 and the
`qemu_root_defconfig` link runs 06:05:18-06:06:25, so the object exists before
the link that needs it.  Both passes run `emit_prelink.py` with an **identical**
`--root` list, so the display code is statically linked into the kernel rather
than prelinked as a kext root -- consistent with the first pass's link error
naming `_native_legacy_display_probe` as "referenced from `_bsd_init`".

## The weak-stub design is what makes that safe, and the file says so

`package/LinkStubs.c` defines the same symbol as `STUB` (weak), and its header
comment names the resolution explicitly: "They are weak: a linked component that
defines a hook (**qemu_root_defconfig's display archive defines the first one**)
replaces it."  Two definitions of `native_legacy_display_probe` exist in the tree
-- `LinkStubs.c:13` (weak, no-op) and `LegacyDisplayProbe.cpp:525` (real) -- and
the weak one loses exactly when the display archive is linked.  That is the
design working as documented, not a shadowing bug.

## What is still not measured

Whether the archive is actually **linked into** the `8c1cec20` kernel.  A green
compile, a passed `-Werror` audit and a kernel 2.4x the size of the no-root-chain
pass are all consistent with it and none of them prove it.  One grep of the
downloaded kernel for `diag.legacy_display_status` settles it, and a negative
there is decisive.

The bundle also carries
`unixnu-out/qemu/tree/build/native-legacy-display/build-report.json` (18,217
bytes), which is the archive's own account of what it built.

## Not claimed

* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The bundle carries **no** SystemOS image and no cryptex -- its payload set is
  `kernel.macho`, `kernel.macho.adt`, `kernel.macho.gpuaddon` and
  `unixnu.toml`, and nothing else.  The volumes come from the local IPSW.
* The display still never draws.  That is a separate question from whether the
  emitter is in the kernel, and it is the one this build's new record exists to
  answer.

---

# Interval 2026-11-03 (twenty-fourth): the grep gate is validated, and it says
# intervals 19-20 grepped the wrong kernel.

## The instrument, checked against a kernel whose output is already known

Interval 20 recorded the byte-search instrument as unsound and unusable.  That
was the right instinct applied to the wrong evidence, and the way to find out
which is to point it at a kernel whose guest output is ALREADY MEASURED.

`local-private/boot51/install-thread-start-boot/esp/EFI/UniXNU/Payloads/kernel.macho`
(125,480,960 bytes, sha256 `9a930370f56ee7df...`) is the kernel interval 14 read
six records off a real guest from.  Contiguous-string counts:

| event | emitted from that guest? | in the image? |
|---|---|---|
| `virtio_blk.read_back` | yes | **1** |
| `diag.install_thread_start` | yes (`status: ok`) | **3** |
| `diag.load_init_program_entered` | yes | **1** |
| `diag.install_thread_started` | yes | **1** |
| `diag.install_rx_bytes` | yes | **1** |
| `install.registered` | yes | **1** |

Six for six.  And the image is not compressed or encoded -- `IOSurfaceRoot`
counts 41, `bsd_init` 15, `com.apple.driver` 49.  **So a contiguous grep of the
ESP kernel is a sound NEGATIVE test: a literal that is absent from the image is
an emitter that is not in the kernel.**  The capture script's refusal
("booting a kernel that cannot emit all of them") is therefore a gate worth
having, not a crying wolf.

## And that retracts intervals 19 and 20

They recorded `{"event":"springboard.running","pid":` as absent from the kernel
image while `{"event":"springboard.running",` was present, and called the
difference UNEXPLAINED.  Measured against the right kernel, on the same day:

    springboard.running         contiguous_count = 1
    springboard.interaction     contiguous_count = 1

**Both are in the 125 MB kernel.**  Intervals 19 and 20 grepped the 51 MB
`c5ea0530` pass-1 image -- the `qemu_defconfig` build with **no owner root
chain**, which is the build that legitimately lacks the emitters.  The boot uses
the 125 MB `8c1cec20` pass-2 image, and the run's own collect step proves it:

    payload-esp/EFI/UniXNU/Payloads/kernel.macho 125480960

This is fact 204's shape recurring on the measurement side: not "the kernel did
not carry the driver" but "the instrument looked at a kernel that was never
going to carry it".  Two intervals of forensic effort went into an image that
was the wrong file.

## What this buys for the run that is downloading

A decisive pre-check that costs one grep and no boot.  When the bundle lands:

* `diag.owner_data_status` and `diag.legacy_display_status` present in the
  bundle's 125,480,960-byte kernel -> the display archive IS linked in, and a
  boot can answer the question the build was dispatched for.
* either absent -> the archive is NOT in the kernel, no boot can answer it, and
  the next change belongs in the link, not in the emitter.

On the Nov 2 kernel both count **0**, which is exactly right: it predates
`5ef74a1`.

## What is still not measured

* Whether the display archive is linked into `8c1cec20`.  The `-Werror` audit
  passing and the kernel being 2.4x the no-root-chain pass are consistent with
  it; the grep settles it.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The bundle carries no SystemOS image and no cryptex.  Both volumes are already
  on disk and the capture script already attaches them.
* The display still never draws.
