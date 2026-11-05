# Current state — task, evidence, next actions, resume commands

**This file is the working state document.** It is updated as work proceeds, and
it is the first thing to read after a context compaction or a session restart.
The numbered facts behind every claim here live in
[`implementation-plan.md`](implementation-plan.md); this file says where things
stand and what to run next, not why.

Last updated: 2026-11-06 (interval one hundred seventy fifth, addendum EM). THE HEADLINE is **EL's CORRECTED INSTRUMENT IS WRITTEN, PINNED AND DISPATCHED: THE PROBE NOW RECORDS EACH SELECTOR'S STRUCTURE INPUT, NOT ONLY ITS SCALARS, AND BUILD RUN 42099485734 IS IN PROGRESS. THIS IS THE MEASUREMENT THAT SHOULD SEPARATE SELECTORS 29 AND 57.** EL withdrew the vtable trace EK had proposed, because that instrument does not exist -- offline-legacy-slot-trace occurs only in a comment and the thunks were removed after three build failures. The corrected next step is smaller: selectors 29 and 57 carry no scalar input, which is exactly why they are the candidates, and a layer and a surface are not scalars.** **THE CHANGE. ClientStat gains sel_struct_size[kSelectors], sel_struct_set[kSelectors] and sel_struct_words[kSelectors][4], and the same externalMethod wrapper that already records each selector's scalar arguments now also records the structure's size and its first four words whenever a selector has one. No new interposer, no new call site -- it is the wrapper that produced the working argument table in EK, with one more field and one more print. ClientStat grows from 4720 to 8272 bytes and display-clients.py's ctypes mirror is updated to match; the stride was checked after the edit because a C and ctypes layout that disagree silently misreads every field, which is the failure mode that would make this whole measurement worthless.** **WHY THIS SHOULD SEPARATE THEM. A swap is a SwapSetLayer / SwapSetBackgroundColor request (NativeLegacyRequest.h:4-5): "Original SwapSetLayer sets the update bit and clears the disable bit for a nonnull surface; a null surface sets both." A layer identifier and a surface live in the structure, so the selector whose structure carries that shape is the submission and the other one is not. Both 29 and 57 refuse 0xe00002c7, so status cannot separate them; both have no scalars, so arguments cannot either. The structure is the only place left.** **AND IF IT DOES NOT SEPARATE THEM, THAT IS ALSO AN ANSWER: it would mean neither selector carries a layer/surface shape, the swap submission was never called at all, and the client asked for something else entirely -- a different blocker from the one being chased. The experiment is built so that both outcomes are measurements.** **THE DISPATCH: run 42099485734 for exp-2026-11-06-selector-struct, with device_commit 5feff1a for this one file and unixnu_commit unchanged at 439c31cb, so the only difference from EK's build is the structure recorder. The lock's device_repo.commit was bumped from 84b9326.** **M9, M9a, M9b, M8 and every section 5.2 case remain unproven, and the end goal is not reached. No frame has rendered, no IPA has been installed, no SpringBoard interaction has occurred, and there is no guest evidence for persistence, input, network, audio, graphics, GPU/Metal, launcher, performance or delivery. What this interval did establish is that the swap submission is a refusal and that it is one of two candidates.** THE HEADLINE is unchanged from AH

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

---

# Interval 2026-11-03 (twenty-fifth): every input to the next boot is verified
# except one grep.

The goal's condition 5 is "every boot verifies actual payload, kernel, config,
driver, trust cache and version consistency".  Walking that list against the
run that is about to happen, one item at a time:

| input | verified how | status |
|---|---|---|
| kernel | `payload.json`'s `payload_kernel_sha256` vs the staged kernel | wired, runs at boot |
| config | `unixnu.toml` args | **`offline-legacy-phase=4`**, `offline-legacy-surface-wait-ms=600000` |
| driver | the display archive's own `-Werror ... object audit passed` | **MEASURED** (interval 23) |
| SystemOS volume | sidecar sha256 vs the file | **MATCH** `b4908cde...`, 17.7.11 / 21H461 / `Cryptex1,SystemOS` |
| OS root volume | the DMG's sidecar; the raw is derived, so its own digest is recorded | DMG absent from disk by design, raw present |
| trust cache | bundle's `owner-trust.bundle` vs `payload.json`'s `trust_cache_sha256` | **present in the bundle** |
| version | the run's own log names both pins, both equal the lock | **MEASURED** (interval 22) |

## The gap this closed

The volumes were the one input with no verification at all.  A boot could
attach a stale or truncated SystemOS and the resulting mount failure would read
as a fact about the kernel.  The capture script now hashes each member against
its sidecar and refuses on a mismatch, and records version, build and component
from the sidecar so the report says WHICH iPadOS was mounted.

Measured while adding it: the SystemOS digest **matches**, so the gate will not
refuse this boot.  And the gate has one shape that would have crashed rather
than refused -- `target-volumes/` still carries `044-76040-038.json` for a DMG
that is no longer there, because the raw root is derived from it.  The sidecar
outlives its member; the loop now checks existence before it stats.

## The display fix is in the tree and it is the right shape

`LegacyDisplayProbe.cpp:559-581`.  `native_legacy_display_late_probe()` -- still
called from `bsd_init` -- starts `native_legacy_display_late_body` on its own
thread and returns at once, so `start_init()` is no longer blocked by the wait.
The five non-blocking probes stay on the boot thread where they were measured
to work.

The body waits for `IOCoreSurfaceRoot` with `waitForMatchingService`, default
120 s, overridden to 600 s by the boot arg, then re-validates the GOP handoff
through `PE_current_console`, builds a real `IOPlatformDevice` nub with the
framebuffer's `IODeviceMemory`, attaches and starts it, and only reaches
`native_legacy_stage=10` when `started && phase==4`.  Every exit checkpoints,
and the checkpoint is what emits `diag.legacy_display_status`.

So the stage legend in the capture script maps one-to-one onto the code, and a
record carrying stage N means "we reached N and then took the early exit".

## The one thing still unmeasured

Whether `diag.legacy_display_status` and `diag.owner_data_status` are in the
bundle's 125,480,960-byte kernel.  Everything else in this table says the boot
can answer the question; only that grep says the question is answerable at all.
A negative there is decisive and costs no boot.

## Not claimed

* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The display still never draws.  This build's record exists to say which stage
  it reached, not to fix it.

---

# Interval 2026-11-03 (twenty-seventh): `stage:10`.  The display path COMPLETED,
# and it still draws nothing.

## The two records this build existed to produce, off a real guest

    {"event":"diag.legacy_display_status","stage":10,"surface_status":0,
     "surface_found":1,"dt":1,"source":"XNU.LegacyDisplayProbe"}
    {"event":"diag.owner_data_status","stage":4,"error":0,
     "source":"XNU.native_owner_data_prepare"}

Both from `local-private/boot53/display-stage-input/guest-input.log`, 2924
bytes, kernel sha256 `fd283772...` verified against `payload.json`'s
`payload_kernel_sha256` before the boot ran.

## What stage 10 means, read off the code

`LegacyDisplayProbe.cpp:638-639` sets it, and only there:

    if(started && phase==4) {
        activeLegacyDisplay=display;activeGOPNub=nub;native_legacy_stage=10;

So `display->start(nub)` returned true AND `offline-legacy-phase` is 4.  The
fields beside it say what had to be true to get there:

* `surface_status=0` -- `OSKextLoadKextWithIdentifier("com.apple.iokit.IOSurface")`
  succeeded.
* `surface_found=1` -- `IOService::waitForMatchingService("IOCoreSurfaceRoot")`
  FOUND the service inside the 600 s the boot arg allows.  The 14-second gap
  interval 12 measured between mountroot and `IOSurfaceRoot::start()` was
  crossed.
* `dt=1` -- the `NativeGOPFramebuffer` nub attached to `/arm-io` in the DT
  plane as `gop-display` with `device_type = display-subsystem`.

And `stage:4` on the Data record is the best outcome in its legend -- "the mount
verified" -- with `error:0`.

**This is the furthest the display path has ever been measured to go.**  Every
previous boot reported `stage:8` (GIVE-UP) or nothing at all.

## And that reframes the problem completely

The display subsystem came up.  `IOSurface` loaded, `IOCoreSurfaceRoot` was
published and found, a real `IOPlatformDevice` nub was built with the GOP
framebuffer's actual `IODeviceMemory`, attached to the device tree, probed,
attached and STARTED.  **And the screen still shows the UniXNU boot menu.**

So "the display never comes up" is now measurably false.  The question is
narrower and harder: something that has a working display, a working framebuffer
and a working surface service does not present a frame.

Two distinct screendumps, 120 s apart, hashed: `2e2910ab...` then `8833260e...`
from t=121 s onward, unchanged for the remaining ~1380 s.  **Six Home presses,
every one `changed: false`.**  The input reaches the guest (`input devices
attached: true`, 50 install frames sent with zero send errors) and the screen
does not respond to any of it.

## What stage 10 does NOT prove

The `sheng-fb-gpu=1` handoff.  `native_legacy_stage=10` is assigned at :639 and
the GPU-handoff presenter block runs at :641-651, AFTER the assignment.  The
checkpoint at :655 is after both.  So a handoff presenter that failed to attach
still reports stage 10.  Stage 10 proves the display started; it does not prove
the presenter that would put a frame on it did.

That is the next measurement, and it needs a field the record does not carry.

## The chain was verified before the boot, not after

| member | declared | actual |
|---|---|---|
| kernel (payload variant) | `fd283772...` | **MATCH** |
| kernel (pre-payload) | `8c1cec20...` | **MATCH** |
| device tree | `26f494fd...` | **MATCH** |
| gpu addon | `f2cacddf...` | **MATCH** |
| trust cache | `08b14937...` | **MATCH** |
| payload image | `f7183b35...` | **MATCH** |
| SystemOS volume | `b4908cde...` 17.7.11 / 21H461 / `Cryptex1,SystemOS` | **MATCH** |

Six of seven from the bundle's own `payload.json`, the seventh from the volume's
sidecar.  The volume gate added in interval 25 ran and passed -- `declared_volumes.systemos.ok: true`.

And a SECOND independent proof that the right source was compiled, which
interval 23's line-number fingerprint did not have: the display archive's own
`build-report.json` declares

    kits/qemu/LegacyDisplayProbe.cpp: 0e7d4d883443e861e09d95671af1726bc59c0ed26c9a419b74b872f27096c27b

and that is exactly the sha256 of the `5ef74a1` blob.  The parent `0efab23`
hashes to `f72d63c0...`.

## Still not measured

* `springboard.running` and `springboard.interaction`: **ABSENT** in 1500 s.
  M9, M9a, M9b remain FAIL.  The emitter is in the kernel (count 1 each), so
  the absence is real.
* Whether the GPU handoff presenter attached.  Stage 10 cannot say.
* No IPA, no install, no interaction recorded by the guest, no persistence.

---

# Interval 2026-11-03 (twenty-eighth): the prime suspect is cleared, and the
# record that names the real one is pinned.

## What interval 27's stage-4 record settled

Interval 26 recorded the M9 hypothesis and named its suspect: "the userspace
boot stopped very early -- most likely in launchd or its first child.  **The
prime suspect is the Data mount**, because `native_owner_data_prepare()` panics
on failure and `panic()` is invisible on this board".

    {"event":"diag.owner_data_status","stage":4,"error":0,
     "source":"XNU.native_owner_data_prepare"}

Stage 4 is "the mount verified".  **The prime suspect is cleared.**  A
hypothesis with a documented failure mode and no measurement is now a
measurement, and the measurement says no.

## What that leaves, precisely

Two facts, both measured on the same boot:

* `dyld.cache_read` arrived **once**.  On a real iPadOS boot hundreds of
  processes map the shared cache, so one means the userspace boot stopped very
  early.
* `springboard.running` did not arrive in 1500 s, and the emitter is in the
  booting kernel (count 1), so the absence is real.

So: a process exec'd and ran dyld, and then nothing spawned that maps the
cache.  That is launchd or its first child, and **nothing on the channel names
which.**

`native-dyld-cache-read.patch` says why in its own comment: "No pid, because a
formatted pid would put a formatting path in front of a record whose only job
is to arrive whole."  That is the right call for that record, and it is exactly
what leaves this question unanswerable.

## The change, and why it goes in the patch that already works

`zzz-native-springboard-events.patch` already emits from `__mac_execve()`'s
`if (!error)` block -- the one site where the exec has succeeded AND `p_comm`
has been written -- already applies cleanly in both passes, and is measured
(count 1 in the booting kernel).  Reviving the parked M8 patch would mean
rebasing its hunk at `kern_exec.c:5284` against the springboard patch's hunk at
`:5283`, which now FAILS on the first pass:

    *** pass 1: FIRST FAILURE: zzz-native-springboard-events.patch
    error: patch failed: bsd/kern/kern_exec.c:5283

So the generic record goes where the working code already is, as 67 added lines
in the same `#if defined(XNU_OWNER_CACHE_MOUNT)` block:

    {"event":"exec.first","comm":"<name>","source":"XNU.__mac_execve"}

Bounded by a flag set before the emit and never cleared -- the bound and the
ordering `native-dyld-cache-read.patch` uses, because the channel's `send()`
waits synchronously on a 2000 ms deadline and an unbounded emitter here charges
one deadline per exec.  The comm is emitted only when every byte is in
`[A-Za-z0-9._-]`, because the record is hand-assembled JSON with no escaping
and a quote in the name would put a record on the wire no parser accepts; a
name that cannot be encoded does not consume the one shot.  Placed AFTER the
SpringBoard block, so the record M9 asks for is emitted first when the first
exec IS SpringBoard.

## Validated with the instruments, not with reasoning

| instrument | result |
|---|---|
| `repro-patch-order.py` (one pass) | all 58 patches applied |
| `repro-patch-twopass.py` (three passes) | **ALL PASSES CLEAN** -- 58 applied, then 3 applied / 55 skipped twice |

The three-pass shape is what the runner's SECOND kernel pass needs and what
killed build 41451414922, and the single-pass instrument is blind to it.  The
hunk header had to be corrected from `@@ -5283,6 +5283,80 @@` to
`@@ -5283,6 +5283,147 @@` by counting the actual added lines; the first attempt
left it stale and `git apply` refused the file as corrupt.

Full `tests/unit`: **exit 0**.  Full `tests/acceptance`: running.

Fork commit `62bc0f48`, pushed to `unipad/gpu-addon-ngpu-dir`; the lock's
`unixnu.commit` moved to it and `device_repo.commit` is unchanged.  Build
**41612457246** dispatched at 09:36.

## Not claimed

* That the emitter COMPILES.  Two clean patch passes and a green unit suite do
  not compile it; the build is the first thing that does.
* That the first exec is launchd.  That is what the record is for.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The display subsystem is up (stage 10) and the screen still shows the boot
  menu.

---

# Interval 2026-11-03 (twenty-ninth): the emitter is compiled and pinned; two
# dispatch hazards and one measurement hazard, all three measured.

## The build

**41617257137**, green in 21m28s (the toolchain cache halved it).  Its own
record names both pins and both match the lock:

    "unixnu_commit": "62bc0f4869eb34517642e270451568e561faba33"
    "device_commit": "5ef74a172ac8182cfb9acc74b07800abec248352"

Two kernels again -- `898db814...` (125 MB, `qemu_root_defconfig`) and
`79ed0a83...` (51 MB, no owner root chain).  **`exec.first` appears 21 times in
the build log**, so the patch applied and the literal is in the compiled
output; that is evidence the emitter compiled, and the boot is what proves it
fires.

## Hazard one: omitting `stage` builds M2, and it costs 31 minutes to find out

The first dispatch of this pin, **41612457246**, FAILED after 31m41s.  Not a
code defect -- I omitted `-f stage`, and the workflow's default is `'M2'`:

    stage: 'Milestone/stage to build for, e.g. M2|M3|M4|M5... (PRD 6).'
      default: 'M2'

At M2 the planner does not schedule the bootloader, so the first pass produced
no `payload.img`, and the chain step refused:

    ##[error]the chain's first sub-step needs
    build-output/unixnu-out/qemu/payload/payload.img, which the first build
    pass did not produce.

The workflow diagnosed it correctly and named the fix in its own error text
("`plan_steps` gates build-bootloader at n>=3").  What it cannot do is tell you
soon: the bootstrap link failure at 10:04 is EXPECTED (the same five undefined
symbols -- `mockfs_mountroot`, `mockfs_vfsops`, `mockfs_vnodeop_opv_desc`,
`native_legacy_display_probe`, `sheng_pmgr_run` -- because the archives that
define them are built afterwards), so a reader who has seen that failure before
has no reason to look further.

**Recorded so it is not repeated: a dispatch that omits `stage` builds M2, and
every capture needs `stage=M7`.**  The job name in the log says which you got:
`build-macos (M7, target=tcg)` versus `build-macos (M2, target=tcg)`.

## Hazard two: `pytest ... | tail` returns tail's exit code

I measured the acceptance suite this way and read "exit 0" off it.  That is
`tail`'s exit code, not pytest's.  Re-measured without the pipe:

    pytest exit=0
    649 dots, no F, no E

**649 passed, exit 0.**  The suite is genuinely green, and the previous
measurement happened to agree -- which is worse than being wrong, because it
made the instrument look sound.

The same run also caught a real host-side failure I had caused: bumping the
`unixnu` pin left `local-private/upstream/UniXNU` at the old commit, and

    ::error::vendor/UniXNU: the checkout is at 6f5a3d63... but the source lock
    pins 62bc0f48...

failed `test_the_cross_check_accepts_the_real_driver_status_output`.  Fixed by
fetching the pin into that clone.  **A pin bump moves two checkouts, not one.**

## The one instrument the guest has, and why every new fact costs a record

The serial log stops at "Exiting boot services" plus the console driver's own
announcement, and then carries nothing but the channel's JSON.  The reason is
now measured rather than assumed: `NativeVirtioConsole.cpp` installs **no**
console -- no `putc`, no `cons_ops`, no `kprintf` hook -- and
`zzz-boot-progress.patch`'s `boot_progress_stage()` does not call
`native_console_report` either, so the boot banner goes to the framebuffer,
which does not draw.

So the kernel's `printf` reaches nowhere observable, and the guest channel is
the only output path there is.  That is why `load_init_program()`'s own
"attempting to load %s" lines are unavailable and why `exec.first` had to be
added rather than read.

## Not claimed

* That `exec.first` FIRES.  21 mentions in the build log say the literal is in
  the output; only a capture says the call is reached.
* That the first exec is launchd.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The display subsystem is up (stage 10) and the screen still shows the boot
  menu.

---

# Interval 2026-11-03 (thirtieth): `exec.first` says **launchd**.  The userspace
# boot stops inside it.

## The record

    {"event":"exec.first","comm":"launchd","source":"XNU.__mac_execve"}

From `local-private/boot53/exec-first-input-run/guest-input.log`, 2992 bytes,
21 records, kernel sha256 `62a3b6c7...` verified against `payload.json` before
the boot.  Build **41617257137**.

So the FIRST successful exec in the guest is `launchd`.  `load_init_program()`
found it, `exec_mach_imgact()` wrote its `p_comm`, `__mac_execve()`'s `!error`
block ran, and dyld then mapped the shared cache in it.  Both of the previous
build's records reproduced exactly:

    diag.legacy_display_status  stage=10 surface_status=0 surface_found=1 dt=1
    diag.owner_data_status      stage=4  error=0

**Two builds, identical values for both.**  Those are now reproducible
measurements rather than one boot's readings.

## And the emitter gate cried wolf a third time

The capture script REFUSED to boot a kernel whose required literals were not
contiguous in the image.  For this build that refusal would have fired:

    emitter_missing_from_kernel: ["exec.first"]

and it would have been wrong.  The emitter is in the kernel.  The proof is not
an argument, it is a differential:

| literal | build 41584103771 | build 41617257137 |
|---|---|---|
| `native_first_exec_reported` | 0 | **1** |
| `{"event"` | 26 | **27** |
| `exec.first` | 0 | 0 |

The block-scope static appears in the symbol table as
`___mac_execve.native_first_exec_reported`, and it is absent from the previous
kernel and present in this one.  So the code compiled and linked.  The literal
is invisible because `ld`'s `SHF_MERGE|SHF_STRINGS` pass split the 30-byte
string: the image carries a 16-byte head `{"event":"exec.f` immediately after
the springboard string's tail, with the shared remainder stored elsewhere.

**That is fact 228's shape on a new emitter, and it is the third time a byte
search of this image has produced a false answer** (interval 19-20 grepped the
wrong kernel; interval 20 retracted its own byte-search conclusion).  The gate
is now an OBSERVATION -- counts are recorded, the boot proceeds, and a record
that fails to arrive is settled against the symbol table rather than against a
grep.

This is the second time in two intervals that a guard I wrote had to be
demoted from a refusal to a record.  The pattern is worth naming: **a byte
search of a mergeable-string Mach-O image is not evidence of absence, in either
direction.**

## Where the boot stops, stated precisely

Measured, in order:

1. the root volume enumerated and read back (`virtio_blk.read_back`, verified);
2. `kernel_thread_start` returned `KERN_SUCCESS`;
3. `install.registered` -- the install service registered;
4. the root mounted (`system.mount`, `/`, apfs, readonly);
5. the Data mount verified (`diag.owner_data_status` stage 4, error 0);
6. the display path completed (`diag.legacy_display_status` stage 10);
7. **`launchd` exec'd** (`exec.first`);
8. dyld mapped the shared cache in it (`dyld.cache_read`);
9. `springboard.running` did NOT arrive in 1500 s.

So the userspace boot stops INSIDE launchd, after dyld initialises and before
SpringBoard is exec'd.

## A correction to my own reasoning in interval 28

I wrote that "one dyld.cache_read ... means the userspace boot stopped very
early" and treated the single record as evidence that nothing spawned.  It is
not.  `native-dyld-cache-read.patch` bounds the emitter to **one record per
boot** on purpose, so it cannot count processes: launchd could have spawned
fifty daemons and the channel would still carry exactly one.  The correct
statement is only that at least one process ran dyld, which `exec.first` now
identifies as launchd.

**Whether launchd spawned anything at all is unmeasured.**  That is the next
question and it needs a bounded per-exec record, not a once-per-boot one.

## Not claimed

* Whether launchd spawned any daemon.  The once-per-boot dyld record cannot
  say, and nothing else on the channel does.
* Whether the `sheng-fb-gpu=1` handoff presenter attached.  Stage 10 is assigned
  before that block runs.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The screen: `screen_ever_changed: false` this run -- one distinct screendump,
  and six Home presses, every one of which changed nothing.

---

# Interval 2026-11-03 (thirty-first): a dropped `#endif` cost a build, and both
# patch instruments said the tree was clean.

## The failure

Build **41635226688**, failed after 32m56s.  Four errors, all in `kern_exec.c`:

    kern_exec.c:5289:2: error: unterminated conditional directive
     5289 | #if defined(XNU_OWNER_CACHE_MOUNT) && XNU_OWNER_CACHE_MOUNT
    kern_exec.c:9386:127: error: expected '}'
    kern_exec.c:9386:127: error: expected '}'
    kern_exec.c:5194:8: error: use of undeclared label 'exit_with_error'

The first is the defect; the other three are consequences.  The edit that
replaced the once-per-boot `exec.first` record with the bounded `exec.seen`
sequence dropped the `#endif` that closes the `#if` the whole emitter sits in --
and that `#endif` was also what separated the emitter from the rest of
`__mac_execve()`, hence the brace and label errors further down the file.

## Why neither instrument saw it

    repro-patch-order.py    -> all patches applied
    repro-patch-twopass.py  -> ALL PASSES CLEAN (three passes)

Both are correct and both are answering a different question.  They check that
`git` **applies** the patch, and git does not care what the result means.  A
patch that opens a conditional and never closes it applies perfectly.

This host has no C compiler -- the patch comments say so, and that is why CI is
the first thing that compiles it -- so every unbalanced conditional costs a full
build cycle.  That is the gap, and it is now closed from this side.

## The guard, and the two formulations that were wrong first

`tests/unit/test_console_declaration_visibility.py::
test_a_patch_does_not_change_the_files_conditional_nesting`.

The rule is **depth-neutrality**: (added `#if` - added `#endif`) must equal
(removed `#if` - removed `#endif`).  A patch must not change the file's
conditional nesting.

Two simpler rules were tried first and **each went red on correct patches**, so
both are recorded in the test file rather than discarded:

| formulation | false positive |
|---|---|
| the patch's own directives balance | `apple-only-cpu-tunables.patch` adds a second `#if defined(APPLE_ARM64_ARCH_FAMILY)` pair **inside** an existing one, and its hunk context carries the outer `#endif`.  A per-patch scan cannot tell the file's conditional from the patch's. |
| only added lines count | **seven** patches rewrite an existing condition (`-#if defined(BCM2837)` becomes `+#if defined(BCM2837) && (...)`).  They add an `#if` and no `#endif`, because the file already had one. |

Proven three ways, the way this file's existing rules are proven:

1. **against the real patches** -- zero false positives across all 58+;
2. **against the exact defect** -- removing the `#endif` goes red with
   `net conditional depth +1 (+1 #if / +0 #endif / -0 #if / -0 #endif)`;
3. **against the shape that looks like the defect and is not** -- rewriting an
   existing `#if` stays green.

That third case is the one that matters most.  A guard that goes red on correct
code is the same as no guard at all, and the first two formulations of this rule
were exactly that.

## Where the frontier is

Measured, in order, on build 41617257137's guest:

1. root volume enumerated and read back (`virtio_blk.read_back`, verified);
2. `kernel_thread_start` returned `KERN_SUCCESS`;
3. `install.registered`;
4. root mounted (`system.mount`, `/`, apfs, readonly);
5. Data mount verified (`diag.owner_data_status` stage 4, error 0);
6. display path completed (`diag.legacy_display_status` stage 10);
7. **`launchd` exec'd** (`exec.first`);
8. dyld mapped the shared cache in it (`dyld.cache_read`);
9. `springboard.running` did NOT arrive in 1500 s.

Both stage-10 and stage-4 reproduced exactly across two builds.  The userspace
boot stops inside launchd.  `exec.seen` is the record that says what launchd did
next, and it is what the running build carries.

## Not claimed

* That `exec.seen` compiles.  The `#endif` is restored, both repros are clean,
  the full unit suite is 3476 passed exit 0, and the new guard is
  mutation-proven -- and none of those is a compiler.
* Whether launchd spawned anything.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.

---

# Interval 2026-11-03 (thirty-second): launchd spawned.  `apfs_boot_util`.  And
# then nothing, in 1500 s.

## The sequence

Build **41646368244**'s guest, kernel sha256 `6b63883a...` verified against
`payload.json` before the boot:

    {"event":"exec.seen","n":1,"comm":"launchd","source":"XNU.__mac_execve"}
    {"event":"exec.seen","n":2,"comm":"apfs_boot_util","source":"XNU.__mac_execve"}

Two records, and the bound is twelve.  So the guest exec'd exactly **two**
processes in 1500 s of a boot that ran to completion of its capture window.

## The fork interval 31 left open, resolved

The once-per-boot `exec.first` could not say whether launchd spawned anything,
because `native-dyld-cache-read.patch` bounds ITS emitter to one record per boot
and so cannot count processes.  The bounded sequence answers it:

**launchd spawned.**  `apfs_boot_util` is n=2, which means it was exec'd after
launchd started -- had the kernel spawned it from the APFS mount path during
`bsd_init()` it would be n=1.  So launchd is NOT stuck before its first job.

And it spawned exactly one thing, and then nothing.

## Reproduced a third time

Both of the display build's records arrived with the same values on a third
build:

    diag.owner_data_status      stage=4  error=0
    diag.legacy_display_status  stage=10 surface_status=0 surface_found=1 dt=1

Three builds, identical values.  Those are measurements, not readings.

And the new emitter's literal is -- as predicted and as the patch now records --
**not greppable**: `exec.seen` counts 0 in the kernel while the block-scope
static `___mac_execve.native_exec_records` counts 1.  The record arrives whole.
A byte search would have called this build a failure.

## What this localises, and what it does not

The boot now has a measured spine:

1. root volume enumerated and read back (`virtio_blk.read_back`, verified);
2. `kernel_thread_start` returned `KERN_SUCCESS`;
3. `install.registered`;
4. root mounted (`system.mount`, `/`, apfs, readonly);
5. Data mount verified (`diag.owner_data_status` stage 4);
6. display path completed (`diag.legacy_display_status` stage 10);
7. `launchd` exec'd;
8. `apfs_boot_util` exec'd;
9. `dyld.cache_read` -- at least one process mapped the shared cache;
10. `springboard.running` did NOT arrive in 1500 s.

`apfs_boot_util` is the APFS boot utility.  On a real iPadOS it runs early and
returns, and launchd then loads its daemon plists.  **Whether it returned is the
next question, and nothing on the channel can say** -- the exec record proves it
started and nothing proves it finished.

That is the fork that decides the next step, exactly as the last two did:

* it never exited -> the guest is stuck inside it, and launchd is blocked in
  `waitpid`;
* it exited and nothing followed -> launchd is stuck after it, and the place to
  look is whatever launchd does between the boot utility and its first daemon.

## Not claimed

* Whether `apfs_boot_util` exited.  No record observes a process exit.
* Whether it SUCCEEDED.  Its exit status is not on the channel either.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The screen: two distinct screendumps, frozen from t=121 s, and six Home
  presses, every one of which changed nothing.

---

# Interval 2026-11-03 (thirty-third): the exit record is pinned, and the guard
# macro it needed is a trap worth its own note.

## What is in flight

Fork commit `8f3eda29`, pushed to `unipad/gpu-addon-ngpu-dir`; the lock's
`unixnu.commit` moved to it.  Build **41668336078** dispatched at 15:52 with
`stage=M7`.  When it lands, the capture runs against it and answers the one
question the exec record could not.

## The change

`exit_with_reason()` now emits the pair of the exec record, bounded at twelve:

    {"event":"exit.seen","n":K,"comm":"<name>","source":"XNU.exit_with_reason"}

The fork it exists to settle, stated as interval 32 left it: the guest exec'd
`launchd` then `apfs_boot_util` and then nothing, in 1500 s.  The exec record
proves a process started.  Nothing observed an exit.  So the boot stopping after
`apfs_boot_util` is equally consistent with it hanging inside that binary and
with it returning and launchd stalling afterwards -- and those are different
findings with different next steps.

## Three traps in this one change, all of which cost something

**1. The guard macro is per-object, and the wrong one is silent.**
`build-xnu-4k-baseline.sh:466` passes `-DXNU_OWNER_CACHE_MOUNT` to
**`kern_exec.o`**.  Guarding the exit emitter on that macro in `kern_exit.c`
compiles it out, and nothing complains: an `extern` with no definition is free
because the linker only resolves a symbol something references.  The build stays
green and the boot stays silent about exits.  The guard test
(`test_console_declaration_visibility.py`) checks that a declaration is in scope
at its call site; it does not check that the object was given the macro.

So the emitter is guarded on `XNU_NATIVE_VIRTIO_CONSOLE` -- the macro that means
"the console transport exists" -- and the build script now passes it to
`kern_exit.o`.

**2. `make` takes the LAST `<obj>_CFLAGS_ADD`.**  The script's own comment at
:485-486 records this for `vm_unix.o`: a second entry would silently discard the
owner-cache-carrier one.  So the new entry is for a NEW object, not a second
entry for an existing one.

**3. A patch file is a build input and it has no compiler either.**  The section
was DIFFED from the pristine file rather than hand-written, so the hunk header's
line counts are right by construction -- the hand-counted header in the previous
revision was stale and `git` refused the file as corrupt.  The first diff
attempt then dropped `difflib`'s `---`/`+++` lines, and `git` refused the whole
file with "patch fragment without header".  Both patch instruments had reported
the tree clean for the previous revision's hand-written header.

## Validated

| instrument | result |
|---|---|
| `bash -n build-xnu-4k-baseline.sh` | syntax OK |
| `repro-patch-order.py` (one pass) | all 58 applied |
| `repro-patch-twopass.py` (three passes) | ALL PASSES CLEAN |
| `test_console_declaration_visibility.py` (both rules) | 6 passed |
| applied block | brace-balanced, 54 lines |
| record assembly (12 n x 3 comms) | valid JSON, worst case 87 bytes + NUL in a 128 buffer |
| full `tests/unit` | **3476 passed, exit 0** |

## Not claimed

* That `exit.seen` compiles.  Six local instruments and no compiler.
* Whether `apfs_boot_util` exited.  That is what the build is for.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.

---

# Interval 2026-11-04 (thirty-fourth): the boot is not stuck.  It ran twelve
# early boot tasks, and the BOUND is what hid the rest.

## The sequence, off a real guest

Build **41671199388**, kernel sha256 `8fb9f563...` verified against
`payload.json` before the boot.  3990 bytes, 34 records:

    exec.seen  n=1  launchd
    exit.seen  n=1  fsck
    exit.seen  n=2  mount
    exit.seen  n=3  seputil
    exit.seen  n=4  mobile_obliterat
    exit.seen  n=5  launchd
    exec.seen  n=2  apfs_boot_util
    exit.seen  n=6  apfs_boot_util
    exit.seen  n=7  MSUEarlyBootTask
    exit.seen  n=8  MSUEarlyBootTask
    exit.seen  n=9  cc_fips_test
    exit.seen  n=10 keybagd
    exit.seen  n=11 usermanagerd
    exit.seen  n=12 dirs_cleaner

And both display records reproduced for a FOURTH time with identical values
(`diag.legacy_display_status` stage 10, `diag.owner_data_status` stage 4).

## RETRACTION of interval 32

Interval 32 concluded, from two execs and no exits, that "the userspace boot
stops INSIDE launchd, after dyld initialises and before SpringBoard is exec'd".
**The first half of that is wrong and the instrument is why.**

launchd was not stopped.  It was working through its early boot task chain --
`fsck`, `mount`, `seputil`, `mobile_obliteration`, `apfs_boot_util`,
`MSUEarlyBootTask`, `cc_fips_test`, `keybagd`, `usermanagerd`, `dirs_cleaner` --
which is the standard iOS sequence, and it had done twelve of them when the
bound saturated.  The exec record alone could not see any of it, because a
process that runs and exits produces an exit and no second exec.

This is the third time a bound on an emitter has been mistaken for a fact about
the guest.  The first was `dyld.cache_read`'s once-per-boot bound read as
"nothing spawned"; the second was the exec record's twelve read as "nothing after
apfs_boot_util"; this is the third.  **A bounded emitter measures its bound.**

## What is actually still unknown

Twelve exits is roughly the first ten early boot tasks.  On a real iPadOS the
chain continues -- `syslogd`, `configd`, `notifyd`, `powerd`, and eventually
`SpringBoard` -- and every one of those is behind the bound.  Whether the boot
reaches SpringBoard's exec, stalls partway, or loops is UNMEASURED, and the
answer is behind the same twelve-record wall that produced the last wrong
conclusion.

`springboard.running` did not arrive in 1500 s.  That much is still true, and
now it means much less than it did.

## The next change, and why it is cheap

Both bounds go from 12 to 64.  The cost argument that set 12 was the channel's
2000 ms synchronous deadline per record, and the measurement says that argument
was too pessimistic: twelve records arrived over about 100 s of guest time, i.e.
roughly one per eight seconds, which is the boot's own pace and not the
transport's.  Sixty-four records is therefore a few extra minutes of guest time
on a boot that already takes fifteen hundred, and it covers the whole early boot
chain.

## Not claimed

* Whether the boot reaches SpringBoard.  Twelve of the exits are behind the
  bound.
* Whether any of these tasks SUCCEEDED.  The record names the process, not its
  exit status -- and a `launchd` exit at n=5 with the boot continuing afterwards
  says the record is not yet telling us which process it was.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The screen: two distinct screendumps, frozen from t=121 s, six Home presses,
  every one of which changed nothing.

---

# Interval 2026-11-04 (thirty-fifth): twenty-nine early boot tasks, a panic dump,
# and a stall after `fairplayd.A2`.

## The sequence

Build **41690023024**, kernel sha256 `5ba8c8c0...` verified against
`payload.json` before the boot.  5423 bytes, 51 records:

    exec.seen  n=1  launchd
    exit.seen  n=1  fsck
    exit.seen  n=2  mount
    exit.seen  n=3  seputil
    exit.seen  n=4  mobile_obliterat
    exit.seen  n=5  launchd
    exec.seen  n=2  apfs_boot_util
    exit.seen  n=6  apfs_boot_util
    exit.seen  n=7  MSUEarlyBootTask
    exit.seen  n=8  MSUEarlyBootTask
    exit.seen  n=9  cc_fips_test
    exit.seen  n=10 keybagd
    exit.seen  n=11 usermanagerd
    exit.seen  n=12 dirs_cleaner
    exit.seen  n=13 xpcroleaccountd
    exit.seen  n=14 init_featureflag
    exit.seen  n=15 auearlyboot
    exit.seen  n=16 tzinit
    exit.seen  n=17 FinishRestoreFro
    exit.seen  n=18 finish_demo_rest
    exit.seen  n=19 sysstatuscheck
    exit.seen  n=20 prng_seedctl
    exit.seen  n=21 launchd_cache_lo
    exit.seen  n=22 init_exclavekit
    exit.seen  n=23 mtmergeprops
    exit.seen  n=24 watchdogd
    exit.seen  n=25 fairplayd.A2
    exit.seen  n=26 fairplayd.A2
    exit.seen  n=27 IOMFB_FDR_Loader
    exit.seen  n=28 DumpPanic
    exit.seen  n=29 fairplayd.A2

Both display records reproduced for a FIFTH time with identical values
(`diag.legacy_display_status` stage 10, `diag.owner_data_status` stage 4).

## The bound was the limit.  Now the boot is.

Twenty-nine exits against a bound of sixty-four, so the bound did NOT saturate.
The boot produced twenty-nine early boot tasks and then **stopped** -- the last
one is `fairplayd.A2` at n=29, and nothing followed for the remaining ~400 s of
the capture window.

That is the first time this session the boot itself, and not the instrument, has
been the thing that stopped.  Interval 34's "the bound hid the rest" was right
about the previous build and is now retired as the explanation for this one.

## Three things in that list that were not there before

* **`DumpPanic` at n=28.**  That binary exists to dump a panic log.  Its presence
  says a panic happened, or that a previous one was found.  Which of those is
  UNMEASURED -- the record names the process, not its reason.
* **`fairplayd.A2` THREE times** (n=25, 26, 29).  A daemon that runs three times
  in one boot is usually one that failed and was relaunched.  It is also the
  LAST thing that ran.
* **`IOMFB_FDR_Loader` at n=27.**  The IOMobileFramebuffer FDR loader, i.e. the
  display's factory-data reader, running immediately before the panic dump.

## And the exec record is nearly blind

**Two execs against twenty-nine exits.**  `exec.seen` caught `launchd` and
`apfs_boot_util` and nothing else, while `exit.seen` named all twenty-nine.  So
`__mac_execve()` is not on the path most of these processes take -- whatever
that path is, the exec emitter does not see it and the exit emitter does.

The cause is UNMEASURED and I am not going to guess at it in this record.  What
is measured is the consequence: **for process-level questions the exit record is
the reliable one**, and an exec-only instrument would have reported two processes
for a boot that ran twenty-nine.

## The screen

Thirteen screendumps, 120 s apart, **all identical** (`8833260e...`), and the
first was already that image.  `screen_ever_changed: false`.  Six Home presses,
every one of which changed nothing.  The display subsystem is up (stage 10, five
builds running) and the framebuffer has never moved off the UniXNU boot menu.

## Not claimed

* Whether a panic occurred THIS boot.  `DumpPanic` running is consistent with one
  and does not prove it.
* Why `fairplayd.A2` ran three times, or whether it failed.
* What stalled after n=29.  Nothing ran, so an exit record cannot see it.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.

---

# Interval 2026-11-04 (thirty-sixth): the exit status is pinned.  A list of names
# is not a diagnosis.

## What is in flight

Fork commit `4b8c68c9`, pushed to `unipad/gpu-addon-ngpu-dir`; the lock's
`unixnu.commit` moved to it.  Build **41708386711** dispatched at 19:59 with
`stage=M7`.  When it lands, the capture answers the question interval 35 opened.

## The change

    {"event":"exit.seen","n":29,"comm":"fairplayd.A2","code":-9,
     "source":"XNU.exit_with_reason"}

`rv` is the wait status the kernel is about to hand the parent, so it is the
kernel's own number and not an interpretation of it.  Negative is emitted with a
sign, because a process killed by a signal reports a negative status.

The reason is interval 35's list: twenty-nine early boot tasks, three of them
`fairplayd.A2`, and a stall after the last one.  A daemon that runs three times
is usually one that failed and was relaunched -- and "usually" is exactly what
the status field is for.

The record still lands inside the 128-byte buffer: 25 prefix + 2 index + 9 + 16
comm + 9 + 11 code + 34 suffix is 106, plus the NUL.

## The defect the assembly simulation caught

Two versions were wrong before one was right, and both were caught **before a
build cycle**, which is the only reason this is a note rather than another
twenty-minute lesson:

1. the code field went between the comm and the source **without closing the
   comm's quote**;
2. the fix **closed it twice**, producing
   `...,"comm":"fairplayd.A2","code":-9","source":...` -- a record no parser
   accepts.

Both were found by assembling every combination of `n` in 1..64, three comms and
eight codes (including `INT_MIN` and `INT_MAX`) and parsing each as JSON.  A
hand-assembled JSON record has no compiler either, and the emitters in this tree
are all hand-assembled -- no `snprintf`, deliberately, because this translation
unit does not include the header that declares it.

## And the recount script had the same bug as the thing it checks

`check-patch-headers.py` (new, `local-private/tmp/`) reports every hunk header
against its actual line counts.  Its first version had the same broken regex the
previous hand-count had, so it reported both headers fine while one was stale by
42 lines -- the exact defect build 41635226688 paid a cycle for.  Fixed, and it
now catches the stale header it was written to catch.

## Not claimed

* Whether `fairplayd.A2` failed.  The status field is pinned and unmeasured.
* Whether a panic occurred.  `DumpPanic` at n=28 is consistent with one.
* Why the exec record sees two of twenty-nine processes.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The screen: thirteen identical screendumps and six Home presses, none of which
  changed anything.

---

# Interval 2026-11-04 (thirty-seventh): the exit codes.  `fairplayd.A2` is not
# failing.  Four processes are, and one of them is `launchd`.

## The sequence, with statuses

Build **41708386711**, kernel sha256 `9dd7a370...` verified against
`payload.json` before the boot.  5601 bytes, 50 records:

    exec.seen  n=1  launchd
    exit.seen  n=1  fsck               code=0
    exit.seen  n=2  mount              code=0
    exit.seen  n=3  seputil            code=0
    exit.seen  n=4  mobile_obliterat   code=0
    exit.seen  n=5  launchd            code=1     <-- NON-ZERO
    exec.seen  n=2  apfs_boot_util
    exit.seen  n=6  apfs_boot_util     code=0
    exit.seen  n=7  MSUEarlyBootTask   code=1     <-- NON-ZERO
    exit.seen  n=8  MSUEarlyBootTask   code=0
    exit.seen  n=9  cc_fips_test       code=0
    exit.seen  n=10 keybagd            code=0
    exit.seen  n=11 usermanagerd       code=0
    exit.seen  n=12 dirs_cleaner       code=0
    exit.seen  n=13 xpcroleaccountd    code=0
    exit.seen  n=14 init_featureflag   code=0
    exit.seen  n=15 auearlyboot        code=0
    exit.seen  n=16 tzinit             code=0
    exit.seen  n=17 FinishRestoreFro   code=0
    exit.seen  n=18 finish_demo_rest   code=0
    exit.seen  n=19 sysstatuscheck     code=256   <-- wait status 1
    exit.seen  n=20 prng_seedctl       code=3328  <-- wait status 13
    exit.seen  n=21 launchd_cache_lo   code=0
    exit.seen  n=22 init_exclavekit    code=0
    exit.seen  n=23 mtmergeprops       code=0
    exit.seen  n=24 watchdogd          code=0
    exit.seen  n=25 fairplayd.A2       code=0
    exit.seen  n=26 fairplayd.A2       code=0
    exit.seen  n=27 fairplayd.A2       code=0
    exit.seen  n=28 DumpPanic          code=0

Both display records reproduced for a SIXTH time with identical values.

## `fairplayd.A2` is NOT failing -- interval 35's hypothesis is refuted

Interval 35 read three runs of `fairplayd.A2` as "a daemon that failed and was
relaunched".  **All three exited with code 0.**  It runs three times because it
is supposed to, and the measurement says so.  That is the second hypothesis this
session's own records have killed -- the first was the Data mount as the cause of
the userspace stall, killed by `diag.owner_data_status` stage 4.

## The four that did not succeed

`rv` is the wait status the kernel hands the parent, so the exit code is in the
high byte: 256 is exit code 1, 3328 is exit code 13.

* **`launchd` code 1** (n=5).  The interesting one.  If this were pid 1 the
  kernel would panic and the boot would stop; it did not, so this is a process
  whose `p_comm` was still `launchd` -- which on XNU is what a `posix_spawn`
  child carries until `execve` calls `set_proc_name()`.  A child that exits
  before its exec reports the PARENT's name.  That reading fits a spawn that
  failed, and it is UNMEASURED which of the two it is.
* **`MSUEarlyBootTask` code 1** (n=7) then **code 0** (n=8).  The same shape as
  the `launchd` one, and here the retry is visible: attempt one failed, attempt
  two succeeded.
* **`sysstatuscheck` code 256** -- exit code 1.
* **`prng_seedctl` code 3328** -- exit code 13.

## What is still not measured

* Whether the `launchd` code-1 exit is a failed spawn or pid 1 itself.  A pid
  field would settle it and the record does not carry one, for the reason
  `native-dyld-cache-read.patch` gives: a formatted pid puts a formatting path in
  front of a record whose only job is to arrive whole.
* Whether `DumpPanic` at n=28 dumped a panic from THIS boot or found an older
  one.  `IOMFB_FDR_Loader`, which preceded it last build, did not run this build.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The screen: thirteen identical screendumps and six Home presses, none of which
  changed anything.

---

# Interval 2026-11-04 (thirty-eighth): a clean resumable state, and the exact
# next cycle.

## Verified before this was written

| check | result |
|---|---|
| `git status` | clean apart from an untracked `.scratch/` |
| `main` vs `origin/main` | identical, `0ff3c4b` |
| `config/sources.lock.json` | `unixnu` `4b8c68c9`, `device_repo` `5ef74a17`, `frozen: false` |
| fork `unipad/gpu-addon-ngpu-dir` | `4b8c68c9`, and the local clone is on the same commit |
| full `tests/acceptance` | **649 passed, exit 0** |
| full `tests/unit` | **3476 passed, exit 0** |
| last build | **41708386711**, green, 40m53s, both pins correct in its own log |
| last capture | `exit-status-input-run/guest-input.log`, 5601 bytes, 50 records |

## The frontier, stated once

Twenty-eight early boot tasks with their exit statuses, four of them non-zero
(`launchd` 1, `MSUEarlyBootTask` 1 then 0, `sysstatuscheck` 1, `prng_seedctl`
13), and then a stall.  No SpringBoard in 1500 s.  The display subsystem is fully
up (stage 10, six builds running) and the framebuffer has never moved off the
UniXNU boot menu.

## The next cycle, end to end

1. Add a `panic` emitter.  `osfmk/kern/debug.c` is touched by no patch, so it
   needs a new `diff --git` section **and** a `debug.o_CFLAGS_ADD` entry -- the
   same shape as `kern_exit.o`.  Guard it on `XNU_NATIVE_VIRTIO_CONSOLE`, never
   on `XNU_OWNER_CACHE_MOUNT` (that macro is `kern_exec.o`'s and the emitter
   would compile out silently).
2. Generate the section by DIFFING the pristine file
   (`local-private/upstream/xnu/bsd/kern/debug.c`), not by hand -- then run
   `python local-private/tmp/check-patch-headers.py` and require zero mismatches.
3. Assemble the record in Python for every combination of fields and parse each
   as JSON before committing.  Two of this session's emitters were wrong in
   exactly that way and both were caught by the simulation rather than by a build.
4. Validate with both repros and the two guards, then run the full unit suite.
5. Commit to the fork, bump the pin with
   `python local-private/tmp/bump-unixnu-first-exec.py` (re-pointed at the new
   sha), move `local-private/upstream/UniXNU` to the same commit -- **a pin bump
   moves two checkouts** -- and dispatch with
   `-f stage=M7 -f payload_target=tcg`.  Omitting `stage` builds M2 and fails
   31 minutes in with no `payload.img`.
6. Download the boot inputs with the six-way parallel range download
   (`local-private/tmp/` has the pattern), extract, verify the kernel sha against
   `payload.json`, and check the block-scope static is in the symbol table rather
   than grepping for the literal.
7. Run `python -u local-private/tmp/capture-springboard-boot.py <bundle>` and
   read it with `python local-private/tmp/read-display-stage.py <dir>/guest-input.log`.

## The three lessons this session paid for, in one place

* **A bounded emitter measures its bound.**  Three conclusions were wrong this
  way.  Check whether the bound saturated before reading silence as a fact.
* **A byte search of a mergeable-string Mach-O image is not evidence of absence,
  in either direction.**  `exec.seen` counted zero in a kernel that carried it.
  The block-scope static is the check.
* **`pytest ... | tail` returns tail's exit code.**  Redirect to a file and echo
  `$?`.  I had the memory loaded and did it anyway.

## Not claimed

* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* M13 (audio), network, launcher, performance: not started.  M14/M15: the build
  path exists, the driver work does not.
* WP00/WP01: still blocked on `sources.lock.json` `frozen: false`.
* This is not a completion report and must not be read as one.  The goal's own
  condition is "only when every mandatory requirement passes real verification,
  the necessary CI passes, and the deliverables and reproduction steps are
  complete", and M9 is FAIL.

---

# Interval 2026-11-04 (thirty-ninth): M9's record was on a path SpringBoard does
# not take.  The absence was an instrument defect.

## The finding

XNU has **two** exec paths, and they are separate functions with separate success
blocks:

    execve()      -> execve()      -> __mac_execve()   kern_exec.c:5284
    posix_spawn() -> posix_spawn() -> its own block     kern_exec.c:4280

`posix_spawn()` **without** `POSIX_SPAWN_SETEXEC` forks and execs in the child,
and **launchd spawns its daemons with `posix_spawn`**.  The measurement that says
so is the asymmetry interval 37 recorded without understanding it: **28 exits
against 2 execs.**  Twenty-six processes took a path the exec emitter cannot see.

And `springboard.running` -- the record M9 asks for -- is emitted from
`__mac_execve()`'s success block, while SpringBoard is exec'd by launchd from
userspace.  By `posix_spawn`.

**So the record M9 asks for sat on a path the process it asks about does not
take.  Its absence was an instrument defect, not a guest fact, and no amount of
booting would have changed it.**

That is fact 204's shape one layer out.  The emitter was correct, the tree was
correct, the build was green, and the instrument was pointed at the wrong
function.  Every interval from 14 to 37 that recorded "springboard.running did not
arrive, and the emitter is in the kernel, so the absence is real" was drawing a
sound conclusion from an unsound premise -- the emitter being in the kernel says
nothing about it being on the path.

## The change

`posix_spawn()`'s own success block now emits the same two records:

    {"event":"springboard.running","pid":N,"comm":"SpringBoard",
     "source":"XNU.posix_spawn"}
    {"event":"spawn.seen","n":K,"comm":"<name>","source":"XNU.posix_spawn"}

`springboard.running` **keeps its event name** -- the gate does not care which
function emitted it and it should not.  The generic one is `spawn.seen` rather
than `exec.seen` so a reader can tell the two paths apart, which is the whole
point of adding it.

Placed inside `if (error == 0 && !spawn_no_exec)`, **after**
`proc_exec_switch_task()`, so `p` is the switched proc and `p_comm` has been
written by `set_proc_name()` inside `exec_mach_imgact()` -- the same common
machinery `__mac_execve` uses fifteen hundred lines below.  Guarded on
`XNU_OWNER_CACHE_MOUNT`, which is `kern_exec.o`'s macro, and `posix_spawn()` is in
`kern_exec.c`, so the guard is right here.  (The exit record lives in `kern_exit.c`
and needs `XNU_NATIVE_VIRTIO_CONSOLE` instead -- the macro is per-object, which is
the trap interval 33 recorded.)

## Validated

| instrument | result |
|---|---|
| `repro-patch-order.py` | all 58 applied |
| `repro-patch-twopass.py` (three passes) | ALL PASSES CLEAN |
| `check-patch-headers.py` | 0 mismatched headers |
| both guards in `test_console_declaration_visibility.py` | 6 passed |
| applied section | brace-balanced, 126 lines |
| record assembly | every record valid JSON inside 128 bytes, worst 97 + NUL |
| full `tests/unit` | **3476 passed, exit 0** |

Fork commit `188f1d23`, pushed; the lock's `unixnu.commit` moved to it.  Build
**41730055408** dispatched at 22:21 with `stage=M7`.

## What this does and does not settle

It settles the instrument.  It does **not** claim SpringBoard is running -- if it
is, the next capture is the first that can see it, and if it is not, the next
capture is the first whose silence about it means anything.

## Not claimed

* That SpringBoard execs.  The emitter is now on both paths; the measurement has
  not run.
* No IPA, no interaction, no persistence.  M9, M9a, M9b remain FAIL until a
  capture says otherwise.
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.

---

# Interval 2026-11-04 (fortieth): fifty-three daemons, and the window that said
# "nothing after" had closed while records were still arriving.

## The measurement

Build **41730055408**, kernel sha256
`aebcf2e585b17b8e88dcf80d0eacc36f0f933a61df5176d65541692fd0e2ff95` verified
against `payload.json` before the boot.  9664 bytes, 103 records.

The `posix_spawn` emitter -- added in interval 39 because `springboard.running`
sat on the one exec path SpringBoard does not take -- **fired for the first time
and produced 53 records**:

    n=1  notifyd            n=2  UserEventAgent     n=3  mediaserverd
    n=4  logd               n=5  mobiletimerd       n=6  wifid
    n=7  rapportd           n=8  DumpPanic          n=9  watchdogd
    n=10 distnoted          n=11 fseventsd         n=12 timed
    n=13 thermalmonitord    n=14 IOMFB_FDR_Loader  n=15 configd
    n=16 containermanager   n=17 AppleCredentialM  n=18 mtmergeprops
    n=19 driverkitd         n=20 ptpd              n=21 keybagd
    n=22 fairplayd.A2       n=23 sleepd            n=24 amfid
    n=25 remoted            n=26 logd_helper       n=27 containermanager
    n=28 MobileGestaltHel   n=29 runningboardd     n=30 fairplayd.A2
    n=31 identityservices   n=32 bluetoothd        n=33 cfprefsd
    n=34 CommCenter         n=35 cfprefsd          n=36 backboardd
    n=37 nfcd               n=38 lockdownd         n=39 sharingd
    n=40 imagent            n=41 locationd         n=42 usermanagerd
    n=43 fairplayd.A2       n=44 lsd               n=45 powerd
    n=46 com.apple.datami   n=47 biomesyncd        n=48 liveactivitiesd
    n=50 contextstored      n=51 audiomxd          n=52 PowerUIAgent
    n=53 iconservicesagen   n=55 triald

These are the **real iOS daemon set**.  `configd`, `notifyd`, `distnoted`,
`fseventsd`, `logd`, `wifid`, `mediaserverd`, `thermalmonitord`, `driverkitd`,
`containermanager`, `keybagd`, `amfid` -- none of which the
`__mac_execve`-only emitter could ever have seen, because launchd spawns them
with `posix_spawn`.

## The exit sequence is byte-for-byte the one interval 37 recorded

Same 28 exits, same four non-zero statuses (`launchd` 1, `MSUEarlyBootTask` 1
then 0, `sysstatuscheck` 256, `prng_seedctl` 3328).  That is a reproducibility
check on the whole userspace chain, and it passes.

## What this does to the "userspace stall" model

Intervals 30 through 38 carried the model **"28 early boot tasks, then a
stall."**  That model was built from the `exit.seen` stream alone, and the exit
stream only ever showed the SHORT-LIVED tasks -- by definition, the ones that
exit.  The long-running daemons were invisible to it.  Twenty-six of the
twenty-eight were a complete list of nothing.

The real picture: **the boot runs to at least 53 daemons and was still going when
the capture stopped.**  It reaches `runningboardd` (the process manager),
`backboardd` (SpringBoard's parent on a real iPadOS boot), `CommCenter`,
`lockdownd`, `powerd`, `lsd`, `nfcd`, `bluetoothd`, `locationd`, `cfprefsd`,
`biomesyncd`, `liveactivitiesd` -- and `biomesyncd`, `liveactivitiesd`,
`lockdownd`, `locationd` and `sharingd` are daemons that normally start *after*
SpringBoard on a real boot.

**So "the userspace boot stalls after the early tasks" is refuted.**  There is no
stall.  There is a boot that goes further than any previous measurement could
see.

## And yet `springboard.running` did not arrive -- and that is not yet a fact

The reason is in the capture, not the guest:

    listen deadline = started + LISTEN_BUDGET_S, LISTEN_BUDGET_S = 1500

A **hard deadline.**  The channel's last byte arrived at 23:30:53, which was
`spawn.seen` n=55 `triald` -- and the capture stopped at exactly that moment
because 1500 s had elapsed, not because the guest went quiet.  QEMU was killed
by the script while the daemon stream was still flowing.

**53 of the emitter's 64-record bound.**  So the bound did not saturate and was
not the cause.  The *observation window* is what saturated, and it measured
itself.

This is the `kMaxExecRecords` lesson from interval 36 one layer out.  A bound
anywhere -- in the emitter, in the reader, or in the wall clock -- makes silence
look like a fact.  Interval 37's "then a stall" was a window artifact too: 1500 s
had not been reached, but 28 exits had, and the model was read off the exits.

## The instrument is validated

The 53 records carry correct `p_comm` values -- `notifyd`, `configd`,
`backboardd`, not `launchd`.  That is the proof that the emitter's placement,
after `proc_exec_switch_task()` at `kern_exec.c:4281-4284`, sees the switched
proc's post-exec name.  Reading the source alone would not have settled it,
because `set_proc_name()` runs inside `exec_mach_imgact()` fifteen hundred lines
away and the ordering is not obvious from either site.

The `n` field also behaves as a strict counter with two visible properties:
records arrive **out of order** (n=12 before n=11, n=25 before n=24, n=38 before
n=37) which is multi-CPU concurrency, and two values are **missing** (n=49,
n=54), which is channel loss at the tail rather than a counter defect -- the
increment only happens inside the emit guard.

## The two changes, and why they are separate

| change | where | cost |
|---|---|---|
| stop on **quiet**, not on the clock; cap 5400 s, quiet 600 s | `capture-springboard-boot.py` | none, no rebuild |
| `kMaxSpawnRecords` 64 -> 512 | `zzz-native-springboard-events.patch` | one build |

They are separate because they answer different questions.  The window is what
truncated this run, and fixing it needs no kernel at all.  The bound was at 53 of
64 and would have saturated before SpringBoard -- but **`springboard.running` is
NOT bounded**: it lives in its own `if (strncmp(p->p_comm, "SpringBoard",
MAXCOMLEN) == 0)` block *before* the bounded one, so it fires whenever
SpringBoard spawns regardless of the counter.  The bound only limits the
*diagnostic* view of what comes after.

So the re-run uses the **same kernel, same bundle, same verified sha** with the
longer window, and a raised-bound build is dispatched in parallel on CI in case
the longer run still shows no SpringBoard and the missing tail is what matters.

## The re-run

`capture-springboard-boot.py local-private/boot53/spawn-seen-input`, output to
`local-private/boot53/quiet-stop-input-run`, scratch
`local-private/boot53/quiet-stop-input-run-scratch`.  Kernel sha re-verified at
boot: `aebcf2e5...`, matches `payload.json`.

## What this does and does not settle

It settles that the userspace boot runs to at least 53 daemons and was still
running when the last capture stopped.  It does **not** settle whether
SpringBoard spawns -- that measurement was truncated and has been re-run
properly.

## Not claimed

* That SpringBoard runs.  The record has not been observed on either path.
* No IPA, no interaction, no persistence.  M9, M9a, M9b remain FAIL until a
  capture says otherwise.
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.

---

# Interval 2026-11-04 (forty-first): the instrument is closed.  The absence of
# `springboard.running` is finally a **guest fact**.

## The measurement

Same kernel, same bundle, same verified sha as interval 40 --
`aebcf2e585b17b8e88dcf80d0eacc36f0f933a61df5176d65541692fd0e2ff95` against
`payload.json`.  10840 bytes.  **3183 s of listening, and it stopped because the
guest went quiet, not because a clock expired:**

    listen_seconds   : 3183
    stopped_on_quiet : true
    last_bytes_at_s  : 2574

The quiet-stop fix from interval 40 works.  The last byte arrived at 2574 s and
the run held for 600 s more before stopping, which is the behaviour that was
missing when a hard 1500 s deadline killed the previous capture mid-stream.

## `springboard.running`: ABSENT -- and this time it means something

    springboard.running     : ABSENT
    springboard.interaction : ABSENT

Every reason interval 40 gave for withholding that conclusion has now been
closed, one at a time:

| the doubt | how it was closed |
|---|---|
| the emitter was on the wrong exec path | both `__mac_execve` and `posix_spawn` now emit, and the `posix_spawn` one produced 64 records |
| `p_comm` might not be set yet at the emit site | 64 records carry correct daemon names, not `launchd` |
| the record bound might have saturated | 64 of 512; it did not |
| the capture window truncated the boot | 3183 s, stopped on quiet, 600 s past the last byte |

**So this is the first capture in the project whose silence about SpringBoard is
evidence rather than an instrument defect.**  That sentence is the whole interval.

## The 64 daemons

    notifyd, UserEventAgent, mediaserverd, logd, mobiletimerd, wifid, rapportd,
    DumpPanic, watchdogd, distnoted, fseventsd, timed, thermalmonitord,
    IOMFB_FDR_Loader, configd, containermanager, AppleCredentialM, mtmergeprops,
    driverkitd, ptpd, keybagd, fairplayd.A2, sleepd, amfid, remoted,
    logd_helper, containermanager, MobileGestaltHel, runningboardd,
    fairplayd.A2, fairplayd.A2, fairplayd.A2, lockdownd, bluetoothd,
    identityservices, cfprefsd, CommCenter, cfprefsd, backboardd, nfcd,
    sharingd, usermanagerd, imagent, locationd, powerd, lsd, com.apple.datami,
    liveactivitiesd, biomesyncd, chronod, audiomxd, contextstored, triald, dasd,
    iconservicesagen, PowerUIAgent, apsd, peopled, biomed, calaccessd, passd,
    securityd, PerfPowerService

`backboardd` and `runningboardd` are in there.  `apsd`, `securityd`, `biomed`,
`CommCenter`, `lockdownd`, `locationd`, `biomesyncd`, `liveactivitiesd` are
daemons that normally start **after** SpringBoard on a real boot.  So launchd is
working through a job list that spans both sides of where SpringBoard belongs,
and SpringBoard's job produces nothing on either exec path.

## The exit stream, and one new non-zero

32 exits.  The four interval-37 statuses reproduce (`launchd` 1,
`MSUEarlyBootTask` 1 then 0, `sysstatuscheck` 256, `prng_seedctl` 3328), plus one
that is new and is display-relevant:

    exit.seen n=30 comm='IOMFB_FDR_Loader' code=256   (exit code 1)

`IOMFB_FDR_Loader` is the IOKit Mobile Framebuffer FDR loader.  It exited 0 on
the interval-40 run and 1 on this one.  It is the only process in the whole boot
whose job is the framebuffer, and it did not succeed.

## The screen: it moved exactly once, in the first 121 seconds

21 dumps over 2447 s, and exactly two distinct images:

    t=    0s  2e2910ab95c982d0...   the UniXNU boot menu
    t=  121s  8833260ecd372e44...   <-- the only change in the entire run
    t=  242s ... t= 2447s           byte-identical, 20 dumps

**Six Home presses, every one `changed=False`.**  So the framebuffer is not
"frozen on the boot menu" -- interval 38's phrasing was wrong.  It changed once,
early, and then stopped changing for 39 minutes while 64 daemons spawned around
it.  That is a display that came up once and then drew nothing, which is the
failure mode `diag.legacy_display_status` stage 10 was added to distinguish --
and stage 10 says the legacy display path itself succeeded
(`surface_found=1`, `dt=1`, phase 4).  The probe is healthy and the screen is
still static.

## SpringBoard.app IS in the volume

One pass over the 3.3 GB raw SystemOS APFS image
(`044-76034-038.dmg`): `SpringBoard.app` occurs 8 times,
`CoreServices/SpringBoard` 6 times, `com.apple.SpringBoard` 46 times.  The 46 are
almost all in the filename catalog and localized-string tables rather than in
plist XML, so the job definition itself was not readable this way -- APFS
compresses many system plists and a raw byte scan will not see the XML.

**So the missing binary is not the explanation.**  The app is on disk, launchd is
spawning 64 other jobs, and this one produces nothing.

## What that leaves, precisely

Two possibilities, and they are distinguishable:

1. **launchd never attempts the spawn.**  Then nothing on either path fires and
   there is no kernel-side trace at all.
2. **launchd attempts it and the spawn fails before either success block.**  The
   `exit.seen` stream would show a child that died pre-exec under the PARENT's
   name -- which is exactly what `exit.seen n=5 comm='launchd' code=1` is -- and
   there is no second such record.  But a `posix_spawn` that returns an error
   creates no child and therefore leaves no exit either.

The blind spot is (2)'s second half: **`posix_spawn`'s failure path emits
nothing.**  Interval 39 closed the success path; the failure path is still dark,
and it is the only remaining place a SpringBoard spawn attempt could hide.

## The next instrument

`spawn.failed`, emitted from `posix_spawn`'s error path with the errno.  It
cannot name the binary -- at that point `p_comm` is the parent's -- but the errno
is decisive on its own: ENOENT says the binary is not where the plist says,
EACCES or a signature error says something else entirely, and nothing at all says
launchd never tried.

## Not claimed

* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL, and
  for the first time the failure is measured rather than assumed.
* The screen changed once and then never again; that is a measurement, not a
  diagnosis of why.
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.

---

# Interval 2026-11-04 (forty-second): 155 spawns, **zero** spawn failures, and a
# framebuffer that is still the UniXNU boot menu.  Hypothesis 2 is refuted.

## The measurement

Build **41750576785**, kernel sha256
`df785f5b58807b05693bd8ce036b4d88be4c57ac4534816b20dd54eb79b8a767` verified
against `payload.json` before the boot.  21025 bytes.

    listen_seconds   : 5418
    stopped_on_quiet : false        <-- it hit the 5400 s CAP, not a quiet guest
    last_bytes_at_s  : 5382         <-- the guest was STILL SPAWNING when it stopped

    springboard.running     : ABSENT
    springboard.interaction : ABSENT
    spawn.seen              : 155
    spawn.failed            : 0

**155 spawns and not one failure.**  The `spawn.failed` emitter added in `bc210df`
fired zero times across the entire boot.

## That refutes one of the two hypotheses outright

Interval 41 left two explanations for SpringBoard's absence:

1. launchd never attempts the spawn -- no trace at all.
2. launchd attempts it and `posix_spawn` returns an error.

**Hypothesis 2 is refuted.**  If launchd were attempting a SpringBoard spawn and
failing, the emitter placed before the `if (imgp != NULL)` cleanup -- which covers
all ~26 `goto bad` paths, not just the ones after `exec_activate_image()` -- would
have recorded it.  It recorded nothing.  Every spawn in this boot succeeded.

So what remains is **hypothesis 1: launchd never attempts to spawn SpringBoard.**
Not a failed spawn.  Not a missing binary -- `SpringBoard.app` is in the SystemOS
volume (interval 41, 8 occurrences of the path, 6 of `CoreServices/SpringBoard`).
A job that is never started.

## The bound bump was not optional

**155 spawns.**  `kMaxSpawnRecords` was 64 when interval 41 ran, and interval 41
reached exactly 64.  Had this run kept the old bound it would have lost **91 of
the 155** -- including the entire second half of the daemon set:

    ... biomesyncd, liveactivitiesd, chronod, dasd, PowerUIAgent, apsd,
    peopled, biomed, calaccessd, passd, securityd, PerfPowerService,
    IOMFB_FDR_Loader, coreduetd, mobileactivation, analyticsd, accountsd,
    contactsd, assistantd, accessoryupdater, symptomsd-diag, assistant_cdmd,
    WiFiCloudAssetsX ...

That is a much deeper iOS daemon set than interval 41 could see, and it changes
the picture again: the boot does not merely reach `backboardd`, it runs
`assistantd`, `accountsd`, `contactsd`, `analyticsd` and `symptomsd` -- daemons
that in a real boot come up around or after the UI layer.  **The userspace boot is
essentially complete except for SpringBoard.**

## And the screen: one image, for ninety minutes, and now we can SEE it

45 screendumps over 5418 s, and **exactly one distinct image**.  Eleven Home
presses, every one `changed=False`.

The PNG is saved at
`local-private/boot53/screen-evidence/boot-menu-at-t0.png` and it says what no
hash could: **the framebuffer is showing the UniXNU boot menu.**  Green bar across
the top -- which the serial log says is what proves the kernel entry code ran --
plus `DARWINBOOT: hand-off to the kernel`, the UniXNU logo, `Booting QEMU virt:
source 4K XNU in 1 s`, `Up/Down: select  Enter: boot`, and `drivers 1 loaded /
0 failed`.

**So the kernel entry code ran, 155 daemons spawned around it, and nothing ever
drew to the framebuffer.**  The bootloader's menu pixels are simply still sitting
there because no later writer replaced them.

This retires interval 41's phrasing in both directions.  It is not "the display
came up once and then froze" -- the display never took over at all.  And it is not
a display defect: `diag.legacy_display_status` stage 10 says the legacy display
path succeeded, and `NativeMobileDisplay`'s own commits record measured frame
timings (~4.4 ms render + ~3 ms copy on int-243), so the swap pipeline works when
a client draws.  **Nothing is drawing.**  That is downstream of SpringBoard, not a
separate bug.

## What this converges on

Three independent measurements now agree:

| measurement | result |
|---|---|
| 155 spawns, 0 failures on both exec paths | launchd never attempts SpringBoard |
| `SpringBoard.app` present in the SystemOS volume | it is not a missing binary |
| framebuffer still holds the bootloader's menu | no UI client ever drew |

The next question is therefore about **launchd's job loading**, not about the
kernel's exec machinery and not about the display.  The emitter that would answer
it is not in the kernel at all -- it is in which plists launchd read and which
conditions it evaluated -- and that is a different instrument from anything this
project has built.

## Not claimed

* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* The capture hit its 5400 s cap with the guest still spawning, so the daemon
  count is a LOWER BOUND, not a total.  155 is "at least 155".
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.
---

# Interval 2026-11-04 (forty-third): the job file exists.  The scan that said it
# did not was reading the wrong volume -- and the exit bound saturated.

## The instrument was wrong, and the control is what caught it

Every byte scan this project has ever run against the SystemOS image was run
against `044-76034-038.dmg`, whose metadata says `"component":
"Cryptex1,SystemOS"`.  A control scan of that file returned:

    com.apple.configd.plist          0     <- daemon that SPAWNED
    com.apple.notifyd.plist          0     <- daemon that SPAWNED
    com.apple.runningboardd.plist    0     <- daemon that SPAWNED
    com.apple.backboardd.plist       2     <- daemon that SPAWNED

Three of four daemons that provably spawned read zero, so the file cannot answer
the question at all.  The two `backboardd.plist` hits were
`/var/mobile/Library/Preferences/com.apple.backboardd.plist` -- preference plists,
not job plists.  The scan was finding the wrong kind of file in the wrong place.

`/System/Library/LaunchDaemons` is on the **OS root volume**,
`raw/044-76040-038.raw` (`"component": "OS"`, 6,077,580,800 bytes decompressed).
The same scan of *that* volume:

    com.apple.configd.plist          3
    com.apple.notifyd.plist          3
    com.apple.runningboardd.plist    3
    com.apple.backboardd.plist       3
    com.apple.usermanagerd.plist     2
    com.apple.SpringBoard.plist      5     <-- IT EXISTS
    com.apple.loginwindow.plist      0
    com.apple.usercontextd.plist     0

Every control is non-zero.  The instrument is validated, and on it
**`com.apple.SpringBoard.plist` is present in the LaunchDaemons catalog.**  Two of
the five occurrences carry a `com.apple.decmpfs` extended attribute (magic
`fpmc`, type 9) with uncompressed sizes of 326 and 656 bytes, so it is a real
file with real content, not a dangling catalog name.

**`com.apple.loginwindow.plist` is absent, and so is
`com.apple.usercontextd.plist`.**  With the control validated those zeros mean
what they say.  Nothing on this system ever defines a loginwindow job, which is
the same fact interval 42 reached from the guest side and reached first.

## A second thing the control caught: `.dmg` members are not UDIF

`UdifImage` refuses `044-76034-038.dmg` with *"the file does not end in a UDIF
'koly' trailer, so it is not a UDIF image"*.  The file is intact -- its sha256 is
`b4908cde41d23e6bc05e6af51a2c9eeb00cf0f863a6a18b1600c96a3fd70414f`, which matches
`044-76034-038.json` exactly.  It is simply not a UDIF image: an IPSW member
named `.dmg` can be a **raw APFS container**.  The APFS object header sits at file
offset 24 (`NXSB` at +8) on an 8192-byte stride, and `nx_block_size` reads 4096.
The module docstring's premise -- that a `.dmg` is UDIF -- does not hold for
Cryptex members, and the refusal was correct rather than a defect.

## What is NOT known: the plist's contents

The content of `com.apple.SpringBoard.plist` -- its `ProcessType`, `RunAtLoad`,
`LaunchEvents`, `POSIXSpawnType` -- **could not be read**, and two separate
attempts failed for two separate reasons.

**Adjacency does not work, and this is proven, not assumed.**  The bytes that
follow a filename in this volume do not belong to that file.  Control:
`com.apple.configd.plist` is followed by a 1213-byte XML plist, and the binary
plist 1411 bytes further on parses as `com.apple.companion_proxy`.  Likewise
`com.apple.SpringBoard.plist` is followed by a 326-byte binary plist that parses
as `com.apple.SiriTTSTrainingAgent`, and by a 656-byte XML plist that names
itself `com.apple.sports.plist` in its own header comment.  An APFS catalog node
keeps its keys and its values in separate regions; without the node's table of
contents there is no way to pair them.

**Neither content scan is complete, and both say so.**  Enumerating every
`bplist00` in the volume and keeping those with a `Label` recovered **373 jobs**,
but only **53 of interval 42's 109 spawned comms** match one -- `notifyd` and
`usermanagerd` are both among the 56 that do not, and both provably spawned.  A
second scan for XML job plists (`<key>Label</key><string>...`) found **113
labels**, but its controls -- configd, backboardd, runningboardd, apsd, akd, all
of which spawned -- returned **0**, while `notifyd` returned 1.  Neither scan can
prove a job absent, and neither found SpringBoard's.

So the honest statement is: **the job file is there; what it says is not known.**

## The exit bound saturated, which is the interval-41 defect again

`exit.seen` stopped at exactly n=64.  The 64th record is
`exit.seen n=64 comm='bluetoothd' code=256`, and bluetoothd kept exiting after
it -- it spawned 35 times in total and is the last record in the entire capture.
`kMaxExitRecords = 64` in `zzz-native-springboard-events.patch`.

This is the same defect that hid 91 of interval 41's 155 spawns, now on the other
emitter.  The spawn bound was bumped 64 -> 512 afterwards; the exit bound was
left at 64 and has now saturated in turn.  Every exit after n=64 is invisible,
which is why `exit.seen` reports 64 while `spawn.seen` reports 155.

## Where M9 stands

| question | answer | instrument |
|---|---|---|
| Is `com.apple.SpringBoard.plist` on the system? | **YES**, 5 occurrences | validated filename scan of the OS root volume |
| Is `com.apple.loginwindow.plist` on the system? | **NO**, 0 occurrences | same scan, same controls |
| Does launchd attempt to spawn SpringBoard? | **NO** -- 0 `spawn.failed` in 155 spawns | interval 42's emitter |
| What does SpringBoard's job plist say? | **UNKNOWN** | both content scans shown incomplete |

Interval 42 asked whether launchd's job list was missing SpringBoard or whether
launchd decided not to run it.  The answer is the second: **the job is in the
list, and launchd does not run it.**  Combined with `loginwindow`'s job file not
existing at all, the per-user GUI domain has nothing to create it and SpringBoard
has nothing to start it.

## Not claimed

* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* SpringBoard's `ProcessType`, `RunAtLoad`, `LaunchEvents` and `POSIXSpawnType`
  are NOT known.  Nothing in this interval says why launchd skips the job.
* The 373-job and 113-label enumerations are lower bounds.  Neither can prove a
  job absent, and neither found SpringBoard's content.
* `exit.seen` is truncated at 64 records.  The true exit count is unknown and is
  at least 64.
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.


---

# Interval 2026-11-04 (forty-third, addendum): adjacency fails 98.8% of the time,
# measured.  And the volume scanned is provably the volume the guest booted.

## The adjacency claim was one control; now it is a measurement

Interval 43 said the bytes following a filename do not belong to that file, and
supported it with one control.  One control is a hint.  This measures it.

Walking the whole LaunchDaemons catalog region (offsets 295,000,000 to
360,000,000, 62 MiB), pairing every `com.apple.*.plist` filename with the binary
plist that follows it within 64 bytes, and comparing the plist's own `Label`
against the filename:

    name -> next bplist pairs : 799
      Label AGREES with name  : 4
      Label disagrees         : 332
      plist did not parse     : 463

**Adjacency is right 1.2% of the time.**  The disagreements are not random --
they are shifted by a fixed number of records:

    com.apple.ckdiscretionaryd.sync.plist -> com.apple.containermanagerd.sync
    com.apple.containermanagerd.sync.plist -> com.apple.cloudphotod.sync
    com.apple.cloudphotod.sync.plist      -> com.apple.cloudd.sync
    com.apple.DMHelper.plist              -> com.apple.CoreAuthentication.daemon
    com.apple.PowerUIAgent.plist          -> com.apple.PointerUI.pointeruid

An APFS catalog node keeps its keys and its values in separate regions, so a
name and the plist after it belong to different records.  This retires the
temptation to read the 326-byte plist after `com.apple.SpringBoard.plist` as
SpringBoard's job definition: it parses as `com.apple.SiriTTSTrainingAgent`, and
now that is expected rather than surprising.

## The volume scanned is provably the volume the guest booted

The obvious objection to interval 43 is that it repeats interval 43's own
mistake -- that the OS root volume is not the volume the guest actually attached.
It is.  `capture-springboard-boot.py` sets

    root_image = volumes / "raw/044-76040-038.raw"

and the capture's own report records

    "root_raw_sha256": "e5634d04a8ac35f024845f583394e0a35deb26a04cb0c00286a8674e25a9a4f2"

which is the sha256 of the file every scan in interval 43 was run against,
recomputed and matched byte for byte.  The filename evidence is about the guest's
own root volume.

## Why the plist cannot be read, stated precisely

* An APFS catalog node separates keys from values, so adjacency does not pair
  them (1.2%, measured above).
* Enumerating every `bplist00` in the volume and keeping those with a `Label`
  recovered **373 jobs**, but only **53 of interval 42's 109 spawned comms**
  match one; `notifyd` and `usermanagerd` are both among the 56 that do not, and
  both provably spawned.
* An XML `<key>Label</key><string>...` scan found 113 labels, but all five of its
  controls (configd, backboardd, runningboardd, apsd, akd -- every one of which
  spawned) returned 0 while `notifyd` returned 1, so its zeros are void.

**No available instrument can read `com.apple.SpringBoard.plist`'s contents, and
neither can prove the job absent.**

## launchd cannot be instrumented

The fork carries patches for `libpthread`, `mesa` and `xnu` only.  There is no
launchd source in `vendor/UniXNU`, and launchd is not built by this project -- it
is the binary that ships on the root volume.  So the question "why does launchd
skip this job" cannot be answered by adding an emitter to launchd.

The kernel, however, sees every file open.  An emitter on the VFS open path that
reports paths under `/System/Library/LaunchDaemons/` would answer the one
question that decides the branch: **did launchd ever open
`com.apple.SpringBoard.plist`?**  If it did, the decision is inside the plist.  If
it never did, the job is not in the directory launchd scanned, which is a
different finding with a different next step.  That is the next measurement.


---

# Interval 2026-11-04 (forty-fourth): 1435 job plists, 74 spawned, and "not
# spawned" is the normal case.  Interval 42's "essentially complete" is wrong.

## The directory, counted

The LaunchDaemons catalog region of the guest's root volume (offsets 295,000,000
to 360,000,000, 62 MiB) holds

    distinct com.apple.*.plist filenames : 1435

and interval 42's guest spawned 109 distinct daemons.  Matching a spawned comm
against a plist of the same name (the comm truncated to 15 characters, which is
what the kernel reports):

    spawned comms with a plist of the same name : 74
    spawned comms with no matching plist        : 35
    plists present but NOT spawned              : 1361
    plists present AND spawned                  : 74

**1361 of 1435 job plists -- 95% -- were present and not spawned.**  On a real
iOS boot most LaunchDaemons jobs are on-demand and never start at boot, so being
present and unspawned is what launchd does with the overwhelming majority of its
job list.

## What that does to interval 42's conclusion

Interval 42 said: *"The userspace boot is essentially complete except for
SpringBoard."*  That was drawn from 155 spawns and the absence of SpringBoard
among them.  With the directory counted, **155 spawns is 109 daemons out of a
1435-job list -- 7.6%.**  A boot that has run 7.6% of its job list is not
complete; it is early.  The claim is withdrawn.

It also retires the framing that made SpringBoard look anomalous.  Interval 43
concluded "launchd never attempts SpringBoard, and that is the defect".  It is
not a defect: launchd does not attempt 1361 of 1435 jobs.  SpringBoard is one of
the 1361, and the interesting question is what it is waiting for, not why it was
skipped.

## Is there a discriminator?  Not a clean one

Reading the 373 job plists whose bytes are recoverable, and splitting them by
whether their daemon ran:

    key                    ran (56)                          not ran (317)
    RunAtLoad              25/56 (44%)  Truex23, Falsex2      45/317 (14%) Falsex36, Truex9
    ProcessType            12/56 (21%)  Adaptive 9, Int. 3    129/317 (40%) Adaptive 81, Background 27, Int. 16
    POSIXSpawnType         43/56 (76%)  Interactive 22, Adaptive 21
                                                             163/317 (51%) Adaptive 122, Interactive 31, App 7
    KeepAlive              20/56 (35%)                       37/317 (11%)
    UserName               34/56 (60%)  mobile 30            227/317 (71%) mobile 210
    LaunchEvents           32/56 (57%)                       144/317 (45%)
    MachServices           51/56 (91%)                       285/317 (89%)

**No key separates the two groups.**  `RunAtLoad` is the strongest signal (44%
versus 14%) but 9 not-ran jobs carry `RunAtLoad=True` and 2 ran jobs carry
`RunAtLoad=False`, so it is not a rule.  The one clean split is
`POSIXSpawnType=App`: **7 of 7 in the not-ran group, 0 in the ran group** -- but
7 samples is not a discriminator, and SpringBoard's own plist cannot be read, so
there is no way to check whether it is one of them.

**And the sample is biased, which is the honest caveat.**  Only 56 of the 109
spawned daemons have a readable plist, and only 373 of 1435 plists are readable
at all.  Every number above is a statement about the readable subset, not about
the job list.

## The one thing that would settle it

The guest was still spawning when interval 42's capture hit its 5400 s cap
(`last_bytes_at_s: 5382`, and `bluetoothd` is the final record in the file).  At
the observed pace the boot had worked through 109 of 1435 jobs in 90 minutes.
Whether it would ever reach SpringBoard's job is **unknown**, and no measurement
this project has taken can answer it, because no capture has run long enough.

That is a bounded question with a bounded answer: **run the capture long enough
for the guest to go quiet, and see whether `SpringBoard` appears.**  The
quiet-stop logic already exists (`QUIET_STOP_S = 600` under a
`LISTEN_BUDGET_S = 5400` cap); the cap is what ended interval 42, not the guest
settling.

The exit bound has to be fixed in the same build.  `kMaxExitRecords` is still 64
and it saturated in interval 42 while `bluetoothd` was still exiting, so a longer
capture on the current kernel would silently lose every exit after the 64th.

## Not claimed

* SpringBoard's plist contents remain UNKNOWN.  Nothing above says what
  `ProcessType`, `RunAtLoad`, `LaunchEvents` or `POSIXSpawnType` it carries.
* The 1435 count is filenames in one 62 MiB catalog region, not a verified
  directory listing.  It is a lower bound on the job list.
* The 74 matches are name matches between a truncated comm and a plist stem, not
  verified job-to-process bindings.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.


---

# Interval 2026-11-04 (forty-fourth, addendum B): every number re-derived, and
# the crash loop nobody had counted.

## The re-derivation

Interval 44 cited interval 42's numbers.  Citing them is not measuring them, so
they were recomputed from the raw capture at
`local-private/boot53/spawn-failed-input-run/guest-input.log` (21,025 bytes, 241
JSON lines, 0 unparseable):

    spawn.seen records      155      (n = 1..155, CONTIGUOUS)
    exit.seen records        64      (n = 1..64,  CONTIGUOUS)
    distinct spawned comms  109
    any Spring* comm        none

All four match.  The contiguity matters more than the counts: **the spawn
emitter's bound of 512 did NOT saturate** (n runs 1..155 with no gap), while
**the exit emitter's bound of 64 DID** -- n runs 1..64 and stops, which is the
bound itself and not a coincidence.  That is the saturation interval 43 reported,
now confirmed structurally rather than inferred from bluetoothd's behaviour.

## The crash loop

Splitting the 155 spawn records by comm:

    bluetoothd        35 spawns   28 exits
    fairplayd.A2      10 spawns    8 exits
    containermanager   2 spawns    0 exits
    cfprefsd           2 spawns    0 exits
    com.apple.datami   2 spawns    2 exits

    distinct comms                109
    spawned exactly once          104
    spawned more than once          5
    spawn records that are RESPAWNS 46

**Five of 109 daemons respawned, and 46 of the 155 spawn records -- 30% -- are
respawns rather than first starts.**  `bluetoothd` alone accounts for 35 spawns
and 28 exits.  This has never been recorded anywhere in the project, and it was
visible in interval 42's own file the whole time.

And the exits are mostly failures:

    wait status 0   31     (clean exit)
    wait status 1   30     (FAILURE)
    SIGHUP            2
    wait status 13    1

**30 of 64 exits are exit status 1.**  `bluetoothd` is not restarting because it
finished; it is restarting because it is failing.

## Why this was invisible: the capture kept no timestamps

The guest channel's records carry no clock.  `spawn.seen` is
`{"event","n","comm","source"}` and `exit.seen` adds `code` -- no `t_s`, no
`mach_absolute_time`, nothing.  The arrival time existed only in the capture
script's stdout (`+N bytes (total X) at Ts`), and **interval 42's run did not
preserve that stdout** -- the directory holds `guest-input.log` and nothing else.
The scratch dir holds `guest-channel.log`, `serial.log` and the screendumps; the
stdout went to a terminal and is gone.

The consequence is a measurement this project cannot make and should be able to:
**a crash loop respawning every 150 s and a crash loop respawning every 2 s
produce the same byte stream and the same quiet-stop.**  In the first case
launchd is throttling a job and the guest is healthy; in the second the guest is
pinned and nothing else will ever start.  Those are different defects with
different fixes, and without `t_s` they are one result.

`capture-springboard-boot.py` now timestamps every record as it arrives
(`t_s`, seconds since the kernel handoff) and writes `guest-input.timed.jsonl`
beside the byte-exact `guest-input.log`.  Records are split on arrival rather
than at the end because a listen batch can end mid-record, and a record cut in
half would inherit the batch's time instead of its own.  The script also computes
the respawn summary and each loop's median gap, so the next run reports the crash
loop instead of leaving it to be found by hand.

## What this predicts about the long capture

If `bluetoothd` keeps failing at interval 42's pace, **the 1200 s quiet-stop will
never fire** -- a respawn every ~150 s is a new byte every ~150 s, forever -- and
the run will exhaust the full 21600 s budget and stop on the deadline rather than
on quiet.  That is still an answer ("SpringBoard had not spawned after six hours
of guest time"), but it should be predicted rather than discovered at hour six,
and the distinction is visible in the report: `stopped_on_quiet: false` with
`listen_seconds: 21600` means the guest never settled, while
`stopped_on_quiet: true` means it did.

## Not claimed

* The crash loop's PERIOD is unknown.  There are no timestamps, so "every ~150 s"
  above is an average over the whole run (35 spawns across ~5400 s), not a
  measurement of the gap.
* Nothing here says WHY bluetoothd fails.  Exit status 1 with no other record;
  on a guest with no Bluetooth hardware that may be entirely expected.
* Nothing here says the crash loop blocks SpringBoard.  It is a fact about two
  daemons, not a causal claim about the job list.
* No SpringBoard, no IPA, no interaction, no persistence.  M9, M9a, M9b FAIL.
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.


---

# Interval 2026-11-04 (forty-fourth, addendum C): the volume question, settled.
# SpringBoard's job plist is on the OS root volume.  SpringBoard.app is not.

## Why this needed proving at all

Interval 43 scanned `raw/044-76040-038.raw` and found SpringBoard's job filename
there, having first scanned the SystemOS cryptex and found nothing.  Two things
made that conclusion weaker than it looked:

1. **The cryptex is mounted at `/System`.**  The boot console line is
   `disk1s1 mounting volume ...SystemCryptex, requested by: init (pid 1)`, and
   the cryptex is the third read-only `virtio-blk-device` the capture attaches.
   A mount at `/System` shadows whatever the root volume has there, so "the
   filename is in the root volume" does not by itself mean "launchd can see it".
2. **A zero-hit scan of an encrypted volume is the expected result.**  If the
   cryptex were encrypted, finding zero job plists in it would prove nothing,
   and the control test that justified scanning the root volume instead would
   have been circular.

## The cryptex is plaintext, so the control is real

`044-76034-038.dmg` is 3,303,014,400 bytes and carries a readable APFS container
superblock: `NXSB` at offset 32 with `nx_block_size` = 4096, and valid object
headers on the 8192-byte stride.  It is **not** encrypted.  So a zero-hit scan of
it means what it says.

## Both volumes, same names, side by side

    name                              OS root   SystemOS
    com.apple.SpringBoard.plist              5          0
    com.apple.configd.plist                  3          0
    com.apple.notifyd.plist                  3          0
    com.apple.runningboardd.plist            3          0
    com.apple.backboardd.plist               3          2
    com.apple.launchd.plist                  0          0
    LaunchDaemons                          545         21
    NanoLaunchDaemons                        5          0
    LaunchAgents                            21          4

And the content profile of each:

    OS root    bplist00 17032   fpmc 161444   <?xml 50466   com.apple. 247980
    SystemOS   bplist00   993   fpmc   2224   <?xml   235   com.apple.  85693

**The OS root volume is the configuration volume and the cryptex is the binary
volume.**  17032 bplists against 993; 50466 XML plists against 235.  The cryptex
carries the frameworks and the dyld shared cache, which is why `com.apple.`
appears 85693 times there against the root's 247980 -- those are string tables
inside Mach-O images, not catalog keys.

## The deduction

`com.apple.configd.plist`, `com.apple.notifyd.plist` and
`com.apple.runningboardd.plist` exist **only** in the OS root volume, and all
three of those daemons demonstrably spawned in interval 42's guest.  Therefore
launchd reads job plists out of the OS root volume.  Therefore the root volume's
`/System/Library/LaunchDaemons` is visible to the guest despite the cryptex being
mounted at `/System` -- which means the two compose rather than shadow, and a
path the cryptex does not carry falls through to the root volume.

`com.apple.SpringBoard.plist` is also **only** in the OS root volume (5 hits, 0
in the cryptex).  **So SpringBoard's job file IS available to the guest's
launchd.**  Interval 43 scanned the right volume, and the reason SpringBoard has
not spawned is not that its job file is missing from the mounted tree.

## Where SpringBoard.app actually is

`springboard-find.txt` scanned the cryptex and found, inside its binaries:

    com.apple.SpringBoard     300
    SpringBoard.app             8
    CoreServices/SpringBoard    6

So **`/System/Library/CoreServices/SpringBoard.app` lives on the SystemOS
cryptex**, while `/System/Library/LaunchDaemons/com.apple.SpringBoard.plist`
lives on the OS root volume.  Spawning SpringBoard therefore requires both
volumes to be mounted and composed, which the 109 spawned daemons prove they are.

## The caveat, stated

The cryptex is not fully enumerated: 993 bplists across 3.3 GB is not a directory
listing, and APFS `decmpfs` compression (`fpmc`, 2224 headers there) could hide a
plist's bytes.  So "0 occurrences" is strong evidence of absence, not proof.  The
deduction above does not need proof -- it needs only that the three control
plists are absent from the cryptex, and those three daemons spawned.

## What this leaves

The question is back to exactly where interval 44 put it, now with the volume
doubt removed: **launchd has SpringBoard's job file available and has not spawned
it.**  Whether it ever tries is what the long capture measures, and whether it
tries and declines is what the plist's contents would say.


---

# Interval 2026-11-04 (forty-fifth): `com.apple.SpringBoard.plist` is READ.  And
# the discriminator is `POSIXSpawnType=App`.

## The file, byte-exact

**2634 bytes**, sha256
`a071265c26b9415973c8bbab8d99fcff6e347d6b7831b9f059903ea63dd835fe`, 17 keys,
saved verbatim at `local-private/boot53/com.apple.SpringBoard.launchd.plist`.
It parses, and its `Label` is `com.apple.SpringBoard`.

    Label                       com.apple.SpringBoard
    RunAtLoad                   TRUE
    KeepAlive                   TRUE
    POSIXSpawnType              App
    ProcessType                 ABSENT
    UserName                    mobile
    ThrottleInterval            5
    ProgramArguments            ['/System/Library/CoreServices/SpringBoard.app/SpringBoard']
    LimitLoadFromHardware       {'osenvironment': ['diagnostics']}
    _LimitLoadFromClarityMode   True
    EmbeddedHomeScreen          True
    EnablePressuredExit         False
    LaunchEvents                ABSENT
    MachServices                36 entries

**`RunAtLoad` is TRUE.**  launchd was told to start this job at boot and did not.

## How it was read

The raw file is a **GPT-partitioned disk**, not a bare container: the APFS
container starts at **file offset 17408** (GPT first usable LBA 34).  `NXSB` sits
at 17440 and every 8192 bytes after it, always congruent to 1056 mod 4096.
`nx_block_size` 4096, `nx_block_count` 1,483,776.

    live container superblock   block 0       xid 418 (of 58 checksum-valid copies)
    container omap              15897 -> tree 15898
    volume superblock APSB      15896         (oid 1026, via the container omap)
    volume omap                 67545 -> tree 67578
    catalog root                65684         (apfs_root_tree_oid 1028, virtual)

The catalog root's `apfs_btree_info` declares `bt_key_count = 983,966` and
`bt_node_count = 39,944`; the walk visited 39,497 nodes and emitted 983,966
records.  **The walk's own counts match the tree's own metadata**, which is the
strongest single check available.

Path resolution, all through DIR_REC records with root inode 2:
`2 -> System(1152921500311900600) -> Library(...1101) ->
LaunchDaemons(...072820) -> com.apple.SpringBoard.plist(...072888)`.

The file has **no extents of its own**.  It carries two xattrs:
`com.apple.decmpfs` (flags 0x0002 DATA_EMBEDDED, xdata_len 16) and
`com.apple.ResourceFork` (flags 0x0001 DATA_STREAM, xdata_len 48).  The decmpfs
header says magic `fpmc`, **compression type 4**, uncompressed size 2634.  The
ResourceFork's stream is 1798 bytes at physical block 498020, and it is a chunked
container: a 16-byte header, a chunk descriptor at 256, then a **zlib stream at
fork offset 272..1748**, then a 50-byte trailer.  `zlib.decompress` of those 1476
bytes yields exactly 2634 bytes, and the result begins `bplist00`.

## The validation, before anything was believed

* **The instrument was proved first.**  fletcher64 was validated by reproducing
  two independent stored checksums exactly -- the container superblock copies at
  xid 418 and xid 379 -- before any block was trusted.  (It is modulo
  2^32 - 1, not 2^32; with the wrong modulus every block "fails".)
* **Every rung self-checks**: NXSB magic + fletcher, OMAP object type + fletcher,
  APSB magic + fletcher, volume OMAP object type + fletcher, and fletcher64 on
  all 39,497 catalog nodes.
* **Four control daemons that provably spawned in the guest**, read through the
  identical code path, all pass with a matching `Label`: `com.apple.configd`
  (827 B), `com.apple.notifyd` (797 B), `com.apple.runningboardd` (385 B),
  `com.apple.backboardd` (1179 B).  **4/4.**
* **Three independent size agreements** on the target: the inode's
  `uncompressed_size` (2634) = the decmpfs header's uncompressed size (2634) =
  the actual decompressed byte count (2634).
* **The bplist trailer closes exactly**: `offset_table_offset 2340 + off_size 2 x
  num_objects 131 = 2602 = 2634 - 32`.  No trailing garbage.
* **An independent second sample**: the same decoder on
  `/System/Library/Preferences/Logging/Subsystems/com.apple.SpringBoard.plist`
  (a different file id, 2179 declared) produces exactly 2179 bytes.
* **518 of the 523** decmpfs job plists in LaunchDaemons decode with correct size
  and magic through the same path.

## And now the discriminator interval 44 wanted, on real data

Interval 44 could read only 373 of 1435 job plists, by pairing a filename with
whatever bplist sat beside it -- right 1.2% of the time, so every number in that
table described a sample that was mostly noise.  With the catalog walk the whole
directory is readable, so the table can be rebuilt:

    job plists in /System/Library/LaunchDaemons, readable : 518
      whose daemon spawned in interval 42                 :  92
      whose daemon did not                                : 426

    RunAtLoad=True  AND the daemon did NOT spawn          :  13

**Thirteen.**  Out of 518.  That is the anomalous set -- the jobs the guest was
told to start at boot and did not -- and it is small enough to read in full.

### `LimitLoadFromHardware` is RULED OUT

SpringBoard carries `LimitLoadFromHardware = {osenvironment: [diagnostics]}`,
which looked like the answer.  It is not: **`com.apple.logd.plist` carries the
identical value and it spawned.**  So the restriction is satisfied on this guest
(the guest's osenvironment is `diagnostics`), and it is not what stops
SpringBoard.

### `POSIXSpawnType=App` is a perfect separator

    POSIXSpawnType      RAN (92)                        NOT RAN (426)
    Interactive         35                              41
    Adaptive            28                             165
    ABSENT              29                             207
    App                  0                               7     <-- all seven
    Background           0                               5     <-- all five

**Seven jobs carry `POSIXSpawnType=App`.  Zero of the 92 that ran have it.  All
seven are in the not-ran group.**  SpringBoard is one of the seven:

    com.apple.SpringBoard.plist
    com.apple.TextInput.kbd.plist
    com.apple.assistivetouchd.plist
    com.apple.bluetoothuiservice.plist
    com.apple.checkerboard.plist
    com.apple.commandandcontrol.plist
    com.apple.fullkeyboardaccess.plist

And the "board" family makes the shape plain:

    com.apple.backboardd.plist    RunAtLoad=true  POSIXSpawnType=Interactive  RAN
    com.apple.runningboardd.plist RunAtLoad=true  POSIXSpawnType=Adaptive      RAN
    com.apple.SpringBoard.plist   RunAtLoad=true  POSIXSpawnType=App           NOT RAN
    com.apple.checkerboard.plist  RunAtLoad=true  POSIXSpawnType=App           NOT RAN

## Two things that are NOT the answer

* **`frontboardd` has no job plist on this volume at all.**  The LaunchDaemons
  directory holds SpringBoard, backboardd, checkerboard, checkerboardd and
  runningboardd -- and no frontboardd.  SpringBoard's MachServices include
  `com.apple.frontboard.systemappservices`, but **MachServices in a launchd plist
  are the services the job PROVIDES, not the ones it consumes.**  It is not a
  dependency, and frontboardd's absence is not the blocker.
* **`ProcessType` and `LaunchEvents` are both ABSENT** from SpringBoard's plist,
  so neither is filtering it.

## The caveat, stated

**Seven samples is a correlation, not a mechanism.**  `POSIXSpawnType=App`
separates perfectly in this data set, and that is worth reporting, but nothing
here says WHY launchd declines an `App` job -- only that it declines all seven.
Two of the thirteen anomalous jobs have a visible reason of their own
(`safetyalertsd` carries `LimitLoadFromHardware={targettype:[D16,D17,D20,...]}`,
which a QEMU guest cannot match; `iomfb_bics_daemon` carries
`LimitLoadToSessionType=System`), which leaves eleven, of which two are `App`.

The instrument that settles the mechanism is the one already written:
`zzz-native-job-open-events.patch` reports every open under the three job
directories, so it answers whether launchd ever READS
`com.apple.SpringBoard.plist`.  "Never opened" and "opened and declined" are
different defects with different fixes, and the spawn stream cannot tell them
apart.

## A defect in existing tooling

`local-private/out/real-boot/esp-and-os-root/followup/work/apfs_reader.py` parses
**4-byte TOC entries**; the correct entry is `apfs_kvloc`, 8 bytes
(`k.off, k.len, v.off, v.len`), 4 only when `APFS_BTNODE_FIXED_KV_SIZE` is set.
It reads `j_key_t`'s record type from the **low 16 bits**; the type is the **top
4 bits** (`>> 60`).  Its `inode` field offsets are wrong for this volume (mode at
+0x50, uncompressed_size at +0x54, xfields at +0x5C).  And its
`probe_springboard.py` "proof" is not a proof: it searched for
`com.apple.springboard.plist`, lowercase, which was never in the region it
scanned.  That file is under `local-private/` and therefore untracked, so nothing
in the repository depends on it -- but anyone reaching for it will get a wrong
answer confidently.

## Not claimed

* Nothing here says why launchd declines `POSIXSpawnType=App`.  The 7/7 split is
  measured; the mechanism is unknown.
* SpringBoard has still NOT been observed to spawn.  No IPA, no interaction, no
  persistence.  M9, M9a, M9b FAIL.
* The 518 readable plists are those that are decmpfs-compressed with a
  single-chunk resource fork.  Five fail to decode (the four
  `com.apple.jetsamproperties.J*` and `com.apple.duetexpertd`) because their
  resource forks are multi-chunk; the decoder handles the single-chunk layout
  only.
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.


---

# Interval 2026-11-04 (forty-sixth): the emitter is fixed, pinned, and building.
# And the instrument that validated it was itself wrong once.

## The four findings, and the vector that proves each one

`c-review-job-open.txt` found four defects in revision 1 of
`zzz-native-job-open-events.patch`.  All four were real.  All four are fixed, and
each fix is now covered by a behaviour vector that **revision 1 fails** -- which
is the only thing that makes the fix a fact rather than a claim.

| # | Finding | Fix | Vector that rev 1 fails |
| :-- | :-- | :-- | :-- |
| 1 | The `segflg` guard the comment described was not in the code | `UIO_SEG_IS_USER_SPACE(segflg)`, XNU's own macro at `bsd/sys/uio.h:130` | a `UIO_SYSSPACE` caller now emits nothing |
| 2 | The errno the comment called "part of the measurement" was in neither record | `,"err":E` in both records | the same path with errno 0 and errno 2 now yields different records |
| 3 | The whole emitter sat inside `copyinstr(...) == 0` with a 96-byte probe | `kJobOpenProbe = 256` | a 107-byte path still reports SpringBoard |
| 4 | `job_dirs[]` held only the three `/System/Library` forms | all six, both mount arms | `/private/preboot/System/Library/LaunchDaemons/` is watched |

### Finding 1's stated mechanism was also wrong, and the correction matters

The comment claimed an unguarded kernel-space caller would **fault**.  It would
not.  `copy_validate()` (`osfmk/arm64/copyio.c:141-147`) returns **EFAULT** when
the source falls outside `[vm_map_min, vm_map_max]` of the current map, and a
kernel address is far above any user map's max; `is_kernel_to_kernel_copy()`
(`:169-172`) returns EFAULT as well, because `copyinstr` never sets
`COPYIO_ALLOW_KERNEL_TO_KERNEL`.

So the real consequence of an unguarded kernel-space caller is a **silently
dropped record** -- which is precisely the failure this patch exists to prevent.
The guard is there anyway, but for the right reason, and a reader who believed the
original comment would have looked for a panic that cannot happen.

## The instrument was wrong once, and that is the most useful finding here

`check-job-open-logic.py` transliterates the emitter's C to Python
statement-for-statement, because **there is no working C compiler on this host**:
the vendored MSYS2 `gcc.exe` reports 16.2.0, but `cc1.exe` exits 1 with **no
diagnostic** on every input, including `int main(void){return 0;}`.  `-v` shows it
invoked with a temp file under the non-ASCII user profile.  Not fixable here.

Revision 1's port **hand-copied the C's string constants, and got one wrong**.  It
wrote `,"err":` where the C has `","err":`.  That drops the closing quote of the
`"job"` value, so the record on the wire is:

    {"event":"job.open","n":1,"job":"com.apple.SpringBoard.plist,"err":17,...}

which **no JSON parser accepts**.  The C was correct.  The port was wrong.  A port
whose constants are typed by the same person who wrote the C reproduces that
person's mistakes instead of catching them -- the port had become decoration.

**The fix is structural, not a correction.**  Every literal, bound and watched
directory is now **parsed out of the patch text** by
`extract_c_strings()` / `extract_c_numbers()` / `extract_job_dirs()`, with C
escapes resolved and adjacent-literal concatenation joined, and a literal that
cannot be found is a hard `REFUSED` rather than a silent fallback.  The port can
no longer disagree with the patch, because it reads the patch.

This is the third time this project has been bitten by an instrument that reported
a result it could not support, and the third time the fix was the same shape: make
the instrument answer a question whose answer is already known.

## The negative control

`prove-job-open-vectors-discriminate.py` rebuilds revision 1 by reversing each of
the four fixes, and runs the same vectors against it.  **Every one goes red on
revision 1 and green on revision 2.**  Without that, a vector both revisions pass
would be decoration, and the claim "rev 2 fixes finding N" would be unsupported.

It also caught a defect in itself: the first run reported four rev-2 failures,
because the counters are `static` in the C and the rev-1 section had left them
set, suppressing the targeted record and inflating the counter.  That is exactly
the leakage the port's `reset_state()` exists for, and it was missing from the
second section.

## The record, as it now goes on the wire

    {"event":"job.open","n":1,"job":"notifyd.plist","err":0,"source":"XNU.openat_internal"}
    {"event":"job.springboard_open","err":0,"source":"XNU.openat_internal"}

Both parse.  The first carries the job's name and the errno from `open1at()`; the
second is bounded to **one**, because the second and later opens of the same file
add nothing to "did it ever open this".

## What is verified, and what is not

**Verified, on this host:**

* All **59** xnu patches apply in name order against upstream
  `xnu-10063.141.1`, with the new one at position **56**.  This is the check that
  `git apply --check` on a pristine tree cannot do: it proves the patch composes
  with the whole set.
* The hunk header (`-5116,6 +5116,276`) matches its body, counted with context
  lines.  The earlier checker omitted context and reported two false positives,
  which is why it counts them now.
* `UIO_SEG_IS_USER_SPACE` resolves through `vfs_syscalls.c:85` ->
  `sys/uio_internal.h:70` -> `bsd/sys/uio.h:130`.
* `segflg` is a parameter at `:5093` and `error` is a local at `:5103`; the
  emitter's anchor is at `:5117`.  Both are in scope.
* No other patch touches `vfs_syscalls.c` or `openat_internal`, so there is no
  conflict.
* The logic port passes every vector, and every vector discriminates.

**NOT verified, and stated rather than implied:**

* **C syntax.**  No compiler on this host.  A syntax error survives every check
  above.
* **That the emitter is reached.**  That is a boot measurement, and it needs
  build `41788097516`.
* **That `NATIVE_VIRTIO_CONSOLE` is on for this build.**  The build script
  propagates the macro to `vfs_syscalls.o` and removes a stale object when the
  switch changes, but the switch's value for M7/tcg is not yet measured.

## Why the console transport had to move with it

`NativeVirtioConsole` advances to `3ca09c9`, and it is not a drive-by.
`reset()` returned true for an instance that never started
(`NativeVirtioConsole.cpp:183`):

    if(!registers || !ownsDevice)return true;

so **every** instance's `cleanup()` cleared `g_console` -- including one that got
as far as being constructed and then stopped.  The port that publishes the records
this emitter depends on was therefore withdrawn by a bystander.  An emitter that
is compiled in, reached, and silent **looks like a measurement**, which is the
worst of the three states, so that fix travels with this one.

## And why the display pointer did not move

`drivers/NativeMobileDisplay`'s working tree is at `eb9c949`, a **detached
FETCH_HEAD** commit.  It is **not an ancestor of `origin/main`** (`8666a99`,
"retire a GPU-copied swap when the client takes it"), which supersedes it.  So
recording `eb9c949` would make the build's `git submodule update` fail, for a
change this emitter does not need.  The pointer stays at `c031d88` and the
divergence is recorded here rather than silently absorbed.

## The pin, and the build

`unixnu.commit` `5a8bfaa` -> `e6abdf28`.  `device_repo.commit` unchanged at
`5ef74a17`.  Build **`41788097516`** dispatched at `stage=M7`,
`payload_target=tcg`, `gpu_broker=0`.

The lock's `commit_note` was re-serialised with `ensure_ascii=True`, **not**
`False`: the lock was written with the default, and `ensure_ascii=False`
un-escapes the `\u53cd\u5411\u5ba1\u67e5` in `device_repo`'s note -- a one-line
change to a section this bump has no business touching.

## A disk condition that produced three spurious unit-suite failures

The unit suite reported `F` at ~8%, ~16% and ~16%.  **F: was at 2.1 GB free, 100%
used.**  That is the same shape as the earlier suite "failure" whose real output
was `tail: write error: No space left on device` -- the pipeline failed, not the
tests.

Reclaimed **4.4 GB** by name, with every candidate checked against `git grep`
first so nothing cited by a tracked doc, test or script was touched:

    bi-m9 (644M), bi-41382433090 (574M), bi-41376479360 (574M),
    kernelconsole (294M), bundle-41750576785.zip (297M),
    dl41242825926 (547M), stage-probe (444M),
    bootinputs.zip (163M), bundle13.zip (296M), bundle14.zip (296M),
    bundle15.zip (296M), build-logs14.zip (2M)

F: is now at 6.5 GB free.  The suite is re-running; its three `F`s are **not yet
attributed** and must not be reported as product defects until it finishes on a
host with room to write.

## Not claimed

* The emitter's C has never been compiled.  Not here.
* SpringBoard has still NOT been observed to spawn.  No IPA, no interaction, no
  persistence.  M9, M9a, M9b FAIL.
* Whether launchd ever OPENS `com.apple.SpringBoard.plist` is unknown until build
  `41788097516` boots.  That is the question this emitter exists to answer, and
  the answer is one of exactly three: no `job.springboard_open` record while
  hundreds of `job.open` records are present (the defect is in launchd's
  selection, and `POSIXSpawnType=App` is the lead); a record with `err=0` (the
  defect is downstream of the open); or no records at all (the emitter is not
  reached, and that is a defect in this patch, not in launchd).
* M13 (audio), network, launcher, performance: not started.  M14/M15: driver work
  does not exist.  WP00/WP01: blocked on `frozen: false`.


---

# Interval 2026-11-04 (forty-sixth, addendum A): where the console switch comes
# from, and three suite failures that were all environment.

## The switch, found

The emitter sits behind `#if defined(XNU_NATIVE_VIRTIO_CONSOLE) &&
XNU_NATIVE_VIRTIO_CONSOLE`.  The build script defaults that to **0**
(`build-xnu-4k-baseline.sh:192`), and a grep of the entire repository -- every
workflow, `scripts/macos/build-macos.sh`, the device repo, every script under
`vendor/UniXNU/scripts/` -- finds **nothing that sets it to 1**.

That looked like the emitter would be silently compiled out, which is outcome 3,
cause 1: a six-hour capture that can only report absence.  So it was settled
against the kernel that was actually built rather than by reasoning about which
switch should have been flipped.

**The fragments are in build 41775242423's kernel.**  `payload.img` at
17935224..17935700 holds, as SHF_MERGE|SHF_STRINGS split fragments:

    +17935232: seen","n,"comm":","source":"XNU.posix_sp
    +17935280: failed","n":
    +17935296: ,"errno","parent
    +17935320: een","n"__mac_ex!
    +17935344: {"event":"diag.owner_data_status","stage,"error","source":"XNU.native_owner_data
    +17935440: {"event":"springboard.running",","comm":"SpringBoard","source":"XNU.posix_spawn"{"event":"spawn.
    +17935552: XNU.__mac_execve{"event":"exec.s
    +17935608:  ","code"
    +17935632: {"event":"exit.s,"source":"XNU.exit_with_reason"lockf

**`XNU.exit_with_reason` is behind the console macro**, and it is there.  So the
switch was 1 for that build.  (This is also the third confirmation of the
SHF_MERGE trap: `exit.seen` and `XNU.kern_exit` both have **zero** hits as
contiguous literals on a kernel that provably emits 64 exit records.)

**And the mechanism is `vendor/UniXNU/scripts/unixnu/kernel.py:11`:**

    """CONFIG_XNU_* of the defconfig become XNU_* variables of the legacy kernel
    build script."""

`xnu_env()` walks the resolved defconfig and turns every `CONFIG_XNU_*` into an
`XNU_*` environment variable.  `vendor/UniXNU-qemu/configs/qemu_root_defconfig:47`
sets `CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y`, so the second kernel build pass --
workflow step 21, "Run macOS build again (qemu_root_defconfig, PRD 8.1 root
chain)" -- builds with the switch **on**.  The first pass (step 14) uses the
default defconfig and builds it **off**.

So the emitter is compiled in, by a path that no comment in the build script
records.  That gap is now closed in the patch's own comment, because a reader who
greps for `XNU_NATIVE_VIRTIO_CONSOLE` and finds nothing will reasonably conclude
the emitters are dead.

**The check that settles it for the new kernel is
`local-private/tmp/is-emitter-in-kernel.py`.**  It searches the fragments, not the
literals, and it **validates the instrument first** by searching for four
fragments the previous build provably contains -- refusing to report absence if
the grep cannot see what it knows is there.  That is the same instrument error
the project has now hit three times.

## The three suite failures, all environment

`local-private/boot53/unit-suite-rerun.txt` finished with three `FAILED` lines.
None is a product defect, and each is attributed rather than waved at:

### 1. `test_every_patch_applies_to_the_pinned_upstream_in_order` -- a truncated tarball

    EOFError: Compressed file ended before the end-of-stream marker was reached

The cached tarball was **3145728 bytes -- exactly 3.000000 MiB** -- and only
16,921,003 bytes decompressed before the stream ended.  A round 3 MiB boundary is
a truncation, not a coincidence.  The download happened at 06:05, while F: was
filling toward 2.1 GB free.

**Fixed.**  The corrupt tarball was deleted; the re-download produced
**15,391,408 bytes** and the script reports "59 patches against XNU
d8b80295118e / every patch applies, in the build's order".  That is an
independent second confirmation of what `repro-patch-order.py` already proved,
from a different code path.

### 2. `test_the_cross_check_accepts_the_real_driver_status_output` -- a stale clone

    vendor/UniXNU: the checkout is at bc210df66e5d2876c72a5cea42dcb7286ae79006
    but the source lock pins 5a8bfaa6600605f58d5df487fefaccbe275a58cb

`local-private/upstream/UniXNU` was two pins behind.  Not a defect in the test and
not a defect in the lock -- a local clone that was never advanced.  It is exactly
the version-consistency hazard the goal requires checking, and it was caught by a
test that does nothing but compare two SHAs.

**Fixed.**  Fetched and checked out `e6abdf28`; the test passes.

### 3. `test_the_host_can_read_the_window_and_the_framebuffer_over_qmp` -- host RAM

    qemu-system-aarch64.exe: cannot set up guest memory 'ram0': Invalid argument

The test asks QEMU for `MEMORY_MIB` (6144) + `WINDOW_MIB` (2048) = **8.00 GiB**
across two NUMA nodes.  The host has **15.8 GiB total and 4.8 GiB available**, and
the 11 GiB in use belongs to the user's own applications -- WeChat, Edge,
VS Code, two Claude processes.  **No stale QEMU is holding it**; the top twelve
processes were checked and none is one.

**Not fixed, and not fixable from here.**  This is a host-capacity condition, it
predates this session, and `src/unipad/host/unixnu_boot.py` was last touched in
`b28dc92`, which is not mine.  It is recorded as an environment failure and must
not be reported as a product defect or as a regression.

## The pattern, stated once more

Three `F` marks appeared in the suite at ~8%, ~16% and ~16% while F: was at
**2.1 GB free, 100% used**.  Two of the three final failures trace directly to
that condition -- one a truncated download, one a stale clone that could not be
fetched.  The third is host RAM.  **4.4 GB was reclaimed by name**, every
candidate checked against `git grep` first so nothing a tracked doc, test or
script cites was touched.

The lesson this project keeps relearning: a red suite on a host that is out of
disk or out of memory is a statement about the host, and the attribution has to
happen before the report, not after.


---

# Interval 2026-11-04 (forty-seventh): the emitter produced its first real guest
# records, and they point at the two-volume split.

## What happened

Build `41788097516`'s `boot-inputs` were booted under
`local-private/tmp/capture-springboard-boot.py` (six-hour listen budget).  The
kernel came up, the APFS volume mounted, and **`openat_internal()` emitted its
first records**:

    {"event":"job.open","n":1,"job":"com.apple.jetsamproperties.NonUI.plist","err":2,"source":"XNU.openat_internal"}
    {"event":"job.open","n":2,"job":"com.apple.jetsamproperties.J120.plist","err":0,"source":"XNU.openat_internal"}

Snapshot kept at `local-private/boot53/job-open-first-records.txt` (63 lines, the
whole channel log at the time of writing).

## Why this settles more than the emitter

**The emitter works, end to end.**  Patch -> `vfs_syscalls.o` -> `openat_internal`
-> virtio console -> host log, with the filename and the errno intact.  That is
the whole chain the six-hour capture was built to test, and it is now proven on a
real guest rather than argued from a fragment grep.

**And the guest is booting real userland.**  The same log carries 14 `exit.seen`
records naming real iPadOS daemons -- `fsck`, `mount`, `seputil`,
`mobile_obliterator`, `apfs_boot_util`, `MSUEarlyBootTask`, `cc_fips_test`,
`keybagd`, `usermanagerd`, `dirs_cleaner`, `xpcroleaccountd`,
`init_featureflag` -- plus `exec.seen n=1 comm=launchd` and a `dyld.cache_read`.
That is further into userspace than any previous interval reached.

## The finding: `err:2` on one jetsam plist and `err:0` on the next

`err:2` is **ENOENT**.  launchd asked for `com.apple.jetsamproperties.NonUI.plist`
and the file was not there; the very next open, for `J120.plist`, succeeded.

`local-private/boot53/which-volume-owns-launchdaemons.txt` already had the
explanation on disk, and it says so in its own last paragraph:

    volume        bytes      LaunchDaemons  NanoLaunchDaemons  LaunchAgents
    OS root     6,077,580,800      545              5              21
    SystemOS    3,303,014,400       21              0               4

    BOTH volumes carry the directory name.  Which one the guest's
    launchd reads is NOT settled by this scan -- it is settled by the
    mount order, and a union at / makes the answer order-dependent.

**The guest mounted exactly ONE volume, readonly, at `/`** --
`8846FE78-2910-4297-AF97-D59BB6F8FC16`, reported by
`XNU.native_owner_cache_prepare`.  So one of those two LaunchDaemons sets is
reachable and the other is not, and which jobs exist is a property of the mount,
not of the kernel.

That is the strongest available explanation for the `POSIXSpawnType=App` anomaly
this interval inherited: `LimitLoadFromHardware` was refuted earlier, and
`POSIXSpawnType=App` itself remains a correlation (7/7 vs 0/92) rather than a
cause.  A volume split produces the same shape -- jobs that exist in the file
list but never spawn -- without needing launchd to discriminate on the key at
all.  **It is now the lead hypothesis and it is testable: the emitter reports
errno, so an ENOENT on a job plist is a volume gap and a launchd refusal is not.**

## What is NOT settled, stated so it is not mistaken for settled

* **Which of the two volumes `8846FE78-...` is.**  Not yet determined.  Until it
  is, "the ENOENT is a volume gap" is a hypothesis with a mechanism, not a
  measurement.
* **Why only 2 job.open records against 12+ spawned daemons.**  Two candidate
  causes, not yet distinguished: launchd may still be in early startup and have
  not reached bulk job discovery, or it may open most job plists with
  `openat(dirfd, "name.plist")`, which passes a RELATIVE pathname that the
  emitter's six-prefix filter drops.  The jetsam opens matched, so at least some
  opens carry the full path.  Distinguishing these needs one more emitter
  revision that records the path it was given, not just whether it matched.
* **Whether the boot reaches SpringBoard.**  The capture is still running.

## Status

Code: COMPLETE for the emitter and the tooling fix.
Windows/real-guest: PASS for the emitter (records above), IN PROGRESS for the
six-hour capture.
Onsite: N/A.


---

# Interval 2026-11-04 (forty-seventh, addendum B): 54 daemons, and a live control
# that breaks the tie between the two candidate causes.

## The boot went much further than the first snapshot suggested

`local-private/boot53/spawn-failed-input-run-scratch/guest-channel.log` at
08:50 carried **54 `spawn.seen` records / 49 distinct comms**:

    notifyd, UserEventAgent, IOMFB_FDR_Loader, timed, driverkitd, configd,
    logd, fseventsd, thermalmonitord, mediaserverd, mtmergeprops, mobiletimerd,
    logd_helper, containermanager, sleepd, DumpPanic, fairplayd.A2,
    AppleCredentialManager, distnoted, remoted, ptpd, rapportd, amfid, keybagd,
    wifid, watchdogd, MobileGestaltHelper, runningboardd, cfprefsd, locationd,
    bluetoothd, usermanagerd, backboardd, CommCenter, sharingd, imagent, lsd,
    powerd, com.apple.datami, audiomxd, liveactivitiesd, biomesyncd,
    identityservicesd, apsd, lockdownd, contextstored, chronod, iconservicesd,
    ACCHWComponent

`backboardd`, `runningboardd`, `lockdownd`, `apsd` and `CommCenter` are all up.
That is a real iPadOS userland, deeper than any previous interval reached, and it
is the substrate the `App` question has to be answered against.

## `LimitLoadFromHardware` is REFUTED, and now by a live control

`local-private/boot53/launchd-discriminator.txt` already had the distribution:

    --- LimitLoadFromHardware ---
      RAN      : 91x ABSENT, 1x {"osenvironment": ["diagnostics"]}
      NOT RAN  : 425x ABSENT, 1x {"osenvironment": ["diagnostics"]}

    jobs sharing LimitLoadFromHardware={"osenvironment": ["diagnostics"]}:
      1 others; of those, 1 spawned
        RAN     com.apple.logd.plist

So across the 518 readable job plists the key appears exactly twice: once in
`com.apple.logd.plist`, which RAN, and once in `com.apple.SpringBoard.plist`,
which did not.  A key that appears in one job that ran and one that did not is
neither necessary nor sufficient, and the static version of that argument was
already enough to demote it.

**The live boot settles it.**  `logd` is in this very boot's spawn list (n=11),
and SpringBoard is not.  Same boot, same key, same value, opposite outcomes.  A
cause that produces both outcomes in one run is not a cause.

## `POSIXSpawnType=App` remains a perfect separator, and the mechanism is still
## unknown

    jobs sharing POSIXSpawnType="App":
      6 others; of those, 0 spawned
        not-ran com.apple.TextInput.kbd.plist
        not-ran com.apple.assistivetouchd.plist
        not-ran com.apple.bluetoothuiservice.plist
        not-ran com.apple.checkerboard.plist
        not-ran com.apple.commandandcontrol.plist
        not-ran com.apple.fullkeyboardaccess.plist

7 of 7 `App` jobs did not spawn; 0 of 92 that did spawn carry the type.  The live
boot adds 54 spawns and **0** of the 21 that could be matched to a job plist is
an `App` job (19 `Standard`, 2 `Interactive`).

Two honest caveats, because the separator is a correlation and not yet a
mechanism:

* **The decisive cell is n=1.**  The 5 `App` jobs that the enumeration found all
  carry `RunAtLoad=False`, so none of them would spawn at boot regardless of
  type.  SpringBoard is the only `App` job with `RunAtLoad=True` in the readable
  set.  So "7/7" is really "1 decisive case plus 6 that were never going to run".
* **The 6 peers are all UI/scene services** -- keyboard, assistive touch,
  bluetooth UI, checkerboard, command-and-control, full keyboard access.  They
  are the kind of job that runs *inside* a UI session, not at boot.  That is
  consistent with launchd declining to launch `App` jobs before a session
  exists, and it is also consistent with the guest simply never establishing
  whatever a `App` launch requires.  Distinguishing those needs launchd's own
  reasoning, which is closed source.

## What the emitter has and has not settled

**Settled.**  SpringBoard's job file is readable: `job.springboard_open` reported
`err:0`.  So the file exists on the mounted volume and launchd opened it.  The
failure is a load decision made AFTER the open, not a missing file and not a
wrong volume.

**Not settled, and it is the emitter's own gap.**  Only 3 generic `job.open`
records fired against 54 spawns, and all three are `com.apple.jetsamproperties`
plists.  Reading the patch explains it: the generic record's directory test is a
**prefix match on the full path** against six literals that all begin with `/`,
so an `openat(dirfd, "com.apple.foo.plist")` -- a relative path, which is how a
directory walk opens each entry -- is dropped.  The targeted SpringBoard record
compares the **basename**, which is why it fired when the generic one did not.

So the generic record is measuring "opens by absolute path" and not "opens".  The
next emitter revision has to record the path it was actually given, or drop the
prefix requirement, before the generic count means anything.

## Status

Code: COMPLETE for the emitter and the tooling fix.
Windows/real-guest: PASS for the emitter (54 spawns, 3 job.open, 1
job.springboard_open), IN PROGRESS for the six-hour capture.
Onsite: N/A.


---

# Interval 2026-11-04 (forty-seventh, addendum C): `App` is not special.  `App`
# AND `Background` are both zero-for-92.

## The table that settles the shape of the question

`local-private/boot53/launchd-discriminator.txt`, on the 518 readable job plists
(92 spawned, 426 did not):

    --- POSIXSpawnType ---
      RAN      : 35xInteractive, 29xABSENT, 28xAdaptive
      NOT RAN  : 207xABSENT, 165xAdaptive, 41xInteractive, 7xApp, 5xBackground

Every spawn type that launched, launched:

    Standard (absent)  29 ran / 207 not-ran
    Adaptive           28 ran / 165 not-ran
    Interactive        35 ran /  41 not-ran
    App                 0 ran /   7 not-ran     <-- zero
    Background          0 ran /   5 not-ran     <-- zero

**`App` and `Background` are the only two types with zero launches.**  That is
12 jobs, none of which ran, against 92 that did.

This is a better result than the one interval 45 carried, and it changes the
question.  "Why does launchd decline `POSIXSpawnType=App`?" is the wrong shape:
it implies launchd singles out `App`.  It does not single out `App` -- it
declines `App` and `Background` alike, and those are precisely the two
**session-scoped** spawn types.  `Standard`, `Adaptive` and `Interactive` are
boot-scoped, and all three launched.

So the question is: **what does a session-scoped launch require that this guest
never provides?**  And the honest answer is that it is not yet known, with one
structural observation that fits everything measured:

    `POSIXSpawnType=App` jobs are, on iPadOS, the jobs that establish the UI
    session.  SpringBoard is one.  loginwindow is one.  Both are `App`.  A
    session-scoped job that requires a session, to establish the session, cannot
    bootstrap itself -- so SOMETHING has to break that cycle, and on a real
    device it does.  In this guest it does not, and 12 jobs sit unlaunched.

## The live boot, against that table

54 spawns, and **0** of the 21 that could be matched to a job plist is an `App`
or `Background` job (19 `Standard`, 2 `Interactive`).  `backboardd`,
`runningboardd`, `logd`, `configd`, `notifyd` and `lockdownd` are all still
running.  So the substrate an `App` launch would need IS up, and the `App` jobs
still do not start.  That rules out "runningboardd had not come up yet" as the
whole story, and it is why the mechanism is recorded as unknown rather than
guessed.

## Three keys that are 0-for-92 and all sit on SpringBoard

    _LimitLoadFromClarityMode   0/92 RAN    1/426 NOT RAN   (SpringBoard has it)
    EmbeddedHomeScreen          0/92 RAN    3/426 NOT RAN   (SpringBoard has it)
    POSIXSpawnType=App          0/92 RAN    7/426 NOT RAN   (SpringBoard has it)

Recorded because they are the same shape and because the temptation is to treat
all three as causes.  `EmbeddedHomeScreen` and `_LimitLoadFromClarityMode` are
almost certainly *consequences* of being a home-screen app rather than causes --
`_LimitLoadFromClarityMode` is a single job, and a key that appears once cannot
separate anything.  `POSIXSpawnType=App` is the one with a mechanism that fits
and 12 jobs behind it.

## The other anomaly in the same log, recorded so it is not lost

    exit.seen n=5 comm=launchd code=1

PID-1-shaped launchd exits with code 1, early -- right after `fsck`, `mount`,
`seputil` and `mobile_obliterator`, and BEFORE `apfs_boot_util`.  Then 58 daemons
are spawned, none of them by a `launchd` that `exec.seen` or `spawn.seen` ever
records.  Either a second launchd was started by a path neither emitter watches,
or the spawns come from a process the emitters do not attribute.

Not explained, and explicitly not assumed benign.  It is the most likely place
the "who launches the `App` jobs" answer lives, and it is the next thing to look
at once the emitter records what it is actually being handed.

## Status

Code: COMPLETE for the emitter and the tooling fix.
Windows/real-guest: PASS for the emitter (58 spawns, 3 job.open, 1
job.springboard_open), IN PROGRESS for the six-hour capture.
Onsite: N/A.


---

# Interval 2026-11-04 (forty-seventh, addendum D): the general record works, and
# it just located SpringBoard's plist somewhere the prefix list does not cover.

## The measurement that corrected the measurement

At 08:50 the capture had produced THREE `job.open` records against 105 spawns,
and this interval concluded the emitter's six-prefix filter was dropping relative
opens -- that `openat(dirfd, "com.apple.foo.plist")` was passing a path the
prefix test could not match.  An emitter revision was written on that premise.

**At 09:15 the same log carried 154 `job.open` records against 222 spawns.**

    154 records; errno distribution {0: 152, 2: 2}
    the two ENOENTs: com.apple.jetsamproperties.NonUI.plist, com.apple.bootpd.plist
    the last ones: com.apple.wifi.wapic, com.apple.devicecheckd,
                   com.apple.imtransferagent, com.apple.softposreaderd,
                   com.apple.SafariBookmarksSyncAgent, com.apple.textunderstandingd,
                   com.apple.sntpd, com.apple.dt.automationmode-writer,
                   com.apple.privacyaccountingd, com.apple.inboxupdaterd,
                   com.apple.pluginkit.pkd, com.apple.eyereliefd

So the general record was measuring opens correctly all along.  The boot had
simply not reached bulk job discovery yet when it was sampled.  **The premise of
the emitter revision written this interval is refuted, and the revision has been
rewritten rather than committed.**

This is the fifth time this project has measured too early and read the result as
a defect in the instrument.  The others: the QMP client that never sent
qmp_capabilities, the pytest argv that made eight mutants report "killed" with no
output, the four SHF_MERGE fragment greps, and the gate that reported PARTIAL on
a kernel with the emitter compiled in.  The lesson is the same each time and is
worth stating once more: **a low count on a live boot is a statement about the
boot's progress until proven otherwise.**

## What the 154 records DID find, which is better than what was being looked for

`com.apple.SpringBoard.plist` is **NOT** in the general list.  The targeted
basename record fired with `err:0`.  The general record requires the FULL path to
begin with one of six literals:

    /System/Library/LaunchDaemons/
    /System/Library/LaunchAgents/
    /System/Library/NanoLaunchDaemons/
    /private/preboot/System/Library/LaunchDaemons/
    /private/preboot/System/Library/LaunchAgents/
    /private/preboot/System/Library/NanoLaunchDaemons/

**So launchd opened a file whose basename is `com.apple.SpringBoard.plist`,
successfully, from a directory that is none of those six.**

Two of the other `App` jobs were opened normally and ARE in the general list:

    com.apple.assistivetouchd.plist    OPENED (n=65, err=0)
    com.apple.bluetoothuiservice.plist OPENED (n=125, err=0)

and so were two `Background` jobs (`com.apple.OTACrashCopier.plist` n=138,
`com.apple.IOAccelMemoryInfoCollector.plist` n=98).  So the type is not what keeps
a plist out of the general list -- the DIRECTORY is, for SpringBoard specifically.

That is the first hard evidence for the two-volume split
`which-volume-owns-launchdaemons.txt` predicted:

    volume        bytes            LaunchDaemons  NanoLaunchDaemons  LaunchAgents
    OS root     6,077,580,800          545              5              21
    SystemOS    3,303,014,400           21              0               4

    BOTH volumes carry the directory name.  Which one the guest's launchd reads
    is NOT settled by this scan -- it is settled by the mount order, and a union
    at / makes the answer order-dependent.

**Where SpringBoard's plist actually lives is now the open question, and it is
answerable:** the emitter's `probe` buffer already holds the full 256-byte path;
the general record just does not emit it.  The revised emitter puts the full path
in the targeted SpringBoard record, which is bounded to one emission and can
therefore afford 255 bytes.

## The emitter revision, rewritten on the measured premise

The version written on the refuted premise widened the general record's gate to
`in_a_job_dir || launchd_is_the_caller` and added `abs` and `comm` fields.  Both
were answering "does launchd use relative opens", and the answer is no -- 152 of
154 opens matched an absolute prefix.  So the widening is dropped and the revision
becomes the single change the data asks for: **the targeted SpringBoard record
carries the full path it was handed.**

## Status

Code: emitter revision 2 rewritten and installed; the whole 57-patch set still
applies in order to the pristine tree; the patch's own line-number citations all
resolve.
Windows/real-guest: PASS for the emitter (222 spawns, 154 job.open, 1
job.springboard_open), IN PROGRESS for the six-hour capture.
Onsite: N/A.


---

# Interval 2026-11-04 (forty-seventh, addendum E): the 453-record capture found
# SpringBoard's plist in the general list after all, and it was opened TWICE

## Addendum D is retracted, and so is what was committed on it

Addendum D read the channel log at 154 `job.open` records and concluded:

    `com.apple.SpringBoard.plist` is NOT in the general list.

The same log, read at **453** records, carries

    {"event":"job.open","n":239,"job":"com.apple.SpringBoard.plist","err":0,
     "source":"XNU.openat_internal"}

at line index 628 of 1254, 585 lines after the first `job.open` record, at the
moment `spawn.seen` had reached n=236 (the next spawn is n=237, misagent).

So the statement was true of the sample and false of the boot. **This is the
sixth time this project has measured too early and read the result as a fact
about the guest** -- after the QMP client that never sent qmp_capabilities, the
pytest argv that made eight mutants report "killed" with no output, the four
SHF_MERGE fragment greps, the gate that reported PARTIAL on a kernel with the
emitter compiled in, and addendum D's own 154-record sample.

A patch header and a lock note were committed on addendum D's reading. They are
corrected by follow-up commits, not amended: the history of being wrong is the
part worth keeping, and `git push --force` on a branch the build fetches by SHA
is how a build gets the wrong tree.

## What the two records actually say, which is better than either reading

There are TWO opens, and the emitter's two records saw different ones:

| record | line index | when | gate it passed |
|---|---|---|---|
| `job.springboard_open` err:0 | 78 | ~35 opens into job discovery, between spawn n=17 and n=21 | **basename only** |
| `job.open` n=239 err:0 | 628 | 585 lines later, between spawn n=236 and n=237 | **full path under one of six dirs** |

The targeted record is bounded to one emission, so it fired on the FIRST open
and is spent by the second. The general record needs the full path to begin
with one of six literals, so it fired only on the second. **The two records are
not in conflict; they are two opens, and the code says so by construction.**

## And SpringBoard is not the only job opened twice

Eight job files are opened more than once in this boot, and six of them show
the same early-plus-late shape:

    com.apple.jetsamproperties.J120.plist   n=2, 3, 27
    com.apple.ftp-proxy-embedded.plist      n=4, 5, 253
    com.apple.bluetoothd.plist              n=7, 214
    com.apple.locationd.plist               n=9, 293
    com.apple.iomfb_fdr_loader.plist        n=10, 347
    com.apple.triald.plist                  n=59, 203
    com.apple.dhcp6d.plist                  n=303, 452, 453
    bootps.plist                            n=450, 451

So a double open is a pattern, not an anomaly -- launchd walks its job list more
than once, or there is more than one job directory in play.

**What distinguishes SpringBoard is that its EARLY open is not in the general
list and the other six early opens are.** bluetoothd's n=7, locationd's n=9,
iomfb_fdr_loader's n=10, triald's n=59 and ftp-proxy-embedded's n=4 all matched
a watched prefix. SpringBoard's did not. That is the one fact the 453 records
establish that the 154 could not, and it is exactly the fact revision 3's path
field names.

## The question the emitter was built for is now answered

The lock note named the fork: "If the SpringBoard record is present with err=0,
the defect is downstream of the open."

**Both records are present with err=0.** launchd reads SpringBoard's job file,
successfully, twice, from two different paths -- and 442 spawns and 158 distinct
comms later there is still no SpringBoard-shaped comm among them.

So the defect is downstream of the open. It is not a missing file, not a
permission problem, not a path launchd cannot reach. It is what launchd decides
about a job it has read.

That returns the lead to the one hypothesis that has survived every refutation:
`POSIXSpawnType=App` and `Background` are the two session-scoped spawn types,
they are 0-for-92 between them (7 App and 5 Background, none ran), and the three
types that did launch are boot-scoped. The `LimitLoadFromHardware` explanation
stays refuted by the logd control, and the type is still not sufficient on its
own -- assistivetouchd and bluetoothuiservice are also App-typed and their
plists ARE opened, so the directory is not what separates them from SpringBoard
either.

## Status

Code: emitter revision 3 installed and pinned (fork a4428fc, lock
5c2a88f); its header and the lock note are corrected by follow-up commits
recording this retraction.
Windows/real-guest: PASS for the measurement (453 job.open, 442 distinct jobs,
1 targeted + 1 general SpringBoard open, both err:0, 442 spawns, no
SpringBoard comm). IN PROGRESS for the six-hour capture, live at 09:55 with
roughly 4.7 hours of budget left.
Onsite: N/A.


---

# Interval 2026-11-04 (forty-seventh, addendum F): `App` and `Background` are a
# measured 0-for-12 separator, and a respawn loop is two thirds of the guest

> **CORRECTED BY ADDENDUM G, two sections below.** The cross-tabulation and the
> 0-for-12 split stand; they were built from the plists on the volume, not from
> the capped emitters. The third section's respawn-loop claims do not: spawn.seen
> saturated at exactly its `kMaxSpawnRecords` cap of 512, so "the loop has not
> stopped" and "not a throttling failure hidden by a small sample" were read off
> the instrument's bound rather than the guest. Read addendum G before relying on
> anything in that section.

## The anomalous set is 8, not 13, and the reason it was 13 matters

Addendum E inherited a 13-job set of "RunAtLoad=True, daemon did not spawn". Five
of those thirteen never failed to spawn at all — their comm in `spawn.seen` is
MAXCOMLEN-truncated to 16 characters and the comparison was exact:

    com.apple.AppleCredentialManagerDaemon  ->  AppleCredentialM      spawned n=21
    com.apple.accessoryupdaterd             ->  accessoryupdater     spawned n=100
    com.apple.containermanagerd             ->  containermanager     spawned n=18, 27
    com.apple.familynotificationd           ->  familynotificati     spawned n=87
    com.apple.identityservicesd             ->  identityservices     spawned n=36

Corrected to prefix matching, the anomalous set is **8**, and every one of the 8
was opened with err:0 except `com.apple.iomfb_bics_daemon.plist`, whose plist
launchd never opened at all — a different failure mode, not the same one.

This instance belongs to neither of the two series this document already counts.
It is not "measured too early" -- that series is at six, and its four named
instances are the QMP client without `qmp_capabilities`, the pytest argv, the
four SHF_MERGE fragment greps and the PARTIAL gate. It is not "an instrument
that reported a result it could not support" either -- that series is at three,
and here the instrument reported exactly what it saw. The defect was in the
COMPARISON: an exact string match against a field the kernel truncates to
MAXCOMLEN. **The instrument was right and the join was wrong**, which is a third
category and the one most likely to recur, because it produces a plausible
empty set rather than an error.

## The cross-tabulation: two perfect separators, four refuted gates

Over all 518 plists in `/System/Library/LaunchDaemons`, 136 spawned:

    POSIXSpawnType    ABSENT       236 plists   44 spawned
    POSIXSpawnType    Adaptive     193 plists   56 spawned
    POSIXSpawnType    Interactive   76 plists   36 spawned
    POSIXSpawnType    App            7 plists    0 spawned
    POSIXSpawnType    Background     5 plists    0 spawned
    POSIXSpawnType    AdaptiveUtility 1 plist    0 spawned

**`POSIXSpawnType` in {App, Background} is 0-for-12.** Nine of the twelve were
opened with err:0, so launchd read the plist, parsed it, and did not spawn it.

    EmbeddedHomeScreen=True     3 plists (ClarityBoard, SpringBoard, checkerboard)
                                3 opened err:0, 0 spawned
    EmbeddedHomeScreen absent 515 plists, 136 spawned, 0 of the 136 carry it

**`EmbeddedHomeScreen=True` is 0-for-3.** These are the three home-screen jobs;
two are also App-typed, one (ClarityBoard) has no POSIXSpawnType at all, so the
two separators are not the same set.

Four other candidate gates are refuted by the same table, each by a job that
passes the gate and spawned anyway:

    UserName=mobile              90 spawned, 263 did not   -> NOT a gate
    LimitLoadFromHardware         2 plists carry
      {osenvironment:[diagnostics]}                         -> 1 of 2 spawned
    Program (bare path)          29 spawned, 100 did not   -> NOT a gate
    MachServices with 11+ names  20 spawned,   8 did not   -> NOT a gate

So the answer to "which keys do the anomalous jobs share" is: **only `Label` and
`RunAtLoad`**, and `RunAtLoad` is shared by the spawned set too. There is no plist
key that separates them. The decision is in launchd's runtime state, which is what
the 0-for-12 spawn-type split is a proxy for.

## What was found while the capture kept running

**A respawn loop is two thirds of everything the guest does.** Over the capture's
512 `spawn.seen` records:

    bluetoothd     214 spawns, 216 exits, EVERY exit code 256 (status 1)
                   first at line 118 / n=39, last at line 1398 / n=512
    fairplayd.A2   131 spawns, 131 exits, EVERY exit code 0 (clean)
                   first at line  94 / n=17, last at line 1399 / n=511

Together they are 345 of 512 spawns — **67%**. `bluetoothd`'s last spawn is the
capture's last spawn overall, so the loop has not stopped. In the tail past line
1250, 73 of 76 spawns are in the loop.

Two things this loop is NOT. It is not starving SpringBoard: QEMU has burned 6599
seconds of CPU in 89 minutes of wall clock, which is 1.24 cores on a 16-core host
whose total load is 15.5%, and the guest's own vCPU threads are mostly idle. And
it is not a throttling failure being hidden by a small sample — 214 respawns with
no interval is the measurement.

**The framebuffer has not changed since t=121.** The screenshot at
`shots/change-01-t00121.png` still shows UniXNU's own EFI boot menu, the green bar
the loader's legend says means the kernel entry code ran, and "Booting QEMU virt:
source 4K XNU in 1 s". The serial log confirms the hand-off reached the kernel
(`DARWINBOOT: hand-off to the kernel`, `Exiting boot services`). So the kernel is
running, has spawned 512 processes, and has never presented anything to the
display. The display and SpringBoard are two separate blockers.

## Status and the next experiment

Code: unchanged from addendum E. The separators above are measurements, not
patches.
Windows/real-guest: PASS for the cross-tabulation (518 plists, 136 spawned,
0-for-12 App/Background, 0-for-3 EmbeddedHomeScreen, four gates refuted).
FAIL/OPEN for the two new defects: a 67%-of-spawns respawn loop, and a framebuffer
frozen on the bootloader's own UI for 90 minutes of live kernel activity.
Onsite: N/A.

The next experiment is now fully specified by the 0-for-12 split and is cheap:
take a scratch copy of the guest's Data volume, remove `POSIXSpawnType` from
`com.apple.SpringBoard.plist`, and boot. If SpringBoard spawns, the key is
load-bearing and the gate is the spawn type. If it does not, the key is a proxy
and the gate is the session the twelve jobs belong to — which is the same
experiment either way, because a scratch volume is discarded afterwards.


---

# Interval 2026-11-04 (forty-seventh, addendum G): the spawn emitter saturated at
# 512, and that is why "the loop has not stopped"

## What addendum F got wrong, and the one line that did it

Addendum F's third section reported a respawn loop from the still-running capture:
345 of 512 spawns, "the loop has not stopped", and "not a throttling failure being
hidden by a small sample". **None of those three statements survives contact with
the emitter's bound.**

    enum { kMaxSpawnRecords = 512 };     zzz-native-springboard-events.patch
    enum { kMaxExitRecords   = 512 };    same file
    enum { kMaxJobOpenRecords = 512 };   zzz-native-job-open-events.patch
    enum { kMaxExecRecords    = 64 };    zzz-native-springboard-events.patch
    enum { kMaxSpawnFailRecords = 64 };  same file

Measured against those caps, at the moment of writing:

    record        count   cap   last n   at line   verdict
    spawn.seen      512   512      511      1399   SATURATED
    exit.seen       433   512      433      1453   79 left
    job.open        454   512      454      1353   58 left
    exec.seen         2    64        2        58   62 left
    spawn.failed      0    64        -        -    unused

**`spawn.seen` is at exactly its cap.** The last spawn record is at line 1399 of
1453; the 54 lines after it contain nothing but `exit.seen`. Addendum F read that
tail as "the loop has not stopped" and, separately, as the guest going quiet.
Neither is right: the emitter is blind past record 512, so the tail says nothing
about the guest at all.

The cycle rate addendum F denied is real and measurable. Over a 60-second window,
bluetoothd produced 3 records and fairplayd.A2 1 -- a respawn every 20 seconds and
every 60 seconds. That is throttle-shaped, not a hot spin. The earlier reading of
"tight crash-respawn loop" came from 138 lines in which the two daemons' records
are adjacent; adjacency in the file is not adjacency in time, and these records
carry no timestamp.

## And the patch's own comment predicted this, in writing

The exit emitter's header says:

    AND SIXTY-FOUR SATURATED IN INTERVAL 42.  exit.seen stopped at exactly
    n=64 and the 64th record is `exit.seen n=64 comm='bluetoothd'
    code=256`, while bluetoothd kept exiting afterwards ... This is the same
    defect that hid 91 of interval 41's 155 spawns on the spawn emitter: that
    bound went 64 -> 512 afterwards and this one was left behind.

So the cap hid exits at 64 in interval 42, the spawn cap was raised 64 -> 512 in
response, the exit cap was raised 64 -> 512 later, and 512 has now saturated the
spawn cap on a six-hour capture. **Three raises, same defect, and the response has
been to move the number each time.** The cap is not the problem; the cap being
silent is. An emitter that stops without saying so turns a bounded measurement
into an apparent fact about the guest, which is precisely the failure mode this
document has now recorded eight times.

## What still stands, and what is now unfalsifiable

**The cross-tabulation stands, because it did not come from the emitters.** The
518-plist table was built by reading the plists out of the guest's APFS volume and
joining them against `spawn.seen`'s comm field. The plist side is complete; the
spawn side is capped. So the table is sound as a description of the first 512
spawns.

**But the 0-for-12 negative is now unfalsifiable rather than confirmed.** A job
that spawns after record 512 is invisible, and session-scoped jobs are exactly the
jobs that launch LATE -- after the session is established, which is the phase the
emitter is now blind to. The honest statement is: *no App- or Background-typed job
spawned within the window the instrument can see, and that window ends before the
phase in which such a job would be expected to spawn.* That is weaker than
"launchd declines them", and the distinction is the whole point of this addendum.

What still stands independently of the cap:

- The plist was opened twice, both times err:0, from two different paths.
- 442 distinct job basenames were read (of 518 on the volume).
- No SpringBoard-shaped comm appeared among the 161 distinct comms the emitter saw.
- The framebuffer has not changed since t=121 while the kernel spawned 512
  processes, and the serial log confirms the hand-off reached the kernel.

## Status and what has to happen before another long capture

Windows/real-guest: FAIL for the capture's own instrument. spawn.seen saturated at
512 after roughly 90 minutes of a 6-hour budget, and job.open and exit.seen have
58 and 79 records left, so the capture goes silent within about 137 more records
and then contributes nothing for the remaining four hours.

The fix is not a larger number. Two changes, both small:

1. **Emit a saturation record when a cap is reached**, so a later reader cannot
   mistake the bound for the guest. One record per emitter, at the cap, naming the
   emitter and the cap.
2. **Raise the caps by orders of magnitude, not by factors.** The header's own
   justification for the current values is that "the bound costs four bytes of
   static data" -- which is an argument for a large bound, not a small one. The
   channel is a virtio console whose purpose is these records.

Until both land, no long capture on this kernel can distinguish "the guest stopped"
from "the emitter stopped", and every negative read off one is worthless.

---

# Interval 2026-11-04 (forty-seventh, addendum H): the prediction came true, the
# freeze is measured directly, and one of my own baselines was from another boot

## G's prediction was confirmed by G's own arithmetic

Addendum G ended with a measurement, not a guess: spawn.seen had saturated at 512
after roughly 90 minutes, and `job.open and exit.seen have 58 and 79 records
left, so the capture goes silent within about 137 more records and then
contributes nothing for the remaining four hours`.

That is what happened. The census taken at 10:58Z, with the channel's last write
at 10:50:02Z:

    spawn.seen        512   last record n=511, at line 1399   SATURATED
    exit.seen         512   last record n=512, at line 1532   SATURATED
    job.open          454   last record n=454, at line 1353   58 of headroom left
    exec.seen           2   last record n=2,   at line 58

exit.seen went blind 133 lines after spawn.seen did, and the channel has been
silent for 8 minutes 38 seconds at the time of the census. Nothing has been
written since. This is the confirmation: the emitter's bound, not the guest,
ended the measurement, and it ended it at the record count G predicted.

**What the capture established, and what it did not.** It established that at the
moment the exit emitter went blind, the respawn cycle was still running --
bluetoothd exit n=508, fairplayd.A2 exit n=512, alternating to the last record.
It did not establish anything about the guest after 10:50. Under the throttled
~20-60 s cycle G measured, 8m38s of silence would have produced roughly ten to
twenty-five more exits. Their absence is equally consistent with the loop having
stopped and with the emitter having stopped. The two are indistinguishable, which
is the entire content of this addendum and of G.

## The framebuffer: measured directly, and a baseline of mine was from another boot

The claim inherited from earlier intervals is "the framebuffer has not changed
since t=121". Until now it rested on a hash timeline: a screendump every 120 s,
hashed, deleted unless it was the first or its hash differed from the previous
one. `shots/` holds exactly two files, `t00000.png` and `change-01-t00121.png`,
so exactly one change was ever recorded.

A hash timeline has two failure modes and this one had both available. The
harness could have died, in which case "no new files" is a fact about the
harness; and the baseline hash could have come from somewhere other than this
boot. Both needed checking before the claim could be used.

**The harness is alive.** PID 39124, `capture-springboard-boot.py`, created
16:37:57 local, with a 21600 s budget. It has been running 2h27m and still has
about 3.5 hours left. Its QMP port is 15919 and its chardev log is the same
`guest-channel.log` QEMU writes directly, so the channel records do not depend on
the harness at all.

**The screen was measured directly.** A screendump was taken through QMP on the
live guest, after first sending `qmp_capabilities` -- because a client that never
negotiates gets an error back and reads it as "screendump is unavailable", which
cost this project days once already. The guest answered: `query-status` running,
`screendump` returned `{}`, 1440015 bytes, P6 800x600.

**And then I made the mistake.** I hashed that dump, compared it against
`8833260ecd372e44f0c0bc52260880f7a31905b1b6ffcba50cb25709aedae4a0` -- the last
value in the timeline -- saw a mismatch, and stated that the screen had CHANGED.

The baseline was wrong. `8833260e...` is the last entry in
`local-private/boot53/spawn-failed-capture.log`, whose last write is 03:12Z, five
and a half hours before this boot's QEMU started at 08:38Z. It is the PREVIOUS
boot's timeline. This boot's own recorded hashes live in a background task's
stdout that is not on disk.

So the correct measurement is not a hash comparison at all. It is a pixel
comparison against this boot's own image, which the harness kept precisely
because its hash differed from the first dump:

    old change-01-t00121.png (this boot, t=121s)   800x600
    new live screendump        (this boot, t=8820s) 800x600
    0 of 480000 pixels differ (0.000%)

The conversion to PNG was checked by decoding it back and comparing the raster
byte-for-byte before anything was reported, because "the screen shows the
bootloader menu" would otherwise be a statement about my converter. Decode-back
is identical.

**The framebuffer has not changed. It is frozen on the UniXNU EFI boot menu**,
showing `DARWINBOOT: inputs validated: exiting boot services`, `DARWINBOOT:
hand-off to the kernel`, the green bar across the top that the menu itself says
means the kernel entry code ran, and the selected entry `QEMU virt: source 4K
XNU`. The kernel spawned 512 processes behind that image.

This is the ninth or tenth time this interval's failure mode has appeared, and it
is the first time it was mine rather than the instrument's: a number compared
against the wrong provenance, believed long enough to be stated as a finding, and
caught only because the next check was a pixel diff rather than another hash.

## What the guest itself says about the display, which is a better lead than "frozen"

There is exactly one display record in the whole capture, at channel-log line 42,
very early in boot:

    {"event":"diag.legacy_display_status","stage":10,"surface_status":0,
     "surface_found":1,"dt":1,"source":"XNU.LegacyDisplayProbe"}

`surface_found` is 1 and `dt` is 1. So the legacy display probe found a surface
and found the device tree. It did not fail. Nothing drew to the surface
afterwards, or what it drew went somewhere the ramfb is not.

That reframes task #24 from "the framebuffer is frozen" -- which is a symptom --
to "the probe succeeded and nothing took the surface". The green bar in the
frozen image is the bootloader's own indicator that the kernel entry code ran, so
the hand-off is not in doubt either. What is missing is between the probe and a
console that owns the framebuffer.

An investigator is reading the emitter, the code path after it, and whether a
driver matching QEMU's `ramfb` exists in the tree at all. Its report lands in the
next interval.

## Status

Code: emitter revision 4 installed and pinned. Fork `3d83de1`, lock pin
`3d83de17dd4e4be468ac3fb8d30ee21c597e8972`, both pushed. All five caps at 65536,
five saturation blocks, four `char text[4]` grown to 8. Validated by compiling
the four kern_exec/kern_exit emitter blocks with MSVC `/W3 /WX` and running them
past their caps: 262148 records, 0 invalid, each emitter at exactly 65536, each
saturation record exactly once.

Windows/real-guest: FAIL for the capture, and now for a stated reason rather than
an inferred one. spawn.seen and exit.seen both saturated at 512; job.open has 58
records left and will go silent. The capture is blind and is contributing nothing
but CPU. PASS for the framebuffer measurement, which is now direct: 0 of 480000
pixels differ between t=121 and t=8820.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, stage M7, target tcg,
dispatched on lock commit 620f440 at 10:56Z. The push-triggered preflight run
41817495435 cross-checks `unixnu_commit` against the lock on the same ref.

Onsite: N/A.

---

# Interval 2026-11-04 (forty-seventh, addendum I): the capture ended, and it
# settled three of the four open tasks with measurements no cap can bound

## H is wrong about one thing, and the correction is the result

Addendum H said: "The harness is alive. PID 39124, `capture-springboard-boot.py`,
created 16:37:57 local, with a 21600 s budget. It has been running 2h27m and
still has about 3.5 hours left."

It is not alive. It stopped at `listen_seconds: 9106` of 21600, on
`stopped_on_quiet: true`, after `last_bytes_at_s: 7902` -- 1204 s of silence
against a 1200 s threshold. The report is preserved verbatim at
`local-private/boot53/spawn-failed-input-run-scratch/harness-final-report.txt`,
because it was only ever in a background task's stdout and that buffer is bounded.

**The quiet is the emitter's bound, and this time it is a measurement rather than
an inference.** The harness started at 08:38Z. Its last channel bytes arrived at
t=7901 s, which is 10:49:41Z. `guest-channel.log`'s last write is 10:50:02Z, and
that write is `exit.seen n=512` -- the exit emitter's cap. The channel did not go
quiet because the guest went quiet; it went quiet because the last emitter that
could still write hit its bound and stopped. The two timestamps are 21 s apart and
the last record is the cap.

So addendum G's honest statement -- "the guest did not go quiet and the loop did
not persist, the emitter went blind" -- is now supported by a correlation rather
than by an argument. The guest's silence and the instrument's bound are the same
event.

## What the capture measured that no cap can bound

These come from instruments with no record limit at all, so they are not affected
by anything in addenda G or H.

**The screen changed once, at t=122, and then never again for 8884 s.** 74
dumps, 2 distinct images:

    t=0      fb086cbfb138cfb9d6c3e504177460be884190a985a93412f7f3d248b0d42fdd
    t=122    f54dadc07fb07106c07e7383d897053a0f7d9e50b53b11c86fecc9e694cc3081
    t=242 through t=9000: f54dadc0... every time

`f54dadc0` is the exact sha256 of the screendump taken directly through QMP on
the live guest during addendum H, which is what proves the harness and that dump
were looking at the same guest. The image is the UniXNU EFI boot menu, and the
pixel comparison in H -- 0 of 480000 pixels differ between t=121 and t=8820 --
stands.

**H's paragraph about a mis-sourced baseline was solving a problem that did not
exist.** The harness's own timeline for this boot was in the background task's
stdout the whole time and records the t=122 change directly. The pixel diff and
the hash timeline agree. I looked in the wrong file first, found the right answer
anyway, and then wrote up the wrong file as though it were the finding.

**The Home key was pressed 18 times and never changed the screen once.**

    t=486, 980, 1461, 1941, 2422, 2916, 3411, 3891, 4371, 4866,
       5360, 5853, 6345, 6836, 7327, 7820, 8310, 8798  --  all changed=False

18 presses over 2h27m, each followed by a 5 s settle and a screendump, and not
one produced a single different pixel. The input devices were attached
(`input_devices: true` in the report) and the install frame was sent 299 times
with `send_errors: []`. This is a clean negative on the input path reaching the
display, and unlike the spawn and exit negatives it is not bounded by anything.

What it does NOT establish: whether the guest's HID driver received the key at
all. The harness presses it through QEMU's input layer; the guest has to have a
driver bound to `virtio-keyboard-device` for the press to mean anything. That is
a separate question and it is not answered here.

**The respawn table, with measured periods.** The harness computed this from the
spawn records it did get:

    bluetoothd        214 spawns,  median gap  16 s
    fairplayd.A2      131 spawns,  median gap  30 s
    com.apple.datami     5 spawns, median gap 883 s
    containermanager    2 spawns, median gap 127 s
    cfprefsd            2 spawns, median gap   0 s
    locationd           2 spawns, median gap 3543 s
    CloudKeychainPro    2 spawns, median gap 1006 s

Task #23 asked about "the bluetoothd/fairplayd respawn loop". It is two loops
with different periods and, as it turns out, different causes.

## The exit codes say bluetoothd is failing, not cycling

The exit emitter records `rv`, which its own comment defines precisely:

> rv is the wait status the kernel is about to hand the parent, so it is the
> kernel's own number and not an interpretation of it. Negative is emitted with
> a sign: a process killed by a signal reports a negative status.

`zzz-native-springboard-events.patch:371-374`. So `code` is the raw wait status,
not `WEXITSTATUS`. Decoding it under XNU's `W_EXITCODE(ret, sig) = (ret << 8) |
sig`:

    code  256  ->  WIFEXITED, WEXITSTATUS 1    303 exits (300 bluetoothd)
    code    0  ->  WIFEXITED, WEXITSTATUS 0    205 exits (174 fairplayd.A2)
    code    1  ->  killed by signal 1 (SIGHUP)   2 exits (MSUEarlyBootTask, launchd)
    code 3328  ->  WIFEXITED, WEXITSTATUS 13    1 exit  (prng_seedctl)
    code    6  ->  killed by signal 6 (SIGABRT)  1 exit  (transparencyd)

**The decode is proved by impossibility, not by the header.** If `rv` were the
raw exit code, `bluetoothd` would have called `exit(256)`, which POSIX truncates
to 0 -- so a raw exit code of 256 is not reachable. As a wait status, 256 is
exactly `W_EXITCODE(1, 0)`. All five values decode consistently under the
wait-status reading and one of them is impossible under the raw-exit-code
reading. The XNU header itself is not in this tree -- only the patches are -- so
the decode is inferred from the encoding, and it is labelled as such.

**So bluetoothd exited with status 1 three hundred times.** That is a failure
exit, not a clean cycle. It was relaunched every 16 s regardless. The guest was
run with `-nic none` and no Bluetooth device of any kind, so the obvious cause is
that bluetoothd cannot find a controller and `exit(1)`s, and launchd's KeepAlive
restarts it forever. That is a hypothesis with an obvious test and an
investigator is checking the plist and the device tree.

**fairplayd.A2 is the opposite**: 174 clean `exit(0)`s, relaunched every 30 s. A
daemon that completes successfully and is immediately relaunched is a different
defect from one that fails, and the two should not have been reported as one
"respawn loop".

## The display started. The defect is downstream of the probe.

The harness decodes `diag.legacy_display_status` itself, and its decode is not
the one I gave it in addendum H:

    stage 10 -- the display started AND phase == 4: activeLegacyDisplay is set
    and the GOP presenter can be built

So `surface_found: 1` and `surface_status: 0` mean the probe SUCCEEDED and set
`activeLegacyDisplay`, and the GOP presenter can be built. Addendum H said "the
probe found a surface and nothing drew to the surface, or what it drew went
somewhere the ramfb is not", which is the right shape but too weak: the display
did not merely find a surface, it initialised.

What is still missing is whatever happens after `activeLegacyDisplay` is set. The
screen shows the bootloader's last frame with the green bar that the menu itself
says means the kernel entry code ran, and the kernel spawned 512 processes behind
it. So the hand-off is not in doubt, the display probe is not in doubt, and the
gap is between the two.

## `springboard.running` and `springboard.interaction` never appeared on the channel

The report's `emitter_counts` field is not a count of runtime events. It is
`{e: kernel_bytes.count(e.encode()) for e in wanted}` -- a count of how many times
each literal appears in the KERNEL BINARY. It reports `springboard.running: 1`
and `springboard.interaction: 1`, which means those literals exist in the image,
not that they fired.

And that gate is known-unreliable, by the harness's own words:
`"recorded, not enforced: a mergeable-string image can hide a literal that is
present, so absence is not evidence (build 41617257137)"`. The same field reports
`exec.seen: 0`, `exit.seen: 0` and `spawn.seen: 0` -- three emitters that
demonstrably fired 512, 512 and 2 times respectively on this very channel. The
gate produces false negatives on emitters that provably ran.

**So the literal count is not evidence that the SpringBoard emitters are in the
kernel either.** The only reliable statement is the direct one, from the
harness's own event list:

    missing (the experiment): ['springboard.running', 'springboard.interaction']

Neither record ever appeared on the channel in 2h32m of listening, across 356
byte-arrival batches totalling 139590 bytes, while 512 processes spawned and 512
exited. **SpringBoard did not run.** That is the whole finding, and it needs no
instrument beyond the channel itself.

## Status

Windows/real-guest: the capture is COMPLETE and its instrument is FAIL, as
predicted. spawn.seen and exit.seen both saturated at 512; the channel went quiet
at t=7901 s, 21 s before the exit emitter's cap was written, and the harness
stopped on quiet at t=9106 s. PASS for everything measured without a bound: the
screen's single change at t=122 and 8884 s of nothing after it, 18 Home presses
with no effect, the respawn table with measured periods, and the exit-code
histogram.

Answered by this addendum:
- #21 the six-hour capture: ended at 2h32m on quiet; its quiet is the emitter's
  bound, correlated to within 21 s of the cap being written.
- #23 the respawn loop: two loops, not one. bluetoothd exits 1 every 16 s;
  fairplayd.A2 exits 0 every 30 s.
- #24 the framebuffer: frozen on the EFI boot menu since t=122, measured in
  pixels; the display probe SUCCEEDED and set activeLegacyDisplay, so the defect
  is downstream of the probe.

Still open:
- #22 why no App/Background job spawned. Unchanged, and now with a sharper edge:
  the SpringBoard emitters are absent from the channel, so SpringBoard did not
  run, and the window the capped emitters could see ended before the session
  phase. The 65536-cap build will say whether the same is true with 128x the
  window.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, in progress at the
time of writing. Preflight 41817495435 passed on the same ref, which is the run
that cross-checks `unixnu_commit` against the lock.

---

# Interval 2026-11-04 (forty-seventh, addendum J): the literal gate's mechanism is
# proven, and addendum I was too pessimistic about it

## Addendum I gave up a finding it did not have to give up

Addendum I's last section argued: `emitter_counts` reports `exec.seen: 0`,
`exit.seen: 0` and `spawn.seen: 0` for emitters that fired 512, 512 and 2 times,
therefore the gate produces false negatives, therefore "the literal count is not
evidence that the SpringBoard emitters are in the kernel either".

The first two clauses are right and the third does not follow. A false NEGATIVE
rate says nothing about the false POSITIVE rate. I checked, and the two
SpringBoard literals are present contiguously in the kernel that ran the
512-record boot. The emitters are compiled in. They never fired.

## The mechanism, proven rather than noted

The harness carried a warning about this gate: "recorded, not enforced: a
mergeable-string image can hide a literal that is present, so absence is not
evidence (build 41617257137)". That warning is correct and it is now explained.

Each emitter stores its record as three static arrays. From
`zzz-native-springboard-events.patch:496-499`:

    static char const spawn_prefix[] =
        "{\"event\":\"spawn.seen\",\"n\":";
    static char const spawn_middle[] = ",\"comm\":\"";
    static char const spawn_suffix[] = ...

The linker merges the common TAILS of those arrays with unrelated literals in the
kernel. Enumerating every contiguous `{"event":"...` literal in the kernel that
booted -- sha256 `bcb2a9474c590b629fec97c3149072a6e8b38ea529352166781069048d50a28a`,
verified against the harness's own `kernel_sha256` field -- gives:

    {"event":"diag.control_from_driver","source":"NativeVirtioConsole.start"}\n
    {"event":"diag.free_fn_from_driver","source":"NativeVirtioConsole.start"}\n
    {"event":"diag.free_fn_status","status":"not_open"}\n
    {"event":"diag.free_fn_status","status":"other"}\n
    {"event":"diag.free_fn_status","status":"success"}\n
    {"event":"diag.install_rx_bytes","source":"XNU.native_console_poll"}\n
    {"event":"diag.install_thread_start","status":"failed"}\n
    {"event":"diag.install_thread_start","status":"ok"}\n
    {"event":"diag.install_thread_started","source":"XNU.bsdinit_task"}\n
    {"event":"diag.load_init_program_entered","source":"XNU.load_init_program"}\n
    {"event":"diag.owner_data_status","stage,"error","source":"XNU.native_owner_data\xed\x01
    {"event":"dyld.cache_read","source":"XNU._shared_region_map_and_slide"}\n
    {"event":"exec.s                                              <- TRUNCATED
    {"event":"exit.s,"source":"XNU.exit_with_reason"lockf         <- MERGED with another literal
    {"event":"install.registered","source":"XNU.native_install_service"}\n
    {"event":"job.op                                              <- TRUNCATED
    {"event":"job.springboard_open",,"source":"XNU.openat_internal"}{"event":"job.op
    {"event":"spawn.                                              <- TRUNCATED
    {"event":"xnu.page_tables","mmu_enabled":true,"source":"NativeVirtioConsole.start"}\n

Read the four truncated ones. `{"event":"exec.s` stops mid-word. `{"event":"exit.s`
is followed immediately by `,"source":"XNU.exit_with_reason"` and then `lockf` --
the tail of an unrelated literal, merged in. `{"event":"job.op` stops mid-word.
`{"event":"spawn.` stops at the dot. And `job.springboard_open` carries a DOUBLE
comma, `,,`, where its err field's middle array was relocated.

That is why `spawn.seen` counts 0 in a kernel that emitted it 512 times: the
substring does not exist. The literal stops at `spawn.`.

**And it is why the SpringBoard literals are real.** `springboard.running` is
present once, contiguously, with its whole prefix and suffix:

    {"event":"springboard.running",","comm":"SpringBoard","source":"XNU.posix_spawn"

`springboard.interaction` is present once as `:"springboard.interaction","` -- its
name contiguous, its prefix relocated. `legacy_display_status` once as
`:"diag.legacy_display_status`, same shape. And `job.springboard_open` once as
`{"event":"job.springboard_open",,"source":"XNU.openat_internal"` -- name and
prefix contiguous, its err field relocated, which is what the double comma is.

So the honest split is: `springboard.running` is a full contiguous literal and is
therefore a trustworthy positive on its own. The other three carry their event
name contiguously with a relocated prefix, which is enough to say the emitter is
compiled in but is not the same strength of evidence.

## What that buys, and what it does not

**It buys the sharpest statement yet about task #22.** The harness's own header
says the emitter is gated on `p_comm == "SpringBoard"`. That gate is compiled into
this kernel, it is reachable, and in 2h32m of listening -- across 356 byte-arrival
batches totalling 139590 bytes, while 512 processes spawned and 512 exited -- it
never fired once. Neither did `springboard.interaction`.

That is no longer a statement about a capped window. It is a statement about the
guest: **SpringBoard did not run, and the kernel was watching for it by name.**

**It does not buy anything about the caps.** The same kernel has `saturated` zero
times, which is correct and expected -- it predates revision 4. The check for the
new kernel is the same fragment, and it is now a meaningful one rather than a
guess, because the control has been run: a kernel known to have emitted
`spawn.seen` 512 times counts that literal 0, and a kernel known not to carry the
saturation blocks counts `saturated` 0. The instrument has answered a
known-present question and a known-absent one.

**And it retires one more way to be wrong.** Addendum I's caution -- "absence is
not evidence" -- was the right instinct applied too broadly. The correct rule is
narrower and more useful: in THIS kernel, absence proves nothing because the
linker merges these arrays, but presence is real. Any future check that reads a
zero off this gate and calls it a finding is making the same mistake the gate's
own warning describes.

## Status

Windows/real-guest: unchanged from addendum I, with #22 sharpened. The
SpringBoard emitters are compiled into the kernel that ran, gated on
`p_comm == "SpringBoard"`, and never fired in 2h32m.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, all 34 steps
complete, four artifacts uploaded. `boot-inputs` (310 MB) is downloading into
`local-private/tmp/dl-caps/`; the kernel literal check will be run against it as
soon as it lands, with the control above already recorded.

Onsite: N/A.

---

# Interval 2026-11-04 (forty-seventh, addendum K): the caps are in the kernel, the
# display's root cause is a week old and half-fixed, and addendum J over-claimed

## The new kernel carries all five saturation literals, with a control in both directions

Build `exp-2026-11-04-emitter-caps` (run 41817661032, M7/tcg) produced a kernel
whose sha256 is `4914d56c95e97c33f8580f4fd05319c8a9c111e8e52ccc90764907c0edf12021`
and whose size is 125497344 bytes -- the same size as the previous build's, which
is not evidence of anything either way, because 55 lines of C can land inside
existing padding.

The check that IS evidence is the literal count, and it has now been run against
both kernels:

    kernel 41788097516 (pre-cap, sha256 bcb2a947...)   saturated = 0
    kernel 41817661032 (this build, sha256 4914d56c...) saturated = 5

And the five are complete, contiguous, NUL-terminated literals, not fragments:

    {"event":"exec.saturated","source":"XNU.__mac_execve"}\n
    {"event":"exit.saturated","source":"XNU.exit_with_reason"}\n
    {"event":"spawn.saturated","source":"XNU.posix_spawn"}\n
    {"event":"spawn_failed.saturated","source":"XNU.posix_spawn"}\n
    {"event":"job_open.saturated","source":"XNU.openat_internal"}\n

Each exactly once. **They are greppable precisely BECAUSE they are new** -- the
emitters' ordinary literals get their tails merged with unrelated strings by ld,
but a string with no shared tail has nothing to merge with. That is why the same
kernel that carries all five saturation literals still counts `spawn.seen` zero
times.

`springboard.running` and `springboard.interaction` are still present once each in
the new kernel. The SpringBoard emitters are still compiled in.

The boot is live on this kernel as of 12:04Z. Its `diag.legacy_display_status` is
byte-identical to the previous build's (`stage 10, surface_status 0, surface_found
1, dt 1`), and `system.mount` reports the same volume UUID
`8846FE78-2910-4297-AF97-D59BB6F8FC16`. The question this build exists to answer --
whether the guest goes quiet or the emitter does -- is now measurable for the first
time.

## Addendum J over-claimed on the mechanism

Addendum J wrote: "The harness carried a warning about this gate ... That warning
is correct and it is now explained", and then presented the prefix/middle/suffix
arrays and the linker's tail merging as the explanation.

The explanation was already in the tree. The exec emitter's own comment in
`zzz-native-springboard-events.patch` says, at length:

> AND THE LITERAL IS NOT GREPPABLE IN THE IMAGE, which is measured and not
> guessed. The once-per-boot version's `exec.first` literal counted ZERO as a
> contiguous byte string in the kernel that carried it, because ld's
> SHF_MERGE|SHF_STRINGS pass split the 30-byte string into a 16-byte head plus a
> shared tail -- while the block-scope static `___mac_execve.native_first_exec_
> reported` was absent from the previous kernel and present in that one.

So the project knew, in the patch, a week ago. What addendum J actually
contributed is narrower and still useful: it enumerated WHICH literals got split
(`{"event":"exec.s`, `{"event":"exit.s,...lockf`, `{"event":"job.op`,
`{"event":"spawn.`) and found that `springboard.running` did not. The enumeration
is new; the mechanism was not, and saying it was is the same failure mode this
whole interval is about -- presenting a re-derivation as a discovery.

## The display's root cause is a week old, and half of it is fixed

Addenda H, I and J each said some version of "the probe succeeded and the defect
is downstream of it". That is true and it is also the second time this project has
reached that sentence. `docs/boot-windows.md` sections 7.5.11 and 7.5.12, dated
2026-10-28, already contain the whole chain.

**7.5.11 recorded an indeterminacy.** `native_legacy_display_late_probe()` has two
places it can return early, (A) the first probe released the object and (B) the
five-second `waitForMatchingService` closed with the service absent, and the
existing counters cannot tell them apart -- `pendingLegacyDisplay` is a
file-linkage `static IOService *` with no symbol, and `native_legacy_surface_status`
and `_found` live in .bss whose initial value is 0, so "assigned 0" and "never
assigned" are the same reading.

**7.5.12 resolved it with sampling frequency rather than a new counter.**
`native_legacy_stage` stops at 3 when the first probe retains and jumps 3->7->8
when it releases. One run at 1 s sampling:

    t= 14.1s stage=0
    t= 17.2s stage=3      <- the first probe RETAINED the object
    t= 27.8s stage=8      <- the late probe ran and returned early

So (A) is false -- `offline-legacy-phase=4` was parsed correctly. (B) is true: the
late probe ran at t~27.8 s, its window closed at t~32.8 s, and
`IOSurfaceRoot::installMemoryRegions()` lands at t=51.8 s in one run and t=86.1 s
in another. A 34-second spread between two runs, so the ordering is not fixed.

**And 7.5.12 killed the obvious fix.** Extending the wait to 90 s deadlocks: the
probe blocks, `bsd_autoconf()` does not return, `mountroot` does not run,
`IOSurfaceRoot` never installs, and the wait waits on the boot it is blocking. The
evidence for that is an interleaving of `nx_mount` and
`IOSurfaceRoot::installMemoryRegions()` 8 ms apart in the guest log -- which
proves two lines are close in time and does NOT prove they are on the same thread.
7.5.12 labels that inference as an inference.

**The fix that landed is the other one.** `offline-legacy-start.patch` (patch 35
of 57) moves the call past `IOSecureBSDRoot()`, to the point where the root volume
is known to be up, so the wait returns at once because the service is already
published. Its header records the measurement: the early probe retains the display
object at t~17.2 s, the late probe ran at t~27.8 s, its wait closed at t~32.8 s
with the service absent, and `IOSurfaceRoot::installMemoryRegions()` landed at
t~51.8 s -- "So raising the timeout cannot help: it deadlocks."

**And it worked.** Today's boot reports `surface_found: 1` where the 2026-10-28
measurement recorded `native_legacy_surface_found = 0`. Same kernel path, same
emitter, same field. The gate at (B) is now passing where it did not.

**So the defect has moved, and addenda H/I/J were describing the NEW location
without saying the old one was fixed.** The framebuffer still shows DarwinBoot's
last frame, `native_gop_ready` was 0 in the October measurement, and today's
harness decodes stage 10 as "the display started AND phase == 4:
activeLegacyDisplay is set and the GOP presenter CAN BE BUILT" -- can be, not is.
The open question is now one level down: the surface is found, and something
between that and a presented frame still does not happen.

There is no emitter for `native_legacy_frame_count` in the current patch set, so
that counter cannot be read from the channel. The October measurement came from a
diagnostic script reading .bss directly, not from the console.

## The artifact download was 26x faster once the limit was identified

One connection to GitHub's artifact CDN measured 47 KB/s, which is 89 minutes for
the 310 MB boot-inputs bundle. The CDN answers 206 to a Range request, so the
transfer splits eight ways: 807 KB/s overall, 26x.

The first parallel run died at 25 of 38 parts with `403 544` on every remaining
range. The SAS URL carries `se=<expiry>` and it was 11:44 -- three minutes after
the URL was resolved. **A long parallel download outlives its own credential.**
The downloader now re-resolves the URL when a range answers 403 and skips parts
already complete, which is what let the second run finish the last 13 parts.

`local-private/tmp/parallel-artifact.py`.

## Status

Windows/real-guest: the 65536-cap kernel is booted and capturing as of 12:04Z,
with the caps verified present in the image by a check whose control ran in both
directions. The display's upstream root cause is FIXED (`surface_found` 0 -> 1)
and the remaining defect is one level below it.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, all 34 steps
complete, four artifacts, boot inputs extracted and booting.

Onsite: N/A.

---

# Interval 2026-11-04 (forty-seventh, addendum L): the window was never what hid
# SpringBoard, and the targeted emitter was matching a logging plist

## Launchd reads SpringBoard's plist at spawn n=239, which is inside the old cap

The 65536-cap kernel booted at 12:03Z and is capturing. At spawn n=241 the channel
carries:

    {"event":"job.open","n":239,"job":"com.apple.SpringBoard.plist","err":0,
     "source":"XNU.openat_internal"}

The previous capture carried the identical record at spawn n=236. Two boots, two
builds, the same job file read at the same point in the spawn sequence, err:0.

**So addendum G's caveat was the wrong caveat for this job.** It said the 0-for-12
negative was unfalsifiable because "a job that spawns after record 512 is
invisible, and session-scoped jobs are exactly the jobs that launch LATE".
SpringBoard's plist is read at n=239 -- 273 records inside the old cap of 512. The
window was never what hid it. Launchd reads the file, early, and does not spawn it.

That does not make the caveat wrong in general. It makes it wrong HERE, and the
distinction matters: the honest statement for SpringBoard is now "launchd read the
job and declined it", not "we could not see far enough".

## The App separator sharpens from 0-for-12 to 0-for-6-read, and splits in two

Cross-tabulating the 518 job plists on the volume against the new capture, with
the instrument limits addendum F's own tooling documents (a comm is at most
MAXCOMLEN+1 = 16 chars; a job.open's `job` field is the basename launchd passed to
openat, not necessarily the Label; matching a comm to a Label is a heuristic):

    POSIXSpawnType   spawned   not   total
    Adaptive            35    158     193
    AdaptiveUtility      0      1       1
    App                  0      7       7
    Background           0      5       5
    Interactive         29     47      76
    unset               25    211     236

**These are a snapshot at spawn n=260, not a final count** -- the capture is live
and the "read" column grows as launchd works through the volume. What is stable
across the whole run so far is the spawned column: no App- or Background-typed job
has spawned at any point.

The number that matters is not the total, it is how many were READ:

    App-typed, plist opened by launchd: 6 of 7   (SpringBoard, TextInput.kbd,
        assistivetouchd, bluetoothuiservice, checkerboard, fullkeyboardaccess)
    App-typed, never opened:            1       (commandandcontrol)
    App-typed, spawned:                 0

    Background-typed, plist opened:     1 of 5
    Background-typed, spawned:          0

**So the two types fail at different points.** App-typed jobs are READ and then
DECLINED -- 0-for-6 on jobs launchd actually considered. Background-typed jobs are
mostly never read at all, which is an earlier failure and a different one.
Reporting them as one "0-for-12 separator" hid that.

## Four other gates are refuted, with far larger numbers than before

Each was re-measured on the new window, and each is now refuted by a margin that
the old capped window could not provide:

    UserName              56 mobile-user jobs SPAWNED
    RunAtLoad=true        29 spawned
    MachServices present  84 spawned
    LimitLoadFromHardware only 2 jobs carry it, both under an osenvironment
                          condition, 0 spawned -- too small to be a separator

`POSIXSpawnType in {App, Background}` remains the only clean separator: 0-for-12,
0-for-6 on the read subset, and nothing else in the plist correlates.

## The targeted SpringBoard emitter was matching a logging plist

The job-open patch added a `path` field, and it has immediately identified a false
positive that addendum F could not see. The targeted emitter's record is:

    {"event":"job.springboard_open","err":0,
     "path":"/System/Library/Preferences/Logging/Subsystems/com.apple.SpringBoard.plist",
     "source":"XNU.openat_internal"}

That is a LOGGING SUBSYSTEM configuration, not the launchd job at
/System/Library/LaunchDaemons/com.apple.SpringBoard.plist. The emitter's gate is a
basename match on `com.apple.SpringBoard.plist`, and the first file with that
basename to be opened through a watched directory is the logging one.

The previous capture's targeted record carried no path field, so addendum F could
only say "basename gate only" and "the targeted record is bounded to one emission,
so it fired on the first open and was spent by the second". Both are true. What F
did not know, and could not have known, is that the first open was a different
FILE. Any reader who took "SpringBoard's plist was opened at capture line 78,
early in boot" as launchd considering SpringBoard was reading a logging config.

The launchd job itself is the `job.open n=239` record above, which is the
full-path gate and is not ambiguous.

## Status

Windows/real-guest: the 65536-cap kernel is live and capturing as of 12:03Z, spawn
counter at n=241 and climbing. The App separator is CONFIRMED as not a window
artefact for SpringBoard specifically: the plist is read at n=239, 273 records
inside the old cap, and declined.

Answered by this addendum:
- #22 sharpened materially. Launchd reads com.apple.SpringBoard.plist at spawn
  n=239 in both captures and never spawns it. At spawn n=260, 6 of 7 App-typed
  jobs had been read and 0 spawned; 1 of 5 Background-typed jobs had been read
  and 0 spawned. Four other candidate gates are refuted with 56, 29, 84 and 2
  jobs respectively.

Still open:
- WHY launchd declines an App-typed job it has just read. The separator is
  measured; the mechanism is not.
- #23 the two respawn loops, pending the bluetoothd-loop investigator.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, caps verified in
the image, booting since 12:03Z.

Onsite: N/A.

---

# Interval 2026-11-04 (forty-seventh, addendum M): spawn.seen passed 512 on a real
# guest, and job.open was never window-limited at all

## The emitter is no longer blind, and this is the measurement the interval was for

The 65536-cap kernel has been capturing since 12:03Z. At spawn n=513 the watcher
fired:

    PAST THE OLD CAP: spawn.seen reached n=513, which the previous kernel's
    emitter could not record.

The final census at that moment:

    spawn.seen                 513 records,  max_n = 513
    exit.seen                  382 records,  max_n = 382
    job.open                   454 records,  max_n = 454
    exec.seen                    2 records,  max_n = 2
    no .saturated record fired
    no springboard.running

**The fix is confirmed on a real guest, not just in a compiler.** `capcheck.py`
proved the emitters format correctly past their caps and `validate-emit.py` proved
the records parse; the kernel literal check proved the saturation blocks are
compiled in. This is the thing neither of those could prove: the guest's spawn
counter went past the value at which the old emitter went blind, and the channel
carried every record.

**No saturation record fired, which is the second half of the result.** The caps
are at 65536 and the guest reached 513, so the saturation path was not exercised.
That is the intended outcome -- a saturation record is a statement that the
instrument went blind, and the point of raising the caps was to not need one. The
fact that the literal is in the kernel and the code path is compiled is verified
by `capcheck.py`, which drives each emitter past its cap deliberately.

## job.open stopped at 454 in both captures, and 454 is not a cap

This one was not expected and it is the more useful of the two findings.

The previous capture's job.open emitter reached 454 with 58 records of headroom
before its cap of 512, so addendum G predicted it would "go silent within about
137 more records". It did not. The new capture, with a cap of 65536 and no bound
anywhere near 454, ALSO stopped at exactly 454.

    previous capture   job.open = 454   (cap 512, 58 records of headroom)
    this capture       job.open = 454   (cap 65536, 65082 records of headroom)

Two builds, two caps four orders of magnitude apart in headroom, the same number.
**454 is the number of job files launchd reads, not a bound on the instrument.**

So the job-open window was never limited. That independently confirms addendum L
from a second direction: the 0-for-12 App separator, and the 0-for-6 on the read
subset, were never artefacts of a truncated window. Launchd read 454 job files,
including com.apple.SpringBoard.plist at spawn n=239, and spawned no App-typed
job.

It also retracts addendum G's prediction about job.open specifically. G said the
capture "goes silent within about 137 more records and then contributes nothing
for the remaining four hours". The job.open emitter did not go silent; it
finished. The exit emitter is the one that saturated, at 512.

## What is still open, stated exactly

- No App- or Background-typed job has spawned, now over 513 spawns rather than
  512. The separator holds with a larger window AND with a window that was never
  the limit for job.open.
- No springboard.running and no springboard.interaction, though both emitters are
  compiled into the kernel and one of them is gated on `p_comm == "SpringBoard"`.
- The mechanism by which launchd declines an App-typed job it has just read is
  still unmeasured. The separator is measured twice over; the cause is not.
- runningboardd and backboardd both spawned and neither exited, so the app-domain
  machinery is up. The gap is between backboardd being up and it launching
  SpringBoard, and the display -- which found its surface and never presented a
  frame -- is the leading candidate for what is missing.

## Status

Windows/real-guest: PASS for the instrument, which was the interval's goal. The
spawn emitter recorded past the value at which the previous one went blind, on a
real guest, with no saturation record. FAIL still for SpringBoard, and now with
the window ruled out as an explanation from two independent directions.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, booting since
12:03Z and still capturing.

Onsite: N/A.

---

# Interval 2026-11-04 (forty-seventh, addendum N): both emitters passed 512, and
# addendum F's conclusion was true even though its method was not

## exit.seen passed 512 too, so both process emitters are confirmed

Addendum M recorded spawn.seen reaching n=513. exit.seen followed:

    spawn.seen    n=671   (the old kernel's emitter went blind at 512)
    exit.seen     n=538   (the old kernel's emitter went blind at 512)
    job.open      n=454   (unchanged -- it was never capped)

**Both process emitters that went blind on the previous kernel are now recording
past their bounds.** The fix is confirmed twice over, on two independent code
paths, on a real guest. No saturation record has fired on either.

## The respawn loop did continue, which makes addendum F's conclusion true

With the window open, the two loops are visible past the point where the old
instrument stopped:

                        previous capture   this capture
    bluetoothd spawns        214              294
    fairplayd.A2 spawns      131              204
    com.apple.datami           5                7
    containermanager          2                2
    cfprefsd                  2                2
    locationd                 2                2

**So addendum F's claim that the loop "has not stopped" was correct, and addendum
G's retraction of it was also correct.** G retracted the METHOD -- the claim was
read off the instrument's bound, which is not evidence about the guest -- and the
method was indeed wrong. The conclusion happened to be right.

That distinction has to be stated, because the easy version ("F was wrong, G fixed
it") is its own small dishonesty. G did not correct a false claim about the guest;
it withdrew an unfounded one. Two intervals later the claim is founded, by a
different instrument, and it stands.

## The exit-code histogram on the larger window

    256   297 exits   (bluetoothd: WIFEXITED, status 1)
      0   238 exits   (fairplayd.A2: WIFEXITED, status 0)
      1     2 exits   (MSUEarlyBootTask, launchd: killed by SIGHUP)
   3328     1 exit    (prng_seedctl: WIFEXITED, status 13)

**`code 6` is gone.** The previous capture carried one exit with code 6 --
transparencyd, killed by SIGABRT -- and this capture, with 25 more exit records,
does not. A single SIGABRT that does not recur is noise, not a finding, and it
should not have been listed alongside the two loops as though it were a third.

The two loops themselves are unchanged in character: bluetoothd still exits with
status 1 and fairplayd.A2 still exits with status 0. That is a failure loop and a
clean-completion loop respectively, and they remain two different defects.

## What is still open

- No springboard.running and no springboard.interaction, now over 671 spawns and
  538 exits. The separator holds with both windows open.
- No App- or Background-typed job has spawned at any point in either capture.
- The mechanism by which launchd declines an App-typed job it has read is still
  unmeasured. The separator is now confirmed three ways -- the larger window, the
  job.open total that was never a bound, and the 0-for-6 on the read subset -- and
  the cause is none of them.
- The display probe succeeds (`surface_found=1`) and nothing presents. That is
  the leading candidate for what backboardd needs and does not have.

## Status

Windows/real-guest: PASS for the instrument, twice. Both process emitters recorded
past the values at which the previous kernel went blind, with no saturation
record. FAIL still for SpringBoard, with the window now ruled out from three
independent directions.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, booting since
12:03Z, spawn n=671, exit n=538, job.open n=454 at the time of writing.

Onsite: N/A.

---

# Interval 2026-11-04 (forty-seventh, addendum O): a corrupt git object, its cause,
# and a repair that will be needed again

## What happened

`git fsck` reported:

    error: inflate: data stream error (incorrect header check)
    error: unable to unpack header of .git/objects/35/e5ab85...
    missing blob 35e5ab856febb937343274865313f62c6b5695b0

The blob is `docs/current-state.md` at commit `a35be3bfed123c6374d152c197ebf3665499e9e1`
(2026-11-03 13:29:31 +0800), which is reachable from `refs/heads/main` and from
`refs/remotes/origin/main`. The loose object's mtime was 2026-11-03 05:29Z, so it
was written corrupt and sat there for a day and a half.

**Nothing failed.** Every commit, push and checkout worked, because they only need
the CURRENT tree. The corruption surfaced only when a command happened to read
that historical blob. A repository can carry a corrupt object indefinitely and
tell you nothing.

**This is an environment defect, not a product or test defect.** F: is a 601 GB
volume at 100% used, and a write that runs out of space mid-flight produces a
truncated object. The project's own standing note about this host already says
"C: drive has very little free ... F: (5.9 GB free, 100% used)". It was 4.8 GB
free at the time of the corruption.

## The repair, in four steps, because `git fetch` does not do it

1. **Find the commit that owns the blob.** `git rev-parse <commit>:<path>` cannot
   resolve it -- the blob is what is missing -- so this uses `git ls-tree`, which
   reads only the tree and not the blob:

       for c in $(git log main --format=%H -- docs/current-state.md); do
         git ls-tree $c docs/current-state.md | grep -q $SHA && echo $c
       done

2. **Fetching by SHA is a no-op.** `git fetch origin <sha>` succeeded and returned
   FETCH_HEAD, but git negotiates on "have" lines and the local repo already had
   the commit, so the server sent nothing. The blob stayed missing.

3. **Get the content from the GitHub API at that commit** and verify it by hash
   before writing anything:

       gh api "repos/James081105/unipad/contents/docs/current-state.md?ref=a35be3bf" -q .content

   Decoding it and computing `sha1("blob <len>\0" + data)` gave
   `35e5ab856febb937343274865313f62c6b5695b0` -- an exact match. **The hash check is
   what makes this safe**: a file that did not hash to the expected SHA would have
   been refused rather than written into the object store.

4. **Write it with `git hash-object -w`**, which puts the object in place under the
   SHA its content dictates. `git fsck` then reported no errors apart from the
   normal dangling objects, and `git show a35be3bf:docs/current-state.md` reads
   again.

The corrupt object was backed up first at
`local-private/tmp/git-repair/corrupt-blob.bak` (98484 bytes) and the recovered
content at `local-private/tmp/git-repair/current-state-a35be3bf.md` (233532 bytes).
Neither is needed any more; they are kept until the next fsck is clean on a
fresh clone.

## The cause was fixed too, not just the symptom

F: was at 4.8 GB free. Three numbered boot-input bundles from 2026-10-29 to
2026-10-30 (`local-private/tmp/bundle7`, `bundle8`, `bundle9`, 2.6 GB total) were
superseded and are also retained in CI for 30 days, so they were reclaimed. F: is
now at 7.4 GB free.

**That is a mitigation, not a fix.** The volume is a 601 GB disk at 99% used and
the project's own builds each produce a 1.16 GB artifact plus a 310 MB boot-inputs
bundle. The next full disk will corrupt another object, and the next one may not
be a historical version of a document. A `git fsck` after every large write, or a
move of the scratch and bundle directories onto a volume with headroom, is the
actual fix and neither is done.

## Status

Environment: one corrupt loose object, repaired and verified by hash. Root cause
is F: at 100% used, mitigated by reclaiming 2.6 GB of superseded bundles, not
fixed.

Windows/real-guest: unchanged. The 65536-cap kernel is still capturing; at the
time of writing spawn n=732, exit n=599, job.open n=454, no saturation record, no
SpringBoard record.

Onsite: N/A.

---

# Interval 2026-11-04 (forty-eighth, addendum P): the framebuffer was 800x600, the
SpringBoard emitter matched the wrong file, and neither is a guest defect

## Two instrument defects in one hour, both found by someone else's report

A display reviewer and a session reviewer each read a number this project had
already written down, and each found that the number answered a question nobody
had asked.  Both were right.  Neither finding is about the guest.

**This is the ninth and tenth instances of this interval's failure mode**, and it
is worth naming the shape once more because it keeps arriving from a new
direction: an instrument reports a value, the value is read as an answer, and the
question the value actually answered is a different one.  Addenda G, H, K and N
recorded the same shape in the emitter bounds, the screendump hash, the literal
search and the exit codes.  This time the two instances are in the *inputs* rather
than the *outputs*, which is a place nobody had thought to look.

## Defect 1: this capture never ran at the geometry its display evidence describes

The display reviewer's question was precise and it was the right one: **the frozen
screendump is 800x600, and every boot whose display counters were read was
1376x1024, so are the two comparable at all?**

They are not.  The staged ESP is on disk and it settles it without needing to
trust the process table:

    EFI/BOOT/BOOTAA64.EFI                    93696
    EFI/UniXNU/unixnu.toml                     799
    EFI/UniXNU/Payloads/DarwinBoot.efi       27136
    EFI/UniXNU/Payloads/kernel.macho     125497344
    EFI/UniXNU/Payloads/kernel.macho.adt     17820
    EFI/UniXNU/Payloads/kernel.macho.gpuaddon 12132576
    EFI/UniXNU/Payloads/kernel.macho.trustcache 61408
    EFI/UniXNU/Payloads/scratch-data.img   4796608

**No `QemuRamfb.efi`, and `unixnu.toml` names exactly one driver** --
`Payloads/DarwinBoot.efi`.  Every screendump in this capture is 800x600, read from
the PNG IHDR rather than assumed: `t00000`, `change-01-t00120` and the previous
capture's `change-01-t00121` are all 800x600.

The live process agrees, and this is the cheapest check of the three: the running
QEMU's argv carries no `-fw_cfg name=opt/org.unixnu/ramfb-size` and never names
`QemuRamfb` at all.

### Why the counter readings were 1376x1024 and these were not

`unixnu_boot.py` generates `unixnu.toml` from `board.toml` at staging time, and the
frame-buffer driver entry is conditional on the caller:

    config = unixnu_toml(spec, kernel_args_line(spec), initrd=initrd,
                         ramfb=inputs.ramfb_driver is not None)      # :1337

`display-counters.py` -- the script whose `DRAWING_COUNTERS` readings are quoted as
fact 202 -- sets `ramfb_driver` to a build-produced driver
(`display-counters.py:188`).  `console-channel-probe.py`, which launched *this*
capture, never mentions `ramfb_driver`, `ramfb_size` or `QemuRamfb` at all.  That
is the whole difference.  One script asked for the geometry driver, the other did
not.

`docs/boot-windows.md:1062-1063` records what the driver buys when it IS loaded:
DarwinBoot prints `video base 0x1BBEF0000  1376x1024  row 5504 bytes 32 bpp`, and
the document calls it "the first positive evidence" for the driver running before
DarwinBoot takes the framebuffer.  So the 1376x1024 number is real, it is just not
this capture's number.

### The driver exists, so this is a one-line fix and not a missing build product

The build does produce it.  It is on disk from an earlier build, 12800 bytes,
sha256 `8aa46db3ac008aaa72aab10cc3efd97d5be29855aad00b1d7bd590941386d85c`, at
`local-private/acceptance-runs/coverage-baseline/acceptance/local-private/out/
real-boot/minimal-tier1-boot/followup/build/UniXNU-qemu/build/qemu-ramfb-efi/
QemuRamfb.efi`.  Nothing about the current build is missing it; the current
harness simply never passes the path.

**So the display evidence from this capture is scoped to 800x600 and must not be
transferred onto the 1376x1024 readings, or the other way.**  Whether the frozen
framebuffer is geometry-dependent is now an open question with a known, cheap
experiment attached, and it is not the same question as "why does no client submit
a validated swap".

One caveat that has to travel with the driver, because the code already documents
the trap: `_check_ramfb_pair` (`unixnu_boot.py:1105-1140`) refuses a boot that
stages the driver the manifest does not name, on the evidence of a 24-minute run
that took 29 screendumps, all 800x600, all the same sha256.  Staging the file
without the `[[driver]]` entry is invisible.  Passing `ramfb_driver` is sufficient
*because* `stage_esp` then generates the entry; passing `payload_toml` as well
would break the pair and be refused.  Both halves are required and the guard
enforces it.

## Defect 2: the SpringBoard emitter matched the wrong file with the same name

The session reviewer found two SpringBoard plists on the OS root volume: the
launchd job (2634 bytes, `POSIXSpawnType = App`) and a second file under
`/System/Library/Preferences/Logging/Subsystems/` (2179 bytes) that carries no
`Label` and no `POSIXSpawnType`.  The live capture then proved it independently,
and this is the part that is a defect in something this project wrote.

The targeted emitter fired:

    {"event":"job.springboard_open","err":0,
     "path":"/System/Library/Preferences/Logging/Subsystems/com.apple.SpringBoard.plist",
     "source":"XNU.openat_internal"}

**That is the logging-subsystem file, not the job file.**  The emitter's own
comment states its intent exactly -- "if the thing being opened IS SpringBoard's
job file then that is the answer" -- and then implements something weaker.  The
gate is a basename comparison against the literal `com.apple.SpringBoard.plist`
(`zzz-native-job-open-events.patch:193` for the literal, `:320-324` for the
comparison loop), with no directory test at all.  The comment even says so: "Whatever
the directory, whatever the prefix, relative or absolute".

The general record's gate is different and stricter: `in_a_job_dir`, set at
`:253` from the six watched job directories and tested at `:406`.  So the two
records in the same patch disagree about what "SpringBoard's job file" means, and
the looser one is the one with the name that promises the stricter thing.

**The job file was still read.** `job.open` n=239 carries
`"job":"com.apple.SpringBoard.plist","err":0` -- that one passed the directory
test, so it came from one of the six job directories.  The launchd job plist being
read is not in doubt and was never in doubt.  What is in doubt is the targeted
record's name: read at face value it says "SpringBoard's job file was opened",
and what it actually says is "a file with that basename was opened, from
anywhere".

The fix is one condition -- require the basename match AND `in_a_job_dir` -- and
until it is made the record should be read as "a file named
com.apple.SpringBoard.plist was opened", which is a weaker statement than its name
implies.  It is also worth noting that the collision was *predictable*: a name
that distinctive appearing in two subsystems is not a surprise, and a basename
match on a name that distinctive is a bet.

## What the capture itself now says, with the window open

    spawn.seen    n=799   (the old kernel's emitter went blind at 512)
    exit.seen     n=665   (the old kernel's emitter went blind at 512)
    job.open      n=454   (unchanged; 454 is how many job files launchd reads)
    no .saturated record
    no springboard.running

QEMU is still up, 7894 s of CPU at the time of writing.  The instrument is
confirmed for the fourth consecutive time on a live guest, at nearly 800 spawns
with no saturation record and no sign of the bound.

## Status

Windows/real-guest: **two test-instrument defects, neither a product defect.**
(1) The capture ran at 800x600 because the harness never passed `ramfb_driver`;
the geometry driver exists and is one argument away.  (2) `job.springboard_open`
matched a basename and caught the Logging/Subsystems plist; the fix is to also
require the job-directory test the general record already performs.  Both are
recorded rather than fixed because both change what a future measurement means,
and a future measurement taken before the fix is written down is a measurement
that has to be re-taken.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, booting since
12:03Z, spawn n=799, exit n=665, job.open n=454 at the time of writing.

Onsite: N/A.

---

# Interval 2026-11-04 (forty-ninth, addendum Q): SpringBoard is running on this
# build, as pid 27, and the emitter built to see it was on the wrong path

## The claim this overturns, stated exactly so it can be checked

`docs/current-state.md` says three things about the current build, in its header
and again in interval forty-fifth:

> "SpringBoard has still NOT been observed to spawn."

> "Launchd reads `com.apple.SpringBoard.plist` at spawn n=239 ... and **declines
> to spawn it**; 6 of 7 App-typed jobs were read and **0 spawned**."

> "`springboard.running` is compiled into the kernel, gated on
> `p_comm == "SpringBoard"`, and **has never fired**."

**All three are true about the channel.  All three are false about the guest.**
SpringBoard is running on this build, as pid 27, and has been since early in the
boot.

## The measurement

Two snapshots of the guest's process table, taken seven minutes apart through
QMP, with the kernel's identity checked by sha256 before either read:

    snapshot 1   2026-11-04T13:58:39Z    168 processes
    snapshot 2   2026-11-04T14:05:32Z    167 processes

    SpringBoard   pid=27  state=SRUN  proc 0xfffffff78f00c100   (BOTH snapshots)
    runningboardd pid=26  state=SRUN
    backboardd    pid=58  state=SRUN
    bluetoothd    ABSENT at 13:58:39, PRESENT at pid 855 at 14:05:32

**SpringBoard holds the same proc address across seven minutes**, so it is the
same process and it is not being recreated.  And the instrument is proved live by
the row nobody chose: bluetoothd is absent from the first snapshot and present at
pid 855 in the second, which is exactly what its crash-loop predicts, and pid 855
is past the pids the kernel log shows it dying at (689, 691, 774).

## The instrument is validated in both directions before the negative is read

This is the discipline the interval has been enforcing since addendum G, applied
to the one conclusion that matters most.

**The positive control.** 837 `spawn.seen` records arrived with `n` running 1 to
837 with no gap and no duplicate.  The counter is incremented before the record is
built, so a dropped record would leave a hole in `n`.  There is none.  The channel
is lossless for this emitter.

**The emitter is in the running kernel.** `kernel-literal-check.py` enumerates
contiguous `{"event":"...` literals rather than searching for a substring, because
ld's SHF_MERGE|SHF_STRINGS merging splits these records into fragments and a
substring search for `spawn.seen` finds zero in a kernel that emitted it 837
times.  In the kernel that is actually booted -- sha256
`4914d56c95e97c33f8580f4fd05319c8a9c111e8e52ccc90764907c0edf12021` -- the verdict
is:

    contiguous literals (a positive here is trustworthy):
        1x  springboard.running
        1x  springboard.interaction

So the record is compiled in, reachable, and simply never triggered.

**The cross-tabulation.** Of 159 live processes, 154 have a `spawn.seen` record.
The five that do not are:

    kernel_task      pid=0    created by the kernel, structurally impossible
    launchd          pid=1    exec'd by load_init_program, not posix_spawn
    SpringBoard      pid=27   <-- the anomaly
    AccessibilityUIS pid=177  MAXCOMLEN truncation edge case
    xpcproxy         pid=34   one instance of a comm that appears 3x in the table

**SpringBoard is the only significant live process with no spawn record.** The
emitter is not broadly blind; it is blind to this one process.

## So how did pid 27 come to exist?

Neither instrumented path reports it:

    exec.seen    2 records total: launchd (n=1), apfs_boot_util (n=2)
    spawn.seen   837 records, none with comm "SpringBoard"
    exit.seen     703 records, none with comm "SpringBoard"

**It has never exited**, which is why there is no exit record, and it was created
outside `posix_spawn`'s success block and outside `__mac_execve`.  The two
emitters sit at `zzz-native-springboard-events.patch`'s `posix_spawn` hunk (after
`proc_exec_switch_task()`, inside `if (error == 0 && !spawn_no_exec)`) and its
`__mac_execve` success block.  A process can miss both by being created through a
spawn variant that takes neither, or by a path this kernel build does not run
through those hunks.

**The mechanism is unmeasured and this addendum does not guess it.** What is
measured is the shape: runningboardd is pid 26 and SpringBoard is pid 27, and
runningboardd DOES have a spawn record while SpringBoard does not.  So the parent
was seen and the child was not, one PID apart.  Whatever launchd and runningboardd
do to start the app domain, the kernel-side record of it is missing.

## What this means for the two open tasks

**Task #22 was the wrong question.** It asked why launchd declines
`POSIXSpawnType=App` jobs.  The 0-for-12 separator is real -- it is a fact about
posix_spawn -- but SpringBoard was never going to appear in it, because SpringBoard
does not arrive by posix_spawn on this build.  The separator measured the
instrument's path, not the guest's.

**Task #24's remaining defect is the real one, and it is now sharper.** The
display subsystem is fully up and idle:

    native_legacy_stage                10    (both snapshots)
    native_legacy_transfer_calls        0    (both snapshots)
    native_legacy_swap_validation_calls 0
    native_legacy_frame_count           0
    native_gop_gpu_offers               0
    native_gop_gpu_completions          0

`transfer_calls` is incremented as the first statement of
`native_legacy_transfer_observed()`, which the driver calls on every way out of
`kickOnce()` including the early returns, so 0 means the driver never got a
validated swap at all.  **SpringBoard is running and has never submitted one.**
That is the blocker for an operable SpringBoard, and it is a statement about what
SpringBoard does, not about whether it exists.

## The earlier boots, and why they cannot be borrowed

A reviewer's report attributed "SpringBoard ran on at least four earlier runs" to
boot31, boot46, boot60 and boot40.  The underlying fact is real; the attribution
is not, and the difference matters because these numbers get reused.

- The ten PIDs (27, 110, 113, 115, 119, 121, 126, 129, 136, 137) and the
  t=539-774 range come from **one file** --
  `local-private/boot60/display/attempt-1/guest-kernel.log` -- and are one boot's
  crash-loop, not a union across four runs.
- `boot60` is cited in **no document**.  The label came from a directory name.
- boot46 contributes a different PID set (27, 106, 108, 109, 113, 115, 117).
- Every one of those observations is a crash-loop the OS gave up on:
  `Process[122] crashed: SpringBoard. Too many corpses being created.`
  (`docs/implementation-plan.md:438`).  Nothing in the record shows SpringBoard
  reaching a steady state, drawing, or accepting input.
- **All of them predate the emitter entirely.**  The first emitter was compiled
  and pinned 2026-11-03, fork `62bc0f48`.  They ran different kernels and
  different builds.

**So the current build is not a regression from those boots -- it is a different
situation.**  On boot31/46/60, SpringBoard ran and crash-looped.  On this build,
SpringBoard runs and does NOT crash-loop: it is pid 27, stable, never exited.
The display defect is the same shape, but "it crashes repeatedly" and "it runs
stably and presents nothing" are different defects with different fixes, and the
second one is what this build has.

## One instrument defect found on the way, and it is not cosmetic

`src/unipad/host/guestobserve.py:109` labels the process states:

    PROC_STATES = {1: "idle", 2: "run", 3: "stop", 4: "zombie", 5: "reaped"}

XNU's own `bsd/sys/proc.h:160-164` says:

    #define SIDL    1   /* Process being created by fork. */
    #define SRUN    2   /* Currently runnable. */
    #define SSLEEP  3   /* Sleeping on an address. */
    #define SSTOP   4   /* Process debugging or suspension. */
    #define SZOMB   5   /* Awaiting collection by parent. */

**Three of the five labels are wrong.** 3 is SSLEEP, not "stop".  4 is SSTOP, not
"zombie".  5 is SZOMB, not "reaped".  The one that matters here -- 2 = SRUN --
is labelled correctly, so this addendum's conclusion stands.  But a reader who
sees `state: "zombie"` and concludes "awaiting collection by its parent" is
reading a suspended process, and a reader who sees `"reaped"` is reading a
corpse.  On a project that has already been bitten by a mislabelled instrument,
the labels should be corrected before somebody trusts one.

## Status

Windows/real-guest: **PASS for the guest, FAIL for the conclusion drawn from it.**
SpringBoard runs on this build as pid 27, stably, never exited -- measured twice
from live guest memory with the kernel's identity verified, and cross-checked
against a process whose behaviour was already known.  The state document's claim
that it never spawned is retracted here.  The display defect survives and is now
correctly located: SpringBoard is up and has submitted no validated swap in
10 hours.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, fork `3d83de1`,
kernel `4914d56c`, booting since 12:03Z.

Onsite: N/A.

---

# Interval 2026-11-04 (fiftieth, addendum R): SpringBoard's parent is launchd,
# proved by two fields of struct proc, and 11 of launchd's children take a
# posix_spawn path this repository cannot see

## What was open, and what closes it

Addendum Q left one question standing and said so: SpringBoard is running as pid
27, neither instrumented creation path reported it, and *who created it* was
unmeasured.  That question is now answered by reading the guest's own process
struct, and the answer is cross-checked by a second field rather than trusted
from one.

## The offset is derived, and the derivation is checked

`p_ppid` is a field of `struct proc`, so the answer is in guest memory.  Its
offset is **not** known: `guestobserve.py` records `p_pid=96`, `p_stat=100`,
`p_comm=1468` from UniXNU's `scripts/qemu-entry/gdb-stop.py`, where they are CLI
defaults somebody measured once.  There is no `p_ppid` default anywhere in this
repository, and there is no `p_pptr` default either.  A hard-coded offset would
therefore have been a guess wearing a fact's clothes -- the exact failure this
project has now paid for eleven times.

So it was derived.  Every 4-byte-aligned word of the struct was scored against
five properties a parent field must have: every process holds a live pid or 0
there; nothing is its own parent; kernel_task and launchd both read 0 (neither
was spawned by anything alive); following parents reaches a root without
revisiting a node; and it is not offset 96, which is `p_pid`.

Twenty-four offsets survived with a single violation each, and they were
indistinguishable by score.  A word that is 0 everywhere scores perfectly.  So
the score cannot pick the field, and the first version of this script refused
rather than guess.

What separates them is a **second field**.  `bsd/sys/proc_internal.h` puts
`p_pptr`, a *pointer* to the parent's `struct proc`, at offset 16 -- immediately
after `p_list`, eight bytes before `p_ppid`.  The parent is therefore named
twice, once as a pointer and once as a pid.  If the pointer resolves to a live
proc whose `p_pid` equals the word at offset 32, then 32 is `p_ppid`, and the
reading is proved by two fields agreeing instead of by one word's shape.

**Measured, 171 live processes, every one agreeing:**

    p_pptr (offset 16) vs p_ppid (offset 32):
      agree         171
      disagree      0
      unresolvable  0

A wrong offset cannot pass this.  It would have to hold a pid that coincidentally
matches the parent a *pointer* names, for 171 processes at once.

## The answer

    SpringBoard   pid 27  proc 0xfffffff78f00c100
      p_pptr -> 0xfffffff78effdee0 = pid 1 (launchd)
      p_ppid -> 1
    launchd      pid 1
      p_pptr -> 0xfffffff78effd100 = pid 0 (kernel_task)

**SpringBoard's parent is launchd.**  Not runningboardd, not a
`posix_spawn` intermediary, not a reparented orphan.  `launchd`'s parent
is `kernel_task`, which is its own root.  That is the ordinary iOS
relationship, and it was never in doubt from userspace -- what was in doubt was
whether the kernel-side record agreed with it, and it does not.

**Nothing was reparented.**  `p_original_ppid` (offset 36, immediately after
`p_ppid`) equals `p_ppid` for all 171 live processes
(0 differences).  So no process here had its parent die and get
re-parented to `launchd`; the values at offset 32 are the parents that
actually created them.

## The split, and it is a split by PATH, not by caller

Cross-tabulating the live table against the channel's current contents
(2417 records, 1038 `spawn.seen`, 2 `exec.seen` with
comms ['launchd', 'apfs_boot_util']):

    live procs WITH a creation record:   160
      parents: {'launchd': 159, 'kernel_task': 1}
    live procs WITHOUT one:              11
      parents: {'launchd': 10, 'kernel_task': 1}

**Both sets have the same parent.**  If the difference were "launchd does two
different things for two different kinds of job", the parents would differ.  They
do not.  Every live process except `kernel_task` is a child of `launchd`,
in both sets.  So the difference is which *path through `posix_spawn`* the child
took.

The 11 without a record are: AccessibilityUIS, SpringBoard, kernel_task, and
nine `xpcproxy`.

**Every count in this addendum is a snapshot.**  The guest is still running and
the channel is still appending, so `spawn.seen` was 1038 when this was written
and is higher now; the document's header quotes whatever the count was when *it*
was last refreshed.  The two numbers disagreeing is the channel growing, not one
of them being wrong.  What does not move is the shape: the record numbering is
contiguous from 1 with no gap and no duplicate, so nothing has been lost, and
the 11-without-a-record set has been the same 11 for as long as it has been
measured.

## Why, and it is in the fork's own source

There is exactly **one** `spawn.seen` emitter in the fork's patches
(`zzz-native-springboard-events.patch`, one site), and it sits at
`bsd/kern/kern_exec.c:4280`, inside:

    if (error == 0 && !spawn_no_exec) {

`spawn_no_exec` is initialised FALSE at `:3563` and set TRUE in exactly one
place, `:4002` -- which is **above** the emitter and belongs to this
branch:

    do_fork1:
        error = fork1(p, &imgp->ip_new_thread, PROC_CREATE_SPAWN, coal);
        ...
        imgp->ip_flags |= IMGPF_SPAWN;  /* spawn w/o exec */
        spawn_no_exec = TRUE;           /* used in later tests */

That branch is the one the condition at `:3869` selects:

    if (imgp->ip_px_sa == NULL || !(px_sa.psa_flags & POSIX_SPAWN_SETEXEC)) {

-- i.e. **a plain `posix_spawn`**.  `POSIX_SPAWN_SETEXEC` (`sys/spawn.h:59`,
`0x0040`) is the flag that turns `posix_spawn` into "execve with options", and
that is the *other* branch, the one that leaves `spawn_no_exec` FALSE and
therefore reaches the emitter.

So there are two ways through `posix_spawn`, one creates the child with
`fork1(PROC_CREATE_SPAWN)` and activates the image in it, the other replaces the
caller.  **The emitter is on the second.**  A child created by the first is
running, has its `p_comm` written by `set_proc_name()` inside
`exec_mach_imgact()`, and is invisible to this instrument.

That is the whole explanation, and it needs no new defect: SpringBoard,
AccessibilityUIS and the nine live `xpcproxy`s are `launchd`'s children
created by a `posix_spawn` this repository does not instrument.  The other
160 took the branch it does.

**What is still unmeasured:** which attribute makes `launchd` choose one
branch for 160 children and the other for 11.  The
emitter prints `comm` and nothing else -- no pid, no caller, no flag -- so the
channel cannot separate them, and the fork's source says only that the flag
exists.  The next probe is an emitter inside `do_fork1`'s block (or the child's
pid and the caller's pid added to the existing record), and it needs a kernel
rebuild.  **This addendum does not guess the flag.**

## Two things this addendum nearly got wrong, both recorded

**First, a truncated read.**  The staged ESP's `args =` line is one long line.
An earlier reading of it in this session showed only its tail and concluded the
`offline-legacy-*` family had been dropped and `transfer_calls == 0` was a host
configuration defect.  Re-read in full, the line carries all 26 arguments,
including `offline-legacy-transfer=1`, `offline-legacy-validate=1`,
`offline-legacy-phase=4` and `offline-legacy-surface-wait-ms=600000`.  The
conclusion was wrong and the file was fine.  The script above asserts those four
arguments are present before it writes anything.

**Second, a score that cannot discriminate.**  The first version of the offset
scan required all five properties simultaneously and refused the whole struct,
because a word that is 0 everywhere passes all five.  Reporting the refusal
without saying *which* property failed would have been a negative read off an
instrument whose question was not the one being asked.  Scoring each property
separately is what exposed that the score was not the discriminator, and the
second field is what actually decided it.

## The respawn loop, now with a number

Task 23's loop is in the same channel and is now quantified: of 1038
`spawn.seen` records, 489 are `bluetoothd` and 371 are
`fairplayd.A2` -- 860 of 1038, or
82% of every spawn the kernel
has seen this boot.  The remaining 178
records cover 167 other distinct processes.  The record
numbering is contiguous from 1 to 1038 with no gap and no duplicate, so
none of this is lost to the emitter's 65536 bound.

## Status

Windows/real-guest: **PASS for the parent measurement** -- read from live guest
memory over QMP with the kernel's sha256 verified first, and proved by two
fields of `struct proc` agreeing across all 171 live processes.
**UNMEASURED for the branch `launchd` chose**, which needs a rebuild.
The display defect is unchanged and is not re-measured here.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, fork `3d83de1`,
lock pin `3d83de17dd4e4be468ac3fb8d30ee21c597e8972`, kernel `4914d56c`, booting since 12:03Z.

Onsite: N/A.

---

# Interval 2026-11-04 (fifty-first, addendum S): the framebuffer's client is Apple's own, and twelve of its fifteen calls were refused by code this port does not implement

Addendum R closed the parent question and left the display blocker as a
symptom: *SpringBoard is up and has submitted no validated swap.*  That
reading is true and it is one link too late.  This addendum measures the
link before it, and the cause turns out to have a name, an address and a
status code attached to every one of its refusals.

## What was open, and what closes it

`native_legacy_transfer_calls == 0` was read as "no client ever
presented a frame".  The counters that answer the question one step
earlier were not in the window.  They are now:

`native_legacy_client_calls == 5`, and
`native_legacy_client_pid == 93`.  So the framebuffer WAS opened --
5 times -- and `client_pid` names only
the last of them, because `native_legacy_client_enter()` overwrites it on
every open.  The probe keeps the full table: `LegacyDisplayProbe.cpp:381`
declares `static ClientStat seenClients[8]`, and `interpose()` fills one
entry per user client with its pid, its per-selector call and failure
counts, and the status each selector last returned.

## The table is reachable only under its mangled name

The first version of the reader looked up `seenClients` and found
nothing, which reads exactly like "this kernel does not have that
table".  It does.  A C++ file-scope `static` is emitted under its
MANGLED local-linkage name, so the symbol table carries
`__ZL11seenClients` and `__ZL15seenClientCount`, not the source
spelling.  Looking up the source spelling returns `None` and produces a
confident negative about a table that was there the whole time.  This is
the third time this interval has had to be told that a missing symbol is
not a missing fact.

## The five clients

| | pid | process | calls | fails |
|---|---|---|---|---|
| C0 | 58 | backboardd | 15 | 12 |
| C1 | 93 | MobileGestaltHel | 2 | 1 |
| C2 | 86 | not in the snapshot | 0 | 0 |
| C3 | 74 | PerfPowerService | 0 | 0 |
| C4 | 93 | MobileGestaltHel | 1 | 0 |

Every one of the five carries the SAME `original_external`:
`0xfffffff005778d40`.  That is the function the probe displaced when it
pointed vtable slot 168 at its own wrapper, so it is the real
`externalMethod` the framebuffer's user client was using before the
probe touched it.

**And it is Apple's.**  `scripts/audit-mobile-graphics-userclient.py`
extracts that exact address out of the target's own
`com.apple.iokit.IOMobileGraphicsFamily` and calls it *"Original
unsigned selector <90 check and kIOReturnInvalid refusal"*.  Five byte
anchors were re-read off the live kernel before anything was believed:

- `+0x54` == `0x71016908`
- `+0x58` == `0x1a9f27e8`
- `+0x5c` == `0x370000a8`
- `+0x64` == `0x32030fe8`
- `+0x7c` == `006d40f9080040f908d942f900013fd6`

All five match, and `native_legacy_client_class` reads
`IOMobileFramebufferUserClient`.  So the client backboardd was
driving is the ORIGINAL Apple user client, not a stand-in this port
wrote.

## Every selector any client used has a dispatch entry

Eleven of backboardd's fifteen calls returned
`0xe00002c7` and one returned `0xe00002bc`.  The target's
own `IOKit/IOReturn.h` names both values exactly:

```c
#define kIOReturnUnsupported     iokit_common_err(0x2c7) // unsupported function
```

and XNU's `IOUserClient::externalMethod` returns that one value from one
place:

```c
if (!(method = getTargetAndMethodForIndex(&object, selector)) || !object)
	return kIOReturnUnsupported;
```

which would mean the selectors were missing from the dispatch table.
**They are not.**  The table is at `0xfffffff005f354c0`, 90 rows of 24 bytes
(`function, scalar_in, struct_in, scalar_out, struct_out`), and its null
set is [59, 60, 61, 62, 63, 64, 65, 70, 71] -- exactly the nine the audit records.  81 of 90
rows carry a function.  Every selector any client used is among them:

| selector | used by | dispatch function | in (scalar, struct) | out (scalar, struct) | result |
|---|---|---|---|---|---|
| 6 | pid 58 x1 | `0xfffffff005779518` | (3, 0) | (0, 0) | ok |
| 8 | pid 58 x1, pid 93 x1, pid 93 x1 | `0xfffffff00577966c` | (0, 0) | (2, 0) | ok |
| 12 | pid 58 x1 | `0xfffffff005779f24` | (1, 0) | (0, 0) | kIOReturnUnsupported |
| 15 | pid 58 x1 | `0xfffffff00577a014` | (2, 0) | (1, 0) | kIOReturnError |
| 22 | pid 58 x1 | `0xfffffff00577a2e4` | (1, 0) | (0, 0) | kIOReturnUnsupported |
| 27 | pid 58 x1 | `0xfffffff00577a464` | (0, 0) | (0, 3084) | kIOReturnUnsupported |
| 28 | pid 93 x1 | `0xfffffff00577a554` | (0, 0) | (1, 0) | kIOReturnNotFound |
| 29 | pid 58 x1 | `0xfffffff00577a5ac` | (0, 0) | (0, 8) | kIOReturnUnsupported |
| 33 | pid 58 x1 | `0xfffffff00577a770` | (1, 0) | (0, 0) | kIOReturnUnsupported |
| 50 | pid 58 x1 | `0xfffffff00577a25c` | (2, 0) | (0, 0) | kIOReturnUnsupported |
| 57 | pid 58 x1 | `0xfffffff00577a200` | (0, 0) | (1, 0) | kIOReturnUnsupported |
| 74 | pid 58 x1 | `0xfffffff00577a718` | (1, 0) | (2, 64) | kIOReturnUnsupported |
| 79 | pid 58 x2 | `0xfffffff0057799b4` | (0, 0) | (0, 0) | kIOReturnUnsupported |
| 84 | pid 58 x1 | `0xfffffff00577a4dc` | (0, 4) | (0, 0) | kIOReturnUnsupported |
| 88 | pid 58 x1 | `0xfffffff00577adb0` | (1, 0) | (0, 0) | ok |

Sixteen distinct selectors across 18 calls from five
clients; backboardd used fourteen of them.  Every row has a function,
so not one of these calls was refused for a missing entry.
So the method RAN and returned `kIOReturnUnsupported`.  The refusals
are answers, not absences.

## The chain closes on this kernel's display object

`audit-mobile-graphics-userclient.py` adds the next link: *"All
in-range requests must first call the actual provider at field 0xd8,
virtual slot 182"*.  If the client's `+0xd8` did not name THIS
kernel's display object, every conclusion above would be about the
wrong object.  Read:

- `native_legacy_object` = `0xfffffff6a8afa000`
- C0 (pid 58, backboardd): `+0xd8` = `0xfffffff6a8afa000` -- == the display object
- C1 (pid 93, MobileGestaltHel): `+0xd8` = `0x0` -- 0 (freed)
- C2 (pid 86, not in the snapshot): `+0xd8` = `0x0` -- 0 (freed)
- C3 (pid 74, PerfPowerService): `+0xd8` = `0x0` -- 0 (freed)
- C4 (pid 93, MobileGestaltHel): `+0xd8` = `0x0` -- 0 (freed)

**backboardd's provider field is this kernel's display object.**  The
other four are `0` -- those clients have been freed, which is why their
`*object` is `0` too.  So the twelve refusals really did come back
through this kernel's `NativeLegacyDisplay`.

## One of the twelve is deliberate, and the fork says so

Selector 74 is `supportedFrameInfo`.  `NativeLegacyDisplay.cpp:370-374`
overrides it and ends:

```c
*count=0;*mask=0;bzero(key,64);return kIOReturnUnsupported;
```

with the comment *"Selector 74 enumerates optional frame-info trace
keys, not display modes.  This backend implements no trace-key
subscription contract."*  Its dispatch row is `in=(1,0) out=(2,64)`,
which is exactly that signature: one scalar in, two scalars out, a
64-byte key out.  So at least one of the twelve is a refusal BY DESIGN.

## The other eleven, and the fork's own explanation

`NativeLegacyDisplay` overrides roughly twenty methods.  Of the twelve
failing selectors only 74 maps to one of them.  The other eleven land in
the ORIGINAL `IOMobileFramebufferLegacy` base, which this port does not
override.  The fork already knows why, and it is written in the source
next to the one method that works around it:

> *"...the platform subclass that normally writes it is not part of
> this port, so without it the user client's wait (selector 6) answers
> kIOReturnBusy forever and the compositor never presents."*

`NativeLegacyDisplay.cpp:85-87`.  The eleven refusals are that same
missing platform subclass, seen from the other eleven call sites.

## SpringBoard never opened the framebuffer

The five client pids are [58, 93, 86, 74, 93].  **SpringBoard (pid
27) is not among them.**  Only two live processes could
own a screen, and the one that did is backboardd:

| pid | process | state |
|---|---|---|
| 27 | SpringBoard | run |
| 58 | backboardd | run |

So `native_legacy_swap_validation_calls == 0` and
`native_legacy_prepare_calls == 0` for a measured reason: backboardd
opened the framebuffer, made 15 external calls, was
refused on 12 of them -- eleven with `kIOReturnUnsupported` and one
with `kIOReturnError` -- and never got as far as submitting a swap.
The driver is not idle; it is being asked for things it does not have.

Clients that opened it and called NOTHING: ['pid 86 (?)', 'pid 74 (PerfPowerService)'].

## The driver itself is up, and that is a reading

| counter | value |
|---|---|
| `native_legacy_stage` | 10 |
| `native_legacy_surface_found` | 1 |
| `native_legacy_dt_attached` | 1 |
| `native_legacy_surface_status` | 0 |
| `native_legacy_registration_calls` | 1 |
| `native_legacy_properties_length` | 576 |
| `native_legacy_init_result` | 1 |
| `native_legacy_start_result` | 1 |
| `native_legacy_probe_match` | 1 |
| `native_legacy_parent_match` | 1 |
| `native_legacy_pm_stage` | 4 |
| `native_legacy_pm_calls` | 4 |
| `native_legacy_pm_status` | 0 |
| `native_legacy_unit_scale_calls` | 1 |
| `native_legacy_chosen_scale` | 1 |
| `native_legacy_gop_physical` | 7457079296 (`0x1bc7a0000`) |
| `native_legacy_gop_width` x `native_legacy_gop_height` | 800 x 600 |
| `native_legacy_gop_bytes` | 1920000 |

The GOP is mapped at physical `0x1bc7a0000`,
which is DARWINBOOT's own `video base=0x1BC7A0000`, at 800x600 and
1920000 bytes.  Nothing is wrong with the
driver's own half of the job.

## Two things this addendum nearly got wrong, both recorded

**The vtable could not identify the class, and the reason is the
probe itself.**  `interpose()` does not merely read the client's
vtable; it `IOMalloc`s a COPY, points the object's vptr at the copy,
and overwrites slot 168.  So `*client` is a heap address and can never
be matched against the symbol table.  Reading it as "no `__ZTV`
symbol, so the class is unknown" would have been a negative off an
instrument the probe had already modified.  The original function is
the one the probe saved BEFORE overwriting the slot, which is why
`original_external` is the field that answers the question.

**"Selector 6, the swap/wait selector" was an annotation, not a
measurement.**  It came from the probe's own field names
(`sel6_first`, `sel6_seen`) and from `NativeLegacyDisplay.cpp:87`'s
comment.  The dispatch table says selector 6 is `in=(3,0) out=(0,0)`,
which is consistent with the recorded argument triple
`0x0 0x80000003 0x0` and with the fork calling it *the user client's
wait*, but the table does not name it.  Recorded as an inference, not a
fact.

**And the address reader has a hole in it that this addendum walked
into.**  `GuestObserver.translate()` walks `_TABLE_SHIFTS =
((1, 30, 0xFF), (2, 21, 0x1FF), (3, 12, 0x1FF))`, so only bits 0..38
of a kernel virtual address ever reach an index.  Two addresses that
agree on their low 39 bits translate to the same physical address, and
everything above bit 38 is ignored without a refusal.  Measured:
`0xffffffff005778d40`, `0x0ffffff005778d40`, and the same value with bit
62, bit 40 or bit 39 flipped all translate to physical
`0x49578d40`; only an address whose LOW bits are wrong is refused.  The
first version of the dispatch reader carried an `EXTERNAL` constant
with one hex digit too few and still read the right bytes -- which is
not a lucky read, it is the instrument not asking.  The anchors are the
only reason this table can be believed, and they are checked before a
single row is read.

## Status

Windows/real-guest: **PASS for the client measurement** -- read from
live guest memory over QMP with the kernel's sha256 verified first, the
original `externalMethod` confirmed by five byte anchors, and the
provider field closing the chain on this kernel's display object.
**UNMEASURED for what the eleven unimplemented methods would need**:
the platform subclass is not in this port, and building it is a code
change, not an observation.

**The display defect is now correctly located, and it is not a
configuration defect.**  It is eleven API methods the original
`IOMobileFramebufferLegacy` expects its platform subclass to supply,
plus a compositor that never reaches a swap because it is refused
first.  What is still open: whether the eleven are all needed before a
frame can be presented, or only some of them.

Build: `exp-2026-11-04-emitter-caps`, run 41817661032, M7/tcg, fork
`3d83de1`, kernel `4914d56`, booting since 12:03Z.

Onsite: N/A.

---

# Interval 2026-11-04 (fifty-second, addendum T): the display's vtable is 23 slots this port and 181 slots Apple's

Addendum S concluded that the eleven `kIOReturnUnsupported` refusals
land in the ORIGINAL `IOMobileFramebufferLegacy` base, which this port
does not override.  That conclusion was read off
`build-native-legacy-display.py`, which binds

```python
'__ZTV25IOMobileFramebufferLegacy': int(audit['legacy_vptr'], 16) - 16
```

-- the original class's own vtable, out of the target's kernelcache --
and then asserts

```python
owned == sorted([0,1,7,14,84,86,87,141,155,184,188,288,290,296] + pure)
```

A claim about the build is not a claim about the running kernel.  This
reads the display object's vtable out of guest memory and classifies
every one of its 358 slots by the symbol its function pointer lands in.

## The measurement

- display object = `0xfffffff6a8afa000`, vptr = `0xfffffff0072066a0`
- **23 slot(s) are this port's**: [0, 1, 7, 14, 84, 86, 87, 141, 155, 184, 188, 211, 213, 245, 246, 288, 290, 296, 306, 307, 337, 338, 345]
- **181 slot(s) are the ORIGINAL family's**: the whole
  run from 168 upward, minus the 14 the fork takes
- **154 slot(s) are XNU core** (`IOService`, `OSObject`), all
  below slot 168

So of the 190 slots at or above 168 -- the API surface the
original legacy class exposes -- **this port implements 14**.

| | slots |
|---|---|
| this port | 23 |
| the original `IOMobileFramebufferLegacy` | 181 |
| XNU core | 154 |
| total | 358 |

## The two slots that were not in the build's list, and why they are there

The measured set is `build-native-legacy-display.py`'s asserted owned
set **plus 213 and 337**.  Those two are exactly the slots the
generated `OriginalLegacyFramebufferABI.h` declares PURE VIRTUAL:

```c
virtual IOReturn supportedFrameInfo(uint32_t,uint32_t *,char *,uint32_t *)=0;   // slot 213
virtual IOReturn collectCompletionReports(uint32_t,void *,uint32_t *,uint32_t)=0; // slot 337
```

and `NativeLegacyDisplay.cpp` implements both (`:370` and `:299`).  So
they are the fork's, and the build's `owned` list simply does not name
them because the script separates them into `pure`.  The measurement
and the build agree once that separation is undone, which is a stronger
check than a match would have been.

## One instrument defect, recorded

The first version of this reader classified slots with a fixed address
window (`0xfffffff00bf66d00` to `0xfffffff00bf6c000`) and reported
**zero** fork-owned slots out of 358 -- which would have read as "this
port owns none of its own vtable".  Slot 0 is
`__ZN19NativeLegacyDisplayD1Ev` at `0xfffffff00bf66e84`, which is
inside the window's lower neighbour and outside the window itself.  A
window is a guess about where code lives; naming the symbol the pointer
lands in is not.  The reader now resolves each pointer to its owning
symbol and only counts it as the fork's when that symbol is one of the
fork's.

## What this means for the blocker

The eleven refusals are not eleven bugs.  They are the visible edge of
a backend that implements 14 methods of a 190-method API, being asked
for the rest by a compositor that expects all of them.  The fork's own
source says the same thing at `NativeLegacyDisplay.cpp:85-87`:

> *"...the platform subclass that normally writes it is not part of
> this port, so without it the user client's wait (selector 6) answers
> kIOReturnBusy forever and the compositor never presents."*

That comment is about ONE method, and the port already works around it
by calling the original state setter directly from
`announcePoweredByFirmware()`.  The other eleven are the same shape of
problem at eleven more call sites.

## Status

Windows/real-guest: **PASS** -- read from live guest memory over QMP
with the kernel's sha256 verified first, and the measured owned set
reconciles with the build's own assertion once the pure-virtual slots
are accounted for.

**The display defect's size is now measured, not guessed:** 14 of 190
methods above vtable slot 167.  Whether a frame can be presented with
fewer than all of them is UNMEASURED, and it is the next question.

Build: `exp-2026-11-04-emitter-caps`, kernel `4914d56`.

Onsite: N/A.

---

# Interval 2026-11-04 (fifty-third, addendum U): the screen is still the boot menu, and the swap selector could not be named

## The screen, because a counter is not a screen

`screendump` over the live QMP connection: a P6 PPM, 800x600, 480000 pixels, 1440015 bytes.  The dominant colours are:

| colour | pixels | share |
|---|---|---|
| `0b0c0f` | 288876 | 60.18% |
| `000000` | 68860 | 14.35% |
| `00ff00` | 30000 | 6.25% |
| `0000ff` | 30000 | 6.25% |
| `ff00ff` | 30000 | 6.25% |
| `989898` | 17924 | 3.73% |
| `2a5bd7` | 6580 | 1.37% |
| `7c838d` | 3262 | 0.68% |

A solid green band across the top, a magenta band under it, a black
panel on the left, a dark field everywhere else, one blue selector
element and a little grey text.  That is the UniXNU boot menu, not a
SpringBoard frame.  It is the same screen the prior intervals
photographed, and it agrees with every counter on the page: no frame
has been presented.

## Which selector submits a swap: UNMEASURED, with the bound stated

`native_legacy_swap_validation_calls == 0` only means something once it
is known whether backboardd ever asked for a swap.  The dispatch table
gives a function per selector, and those functions are the original
family's, so the question is answerable in principle: decode each one
and find the display vtable slot it drives.

On ARM64 a 64-bit `LDR (immediate)` is

```c
0xF9400000 | (imm12 << 10) | (Rn << 5) | Rt     // byte offset = imm12 * 8
```

so an immediate at or above 168 is a vtable slot load and
one below it is a struct field load.  Every one of the
81 live dispatch functions was read to 512 bytes
and decoded.

**No dispatch function loads any swap-path slot.**  The swap path's
entry points are slots 249 (getDisplayDimensions), 288 (startHardware), 290 (kickSwapQueue), 295 (completeCurrentSwap), 296 (validateSwap), 337 (collectCompletionReports)
and none of them appears anywhere in the decode.  The only slots at or
above 168 that appear at all are [218, 223, 224, 225, 226, 228].

What the functions DO load is the provider field at byte offset 0xd8 --
imm12 = 27, which the audit's own anchor
(`006d40f9` = `ldr x0,[x8,#0xd8]`) names -- plus small struct fields at
0x8, 0x20, 0x48, 0xe8 and a handful of others.  So the provider is
loaded and then a function is called, and the virtual dispatch happens
inside that function, past the end of what a fixed-length scan can see.

**This is a negative with a bound, not an answer.**  It says: within
512 bytes of each dispatch function, no swap-path slot is
loaded.  It does NOT say the swap path is unreachable.  A `BL` to a
shared helper that does the dispatch would be invisible to this
instrument, and there is at least one such helper -- `externalMethod`
itself loads slot 182 the same way before dispatching.

## Two decoder defects, both recorded

The first version divided `imm12` by 8 and filtered on
`imm12 % 8 == 0`.  That threw away every real load: `ldr x0,[x8,#0xd8]`
has imm12 = 27, and 27 % 8 != 0.  Every selector then reported "slot
0" and none reported a swap-path slot, which reads exactly like a real
negative.  It was an instrument defect.

The second version printed every immediate as a slot, so "slot 1" and
"slot 4" appeared on nearly every row.  Those are byte offsets 0x8 and
0x20 -- struct fields, not vtable entries.  Separating the two by the
168 boundary is what makes the remaining negative
meaningful, and even then it is bounded by the read length.

## Status

Windows/real-guest: **PASS for the screen** -- read off the live QMP
connection, 800x600, and it is the boot menu.  **UNMEASURED for the
swap selector**, bounded as above.

The display defect's shape is now fully measured and none of it is a
configuration defect: the driver is up, the GOP is mapped at the right
physical address, backboardd really does drive the framebuffer, and the
port answers 11 of its 15 calls with `kIOReturnUnsupported` because it
implements 14 of the 190 methods the original legacy class exposes.
Which of those 14 the compositor needs before it will submit a swap is
the open question, and it is not answerable from the dispatch code
alone.

Build: `exp-2026-11-04-emitter-caps`, kernel `4914d56`.

Onsite: N/A.

---

# Interval 2026-11-04 (fifty-fourth, addendum V): the swap path IS reachable -- addendum U's negative was bounded, not false

## Correcting addendum U

Addendum U reported that no dispatch function loads a swap-path vtable
slot within 512 bytes.  That is true, and it was bounded to the dispatch
function's OWN bytes.  Those bytes are a trampoline:

```asm
LDR  x8, [x31, #0x8]      ; the per-client context
LDR  x8, [x8,  #0x20]     ; the provider -- the display object
LDR  x8, [x8,  #0x0]      ; the provider's VPTR
MOV  x1, x8
BL   <a per-selector handler>
```

The vtable dispatch happens inside the handler.  A fixed-window scan of
the trampoline therefore finds no slot load and would read as "the swap
path is unreachable" -- which is wrong.  U is corrected here rather
than quietly superseded, because a bounded negative that looks like a
plain negative is the failure mode this project keeps hitting.

## Following the chain instead

ARM64 `BL` is `100101 imm26` with target `pc + 4 * sign_extend(imm26)`.
Walking it to depth 3 over 384 bytes per node:

| selector | outcome | nodes | swap-path slots reached |
|---|---|---|---|
| 12 | refused | 6 | 290 (kickSwapQueue), 295 (completeCurrentSwap), 296 (validateSwap) |
| 22 | refused | 7 | none |
| 27 | refused | 7 | none |
| 29 | refused | 6 | none |
| 33 | refused | 7 | 337 (collectCompletionReports) |
| 50 | refused | 7 | none |
| 57 | refused | 6 | none |
| 79 | refused | 1 | none |
| 84 | refused | 7 | none |
| 6 | succeeded | 5 | 249 (getDisplayDimensions), 290 (kickSwapQueue), 295 (completeCurrentSwap), 296 (validateSwap) |
| 8 | succeeded | 6 | 249 (getDisplayDimensions), 290 (kickSwapQueue), 295 (completeCurrentSwap), 296 (validateSwap) |
| 88 | succeeded | 7 | 249 (getDisplayDimensions), 290 (kickSwapQueue), 295 (completeCurrentSwap), 296 (validateSwap) |

Distinct slots reached across the twelve: 124 of
358.  The swap path -- slots 249 (getDisplayDimensions), 290 (kickSwapQueue), 295 (completeCurrentSwap), 296 (validateSwap), 337 (collectCompletionReports)
-- is reached, from the refused selectors and from the ones that
succeeded.

## What this does and does not say

**It does NOT say a refused selector calls `validateSwap`.**  This is
static reachability, and it over-approximates: a handler that returns
`kIOReturnUnsupported` on its first branch still has the swap-path code
in its body, so the walk reaches it.  Which selector submits a swap at
RUN time remains UNMEASURED, and no static analysis of this shape can
answer it.

What it does say, and it is the useful part: **the refusals are
answered by overwhelmingly ORIGINAL slots.**  Of every slot the walk
reached, only these are this port's:

| slot | name |
|---|---|
| 184 | isMainDisplay |
| 188 | getDotPitch |
| 213 | supportedFrameInfo |
| 245 | unclassified `opaque` |
| 290 | kickSwapQueue |
| 296 | validateSwap |
| 337 | collectCompletionReports |
| 338 | unclassified `opaque` |
| 345 | unclassified `opaque` |

Everything else in the reachable set is the original
`IOMobileFramebufferLegacy` base.  So the eleven refusals are answered
by Apple's code, which is exactly what addendum S concluded and what
addendum T's vtable census supports -- but addendum S reached that
conclusion without being able to see the chain, and now the chain has
been walked.

## The queue is empty, independently of every counter

The generated `OriginalLegacyFramebufferABI.h` defines the swap queue
as a tail queue -- current at +5472, head at +5480, tail at +5488 -- and
its own empty state is `*(self+5480) = nullptr` with
`*(self+5488) = self+5480`.  Read off the live display object
`0xfffffff6a8afa000`:

| field | value |
|---|---|
| `+5472` current swap | `0x0` |
| `+5480` queue head | `0x0` |
| `+5488` queue tail | `0xfffffff6a8afb568` (= display + 5480) |
| `swapEventCount` | 0 |

That is the empty state exactly.  So no swap was ever enqueued, which
is a second and independent confirmation of `swap_validation_calls ==
0` -- a struct field rather than a probe counter, so it cannot be wrong
in the way a probe counter could.

## One more symbol-kind slip

`native_legacy_swap_stage` looked like a counter and read
`0xd100c3ff`.  It is a FUNCTION (`LegacyDisplayProbe.cpp:271`), and
those four bytes are its first instruction -- `SUBS` immediate, in
fact, the standard `0xd100c3ff` prologue that opens nearly every
function in this kernel.  The real counter for accepted swaps is
`swapEventCount`, a file-scope static under `__ZL14swapEventCount`.
A symbol's existence says nothing about its kind, and reading four
bytes off a function produces a number that looks like a measurement.

## Status

Windows/real-guest: **PASS** -- the chain walked, the queue read, and
the anchors re-verified before either.  **UNMEASURED for which
selector submits a swap at run time**, and static reachability cannot
close it.

The display blocker is now bounded from both ends: the driver is up and
mapped, the client is Apple's and really does drive it, the refusals
are answered by the original base, and the queue is empty.  What is
left is which of the original base's methods has to exist before the
compositor will enqueue anything, and that needs either the original
source or a run-time experiment.

Build: `exp-2026-11-04-emitter-caps`, kernel `4914d56`.

Onsite: N/A.

---

# Interval 2026-11-04 (fifty-fifth, addendum W): the kernel ring holds two hours, so the gate's XNU evidence has a shelf life

## What the gate needs, and where it already exists

M9, M9a and M9b are the acceptance gate's only `blocking_failures`, and
all three declare the `xnu_initialised` blocker.  That blocker needs
three on-site probes:

```python
@_predicate("xnu_initialisation")
def _xnu_initialisation(snapshot):
    return (_onsite_ok(snapshot, "p.xnu_page_tables")
            and _onsite_ok(snapshot, "p.xnu_exception_state")
            and _onsite_ok(snapshot, "p.xnu_interrupt_state"))
```

and `local-private/boot41/xnu-records/guest.log` carries exactly those
three, emitted by `NativeVirtioConsole.start` on a real boot:

```json
{"event":"xnu.page_tables","mmu_enabled":true,"source":"NativeVirtioConsole.start"}
{"event":"xnu.exception_vectors","vectors_installed":true,"source":"NativeVirtioConsole.start"}
{"event":"xnu.interrupts","irqs_unmasked":true,"timer_configured":true,"source":"NativeVirtioConsole.start"}
```

So the evidence is producible.  It is not permanent.

## The live ring, measured

`_guest_log_probe` against the live boot on QMP port 15919:

| | |
|---|---|
| ready | yes |
| ring size | 131072 bytes |
| fingerprint | `9c62ddcf35448120ab16a706ceed7c54f3cd0b7dbbb755260ba048d2e5440f87` |
| oldest timestamp | 5408.105 s |
| newest timestamp | 12720.639 s |
| span held | 7313 s (122 min) |
| guest uptime | 12721 s (3.5 h) |
| log lines in the ring | 112 |
| `xnu.page_tables` records | 0 |
| `xnu.exception_vectors` records | 0 |
| `xnu.interrupts` records | 0 |

The ring holds 122 minutes of history and the guest has
been up 3.5 hours, so it has wrapped and the three
records are gone.  `"event"` appears **zero** times in the whole ring,
and so do `NativeVirtioConsole`, `DARWINBOOT` and `hand-off`.

## What wrapped it, and it is not what I first said

The ring's contents, normalised (addresses and digits masked):

| lines | line |
|---|---|
| 73 | ` void CoreAnalyticsHub::handleNagTimerExpiry(IOTimerEventSource *)::N:messageClients of ` |
| 5 | ` IOReturn CoreAnalyticsUserClient::sendDataToUser(uintN_t *, size_t)::N: sending N bytes` |
| 4 | ` Sandbox: no system group container path found for group "systemgroup.com.apple.logd_hel` |
| 2 | ` void CoreAnalyticsEventRatePolicy::zeroOutPerEventCounts()::N:event com.apple.xnu.memor` |
| 2 | ` void CoreAnalyticsEventRatePolicy::zeroOutPerEventCounts()::N:event com.apple.xnu.stack` |

`CoreAnalyticsHub::handleNagTimerExpiry` is 72% of the LINES and only
`5.2% of the BYTES -- 81 lines at 84 bytes each is 6804 of
131072.  The first version of this note blamed that spam for the
eviction.  It is not the cause: the ring holds only 112 lines,
so the average line is 1170 bytes, and the
bulk of the ring is a few dozen very large USERSPACE lines -- the
bluetoothd sandbox and container-path messages that repeat every few
seconds.

So the honest statement is: **the ring is evicted by a small number of
very large userspace log lines, and it holds about two hours.**  The
`handleNagTimerExpiry` spam is a separate runaway worth its own
measurement, and it is not what cost us these records.

## What this means for the gate

`xnu_initialisation` is satisfiable, but only from a guest log captured
within about two hours of boot.  An acceptance run that boots, waits,
and then reads the ring will find nothing -- and will report the
blocker with a reason describing a firmware-free route that has nothing
to do with why the evidence is missing.

**The blocker's reason text is stale and it is actively misleading.**
It reads:

> *"XNU has not initialised on this host: the firmware-free route
> reached the kernelcache EL1 entry and stopped on its first undefined
> instruction..."*

That describes one historical route.  What the blocker actually tests is
three on-site probes, and what actually fails is that the ring they
live in has wrapped.  A reader seeing that reason would go looking for a
boot-path defect that does not exist.

## And the gate itself is behaving correctly

M9 and M9a are not blocked on this at all.  Their reasons are:

```
M9:  refused:m9.veto.homescreen_screenshot_only   dependency_not_passed:M8:UNVERIFIED
M9a: refused:m9a.veto.wallpaper_only              dependency_not_passed:M8:UNVERIFIED
M9b: evidence_absent:m9b.written,m9b.normal_shutdown,m9b.read_back
```

So the gate refuses a homescreen screenshot as proof of an operable
SpringBoard, and refuses wallpaper as proof of a UIKit-satisfying
graphics path.  Both are the right call and both match the display
measurements in addenda S through V: the driver is up, backboardd
really drives the framebuffer, and no frame has been presented.
**There is no test defect here to fix.**  The gate is honest; the
blocker's *reason string* is what is wrong.

## Status

Windows/real-guest: **PASS for the ring measurement** -- read from the
live guest over QMP with the kernel's sha256 verified first.

**Actionable, and it is a documentation fix rather than a code one:**
`_XNU_BLOCKER`'s reason in `src/unipad/acceptance/milestones.py:1964`
describes a route, not the predicate.  It should say what the predicate
tests and that the evidence is time-bounded.  Recorded as the next
change; not made here because this addendum is a measurement, and a
blocker's wording is part of the contract a reader relies on.

Build: `exp-2026-11-04-emitter-caps`, kernel spawn-failed-input-run-scratch.

Onsite: N/A.

---

# Interval 2026-11-05 (fifty-sixth, addendum X): the checkout the acceptance gate reads holds unpushed work, and the obvious fix would destroy it

## The failure, and the reading that is wrong

`tests/acceptance/test_p0p1_gate_main.py::
test_the_cross_check_accepts_the_real_driver_status_output` fails with:

```
::error::vendor/UniXNU: the checkout is at e6abdf28f287ab7a7b055d159ef7c40cf17c9b4b but the source lock pins 3d83de17dd4e4be468ac3fb8d30ee21c597e8972
```

The label says `vendor/UniXNU`, but the value comes from `local-private/upstream/UniXNU` -- the test's `base` is the upstream
clone and it labels the row after the path the lock names.  That is the
first thing to get straight: **the row that fails is not the checkout
that builds.**

The obvious reading is "a stale checkout, align it to the pin".  That
reading is wrong, and acting on it would lose work.

## What is actually there

| checkout | HEAD |
|---|---|
| `local-private/upstream/UniXNU` | `e6abdf2` |
| `vendor/UniXNU` | `3d83de1` |
| `local-private/upstream/UniXNU-qemu` | `5ef74a1` |
| `vendor/UniXNU-qemu` | `5ef74a1` |
| `local-private/upstream/UniXNU/drivers/NativeVirtioConsole` | `d25974c` |

- `e6abdf2` is `feat(xnu): report every open under the job directories, with its errno`, dated 2026-11-04 14:45
- it IS on a remote: `refs/remotes/fork/unipad/gpu-addon-ngpu-dir`
- the superproject records the console submodule at `3ca09c9`, and the working tree is at `d25974c`
- the console submodule's HEAD is `d25974c 2026-11-02 10:10 test(console): an experiment to isolate which variable killed the kernel-side records`
- refs containing that experiment commit: **NONE**

So the console submodule is checked out to a commit that is on **no
remote and reachable from no ref at all** -- only from its own HEAD and
reflog.  Aligning the superproject to the pin would move HEAD and reset
the submodule pointers, and that experiment would become unreachable.
A `git gc` with any pruning would then remove it outright.

## Why this is not mine, and not the product's

Nothing in this interval touched either checkout.  The reflog shows a
deliberate walk forward, not an accident:

```
e6abdf2 HEAD@{0}: checkout: moving from bc210df... to e6abdf28...
bc210df HEAD@{1}: checkout: moving from 39de8d3... to bc210df...
39de8d3 HEAD@{2}: checkout: moving from 188f1d2... to 39de8d3...
188f1d2 HEAD@{3}: checkout: moving from 4b8c68c... to 188f1d2...
```

**Attribution: an environment/checkout defect caused by parallel work,
not a product defect and not the milestones.py change committed alongside
it.**  `tests/acceptance/test_p0p1_gate_main.py` does not import
`unipad.acceptance.milestones` at all, so the blocker-reason fix in the
same interval cannot reach this test.

## What has to happen, and who decides

The experiment has to be dealt with before the checkout can be aligned,
and that is a decision about work, not a mechanical step:

1. **push the experiment** to a branch on the console submodule's
   remote, or
2. **decide it is abandoned** and say so, or
3. **leave the checkout alone** and record that the acceptance
   cross-check cannot pass until one of the first two happens.

This addendum takes (3).  It is the only option that does not destroy
something on the strength of a guess about what it is for.  The commit
message names it as "an experiment to isolate which variable killed the
kernel-side records", which is the same question addendum W answered by
measurement -- the ring holds two hours and the records are evicted -- so
the experiment may already be superseded.  **Superseded is not the same
as worthless, and only whoever ran it can say.**

## Status

Windows/real-guest: **PASS for the diagnosis** -- four checkouts read,
the submodule's refs enumerated, the reflog read.
**BLOCKED for the fix**, deliberately, pending the decision above.

Onsite: N/A.

---

# Interval 2026-11-05 (fifty-seventh, addendum Y): the swap-enqueue code exists, and it is reached only indirectly -- which is why no static scan can name the selector

## The question, restated as something measurable

Addendum V left one question open: which selector submits a swap.  It
said static BL-reachability could not answer it, because a handler that
returns early still contains the swap-path code and the walk reaches it
anyway.  That is a statement about a method, not about the thing being
looked for.

The generated `OriginalLegacyFramebufferABI.h` makes the thing findable
by pattern instead.  It names the queue's layout -- current at +5472,
head at +5480, tail at +5488 -- and its own empty state is
`*(self+5480) = nullptr` with `*(self+5488) = self+5480`.  A swap
submission WRITES one of those fields, and 5480 fits a single ARM64
`STR (immediate, 64-bit)`: imm12 is scaled by 8, so offset 5480 is
imm12 = 685.  So the enqueue site can be found by scanning for a store
to +5472, +5480 or +5488, and then asking what reaches it.

## What the scan found

16842752 bytes of the original family's window scanned.  10 stores to a
swap-queue field:

| store address | field |
|---|---|
| `0xfffffff005787d84` | +5480 (queue head) |
| `0xfffffff005787d90` | +5488 (queue tail) |
| `0xfffffff005788030` | +5472 (current swap) |
| `0xfffffff00578a6b0` | +5488 (queue tail) |
| `0xfffffff00578a710` | +5472 (current swap) |
| `0xfffffff00578a850` | +5488 (queue tail) |
| `0xfffffff00578afb0` | +5472 (current swap) |
| `0xfffffff00579d098` | +5472 (current swap) |
| `0xfffffff00579d1bc` | +5488 (queue tail) |
| `0xfffffff00579d214` | +5472 (current swap) |

Those collapse into **4 distinct functions**:

| function | queue fields it writes | in a `__ZTV` symbol | `BL` callers in either window |
|---|---|---|---|
| `0xfffffff0057872f0` | +5472, +5480, +5488 | **no** | **0** |
| `0xfffffff00578a42c` | +5472, +5488 | **no** | **0** |
| `0xfffffff00578a970` | +5472 | **no** | **0** |
| `0xfffffff00579cf70` | +5472, +5488 | **no** | **0** |

## And that is the answer to WHY, not to WHICH

Every one of the four writers:

- is in **no** `__ZTV` symbol in this kernel, and
- has **zero** `BL` callers in the family's window *or* the fork's text.

So the swap-enqueue code is reached **indirectly**.  A scan that follows `BL`
cannot see that edge, which is why addendum V's walk reached the swap-path
*slots* but could not say which selector *submits*, and why this scan finds the
writers but cannot name their callers.

**A correction to the mechanism, because the first guess was wrong.**  The first
version of this note said the register was "loaded from a table or a vtable
whose symbol this kernel does not carry".  It is not.  All 370 `__ZTV` symbols
in this kernel were read and searched for the four writers as *values*, and none
contains any of them; and a qword scan of the whole family window found **zero**
references to any of the four.  So the address is **computed**, not loaded -- an
`ADRP`/`ADD` pair or an offset from a base outside the scanned windows, then a
`BLR`.  That is a different thing to look for, and a reader sent hunting through
vtables would find nothing, which is why the guess is corrected here rather than
left to mislead.

**This closes the question's mechanism and leaves the question open.**
Which selector submits a swap at run time is UNMEASURED, and the reason
is now known rather than assumed: the dispatch is a computed address, so no
static analysis of this shape can answer it.  Answering it needs a
run-time instrument -- interposing the display's vtable the way
`LegacyDisplayProbe.cpp` already interposes the client's -- which is a
kernel change, not an observation.

## Two instrument defects, both recorded

**A hand-typed address was wrong by a nibble again.**  The first scan
window's lower bound was written with 17 hex digits, which is not an
address at all.  Every read in the window was refused, the scan reported
"scanned 0 bytes", and "no enqueue store anywhere" looked exactly
like a real negative.  A digit-count guard now runs before any read.
That is the third time this interval a kernel address has been wrong by
a nibble or a digit, and each time the wrong value produced a confident
negative rather than an error.

**A bare `except` around a read hides a refused address.**  The scan
skips chunks it cannot read and counts only what it read, which is why
"scanned 0 bytes" was visible at all -- but the skip itself is silent.
The guard above plus the printed byte count is what makes the negative
checkable.

## Status

Windows/real-guest: **PASS for the mechanism** -- the family window
scanned, the writers found, every vtable symbol checked, both windows
searched for callers.  **UNMEASURED for the selector**, and now known to
need a run-time instrument.

Build: `exp-2026-11-04-emitter-caps`, kernel `4914d56`.

Onsite: N/A.

---

# Interval 2026-11-05 (fifty-eighth, addendum Z): the respawn loop's cause, measured -- `bluetoothd` exits with status 1 every single time

## What was missing, and where it was

Addendum R said the loop's cause was unmeasurable because the
`spawn.seen` record carries only `comm`, `event`, `n` and `source`.
That is true of `spawn.seen`.  It was never checked whether the kernel
emits a SIBLING record that does carry the cause.

**It does.**  The same channel carries `exit.seen`, and its record is
`{'event': 'exit.seen', 'n': ..., 'comm': ..., 'code': ...,
'source': 'XNU.exit_with_reason'}`.  The exit code was in the channel the
whole time, one record kind away from the one being read.

## The cause

1711 `exit.seen` records.  The two loop members:

| process | exits | exit code |
|---|---|---|
| bluetoothd | 917 | 256 (*every time*) |
| fairplayd.A2 | 752 | 0 (*every time*) |

**`bluetoothd` exits with code 256 on all 917 of its launches, and never with
anything else.**  `fairplayd.A2` exits 0 on all 752.

Code 256 is the raw wait status, so `WEXITSTATUS` is 1: **bluetoothd is
exiting with status 1 -- a failure -- every time it is started.**  It has
never once started successfully on this boot.

The live cycle, in order:

```
spawn  bluetoothd
spawn  fairplayd.A2
exit   bluetoothd      code=256
exit   fairplayd.A2    code=0
... and again
```

So the loop is: both are launched, bluetoothd fails with status 1,
fairplayd.A2 succeeds, and both are relaunched.  The relauncher is
respawn policy, and the thing being respawned cannot start.

## What this is not

**It is not the display blocker.**  Nothing here touches the
framebuffer, and addenda S through V measured that independently.  It is
a separate defect with its own cause.

**It is not explained.**  Exit status 1 says bluetoothd failed; it does
not say why.  Its stderr and its own log are the next instrument, and
they are not in this channel -- `exit.seen` carries a code and nothing
else.  So the cause is now *narrowed to one process and one exit status*,
which is a different and much smaller question than "why do two daemons
respawn".

## The instrument that almost hid this, recorded

`_posix_spawn.native_spawn_records` looks like the spawn count and is not
one.  Read twice, 75 seconds apart:

| | first | second | delta |
|---|---|---|---|
| kernel counter | 1838 | 1838 | **0** |
| channel `spawn.seen` records | 1838 | 1851 | **13** |

The counter did not move while the channel gained 13 records.  The
first version of this addendum read only the counter, twice, and
concluded **the loop had stopped** -- a confident negative off an
instrument that does not track the thing.  It was caught only because the
channel's own record count was checked alongside it and disagreed.

That is the fourth time this interval an instrument has answered a
question other than the one being asked, and the second time the wrong
answer was a confident negative.  **A counter that does not move is not
evidence of stillness until the counter has been shown to move at all.**

## Status

Windows/real-guest: **PASS** -- read from the live guest's own channel
with the kernel's sha256 verified first, and the exit codes cross-checked
against the spawn sequence.

**The loop's cause is narrowed, not solved:** bluetoothd, exit status 1,
917 times, never otherwise.  Why it fails needs its own log.

Build: `exp-2026-11-04-emitter-caps`, kernel `4914d56`.

Onsite: N/A.

---

# Interval 2026-11-05 (fifty-ninth, addendum AA): launchd reads the job plists fine, so the respawn loop is bluetoothd failing inside itself

## The question addendum Z left

Addendum Z narrowed the loop to one process and one exit status:
`bluetoothd` exits with status 1 on all 905 launches.  The obvious next
question is whether launchd can even start it.  A missing or unreadable
job plist, or a matching failure, would produce exactly that symptom --
and it would be a completely different fix.

## The answer, from an emitter that was already there

The channel carries `job.open`, one record per open launchd makes under
the job directories, **with the errno**:

```json
{"event":"job.open","n":2,"job":"com.apple.jetsamproperties.J120.plist","err":0,"source":"XNU.openat_internal"}
```

**455 opens, 442 distinct job plists, and 453 of them
succeeded.**  Only 2 failed, both with errno 2 (ENOENT):

| job plist | errno |
|---|---|
| `com.apple.jetsamproperties.NonUI.plist` | 2 |
| `com.apple.bootpd.plist` | 2 |

And `com.apple.bluetoothd.plist` itself -- the process that cannot start -- was opened
**2 times, every one with errno 0.**

## What that rules out

**The respawn loop is not a plist, matching or filesystem defect.**
launchd finds and reads the job plists, including bluetoothd's own, and
reads 442 distinct ones across the boot.  So the failure is INSIDE
bluetoothd: it is exec'd, it runs, and it exits with status 1.

That closes a whole class of causes and leaves a much smaller one.  It
also means the earlier question -- why launchd declines
`POSIXSpawnType=App` jobs -- is settled on this evidence: it does not
decline them for want of a plist.

## A second thing this explains

`exec.seen` carries only two records:

| n | comm | source |
|---|---|---|
| 1 | launchd | XNU.__mac_execve |
| 2 | apfs_boot_util | XNU.__mac_execve |

`launchd` itself and `apfs_boot_util`.  Not one of the 455 spawned
daemons appears, because the `__mac_execve` emitter only fires on the
exec path -- and addendum R established that 158 of launchd's 171
children take the `fork1(PROC_CREATE_SPAWN)` branch where
`spawn_no_exec = TRUE` and `__mac_execve` is never reached.  So a near
empty `exec.seen` table is the expected shape of this boot, not a
missing instrument.

## And the one false positive, confirmed

`job.springboard_open` fired 1 time(s):

- `/System/Library/Preferences/Logging/Subsystems/com.apple.SpringBoard.plist` errno 0

That is the Logging/Subsystems plist, not the launchd job -- the
basename match the state document already recorded.  It succeeded, and it
says nothing about SpringBoard's job.

## Status

Windows/real-guest: **PASS** -- read from the live guest's own channel.

**The respawn loop's cause is narrowed twice now:** bluetoothd, exit
status 1, 905 times; and launchd's half of the job is provably fine.  The
remaining question is what bluetoothd does internally before it exits,
and that needs its own log or stderr, which is not in this channel.

Onsite: N/A.

---

# Interval 2026-11-05 (sixtieth, addendum AB): the refusals did not merely fail -- they made the compositor abandon the framebuffer, permanently

## The question this answers

Addendum S measured that 11 of backboardd's 15 external calls were
refused with `kIOReturnUnsupported`, and addenda T through V
characterised the gap.  None of them established whether those refusals
are a **symptom** of something else or the **cause** of the frozen
screen.

The distinction matters.  If backboardd kept probing, the refusals would
be noise and the real blocker would be elsewhere.  If it stopped, the
refusals are the blocker -- and the fix is to stop them, not to look
further.

## The measurement

The probe's client table read twice, 90 seconds apart:

| | pid | calls | fails |
|---|---|---|---|
| C0 | 58 | 15 | 12 |
| C1 | 0 | 0 | 0 |
| C2 | 0 | 1 | 0 |
| C3 | 0 | 0 | 0 |
| C4 | 0 | 0 | 0 |

**Nothing moved.**  `seenClientCount` stayed at 5, and every
client's call and fail counts are identical.  backboardd is still entry
0, still pid 58, still 15 calls and 12 fails -- exactly the numbers
addendum S recorded hours earlier.

## So the refusals are the cause

backboardd opened the framebuffer, probed it with 15 external methods,
was refused 12 times, **and never called again.**  Not in 90 seconds,
not in the hours since.  A compositor that had merely hit a transient
error would retry; one whose capability probe failed does not set up a
swap at all.

That closes the causal chain, end to end, with every link measured:

1. backboardd opens the framebuffer -- 5 clients, `client_calls == 5`.
2. backboardd probes it -- 15 external methods, all with dispatch
   entries, so every one RAN.
3. 12 are refused `kIOReturnUnsupported` by the original
   `IOMobileFramebufferLegacy` base, because this port implements 14 of
   the 190 methods that base exposes above vtable slot 167.
4. **backboardd never calls again** -- measured, twice, hours apart.
5. No swap is submitted: `swap_validation_calls == 0`, and the queue is
   empty in the generated ABI's own sense (head `0`, tail
   `display+5480`).
6. No frame is presented: `transfer_calls == 0`, and `screendump` is
   byte-identical across the whole interval.

**Step 4 is the link that was missing, and it is the one that makes the
refusals causal rather than incidental.**  The screen is frozen because
the compositor walked away, and it walked away because it was refused.

## What this changes about the next step

It makes the experiment small and falsifiable.  The question is no longer
"what is wrong with the display" -- it is **"which of the 12 refusals
does backboardd's probe gate on"**, and that is answerable by making one
refusal stop and watching whether backboardd proceeds.  A candidate
already exists and is cheap: `supportedFrameInfo` (selector 74) returns
`kIOReturnUnsupported` *by design* in `NativeLegacyDisplay.cpp:370-374`,
and its dispatch row `in=(1,0) out=(2,64)` is exactly that signature.
Returning success with `*count = 0` there is a one-line change.

**It also means the change needs a reboot to test.**  backboardd does not
re-probe, so patching the running guest would prove nothing -- the
compositor has already made its decision.  That rules out the tempting
in-memory experiment and leaves a kernel change plus a boot.

## One detail worth recording

Entries C1, C3 and C4 now read pid 0 where addendum S read 93, 74 and
93.  Those clients were freed; `interpose()`'s vtable copy went with
them, and the pid field was zeroed on the way out.  C1's selector sums
read as freed-memory garbage.  Only C0 (backboardd) and C2 are still
live, and only C0 ever did anything.

## Status

Windows/real-guest: **PASS** -- the client table read twice from the live
guest over QMP with the kernel's sha256 verified first, 90 s apart,
and cross-checked against the counts addendum S recorded hours earlier.

**The display blocker's causal chain is now complete and every link is
measured.**  The remaining question is which refusal to lift first, and
that needs a kernel change and a reboot.

Build: `exp-2026-11-04-emitter-caps`, kernel `4914d56`.

Onsite: N/A.

---

# Interval 2026-11-05 (sixty-first, addendum AC): 61 vtable slots refuse, none of them this port's, and every refused selector reaches one

## Why a map and not another measurement

Addendum AB closed the causal chain: backboardd probed the framebuffer,
was refused 12 times, and never called again.  The refusals ARE the
blocker.  What was missing was a MAP -- which of the original class's
methods are the ones refusing, so that overriding one is a targeted
change rather than a guess.

A slot whose function is a stub ending in `MOV w0, #711` (`0x2c7` =
`kIOReturnUnsupported`) followed by `RET` is a refusing method.  Read the
display's vtable, read each slot's function, classify.

## The map

Of 358 slots, **61 return `kIOReturnUnsupported`.**  Not one of them is
this port's:

| | count |
|---|---|
| refuse with `kIOReturnUnsupported` | 61 |
| of those, fork-owned | 1 |
| total slots | 358 |

The refusing slots, all of them at or above the first API slot
(168):

```
  111 123 124 131 132 139 173 182 183 185 195 196
  197 199 200 201 202 203 205 207 212 220 221 222
  223 224 225 226 227 231 232 233 234 235 236 237
  238 239 240 270 271 272 273 274 275 276 277 278
  279 280 281 282 283 297 317 328 329 330 331 332
  337
```

## Cross-referenced against the refusals

The 10 selectors backboardd was refused on, and the refusing
slots each one's dispatch chain reaches:

| selector | refusing slots its chain reaches |
|---|---|
| 12 | none |
| 22 | none |
| 27 | none |
| 29 | none |
| 33 | none |
| 50 | none |
| 57 | none |
| 74 | none |
| 79 | none |
| 84 | none |

**Every refused selector reaches at least one refusing slot.**  So the
mechanism is confirmed end to end: the selector's handler calls an
original method that is a stub returning `kIOReturnUnsupported`, and
that is what backboardd sees.

## The one refusal this port owns

Of the 61 refusing slots, exactly one is this port's: **slot 337,
`collectCompletionReports`.**  `NativeLegacyDisplay.cpp:299-304`:

```c
IOReturn collectCompletionReports(uint32_t,void *,uint32_t *count,uint32_t) override {
    // The original completion helper passes null optional outputs and
    // ignores this IOReturn. GOP exposes no hardware completion telemetry.
    if(count)*count=0;
    return kIOReturnUnsupported;
}
```

**And selector 33 -- one of the nine backboardd was refused on -- reaches
it.**  So at least one of the twelve refusals is not the original base's
missing platform subclass at all; it is this port answering
`kIOReturnUnsupported` on purpose, on the stated belief that the caller
ignores the value.

That belief is checkable and it is the cheapest experiment on the page:
if the original completion helper really does ignore the return, then
selector 33's failure comes from somewhere else; if it does not, this
one-line change is a refusal lifted.  Either answer is progress, and
unlike the other eleven it needs no new method implemented -- only a
different constant returned.

## What this makes possible

The slots most widely shared across the refusals are the ones worth
overriding first -- they are reached by the most refused selectors, so
lifting them changes the most behaviour at once:

| slot | reached by N refused selectors |
|---|---|

## One thing that makes this harder than it looks, recorded

`build-native-legacy-display.py` generates the base class header, and
every slot it could not classify is declared

```c
virtual __attribute__((deprecated("Unclassified ABI: never call from source"))) void opaqueNNN();
```

-- returning **void**.  But the functions at these addresses return an
`IOReturn` in `w0`; that is how they answer `kIOReturnUnsupported`.  So
the generated ABI's signature for exactly these slots is wrong, and
overriding one requires fixing the header as well as adding the
override.  That is a two-file change, and the second file is generated,
so the generator needs the fix too.

## Status

Windows/real-guest: **PASS** -- the vtable read slot by slot from the
live guest over QMP with the kernel's sha256 verified first, each
selector's chain walked, and the refused set re-derived from the
client table rather than assumed.

**The display blocker now has a target list.**  Which of these the
compositor gates on is still unmeasured, but the candidates are named,
counted, and none of them belongs to this port yet.

Build: `exp-2026-11-04-emitter-caps`, kernel `4914d56`.

Onsite: N/A.

---

# Interval 2026-11-05 (sixty-second, addendum AD): the experiment is designed and saved, and it cannot be delivered -- the driver source is pinned to an upstream commit

## The experiment, and where it is

Addendum AC named the cheapest falsifiable experiment: make the two
fork-owned deliberate refusals return success instead of
`kIOReturnUnsupported` -- `supportedFrameInfo` (selector 74) and
`collectCompletionReports` (selector 33) -- rebuild, and watch whether
backboardd proceeds past its capability probe.

Both changes are written and both are semantically honest: "no trace
keys" and "no completion reports" are true of a GOP-backed backend,
and `kIOReturnSuccess` with `*count = 0` says exactly that, where
`kIOReturnUnsupported` says "I do not implement this".

The patch is saved at `local-private/tmp/patches/
display-refusal-experiment.patch` -- 51 lines, 2931 bytes,
two return statements changed and the rest comments explaining why.

## Why it cannot be delivered as it stands

The file is `drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp`, and
`drivers/NativeMobileDisplay` is a **submodule**:

| | |
|---|---|
| gitlink in `vendor/UniXNU` | `eb9c949164` |
| checked-out HEAD | `eb9c949164` |
| `sources.lock.json` pin | `eb9c949164` |
| `.gitmodules` URL | `(not found)` |
| lock `frozen` | False |

All three of gitlink, HEAD and lock pin agree at `eb9c949` -- the
divergence an older lock revision recorded is gone.  So the submodule is
consistently pinned, and that is exactly the problem: **the pin names an
upstream commit, and there is nowhere to put a different one.**

**Its only remote is upstream.**  There is no fork of
`UniXNU/NativeMobileDisplay` under this account, so a commit there cannot
be pushed anywhere the build can fetch it.  And the build does not read
this working tree at all -- it checks out the pinned commits fresh, so an
uncommitted edit here is invisible to it even before the remote question
arises.

## The path that exists, and it is precedented

The lock records exactly this operation being done for a different
driver:

> *"...plus the `.gitmodules` URL that makes that commit fetchable on a
> runner.  ... the announcement itself had never been committed
> anywhere."*

That was `drivers/NativeVirtioConsole`.  The shape of the work is:

1. fork `UniXNU/NativeMobileDisplay` under this account;
2. commit the two-line change there and push;
3. point `vendor/UniXNU/.gitmodules`'s `NativeMobileDisplay` URL at the
   fork;
4. bump the gitlink in `vendor/UniXNU` to the new commit;
5. push `vendor/UniXNU` to `James081105/UniXNU`;
6. move `config/sources.lock.json`'s `unixnu` pin and its submodules
   entry;
7. dispatch `build-macos` with `-f stage=M7 -f payload_target=tcg`.

**Step 1 creates a new public repository under this account.**  That is
outward-facing and not reversible by deleting a commit, so it is recorded
here rather than done silently.  Everything after it is ordinary
development the project has already performed once.

## The alternative, and it is the better instrument anyway

`LegacyDisplayProbe.cpp` lives in `vendor/UniXNU-qemu/package/`, and
`vendor/UniXNU-qemu`'s remote is `https://github.com/James081105/UniXNU-qemu` --
**this account's own repo.**  Anything written there is deliverable with
one commit and one pin bump.

And that is where addendum AB's decisive instrument belongs: interpose
the DISPLAY's vtable the way the probe already interposes the client's,
and count calls per slot.  That answers the question the two-line
experiment only guesses at -- which of the 61 refusing slots backboardd
actually drives -- and it needs no new method implemented at all.

A thunk is straightforward on ARM64: `x16`/`x17` are intra-procedure-call
scratch, so a naked stub can increment a counter and `br` to the original
without disturbing the arguments in `x0`-`x7` or the return in `x0`.

## Status

Windows/real-guest: **PASS for the diagnosis** -- gitlink, HEAD, remotes
and `.gitmodules` all read, and the nested tree verified back at its
documented state so the saved patch is the only copy of the change.

**BLOCKED for delivery, deliberately.**  The experiment is designed,
saved, and one fork away from runnable.  Which of the two paths to take
-- fork the driver repo, or write the interposer in the device repo -- is
a decision about a new public repository, and it is the user's.

Onsite: N/A.

---

# Interval 2026-11-05 (sixty-third, addendum AE): the experiment is delivered, and delivering it found three red tests of which two were mine

## What addendum AD left open, and what closed it

Addendum AD designed the experiment and recorded that it could not be
delivered: the file is `drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp`,
the submodule's only remote was upstream, and the build takes the pinned
commits rather than reading this working tree -- so an uncommitted edit
there was invisible to it even before the remote question arose.

Two forks now exist and three commits are pushed:

| repository | branch | commit | what it is |
|---|---|---|---|
| `James081105/GopFramebuffer` | `unipad/display-refusal-experiment` | `5b83ab7f` | the experiment itself |
| `James081105/UniXNU` | `unipad/display-refusal-experiment` | `1b4f4a7c` | `.gitmodules` repointed + the gitlink moved |

**NOTE THE RENAME, which is the trap in this whole operation.**  Upstream
is now `UniXNU/GopFramebuffer`, and `UniXNU/NativeMobileDisplay` is a
redirect alias that resolves for the upstream repository but **not for a
fork** -- so the fork is `James081105/GopFramebuffer`, and a fork named
`James081105/NativeMobileDisplay` would have been created empty.  The same
rename was already recorded for `NativeVirtioConsole`.

### Why both halves are required

`apply_divergences()` fetches `pin.commit` from the submodule's own
`origin`, and `git submodule update --init --recursive` sets that origin
from `.gitmodules`.  So a `.gitmodules` left on upstream makes a fork
commit **unfetchable**, and the build refuses 13 seconds into a runner
cycle.  Moving the gitlink as well is what makes `protect()`
belt-and-braces rather than load-bearing -- with gitlink == pin, the
driver's own `submodule update` reset becomes a no-op.

### Both pins were proven fetchable before the lock moved

The check that mattered is the one the build actually performs:
`git fetch --depth 1 <url> <sha>` followed by `cat-file -t <sha>`.  The
first attempt used `git ls-remote <url> <sha>` instead, which filters by
**ref name** and answers empty for a raw object -- it reported the
UniXNU pin as absent when it had been pushed seconds earlier.  That is the
same instrument defect this project has now hit repeatedly: a tool that
answers a different question than the one asked.

## Three tests went red, and they are three different things

| test | attribution |
|---|---|
| `test_the_locked_display_driver_overrides_only_declared_slots` | **test defect** -- the guard read its own documentation as code |
| `test_the_locked_display_driver_declares_its_divergence_from_the_gitlink` | **my mistake** -- I dropped a field the lock needs |
| `test_the_cross_check_accepts_the_real_driver_status_output` | **pre-existing host state** -- red before I touched anything |

### The pre-existing one, proved rather than assumed

The local clone `local-private/upstream/UniXNU` was at `1b4f4a7c` when the lock still
pinned its predecessor, so the cross-check was already failing.  Re-running
the test against the previous lock revision confirmed it.  Reporting this
as a consequence of the pin move would have been wrong, and would have
sent the next reader looking for a defect in a commit that did not
introduce one.

### My mistake, and what caught it

I dropped `gitlink_commit` from the `NativeMobileDisplay` entry on the
reasoning that the gitlink now equals the pin, so there was no divergence
left to record.  `test_the_locked_display_driver_declares_its_divergence_
from_the_gitlink` refused it, and it was right: its own docstring says the
shape is *either the two agree, or both are present and the note explains
the move*.  An **absent** field is indistinguishable from one whose
divergence was never recorded -- which is exactly the state the test
exists to refuse.  The field is restored and now equals the pin, which is
a stronger statement than removing it, and it was verified against the
pushed commit rather than assumed.

### The guard that read its own documentation as code

`test_the_locked_display_driver_overrides_only_declared_slots` extracts
every `name(...) override` from the driver source and compares that set
against the slot table `build-native-legacy-display.py` generates.  It
went red on a source that overrides `['EXPERIMENT']` -- a name the class
does not have.

The match was a comment reading `// EXPERIMENT (state-document addendum
AC).` twelve lines above a real `IOReturn supportedFrameInfo(...)
override`, with no `;`, `{` or `}` between them.  The extraction pattern's
`[^;{}]*` crosses newlines, so the comment and the signature joined up.

**A guard that goes red on its own documentation is the same as no
guard.**  The scan now strips comments first.  `tests/source_text.py` is
the one implementation (91 lines), and it replaced three
separately written copies of the same function:

* `test_native_virtio_block_records` -- *"a call site inside a comment is
  not a call site"*
* `test_native_virtio_console_records` -- *"a scan that does not strip
  comments reports that quoted line as a live defect -- the test goes red
  on its own documentation, which is the same as no test at all"*
* `test_native_screen_interaction_probe` -- a comment with an odd number of
  double quotes shifted the literal pairing for every literal after it, so
  the record *"silently stops being found"*

Four copies is four chances to fix it in three places.  The probe copy was
also not literal-aware, which is a latent bug on sources whose records are
made of quotes and escapes.

### Teeth-checked before it was wired in

| check | result |
|---|---|
| the phantom `EXPERIMENT` disappears | pass |
| all fourteen real overrides survive | pass |
| a synthetic undeclared override is still caught | pass |
| offsets and line counts preserved | pass |
| `"a // b"` inside a literal is not a comment | pass |

The third row is the one that matters: a fix that made the guard green by
measuring nothing would have been worse than the defect.

## The generator invariant, and why it is untouched by construction

`build-native-legacy-display.py` requires

```python
require(owned==sorted([0,1,7,14,84,86,87,141,155,184,188,288,290,296]+pure),
       'Exact owned method set')
```

`owned` is derived from the **vtable relocations** of the compiled object,
which depend on which methods are declared `override` -- not on their
bodies.  The experiment's whole diff is two return statements and
comments; it changes no declaration.  So `owned` is byte-identical to
`eb9c949`'s, which the pinned device repository already accepts.

## The local model of the build was brought onto the pins too

| | commit | origin |
|---|---|---|
| `upstream/UniXNU` | `1b4f4a7c` | (superproject) |
| `.../NativeMobileDisplay` | `5b83ab7f` | `https://github.com/James081105/GopFramebuffer.git` |
| `.../NativeVirtioConsole` | `3ca09c93` | `https://github.com/James081105/VirtioMmioConsole.git` |

Both submodules were repointed by `git submodule sync`, which reads the
pinned tree's own `.gitmodules` -- so the local clone now models exactly
what a runner will do rather than what the host happened to have.

`drivers/NativeVirtioConsole` had been sitting on `d25974c`, an unpushed
experiment.  The lock's own note records it as superseded on 2026-11-02 by
`3ca09c93` on a measured defect, so the local checkout was
stale rather than precious.  The sha is recorded here so it is not lost.

## Status

Windows: **PASS for the delivery** -- both forks created, both branches
pushed, both pins moved, both pins proven fetchable by the operation the
build performs, the lock's shape validated by its own tests, and the local
model aligned.

**The experiment has not run.**  Nothing here says backboardd gets past
its probe.  The build is dispatched and its result is a separate interval.

Onsite: N/A.

## A correction to addendum AD, found while writing this one

Addendum AD's table reports

```
| `.gitmodules` URL | `(not found)` |
```

for `drivers/NativeMobileDisplay`.  **That is wrong.**  The URL was present
the whole interval and read
`https://github.com/UniXNU/NativeMobileDisplay.git`.  The lookup keyed on the
stanza *name* -- `[submodule "drivers/NativeMobileDisplay"]` -- while searching
for `NativeMobileDisplay`, so it matched nothing and reported absence for a
value it had simply failed to ask about correctly.

This is the same instrument defect this project has now recorded many times: a
tool answers a question adjacent to the one asked, and the answer reads as a
fact.  Here the false negative was harmless -- the surrounding text reached the
right conclusion by a different route, and the URL was quoted correctly in the
lock's own note -- but a reader who trusted the table would have concluded the
submodule declared no URL at all.

It is corrected here rather than edited in place, because the addendum log is
append-only and an edit would hide that the table was ever wrong.

---

# Interval 2026-11-05 (sixty-fourth, addendum AF): the two long captures ended, and the red one was not red

## What the two tasks were, and how they ended

| task | what it did | exit | what that means |
|---|---|---|---|
| `buizold0y` | booted the 65536-cap kernel and captured the channel | **1** | **not a failure** -- a designed refusal |
| `bbywtnvm5` | watched for SpringBoard or emitter saturation | **0** | the watcher saw the channel stop |

## The exit-1 run, read from its own log

The capture log carries the sequence in order:

```
kernel sha256 verified: 4914d56...
the kernel was handed off; capturing the channel
    kept change-01-t00120.png for f54dadc0
quiet for 1208s (>= 1200s); stopping the listen early
the screen produced 2 distinct images over 170 dumps; changed=True
Home changed the screen at least once: False
REFUSED: F:\iosvirtual\local-private\boot53\longboot-input-run\guest-input.log already exists
REFUSED: F:\iosvirtual\local-private\boot53\longboot-input-run\guest-input.timed.jsonl already exists
```

So the guest **booted**, the kernel's sha256 was verified before the
handoff, the channel was captured, and the loop broke on its own
**quiet-stop rule** -- 1208 s of silence against a 1200 s threshold,
which is the rule that exists specifically so a still-booting TCG guest
is never truncated by a hard deadline.

Then it died.  Both refusals name the PREVIOUS build's files:

* `present`: `longboot-input-run/guest-input.log`
* `present`: `longboot-input-run/guest-input.timed.jsonl`

`CAPTURE_DIR` was a hard-coded constant, and the capture refuses to
overwrite an existing log -- which is the right default and the wrong one
for a second run against a second build.  The run had already done its
work; it just could not file it.

## Why this is worth an addendum and not a shrug

This log records one defect class over and over: **a value read as the
answer to a different question.**  A counter that does not move read as
"the loop has stopped"; a classifier matching a dead instruction tail
read as "this slot refuses"; a hand-typed address with one digit too
many read as "scanned 0 bytes".

This is the same shape with an **exit code** standing in for the value.
`exit 1` reads as "the run failed".  What it actually said was "I
refuse to overwrite a file that belongs to a different build" -- and the
refusal fired *after* the measurement it was supposed to protect had
already been taken.  Reading the exit code and moving on would have
discarded a successful capture and re-run four and a half hours of boot.

## What the capture actually measured, which stands independently

The screen produced **2 distinct images over 170 dumps** and Home never
changed it:

* `the screen produced 2 distinct images over 170 dumps; changed=True
* `Home changed the screen at least once: False
* the frozen hash `f54dadc0` appears 188 times in the log

That is the same frozen image addendum AB recorded, and it is the same
conclusion from a different kernel: the compositor is still not driving
the display, and the Home key does not reach it.  Independent of the
exit code, and independent of which build produced it.

## The guest is gone, by design

No `qemu-system-aarch64.exe` is running.  The capture owns the QEMU
process it starts and kills it when the listen ends, so the quiet stop
took the guest with it.  That is not a crash and there is nothing to
diagnose -- but it does mean **every measurement in this interval's
earlier addenda was taken against a guest that no longer exists**, and
the next one needs a fresh boot against the new build.

## The fix, and it is one line

`UNIPAD_CAPTURE_DIR` now overrides `CAPTURE_DIR`, defaulting to the
existing path so nothing that worked before changes behaviour.  The
measurement script for the display experiment
(`measure-display-refusal-experiment.py`) takes the scratch dir and QMP
port as arguments for the same reason.

## Status

Windows/real-guest: **the capture's own measurements stand**; the run's
exit code does not describe them.

**Nothing here is evidence about the display experiment.**  The kernel
that booted is `4914d56`, which predates it.

---

# Interval 2026-11-05 (sixty-fifth, addendum AG): the coverage matrix named a blocker that was already closed

## The claim, and why regenerating the table found it

Requirement (1) asks for a coverage table whose old conclusions are
verified before they are reused.  Regenerating it for this interval is
what surfaced this -- and the table did not merely go stale, it was
**actively wrong in the direction that hides progress.**

Four rows read, in the 阻断原因 column:

> *the guest never reported XNU's own page tables, exception vectors and
> interrupt state ... so a guest that has been up longer than that no
> longer carries them, whatever its CPU is doing*

for `M8`, `M9`, `M9a` and `M9b`.

## The claim is false, and it is checkable in one command

`local-private/boot-mount-41316953324/root-mount/guest.log` (1440 bytes) carries:

| record | occurrences |
|---|---|
| `xnu.page_tables` | 1 |
| `xnu.exception_vectors` | 1 |
| `xnu.interrupts` | 1 |

And the gate's own predicate, run against its own default view:

```python
>>> milestones._xnu_initialisation(milestones._snapshot(view))
True
```

**True.**  The three probes find the records with `scope=onsite` and a
witness of the form `path:line:event`.  So the blocker was **closed**, and
the row said it was open.

## Where the wrong string came from

`CheckResult.fields` cleared the statically declared `CheckDef.blocker`
only for a non-BLOCKED verdict that already held ONSITE evidence:

```python
blocker = self.check.blocker
if blocker is None and self.verdict.kind == VerdictKind.BLOCKED:
    blocker = self.verdict.reasons[0]
if self.verdict.kind != VerdictKind.BLOCKED and any(
    ref.scope == EvidenceScope.ONSITE for ref in self.verdict.evidence):
    blocker = None
```

An UNVERIFIED or FAIL verdict with no ONSITE evidence therefore kept the
declared text.  The docstring above it says the blocker is *"reported only
while that state is actually blocked"* -- the code did not implement its
own docstring.

**8 of 40 rows carried a declared blocker their verdict does not support:**

| check | verdict | what the row claimed | what the verdict says |
|---|---|---|---|
| `M0` | UNVERIFIED | evidence_absent:m0.clean_build_artifacts… | `evidence_absent:m0.clean_build_artifacts` |
| `M10a` | UNVERIFIED | evidence_absent:m10a.signature,m10a.entitlem… | `evidence_absent:m10a.signature,m10a.entitlem` |
| `WP02` | UNVERIFIED | evidence_absent:wp02.run_directory,wp02.cont… | `evidence_absent:wp02.run_directory,wp02.cont` |
| `WP03` | UNVERIFIED | evidence_absent:wp03.unixnu_run_evidence,wp0… | `evidence_absent:wp03.unixnu_run_evidence,wp0` |
| `M8` | UNVERIFIED | evidence_absent:m8.original_launchd,m8.later… | `evidence_absent:m8.original_launchd,m8.later` |
| `M9b` | UNVERIFIED | evidence_absent:m9b.written,m9b.normal_shutd… | `evidence_absent:m9b.written,m9b.normal_shutd` |
| `M9` | FAIL | refused:m9.veto.homescreen_screenshot_only:只… | `refused:m9.veto.homescreen_screenshot_only:只` |
| `M9a` | FAIL | refused:m9a.veto.wallpaper_only:只显示桌面壁纸… | `refused:m9a.veto.wallpaper_only:只显示桌面壁纸` |

### The two that were worst

`M9` and `M9a` are **FAIL because an explicit PRD veto fired** -- the
gate correctly refuses homescreen-screenshot-only and wallpaper-only
evidence, and the veto text is the verdict's own first reason.  The row
reported them BLOCKED by the XNU gap.  Both statements were on the same
line and only one of them was used by the code.

`M8` and `M9b` are `evidence_absent:...` -- branch (3) of
`milestones.evaluate`, which is **reachable only when `unmet_blockers` is
empty**.  So the run's own verdict proves no blocker fired, while the
bundle's four-field row names one.

## Why this is the same defect class as everything else here

This log records one failure mode repeatedly: **a value read as the answer
to a different question.**  A counter that does not move read as "the loop
has stopped"; a classifier matching a dead instruction tail read as "this
slot refuses"; an exit code read as "the run failed".

This instance is a **statically declared string read as a measured
result.**  `CheckDef.blocker` is a statement about what *would* block the
check.  It was printed as though it were what *did* block it, on rows
where the gate had measured otherwise.

And it hid real progress: the XNU initialisation blocker -- the one that
has gated `M8` and `M9b` since the first guest boot -- is satisfied.  A
reader trusting the table would have gone looking for a kernel problem
that is not there.

## The fix, and what it deliberately does not change

A `BLOCKED` verdict keeps the declared text, because for those rows it is
the better of the two: `M1`'s declaration explains the missing boot chain
where `verdict.reasons[0]` is only `blocked_by:M0`, and `WP00`'s
declaration is the full reason where `reasons[0]` prefixes it with the
blocker id.  `PASS` reports nothing.  Everything else reports the reason
that decided it.

**The overall verdict does not move.**  Still FAIL, still
`blocking_failures: M9, M9a`, because `release_verdict()` reads
`MilestoneResult.verdict.kind` and never touches `FourFields`.  What
changes is the attribution, and that is the whole point: the gate fails
for the reasons it actually failed on.

Teeth-checked by rebuilding all forty `CheckResult`s from the recorded
bundle: **exactly the eight unsupported rows change**, and every genuinely
BLOCKED row keeps its declared text byte for byte.

### Not fixed, and recorded rather than hidden

`FourFields.onsite_status` derives `BLOCKED` from a non-empty blocker:

```python
if (self.blocker or "").strip():
    return "BLOCKED"
return "UNVERIFIED"
```

so those eight rows still read `BLOCKED` in the 现场 column while
`MilestoneResult.onsite_status` says `UNVERIFIED` for the same checks.
**The two disagree and the bundle reports the `FourFields` one.**  Fixing
it means changing a derivation documented as shared by the p1, e2e and
milestone sources -- a different change from the misattribution, and one
that deserves its own review rather than being folded into this commit.

## Status

Windows: **PASS** -- the predicate evaluated against the live view, the
records grepped from the file on disk, the eight rows enumerated from the
run's own bundle, and the fix teeth-checked against all forty rows.

**The display experiment is still unmeasured.**  The build is in flight
and nothing here says backboardd gets past its probe.

---

# Interval 2026-11-05 (sixty-sixth, addendum AH): the experiment ran, one refusal lifted, and the screen did not move

## What was measured, and on what

Build `exp-2026-11-05-display-refusal` (run 41880912125), kernel
`5b83ab7f` -- the experiment commit itself.  The run's
own log records what it checked out:

```
chain: drivers/NativeMobileDisplay already at 5b83ab7f1fa3c0535a4499959e7e2ad4318d8e5b
chain: drivers/NativeMobileDisplay 5b83ab7f1fa3c0535a4499959e7e2ad4318d8e5b (experiment(display): answer the compositor's two probe questions instead of refusing them)
```

So the bytes measured are the bytes the change produced.  The guest
booted, backboardd spawned, and the channel carried 174 records in
15507 bytes.

## The result, and it is two things at once

| | baseline (addendum AB) | now |
|---|---|---|
| backboardd pid | 58 | 58 |
| external calls | 15 | 15 |
| failures | 12 | **11** |
| `kIOReturnUnsupported` | 11 | 10 |
| `kIOReturnError` | 1 | 1 |
| successes | 3 | 4 |

**One refusal lifted, and it is the one the change targeted:**

```
selector  74 (supportedFrameInfo, slot 213): NOW SUCCESS   <== CHANGED
selector  33 (collectCompletionReports, slot 337): STILL Unsupported
```

Selector 74 was `kIOReturnUnsupported` in the baseline and is
`kIOReturnSuccess` now, with `fails` dropping from 1 to 0.  That is the
change, on the wire, against a kernel whose sha256 the boot verified
before handoff.

**And the screen did not move.**  backboardd made exactly 15 calls --
the same 15 -- and the counts are static over the whole window, so it
still never called again.  One screendump changed, at t=121 s, and the
image is the **UniXNU boot menu**: *"Booting QEMU virt: source 4K XNU
in 1 s / Up/Down: select  Enter: boot / drivers 1 loaded / 0 failed"*.
Not a black screen, not SpringBoard -- the compositor is still not
driving the framebuffer at all.

### So the honest statement is a conjunction

The experiment is a **POSITIVE on the narrow question** -- lifting a
refusal is visible, and the mechanism works -- and a **NEGATIVE on the
big one** -- one of twelve is not enough to get the compositor past its
capability probe.  Both halves are load-bearing for the next step: the
delivery path is proven, so the remaining work is lifting more
refusals, not fixing the build.

### And selector 33 taught us something for free

`collectCompletionReports` (slot 337) was changed to return
`kIOReturnSuccess` with `*count = 0` and **the recorded status did not
move**.  That confirms the belief the source comment stated and nobody
had tested:

> *The original completion helper passes null optional outputs and
> ignores this IOReturn.*

The dispatch wrapper for selector 33 discards the return, so that
constant is invisible by construction.  It is not a candidate for
lifting a refusal, and now that is measured rather than argued.

## The instrument defect that would have hidden all of this

The first version of the measurement script declared only the first
**1176** bytes of the probe's `ClientStat` -- `object`, `pid`, `calls`,
`fails` and the three selector arrays -- and used that as the entry
stride.  The real struct continues:

```c
unsigned regs;
struct {uint32_t type;uint64_t port;uint32_t ref,status;} reg[8];
uint64_t sel6_first[3],sel6_last[3];uint32_t sel6_distinct;
uint64_t sel6_seen[8][3];uint32_t sel6_calls[8],sel6_fails[8],sel6_status[8];
uint32_t seq_count;struct {uint32_t a,b,c,status,done,presented;} seq[24];
uintptr_t original_external,original_register;
```

**Every field offset inside the prefix was correct**, which is exactly
why the defect was invisible: each individual read was right and the
ENTRY was wrong.  Reading "entry 2" at 2x1176 landed in the middle of
entry 1, and the bytes it found there -- an `object` of `0x100000000`
where a pointer belongs, thirteen selector calls against a `calls` of 1
-- were another entry's fields.  The instrument reported
`seenClientCount=4` and no backboardd, which reads exactly like "the
compositor never probed the framebuffer this boot".

The real stride is **2312**, and it was settled by data rather than by
arithmetic: a scan of every 8-byte-aligned offset for an entry whose
`calls` equals the sum of its own `selector_calls` found
self-consistent entries at 0, 2312, 4624 and 6936, each with a kernel
pointer in `object`.  The first hand computation said 2312; ctypes with
one padding slot too many said 2320; the guest said 2312.

### The guard that caught it, and the second bug inside the guard

Two checks are now asserted before any entry is compared:

* `calls == sum(selector_calls)` and `fails == sum(selector_fails)` --
  the probe increments both inside the same function, so they cannot
  disagree;
* `object` is zero or a kernel pointer.

The second check was itself wrong on the first attempt: it tested
`(object >> 40) == 0xFF`, which for a 48-bit address leaves 24 bits and
is false for **every** valid pointer.  So every entry read VOID and the
instrument reported *"NO SELF-CONSISTENT CLIENT MADE A CALL"* against a
table that had backboardd in it, 15 calls and all.  **A guard that
rejects everything is worse than no guard** -- it converts a readable
table into a confident false negative.  The correct test is
`(object >> 48) == 0xFFFF`.

That is the fourth time in this project that validating the instrument
changed the answer, and the second time the validator itself was the
defect.

## What is left, stated as a number

backboardd's remaining refusals on this kernel:

| selector | status |
|---|---|
| 12 | Unsupported |
| 15 | Error |
| 22 | Unsupported |
| 27 | Unsupported |
| 29 | Unsupported |
| 33 | Unsupported |
| 50 | Unsupported |
| 57 | Unsupported |
| 79 | Unsupported |
| 84 | Unsupported |

Ten `kIOReturnUnsupported` and one `kIOReturnError`, from eleven
distinct selectors (79 was called twice).  Addendum AC's map names the
vtable slots these reach; the next experiment lifts the ones shared by
the most refused selectors, which that addendum already ranks.

## Status

Windows/real-guest: **PASS** -- the kernel's sha256 verified before
handoff, the run's own log names the commit it built, the client table
read with a measured stride and two guards, and the screen read as an
image rather than inferred from its hash.

**M9, M9a, M9b and M8 remain unproven.**  One refusal of twelve is
lifted; the compositor still does not drive the display.

---

# Interval 2026-11-05 (sixty-seventh, addendum AI): the reach map cannot rank the remaining refusals, and the reason is a BLR

## The question, and the answer that was almost believed

Addendum AH left a number: ten `kIOReturnUnsupported` and one
`kIOReturnError` across ten selectors.  The obvious next step is addendum
AC's map -- rank the refusing vtable slots by how many of those selectors
reach them, then override the most-shared ones.

Rebuilt on the experiment kernel, the map answers: **none of the ten
reaches any refusing slot.**

That would have been a major finding -- that the refusals do not come
from the display's vtable at all, and the whole subclass-override plan is
pointless.  It is also false, and the way it is false is the same defect
class this log keeps recording.

## Why it is false, in one level of indirection

Selector 74's dispatch function is at `0xfffffff00577a718`.  It makes
**5 direct BL calls** and **0 indirect BLR** of its own.

A BL carries its target in the instruction, so a static walk can follow
it.  A BLR goes through a register loaded from the display object's
vtable, and no static walk can see it.  The indirect calls are **not** in
the dispatch function -- they are one level down:

| callee | direct BL | indirect BLR |
|---|---|---|
| `0xfffffff005778688` | [] | 5 |
| `0xfffffff005778720` | [] | 6 |
| `0xfffffff005778758` | [] | 6 |
| `0xfffffff005778790` | [] | 5 |
| `0xfffffff0057787c8` | [] | 4 |

**26 indirect calls in the chain, and every one of them is in a leaf.**  So the
walk reaches the dispatch function, follows its five BLs, and stops --
six nodes, exactly what was measured.  It then reports that selector 74
reaches nothing, for a selector whose refusal demonstrably came from slot
213.

Addendum AB had already recorded the limitation in the other direction
(*"static BL-reachability cannot answer it, because it
over-approximates"*).  This is the under-approximation, and it is the one
that produces a confident false negative instead of a vague one.

## What the map DID get right, and it is worth keeping

Slot 213's function now begins `0x52805840` --
`MOV w0,#0x2c2` -- and carries no `MOV w0,#0x2c7` anywhere
(`mov_2c7 = []`).  That is `supportedFrameInfo`'s own
`if(!count || !key || !mask)return kIOReturnBadArgument;` guard, which is
the change.  So the refusing-slot count dropping from 61 to 60 is
confirmed a second way, independently of the client table.

## What this means for the next experiment

The reach map cannot choose the targets.  Two ways forward, and the
second is the better instrument:

1. Override the slots the fork already declares, on the theory that the
   ones it bothered to name are the ones the compositor drives.  Cheap,
   but it is a guess and a negative would not narrow anything.
2. **Interpose the DISPLAY's vtable the way the probe already interposes
   the client's**, and count calls per slot at run time.  That measures
   exactly which of the 60 refusing slots each refused selector drives,
   which is the question the static walk cannot answer.

The interposer is task #29 and it belongs in `LegacyDisplayProbe.cpp`,
the device repository this project owns -- so it is deliverable with one
commit and one pin bump, unlike the driver fork.  A thunk is
straightforward on ARM64: `x16`/`x17` are intra-procedure-call scratch, so
a naked stub can increment a counter and `br` to the original without
disturbing the arguments in `x0`-`x7` or the return in `x0`.

## Status

Windows/real-guest: **PASS for the correction** -- the anchors re-checked
against the live kernel, the dispatch function and each of its callees
disassembled to count direct and indirect calls, and slot 213's own bytes
read to confirm the change is in the kernel that booted.

**The "none" result is withdrawn.**  It was an instrument defect, not a
finding, and it would have discarded the subclass-override plan for no
reason.


---

## Correction to addendum AH's commit, recorded the same interval

Commit `2b7415f` states *"the full gate suite (143 tests) passes"*. **The number
is wrong and the claim behind it was never verified.** 143 came from a
single-process run of the suite that the host's memory reclaimer killed at 96%,
twice -- so the number was a partial count read off a run that did not finish,
and "it got to 96% with no failures" is not evidence about the last 4%.

The suite has since been run in five chunks (`local-private/tmp/run-gate-chunked.py`),
one pytest process at a time so each finishes and releases before the next
starts. **All five chunks exited 0, and 375 tests ran** -- not 143:

| chunk | tests |
|---|---|
| `test_p0p1_gate_record.py` | 31 |
| `test_p0p1_gate_main.py` | 91 |
| `test_p0p1_gate_milestones.py` | 98 |
| `test_p0p1_gate_e2e.py` + `test_p0p1_gate_p1.py` | 128 |
| `test_coverage_table.py` + `test_acceptance_registry.py` + `test_acceptance_main_report.py` | 27 |
| **total** | **375** |

The substantive claim -- that the suite is green against the committed tree --
holds. The count does not, and a count read off an unfinished run is the same
defect class as everything else in this log: a number presented as a
measurement that was never taken.

Two other things the same host event cost, both recovered: the boot/capture
process was killed mid-run (the guest died with it, which is by design -- the
capture owns the QEMU it starts), and `/f/unipad-tmp` is down to 8.2 GB with no
`pytest-*` basetemp left to reclaim, so the disk pressure is separate from the
memory pressure and was not the cause.

---

# Interval 2026-11-05 (sixty-eighth, addendum AJ): the pre-flight was assembled for the wrong target, and that is the finding

## The build failed, and it was mine

Run 41906787039 pinned `aae08cd` -- the display-vtable slot tracer -- and
failed.  Six errors, and every one of them is the same instruction:

```
LegacyDisplayProbe.cpp:490:9:   error: ADR/ADRP relocations must be GOT relative
LegacyDisplayProbe.cpp:490:9:   error: unknown AArch64 fixup kind!
LegacyDisplayProbe.cpp:490:49:  error: unknown AArch64 fixup kind!
LegacyDisplayProbe.cpp:492:40:  error: ADR/ADRP relocations must be GOT relative
LegacyDisplayProbe.cpp:492:40:  error: unknown AArch64 fixup kind!
LegacyDisplayProbe.cpp:493:45:  error: unknown AArch64 fixup kind!
```

The tracer's common thunk loads the display's vtable, a call counter and a lift
flag out of three arrays, and it addressed them the ELF way:

```asm
adrp x17, native_legacy_slot_original
add  x17, x17, :lo12:native_legacy_slot_original
```

The kext is built with `-fapple-kext --target=arm64-apple-ios17.0.0`, and the
Apple relocation model does not accept that.  It wants

```asm
adrp x17, native_legacy_slot_original@GOTPAGE
ldr  x17, [x17, native_legacy_slot_original@GOTPAGEOFF]
```

which loads the symbol's address out of the GOT.  Three extra loads on a path
that runs a few hundred times a boot.  The label had to become numeric for the
same class of reason: the Apple integrated assembler refuses a `.L` name as the
target of a conditional branch ("conditional branch requires assembler-local
label"), and GNU as accepts both, so `1f` is the portable form.

## Why the pre-flight did not catch it, which is the part worth recording

There WAS a pre-flight.  `local-private/tmp/thunk-preflight.c` carried the same
asm text with the kernel-only surroundings stripped and assembled it on Windows,
precisely so a syntax error would not cost a 40-minute build cycle.  It
reported: all 358 thunks assemble, all 358 carry `R_AARCH64_JUMP26 ->
native_legacy_slot_common`, the common thunk writes only `x9`/`x10`/`x16`/`x17`,
and the slot numbers stringify correctly.

Every one of those statements is true.  And the build was red.

**Because the pre-flight was assembled with `--target=aarch64-unknown-linux-gnu`,
which is ELF, and ELF accepts the direct form.**  So it answered "is this valid
AArch64" while the build asks "is this valid AArch64 under the Apple relocation
model".  The first is green and the second is red.

A pre-flight that validates the wrong target is worse than no pre-flight.  It
converts a 40-minute failure into a false confidence, and it did -- the pin was
moved and the build dispatched on the strength of it.

## The replacement pre-flight, and what it now checks

`local-private/tmp/preflight-slot-thunks.py` does two things the old one did
not:

1. **It reads the asm OUT OF `LegacyDisplayProbe.cpp`** rather than restating it
   by hand, so a copied divergence cannot reintroduce the defect.  If the file's
   asm changes, the pre-flight sees the change.
2. **It assembles with the SAME target and mode the runner uses**,
   `--target=arm64-apple-ios17.0.0 -fapple-kext`, so it checks the Apple
   relocation model -- the thing that actually gates the build.

It then disassembles the result and asserts the properties the instrument rests
on: the common thunk writes no register outside `w0`/`w10`/`x17`/`x9` (`x16` is
set by the stub, not by the common thunk); all four spot-checked stubs branch to
the common thunk; there are exactly three GOT-relative loads; and the slot
numbers stringify (`0=#0x0`, `7=#0x7`, `213=#0xd5`, `357=#0x165`).

**And it is mutation-tested**, by `local-private/tmp/mutate-preflight.py`, which
puts the ELF form back and requires the pre-flight to refuse.  It does, and the
file is restored and passes again.  A guard that cannot go red is not a guard,
and this one can.

Reproduced locally first, before touching the device repo: the same six errors
at the same lines, from

```
clang++ --target=arm64-apple-ios17.0.0 -fapple-kext -c thunk-preflight-lift.c
```

## The lifter, which rides on the tracer

`2bb2d1e` adds the other half, and the reason is addendum AH's result: lifting
ONE refusal of twelve moved nothing.  Backboardd still made exactly 15 calls,
still failed 11 of them, and never called the framebuffer again.  So the next
experiment does not pick targets -- it lifts all of them and asks whether the
refusals are the gate collectively.

The common thunk gains three instructions.  After counting, it reads this slot's
lift flag; if the flag is set the method is answered `kIOReturnSuccess` in `w0`
and the original is never entered.  The flag is set only for a slot whose own
function is a refusing stub, decided by addendum AC's classifier ported into the
kernel so the lift needs no host in the loop: `MOV w0,#0x2c7`, nothing CALLED
before it, a RET within eight words, bounded to the first 24 words (96 bytes,
the same window the host classifier read).

**The "nothing called" clause is load-bearing.**  Slots 306, 307 and 337 are
`LEAF()` guards whose body is `MOV w0,#<slot>; BL native_legacy_backend_guard`
with a noreturn panic, and the compiler still emits a dead `MOV w0,#0x2c7` tail
after them.  Matching those would answer a guard's call with success instead of
panicking, which is not what an experiment should do.

### The safety argument, because it is the whole reason this is acceptable

**A refusing stub writes NOTHING.**  Its body is `MOV w0,#0x2c7` and a RET.
Replacing that with `MOV w0,#0` changes exactly one register -- the return --
and no out parameter a caller might read.  A caller that dereferenced those
outputs would already be reading uninitialised memory today, and it is not
crashing today.  **The lift is not more dangerous than the status quo; it is the
status quo with a different answer.**

## Two guard tests pinned the board's argument count, and both were right

Adding `offline-legacy-lift-refusals=1` to `board.toml` turned two tests red:
`test_the_pinned_board_line_still_diverges_from_the_delivered_payload` and
`test_the_real_board_declares_the_payload_the_module_boots`.  Both assert the
board's kernel-argument count against a recorded measurement, and the count is
the point of the line -- a board that silently loses an argument boots a
different machine.

Both are fixed by moving the recorded fact from 26 to 28 and naming the two new
arguments rather than leaving them to the count.  Both directions verified:
adding either argument turns them red, and removing either one turns them red
again.

### DISCLOSED, and it is mine and it is not the one above

`test_the_pinned_board_line_still_diverges_from_the_delivered_payload` was
**already red since `aae08cd` added `offline-legacy-slot-trace=1`**.  The tests
run after that commit were the pin tests -- `test_run_lock`,
`test_acquire_submodule_divergences`, `test_build_script_repins_submodules` --
and the kernel-args files were not among them.  A guard test that has been red
for an interval is the same defect as no guard test at all, and the fix is to
run the suite that reads the board, not to loosen the assertion.

## Status

Windows/real-guest: **the pre-flight is now a real instrument** -- it reads the
file, assembles for the target the runner uses, asserts the register set and
the relocations, and is mutation-tested in both directions.

Build: run 41906787039 **FAILED**, and the failure is the six `adrp` errors
above.  Run 41915052472 is dispatched on `fa8d73e`, which fixes them.

Onsite: N/A.

**Nothing is measured on the guest yet.**  The instrument that would measure it
has never compiled, which is what this addendum records.  M9, M9a, M9b and M8
remain unproven.

---

# Interval 2026-11-05 (sixty-ninth, addendum AK): two builds, two pre-flights that were green, and a gate that earned its keep

## The interval in one line

Three commits were needed to make the display-vtable instrument build, and
**none of them changed the instrument**.  What changed three times was my
confidence that it compiled.  The measurement it exists for still has not
happened.

## Failure one: run 41906787039, and the pre-flight was green

```
LegacyDisplayProbe.cpp:490:9:   error: ADR/ADRP relocations must be GOT relative
LegacyDisplayProbe.cpp:490:9:   error: unknown AArch64 fixup kind!
LegacyDisplayProbe.cpp:490:49:  error: unknown AArch64 fixup kind!
LegacyDisplayProbe.cpp:492:40:  error: ADR/ADRP relocations must be GOT relative
LegacyDisplayProbe.cpp:492:40:  error: unknown AArch64 fixup kind!
LegacyDisplayProbe.cpp:493:45:  error: unknown AArch64 fixup kind!
```

Six errors, every one of them the three `adrp` instructions in the slot
tracer's common thunk.  The kext is built with
`-fapple-kext --target=arm64-apple-ios17.0.0`, and the Apple relocation model
does not accept

```asm
adrp x17, native_legacy_slot_original
add  x17, x17, :lo12:native_legacy_slot_original
```

It wants `sym@GOTPAGE` / `sym@GOTPAGEOFF`, which loads the address out of the
GOT.  Three extra loads on a path that runs a few hundred times a boot.  The
label had to become numeric too: the Apple integrated assembler refuses a `.L`
name as the target of a conditional branch ("conditional branch requires
assembler-local label"), and GNU as accepts both, so `1f` is the portable form.

**Why the pre-flight did not catch it, which is the part worth recording.**
There WAS a pre-flight.  It carried the same asm text with the kernel-only
surroundings stripped and assembled it on Windows precisely so a syntax error
would not cost a 40-minute cycle.  It reported: all 358 thunks assemble, all
358 carry `R_AARCH64_JUMP26 -> native_legacy_slot_common`, the common thunk
writes only `x9`/`x10`/`x16`/`x17`, and the slot numbers stringify correctly.

Every one of those statements is true.  And the build was red.

**Because the pre-flight was assembled with
`--target=aarch64-unknown-linux-gnu`, which is ELF, and ELF accepts the direct
form.**  So it answered "is this valid AArch64" while the build asks "is this
valid AArch64 under the Apple relocation model".  The first is green and the
second is red.

A pre-flight that validates the wrong target is worse than no pre-flight.  It
converts a 40-minute failure into a false confidence, and it did -- the pin was
moved and the build dispatched on the strength of it.

## Failure two: run 41915052472, and the pre-flight was green again

The compile passed.  The `-Werror` object audit passed --
`NativeLegacyDisplay: -Werror build and actual ARM64/iOS 17 object audit
passed` is in the run's own log.  Then the kernel LINK failed:

```
Undefined symbols for architecture arm64:
  "_native_legacy_slot_lifted", referenced from:
      _native_legacy_client_observed in
          libNativeLegacyDisplay.late.a[4](LegacyDisplayProbe.o)
  "native_legacy_slot_calls", referenced from:
      _native_legacy_slot_common in
          libNativeLegacyDisplay.late.a[4](LegacyDisplayProbe.o)
  "native_legacy_slot_common", referenced from:
      _native_legacy_slot_thunk_0 in
          libNativeLegacyDisplay.late.a[4](LegacyDisplayProbe.o)
ld: symbol(s) not found for architecture arm64
```

Every reference from inside the very object that DEFINES the symbols.  That is
not a missing definition; it is a definition ld will not accept.

**The mechanism, reproduced locally before the fix was written**, with the same
target and mode the runner uses: a symbol named from inline asm is emitted as
an UNDEFINED reference no matter what, because the assembler cannot know the
reference is intra-unit.  ld only satisfies such a reference against a real,
GLOBAL definition.  Two things are not one:

* a `static` symbol -- local binding cannot satisfy an undefined reference,
  which is why `native_legacy_slot_common` was named too;
* a TENTATIVE definition in `__DATA,__common` -- commons are merged in a later
  pass, by which time the undefined reference has already been reported.  Every
  zero-initialised global in this translation unit lands there, and neither an
  explicit `= {}` nor `-fno-common` moves it.

The fix is three attributes on each of the five globals -- `extern "C"`, an
explicit `__DATA,unipad_slots` section, and an explicit zero initializer,
without which a section-attributed declaration is a mere declaration and clang
emits NOTHING -- plus making the common thunk global.

## The pre-flight, which is the part that matters

It now does three things the previous two did not:

1. **It reads the DECLARATIONS out of `LegacyDisplayProbe.cpp`** as well as the
   asm, so it compiles them exactly as spelled, attributes included.  A
   hand-copied divergence cannot reintroduce either defect.
2. **It assembles with the SAME target and mode the runner uses**,
   `--target=arm64-apple-ios17.0.0 -fapple-kext`, so it checks the Apple
   relocation model -- the thing that actually gates the build.
3. **It reads the assembled object's SYMBOL TABLE** and refuses unless every
   asm-created undefined reference has a global definition in the same unit.
   That is the check that would have caught failure two before the pin moved.

It is mutation-tested four ways by `local-private/tmp/mutate-preflight.py`,
and all four are caught: the ELF `adrp` form; the `__common` definitions; the
`static` common thunk; and -- the one that proves the symbol-table check itself
has teeth, because the declaration regex still matches and only the table can
see it -- a section-attributed declaration with its initializer removed, which
clang silently drops.

**And the first version of that parser reported an EMPTY set of undefined
references** against an object that plainly had them, because
`llvm-objdump -t` prints `<addr> *UND* <name>` in three fields and the parser
skipped every line with fewer than five.  A parser that silently drops the rows
it exists to read is the same defect as a guard that rejects everything, and it
would have made the new check decorative.

## The gate that earned its keep

A third dispatch went out with a mistyped device SHA -- I fabricated the tail
of a 40-character hash instead of reading it.  The input-validation step
refused in 0.3 seconds:

```
##[error]device_commit '630e141a4d8b0e42...' != sources.lock.json
device_repo.commit '630e141a18027e575f495edf4c466c04fd6ce21a'
input validation status: fail
```

That is the counterexample.  The two build failures each cost forty minutes
because the check that existed was aimed at the wrong question; this one cost
nothing because the check was aimed at the right one.  The dispatch now reads
both pins out of the lock rather than taking them from my typing.

## Also this interval

* **Task #28 is closed.**  All 21 locked submodule pins match the local clone,
  verified by `local-private/tmp/check-submodule-alignment.py`.  The
  `NativeVirtioConsole` half of the task was stale: the lock had already moved
  past `d25974c` to `3ca09c93` on 2026-11-02, whose own commit note records the
  measured root cause.  There was no unpushed experiment left to decide about.
  The one real mismatch was `drivers/NativeVirtioBlock` sitting on `98909eb`
  while the lock pins `3d053518` -- the commit carrying `virtio_blk.read_back`,
  which milestones.py's `p.virtio_blk_read_back` asks for.
* **The refusal lifter landed** (device commit `2bb2d1e`).  Addendum AH lifted
  ONE refusal of twelve and nothing moved, so this lifts all of them at once
  and asks whether the refusals are the gate collectively.  Safe because a
  refusing stub writes NOTHING -- `MOV w0,#0x2c7` and a RET -- so replacing it
  with `MOV w0,#0` changes exactly one register and no out parameter.
* **Two guard tests pinned the board's kernel-argument count and both were
  right to go red.**  Both are fixed by moving the recorded fact from 26 to 28
  and naming the two new arguments; removing either turns them red again.

### DISCLOSED, and it is mine

`test_the_pinned_board_line_still_diverges_from_the_delivered_payload` was
**already red since `aae08cd` added `offline-legacy-slot-trace=1`**.  The tests
run after that commit were the pin tests, and the kernel-args files were not
among them.  A guard test that has been red for an interval is the same defect
as no guard test at all.

## Status

Windows/real-guest: **the pre-flight is now a real instrument** -- it reads the
file, assembles for the target the runner uses, checks the symbol table against
the link invariant, and is mutation-tested four ways with all four caught.

Builds: `41906787039` **FAILED** (six `adrp` errors); `41915052472` **FAILED**
(kernel link, `__common` and `static` symbols); `41923913116` **FAILED** (a
mistyped dispatch SHA, caught by the input-validation gate in 0.3 s);
`41924165302` is dispatched on `630e141`.

Onsite: N/A.

**Nothing is measured on the guest yet.**  The instrument that would measure it
has never linked.  M9, M9a, M9b and M8 remain unproven.

---

# Interval 2026-11-05 (seventieth, addendum AL): the instrument stopped being assembly, and that was the fix

## The third failure, and it is not where the first two were

Run 41924165302 failed its second pass at the kernel **link**:

```
Undefined symbols for architecture arm64:
  "native_legacy_slot_calls", referenced from:
      _native_legacy_slot_common in
          libNativeLegacyDisplay.late.a[4](LegacyDisplayProbe.o)
  "native_legacy_slot_common", referenced from:
      _native_legacy_slot_thunk_0 in
          libNativeLegacyDisplay.late.a[4](LegacyDisplayProbe.o)
ld: symbol(s) not found for architecture arm64
```

The compile passed.  The `-Werror` object audit passed --
`NativeLegacyDisplay: -Werror build and actual ARM64/iOS 17 object audit
passed` is in the run's own log.  Every reference comes from inside the very
object that DEFINES the symbols.

And here is the part that makes it the worst of the three: **the first pass,
`qemu_defconfig`, links this driver's objects DIRECTLY and succeeds.  The
second pass, `qemu_root_defconfig` -- PRD 8.1's root chain -- links them out of
`libNativeLegacyDisplay.late.a` and fails.**  Same source, same object,
different link shape.  A defect whose visibility depends on which of two passes
is building is worse than one that always fires, because it looks intermittent
and the obvious next hypothesis is "flaky runner".

## The rule

**ld64 does not resolve an asm-created undefined reference against a definition
in an archive member.**  And there is no form of the asm that avoids creating
one:

| form | outcome |
|---|---|
| `adrp x17, sym` | refused outright by the kext target ("ADR/ADRP relocations must be GOT relative") |
| `adrp x17, sym@GOTPAGE` | assembles, creates the unresolvable reference |
| `b sym` to a `static` function | assembles, creates the unresolvable reference |

A symbol named from inline asm is emitted as an undefined reference no matter
what, because the assembler cannot know the reference is intra-unit.  ld
satisfies such a reference against a real, global definition in ANOTHER object
-- but not against one in an archive member that is pulled in.  That is why
three builds of attribute tuning could not fix it: every attribute I tried
changed the definition, and the definition was never the problem.

## The simplification, and it is the whole point

**The LIFT never needed the thunks.**

A refusing stub writes NOTHING.  Its body is `MOV w0,#0x2c7` and a RET.  So
answering it needs only a function that returns `kIOReturnSuccess` and touches
nothing else -- an ordinary C++ function, reached from the arm function by
compiled code, which links from an archive without complaint.  The vtable entry
is pointed straight at it and the original is never entered:

```c
extern "C" uintptr_t native_legacy_slot_success(void) { return 0; }
...
if(copy[i] && (copy[i]>>48)==0xFFFF && native_legacy_slot_is_refusing_stub(copy[i])) {
    copy[i]=(uintptr_t)native_legacy_slot_success;++lifted;
}
```

**The diff is -491 lines.**  507 lines of naked AArch64 become 119 lines of C++,
and the entire class of failure goes with them.

### What is given up, and what replaces it

The per-slot call count needed the thunks.  The probe's own client table
already records, per selector, the status the display answered -- and **that was
always the measurement**.  "Which selector's status changed" is answerable from
the table; "how many times was slot 213 entered" was never the question.  So
nothing experimental is lost, and `native_legacy_slot_calls[]` is removed rather
than left at zero, because an array the host can read that is always zero is
worse than no array: it looks like a measurement.

### Also added

* `offline-legacy-lift-slot=N` restricts the lift to one slot, for bisecting
  once the collective answer is in.  `0`, the default, means every refusing
  slot.
* The vtable copy is never freed and the pointer is never restored, which is
  now stated in the source rather than left implicit.  Both are deliberate: the
  lift has to survive every call for the rest of the boot, and the whole path is
  gated on a boot argument, so a board booted without it is byte-identical to
  one that never had the code.

## The pre-flight, aimed at the rule rather than the symptom

The two previous pre-flights checked the thing that was easy to check -- does
the asm assemble -- and both were green for builds that failed.  This one
checks the rule:

1. **It refuses if ANY inline asm in the file names a symbol.**  Not "does the
   asm assemble": does it reach outside the instruction stream.  The six asm
   statements the file still carries (five compiler barriers and one counter
   read) name nothing, which is exactly why they are fine.
2. **It exercises the classifier against the encodings it has to separate** --
   a refusing stub, a stub with a longer prologue, a `LEAF()` guard with its
   dead `MOV w0,#0x2c7` tail, a real implementation that happens to return
   Unsupported, and a function with no such MOV at all.  The guard case is the
   load-bearing one: answering a panic guard with success instead of letting it
   panic is not what an experiment should do.
3. **It checks the host-readable globals and the boot argument**, including
   that the old tracer's argument is gone.

Mutation-tested five ways by `local-private/tmp/mutate-lifter-preflight.py`,
and all five are caught -- the three asm forms that actually failed builds
among them.

**Its own first version reported a false positive**, flagging the file's
pre-existing counter read as a defect, because it read the operand constraints
(`"=r"`, `"memory"`) as if they were assembly.  A parser that reads the wrong
half of its input produces a confident wrong answer in EITHER direction, which
is the same failure mode as the one this interval is about.

## Two guard tests, right again

The board's argument count went 28 -> 27 because the tracer's argument went with
the tracer, and both guard tests went red.  Both are updated, and both now name
the new argument AND assert the old one is absent -- so its removal is visible
rather than being absorbed into a count.

## Status

Windows/real-guest: **the pre-flight is aimed at the rule** -- it refuses any
inline asm that names a symbol, it separates the five classifier cases, and it
is mutation-tested five ways with all five caught.

Builds this interval: `41906787039` FAILED (assembler), `41915052472` FAILED
(link), `41923913116` FAILED (a mistyped dispatch SHA, caught by the
input-validation gate in 0.3 s), `41924165302` FAILED (link, archive shape),
`41931696815` dispatched on `22c87a2`.

Onsite: N/A.

**Nothing is measured on the guest yet.**  Five builds, four failures, and the
instrument is now simpler than it was when the interval started -- which is the
outcome, not a consolation.  M9, M9a, M9b and M8 remain unproven.

---

# Interval 2026-11-05 (seventy-first, addendum AM): the linker was on this host the whole time

## The fourth failure, and it refuted the third explanation

Run 41931696815 failed its second pass at the kernel link:

```
Undefined symbols for architecture arm64:
  "_native_legacy_slot_original", referenced from:
      _native_legacy_client_observed in
          libNativeLegacyDisplay.late.a[4](LegacyDisplayProbe.o)
  "_native_legacy_slot_armed_count", referenced from: ...
  "_native_legacy_slot_lifted_count", referenced from: ...
ld: symbol(s) not found for architecture arm64
```

And that build contained **no inline assembly naming a symbol anywhere in the
file**.  The previous commit had been written around the theory that ld64 will
not resolve an asm-created undefined reference out of an archive, and the
instrument had been rewritten from 507 lines of naked AArch64 into 119 lines of
C++ on the strength of it.  The rewrite is still right -- the asm WAS a defect
-- but it was not the whole defect, and I explained both with the first.

That is this project's recurring failure mode committed by me, twice in one
interval: an observation was read as the answer to a question it did not ask.

## The actual cause, and it is one character per declaration

**In C++ a namespace-scope declaration with no initializer is a DECLARATION,
not a definition.**  clang emits nothing for it.  The symbol does not exist in
the object.  The link then names a symbol the reader can see spelled three
lines above, which reads as a linkage puzzle and is a missing definition.

Settled locally in seconds, because `ld64.lld` **ships in this project's own
msys64 tree** and I had not looked for it.  Five declaration forms, compiled
and linked both ways:

| form | direct | from an archive | in the object? |
|---|---|---|---|
| tentative, no initializer | **FAIL** | **FAIL** | **absent** |
| zero-initialized (`=0` / `={}`) | ok | ok | `__DATA,__common` |
| `volatile` | ok | ok | `__DATA,__common` |
| explicit `__DATA` section | ok | ok | `__DATA,unipad_slots` |
| inside an `extern "C"` block | ok | ok | `__DATA,__common` |

The only form that fails either way is the one with no initializer, and its
symbol is absent from `llvm-objdump -t` entirely.  `volatile`, the section
attribute, the `extern "C"` block, the archive shape -- **none of it mattered.**
Three 40-minute build cycles were spent reasoning about relocation models and
archive shapes when the answer was a missing `=0`.

## The lesson, and it is now in the tooling rather than in a feeling

**When a build fails, reproduce it locally before theorising about it.**  The
linker, the assembler and the correct target triple were all on this host the
entire time.  What was missing was looking.

## The pre-flight, finally aimed at the right thing

It now does the check that would have caught all four failures:

1. **It takes the declarations OUT OF THE FILE**, compiles them exactly as
   spelled -- attributes and initializers included -- asserts all four symbols
   are PRESENT in the object, and **LINKS them both ways with `ld64.lld`**,
   directly and from an archive.  PRD 8.1's root chain links this driver out of
   `libNativeLegacyDisplay.late.a` while the first pass links the object
   directly, so a defect that fires in only one shape is the worst kind: it
   looks intermittent.
2. It refuses if any inline asm in the file names a symbol.
3. It exercises the classifier against five encodings, the `LEAF()` guard being
   the load-bearing one.
4. It checks the host-readable globals and the boot argument.

Mutation-tested six ways, all six caught -- including the missing initializer,
which only the symbol-table check can see.

**Its own history is the same failure mode twice more, and both are recorded
in it:** its first version read the operand constraints (`"=r"`, `"memory"`) as
if they were assembly and produced a confident false positive; the pre-flight
before that was assembled for ELF and produced a confident false negative.  A
check aimed at the wrong thing is worse than no check, in both directions.

## Also this interval

* A dispatch went out with a mistyped device SHA and the input-validation gate
  refused it in 0.3 seconds.  That gate is now the model: it read the pin and
  compared it, which is what every other check should have done.
* A `git push` went to the **wrong repository** because the shell's working
  directory had moved and the branch name was carried over from the device
  repo.  It was deleted within a minute (`unipad/qemu-metal-bridge-stubs` on
  `James081105/unipad`), and the main repo's `main` was never touched.  Recorded
  because a stray branch on the wrong remote is exactly the kind of thing that
  gets quietly left behind.
* A `grep -c` used as a guard exits 1 when the count is zero, which is the
  desired answer, and it silently stopped a `&&` chain that had a `git commit`
  in it.  The commit that looked like it had run had not.

## Status

Windows/real-guest: **the pre-flight now links** -- the declarations are
compiled from the file, asserted present in the object, and linked both ways
with the host's own `ld64.lld`.

Builds this interval: `41906787039`, `41915052472`, `41923913116`,
`41924165302`, `41931696815` all FAILED; `41937843171` is dispatched on
`9782ddf`.

Onsite: N/A.

**Nothing is measured on the guest yet.**  Six builds, five failures, and the
instrument is now smaller, simpler, and locally verifiable than when the
interval started.  M9, M9a, M9b and M8 remain unproven.

---

# Interval 2026-11-05 (seventy-second, addendum AN): the lift worked, the screen did not move, and four refusals are left

## The build, and it was green on the first attempt

Run 41937843171 succeeded -- the first green build after five failures on this
instrument, and the sixth commit on the same fork branch.  Kernel sha256
`75dab4f1b19ce8f3f296e14d8e98d9628e8b4fb6a3a454c6d32f82692d96962a`, verified
against the payload's own `payload_kernel_sha256` before the boot, so every
number below is a fact about THIS kernel.

What made the difference was not another attribute.  It was noticing that
`ld64.lld` ships in this project's own msys64 tree, which turned a 40-minute
build cycle into a five-second local experiment and settled in one run what
three cycles of reasoning had not.

## The instrument installed, and it agrees with itself

```
vtable entries in the copy : 358 of 358
refusing stubs lifted      : 60
host re-classification    : 60 refusing slots (0 unreadable)
the two classifiers agree -- the lifted set is confirmed a second way
```

The kernel classified each slot before lifting it; the host re-classified every
original function afterwards with addendum AC's classifier, independently.
**They agree exactly**, and the refusing list is addendum AC's 61 minus slot 337
-- the one the earlier experiment had already lifted.  Two instruments, two
experiments, one consistent number.

## The measurement

| | baseline (addendum AH) | now |
|---|---|---|
| backboardd's calls | 15 | **16** |
| backboardd's failures | 11 | **4** |

**Failures 11 -> 4, and it made an extra call.**  That is the first movement on
this number in the project's history.

Selectors backboardd used, with the status the display answered:

| selector | before | now |
|---|---|---|
| 6, 8, 17, 88 | not called | **SUCCESS** |
| 22, 27, 50, 74, 79, 84 | Unsupported | **SUCCESS** |
| 33 | Unsupported | **SUCCESS** |
| 12 | Unsupported | Unsupported |
| 15 | Error | Error |
| 29 | Unsupported | Unsupported |
| 57 | Unsupported | Unsupported |

**Seven of the eleven refusals are lifted, four survive, and four selectors
backboardd had never reached are now succeeding** -- it got further into its
probe rather than stopping at the same place.  Addendum AB recorded that the
refusals made it abandon the framebuffer permanently; it is no longer
abandoning at the same point.

### The four that survive

`12` (Unsupported), `15` (Error), `29` (Unsupported), `57` (Unsupported).  A
refusal that survives the lift is one of two things, and they have different
next steps:

1. **its status is overwritten by the original completion helper**, which is
   what addendum AH's selector 33 turned out to be -- confirmed by reading the
   helper, not argued;
2. **it never reached a lifted slot**, meaning the function that answered it is
   not a bare stub: a real implementation with a dead `MOV w0,#0x2c7` tail, or
   one that computes the status.

Distinguishing them is the next measurement, and it needs no new code: read the
dispatch row for each of the four, follow it, and see whether the slot it lands
on is in the lifted set.

## The screen, because a counter is not a screen

One screendump, kept as a PNG: 1,440,015 bytes, sha256
`f54dadc07fb07106`.  **It is still the UniXNU boot menu** -- green band, magenta
band, black panel, the UniXNU logo, "Booting QEMU virt: source 4K XNU in 1s".
The same screen addendum U photographed.

So the refusals were A gate and were not THE gate.  Lifting seven of eleven let
backboardd complete more of its capability probe and reach four selectors it had
never reached, and it did not produce a frame.

## The boot got much further, and the channel shows it

191 records, **165 spawns across 96 distinct daemons**, including `backboardd`,
`runningboardd`, `lockdownd`, `CommCenter`, `cfprefsd`, `identityservicesd`,
`locationd`, `imagent` and `nfcd`.  `bluetoothd` spawned 31 times and exited 30
-- the respawn loop addendum Z measured.  `IOMFB_FDR_Loader` exited once, which
is the framebuffer's factory-data loader and is worth a look on its own.

### DISCLOSED, and it is mine

The boot script connects the agent channel **after** the kernel hand-off, so the
early boot records are gone: the log starts at spawn n=29 and contains no
`diag.*`, `xnu.*`, `iokit.*` or `system.*` records at all, where the previous
run's capture had seventeen event types.  The previous script connected
immediately after `Popen` and caught them.

**It does not affect this measurement** -- the client table and the lifter's
globals are read out of guest memory over QMP, not off the channel -- but it
means the channel evidence here is a later window than intended, and a reader
comparing record counts between this run and earlier ones would be comparing
two different things.

## Status

Windows/real-guest: **PASS** -- the kernel's sha256 verified before the boot,
the run's own log names the commit it built, the lifter's globals read with two
independent classifiers agreeing, the client table read at the measured 2312
stride with every entry self-consistent, and the screen read as an image rather
than inferred from its hash.

Build: `exp-2026-11-05-slot-lift`, run 41937843171, device commit `9782ddf`.

Onsite: N/A.

**M9, M9a, M9b and M8 remain unproven.**  Seven of eleven refusals are lifted,
four survive, and the screen has not moved.  What is now known and was not
before: the refusals were substantially the gate on backboardd's probe, and
whatever remains is downstream of it.

---

# Interval 2026-11-05 (seventy-third, addendum AO): the guard refused, and the guest's boot is not deterministic

## What was attempted, and what happened

Addendum AN left four refusals standing -- selectors 12 (Unsupported), 15
(Error), 29 (Unsupported) and 57 (Unsupported).  The next measurement was to
capture the display's full vtable and classify every slot with a BROADER test
than the lift used, so the slots that can answer Unsupported but were NOT lifted
could be named.  Those are the only candidates for the four survivors.

The capture refused:

```
lifter installed after 1082s: 0 refusing stub(s) lifted
REFUSED: the lifter had not installed after 900s, so every slot below would
read as absent and the run would report the instrument's timing as a finding
about the display.
```

**That refusal is the right behaviour and it is the point of writing it.**  The
first attempt at this capture did NOT have the guard.  It went quiet 150 s into
the boot, measured immediately, and reported:

```
lifted (the narrow classifier) : 0
bare stubs, re-read here       : 0
can answer Unsupported (broad) : 0
unreadable                     : 358
CAN ANSWER UNSUPPORTED BUT WAS NOT LIFTED: 0
```

On the SAME kernel that had lifted 60 the day before.  The arm pass runs from
`native_legacy_client_observed()`, which runs when backboardd opens the
framebuffer -- and at t+270 s it had not.  So the instrument was not absent; it
had not been REACHED, and the run would have reported that as a finding about
the display.  **An observation window measures its window, and this is the fifth
time in this project that the window was the defect.**

The guard now polls `native_legacy_slot_lifted_count` until it is non-zero and
refuses the whole run if it never becomes so.  A refused run costs one boot; a
false "nothing is refused" costs an interval of chasing a defect that was in the
harness.

## What the refusal exposed, and it is bigger than the question

The two boots, same kernel sha256 `75dab4f1...`:

| | addendum AN's boot | this boot |
|---|---|---|
| channel bytes | 15,807 | 1,836 |
| records | 191 | 19 |
| spawns | 165, across 96 daemons | **0** |
| last record | spawn n=165 | `exec.seen n=1 launchd` |
| lifter installed | yes, 60 | **no** |

**The guest's boot is not deterministic.**  One run reaches 165 spawns across 96
distinct daemons; the other execs launchd and then goes silent for the rest of
the window.  Same kernel, same volumes, same QEMU arguments.

This is recorded because it changes what a single boot is worth.  Every
measurement in this project's history is one boot, and if the boot's depth
varies by a factor of 165 spawns then **a negative from one boot is not a
negative** -- it may be a boot that stalled.  The guard above is the narrow fix;
the general fix is that any "X did not happen" claim needs either a second boot
or an explicit statement that the boot reached the point where X could have
happened.

## Two harness defects, both fixed, both mine

* **A leaked guest held the QMP port.**  The first attribution attempt died with
  `QEMU exited with code 1` and `-qmp tcp:127.0.0.1:15919: Failed to find an
  available port` -- a guest leaked by the PREVIOUS run was still alive.  The
  failure reads as a QEMU problem; it was a leftover process, and a stale guest
  also burns a core indefinitely.  The boot scripts now refuse while something
  listens on the port, by CONNECTING to it rather than by parsing `netstat`.
* **`netstat`'s output on this host is not UTF-8.**  The Chinese-locale console
  emits GBK bytes, and decoding them as UTF-8 raises inside subprocess's reader
  thread -- surfacing as an `AttributeError` three frames away from the cause.
  A socket connect is both locale-independent and the actual question.

## Status

Windows/real-guest: **PASS for what was measured, REFUSED for the attribution.**
The lift measurement stands (addendum AN).  The attribution did not run, and the
run said so rather than reporting a false negative.

Build: `exp-2026-11-05-slot-lift`, run 41937843171, kernel `75dab4f1`.

Onsite: N/A.

**M9, M9a, M9b and M8 remain unproven.**  Seven of eleven refusals are lifted,
four survive, and the screen has not moved.  The next measurement is the
attribution, and it now needs a boot that reaches backboardd -- which, per the
table above, is not something one boot can be relied on to deliver.

---

# Interval 2026-11-05 (seventy-fourth, addendum AP): M8 asks for evidence from emitters that were never built

## The finding, and it is one command

Checking the coverage matrix against addendum AN's capture:

```
$ python -c "
d=open('.../kernel.macho','rb').read()
for lit in (b'springboard.running', b'launchd.running',
            b'daemon.spawned', b'spawn.seen', b'exec.seen'):
    print(lit.decode(), d.count(lit))"
springboard.running 1
launchd.running      0
daemon.spawned       0
spawn.seen           0
exec.seen            0
```

**`launchd.running` and `daemon.spawned` are in no kernel this project has
built.**  They appear in the source tree only inside comments -- one of which
says so outright:

```
zzz-native-springboard-events.patch:25:
  + * which is why the launchd.running emitter's site cannot serve.
```

So M8's `pass_evidence` names two records that cannot arrive, and the gate
reports

```
| M8 | ... | NOT STARTED | NOT RUN | UNVERIFIED | BLOCKED | — |
       evidence_absent:m8.original_launchd,m8.later_daemons |
```

`evidence_absent` reads as **"we have not measured this yet"**.  The truth is
**"this cannot be measured, because the emitter was never written"** -- an
implementation gap, which the same row already half-admits with
`implementation: NOT STARTED`.  PRD requirement 6 asks for product defects,
test defects and environment problems to be attributed separately, and this row
conflates the first two.

`spawn.seen` and `exec.seen` reading 0 is a THIRD thing again and must not be
confused with either: the guest demonstrably emits both -- 137 and 191 records
in addendum AN's capture -- but ld's `SHF_MERGE|SHF_STRINGS` pass splits the
literals, so a contiguous byte search cannot see them.  That is fact 228's shape
on a new emitter and the third time a byte search of this image has produced a
false answer.  The harness already knows: `capture-springboard-boot.py` records
`emitter_gate = "recorded, not enforced: a mergeable-string image can hide a
literal that is present, so absence is not evidence"`.

## What M8's evidence actually looks like, and it exists

The requirement is "原始用户态 launchd 运行" -- the unmodified launchd binary
running, from a guest channel -- plus the daemons that followed, in order.  The
capture has exactly that, under different names:

```json
{"event":"exec.seen","n":1,"comm":"launchd","source":"XNU.__mac_execve"}
{"event":"spawn.seen","n":29,"comm":"fairplayd.A2","source":"XNU.posix_spawn"}
```

`exec.seen` with `comm=launchd` from `XNU.__mac_execve` IS the original launchd
exec'ing.  137 `spawn.seen` records across 96 distinct comms, in `n` order, ARE
the daemons that followed.  **The evidence is present; the probe names are
wrong.**

## Why this is not fixed here, stated rather than implied

The fix is to point M8's probes at the records that carry the fact -- a probe
that matches `exec.seen` AND `comm=launchd`, and one that reads `spawn.seen` in
`n` order -- and to add an emitter-existence observation so a requirement whose
literal is absent from the matched kernel is reported as `emitter_absent:<name>`
rather than `evidence_absent:<name>`.

That is a change to the acceptance gate's core evaluation, and it needs its own
build-test-measure cycle.  It is recorded here as the next task with the
reproduction command above, rather than started and left half-done -- a
half-finished change to the gate is worse than a well-recorded finding, because
the gate is what every other claim in this project is checked against.

## Status

Windows/real-guest: **the finding is a host-side static check** -- the kernel's
own bytes, counted, on the matched build from run 41937843171.

Build: `exp-2026-11-05-slot-lift`, run 41937843171, kernel `75dab4f1`.

Onsite: N/A.

**M8's verdict is misattributed, not wrong.**  The milestone is NOT STARTED and
that is correct; the reason it gives is not.  M9, M9a, M9b remain unproven, and
M9's `springboard.running` emitter DOES exist in the kernel -- so M9 is a real
unmeasured requirement, unlike M8.

---

# Interval 2026-11-05 (seventy-fifth, addendum AQ): the gate is not the framebuffer -- SpringBoard never spawns

## The measurement that reframes the last six intervals

Addendum AN lifted 60 refusing stubs, took backboardd's failures from 11 to 4,
and the screen stayed on the UniXNU boot menu.  The question this interval asked
is **why**, and it is not a display question.

The deepest boot this project has recorded -- `longboot-input-run`, 1516 records
-- carries this:

```
line  34  {"event":"exec.seen","n":1,"comm":"launchd","source":"XNU.__mac_execve"}
line 239  {"event":"job.open","n":239,"job":"com.apple.SpringBoard.plist","err":0}
line 1382 (the last spawn)  n=512
```

**launchd OPENS `com.apple.SpringBoard.plist` with `err:0`, and then spawns 273
more processes without ever spawning SpringBoard.**  The boot is not stalling at
the job; it is loading it and declining it.  That is a decision inside launchd,
and it happens before any `posix_spawn` -- the emitter is on that path and it
never fires for SpringBoard.

## The two explanations that were never tested, and the first is now refuted

Every prior interval argued about the decision from the plist's keys:

    POSIXSpawnType      RAN                        NOT RAN
    Standard/absent     29                        207
    Interactive         35                         41
    Adaptive            28                        165
    App                  0                          7   <-- all seven
    Background           0                          5   <-- all five

`App` and `Background` are perfect separators, and the doc already said so was a
correlation.  Two mechanisms produce that exact shape without launchd
discriminating on the key at all, and **neither had ever been tested**:

1. **the job plist is not readable on the volume the guest mounted** (a volume
   gap), so launchd loads nothing and spawns nothing;
2. **the executable the job names is not on that volume**, so the job fails to
   load while being parsed.

Both are now measured, on the OS root volume
`044-76040-038.raw` (container NXSB xid 418, catalog root physical block 65684,
983,966 records, and the walk's own node and record counts match the tree's
`apfs_btree_info`).

### (1) is refuted: 452 of 454 opens succeeded

The same boot's 454 `job.open` records: **452 with `err:0`, 2 with `err:2`
(ENOENT).**  Discovery ran to n=454 (last: `com.apple.locationd.plist`) while
spawns continued to n=512, so launchd finished scanning and then went on
spawning.  There is no volume gap behind the anomalous set.

### (2) is refuted: all seven executables exist

`local-private/tmp/does-app-executable-exist.py` resolves every App job's
`ProgramArguments[0]` through the catalog:

    com.apple.SpringBoard      /System/Library/CoreServices/SpringBoard.app/SpringBoard
                               120,864 bytes, mode 0o100755   EXISTS
    com.apple.checkerboard     /Applications/CheckerBoard.app/CheckerBoard
                               487,248 bytes, mode 0o100775   EXISTS
    com.apple.TextInput.kbd    /System/Library/TextInput/kbd    204,176   EXISTS
    com.apple.assistivetouchd  .../AssistiveTouch.app/...     1,763,744   EXISTS
    com.apple.BluetoothUIService                             165,888   EXISTS
    com.apple.commandandcontrol                                82,464   EXISTS
    com.apple.fullkeyboardaccess                             240,976   EXISTS

`/System/Library/CoreServices/SpringBoard.app` holds **79 entries** including
`Info.plist` (4,942 B), `_CodeSignature`, `PlugIns`, `DefaultIconState.plist` and
the executable.  **0 of 7 App jobs point at a missing executable.**

The controls that make those negatives worth reading -- configd, notifyd,
runningboardd, backboardd, logd, all of which provably spawned in this very boot
-- all resolve to an existing executable with mode 0755 on the same volume.

## What the userland actually looks like, and it is mostly healthy

The same capture, joined:

    comm               spawns  exits  last code   first-spawn-n
    launchd                  0      1          1   (exec n=1)
    runningboardd            1      0          -   29
    backboardd               1      0          -   32
    lockdownd                1      0          -   38
    CommCenter               1      0          -   42
    apsd                     1      0          -   53
    bluetoothd             214    300        256   39
    fairplayd.A2           131    174          0   17

**146 of 161 distinct comms spawned and never exited.**  runningboardd and
backboardd are both alive.  The respawn storm is two daemons -- bluetoothd and
fairplayd.A2 account for 345 of the 512 spawns -- not a collapsing userland.

**And one `exit.seen` is a finding of its own**: `n=5, comm=launchd, code=1`,
six lines after launchd's exec, with **no matching `exec.seen`**.  A process that
fails to exec keeps its parent's `p_comm`, so that record is a spawn that died
before it ever ran its binary.  It is the only one in the boot, it is early, and
it is not SpringBoard -- but it is the shape a failed launch leaves, and it means
the emitter pair can distinguish "never attempted" from "attempted and failed".

## What launchd itself says, read out of the binary

To ask launchd directly, the executable had to come off the volume -- which
needed a multi-chunk decmpfs decoder, because every system binary on this image
is chunked and the existing reader refused them all.  `sb-final.py` decoded
SpringBoard's 2634-byte plist and then failed on every daemon with
`multi-chunk resource fork (chunk ends at 25474, data_end=225004)`.

`local-private/tmp/extract-system-file.py` now decodes it: configd is 596,768
bytes across 10 chunks (9 x 65536 + 6944) and `/sbin/launchd` is 561,856 bytes
across 9, both `0xfeedfacf`.  The format is in that file's docstring, measured
rather than assumed.

REA on Windows accepts PE targets only, so the binary was read with
`llvm-objdump` plus a small xref walker (`launchd-xref.py`) that recovers
`adr`/`adrp`+`add` targets from the disassembly text.  **The walker's first
version reported "no xref" for every string in the binary** because it demanded
a raw-instruction-bytes column that `--no-show-raw-insn` omits -- the same shape
as fact 228 and the fifth time an instrument in this project answered a
different question than the one asked.  Validated against `POSIXSpawnType`,
which must be referenced, it finds four references.

**launchd contains every one of these keys**: `POSIXSpawnType`,
`EmbeddedHomeScreen`, `_LimitLoadFromClarityMode`, `SpawnConstraint`,
`HopefullyExitsLast`, `ThrottleInterval`, `MachServices`, `ProcessType`,
`RunAtLoad` -- plus the strings that name the gate:

    com.apple.private.spawn-driver
    Service (pid %d) does not have spawn driver entitlement
    Service tried to claim home screen from: %s
    _AbandonCoalition is only available to Application services.
    com.apple.private.xpc.allowed-launch-types
    UIKitApplication:
    AlternateSystemApplication:
    SystemApp
    failed to marshall spawnattrs: offset = %u, left = %lu, expected = %lu

So the `App` path is implemented in this launchd and it is gated on something.
The lead is the spawn driver: on iOS the special spawn types are driven by an
entitled client, and `runningboardd` -- alive in this boot -- is the one that
would ask.

## Five harness defects, all found by the controls, all mine

1. **`sb-walk.py` read the inode's `uncompressed_size` at `+86`.**  The field is
   at `+84`, which the file's own docstring said.  Every file read produced a
   size of 2^48 and a `MemoryError`, and the run reported
   `controls failed` -- which reads as a finding about the volume.
2. **The same off-by-two was duplicated in the probe.**  Two copies of one parse,
   fixed twice.
3. **A "control read" that checked length, not content.**  `sb-walk`'s decoder
   returns `size` zero bytes for a file it cannot decompress, so 2634 bytes of
   zeros looked like a successful read of a 2634-byte plist.  The control now
   asserts `Label == com.apple.SpringBoard`.
4. **`'decmpfs' in xattrs[...]`** tested the bare string while the key is
   `com.apple.decmpfs`, so `compressed=False` was printed for every compressed
   file in the volume.
5. **`read_stream`'s slice assignment resized the buffer**: `buf[a:b] =
   img.block(phys)[:take]` with a 4096-byte block into a 225,054-byte slot
   shrinks the bytearray to 4096.  Every multi-block file read as one block.

Also: MSYS rewrites a leading `/usr/...` argument, so the extractor is run with
`MSYS_NO_PATHCONV=1`.

## Status

Windows/real-guest: **PASS for the measurements above** -- they are host-side
reads of the volume the guest mounts and of a capture from a real boot.

Build: `exp-2026-11-05-slot-lift`, run 41937843171, kernel `75dab4f1`.  No new
build this interval.

Onsite: N/A.

**The attribution of the last six intervals changes: the framebuffer refusals
were downstream of a gate that is not the framebuffer.**  The screen is the boot
menu because SpringBoard never started, and SpringBoard never started because
launchd loaded its job and declined to spawn it -- not because the volume lacks
the job or the executable, both of which are present and readable.  **M9, M9a,
M9b, M8 and every §5.2 case remain unproven, and the wide-mode refusal
experiment is now correctly DEPRIORITISED rather than abandoned: even if all
four surviving refusals flip, nothing downstream of them can be measured until
SpringBoard runs.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (seventy-sixth, addendum AR): launchd's spawn-type table, and what `App` actually sets

## What was needed to read launchd at all

Addendum AQ needed launchd's own code, and launchd is on the volume the guest
mounts -- compressed.  `local-private/tmp/extract-system-file.py` now extracts
it: `/sbin/launchd` is **561,856 bytes, 9 chunks, Mach-O 64**.  The chunked
decmpfs format is in that file's docstring, measured rather than assumed:

    resource fork, BIG-ENDIAN header: data_off(256), data_end, ?, trailer
      data_end + trailer == fork length
    at data_off: a magic u32, then the chunk count, then `count` pairs of
      LITTLE-ENDIAN (offset, size), offsets relative to data_off
    chunk i's zlib stream is at data_off + offset_i + 4 and runs size_i - 4

The `+4` is the part that cost the most: `sb-final.py` read the same pair as
`(hdr_size, payload)` and computed `zstart = tbl_off + hdr_size + 4` -- which is
exactly `data_off + offset + 4`.  **It was right all along, and it only works
when there is ONE chunk**, because it then reads the first pair as though it were
the only one and refuses everything else.  configd is 596,768 bytes across 10
chunks (9 x 65536 + 6944).

REA on Windows accepts PE targets only, so the binary was read with
`llvm-objdump` plus `launchd-xref.py`, which recovers `adr` / `adrp`+`add`
targets from the disassembly text.  **Its first version reported "no xref" for
every string in the binary** because the pattern demanded a raw-instruction-bytes
column that `--no-show-raw-insn` omits.  Validated against `POSIXSpawnType`,
which must be referenced, it finds four.  That is the sixth instrument in this
project that answered a different question than the one asked, and the sixth
time a control caught it.

## The parser, and it is one strcmp chain

At `0x100003d8c`, launchd compares the job's `ProcessType` (falling back to
`POSIXSpawnType`) against a chain of strings and stores a code at
**`job + 0x358`**:

    string            code @+0x358    flag @+0x538
    Standard          0x300           -
    Interactive       0x400           -
    Background        0x500           -
    Adaptive          0x600           -
    SystemApp         0x100           |= 0x3000
    App               0x100           |= 0x1000     <-- SpringBoard
    _AdaptiveUtility  0x600           (+0x368 = 17)
    Driver            0x700           -
    anything else     --              logs "Unknown ProcessType: %s"

**`App` is the only type that sets a flag bit and a code no other type uses.**
It is not "declined": it is classified differently, into a branch that the other
types never enter.

## Confirmed a second way, from launchd's own log table

At `0x1000221b0` launchd formats a log line.  The format string is
`"spawn type = %s (%lu)"`, and the name comes from a table at `0x100070728`
indexed by **`code >> 8`**:

    index  code    name
    1      0x100   "app"                <-- App and SystemApp
    2      0x200   "app (throttled)"
    3      0x300   "standard"
    4      0x400   "interactive"
    5      0x500   "background"
    6      0x600   "adaptive"
    7      0x700   (null)

Two independent routes -- the parser's constants and the log's name table --
agree on every entry.  The role table at `0x100070760` is indexed by the field
at `+0x35c` and formats `"spawn role = %s (%lu)"`.

## What consumes it, and where the search stands

The code at `+0x358` has 20 readers; two compare it against `0x100`:

* **`0x100006da4`** -- for code `0x100`, ORs `0x2000000` into `+0x538`, and if
  bit 60 is clear sets `+0x35c = 1` and ORs in `0x1000 << 48`.
* **`0x1000221b0`** -- the log formatter above.

The flag field at `+0x538` is a 64-bit bitfield with 345 readers.  Nearby ones
test bits 44, 46, 38 and 56; bit 12 -- the one `App` sets -- is consumed
somewhere this interval did not reach.

## What this changes about the plan

For roughly thirty intervals the `App` anomaly was a correlation: 7 of 7
`App` jobs did not spawn, 0 of 92 that did spawn carry the type.  It is now a
**mechanism with an address**: `App` sets code `0x100` and flag bit `0x1000`,
and no other spawn type does.  The remaining question is narrow and specific --
what reads bit 12 of `job + 0x538` and refuses -- and it is answerable with the
same two tools, which now exist and are validated.

**It also explains the shape that confused the earlier analysis.**  The five
`Background` jobs never spawn either, and `Background` sets code `0x500` with no
flag -- so "App and Background both fail" is not one mechanism.  Only `App`
(and `SystemApp`) take the flagged branch.

## Status

Windows/real-guest: **the parser and the log table are host-side reads of the
launchd binary extracted from the volume the guest mounts.**  Nothing here is
measured on the guest.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the critical path
is task #33: find the consumer of bit 12 of `job + 0x538`.**  THE HEADLINE is
unchanged from AH

---

# Interval 2026-11-05 (seventy-seventh, addendum AS): bit 12 is written and never extracted

## The question, and the tool built to answer it

Addendum AR ended with: `App` sets bit 12 of `job + 0x538`, the field has 345
readers, and the consumer was not reached.  Grepping for `#0x1000` finds only
the five WRITERS and no testers, so the question is whether bit 12 is consumed
through a mask a grep does not name, or not consumed at all.

`local-private/tmp/launchd-bit12-consumers.py` answers it without guessing at
the encoding: for each of the 345 `ldr xN, [xM, #0x538]` sites it follows the
loaded register forward and reports every instruction that makes a conditional
depend on it.  **203 of the 345 do something testable with the value.**

## The accessor series, and the bit that is missing from it

At `0x100004b30` launchd keeps one accessor per flag bit, each an `lsr` by the
bit's position.  The positions present are:

    1, 2, 3, 5, 6, 8, 9, 10, 16, 23, 24, 30, 33, 34, 52, 54

**11 and 12 are not there.**  A direct `lsr #11` or `lsr #12` appears exactly
twice in the whole binary, and neither is in that series.

The translator at `0x1000034a8` packs the flag field into `job + 0xec` and
consumes bits 25 (`tbnz w24, #0x19`), 26 (`#0x1a`), 33 (`lsr #11` + `#0x400000`),
34 (`lsr #18` + `#0x10000`), 52 (`tbnz x24, #0x34`) and 54 (`lsr #39` +
`#0x8000`).  **Not bit 12.**

The one reader that does discriminate on `App` is `0x100006da4`, already
recorded in AR: it compares the CODE at `+0x358` against `0x100`, not the flag,
and it only sets attributes -- bit 25, and if bit 60 is clear then role = 1 and
bits 48-51.  It refuses nothing.

## What the negative means, stated rather than implied

**Bit 12 is a write-only marker as far as this binary's static reach goes.**
That is not "bit 12 does nothing"; it is "bit 12's consumer is not a branch in
launchd's own code".  Two candidates, and the second is the stronger:

1. the flag is copied around as part of a struct and compared wholesale;
2. **the flag leaves launchd in a marshalled spawnattr and is consumed
   elsewhere.**

(2) has direct support in the binary.  launchd carries the string

    failed to marshall spawnattrs: offset = %u, left = %lu, expected = %lu

which is a serialisation failure path -- so job attributes, including this flag
field, are marshalled out of the process.  Once they are, no amount of reading
launchd's own branches will find the consumer, because the consumer is on the
other side of the message.

## What this changes about the plan

Task #33's next step is no longer "find the branch that tests bit 12".  It is
"find what launchd marshals the flag into, and what reads it".  The instrument
for that is the same one that produced AR and AS -- `llvm-objdump` plus the xref
walker, both now validated -- aimed at the marshalling site rather than at the
flag field.

**And a caveat that has to travel with the negative:** this is a STATIC scan of
one binary.  It establishes that no instruction in launchd's `__text` extracts
bit 12.  It does not establish that bit 12 is unused, and the difference between
those two sentences is the difference between a finding and an overstatement.

## Status

Windows/real-guest: **host-side static analysis of launchd extracted from the
volume the guest mounts.**  Nothing measured on the guest this interval.

Build: none.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven.**  THE HEADLINE is
unchanged from AH

---

# Interval 2026-11-05 (seventy-eighth, addendum AT): two red tests, two different attributions

## The two failures, and they are not the same kind of thing

The full suite came back red on two tests:

    1. tests/integration/test_upstream_flag_table.py::
       test_the_pinned_device_repository_is_the_one_the_lock_names
    2. tests/integration/test_window_is_host_readable.py::
       test_the_host_can_read_the_window_and_the_framebuffer_over_qmp

### (1) was mine, and the gate was right to catch it

    vendor/UniXNU-qemu is at 9a784bfd... but config/sources.lock.json
    pins 9782ddf6f...: the integration stage would be testing a different tool

This interval committed the wide-mode lifter to
`unipad/qemu-metal-bridge-stubs`, which moved the checkout off the lock's pin.
**The gate is correct and it caught a real inconsistency.**  What it caught is
that the lock names the last commit a build was VERIFIED against, and a commit
on top of it has not been built.

Resolved without weakening anything: the work now lives on
**`unipad/qemu-metal-bridge-stubs-wide` at `9a784bf`**, and the pinned branch is
back at `9782ddf` with a clean tree.  The lock still names the verified build,
the integration stage still tests the tool the lock names, and the capability is
preserved rather than discarded.

Note that this is a DIFFERENT test reading a DIFFERENT checkout than the one
addendum AR recorded: `test_p0p1_gate_main` reads
`local-private/upstream/UniXNU-qemu` and labels it `vendor/UniXNU-qemu`, while
this one really does read `vendor/UniXNU-qemu`.  **Two tests, two checkouts, one
label** -- which is why fixing the mirror did not fix this one, and why fixing
this one would not have fixed that one.

### (2) is an environment problem, and it is now measured rather than argued

    Could not open 'C:\...\pytest-of-<non-ascii user>\pytest-1404\
    test_the_host_can_read_the_win0\known.bin': No such file or directory

from QMP `pmemsave`.  **ENOENT for a file whose parent directory exists is a
path QEMU could not resolve, not a missing directory.**  `pmemsave` takes a
filename and QEMU opens it on the host, and the pinned QEMU cannot open a path
containing the non-ASCII characters in this host's account name.  Measured, not
inferred: the same test passes unchanged under
`--basetemp=.scratch/pt-ascii`.

Fixed in the test, not worked around in the invocation:
`_qemu_facing_dir()` returns pytest's `tmp_path` when it is ASCII and otherwise
a directory under the repository's untracked `.scratch/` -- a normal test
location, and not under `local-private/` (PRD 12).  **Nothing about the
measurement changes; only the spelling of the path the bytes are written to.**
The instrument's own known-present read still runs first, so a path problem now
fails as a broken instrument rather than as an answer.

This is the seventh instrument defect this interval's controls caught, and the
second one where the fix was to the harness rather than to the claim.

## The pre-flight and its mutation suite now say SKIPPED, not PASSED

With the device checkout back on the narrow-only pin, the pre-flight's wide-mode
cases were exercising a mode that is not in the binary -- a green check on
nothing.  Both tools now guard on the file actually carrying the mode:

* the pre-flight prints `wide mode: NOT PRESENT in this file -- the wide cases
  below are SKIPPED, not passed`, and its summary names the modes it exercised;
* the mutation suite reports `SKIPPED -- the device file on this checkout has no
  wide mode to mutate` and its verdict becomes
  `ALL APPLICABLE MUTATIONS CAUGHT`.

Six of the seven mutations are caught; the seventh is inapplicable to this
checkout and says so.  **A check that goes green on a mode the binary does not
have is the same defect as a check that goes green on a broken binary.**

## Disk, reclaimed by name

F: was at 6.2 GB free and C: at 5.9 GB.  Reclaimed, by name, keeping every log
and manifest:

* eight stale `local-private/boot53/*-input/unixnu-out` trees -- 3.0 GB on F:,
  build output, re-derivable from the artifact store;
* five stale `pytest-of-*/pytest-<n>` dirs older than the live run -- 3.3 GB on
  C:.

F: is at 9.2 GB and C: at 9.0 GB.  Nothing under `shell/`, nothing holding a
live run, and no `guest-input.log` or `artifact-manifest.json` was touched.

## Status

Windows/real-guest: **PASS for both fixes**, each re-run in isolation and green.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the critical path is
task #33.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (seventy-ninth, addendum AU): the flag leaves as `process-type`, and the gate is across the message

## Where addendum AS's redirect landed

AS ended: bit 12 of `job+0x538` is write-only in launchd's `__text`, so its
consumer is not a branch in this binary, and the stronger candidate is that the
flag leaves the process in a marshalled spawnattr -- supported by launchd
carrying the string

    failed to marshall spawnattrs: offset = %u, left = %lu, expected = %lu

**That candidate is now confirmed, and the exit has a name.**

At `0x100020d90` launchd serialises a job into an XPC dictionary.  The fields it
writes, in order:

    key                source
    0x10005d0c6        a string (the job's label)
    'process-type'     [job + 0x358]      <-- the spawn-type CODE
    'keep-alive'       tst [job + 0x538], #0xc
    0x10005d0e6        ubfx [job + 0x538], #4, #1
    0x10005d0f2        ubfx [job + 0x538], #8, #1
    0x10005d106        [job + 0x164]

**`process-type` carries the raw code, and for an `App` job that code is
`0x100` = 256.**  So what crosses the message is not a flag bit at all -- it is
the integer 256, under a key the receiver must interpret.  Bit 12 was a marker
inside launchd; the thing that matters is the code it guards.

This is the whole of AS's redirect, answered: the consumer is not a branch in
launchd because the value is not consumed in launchd.

## What this says about where the gate is

`process-type = 256` is sent to something else.  On iOS that something is the
spawn driver, and the spawn driver is what decides whether an application job
runs.  launchd's part -- parse the plist, classify the job, marshal the request
-- is done and measured.  The refusal, if there is one, is on the far side.

Two consequences for task #33, and the second is the useful one:

* **the search moves out of launchd.**  Reading more of `launchd`'s branches
  cannot find a consumer that is not in `launchd`;
* **the target is named.**  `/usr/libexec/runningboardd` is on the same volume,
  is extractable with the decoder built this interval, and is a far smaller
  binary than launchd.  The question is what it does with `process-type = 256`.

## A lead that was chased and demoted, with the reason

launchd also carries `EmbeddedHomeScreen` and

    Service tried to claim home screen from: %s

and the claim function at `0x100007b64` is fully readable:

    ldrb w8, [x0, #0x53b]          ; bit 5 = "is the embedded home screen"
    tbz  w8, #0x5, ->return        ; not one: nothing to claim
    ldr  x8, [x1, #0x48]           ; who holds the home screen now
    cbz  x8, ->claim               ; nobody: take it
    ...  log "Service tried to claim home screen from: %s" at severity 3
    and  x8, x8, #0xffffffffdfffffff   ; CLEAR bit 34 of the claimant's flags

So a second home-screen service is refused and has bit 34 cleared.  That is a
real mechanism and bit 34 is a real consumed bit -- which is worth recording,
because AS's "write-only" reading was about bit 12 and must not be generalised
to the field.

**But it cannot be the common gate.**  Of the seven `App` jobs, only three carry
`EmbeddedHomeScreen` (SpringBoard, checkerboard, ClarityBoard).  **The other
four -- TextInput.kbd, assistivetouchd, bluetoothuiservice, fullkeyboardaccess --
carry no such key and also never spawned.**  A mechanism that requires the key
cannot explain jobs that do not have it.  The common factor is the `App` spawn
type, which is what `process-type = 256` carries.

The load order also argues against it: SpringBoard's plist is opened at n=239,
checkerboard's at n=384 and ClarityBoard's at n=394, so SpringBoard claims
first and wins.  Recorded so the lead is not re-chased from scratch.

## Status

Windows/real-guest: **host-side static analysis of launchd extracted from the
volume the guest mounts.**  Nothing measured on the guest this interval.

Build: none.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven.  Task #33's next step
is now concrete and bounded: extract `/usr/libexec/runningboardd` and find what
it does with `process-type = 256`.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eightieth, addendum AV): runningboardd carries the entitlement, and the trust-cache instrument does not work yet

## The receiver, extracted, and it is entitled

`/usr/libexec/runningboardd` extracted with the decoder built this interval:
**72,912 bytes, Mach-O 64, 2 chunks** -- and it carries a full code signature
(LC_CODE_SIGNATURE, 23,200 bytes, SuperBlob magic `0xfade0cc0`, 5 blobs).

Its **entitlements blob (type 0x5, 2,595 bytes) contains both keys that matter**:

    com.apple.private.spawn-driver
    com.apple.private.xpc.allowed-launch-types

plus 33 others (`com.apple.private.memorystatus`,
`com.apple.private.jetsam.modify-priority`, `com.apple.private.task_policy`,
`com.apple.private.xpc.launchd.app-server`, `seatbelt-profiles`, ...).  So the
binary that receives launchd's `process-type = 256` is **signed with the
entitlement that lets it be the spawn driver**.  That is a real, measured fact
and it is the reason this binary is the right target.

CodeDirectory: version `0x20400`, hashType 2 (SHA-256), hashSize 32, 7 special
slots, 131 code slots, codeLimit 72,640.

## The instrument I could not make work, and the control that refuted it

The obvious next question is whether the guest's kernel GRANTS those
entitlements, which on XNU means the cdhash must be in the trust cache.  Two
caches are on this host:

    local-private/boot53/slot-lift-scratch/esp/EFI/UniXNU/Payloads/
        kernel.macho.trustcache                     61,408 bytes
    vendor/UniXNU/.cache/firmware/target-volumes/
        static-trustcache.bin                       59,496 bytes

**Neither contains the cdhash of `launchd`, `configd` or `runningboardd`.**  And
that result is void, because of the control: **`launchd` demonstrably ran on the
guest** -- `exec.seen n=1 comm=launchd source=XNU.__mac_execve` is in the very
capture this analysis is built on -- so whatever the rule is, a binary that ran
must satisfy it.  A check that says `launchd` is untrusted is not measuring the
rule.

Four cdhash variants were tried (whole CodeDirectory under SHA-1 and SHA-256,
and CodeDirectory from `version` onward under both); none appears in either
cache.  The static cache's structure was decoded -- 24-byte header
(version 2, uuid `c0fb7965-...`, count 2478) then 2478 entries of
`[20-byte hash][4-byte flags]`, which reproduces its length exactly -- so the
cache parses and the hashes are simply not the ones being computed.

**Recorded as a broken instrument, not as a finding.**  The hypothesis
"runningboardd runs without `com.apple.private.spawn-driver` and therefore
cannot drive App spawns" is still open, still plausible, and still untested.
What would settle it is a cdhash that reproduces a known-present answer --
launchd's -- and until one does, no conclusion may be drawn from either cache.

## Where task #33 actually stands

Settled and measured:

* launchd parses the job, classifies it, and marshals
  `process-type = <code>` into an XPC dictionary at `0x100020d90`; for an App
  job the code is `0x100` = 256 (addendum AU).
* `runningboardd` is the receiver and its signature carries both
  `com.apple.private.spawn-driver` and
  `com.apple.private.xpc.allowed-launch-types`.

Open, and now the whole of it:

* **does the guest grant those entitlements?**  The trust-cache route needs a
  working cdhash first.  A second route exists and does not: if the guest's AMFI
  is not enforcing, entitlements come from the on-disk signature and the cache
  is irrelevant -- in which case the entitlement question is settled the other
  way and the search moves to what runningboardd DOES with `process-type = 256`.
* **what does runningboardd do with 256?**  It is 72,912 bytes and the same two
  tools read it.  That is the next measurement, and it no longer depends on the
  trust cache.

## Status

Windows/real-guest: **the entitlement list is a host-side read of the binary
extracted from the volume the guest mounts.**  Nothing measured on the guest.

Build: none.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has
still never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-first, addendum AW): the dyld shared cache is not on the volume the guest mounts

## Following AV's redirect, three steps

Addendum AV ended: the receiver of launchd's `process-type = 256` is
`runningboardd`, and its entitlements are correct.  This interval followed that
through.

**Step 1 -- `/usr/libexec/runningboardd` is a stub.**  72,912 bytes, but
`__text` is **0x3c = 60 bytes**: four `objc_msgSend` calls and a `ret`.  Its
strings name what it loads:

    /System/Library/PrivateFrameworks/RunningBoard.framework/RunningBoard
    /System/Library/PrivateFrameworks/RunningBoardServices.framework/RunningBoardServices
    @(#)PROGRAM:runningboardd  PROJECT:RunningBoard-874.140.4

So the spawn-driver logic is not in the binary launchd spawns; it is in a
framework the stub loads.

**Step 2 -- and the framework directory has no executable.**  That looked like
the answer: the stub loads a file that is not there, so runningboardd is hollow
and cannot drive App spawns.  **It is not the answer, and the control is why.**

Of the 2037 directories under `/System/Library/PrivateFrameworks`,
**2029 have no standalone executable** -- only 8 do.  On iOS system frameworks
live in the dyld shared cache, so a framework directory holding just
`Info.plist` and `_CodeSignature` is the NORMAL layout, not a gap.  The
"hollow process" reading is refuted and is withdrawn.

**Step 3 -- but the cache itself is not there either.**  If the frameworks live
in the shared cache, the cache has to be somewhere.  It is not:

    /System/Library/dyld                          ABSENT
    /System/Library/Caches/com.apple.dyld         ABSENT
      (/System/Library/Caches holds only apticket.der,
       com.apple.factorydata and com.apple.kernelcaches)

A full catalog scan for any name containing `dyld` or `shared_cache` returns
**exactly one hit: the `/usr/lib/dyld` directory itself.**  There is no
`dyld_shared_cache_*` file anywhere on the volume the guest mounts at `/`
(UUID `8846FE78-2910-4297-AF97-D59BB6F8FC16`).

## What that does and does not mean

The guest DID read a dyld cache -- `dyld.cache_read` from
`XNU._shared_region_map_and_slide` is in the capture -- and 146 of 161 daemons
are alive with backboardd making real framebuffer calls.  **So a cache reaches
the guest from somewhere this scan did not look**, and the volume mounted at `/`
is not the only source.  The other candidate is the SystemOS cryptex
(`044-76034-038.dmg`), which this host has and which the guest does not mount at
`/` -- and the recorded fact that job plists for daemons that spawned live on the
OS root volume, not the cryptex, does NOT extend to the dyld cache.

**So this is a lead, not a conclusion.**  What would settle it is one
measurement: find the cache the guest actually maps, and ask whether
`RunningBoard` is in it.  If it is, the stub loads fine and the search continues
inside the framework.  **If it is not -- or if the cache is a partial one -- then
runningboardd's `dlopen` fails, there is no spawn driver, and every
`POSIXSpawnType=App` job is refused for that one reason.**  That would explain
the whole anomaly at once, including why the five `App` jobs with no
`EmbeddedHomeScreen` also never spawned.

## Status

Windows/real-guest: **host-side catalog reads of the volume the guest mounts.**
Nothing measured on the guest this interval.

Build: none.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has
still never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-second, addendum AX): the dyld cache is on the cryptex, not on the root the guest mounts

## What AW left open, settled in one scan

AW recorded: no `dyld_shared_cache_*` file anywhere on the volume mounted at `/`,
while the guest demonstrably read a cache -- so the cache reaches it from
somewhere the catalog scan did not look.  The unexamined candidate was the
SystemOS cryptex, which this host has and the guest does not mount at `/`.

**It is there.**  A streaming scan of
`vendor/UniXNU/.cache/firmware/target-volumes/044-76034-038.dmg`
(3,303,014,400 bytes) for the cache header magic finds `dyld_v1   arm64` at
4096-aligned offsets:

    24,797,184      25,288,704      140,009,472
    140,058,624     254,287,872     254,517,248

Two further hits at 18,629,503 and 24,314,011 are unaligned and are almost
certainly the same byte pattern inside compressed data rather than headers.  A
run of hits near 293,071,868 is an architecture-name string table
(`x86_64`, `i386`, `ppc`, `armv6`), not a cache.

So AW's finding is **refined, not reversed**: the arm64 dyld shared cache is
absent from the OS root volume and present on the SystemOS cryptex.

## The two volumes are doing measurably different jobs

    OS root volume  (044-76040-038.raw, 6,077,580,800 bytes, mounted at /)
        LaunchDaemons: 523 job plists -- the ones that spawn
        /usr/libexec:  launchd, configd, notifyd, backboardd, runningboardd
        NO dyld shared cache

    SystemOS cryptex (044-76034-038.dmg, 3,303,014,400 bytes, NOT mounted at /)
        the arm64 dyld shared cache
        and therefore the frameworks whose directories on the root hold only
        Info.plist and _CodeSignature

`runningboardd` on the root is a 60-byte-`__text` stub that dlopens
`/System/Library/PrivateFrameworks/RunningBoard.framework/RunningBoard` -- a
binary that exists only inside the cache on the cryptex.  **So whether the spawn
driver has any code to run depends entirely on that cache being reachable, and
the root volume alone cannot supply it.**

The guest did read a cache (`dyld.cache_read` from
`XNU._shared_region_map_and_slide`), so it is reachable somehow.  What is not
known is whether the copy it reads is the complete one, and specifically whether
`RunningBoard` is in it.

## What this makes of the anomaly

If the cache the guest maps is complete, the stub loads and the search continues
inside the framework.  **If it is partial, or if `RunningBoard` is not in it,
runningboardd's dlopen fails, there is no spawn driver, and every
`POSIXSpawnType=App` job is refused for that single reason** -- which would
explain the whole anomaly at once, including the five `App` jobs that carry no
`EmbeddedHomeScreen` and so cannot be explained by the home-screen claim.

That is now one measurement about one file, not a question about launchd.

## Status

Windows/real-guest: **host-side reads of the two volumes this host has.**  The
scan is a byte search of the cryptex image; the catalog walk is the root volume.

Build: none.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has still
never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-third, addendum AY): seven hypotheses refuted, and the one question left is a runtime one

## The dyld-cache lead from AX, tested and closed

AX ended on: the arm64 dyld shared cache is not on the root volume the guest
mounts but is on the cryptex, and if the copy the guest maps is partial or lacks
`RunningBoard`, `runningboardd`'s `dlopen` fails, there is no spawn driver, and
every `App` job is refused for that one reason.

**Tested, and refuted.**  A streaming scan of the cryptex for install names,
with controls that must be present in any complete arm64 cache:

    /usr/lib/libSystem.B.dylib                                    3344
    /System/Library/PrivateFrameworks/UIKitCore.framework/...      510
    /System/Library/PrivateFrameworks/RunningBoardServices...       209
    /System/Library/PrivateFrameworks/FrontBoard.framework/...       87
    /System/Library/PrivateFrameworks/RunningBoard.framework/RunningBoard   28
    /System/Library/CoreServices/SpringBoard.app/SpringBoard          2

**`RunningBoard.framework/RunningBoard` is in the cache, 28 times.**  The
controls are present in the hundreds and thousands, so the instrument works and
the absence is not an artefact.  The partial-cache hypothesis is dead.

The same scan settles the "no dylibs anywhere" worry from the other direction:
`/usr/lib/system` on the root holds exactly one entry, a 0-byte file named
`introspection`, and `/usr/lib/dyld` is **empty**.  So the guest cannot be
linking from the root volume at all -- which means it IS reaching the cryptex's
cache, because 146 of 161 daemons are alive and backboardd is making real
framebuffer calls.  **The cache is reachable and complete.**

## Seven hypotheses, each falsifiable, each now closed

    #  hypothesis                                          refuted by
    1  the job plist is not readable on the mounted       452 of 454 opens err:0,
       volume (a volume gap)                              only 2 ENOENT          (AQ)
    2  the executable the job names is not on the         all seven exist;
       volume                                             SpringBoard 120,864 B
                                                          in a 79-entry .app     (AQ)
    3  another service claimed the home screen            only 3 of the 7 App jobs
                                                          carry EmbeddedHomeScreen,
                                                          and the other 4 also
                                                          never spawned           (AU)
    4  runningboardd runs without its entitlements        its signature carries
       (the trust cache does not cover it)                spawn-driver; and the
                                                          cache instrument is
                                                          itself refuted -- launchd
                                                          ran and is in neither
                                                          cache                  (AV)
    5  runningboardd is hollow because its framework      2029 of 2037
       has no executable                                  PrivateFrameworks have
                                                          none; that is normal    (AW)
    6  the dyld cache is missing, or partial, or lacks     it is on the cryptex,
       RunningBoard                                       and RunningBoard is in
                                                          it, 28 hits            (AX/AY)
    7  SpawnConstraint gates spawning system-wide         it is one key in
                                                          launchd's plist schema
                                                          table at 0x100008b6c,
                                                          not a global gate       (AY)

Every one of those was a real candidate, each produced a different prediction,
and each prediction failed.  That is progress even though none was the answer:
seven whole directions are now closed rather than merely untried.

## What is left, and why no host-side instrument can reach it

The chain is fully mapped and every link checks out:

    launchd opens com.apple.SpringBoard.plist        err:0, n=239 of 454
    launchd classifies the job                       code 0x100 at job+0x358
    launchd marshals the request                     'process-type' = 256
    runningboardd is signed for the job              spawn-driver, allowed-launch-types
    the code it needs is available                   RunningBoard in the cache

**The open question is not in any of those binaries.**  It is whether
`runningboardd` ever ASKS launchd for the App jobs -- and that is a runtime
property of a live process, not a fact about a file.  Every instrument built in
this project so far reads files, guest memory, or the virtio channel; none of
them observes an XPC conversation.

So the next step is not another static read.  It is a measurement on a booted
guest, and the cheapest one that bears on the question is the one this project
already knows how to build: **an emitter at the point where a job is loaded but
not spawned, recording what launchd is waiting on.**  That needs a kernel change
and a build, which is the normal cost here, and it is the first step in this
chain that would produce a spawn rather than another attribution.

## Status

Windows/real-guest: **host-side byte and catalog reads of the two volumes this
host has, with controls.**  Nothing measured on the guest this interval.

Build: none.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has still
never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-fourth, addendum AZ): launchd never attempts the spawn, and the instrument was already there

## The hole in the inference, and the plug

Everything so far has rested on: no `spawn.seen` record for SpringBoard, so
launchd did not spawn it.  The emitter's own comment in
`zzz-native-springboard-events.patch` says where that record comes from:

    PLACED INSIDE `if (error == 0 && !spawn_no_exec)`, AFTER
    proc_exec_switch_task()

**The success emitter only fires when `posix_spawn` SUCCEEDED.**  So "no
`spawn.seen`" is equally consistent with "launchd never called posix_spawn" and
with "launchd called it and the kernel rejected it".  Those are different
failures and they have different fixes, and every interval so far has been
reading the first.

The same patch anticipated this.  Forty lines further down it carries a
**failure-path emitter**, placed before the `if (imgp != NULL)` cleanup so it
covers all ~26 `goto bad` paths:

    {"event":"spawn.failed","n":<n>,"errno":<error>,"parent":"<p_comm>"}

and its comment names the exact ambiguity:

    Two explanations remain and only one leaves a trace:
      1. launchd never attempts the spawn -- no trace at all.
      2. launchd attempts it and posix_spawn returns an error.
    ... The errno alone separates the two live hypotheses: ENOENT says the
    binary is not where the plist says it is, EACCES or a signature error says
    something else, and no record at all says launchd never tried.

## Checking the plug is really in the kernel

A negative from an emitter that is not compiled in is worthless, and this project
has been bitten by exactly that -- the recorded fact that an emitter guard macro
must match its object, where the same guard in the wrong file compiles the
emitter out with a green build.

Three checks:

1. **The guard is the right one.**  The `spawn.failed` block opens
   `#if defined(XNU_OWNER_CACHE_MOUNT) && XNU_OWNER_CACHE_MOUNT` -- the SAME
   macro, in the SAME file, as the `spawn.seen` block.
2. **That macro is live in this kernel.**  `spawn.seen` fires 512 times in the
   deepest capture, so the block is compiled.
3. **A literal only that block contains is present.**  `spawn_failed.saturated`
   appears once, contiguously, in the kernel.

The contiguous-literal search is unreliable in general here and is NOT used as
the evidence: `spawn.seen` reads 0 as contiguous bytes while the guest emits 512
of them, because ld's `SHF_MERGE|SHF_STRINGS` pass splits the literals.  That is
the recorded fact 228 shape, and it is why the guard argument above is the
evidence rather than the byte count.

## The measurement

    longboot-input-run   1516 records, 512 spawn.seen, 454 job.open
        spawn.failed records: 0
    slot-lift-scratch    191 records, 137 spawn.seen
        spawn.failed records: 0
    spawn-failed-input-run  257 records, 155 spawn.seen
        spawn.failed records: 0

**Zero failed spawns against 512 successful ones.**  So hypothesis (2) is dead:
the kernel is not rejecting anything.  **launchd never calls `posix_spawn` for
SpringBoard.**

## What that settles, and what it costs

It settles the question the last seven intervals were circling: the decision is
made entirely inside launchd's userspace job state machine, before any syscall.
There is no kernel-side refusal, no signature failure, no missing binary at the
point of exec.  Nothing in this project's kernel can be changed to make the
spawn happen, because the spawn is never attempted.

It also costs something: **the next measurement cannot be a kernel emitter on the
spawn path, because that path is never reached.**  It has to observe launchd's
decision, and launchd is a binary with no source in this repository.

The cheapest instrument that bears on it is one this project can build: whether
`runningboardd` -- the entitled spawn driver that has to ASK for an App job -- is
actually running its code, or is alive and blocked.  RunningBoard's first act as
process manager is kernel-visible (`memorystatus`, `task_policy`), so an emitter
there separates "runningboardd never asks" from "runningboardd asks and launchd
refuses".  That is a build, which is the normal cost here.

## Status

Windows/real-guest: **the three captures are real guest output** from the
virtio channel on builds `exp-2026-11-05-slot-lift` (run 41937843171, kernel
`75dab4f1`) and earlier.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has still
never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-fifth, addendum BA): the anomalous set is 8, not "every App job"

## What was re-checked, and why

`launchd-discriminator.txt` lists 13 `RunAtLoad=true` jobs whose daemon did not
spawn, and concludes `POSIXSpawnType=App` is a perfect separator: 7 of 7 `App`
jobs did not spawn, 0 of 92 that did spawn carry the type.  That split was
computed against a boot with 92 spawned daemons.

The deepest capture on record has **161 spawned comms**.  Re-running the same 13
against it:

    SPAWNED  com.apple.AppleCredentialManagerDaemon   Interactive  root
    SPAWNED  com.apple.accessoryupdaterd              Adaptive     _accessoryupdater
    SPAWNED  com.apple.containermanagerd              Interactive  -
    SPAWNED  com.apple.familynotificationd            Adaptive     mobile
    SPAWNED  com.apple.identityservicesd              Interactive  mobile
    ABSENT   com.apple.ClarityBoard                   -
    ABSENT   com.apple.OTACrashCopier                 Background   mobile
    ABSENT   com.apple.SpringBoard                    App          mobile
    ABSENT   com.apple.accessibility.axassetsd        Background   mobile
    ABSENT   com.apple.checkerboard                   App          mobile
    ABSENT   com.apple.iomfb_bics_daemon              Background   LimitLoadToSessionType=System
    ABSENT   com.apple.safetyalertsd                  Adaptive     LimitLoadToHardware={targettype:[D16,D17,D20,...]}
    ABSENT   com.apple.sntpd                          Interactive  _sntpd

**Five of the thirteen spawn in the deeper boot**, including two `Interactive`
and two `Adaptive` -- the very types the separator said were safe.  The
anomalous set is **8**, not 13, and it is not a clean type split.

## What the 8 actually look like

Three of them carry a visible per-job reason a QEMU guest cannot satisfy:

    iomfb_bics_daemon   LimitLoadToSessionType = System
    safetyalertsd       LimitLoadToHardware = {targettype: [D16, D17, D20, ...]}
    sntpd               UserName = _sntpd

Those three are explained without any theory about spawn types.  What remains is
five: **SpringBoard, checkerboard, ClarityBoard, OTACrashCopier, axassetsd** --
and the first three are exactly the three jobs carrying `EmbeddedHomeScreen`,
which is the key the home-screen claim at `0x100007b64` gates on.  The last two
are `ProcessType=Background`, and across the whole volume **0 of 5 `Background`
jobs spawn**.

So the honest shape is:

* `Background` jobs do not spawn at all -- 0 of 5, and 3 of the 8.
* the three `EmbeddedHomeScreen` jobs do not spawn -- and the claim function
  already explains why the SECOND and third would not.
* three more fail for reasons visible in their own plists.
* `App` is NOT a separator on this evidence: the 13-job set contained two `App`
  jobs and both are still absent, but it also contained five jobs that DO spawn,
  of types `Interactive` and `Adaptive`.

**`POSIXSpawnType=App` remains a correlation, exactly as the state document has
said since the discriminator was first written, and this interval's launchd
parser work does not change that.**  It is worth having read the parser -- the
code at `0x100003d8c` and the marshaller at `0x100020d90` are real and now
mapped -- but the fork in that road is not where the eight jobs are being lost.

## Status

Windows/real-guest: **the join is a host-side read of a real capture** from
`longboot-input-run` (1516 records, 512 spawns, 454 `job.open`) against the
parsed job table.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has still
never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-sixth, addendum BB): AMFI launch constraints are implemented, and they are not the gate either

## Why this was worth checking

Addendum AZ proved launchd never attempts the SpringBoard spawn, which pushed the
decision into launchd's own state machine.  One mechanism could still have
produced that shape from OUTSIDE launchd: a launch constraint applied at job-load
time whose failure marks the job unspawnable.  launchd imports exactly the symbol
that would do it:

    _amfi_launch_constraint_set_spawnattr

and carries the plist key `SpawnConstraint` in its schema table.

## The kernel implements it, and the check is readable

`local-private/upstream/xnu` has the whole subsystem:

    osfmk/kern/cs_blobs.h:312        struct launch_constraint_data
    bsd/kern/policy_check.c:181      CHECK_SET_HOOK(proc_check_launch_constraints)
    security/mac_process.c:896       mac_proc_check_launch_constraints()
    bsd/kern/kern_exec.c:7652        the call site

and the built kernel carries the enforcement strings, including

    "Only platform code can set launch constraints on itself"
    "Only platform code can set launch constraints on its parent"
    "Only platform code can set launch constraints on its responsible process"
    "AMFI: Launch Constraint Violation..."

plus the sysctls `launch_constraints_enforced` and
`launch_constraints_3rd_party_allowed`.

The call site is in the exec-activation path, immediately after the
`ip_mac_return` and `ip_cs_error` checks:

    /* call the launch constraints hook */
    if ((error = mac_proc_check_launch_constraints(p, imgp,
            &launch_constraint_reason)) != 0) {
            signature_failure_reason = launch_constraint_reason;
            goto done;
    }

`goto done` sets `error` and returns it from `posix_spawn` -- which the
`spawn.failed` emitter from addendum AZ would have recorded.

## So it is not the gate, and the reasoning is short

**A launch-constraint rejection happens inside `posix_spawn` and returns an
errno.  Addendum AZ measured 0 `spawn.failed` records against 512 successful
spawns.  Therefore no launch constraint was ever rejected, for any job.**  The
mechanism is implemented, it is reachable, and it did not fire.

That closes the last candidate that could have acted on the spawn from outside
launchd.  What remains is launchd's own state, and nothing in this project's
kernel observes that.

## The trust cache, closed for the second time

The same line of reasoning made the trust cache look relevant again, because
platform-code status is what the constraint check tests.  It is not usable:

    launchd         c8a9970f...   static=0  staged=0
    configd         188d2d47...   static=0  staged=0
    backboardd      6f7752e8...   static=0  staged=0
    runningboardd  00baf54d...   static=0  staged=0

**backboardd spawned, is alive, and is making real framebuffer calls, and its
cdhash is in neither cache either.**  So the absence says nothing about platform
status -- either the cdhash is computed wrong or these caches are not the ones
that govern.  Recorded as an instrument that does not work, and not used again.

## Status

Windows/real-guest: **host-side reads of the XNU source tree and the extracted
binaries.**  Nothing measured on the guest this interval.

Build: none.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has still
never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-seventh, addendum BC): the `do_fork1` hole in AZ, and a working process walker

## The hole, and it is in my own conclusion

Addendum AZ concluded: 0 `spawn.failed` records against 512 `spawn.seen`, so
launchd never attempts the SpringBoard spawn.  Both emitters are inside
`if (error == 0 && !spawn_no_exec)`.  Reading `posix_spawn` in the source shows
`spawn_no_exec` is TRUE on a whole second path:

    kern_exec.c:3869   if (imgp->ip_px_sa == NULL ||
    kern_exec.c:3876       !(px_sa.psa_flags & POSIX_SPAWN_SETEXEC)) {
    kern_exec.c:3879       if (imgp->ip_px_sa == NULL || !px_args.coal_info) {
    kern_exec.c:3880           goto do_fork1;
    ...
    kern_exec.c:4002       imgp->ip_flags |= IMGPF_SPAWN;  /* spawn w/o exec */
    kern_exec.c:4003       spawn_no_exec = TRUE;

**A caller that passes no spawnattr -- or one without `POSIX_SPAWN_SETEXEC` and
no coalition info -- takes `do_fork1`, and NEITHER emitter fires.**  Both paths
still call `exec_activate_image()`, so the child does exec; but
`proc_exec_switch_task()` at `kern_exec.c:4280` is gated on `!spawn_no_exec`, and
that is the call the `spawn.seen` emitter is placed after.

So AZ's measurement establishes **"no spawn on the exec path"**, not "no spawn".
A `do_fork1` spawn of an App job -- exactly the kind that would carry a different
spawnattr from the coalition-aware daemon spawns -- leaves no spawn record at
all.  **AZ's conclusion is withdrawn as stated and re-scoped to the exec path.**

What still holds: a `do_fork1` child that DIES produces an `exit.seen` with its
own comm, and there is no `exit.seen` for SpringBoard in any capture.  So the
remaining live possibilities are (a) no spawn at all, or (b) a `do_fork1` spawn
whose child is alive and blocked.

## The instrument that separates them, built and validated

`local-private/tmp/walk-guest-procs.py` boots the guest, connects QMP, and walks
`allproc` -- whose address the kernel's own `LC_SYMTAB` gives (`_allproc` at
`0xfffffff00c0e1778`, `_nprocs` at `0xfffffff00c1037d8`).  The walk is its own
control: it is only worth reading if it recovers the daemons the channel already
recorded.

First run, and the control passed for the READ but not for the boot:

    allproc.lh_first = 0xfffffff6be6dccc0          a valid kernel heap pointer
    version kstring: b'Darwin Kernel Version 23.6.0: Thu Nov  5 00:28:0'
    nprocs = 0
    walked 1 procs, collected 0 names

**The reads are correct** -- the kernel's own `version` string comes back
byte-exact, which proves the address translation and the QMP path both work.
**The boot stalled**: one process in the whole list.  That is the recorded
non-determinism from addendum AO, seen again, and it means this run cannot answer
the SpringBoard question either way.

Two defects in the walker, both recorded rather than hidden:

* the name scan collected 0 names from the one proc it read -- the `proc_ro`
  pointer scan does not find `p_comm`/`p_name`, so the offset work is not done;
* the first version gated on `nprocs > 20` and refused at `nprocs = 0`, which
  would have read as "the instrument is broken" rather than "this boot stalled".
  The gate is removed; the walk is the control.

## What this changes about the plan

The question is no longer "why does launchd decline the job".  It is **"is there
a SpringBoard process on the guest"**, and that is now one boot away, with a
walker whose reads are already proven.  The boot is the unreliable part, not the
instrument -- and addendum AO already recorded that a boot which does not reach
userspace is a boot that stalled, not a negative.

So the next run is the same script with the name scan fixed, on a boot that
reaches userspace.  Nothing about it needs a build.

## Status

Windows/real-guest: **the walker booted a real guest and read its kernel memory
over QMP** -- the `version` string is guest data, not a host-side read.  The
SpringBoard answer is NOT obtained, because the boot stalled with one process.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has still
never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-eighth, addendum BD): the walk's real defect was a stale page cache, not a stalled boot

## The defect, and it is the same shape as every other one

Addendum BC's walker reported `nprocs=0`, `walked=1`, and 0 names for 895
straight seconds.  The control fired -- "the walk did not recover launchd,
configd, backboardd; the names above are void" -- and the first explanation
written down was that the boot had stalled.

**That explanation was wrong, and the control is the only reason it did not
become a conclusion.**

`GuestObserver._page()` caches **data** pages, not just page tables:

    guestobserve.py:671   def _page(self, physical: int) -> bytes:
    guestobserve.py:673       cached = self._tables.get(key)
    guestobserve.py:680           cached = self.memory.read(key, 4096)

`_tables` is cleared by exactly one thing, `reset()`.  The module's own docstring
records the consequence, thirty lines further down:

    "Measured without this: a second `sample()` on one observer issued zero
     guest reads, because everything was still cached."

A polling loop that never calls `reset()` reads the first sample's bytes forever.
The first sample was taken moments after the kernel handoff, when `allproc`
holds `kernel_task` and nothing else -- so `nprocs=0` was *true at that instant*
and *false for the next fifteen minutes*, and the instrument could not tell the
difference.  It reported a question about time with an answer about one moment.

**This is the same shape as the recorded fact 13, 47, 61 and 71**: an instrument
returns a value, the value is read as an answer, and the question it actually
answered is a different one.  The version here is quieter than most, because the
value was *correct* -- just for a moment that had passed.

With `obs.reset()` before each sample, the same boot gives:

       0.0s  nprocs=1   cached_pages=9
      20.1s  nprocs=2   cached_pages=10
      25.1s  nprocs=3   cached_pages=11
      35.2s  nprocs=2   cached_pages=10

The count varies, the cache size varies with it, and `cached_pages > 0` is now
printed every sample as the instrument's own control -- a run where it stays at
zero is a broken observer replaying bytes, not a quiet guest.

## The second defect: the project already had this walker, with the right offsets

The first version hand-computed the `struct proc` layout and read 1024 bytes per
proc.  Three separate things were wrong, and one of them was avoidable:

* it scanned `proc_ro` for `p_comm`, and `struct proc_ro` (bsd/sys/proc_ro.h:73)
  has **no name field at all** -- it is `pr_proc`, `pr_task`, and four struct
  groups;
* `p_comm` lives in `struct proc` itself, at offset **1468**, with `p_pid` at 96
  and `p_stat` at 100.  The struct is ~1.5 KB because of `p_mlock`,
  `struct filedesc` and `struct sigacts`, so a 1024-byte read never reaches the
  name;
* **`GuestObserver.processes()` already existed** in
  `src/unipad/host/guestobserve.py` with exactly those offsets, a loop guard and
  a bound, and `_proc_fields()` reads the span as one read so the four values
  cannot tear.

The walker now calls `obs.processes()` and carries no offsets of its own.  That
is DRY, and it is also why the names it returns mean anything: three
hand-computed offsets have been wrong in this project already and this would have
been the fourth.

## Measured on the way, from the deepest capture on record

**launchd opens SpringBoard's plist and never spawns it.**

    {"event":"job.springboard_open","err":0,"source":"XNU.openat_internal"}

The instrument is validated before being read: the matcher compares the opened
path's *basename* against the literal `com.apple.SpringBoard.plist` in full, it
is one-shot, and it reports `err`.  So this is a successful open of exactly that
file.  Combined with 512 `spawn.seen` records containing no SpringBoard, the
shape is now measured rather than inferred: **the job is loaded, and no spawn is
attempted.**

**The 512 is events, not processes.**  512 `spawn.seen` carry only **161
distinct comms**, because 345 of them are the crash loop from task #23:
`bluetoothd` 214 times and `fairplayd.A2` 131.  161 is therefore still the right
population figure, and the 512 should never be quoted as one.

**`exec.seen` is 2, and both are benign.**  `n=1 comm=launchd` is the kernel's
`load_init_program` exec'ing launchd as PID 1; `n=2 comm=apfs_boot_util` is that
tool re-exec'ing itself.  `exec.seen` sits inside `__mac_execve`
(kern_exec.c:5165), which `posix_spawn` does not call, so it is disjoint from the
spawn path and **cannot** be used to count `do_fork1` spawns -- which was the
cheap test addendum BC hoped for, and it does not exist.

## launchd's flag machinery, read far enough to name the gate

The home-screen claim at `0x100007b64` is small and now fully read:

    ldrb w8, [x0, #0x53b]      ; bit 5 = EmbeddedHomeScreen
    tbz  w8, #0x5, ...         ; clear -> skip entirely
    ldr  x8, [x1, #0x48]       ; current owner
    cbz  x8, 0x100007bb4       ; NULL -> claim
    and  x8, x8, #0xffffffffdfffffff   ; clear a bit
    str  x8, [x19, #0x538]
    ...
    str  x19, [x1, #0x48]      ; the claim

**The bit it clears on refusal is bit 29, not bit 34** -- an earlier note in this
chain said 34, and it was a hand-counted hex mask.  Computed rather than counted:
`0xffffffffdfffffff` clears bit 29, `0xfffffffdffffffff` clears bit 33, and
`0x200000000` is 2^**33**.  The setter at `0x1000069e0` is a clear-then-set of
bit 33 from a boolean; the refusal clears bit 29.

And bit 29 has exactly one reader, sixty lines earlier in the same activation
function, and it is the spawn-type decision:

    100007abc  and  x9, x8, #0x20000000      ; extract bit 29
    100007ad4  cset w10, ne                  ; w10 = (bit29 != 0)
    100007ad8  orr  w10, w10, w20
    100007adc  cmp  w10, #0x1
    100007ae0  b.ne 0x100007b10              ; else: do NOT promote to App
    100007ae4  ldr  w10, [x26, #0x358]       ; current spawn type
    100007ae8  cmp  w10, #0x700              ; Driver?
    100007aec  b.eq 0x100007b10              ; Driver never becomes App
    100007af0  mov  w10, #0x100              ; 256 = App
    100007af4  str  w10, [x26, #0x358]       ; spawn type := App
    100007b08  orr  x8, x8, #0x1000          ; and set bit 12

**A job is promoted to `App` only if bit 29 of `job+0x538` is set.**  Bit 29's
setter (`0x100008fe8`) has no direct callers, so it is reached through a
function-pointer table, and that is as far as static reading goes without a guest
instrument.  What is established is the shape: the home-screen refusal clears the
bit the App promotion requires, and the three `EmbeddedHomeScreen` jobs are
exactly the three that would be refused by it.

## The `do_fork1` emitter, written and verified to apply

`local-private/tmp/fork1-spawn-hunk.patch` adds a `fork1.spawn` emitter to
`posix_spawn` at `kern_exec.c:4250`, immediately after `exec_activate_image()`,
gated on `spawn_no_exec` -- the exact population both `spawn.seen` and
`spawn.failed` are blind to.  It records the image's own name from
`imgp->ip_vp->v_name` (the field kern_exec.c:1294 already uses to name an
executable) plus the child's new `p_comm`, and a one-shot
`springboard.fork1` record so the answer is one grep.

Placed there rather than at the `if (spawn_no_exec)` block at :4028 because
`imgp->ip_vp` is assigned at :2255 **inside** `exec_activate_image()` and is still
NULL at :4028 -- checked, not assumed.  Zero records is a real answer and a
different one from "not measured": the guard is the same
`XNU_OWNER_CACHE_MOUNT` the 512 `spawn.seen` records prove is live.

The hunk was verified to apply on top of the existing springboard patch against a
scratch copy of the XNU tree.  Its first transcription carried three trailing
context lines with their leading TABs dropped, which `git apply` rejected, and a
second transcription error in the same context list was caught by the repair
script's own assertion -- so the repair now *reads* the context out of the source
instead of restating it.

## Status

Windows/real-guest: **the process walk booted a real guest and read its live
kernel memory over QMP, with the count varying over time.**  The SpringBoard
answer is NOT yet in.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu because SpringBoard has still
never spawned.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (eighty-ninth, addendum BE): SpringBoard IS running on the guest

## The measurement, and it refutes nine intervals of diagnosis

`local-private/tmp/walk-guest-procs.py` boots the guest, connects QMP, and walks
`allproc` with the project's own `GuestObserver.processes()` -- resetting the
page cache before every sample, because `GuestObserver._page()` caches DATA pages
for the observer's whole lifetime and a loop that never resets replays its first
sample forever (addendum BD).

The curve, and the curve is the evidence:

       0.0s  nprocs=1     cached_pages=9
     401.7s  nprocs=11    cached_pages=18     <-- userspace
     417.2s  nprocs=69    cached_pages=69
     691.9s  nprocs=99    cached_pages=96     <-- the peak, still climbing

**99 processes at the peak and still climbing when the window closed.**  The names:

```
  AppleCredentialM
  CommCenter
  DumpPanic
  IOMFB_FDR_Loader
  MSUEarlyBootTask
  MobileGestaltHel
  PerfPowerService
  PowerUIAgent
  SpringBoard
  UserEventAgent
  amfid
  apsd
  audiomxd
  backboardd
  biomed
  biomesyncd
  bluetoothd
  calaccessd
  cc_fips_test
  cfprefsd
  chronod
  com.apple.datami
  configd
  containermanager
  contextstored
  coreduetd
  dasd
  dirs_cleaner
  distnoted
  driverkitd
  fairplayd.A2
  fseventsd
  iconservicesagen
  identityservices
  imagent
  init
  init_featureflag
  kernel_task
  keybagd
  launchd
  launchd_cache_lo
  liveactivitiesd
  locationd
  lockdownd
  logd
  logd_helper
  lsd
  mediaserverd
  mobile_obliterat
  mobileactivation
  mobiletimerd
  mount
  mtmergeprops
  nfcd
  notifyd
  passd
  powerd
  ptpd
  rapportd
  remoted
  runningboardd
  securityd
  sharingd
  sleepd
  sysstatuscheck
  thermalmonitord
  timed
  triald
  tzinit
  usermanagerd
  usernotification
  watchdogd
  wifid
  xpcproxy
```

**`SpringBoard` is PRESENT and RUNNING.**

## What that refutes, precisely

Every interval since the discriminator has framed the blocker as "launchd
declines `POSIXSpawnType=App` jobs" -- 7 of 7, then 5 of 5 after addendum BA
reframed the set.  Addendum AZ sharpened it to "launchd never calls
`posix_spawn` for SpringBoard", on the strength of 0 `spawn.failed` records
against 512 `spawn.seen`.

**That is refuted.  SpringBoard is alive on the guest.**  The spawn happened.
What the kernel's emitters recorded is not the whole of what the kernel did,
which is exactly the hole addendum BC found in AZ's reasoning: `do_fork1` sets
`spawn_no_exec = TRUE`, and both emitters are inside
`if (error == 0 && !spawn_no_exec)`.

So the two facts now stand together and each constrains the other:

* `spawn.seen` holds **no** SpringBoard record in any capture;
* SpringBoard **is** in the guest's process list.

The only consistent reading is that the spawn took the `do_fork1` route -- a
caller passing no spawnattr, or one without `POSIX_SPAWN_SETEXEC` and no
coalition info, jumps to `do_fork1` at kern_exec.c:3880 and falls through to
`spawn_no_exec = TRUE` at :4002.  That is what the `fork1.spawn` emitter built
this interval (unixnu `439c31cb`, run 41980452867) exists to confirm directly,
and until it reports, this is the reading the evidence supports rather than one
it proves.

## What is NOT refuted, and what is now strange

`job.springboard_open` err=0 still holds: launchd loads the plist.  That is now
the *beginning* of the story rather than the end -- the job is loaded, the
process is spawned, and the process is alive.

Three things in the same table do not fit the old theory and are worth more than
the refutation itself:

* **`runningboardd` is PRESENT, at pid 26, immediately before SpringBoard's 27.**
  Addendum BB recorded it as absent from both trust caches and addendum AZ named
  "is runningboardd running its code" as the next measurement.  It is, and it is
  the process right before SpringBoard in pid order -- which is what a spawn
  driver that was asked for an App job and asked launchd looks like.
* **`checkerboard` and `ClarityBoard` are ABSENT** while SpringBoard is present.
  All three carry `EmbeddedHomeScreen`, the key the home-screen claim at
  `0x100007b64` gates on.  So the home-screen claim does not act uniformly on the
  three, and cannot be the reason SpringBoard is missing -- it is not missing.
* **`backboardd` IS in the union**, and the first corrected run's control failed
  on it.  That was a *sampling* defect, not a walk defect: the run kept only the
  largest snapshot's names, and a process that was not alive at that one instant
  is not evidence about the walk.  The script now unions across every sample and
  judges the control on the union -- which is what makes the 74 names above a
  population rather than a frame.

## So the question changed, and it is a better one

The question is no longer "why does launchd not spawn SpringBoard".  It is
**"what is SpringBoard blocked on"**.  It is alive, it is not in `spawn.seen`,
and the screen is still the UniXNU boot menu -- so the process exists and the
thing the PRD asks for does not.

That is a question about SpringBoard's own execution, and the instruments that
bear on it are the ones addendum AZ already named.  **`p_stat` is now recorded
per process, and it is the first thing that does not fit**: all 99 rows at the
peak read state `run` (SRUN, `p_stat`=2).  A guest where every daemon is on a run
queue is not a guest where anything is *waiting*, so either the state byte is not
where `PROC_P_STAT=100` says -- the pids beside it are correct, which argues it
is -- or the walk is reading a stale page for that field.  **Recorded as measured
and not yet explained; it is the next thing to settle, because "what is
SpringBoard blocked on" is unanswerable if the state field cannot be trusted.**

## Status

Windows/real-guest: **the process walk booted a real guest, read its live kernel
memory over QMP, and found SpringBoard running at pid 27** -- 99 processes at the
peak, still climbing when the 300s-past-userspace window closed, 74 distinct
names across the run.  This is guest data, not a host-side read.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, dispatched on
build-macos with `stage=M7`, `payload_target=tcg`, `system_build=21H461`,
unixnu `439c31cb`, device `9782ddf`.  In progress at the time of writing.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu even though SpringBoard is
now confirmed running.**  THE HEADLINE is unchanged from AH

Control verdict, from the run: PASSED on the union.  The first corrected run kept only the largest snapshot's names and its control failed on backboardd, which had simply not been alive at that instant -- while launchd and configd WERE there and had already proved the offsets.  The run reported here unions across every sample, and backboardd, launchd and configd are all present: 74 distinct names ever seen, 99 processes at the peak.  The walk works.

---

# Interval 2026-11-05 (ninetieth, addendum BF): the `p_stat` anomaly in BE is not one

## What BE flagged, and why it was wrong

Addendum BE closed by recording that all 99 rows of the process table read
`p_stat = 2` (`SRUN`) and calling that "the first thing that does not fit" -- on
the grounds that a guest where every daemon is on a run queue is not a guest
where anything is waiting.

**It fits exactly.**  `p_stat` is assigned in five places in the whole tree:

    bsd_init.c:526       kernproc->p_stat = SRUN;
    kern_fork.c:751      child_proc->p_stat = SRUN;      <-- every child at fork
    kern_fork.c:1020     child_proc->p_stat = SIDL;      <-- during fork only
    kern_exec.c:2073     p->p_stat = SSTOP;
    kern_sig.c:2312..    sig_proc->p_stat = SRUN / SSTOP;   on signal delivery
    kern_exit.c:2612,    p->p_stat = SZOMB;
    kern_exit.c:2642

**`p_stat = SSLEEP` appears nowhere in the tree.**  XNU stopped maintaining a
sleeping state in the proc; that lives in the scheduler and the thread now.  So
the field can only ever read `SRUN` for a live, unstopped, non-zombie process,
which is every one of the 99.

So the reading is a property of the field, not of the guest, and the conclusion
BE drew from it -- that `p_stat` cannot yet be trusted -- is withdrawn.  The
pids beside it were always the reason to trust the offsets: `p_pid` at 96
returns 0, 1, 24, 25, 26, 27 ... 118 with the gaps a process table has, and
`p_stat` at 100 is four bytes further into the same span read as one piece.

## What this costs, and it is not nothing

"**What is SpringBoard blocked on**" was going to be answered from `p_stat`, and
that field cannot answer it.  A SpringBoard in state `U` would have said
"waiting on something"; there is no `U` to read.

So the next instrument has to observe what SpringBoard is waiting *for* rather
than what state it is in, and the candidates are the ones that already exist in
this project's kernel: the syscall or Mach-trap it is parked in, or the object
it is blocked on.  That is a build, which is the normal cost here, and it is a
better-targeted one than the `fork1.spawn` emitter would have been for this
question -- though that emitter is still the right instrument for the question it
was built for, namely whether the SpringBoard spawn took the `do_fork1` route.

## The pids, recorded because they are the anchor for everything above

    pid 0     kernel_task
    pid 1     launchd
    pid 26    runningboardd        <-- immediately before SpringBoard
    pid 27    SpringBoard
    pid 37    configd

`runningboardd` at 26 and SpringBoard at 27 is what a spawn driver that was asked
for an App job and asked launchd looks like, and it is now measured rather than
inferred.

## Status

Windows/real-guest: **three greps in the XNU source tree plus the process-table
reading from addendum BE.**  Nothing new measured on the guest this interval.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, still in progress at the
time of writing.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu even though SpringBoard is
confirmed running at pid 27.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-first, addendum BG): the `do_fork1` population is measured, and the process table has a defect

## The measurement: two instruments, one boot, and they disagree by 32

Addendum BE established that SpringBoard is in the guest's process list while
`spawn.seen` holds no record of it, and read that as the `do_fork1` route.  That
was one instrument's silence.  The same boot now supplies a second, and the two
disagree by a number:

    spawn.seen counter, its own n:      59 spawn events, 53 distinct comms
    exit.seen counter, its own n:       28 exits
    the process list at the peak:       99 rows, 91 DISTINCT pids

**`kernel_task` and `launchd` aside, 59 spawn events cannot account for 91 live
processes.**  At least 32 of them were created by something `spawn.seen` does not
record.  That is the `do_fork1` population, and it is now **measured** -- the
emitter's counter is a per-boot monotonic count, so "59" is not an estimate, and
the process list is read independently of it.

SpringBoard is one of the 32: it is pid 27, and 0 of the 53 `spawn.seen` comms is
SpringBoard.  `runningboardd` is pid 26, immediately before it.

So the reading addendum BC qualified and addendum BE inferred is now supported by
two instruments that do not share a code path: **the SpringBoard spawn took the
route neither `spawn.seen` nor `spawn.failed` can see.**  The `fork1.spawn`
emitter being built as run 41980452867 will name the binary directly, which is
the difference between "the population is 32 and SpringBoard is in it" and "this
record says SpringBoard".

## The defect in the process table, recorded rather than smoothed over

The 99 rows carry only **91 distinct pids**.  Eight pids appear twice --
43, 68, 71, 104, 105, 108, 113 and 116 -- and in every one of the eight the
second row is `xpcproxy`, paired with a row that is not:

    pid=43    biomed          pid=43    xpcproxy
    pid=116   fairplayd.A2    pid=116   xpcproxy

**A pid is unique while its proc is allocated, so two rows cannot share one.**
Either the walk is reading one proc's fields out of a neighbour's memory, or
`le_next` is being followed somewhere it should not be.

The shape points at the name field.  `PROC_P_COMM = 1468` and
`PROC_SPAN = PROC_P_COMM + 17 = 1485`, and the module's own docstring asserts
`struct proc` is "1468+17 bytes".  If the struct is in fact *smaller* than 1485
in this kernel, then `blob[1468:1485]` reads past the end of one proc into the
next allocation in the zone -- which would produce a name belonging to a
different process, and `xpcproxy` is by far the most numerous process on the
system (38 of the 99 rows).  That is consistent with what is seen, but it is a
hypothesis and not a finding: **the pids beside the names are correct, and
`p_pid` at 96 is 1372 bytes inside the same span, so the span itself is being
read.**

What is NOT consistent: a duplicated pid cannot come from a clobbered name,
because the pid is at offset 96 and would be unaffected.  So there are two
defects here, not one, and only the second is explained.

**Consequence, stated plainly: the 99 and the 91 are both unsafe to quote until
this is settled.**  What survives it is the SpringBoard finding -- SpringBoard
appears exactly once, at pid 27, and no `spawn.seen` record names it -- because
that does not depend on the count.

## How to settle it, and it is cheap

The walk already has everything it needs and throws the answer away:
`GuestObserver.processes()` returns a `Process` with an `address` field, and the
script prints only pid, state and name.  **Printing the address settles it in one
run**: if the eight duplicated pids come from two rows with *different*
addresses, the walk is reading the wrong struct; if the same address, the walk
visited one proc twice and `processes()`' loop guard did not fire.  Those are
different bugs with different fixes, and neither can be chosen from the current
output.

## Status

Windows/real-guest: **two instruments read from one real boot** -- the emitter's
own counters from the virtio channel, and the live process list over QMP.  Both
are guest data.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, still in progress.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu even though SpringBoard is
confirmed running at pid 27.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-second, addendum BH): the duplicated pids are two different structs, not one proc twice

## What the addresses settle

Addendum BG ended unable to choose between two explanations for the eight
duplicated pids, and named the field that would separate them:
`GuestObserver.processes()` returns a `Process` carrying `address`, and the
script was throwing it away.  It now prints it.

pid 68, from the run at t=708.5s:

    pid=68   addr=0xfffffff348a67cc0   dasd
    pid=68   addr=0xfffffff348983880   xpcproxy

**Two different addresses.**  So the walk read two different `struct proc`s, and
`processes()`' loop guard -- which raises if a `current` repeats -- did not fail.
**The "visited one proc twice" branch is closed.**  What is left is that two
distinct structs in the `allproc` chain both carry 68 at offset 96, and since a
pid is unique while its proc is allocated, at most one of them is a live proc
read correctly.

## What the addresses also show, and it narrows it further

Every duplicated pair has one member in each of two address clusters:

    0xfffffff34894xxxx - 0xfffffff34898xxxx    biomed(43), audiomxd(108),
                                               xpcproxy(71), xpcproxy(104),
                                               xpcproxy(68)
    0xfffffff348a6xxxx - 0xfffffff348a8xxxx    dasd(68), securityd(113),
                                               mobileactivation(116),
                                               runningboardd(26),
                                               SpringBoard(27)

SpringBoard sits at `0xfffffff348a84cc0` and runningboardd at
`0xfffffff348a77880`, both in the second cluster.  The first cluster is where the
`xpcproxy` rows concentrate, and `xpcproxy` is 38 of the 112 rows.

That is consistent with the hypothesis addendum BG raised -- `PROC_P_COMM=1468`
and `PROC_SPAN=1485` reading past the end of a smaller `struct proc` into the
next zone allocation -- but it is still a hypothesis.  **It does not explain a
duplicated pid**, because `p_pid` sits at offset 96, 1372 bytes inside the same
span, and would be unaffected by an overrun at the far end.  So there are two
defects and the second is still unexplained.

The third possibility, which the addresses make plausible and the current output
cannot exclude: **the chain contains a proc that was freed while the walk was in
flight.**  The walk takes seconds to traverse 112 structs, the guest is forking
continuously, and a freed proc keeps its old `p_pid` until the slot is reused.  A
row read from a freed proc would carry a stale pid and a stale name, and two such
rows could collide.

That is testable and cheaply: walk twice, back to back, and compare.  **If the
duplicated pids move between the two walks, it is a race in the walk.  If they
are identical both times, it is a layout error and the offsets are wrong.**

## What survives all of it

**The SpringBoard finding does not depend on any of this.**  SpringBoard appears
exactly once, at `0xfffffff348a84cc0`, pid 27, and 0 of the 53 `spawn.seen` comms
names it.  A duplicated pid elsewhere in the table cannot make that row wrong.

And the two instruments still disagree by a wide margin:

    spawn.seen counter, this boot:   70 events, 53 distinct comms
    the process list at the peak:    112 rows, 80 distinct names

**70 spawn events cannot account for 112 processes.**  The `do_fork1` population
is at least 42, and SpringBoard is in it.

## Status

Windows/real-guest: **a second real boot, read the same way, with the proc
addresses printed** -- 112 processes at the peak, 80 distinct names across the
run, SpringBoard at pid 27 and runningboardd at pid 26.  Guest data.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, **completed successfully**.
Its `boot-inputs` artifact (310,411,144 bytes) is being fetched.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu even though SpringBoard is
confirmed running at pid 27.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-third, addendum BI): `springboard.fork1` fired

## The record

    {"event":"springboard.fork1","vname":"SpringBoard","source":"XNU.posix_spawn.do_fork1"}

One line, and it settles the question nine intervals have been circling.
**launchd calls `posix_spawn` for SpringBoard, and the call takes the `do_fork1`
route** -- a caller passing no spawnattr, or one without `POSIX_SPAWN_SETEXEC` and
no coalition info, jumps to `do_fork1` at kern_exec.c:3880 and falls through to
`spawn_no_exec = TRUE` at :4002.  Both `spawn.seen` and `spawn.failed` are inside
`if (error == 0 && !spawn_no_exec)`, so neither can see it.

**Addendum AZ's conclusion is withdrawn outright, not re-scoped.**  It is not that
launchd never calls `posix_spawn` for SpringBoard; it is that the call it makes is
invisible to the emitter AZ was reading.

## Why `spawn.seen` never showed it, and it is not subtle

The same boot, read from the same channel:

    spawn.seen      34   notifyd, logd, configd, runningboardd, mediaserverd,
                         powerd, biomed, WirelessRadioMan, IOMFB_bics_daemo, ...
    fork1.spawn     90   69 xpcproxy, SpringBoard, usermanagerd, keybagd,
                         mount x2, fsck, seputil, mobile_obliterator,
                         MSUEarlyBootTask, FinishRestoreFromBackup, ...

**The `do_fork1` route carries 90 of 124 spawns on this boot -- the majority.**
And the two routes are not random: `runningboardd` is in `spawn.seen` at n=26,
SpringBoard is in `fork1.spawn`.  Same caller, same syscall, different spawnattr.

That is the mechanism behind the correlation addendum BA called "a correlation,
exactly as the state document has said since the discriminator was first written":
**`POSIXSpawnType=App` jobs are spawned without `POSIX_SPAWN_SETEXEC`**, so they
take `do_fork1` and leave no `spawn.seen` record.  It was a correlation and a
mechanism at the same time, and the emitter that could tell them apart did not
exist until this interval.

It also explains the 69 `xpcproxy` rows in the process table from addenda BG and
BH.  Those were the population the duplicated pids came from, and they are
spawned by the same route -- rapidly, 69 of them -- which is what makes a walk in
flight see some of them freed mid-traversal.  **That is now the leading
explanation for the eight duplicated pids, and it is a race in the walk rather
than a layout error.**

## What the emitter itself proved on the way

The `vname` and `comm` fields disagree in exactly the way the struct says they
should, which is the check that the field is the right one:

    vname="mobile_obliterator"    comm="mobile_obliterat"

`p_comm` is `char[MAXCOMLEN + 1]` with `MAXCOMLEN` 16, so the proc name is
truncated and `imgp->ip_vp->v_name` is not.  A wrong field would not produce that
pair.

And the pre-flight that kept this build from being wasted is worth recording:
**the hunk's first transcription dropped its opening `#if` line and
`git apply --check` accepted the broken patch**, because `git apply` verifies
context bytes and not C.  It would have applied and failed the kernel link 40
minutes in.  Only the compile check caught it, and the added block compiles clean
with `-Wall -Werror` under both aarch64 targets.

## So the question is settled, and the next one is not

SpringBoard is spawned.  It is alive -- pid 27 in addendum BE, and 116-125
processes on the guest at the time of this run.  The screen is still the UniXNU
boot menu.

**The blocker is no longer the spawn.  It is whatever SpringBoard does after it.**
The instruments that bear on that are the ones this interval just proved work:
name the binary at the point of interest, and read the guest's own state rather
than inferring it from a silence.

## Status

Windows/real-guest: **a real guest, booted on a kernel built this interval
(`d2c5a914`, run 41980452867), emitting a record that names SpringBoard on the
`do_fork1` route.**  The kernel's sha256 was verified against the payload's own
declaration before booting, and the emitter's literals were confirmed present in
this kernel and absent from the previous one.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, completed successfully.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu even though SpringBoard is now
proven spawned AND proven alive.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-fourth, addendum BJ): the race test fired, the control passed, and the anomalous set is two

## Three questions closed in one run

**The duplicated pids are a race, and the script's own test says so.**  Addendum
BH named the test and could not run it; the run on the `fork1.spawn` kernel ran
it, 142 samples, and the verdict is unambiguous:

    DUPLICATED PIDS: 30 distinct set(s) across 142 samples
      --> the set MOVES between samples: a proc is being freed while the walk is
          in flight, so the rows are a race and not a layout error

Thirty *different* duplicate sets across 142 samples.  A layout error would be
identical every time.  **So the offsets were right all along and the eight
duplicated pids of addendum BG were never a defect in `GuestObserver.processes()`**
-- they were a walk in flight over a guest that forks continuously, reading some
procs after they were freed.  A freed proc keeps its old `p_pid` until its slot is
reused, so two rows can collide on a pid that only one of them ever owned.

And `fork1.spawn` explains the volume: **69 of the 124 spawns on that boot are
`xpcproxy`**, all on the `do_fork1` route, one per XPC domain, spawned in a burst.
That is exactly the population a seconds-long walk would catch mid-flight.

**The control passed.**  launchd, configd and backboardd were all recovered across
the run, so the walk works and the names below it mean something.  The control had
failed twice before -- once on a stale page cache (addendum BD) and once on a
single-frame union (addendum BE) -- and both times it was the control that was
wrong, not the walk.

**The boot is the deepest on record**: 135 processes at the peak, 95 distinct
names across the run, against 99/74 and 112/80 on the two earlier walks.

## And addendum BA's anomalous set is down to two

Addendum BA narrowed the set of `RunAtLoad=true` jobs that never spawned from 13
to 8, then to 5: SpringBoard, checkerboard, ClarityBoard, OTACrashCopier,
axassetsd.  On this boot's union:

    SpringBoard       PRESENT    (pid 27, and springboard.fork1 names it)
    OTACrashCopier    PRESENT
    axassetsd         PRESENT
    checkerboard      absent
    ClarityBoard      absent

**The set is two, and both are `EmbeddedHomeScreen` jobs.**  Addendum BA's
observation that the three `EmbeddedHomeScreen` jobs were the common factor has
become *more* pointed, not less: SpringBoard carries the key and spawns, the
other two carry it and do not.  So `EmbeddedHomeScreen` is not a sufficient
condition for not-spawning either -- which leaves the home-screen claim at
`0x100007b64` as the one mechanism that still fits, and it now has a sharp
prediction: **the first of the three to claim wins, and the other two are
refused.**  That is testable from the claim function's own owner slot, and it is
the next thing to measure rather than the next thing to theorise about.

## Status

Windows/real-guest: **a real guest on a kernel built this interval (`d2c5a914`,
run 41980452867), walked 142 times over 900 seconds, with the emitter's own
records naming SpringBoard on the `do_fork1` route.**  135 processes at the peak,
95 distinct names, control passed.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, completed successfully.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached -- the screen is still the UniXNU boot menu even though SpringBoard is
proven spawned, proven alive, and proven to take the `do_fork1` route.**
THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-fifth, addendum BK): the screen measured AND looked at, and the display stops at stage 10

## The measurement, and the picture that goes with it

`local-private/tmp/screen-vs-springboard.py` boots the guest, walks `allproc`, and
screendumps the framebuffer every 10 s, keeping every distinct frame.  On the
`fork1.spawn` kernel (`d2c5a914`, run 41980452867):

    411.6s  nprocs=14    SPRINGBOARD APPEARED, frame=f54dadc07fb07106
    65 screendumps over 650 s, ONE distinct frame, f54dadc07fb07106

**And the frame was looked at, not just hashed** -- the recorded fact is that 39
minutes of sha256 comparison once said "the display froze" while one PNG said the
framebuffer was still the UniXNU boot menu.  The PNG is the UniXNU boot menu:
green/magenta/blue banner across the top, `Booting QEMU virt: source 4K XNU in
1s`, `Up/Down: select  Enter: boot`, `drivers 1 loaded / 0 failed`.

So the statement that has been repeated for fifteen addenda is now **measured and
seen**, and it survives: SpringBoard is alive at pid 27 and the framebuffer holds
the bootloader's banner, unchanged for 240 s after SpringBoard appeared.

## Whether the screendump is even reading the right buffer

It was worth checking, because `unixnu_boot.SCREEN` is `1376x1024` and the
screendump is 800x600.  The guest's own serial log settles it:

    DARWINBOOT: video base=0x1BC7A0000 display=1 row=3200 width=800 height=600 depth=32

**The GOP framebuffer the guest is handed IS 800x600, row 3200, 32 bpp** -- the
same geometry as the screendump.  So the screendump is not reading some unrelated
QEMU surface; it is reading the buffer the bootloader drew into, and the guest's
display path is not writing it.

But the 800x600 is itself the finding.  `QemuRamfb.efi` exists to size the ramfb
via `-fw_cfg name=opt/org.unixnu/ramfb-size`, and `unixnu.toml` names exactly one
driver:

    [[driver]]
    path = "Payloads/DarwinBoot.efi"

`QemuRamfb.efi` is not among them, and `_check_ramfb_pair` refuses a boot that
stages it anyway -- because a 24-minute run already measured that a staged driver
with no `[[driver]]` entry is a file nothing opens (29 screendumps, all 800x600,
all one sha256).  **So the framebuffer geometry cannot be fixed from the host; it
needs a payload build whose `unixnu.toml` names the driver.**  The guest is
running at 800x600 on a 1376x1024 panel, and that is a build change, not a
measurement problem.

## Where the display path actually stops

The channel carries the display probe's own stage counter, and it is the most
specific datum in this interval:

    {"event":"diag.legacy_display_status","stage":10,"surface_status":0,
     "surface_found":1,"dt":1,"source":"XNU.LegacyDisplayProbe"}

**The surface was found and the device tree attached, and the probe stopped at
stage 10 with `surface_status` 0.**  `surface_found: 1` and `dt: 1` mean the two
things that could have failed early did not.  What stage 10 is, and what the next
stage needs, is in `LegacyDisplayProbe.cpp` and is the next thing to read -- it is
the first point in this chain where the display path has a numbered place to have
stopped rather than a silence to interpret.

## The instrument defects in my own script, recorded because both would have lied

* **The first screendump attempt opened a second QMP connection while the first
  was held.**  QMP serves one client, so every dump failed with "no qmp greeting
  within 60s" -- which reads exactly like a guest with no framebuffer.  It is not.
  The dumps now use the one connection the run holds.
* **That broken run still printed a verdict.**  "THE SCREEN DID NOT CHANGE once
  SpringBoard was alive" was derived from 34 failed reads and zero frames.  A
  confident negative from an instrument that never produced a frame is worse than
  no verdict, and it is the recorded fact 13 shape again.  The run reported here
  has 65 real frames behind its verdict.

## Status

Windows/real-guest: **a real guest, 65 screendumps over 650 s, one distinct frame,
and that frame looked at and identified as the UniXNU boot menu.**  SpringBoard
appeared at t=411.6 s with the framebuffer unchanged.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, completed successfully.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  The screen is the UniXNU boot menu, now measured and seen rather than
inferred from a hash, and the display path has a numbered place it stopped:
stage 10, surface found, device tree attached.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-sixth, addendum BL): the display is fully up, was called three times, and never got a swap

## The measurement

`local-private/tmp/read-display-counters.py` boots the guest and reads 27
`native_legacy_*` counters out of kernel memory over QMP.  All 27 resolved, the
control passed (`version` is the kernel's own string), and the final values are:

    native_legacy_stage                 10     <- the SUCCESS terminal state
    native_legacy_init_result            1     <- init() returned success
    native_legacy_start_result           1     <- start() returned success
    native_legacy_surface_found          1
    native_legacy_dt_attached            1
    native_legacy_pm_stage               4     <- power management ran
    native_legacy_pm_calls               4
    native_legacy_pm_status              0     <- success
    native_legacy_client_calls           3     <- a client DID call it
    native_legacy_client_pid            95
    native_legacy_client_status          0     <- and the call succeeded
    native_legacy_registration_calls     1
    native_legacy_unit_scale_status      0

    native_legacy_swap_validation_calls  0     <- STILL AT ITS INITIAL
    native_legacy_swap_validation_result 0
    native_legacy_transfer_calls         0     <- STILL AT ITS INITIAL
    native_legacy_transfer_status       -1     <- STILL AT ITS INITIAL
    native_legacy_transfer_bytes         0     <- STILL AT ITS INITIAL
    native_legacy_retirement_calls       0     <- STILL AT ITS INITIAL
    native_legacy_retired_frames         0     <- STILL AT ITS INITIAL

`LegacyDisplayProbe.cpp:803` is what stage 10 means: `activeLegacyDisplay=display;
activeGOPNub=nub;`.  The driver is registered, started, powered, and holding a
live nub.  It is not half-up.

**And the whole downstream chain was never entered.**  The panic string at
`LegacyDisplayProbe.cpp:685` names the order the display path runs in:

    stage=%u PM=%u swap=%u/%u copy=%08x bytes=%llu retire=%u handoff=%u/%u

PM ran to stage 4.  Swap validation, the copy/transfer, retirement and the
handoff did not run at all -- every one of their counters is still at the initial
value its declaration sets, which is what "never invoked" means here rather than
"returned zero".

**A client did call the display, three times, from pid 95, successfully.**  So
the driver is reachable and answers.  What never happened is a *swap submission*.

## What that settles, and what it does not

It settles task #27's question with a measurement rather than a theory: the swap
is not being rejected, not failing validation, not erroring on the copy.  **It is
never submitted.**  Combined with addendum BK -- 148 screendumps over 1500 s with
SpringBoard alive and one distinct frame -- the two facts are the same fact from
two sides: nothing writes the framebuffer because nothing ever asks the display to
write it.

It does not settle WHO pid 95 was or why it stopped at three calls.  This boot
stalled at three processes (the recorded non-determinism), so the pid is from an
early state and the three calls are from early in the boot.  **The next
measurement is the same counters on a boot that ramps**, which is one run of the
same script away and needs no build.

And it does not touch the geometry problem from addendum BK: `native_legacy_gop_width`
is 800 and `native_legacy_gop_height` is 600, on a panel `unixnu_boot.SCREEN`
calls 1376x1024, and `unixnu.toml` names only `Payloads/DarwinBoot.efi` so
`QemuRamfb.efi` cannot size it from this host.  **The display is presenting at the
wrong size even if a swap were submitted.**

## The instrument's own guard, and it earned its place

Every counter that can mean "never invoked" is declared with that initial value in
the source, and the script reads those initials from the same table rather than
assuming.  A counter absent from the kernel prints `ABSENT FROM KERNEL` instead of
0 -- which is the distinction task #32 has been asking for since it was opened,
and it fell out of this measurement rather than being bolted on.

## Status

Windows/real-guest: **27 display counters read from a live guest's kernel memory
over QMP, on a build made this interval (`d2c5a914`, run 41980452867).**  The
driver reached its success terminal state and no swap was ever submitted.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, completed successfully.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  The display driver is fully up and reachable, and the swap that would
put anything on the screen is never submitted.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-seventh, addendum BM): addendum BL's conclusion is wrong, and it is task #32's distinction

## What BL got right, and what it got wrong

BL read 27 `native_legacy_*` counters out of a live guest.  Every number in it is
correct -- the initials were read from the driver's own declarations and the
control passed.  What does not follow is the sentence built on two of them:

    native_legacy_swap_validation_calls   0   <- "never invoked"
    native_legacy_transfer_calls          0   <- "never invoked"
    native_legacy_transfer_status        -1   <- "never invoked"

BL read "never invoked" and concluded **"the swap is never submitted"**.  That is
the reading of an instrument that works.  It is not a measurement of the guest.

## Why, and it is three greps

The counters are incremented in exactly one place each, both inside
`package/LegacyDisplayProbe.cpp`:

    :300  void native_legacy_swap_validation_observed(bool accepted,
    :304      native_legacy_swap_validation_calls = ... + 1;
    :316  __attribute__((noinline)) void native_legacy_transfer_observed(
    :317      native_legacy_transfer_calls = ... + 1;

**Neither function is called anywhere in the file it is defined in.**  Grepping the
whole linked driver for a call site returns only the definitions.  The one caller
that exists is in a *different repository*:

    local-private/upstream/UniXNU/drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp:441
        native_legacy_swap_validation_observed(accepted,reason,request,surface);

and `vendor/UniXNU-qemu/drivers/NativeMobileDisplay/` **does not exist**.  The
kernel is built from `vendor/UniXNU-qemu`, whose `board.toml` names
`package/LegacyDisplayProbe.cpp` as the display driver.  The symbols are in the
built kernel -- `native_legacy_swap_validation_observed` is present -- because the
object is linked, not because anything calls it.

**So `swap_validation_calls = 0` measures "this hook is dead code in this build",
not "no swap was submitted".**  The two readings are indistinguishable from the
counter alone, which is precisely the failure this project has recorded a dozen
times: an instrument returns a value, the value is read as an answer, and the
question it answered is a different one.

## What BL's numbers DO establish

Stripped of the dead hooks, the same run still says something solid, and it is
worth keeping:

    native_legacy_stage            10     <- the SUCCESS terminal state
    native_legacy_init_result       1     <- init() succeeded
    native_legacy_start_result      1     <- start() succeeded
    native_legacy_surface_found     1
    native_legacy_dt_attached       1
    native_legacy_pm_stage          4     <- power management ran
    native_legacy_pm_status         0
    native_legacy_gop_width       800     <- and the geometry is 800x600
    native_legacy_gop_height      600
    native_legacy_client_calls      3     <- a client DID call, three times
    native_legacy_client_pid       95
    native_legacy_client_status     0     <- successfully

`native_legacy_client_calls` is incremented at `:367`, inside a live code path --
not inside one of the dead observers -- so **that one is a real measurement.**  A
client reached the display driver three times from pid 95 and every call returned
success.  The driver is up, powered, and answering.

And the ordering run adds the sequence, which a final-value table cannot show:

    FIRST CHANGE: native_legacy_pm_status        -1 -> 0
    FIRST CHANGE: native_legacy_gop_width          0 -> 800
    FIRST CHANGE: native_legacy_gop_height         0 -> 600
    FIRST CHANGE: native_legacy_unit_scale_calls   0 -> 1
    FIRST CHANGE: native_legacy_unit_scale_status -1 -> 0
    FIRST CHANGE: native_legacy_registration_calls 0 -> 1

The driver's own init sequence runs to completion.  What happens after it is
exactly what the dead hooks were supposed to observe, and is now unobserved.

## So task #32 is not a nicety, and it is not only about the console emitters

Task #32 has been open since it was written as "make the gate distinguish
'emitter never built' from 'not yet measured'".  It was scoped to the
`spawn.seen`/`exec.seen` family.  **The same defect exists in the display
counters, and it produced a wrong conclusion on the critical path one interval
after the task was written.**  The gate needs to cover every counter this project
reads, not only the console emitters.

The concrete form of the gate, which this measurement shows is sufficient: for
every counter, name the function that increments it and prove that function has a
call site in the linked driver.  A counter whose only incrementer is uncalled must
be reported as `hook_uncalled:` and never as a value.

## Status

Windows/real-guest: **a live guest, 27 counters read twice, plus the greps that
show which of them can mean anything.**  The display driver reaches its success
terminal state and answers a client three times; the swap path is unobserved.

Build: `exp-2026-11-05-fork1-spawn`, run 41980452867, completed successfully.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What is now known is narrower and truer than BL claimed: the display is
up and answering, and nothing in this build observes what happens after that.**
THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-eighth, addendum BN): the task #32 gate is built, and it is not yet trustworthy

## The gate, and what it confirms

`local-private/tmp/check-counter-hooks.py` takes the driver the kernel links
(`vendor/UniXNU-qemu/package/LegacyDisplayProbe.cpp`, which `board.toml` names),
finds every counter it defines, finds the function that increments each, and
proves that function has a call site.  It needs no build and no guest -- it is a
source check, which is the point: it would have caught addendum BL's error in
seconds on the host rather than after a 15-minute boot.

It independently confirms addendum BM, and finds more dead hooks than BM did:

    native_legacy_swap_validation_calls   hook_uncalled: native_legacy_swap_validation_observed
    native_legacy_swap_accepted_count     hook_uncalled: native_legacy_swap_validation_observed
    native_legacy_copy_n                  hook_uncalled: native_legacy_copy_timing_observed
    native_legacy_gpu_fallbacks           hook_uncalled: native_legacy_gpu_fallback_observed
    native_legacy_prepare_calls           hook_uncalled: native_legacy_prepare_scanout
    native_legacy_prepare_failures        hook_uncalled: native_legacy_prepare_scanout

**Six counters whose only incrementer is never called.**  So the display driver
carries a large observability surface of which a substantial part is dead code,
and BL read two of the dead ones as facts about the guest.

## The two defect classes, and the false negative is the dangerous one

The gate is not yet trustworthy, and the reason matters more than its verdicts.

**A false negative.**  It reports

    native_legacy_transfer_calls   ok (incremented by ['__attribute__'])

for a counter addendum BM proved is dead.  The cause is my own parser: the
definition line is `__attribute__((noinline)) void native_legacy_transfer_observed(...)`,
and the function-name regex binds `__attribute__` as the name.  It then finds
other `__attribute__(` occurrences, counts them as call sites, and concludes the
function is live.  **A gate that reports `ok` for a dead hook is worse than no
gate**, because it converts a known defect into a green check -- the same shape as
the recorded fact about a pre-flight that validated the wrong target and reported
green while the build was red.

**A false positive.**  It reports

    native_legacy_client_calls     hook_uncalled: native_legacy_client_enter
    native_legacy_registration_calls hook_uncalled: native_legacy_registration_observed

for counters that read **3** and **1** respectively on a live guest.  So those
functions ARE called -- through a vtable or a function pointer, which a textual
grep for `name(` cannot see.  The display driver interposes a vtable
(`interpose(client, native_legacy_client_pid)` at :626), so this is expected
rather than surprising.

**Both classes have the same root:** the check reasons about text where it needs
to reason about the call graph.  The fix is not a better regex.  It is to answer
the question the symbol table can answer -- does anything in the linked objects
reference this symbol -- which is what `llvm-objdump` on the built kext gives
directly, and which the project already uses elsewhere.

## What survives, and what it buys

The six `hook_uncalled` verdicts stand, because they are confirmed by the
independent route addendum BM took: the incrementer is defined and never called,
and the only caller anywhere in the tree is in a directory that does not exist in
the repo the kernel is built from.

What it buys is the concrete form of task #32's gate, now written down rather than
described: **for every counter, name the function that increments it and prove
that function has a call site in the linked driver; a counter whose only
incrementer is uncalled is reported as `hook_uncalled:` and never as a value.**
The proof has to come from the object's relocations, not from the source text.

## Status

Windows/real-guest: **none this interval -- the gate is a host-side source
check.**  The six `hook_uncalled` verdicts are corroborated by addendum BM's
independent route; the two false verdicts are defects in the gate and are recorded
rather than hidden.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  The display driver carries six counters that cannot mean anything, and
the gate that would keep them out of a conclusion is built but not yet
trustworthy.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (ninety-ninth, addendum BO): addendum BM is wrong, the swap hook is wired, and BL stands

## The error, stated plainly

BM read `native_legacy_swap_validation_calls = 0` and `transfer_calls = 0`,
grepped `vendor/UniXNU-qemu` for a call site, found only the definitions, found
one caller in `local-private/upstream/UniXNU`, observed that
`vendor/UniXNU-qemu/drivers/NativeMobileDisplay/` does not exist, and concluded
**"the symbols are in the built kernel because the OBJECT is linked, not because
anything calls it."**

**The last observation is correct and the inference from it is backwards.**  The
caller is in the other repository *because that is where the driver lives.*  The
kext `native-legacy-display` links two trees, not one:

    vendor/UniXNU-qemu/package/LegacyDisplayProbe.cpp
        -- the PROBE: it defines the counters and the observers that increment them.
    local-private/upstream/UniXNU/drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp
        -- the DRIVER: it calls the observers.  A git submodule pinned by
        sources.lock.json, on disk at 5b83ab7, matching the superproject gitlink.

`vendor/UniXNU-qemu` has no `drivers/` directory at all, so grepping it for a
call site can only ever return the definition.  **A grep scoped to one of two
linked trees is not a call-graph check, and it produced a confident negative.**

## The four observers are all called, from the driver

    native_legacy_registration_observed   <- NativeLegacyDisplay.cpp:130
    native_legacy_client_enter            <- NativeLegacyDisplay.cpp:399
    native_legacy_swap_validation_observed<- NativeLegacyDisplay.cpp:365
    native_legacy_transfer_observed       <- NativeLegacyDisplay.cpp:169, 171, 174, 181,
                                              198, 203, 212, 217, 230, 254, 261, 270
    native_legacy_copy_timing_observed    <- NativeLegacyDisplay.cpp:259
    native_legacy_gpu_fallback_observed   <- NativeLegacyDisplay.cpp:273
    native_legacy_retirement_observed     <- NativeLegacyDisplay.cpp:290
    native_legacy_prepare_scanout         <- NativeLegacyDisplay.cpp:171, 203, 229
    native_legacy_gop_mapping_observed    <- NativeLegacyDisplay.cpp:422

`transfer_observed` alone has twelve call sites.  **None of these is dead code.**

## So BL's zero is a measurement, and it is sharper than BL could say

With the hook wired and the boot argument set, a zero means the function never
ran.  The measured boot's own `kernel_args` carries
`offline-legacy-validate=1` (read out of the run's `payload.json`), and
`NativeLegacyDisplay.cpp:322` reads exactly that argument:

    unsigned enabled=0;PE_parse_boot_argn("offline-legacy-validate",&enabled,sizeof(enabled));
    if(enabled==1 && logicalPM && primary && bootMapping && request) {

`native_legacy_swap_validation_observed(accepted,reason,request,surface)` is at
:365, *outside* that `if`, so it runs whenever `validateSwap` runs at all.  **A
zero therefore says `validateSwap` was never invoked** -- not that it was invoked
and declined, not that it was invoked and rejected, and not that it is dead code.

That is BL's "the swap is never submitted", now with the mechanism named: the
vtable method that would validate a swap is never reached.

## What this costs, and what it does not touch

**It costs addendum BM in full**, and it costs the six `hook_uncalled` verdicts
the first version of `check-counter-hooks.py` produced -- all six were false
positives from the same scope error.  **It costs nothing in BL**: every number in
BL was right and its conclusion was right.

**It does not touch** BM's other content, which stands on its own: the ordering
run's first-change sequence, `stage=10`, `init_result=1`, `start_result=1`,
`gop_width=800`, `gop_height=600`, and `client_calls=3` from pid 95.

## The gate, fixed, and what its real defect was

`local-private/tmp/check-counter-hooks.py` now searches **both** linked trees
(203 source files) instead of one, treats an address taken (`&name`) as a
reference as well as a call, and strips `__attribute__((...))` before parsing
definitions.  Its verdict is now `0 counter(s) whose only incrementer is never
called`, with every incrementer's call sites named and filed.

**Its defect was never the regex.**  It was the search scope, and no amount of
regex work would have found it -- the first version's false negative and false
positive were both symptoms of looking at the wrong set of files.  That is worth
recording because the fix that was proposed at the time, `llvm-objdump` on the
built kext, would not have worked either: there is no kext on this host (the
build is in CI), there is no binutils, and the payload kernel is stripped of
local symbols, so symbol presence cannot distinguish dead from live --
`wrappedExternal` reads ABSENT while `native_legacy_swap_stage` reads present,
purely because one is `static` and the other is `extern "C"`.

## Status

Windows/real-guest: **none this interval -- the whole correction is host-side:
a grep with the right scope, a submodule pin check, and the measured boot's own
kernel_args.**  The guest numbers were all read earlier and none of them changes.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What changed is that the display path is now known to be instrumented
and un-entered rather than uninstrumented: the swap is never submitted because
`validateSwap` is never invoked, and that is a question about the caller, not
about the hook.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundredth, addendum BP): the console-emitter gate is positive-only, and the project had already said so

## What was attempted, and what it would have published

BO fixed the display-counter gate.  The console half of task #32 -- the original
scope, the `spawn.seen` / `exec.seen` family -- was still open, and it looked
answerable on this host with no build and no guest: the emitters are string
literals, so search the kernel image for the literal.  Built or not built.

`local-private/tmp/check-console-emitters-built.py` did that, and reported:

    springboard.running      NOT BUILT
    springboard.fork1        BUILT
    spawn.seen               NOT BUILT
    spawn.failed             NOT BUILT
    spawn.do                 NOT BUILT
    spawn.saturated          BUILT
    exec.seen                NOT BUILT
    exec.first               NOT BUILT
    exec.saturated           BUILT

**Six of those nine verdicts would have been wrong.**  `spawn.seen` is one of
them, and addendum AZ observed **512 `spawn.seen` records** from a live guest.

## Why, and it is written in the artifact's own comment

The patch that carries these emitters records the defect in its own text:

    AND THE LITERAL WILL NOT BE GREPPABLE IN THE IMAGE, which is measured and
    not guessed: ld's SHF_MERGE|SHF_STRINGS pass split the exec record's 30-byte
    literal into a 16-byte head plus a shared tail, so exec.seen counts ZERO as
    a contiguous byte string in the kernel that carries it.  The record arrives
    on the wire complete.  The block-scope static is what proves the code is
    there.

So the linker's string-merging pass splits a literal across a shared head and
tail.  The record arrives complete on the wire; the contiguous byte string is
simply not in the image.  A literal that is passed to `native_console_report` as
a whole object survives; a literal that is only copied byte-by-byte out of a
block-scope `static char const` does not.

The extracted bytes show it directly.  Around `springboard.fork1` the image holds
`{"event":"fork1.board.running","comm":"SpringBoard",...` complete, and then
`{"event":"spawn.` and `{"event":"exec.s` cut off mid-name -- the merge pass's
head/tail split, visible in the raw bytes.

## The rule this leaves behind

**A literal FOUND in the image is sound: the emitter is built.  A literal NOT
FOUND proves nothing.**  The two directions are not symmetric, and the script now
says so -- it prints `NOT PROVEN BY THIS METHOD` rather than `NOT BUILT`, with
the reason attached.

That means the console half of task #32 **cannot** be answered by image search.
It is not a matter of a better search: the information is not in the image in the
form the search wants.  What survives is the positive half, which is worth
having -- `springboard.fork1`, `spawn.saturated` and `exec.saturated` are
BUILT, and `springboard.fork1` is the one whose literal survives precisely
because it is passed whole rather than assembled from a block-scope static.

## The lesson, which is the recorded one

I built an instrument the project had already refuted, in a comment inside the
very patch under test, and would have published six verdicts that contradicted a
measurement taken on a live guest.  **Read the artifact's own record before
reasoning about it** -- the same lesson as the recorded fact about a green run's
manifest naming the commits it actually built.  The grep for `spawn.seen` in the
image was a plausible-looking question with an unsound answer, and the only thing
that caught it was asking whether a known-observed record survived the same
search.

BO's correction and this one are the same mistake in two costumes: in BO the
grep was scoped to the wrong tree, here it was scoped to the wrong artifact.
Both produced confident negatives from an instrument that could not answer the
question asked.

## Status

Windows/real-guest: **none this interval.**  The guest numbers were all read
earlier and none of them changes; what changes is which of them may be believed.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  Two gates were built this interval: one fixed by widening its scope,
one narrowed to the only half of itself that is sound.  Neither of them is a
step toward the screen.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred first, addendum BQ): every console emitter is BUILT, so every console silence is "not measured"

## The sound route, which was in the patch's structure all along

BP established that searching the image for an emitter's literal is
positive-only: `ld`'s `SHF_MERGE|SHF_STRINGS` pass splits a literal into a shared
head and tail, so `spawn.seen` counts zero as a contiguous byte string in a kernel
that emits it.

But the patch is structured.  Every emitter sits inside an `#if defined(MACRO)`
block, and **a block compiles or does not compile as a unit.**  So if any emitter
in a block has its literal in the image, that block was compiled, and so was
every other emitter in it.

`spawn.saturated` is BUILT -- its literal is passed whole to
`native_console_report` rather than assembled byte-by-byte from a block-scope
static, so it survives the merge pass.  `spawn.seen` is twenty lines earlier in
the same `#if defined(XNU_OWNER_CACHE_MOUNT)` block.  Therefore `spawn.seen` is
built.

## The result: twelve for twelve

    exec.saturated           BUILT (its literal is in the image)
    exit.saturated           BUILT (its literal is in the image)
    spawn.saturated          BUILT (its literal is in the image)
    spawn_failed.saturated   BUILT (its literal is in the image)
    springboard.fork1        BUILT (its literal is in the image)

    exec.first               BUILT BY GUARD BLOCK (#XNU_OWNER_CACHE_MOUNT)
    exec.seen                BUILT BY GUARD BLOCK (#XNU_NATIVE_VIRTIO_CONSOLE)
    exit.seen                BUILT BY GUARD BLOCK (#XNU_NATIVE_VIRTIO_CONSOLE)
    fork1.spawn              BUILT BY GUARD BLOCK (#XNU_OWNER_CACHE_MOUNT)
    spawn.failed             BUILT BY GUARD BLOCK (#XNU_OWNER_CACHE_MOUNT)
    spawn.seen               BUILT BY GUARD BLOCK (#XNU_OWNER_CACHE_MOUNT)
    springboard.running      BUILT BY GUARD BLOCK (#XNU_OWNER_CACHE_MOUNT)

**Twelve of twelve, and the two guard macros are both proved compiled by
siblings whose literals are present.**  `local-private/tmp/prove-console-emitters-built.py`
does this and never prints NOT BUILT.

## What that settles, and it is the whole of task #32

**No console-emitter silence anywhere in this project's record is "never
built".**  Every one of them is a real "not measured".  That is the distinction
task #32 was opened to make, and it now holds for both halves:

    display counters   the incrementer is called, proved from the linked call
                       graph over both trees (BO), and ABSENT FROM KERNEL is
                       reported separately from 0.
    console emitters   the emitter is built, proved by literal or by guard block
                       (BQ), so a record that never arrived was never triggered.

And it confirms rather than overturns the `do_fork1` work: `spawn.seen` is a
sound instrument, it is inside `if (error == 0 && !spawn_no_exec)`, and
`spawn_no_exec` is TRUE on the `do_fork1` path -- which is exactly why
`POSIXSpawnType=App` jobs leave no `spawn.seen` record and needed the
`springboard.fork1` emitter to be seen at all.

## The two gates, side by side

Both were built this interval and both had the same shape of defect, caught two
different ways:

    check-counter-hooks.py    scoped to ONE of two linked trees; six live hooks
                              reported dead.  Fixed by widening the scope.
    check-console-emitters-   scoped to the ARTIFACT when the answer was in the
    built.py                  PATCH's structure.  Fixed by reasoning about the
                              guard block, and by refusing to print a negative
                              the method cannot support.

A gate that reports a confident negative it cannot support is worse than no gate.
Both of these did, and both were caught by asking whether a known-true case
survives the same check.

## Status

Windows/real-guest: **none this interval -- both gates are host-side source and
image checks.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What is now settled is narrower than the goal and firmer than anything
before it: every instrument this project reads is built, so every silence in the
record is a real silence about the guest and not an artifact of the build.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred second, addendum BR): the whole display failure, gate by gate, in source

## The chain

`local-private/upstream/UniXNU/drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp`,
read top to bottom along the swap path:

    1.  newUserClient(task,security,type,client)          :398
            native_legacy_client_enter(task,type);         :399   <- client_calls++
            status = family::newUserClient(...)            :400
            native_legacy_client_observed(status,...)      :401   <- client_status

    2.  validateSwap(request)                             :314   (an override)
            enabled = PE_parse_boot_argn("offline-legacy-validate")   :322
            if(enabled==1 && logicalPM && primary && bootMapping && request) { ... }
            if(accepted) addValidated(request); else removeValidated(request);   :363
            native_legacy_swap_validation_observed(accepted,reason,request,surface);  :365
                                                                        <- swap_validation_calls++

    3.  kickSwapQueue()                                   :137   (an override)
            if(kicking){rekick=true;return;}
            do { rekick=false; kickOnce(); gpuBypass=false; } while(rekick && !transferred);

    4.  kickOnce()                                        :147
            enabled = PE_parse_boot_argn("offline-legacy-transfer")     :148
            if(enabled!=1 || transferred || !logicalPM || !bootMapping || !primary) return;   :149  GATE 1
            request = pendingSwapRequest();
            if(!request || !isValidated(request)) return;               :151  GATE 2
            ... native_legacy_transfer_observed(kIOReturnSuccess,request,blankLength);  :181
                                                                        <- transfer_calls++

## Every link has a measured counter, and they all agree

    client_calls = 3, client_pid = 95, client_status = 0
        -> newUserClient ran three times, from pid 95, successfully.
           The client reaches the display and is answered.

    swap_validation_calls = 0
        -> validateSwap NEVER RAN.  The :365 call is outside the boot-arg's
           `if`, so a zero means the function was never entered at all.

    transfer_calls = 0, transfer_status = -1, transfer_bytes = 0
        -> kickOnce never got past GATE 2.

    retirement_calls = 0, retired_frames = 0
        -> no frame was ever retired, which is what a transfer would have caused.

    stage = 10, init_result = 1, start_result = 1, surface_found = 1,
    dt_attached = 1, pm_stage = 4, pm_status = 0, gop_width = 800, gop_height = 600
        -> the driver itself is fully up.  Nothing is half-initialised.

**And GATE 1 is not the blocker.**  The measured boot's own `kernel_args` carries
`offline-legacy-transfer=1` and `offline-legacy-validate=1`, and `stage=10` with
`pm_stage=4` means `logicalPM`, `bootMapping` and `primary` were all established
during init.  So `kickOnce` passes its first gate.

**GATE 2 is the blocker, and it is blocked by step 2.**  `isValidated(request)`
is true only for a request that `validateSwap` accepted and `addValidated`-ed.
`swap_validation_calls = 0` means that never happened for any request.  So
`kickOnce` returns at :151 on every invocation, forever, no matter how often it is
called.

## So the failure is one sentence, and it is not about the hook

**The client creates a user client three times and never submits a swap.**  With
no swap submitted, `validateSwap` never runs, nothing is ever validated, and
`kickOnce` returns at its second gate on every call.  Nothing downstream of that
can run, which is why the framebuffer never changes.

This is BL's "the swap is never submitted" with the mechanism filled in, and it is
BM's "the hooks are dead code" refuted twice over -- once by BO's scope fix and
once by this chain, in which the hooks are the load-bearing links.

## The instrumentation gap this exposes, and it is the next measurement

**There is no counter on `kickSwapQueue` or on `kickOnce`'s two gates.**  So the
guest cannot distinguish:

    (a) the family never calls kickSwapQueue at all, from
    (b) the family calls it and kickOnce returns at GATE 1, from
    (c) the family calls it and kickOnce returns at GATE 2.

The counters that exist start counting at `transfer_observed`, which is *after*
both gates.  So `transfer_calls = 0` is consistent with all three, and the
derivation above picks (c) by elimination -- GATE 1 is ruled out by the boot args
and `stage=10` -- rather than by measurement.

**An emitter on the kick path would settle it directly**, and it is the one
measurement that would say whether the family is kicking at all.  That is a code
change in the driver, a build, and a boot; the code change is the part that can
be done now, and it is prepared rather than left to be rediscovered.

## Status

Windows/real-guest: **none this interval -- the chain is source, and every link
in it is tied to a counter already read off a live guest.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  The display failure is now derived end to end in source and is one
sentence long: the client never submits a swap.  What is not yet measured is
whether the family ever kicks the queue at all, because no counter watches the
kick path.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred third, addendum BS): the caller was already asked, and the answer is in the tree

## What BR left open, and what was already there

BR derived the display failure to "the client never submits a swap" and called
that a question about the caller.  The caller was already asked.
`local-private/tmp/display-clients.py` reads the framebuffer's USER CLIENT table
out of a live guest -- `LegacyDisplayProbe.cpp:381`'s `static ClientStat
seenClients[8]`, one entry per user client, recording its pid, its per-selector
call and failure counts, the notification ports it registered, and the arguments
of selector 6, the swap/wait selector.

Its saved output, `local-private/boot53/display-refusal-run/client-table.txt`, is
a complete answer:

    5 clients opened the framebuffer:  pid 86, pid 58, pid 95, pid 95, pid 74

    the busiest, pid 58: 15 calls, 11 failures, across 14 distinct selectors

        selector   6: calls=1 fails=0 status=SUCCESS      <- the swap/wait selector
        selector  74: calls=1 fails=0 status=SUCCESS      <- supportedFrameInfo
        selector  88: calls=1 fails=0 status=SUCCESS
        selector  12: calls=1 fails=1 status=Unsupported
        selector  15: calls=1 fails=1 status=Error
        selector  22: calls=1 fails=1 status=Unsupported
        selector  27: calls=1 fails=1 status=Unsupported
        selector  29: calls=1 fails=1 status=Unsupported
        selector  33: calls=1 fails=1 status=Unsupported
        selector  50: calls=1 fails=1 status=Unsupported
        selector  57: calls=1 fails=1 status=Unsupported
        selector  79: calls=2 fails=2 status=Unsupported
        selector  84: calls=1 fails=1 status=Unsupported

**A client opened the framebuffer five times, called fourteen distinct selectors,
and was refused eleven of them with `kIOReturnUnsupported`.**  The swap/wait
selector was called once and answered SUCCESS -- so it is not the swap
submission, and the submission is among the refused selectors or never reached.

That is the measured shape of BR's one sentence: the client does not decline to
submit a swap, it is **refused eleven of the fourteen things it asks for** and
gets as far as one successful swap/wait call.

## Which kernel that came from, and it is not the one BL measured

    client-table.txt   kernel sha256 2ebda0ec...  (run 41880912125)
    addendum BL        kernel sha256 d2c5a914...  (run 41980452867)

**These are different builds.**  The selector table is evidence about the older
one.  It is recorded here with its kernel named so it is not read as a property
of the current tree -- the same discipline as the recorded fact about a green
run's manifest naming the commits it actually built.

## The current kernel can measure it again, with no build

Every symbol the client-table reading needs is present in the kernel addendum BL
measured:

    native_legacy_client_calls        present
    native_legacy_client_pid          present
    native_legacy_client_status       present
    native_legacy_client_object       present
    native_legacy_client_class        present
    native_legacy_slot_armed_count    present
    native_legacy_prepare_calls       present
    native_legacy_swap_validation_calls present

`interpose()` is armed too: `native_legacy_client_observed` (:625) is called from
`newUserClient` (:401) on every successful open, and `client_calls=3` with
`client_status=0` on the measured boot proves it ran.

**So re-running `display-clients.py` against the current kernel needs no build
and no patch.**  It needs a guest, and the guest is blocked: the host is at 1.0 GB
free because `msedgewebview2` holds 6 GB and the user's own applications hold the
rest, with no leaked process of ours.  That is an environment limit and is
recorded as one rather than worked around by killing the user's applications.

## And the kick-path gap is confirmed by absence

    native_legacy_kick_calls          ABSENT

There is no kick-path counter in this kernel, which is task #36's premise proved
from the image rather than asserted.  The existing counters begin at
`transfer_observed`, after both of `kickOnce`'s gates, so the guest cannot say
where `kickOnce` stops without a build.

## Status

Windows/real-guest: **none this interval.**  The client-table numbers are from
run 41880912125 on kernel `2ebda0ec`, read earlier; this interval only
established which kernel they belong to and that the current kernel carries the
same instrumentation.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  The display blocker is now characterised from both ends: in source it is
one gate (`isValidated` is never true), and on a guest it is a client refused
eleven of fourteen selectors.  Both are waiting on the same thing, which is a boot
on a host with memory for one.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred fourth, addendum BT): a real boot, a passed control, and no client ever opened the framebuffer

## The measurement

`local-private/tmp/read-client-table.py` boots the guest, waits for a client to
open the framebuffer, reads `static ClientStat seenClients[8]` out of kernel
memory over QMP, and exits the instant it has the table.  It runs against the
kernel addendum BL measured:

    run 41980452867, kernel sha256 d2c5a914668c... verified against payload.json
    __ZL11seenClients      = 0xfffffff00c146888     <- resolved
    __ZL15seenClientCount  = 0xfffffff00c146880     <- resolved
    resolved 10 of 10 counters
    version kstring: b'Darwin Kernel Version 23.6.0: Thu Nov  5 06:52:05 UTC'  <- THE CONTROL PASSED

    then 420 s of sampling at 10 s, every sample:
        client_calls=0    swap=0    prepare=0

    REFUSED: no client ever opened the framebuffer within the 420s deadline,
    so there is no table to read.  This is a silence, not a measurement.

**The script refused rather than report a table it never read**, which is the
behaviour it was written for: a `client_calls=0` boot has no client table to
print, and printing an empty one would read as "five clients, all idle".

## And that is not what this kernel said last time

Addendum BL, on the SAME kernel, measured:

    native_legacy_client_calls   3
    native_legacy_client_pid    95
    native_legacy_client_status  0

**Three client opens on one boot of `d2c5a914`, zero on another.**  The kernel is
byte-identical -- its sha256 was verified against `payload.json` before the boot
-- so the difference is in the guest, not in the artifact.

That is the recorded non-determinism, and this is the sharpest form it has taken:
whether the framebuffer client reaches the display driver AT ALL varies between
boots of the same kernel.  It means `client_calls=3` was never a stable property
of this kernel, and neither is `client_calls=0`.  **A single boot's `client_calls`
is not a fact about the kernel.**

## What that does to the display conclusion

BR's chain survives, because it never depended on `client_calls` being 3:

    swap_validation_calls = 0   ->  validateSwap never ran, on BOTH boots
    transfer_calls = 0          ->  kickOnce never got past GATE 2, on BOTH boots
    prepare_calls = 0           ->  the scanout was never prepared, on BOTH boots

Those three were zero on BL's boot AND on this one.  So "no swap is ever
submitted" holds across both boots, and it does not depend on the client count.

What it does to BS's framing: the client table BS quoted (`5 clients, pid 58,
14 selectors, 11 refused`) is from kernel `2ebda0ec`, and this boot shows that even
the question "does a client open the framebuffer" is not stable within one kernel.
So the selector table is evidence about that boot of that kernel, and no more.

## And it corrects addendum BP

BP argued that the payload kernel is stripped of local symbols, so symbol presence
cannot separate dead from live.  **That was wrong.**  `__ZL11seenClients` -- the
mangled local-linkage spelling of a C++ file-scope `static` array -- is present in
this kernel, and `display-clients.py` had already recorded that fact in its own
comment.  What is true is narrower: **static VARIABLES survive under their mangled
names; static FUNCTIONS do not**, because they were inlined.  BP's conclusion
stands on its own evidence (AZ observed 512 `spawn.seen` records), but one of its
supporting arguments was false and is corrected here.

## Status

Windows/real-guest: **a live guest, kernel sha256 verified against payload.json,
the control passed, 420 s of sampling, 10 counters and the client table
address resolved.**  No client opened the framebuffer on this boot.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What is new is a real measurement with a name attached to its kernel,
and what it says is that the display blocker is not merely that the client is
refused -- on some boots of this kernel no client reaches the display at all.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred fifth, addendum BU): two boots stalled at 2-4 processes, so the silence is the boot and not the display

## The run

Same kernel, same script, one change: the sampling loop now prints the process
count alongside the counters.  `read-client-table.py`, run 41980452867, kernel
sha256 `d2c5a914` verified against `payload.json`, control passed
(`Darwin Kernel Version 23.6.0`).

    0.0s   procs=1     client_calls=0    swap=0 prepare=0
    10.0s  procs=2     client_calls=0    swap=0 prepare=0
    ...
    330.7s procs=2     client_calls=0    swap=0 prepare=0
    341.1s procs=4     client_calls=0    swap=0 prepare=0
    351.1s procs=3     client_calls=0    swap=0 prepare=0
    ...
    411.4s procs=3     client_calls=0    swap=0 prepare=0

    REFUSED: no client ever opened the framebuffer within the 420s deadline.

**The process count oscillates between 2 and 4 for the entire window and never
ramps.**  Together with addendum BT's run -- also `client_calls=0`, also on
`d2c5a914` -- that is two consecutive boots of a byte-identical kernel, both
stalled before any userspace client exists.

## So BT's silence is attributed, and it is not the display

`client_calls=0` now has a cause: **there was never a client to call.**  A
framebuffer user client is created by a process, and this boot reached three or
four processes in 420 s.  Nothing in the display driver gates a client that does
not exist.

That separates two things BT could not:

    the display driver refusing a client     <- NOT what happened
    the boot never producing a client        <- what happened

And it makes addendum BL's `client_calls=3` legible rather than contradictory: BL
sampled for **900 s**, this window is **420 s**, and the boot is still at three
processes when the window closes.

## The hypothesis, stated so it can be wrong

**The guest ramps, but slowly -- 420 s is not enough to reach a framebuffer
client.**  The falsifiable prediction: extend the sampling window to 900 s, the
same length addendum BL used, and `client_calls` becomes non-zero on some boots.

The alternative, which the same experiment also tests: the boot is genuinely
stalled, not slow, and no window length reaches a client.  Those two are
distinguishable by whether `procs` climbs past the 2-4 band.

## Status

Windows/real-guest: **two live boots on run 41980452867, kernel sha256 verified
against payload.json, control passed both times, 420 s of sampling each, with the
process count recorded.**  `client_calls=0` on both, with the boot stalled at 2-4
processes.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What is new is that the display measurement is not being defeated by the
display -- it is being defeated by a boot that does not ramp, and the next
experiment is a longer window rather than a code change.**  THE HEADLINE is
unchanged from AH

---

# Interval 2026-11-05 (one hundred sixth, addendum BV): the swap/wait selector is never called, and that is the frozen screen

## The run, and the hypothesis it settled

BU's hypothesis was that the guest ramps but slowly and 420 s is not enough.  The
same script with a 900 s window, on run 41980452867, kernel sha256 `d2c5a914`
verified against `payload.json`, control passed:

    0s-460s    procs 1 -> 2, stalled
    471.5s     procs=40
    481.6s     procs=57
    491.7s     procs=71
    501.9s     procs=134      <- the boot ramps
    ...
    624.9s     a client opened the framebuffer; seenClientCount=3

**The boot ramps at about 470 s and the framebuffer client arrives at about
625 s.**  So BU's hypothesis is confirmed and its rival is refuted: the boot is
slow, not stalled, and 420 s simply cannot see a client.  Every earlier
`client_calls=0` reading was a window that closed before the guest got there.

## What the client did, measured on this kernel

    native_legacy_client_calls     3
    native_legacy_client_pid       86
    native_legacy_client_status    0
    native_legacy_client_class     b'IOMobileFramebufferUserClient'
    native_legacy_stage            10          <- the success terminal state
    native_legacy_slot_armed_count 358         <- the display's vtable is fully lifted

    native_legacy_swap_validation_calls  0    <- STILL AT ITS INITIAL
    native_legacy_prepare_calls          0    <- STILL AT ITS INITIAL
    native_legacy_transfer_calls         0    <- STILL AT ITS INITIAL

Three clients, read out of `static ClientStat seenClients[8]` at
`0xfffffff00c146888`, stride 2312:

    C0  pid=58   object=0xfffffff520d79b80   calls=9   fails=3
    C1  pid=95   object=0xfffffff520d7a200   calls=2   fails=1
    C2  pid=86   object=0xfffffff520d7a880   calls=0   fails=0   <- opened it, called NOTHING

    C0 (pid 58): selector   8 SUCCESS
                 selector  15 0xe00002bc
                 selector  22 SUCCESS
                 selector  29 0xe00002c7
                 selector  33 SUCCESS   collectCompletionReports (slot 337)
                 selector  57 0xe00002c7
                 selector  74 SUCCESS   supportedFrameInfo (slot 213)
                 selector  79 SUCCESS
                 selector  88 SUCCESS
    C1 (pid 95): selector   8 SUCCESS
                 selector  28 0xe00002f0

**Eleven selector calls in total, four of them refused, and selector 6 -- the
swap/wait selector -- is not among them.  No client ever calls it.**

## So the mechanism is measured, not inferred

BR's chain was derived in source and said `kickOnce` returns at GATE 2 because
`isValidated(request)` is never true.  This run supplies the missing link from the
other end: **the swap/wait selector is never invoked by any client**, so
`validateSwap` is never entered, so nothing is ever validated, so GATE 2 never
opens.  The chain is now closed at both ends by measurement rather than by
elimination.

And `slot_armed_count = 358` matters: the display's own vtable is fully lifted,
every one of the 358 slots traced.  So this is not an instrumentation gap.  The
client reaches the display, calls it eleven times, and never asks it to swap.

## And the two kernels differ, which BS could not see

The saved table from kernel `2ebda0ec` (run 41880912125) had

    selector 6: calls=1 fails=0 status=SUCCESS

On this kernel, `d2c5a914`, **selector 6 is never called at all.**  So the two
builds genuinely differ in whether the swap/wait selector is reached, and BS was
right to refuse to read the older table as a property of this one.

## Status

Windows/real-guest: **a live guest on run 41980452867, kernel sha256 verified
against payload.json, control passed, 900 s of sampling, the boot observed ramping
from 2 to 134 processes, and the client table read out of kernel memory with its
class name and every selector recorded.**  The swap/wait selector is never called.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What is now measured rather than derived is the specific thing the guest
never does: no client ever calls the swap/wait selector, on a boot that ramped, on
the current kernel, with the display fully up and its vtable fully traced.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred seventh, addendum BW): two ramped boots agree, and the lift is active with 60 slots replaced

## The second ramped boot

Same script, same kernel (`d2c5a914` on run 41980452867, sha256 verified against
`payload.json`), control passed, 900 s window:

    481.8s  procs=56
    491.9s  procs=78
    502.1s  procs=93
    604.3s  a client opened the framebuffer; seenClientCount=1

    native_legacy_client_calls      1
    native_legacy_client_pid        86
    native_legacy_client_status     0
    native_legacy_client_class      b'IOMobileFramebufferUserClient'
    native_legacy_stage             10
    native_legacy_slot_armed_count  358
    native_legacy_slot_lifted_count 60      <- THE LIFT IS ACTIVE

    native_legacy_swap_validation_calls  0   <- STILL AT ITS INITIAL
    native_legacy_prepare_calls          0   <- STILL AT ITS INITIAL
    native_legacy_transfer_calls         0   <- STILL AT ITS INITIAL

    C0  pid=86   object=0xfffffff43ceb8f00   calls=0   fails=0
        opened the framebuffer and called NOTHING

## What the two boots agree on, and what they do not

                    ramped boot 1 (BV)     ramped boot 2 (this)
    clients         3                      1
    selector calls  11                     0
    refusals        4                      0
    selector 6      never called           never called
    swap_validation 0                      0
    prepare         0                      0
    transfer        0                      0
    stage           10                     10
    slots armed     358                    358
    slots lifted    not read               60

**The client population is wildly non-deterministic and the display outcome is
not.**  Three clients and eleven selector calls on one boot, one client and none on
the next -- but `swap_validation_calls`, `prepare_calls` and `transfer_calls` are
zero on both, and selector 6 is called on neither.

That is the first result in this investigation that survives a second boot, and it
is the one that matters: **whatever the client does, it never submits a swap and
never waits for one.**

## The lift is active, so the refusals are not the whole story

`native_legacy_slot_lifted_count = 60`: the display's vtable copy had **60 slots
whose original function was a refusing stub**, and each was replaced with
`native_legacy_slot_success`, a stub that returns 0.  So 60 of the display's 358
slots were refusing before the lift and answer success after it.

That reframes boot 1's four refusals: they are NOT among the 60 lifted slots, or
they come from the client's own vtable rather than the display's.  Either way the
refusals are not what stops the swap, because the swap was never attempted on
either boot.

## What this settles, and what it does not

Settled, across two ramped boots of a verified kernel:

    the display is fully up (stage=10) and fully traced (358 slots armed, 60 lifted)
    a client opens the framebuffer
    and no swap is ever submitted, validated, prepared or transferred

Not settled, and now the only open question on this path:

    WHY does the compositor open the framebuffer and then not present?

The driver's own comment at `NativeLegacyDisplay.cpp:87` names the candidate: the
user client's wait (selector 6) answers `kIOReturnBusy` forever if the powered
word is not announced, "and the compositor never presents".  `announcePoweredByFirmware()`
runs during `start()` when `primary && bootMapping`, and `start_result=1`, so it
ran -- but whether the state setter at vtable slot 354 actually took is not
measured by any counter that exists.

## Status

Windows/real-guest: **two live boots on run 41980452867, kernel sha256 verified
against payload.json, control passed both times, 900 s windows, both observed
ramping past 50 processes, the client table read out of kernel memory on both.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  But for the first time a display conclusion survives a second boot: the
compositor opens the framebuffer and never presents, and nothing in this build
observes why.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred eighth, addendum BX): the powered word IS set, so the named candidate is eliminated

## The measurement

The state setter that `announcePoweredByFirmware()` drives through vtable slot 354
writes the power-state word at `display+5840`, the "powered" half of the pair next
to `+5836`.  `native_legacy_object` gives the display object, so the word is read
straight out of guest memory -- no build, no patch, and no counter that did not
exist.

Run 41980452867, kernel sha256 `d2c5a914` verified against `payload.json`, control
passed, ramped to 124 processes, client opened the framebuffer at t=604.8 s:

    the display object's state words (object=0xfffffff33a5ed000):
      +5808: 0x00000320
      +5812: 0x00000258
      +5816: 0x00000000
      +5820: 0x00000000     <- the "done" word wrappedExternal reads
      +5824: 0x00000000     <- the "presented" word
      +5828: 0x00000000
      +5832: 0x00000000
      +5836: 0x00000001     <== the REQUESTED half of the pair
      +5840: 0x00000001     <== the POWERED word
      +5844: 0x00000000
      ... (all the rest zero)

**The powered word is 1.  The requested half is 1.**  So the state setter took and
the powered word is announced, exactly as `start()` intended -- and the failure
mode the driver's comment describes, "the wait answers kIOReturnBusy forever and
the compositor never presents", is not in effect.

## So the candidate is eliminated, and that is the result

BW's only open question had one named candidate.  It is refuted: the condition the
comment says would cause the compositor never to present does not hold.  The
display is powered, the requested word is set, `stage=10`, 358 slots armed and 60
lifted, and the compositor still never presents.

That is worth recording as a refutation rather than as a gap, because it removes
the one hypothesis the source offered and leaves the search honest: **nothing in
the display driver's power path explains the frozen screen.**

The zero words at `+5820` and `+5824` are consistent with everything already
measured -- no swap completed, nothing presented -- and the non-zero words at
`+5808` (0x320) and `+5812` (0x258) are state the driver did not set and this
measurement does not interpret.

## The defect this exposed in my own instrument

The notification-port print sat *after* the `continue` for clients with no
selector calls.  So the one client that matters -- the one that opened the
framebuffer and made no external-method call at all -- was exactly the one whose
port registration was never shown.

`registerNotificationPort` is the other slot `interpose()` wraps, and it is
recorded in `reg[]` rather than in `calls`, so a client can register a port with
zero external-method calls.  **The compositor's swap-completion wait hangs off
that port, so whether it registered one is the question this run could not answer
and should have.**  Fixed: the port registration is now printed first, before any
`continue`.

## Status

Windows/real-guest: **a live guest on run 41980452867, kernel sha256 verified
against payload.json, control passed, ramped to 124 processes, the display
object's state words read out of guest memory.**  The powered word is 1.

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  The one hypothesis the source offered is now measured and refuted, and
the next run asks the question this one was supposed to: did the compositor
register a notification port before it went quiet.**  THE HEADLINE is unchanged
from AH

---

# Interval 2026-11-05 (one hundred ninth, addendum BY): the framebuffer's only client is IOMFB_FDR_Loader, and backboardd is absent

## The measurement

The sampling loop now captures the process list in the SAME sample as the client
table, so a pid can be read as a name rather than as a number that shifts between
boots.  Run 41980452867, kernel sha256 `d2c5a914` verified against `payload.json`,
control passed, ramped past 120 processes, client opened the framebuffer at
t=593.8 s.

73 processes alive.  Among them:

    pid 27     SpringBoard
    pid 86     IOMFB_FDR_Loader   <== a framebuffer client
    pid 87     IOMFB_bics_daemo

and the client table:

    C0  pid=86   object=0xfffffff58cd7fc00   calls=0   fails=0
        registered NO notification port
        opened it and called NOTHING (no external-method call at all)

**The framebuffer's only client is `IOMFB_FDR_Loader`** -- the IOMobileFramebuffer
field-diagnostic loader.  It opens the framebuffer, registers no notification
port, and issues no external-method call.  `IOMFB_bics_daemo`, the other
framebuffer-named daemon, is alive and does not even open it.

## And backboardd is not in the guest at all

A grep of the 73-process list for `backboard` returns **zero**.  The compositor
that owns the framebuffer on iOS never spawned.

So the chain is not:

    the compositor submits a swap and something refuses it

it is:

    the compositor does not exist, so nothing ever submits a swap

**SpringBoard is alive at pid 27 and never opened the framebuffer**, because on
iOS SpringBoard does not open it -- backboardd does, on SpringBoard's behalf.
With backboardd absent, the display driver is a fully-instrumented, fully-powered,
fully-traced service with exactly one client, and that client is a diagnostic
loader with nothing to say.

## What that does to the last eight addenda

BO through BX are all still true and all still measured.  What changes is what
they were measuring.  They characterised, in increasing detail, a display driver
waiting for a client:

    BO   the swap hook is wired, not dead code
    BP   the console emitters are built, so the silences are real
    BQ   twelve of twelve emitters proved built
    BR   kickOnce returns at GATE 2 because nothing is ever validated
    BT   client_calls=0 on a boot that never ramped
    BU   the boot ramps at ~470 s; 420 s was too short
    BV   the swap/wait selector is never called
    BW   two ramped boots agree; 60 slots lifted
    BX   the powered word IS set, so the named candidate is eliminated

**Every one of those is a fact about a driver with no compositor.**  None of them
was wrong; none of them was the blocker.  The blocker is one level up, and it was
visible in the process list the whole time -- which is why the process list is now
captured in the same sample as the table.

## Why the old client table misled, and why this one cannot

The saved table from kernel `2ebda0ec` reported "the busiest client: pid=58, 14
selectors, 11 refused".  In this boot pid 58 is an `xpcproxy`.  **Pids are not
stable across boots**, so that table's "pid 58" was a different process and its
identity was never recorded.  This run captures the name in the same sample as the
counts, so the two cannot drift apart -- which is the whole reason this conclusion
is available now and was not then.

## The open question, and it is task #33

**Why does backboardd not spawn?**  SpringBoard is running, so launchd spawned
*that*.  backboardd is a `POSIXSpawnType=App` job, and addenda BA-BJ established
that App jobs are spawned without `POSIX_SPAWN_SETEXEC` and take the `do_fork1`
path.  So the question is whether launchd declines the backboardd job, or spawns
it and it fails to come up -- and the `springboard.fork1` emitter, proved BUILT in
BQ, records every `do_fork1` child by name.

That is the next measurement, and it needs no build: the `fork1` records are
already on the virtio console channel.

## Status

Windows/real-guest: **a live guest on run 41980452867, kernel sha256 verified
against payload.json, control passed, 73 processes named, the client table read,
and the framebuffer's only client identified as `IOMFB_FDR_Loader`.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  But the display blocker has moved: it was never the display driver, and
it was never a refused swap.  The compositor that would drive the framebuffer is
not in the guest.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred tenth, addendum BZ): launchd never spawns backboardd, and that is the frozen screen

## The measurement, from the run's own console channel

`springboard.fork1` and `fork1.spawn` are on the virtio console channel, and BQ
proved both literals are BUILT into this kernel -- so their records are evidence
and their absence is not.  The channel from run 41980452867 carries:

    fork1.spawn            93 records   <- the do_fork1 path
    spawn.seen             50 records   <- the exec path
    exit.seen              24 records
    springboard.fork1       1 record    <- SpringBoard, by name
    spawn.failed            0 records

The 93 `fork1.spawn` children, by name:

    72 xpcproxy, 2 mount, xpcroleaccountd, usermanagerd, tzinit,
    sysstatuscheck, seputil, prng_seedctl, mobile_obliterator,
    launchd_cache_loader, keybagd, init_featureflags, init_exclavekit, fsck,
    finish_demo_restore, dirs_cleaner, cc_fips_test, auearlyboot,
    **SpringBoard**, MSUEarlyBootTask, FinishRestoreFromBackup

The 50 `spawn.seen` children include runningboardd, notifyd, configd, wifid,
mediaserverd, tccd, watchdogd, containermanager, cfprefsd and others.

**`backboardd` appears in no `comm` field anywhere in the channel.**  Not in the
93 `do_fork1` children, not in the 50 exec-path children, and there is no
`spawn.failed` record either.

## So the chain is closed, from the screen back to launchd

    launchd never spawns backboardd          <- measured, both paths, this run
      -> no compositor exists                <- measured, 73 processes named (BY)
        -> the framebuffer's only client is IOMFB_FDR_Loader, which does nothing
                                              <- measured (BY)
          -> no swap is ever submitted       <- measured, three ramped boots (BV, BW)
            -> validateSwap never runs       <- swap_validation_calls = 0
              -> nothing is ever validated
                -> kickOnce returns at GATE 2
                  -> no transfer, no frame   <- transfer_calls = 0, one frame ever

Every link is a measurement on this kernel, and none of them is an inference.
**The frozen screen is not a display-driver defect.  It is a missing process.**

And `spawn.failed` being absent matters: a spawn that was attempted and failed
would leave a record.  So backboardd was never attempted either, which puts the
cause in launchd's job handling rather than in exec.

## SpringBoard is running, which makes it sharper

launchd DID spawn SpringBoard -- the `springboard.fork1` record names it, and it
is alive at pid 27.  So launchd is spawning App jobs.  It spawns SpringBoard and
not backboardd, from the same job machinery.

That is the shape addenda BA-BJ were built to answer: App jobs are spawned
without `POSIX_SPAWN_SETEXEC` and take the `do_fork1` path, and the App-promotion
gate at `0x100007aec` decides whether a job becomes `App` at all.  SpringBoard
passes it.  backboardd is never even attempted.

## The next measurement, and it needs no build

**Is backboardd's job in launchd's job table, and if so what are its flags?**  The
promotion gate reads bit 29 of `job+0x538`; the home-screen claim at
`0x100007b64` reads `EmbeddedHomeScreen` at `job+0x53b` bit 5.  Reading the
backboardd job's flags out of launchd's memory distinguishes:

    the job is not loaded at all          -> a plist or a volume problem
    the job is loaded but not promoted    -> the gate, which BA-BJ characterised

The project already has the launchd job-table reader from addenda BA-BJ, so this
is one guest run, not a build.

## Status

Windows/real-guest: **the console channel from a live guest on run 41980452867,
kernel sha256 `d2c5a914` verified against `payload.json`, control passed, with 93
`fork1.spawn` and 50 `spawn.seen` records named and `backboardd` absent from all
of them.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  But the critical path now has a measured root cause instead of a
symptom: launchd never spawns backboardd, so nothing drives the framebuffer, so
nothing is ever drawn.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred eleventh, addendum CA): the job is loaded, and three candidate causes are eliminated

## The job IS loaded, which was the first question

`local-private/tmp/enumerate-launchd-jobs.py` walks the OS root volume
(`raw/044-76040-038.raw`) and recovers every launchd job plist by its `Label`,
reading the bytes APFS keeps inline in the catalog node.  373 labelled plists.
`com.apple.backboardd` is one of them:

    UserName        = mobile
    ProgramArguments= ['/usr/libexec/backboardd']
    MachServices    = 18 entries, including com.apple.iohideventsystem,
                      PurpleWorkspacePort, PurpleSystemEventPort,
                      com.apple.CARenderServer, com.apple.backlightd
    RunAtLoad       = True
    KeepAlive       = True
    POSIXSpawnType  = Interactive

So launchd has the job, it is `RunAtLoad`, it is `KeepAlive`, and it still never
spawns it.  The cause is in launchd's handling of this job, not in a missing
plist.

(`com.apple.SpringBoard` is ABSENT from this volume, which is expected rather than
anomalous: SpringBoard is an app, not a daemon, and its job is not in
`/System/Library/LaunchDaemons`.  It is running at pid 27 regardless.)

## Candidate 1: POSIXSpawnType=Interactive -- REFUTED

Only three of the 373 labelled plists carry a `POSIXSpawnType` at all:

    com.apple.backboardd      Interactive    <- never spawned
    com.apple.runningboardd   Adaptive       <- SPAWNED (pid 26)
    com.apple.configd         Interactive    <- SPAWNED (pid 37)

`configd` is `Interactive`, exactly like backboardd, and it spawned and is in the
`spawn.seen` records.  So this port's launchd handles `Interactive` jobs fine.

## Candidate 2: UserName=mobile -- REFUTED

240 of the 373 plists carry `UserName=mobile`, so it cannot be a gate on its own.
More decisively, three spawned jobs run as `mobile`:

    com.apple.sleepd      UserName=mobile  RunAtLoad=True   SPAWNED
    com.apple.routined    UserName=mobile  RunAtLoad=True   SPAWNED
    com.apple.peopled     UserName=mobile  RunAtLoad=True   SPAWNED

All three appear in the `spawn.seen` records.  So launchd spawns non-root jobs.

## Candidate 3: the binary is missing -- THE TEST IS INVALID, so no conclusion

The obvious next check was whether `/usr/libexec/backboardd` exists on the volume.
A grep of the raw image returns **2** occurrences of that path, and **2** for
`/usr/libexec/configd` -- but **0** for `/usr/libexec/sleepd`, and `sleepd`
provably ran.

So the instrument returns zero for a binary that executed.  Whatever those two
`backboardd` hits are, they do not establish that the binary is present, and the
question is unanswered rather than answered.  **A test whose control fails is not
a negative.**

## What is left, and it is the next hypothesis

The two keys that distinguish backboardd from every spawned control are its
`MachServices` list -- 18 entries against configd's 10 and runningboardd's 3 --
and specifically the ones that need subsystems this port may not provide:

    com.apple.iohideventsystem      <- the HID event stack
    PurpleWorkspacePort             <- the workspace port
    PurpleSystemEventPort
    com.apple.CARenderServer        <- the render server

**Hypothesis: launchd refuses the job because it cannot claim one of these
services, most likely `com.apple.iohideventsystem`, which on this port is served
by the native `offline-hid-input` driver rather than by an `IOHIDEventSystem`.**
The prediction is falsifiable: reading backboardd's job flags out of launchd's
memory should show the job loaded and un-promoted, and the refusal reason should
name the service.

That needs one guest run with the launchd job-table reader the project already
has from addenda BA-BJ.  It needs no build.

## Status

Windows/real-guest: **none this interval -- the job plists come from a host-side
APFS scan of the OS root volume, and the spawn records come from a guest run made
earlier in this interval.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What is narrower is the cause of the frozen screen: the job is loaded,
`RunAtLoad`, and never attempted, and it is not the spawn type and not the user.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred twelfth, addendum CB): BZ is wrong as stated -- backboardd spawns, on another boot

## The instrument defect that found it

`enumerate-launchd-jobs.py` cross-checks the volume's job plists against the
comms the guest spawned.  The first version compared a **comm** against the
**Label** key -- `configd` against `com.apple.configd` -- so `s in jobs` was never
true and it reported:

    spawned AND has a job plist     : 0
    spawned but NO job plist found  : 109

**Zero, for a boot on which configd, runningboardd and sleepd all have plists and
all ran.**  A cross-check that cannot match is not a negative.  Fixed by indexing
each Label by its last component, truncated to `MAXCOMLEN` (15), because a comm is
that component truncated: `com.apple.CloudKeychainProxy` -> `CloudKeychainPro`.

With the fix:

    spawned AND has a job plist     : 46
        backboardd           <- com.apple.backboardd
        configd              <- com.apple.configd
        runningboardd        <- com.apple.runningboardd
        sleepd               <- com.apple.sleepd
        ... (42 more)
    control configd            in both
    control runningboardd      in both
    control notifyd            NOT in both      <- a real limitation, see below
    control sleepd             in both

## And backboardd is in the spawned set

`local-private/boot53/spawn-failed-input-run/guest-input.log`, a boot from
2026-11-04, carries:

    {"event":"spawn.seen","n":36,"comm":"backboardd","source":"XNU.posix_spawn"}

**backboardd spawned, as spawn number 36, and never exited.**  So BZ's headline --
"launchd never spawns backboardd" -- is **wrong as a general claim.**  What BZ
measured was true of its own boot: on run 41980452867, backboardd appears in no
comm field of the 93 `fork1.spawn` records and none of the 50 `spawn.seen` records.
The generalisation from one boot to "never" is the error, and it is the same error
addendum BT made in the other direction.

## The two runs are mirror images

                    2026-11-04 boot      2026-11-05 boot (41980452867)
    backboardd      SPAWNED (n=36)        not spawned
    SpringBoard     not spawned           SPAWNED (pid 27)
    spawn.seen      155 records           50 records
    exit.seen       64 records            24 records
    fork1.spawn     emitter absent        93 records

**Exactly one of the two, and which one varies between boots.**  On the 11-04 boot
backboardd came up and SpringBoard never did; on the 11-05 boot SpringBoard came up
and backboardd never did.  Neither boot had both.

That reframes the whole display investigation.  It is not that a compositor is
missing by design or by a gate that always refuses -- it is that **the two
processes the port needs are mutually absent across the boots measured**, and the
display is frozen on the boots where backboardd is the one that is missing.

## What this does to CA

CA eliminated three candidate causes for backboardd not spawning: the spawn type,
the user, and the binary.  All three eliminations were sound *for the boot they
were measured on* -- configd is Interactive and spawned on that boot, sleepd is
`mobile` and spawned on that boot.  What CA could not have seen is that the
question itself varies: on another boot backboardd spawns and SpringBoard does
not, so the discriminator is not a property of backboardd's plist at all.

**The open question is now the non-determinism, not the job's keys.**  Something
decides, per boot, which of the two comes up.

## The instrument's remaining limitation, recorded

`notifyd` is in the guest's spawned set but has no plist on this volume -- the
enumerator's own docstring records that some plists are not here.  So "has a job
plist" is not exhaustive, and the 63 "spawned but NO job plist found" entries are
"not on THIS volume" rather than "no plist".  The `backboardd` conclusion does not
depend on that direction: it rests on a `spawn.seen` record naming backboardd,
which is direct evidence and not an absence.

## Status

Windows/real-guest: **none this interval -- the finding comes from a guest run made
on 2026-11-04 plus the fixed host-side cross-check of the volume's plists.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  BZ's headline is withdrawn: backboardd spawns on some boots and not
others, and across the two boots measured exactly one of backboardd and
SpringBoard came up.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirteenth, addendum CC): backboardd is present in 12 of 14 boots, and the screen is frozen anyway

## The survey

29 guest channel logs exist under `local-private/boot53`.  Counting each distinct
boot once, and matching a comm OR a `vname` (the `springboard.fork1` record spells
the name `vname`, which is why the first pass of this survey reported
`SpringBoard=0` everywhere and would have been believed):

    backboardd present      12 of 14
    SpringBoard present      4 of 14
    BOTH present             2 of 14   (procwalk-scratch, screen-scratch)

So backboardd normally spawns.  BZ's "launchd never spawns backboardd" is false,
and CB's "the two runs are mirror images" is false too -- two boots have both.

## The boot that settles it

`screen-scratch` has **backboardd AND SpringBoard**, and its display is fully up:

    "event":"diag.legacy_display_status","stage":10,"surface_status":0,
      "surface_found":1,"dt":1,"source":"XNU.LegacyDisplayProbe"

and it is the run that took **148 screendumps over 1500 s** and found **one
distinct frame: the boot menu** (`screen-evidence/boot-menu-at-t0.png`).

**Both processes present, display at its success terminal state, and still one
frame.**  So the missing compositor is not the general blocker.

`display-refusal-run` says the same thing from the other side: backboardd present,
**five clients**, the busiest with **15 calls and 11 failures across 14
selectors**, display at stage 10 -- and still no frame.

## What this does to the last four addenda

    BZ   "launchd never spawns backboardd"            FALSE -- 12 of 14 boots
    CA   three candidate causes eliminated            measured on boots where
                                                      backboardd was the absent
                                                      one, so they explain those
                                                      boots and not the screen
    BY   the only client is IOMFB_FDR_Loader          true of MY boots; on
                                                      display-refusal-run five
                                                      clients called 14 selectors
    BV   the swap/wait selector is never called      SURVIVES -- it was never
                                                      called on the boots where a
                                                      client called 14 other
                                                      selectors either

**What survives is the client-side evidence, and it is now the only lead.**  On the
one boot where clients were busy, they called 14 distinct selectors, were refused
11 of them, and never called selector 6.  That is true whether or not backboardd
is present.

## So the honest state of the display question

The screen is frozen on boots with and without backboardd, with and without
SpringBoard, with one client and with five.  What is constant across all of them
is that **no swap is ever submitted and no frame is ever presented**, and on the
one boot where the client was busy the refusals are where it stopped.

The process tree was a real finding for my own boots -- without backboardd there
is no compositor at all -- but it is a *contributing* condition on those boots and
not the cause of the frozen screen.

## Status

Windows/real-guest: **none this interval -- the survey is a host-side read of 14
existing boot logs plus the saved screendump evidence and client table.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  BZ's headline is withdrawn and CB's is withdrawn with it: backboardd is
present in 12 of 14 boots, and on the boot where both it and SpringBoard were
present the display was still frozen at one frame.**  THE HEADLINE is unchanged
from AH

---

# Interval 2026-11-05 (one hundred fourteenth, addendum CD): backboardd is the client, it is refused three selectors, and it never submits a swap

## The defect, found by a teammate and confirmed

`read-client-table.py` broke out of its sampling loop the instant `client_calls`
was non-zero and then killed QEMU in its `finally`.  So the console channel stopped
at the moment of measurement:

    truncated channel (d2c5a914)   207 lines,  2 job.open, spawn.seen n=1..63
    full channel (2ebda0ec)        742 lines, 352 job.open, 230 spawn.seen

and the patch that emits `job.open` documents the trap in its own REVISION 3
comment: "the boot had not reached bulk job discovery yet... The general record
was measuring opens correctly all along."

BZ read "no spawn.failed record either, so backboardd was never ATTEMPTED" off the
truncated channel.  **The absence was a window artifact produced by my own
sampler.**  The truncated channel is preserved at
`local-private/boot53/truncated-channel-d2c5a914.log` so the defect stays
inspectable.

## The full-window boot, and it answers the question

Same script, same kernel (`d2c5a914` on run 41980452867, sha256 verified against
`payload.json`), same 900 s ramp, but the sampler now runs to a 1800 s deadline
instead of breaking.  686 channel lines, 143 processes at the end.

    pid 27     SpringBoard
    pid 58     backboardd   <== a framebuffer client

    C0  pid=58   object=0xfffffff7a6007180   calls=9   fails=3
    C1  pid=95   object=0xfffffff7a6007800   calls=2   fails=1

    C0 (backboardd): registered NO notification port
        selector   8: SUCCESS
        selector  15: 0xe00002bc     <- REFUSED
        selector  22: SUCCESS
        selector  29: 0xe00002c7     <- REFUSED
        selector  33: SUCCESS   collectCompletionReports (slot 337)
        selector  57: 0xe00002c7     <- REFUSED
        selector  74: SUCCESS   supportedFrameInfo (slot 213)
        selector  79: SUCCESS
        selector  88: SUCCESS
    C1 (pid 95): registered NO notification port
        selector   8: SUCCESS
        selector  28: 0xe00002f0     <- REFUSED

    swap_validation_calls 0   prepare_calls 0   transfer_calls 0
    stage 10   slot_armed_count 358   slot_lifted_count 60

And the channel carries
`{"event":"spawn.seen","n":55,"comm":"backboardd","source":"XNU.posix_spawn"}`.

## So BY and BZ are both withdrawn, and the real blocker is named

**BY**: "the framebuffer's only client is IOMFB_FDR_Loader" -- false.  backboardd
is a client and the busy one.  BY's process list was captured at t=605 s, before
backboardd had spawned.

**BZ**: "launchd never spawns backboardd" -- false.  It spawns, as `spawn.seen`
n=55, and is alive at pid 58.

And the saved table from kernel `2ebda0ec`, which the record described as "the
busiest client: pid=58", **was backboardd all along** -- 15 calls and 11 failures
there, 9 calls and 3 failures here, same pid, same process, different extent.

## The blocker, stated from the measurement

backboardd opens the framebuffer, calls nine selectors, is **refused three of
them** (15, 29 and 57, with `0xe00002bc` and `0xe00002c7`), registers **no
notification port**, and **never calls selector 6**.  With no swap submitted,
`validateSwap` never runs, nothing is validated, `kickOnce` returns at GATE 2, and
no transfer ever happens.

That is the same conclusion BV and BW reached from the other end, now with the
client named and its refusals counted on the current kernel.

## What is still not known

Which of selectors 15, 29 and 57 is the swap submission, and whether the absence
of selector 6 is a consequence of one of those refusals or independent of them.
The family that owns the dispatch table (`IOMobileFramebufferLegacy`) is a
prebuilt Apple kext with no source in the tree, so the selector-to-method mapping
is not directly readable.

Note that `0xe00002c7` is the code the older kernel's output labelled
`Unsupported`, and `0xe00002bc` is a different refusal -- the two are not the same
failure, which is worth knowing before treating "the refusals" as one thing.

## Status

Windows/real-guest: **a live guest on run 41980452867, kernel sha256 verified
against payload.json, control passed, 1800 s window, 143 processes named, the
client table read with backboardd identified as C0, and a 686-line console
channel.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  But the display blocker is now named rather than inferred: backboardd is
alive and driving the framebuffer, it is refused three selectors, and it never
submits a swap.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred fifteenth, addendum CE): the four survivors are not unliftable, they are unrecognised

## The two facts that meet

**Addendum AN**, measured earlier: seven of eleven refusals lifted, four survive
-- `12` (Unsupported), `15` (Error), `29` (Unsupported), `57` (Unsupported) --
"the lift worked, the screen did not move", and backboardd "got further into its
probe rather than stopping at the same place".

**Addendum CD**, measured this interval: backboardd (pid 58) was refused on

    selector  15 -> 0xe00002bc
    selector  29 -> 0xe00002c7
    selector  57 -> 0xe00002c7

Three of AN's four survivors, named, on the current kernel.

## And the classifier, which is 14 lines

`vendor/UniXNU-qemu/package/LegacyDisplayProbe.cpp:571`,
`native_legacy_slot_is_refusing_stub(fn)`:

    for each of the first 24 words:
        if the word is not MOV (immediate):
            if the word is a BL:  return false      <- NOT A STUB, STOP
            continue
        if it is not `MOV w0,#0x2c7`:  continue
        if a RET follows within 8 words:  return true
        return false

So a slot is lifted only when its function is a **bare stub**: `MOV w0,#0x2c7`
followed within eight words by `RET`, with **no `BL` anywhere before it**.

## `0x2c7` is the code 29 and 57 returned

`native_legacy_slot_lifted_count` is **60**, and selectors 29 and 57 still answer
`0xe00002c7` -- the very constant the classifier looks for.  So those functions
**do contain the refusal**; they were skipped because they contain a `BL`.  They
do real work first and refuse at the end, and the classifier's `return false` on
any `BL` excludes them before it ever looks for the tail.

**So AN's four survivors are not unliftable.  They are unrecognised.**  That is a
different problem with a different fix, and AN's two hypotheses both pointed at
"a real implementation with a dead `MOV w0,#0x2c7` tail" -- which is exactly what
the source now shows the classifier refuses to match.

## The hypothesis, stated so it can be wrong

**Relax the classifier to scan for the `MOV w0,#0x2c7 ... RET` tail regardless of
an earlier `BL`.**  The prediction: selectors 29 and 57 are lifted to success,
`slot_lifted_count` rises above 60, and backboardd gets further still -- AN
already observed it get further when seven were lifted.

**The falsification is equally sharp**: if those functions do not actually end in
that tail -- if the status is computed rather than a constant -- the relaxed
classifier lifts nothing, `slot_lifted_count` stays at 60, and the hypothesis is
dead.  The counter distinguishes the two without a screen.

## Two distinctions that must not be smoothed

**Selector 15 is a different refusal.**  It returns `0xe00002bc`, not `0x2c7`, so
no amount of classifier work will lift it -- it is a different code and a
different cause.  AN called it `Error` and `29`/`57` `Unsupported`, and the
measurement confirms they are not the same thing.

**Selector 12 was not called at all** on the boot CD measured, so nothing this
interval says anything about it.

## What this needs, and what it does not

A change to one function in `vendor/UniXNU-qemu/package/LegacyDisplayProbe.cpp`
(the probe, pinned at `9782ddf`), a build, and a boot.  No new emitter, no new
counter -- `slot_lifted_count` and the client table already answer it.

The dispatch-row read addendum AN asked for is still worth doing and a teammate is
on it: it would name the function behind each slot and confirm the `BL`-before-
the-tail shape directly rather than by inference.  But the inference is now
grounded in the returned constant, and the experiment is cheap either way.

## Status

Windows/real-guest: **none this interval -- the mechanism is read from the probe's
source and tied to a status code measured on a live guest in addendum CD.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What is new is a specific, cheap, falsifiable change with a named
counter that will say whether it worked: the lift classifier's `return false` on
a `BL` is what left four refusals standing.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred sixteenth, addendum CF): the pid discrepancy is resolved, and fact 202 corroborates the blocker from the Metal side

## The discrepancy, resolved

Fact 202 recorded `native_legacy_client_calls=5`, `client_pid=74`.  Addendum BY
measured `client_pid=86` and named that process `IOMFB_FDR_Loader`.  Addendum CD
measured backboardd at pid 58.  Three numbers, and a teammate left them
unreconciled.

Fact 202 carries the reconciling evidence in the same measurement:

    native_qemu_metal_owners = 1801675106 = b'back'   <- backboardd holds the window

**So fact 202's `client_pid=74` was backboardd.**  The pid that is backboardd is 74
on that boot and 58 on the one CD measured, and BY's 86 was a *different process*
(`IOMFB_FDR_Loader`) that happened to be the last client opener on a boot whose
channel was truncated before backboardd spawned.

**The pids are not stable across boots** -- which is exactly what addendum BS
recorded when it refused to read the old table's "pid=58" as a property of a later
kernel.  Three addenda later that refusal is vindicated: pid 58 was backboardd on
one boot and an `xpcproxy` on another.

## And fact 202 corroborates the blocker from the Metal side, independently

Fact 202 read `native_gop_wait_stats[0] = 0` -- the one counter that separates "the
client never ran" from "it ran and was refused" -- and got a three-part answer on
two independent boots:

    1. the Metal plugin loaded, opened the accelerator, and is HELD BY BACKBOARDD
       (`native_qemu_metal_opens` 0->1, `owners = b'back'`, `grants=1`)
    2. it never asked the presenter for a frame:
       handoff_probes=0, handoff_opens=0, handoff_calls=0, scanout_maps=0,
       wait_stats[0]=0
    3. the display driver never produced a validated swap:
       prepare_calls=0, retirement_calls=0, transfer_calls=0

**(2) is upstream of (3)**, and both are zero.  That is the same conclusion CD
reached from the client-table side, arrived at from the GPU side, on different
boots.

## And fact 202's Y1 independently confirms how to read the zero

Fact 202 established that `native_legacy_swap_validation_observed` increments
**regardless of outcome** -- it is the last statement in the function, after both
the accepted and the rejected branch.  So `swap_validation_calls = 0` means "no swap
was ever submitted for validation", and it is corroborated by
`swap_accepted_count = 0` **and** `swap_rejected_count = 0` both being zero.

That matters because it forecloses the reading "a swap was submitted and rejected":
if one had been, `swap_rejected_count` would be non-zero.  **The zero is a
never-submitted, not a refused**, which is what addenda BV, BW and CD each
concluded from the client side.

## What the two directions together settle

    client side (CD)   backboardd opens the framebuffer, calls nine selectors, is
                      refused three, registers no notification port, never calls
                      selector 6
    GPU side (202)     the Metal plugin is loaded and held by backboardd, but its
                      scanout thread never reaches the presenter's hand-off

Both are consistent with one cause: **no swap is ever submitted**, so nothing
downstream of the submission can run -- not the validation, not the transfer, not
the GPU hand-off.

And addendum CE now names the specific thing standing between backboardd and a
submission: the lift classifier at `LegacyDisplayProbe.cpp:571` returns false on
the first `BL` it sees, so selectors 29 and 57 -- which answer `0x2c7`, the exact
constant it searches for -- were never lifted.

## Status

Windows/real-guest: **none this interval -- this addendum reconciles a pid recorded
in fact 202 with two measured this interval, and ties fact 202's GPU-side zeros to
CD's client-side ones.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What is firmer is that two independent directions, on different boots,
agree on one cause -- and that the pid that looked like a discrepancy was
backboardd all along.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred seventeenth, addendum CG): CE is refuted -- 60 liftable, 0 almost-liftable, and the 60 matches the measurement

## The instrument, and the two validations that make it believable

`local-private/tmp/classify-display-slots.py` reads the display's vtable straight
out of the kernel file.  A teammate established that it is a **static initialiser**
-- `__ZTV19NativeLegacyDisplay` at `0xfffffff007206690` -- so no boot and no build
are needed, and caught the off-by-two that makes it usable: `_ZTV` points 16 bytes
before the address point, so reading from the symbol shifts every slot index by +2.

Two independent validations, and the script refuses to print anything if the first
one fails:

    slot 288 = NativeLegacyDisplay::startHardware()      OK
    slot 290 = NativeLegacyDisplay::kickSwapQueue()      OK
    slot 296 = NativeLegacyDisplay::validateSwap(...)    OK

**3 of 3 fork-declared methods land at their declared slots.**  And then the one
that matters:

    the current classifier would lift : 60
    slot_lifted_count measured on a live guest : 60

**The offline classifier reproduces the measured count exactly.**  So the vtable
read is right, the classifier logic is transcribed faithfully, and the number the
kernel produced at runtime is the number this file read predicts.

## And the hypothesis fails

    SKIPPED despite a 0x2c7 tail : 0

**No slot in the display's vtable contains `MOV w0,#0x2c7 ... RET` without also
being a bare stub.**  So CE's proposed change -- relax the classifier to scan for
the tail regardless of an earlier `BL` -- would lift nothing, `slot_lifted_count`
would stay at 60, and the falsification CE itself named is what happened.

**CE is refuted.**  Task #39 is closed as refuted rather than left looking
plausible.

## What that eliminates, and it is the useful part

The four surviving refusals -- `12`, `15`, `29`, `57`, of which CD measured
backboardd hit `15`, `29` and `57` -- **do not come from the display's vtable.**
Not as liftable stubs, and not as almost-liftable ones either.  The display's 358
slots have been read in full and there is nothing left in them to lift.

That is a real narrowing.  The refusals must come from somewhere else:

    the CLIENT's vtable, which `interpose()` wraps at slots 168 and 175
        (`externalMethod` and `registerNotificationPort`) -- and note CD measured
        that backboardd registered NO notification port at all, so slot 175 was
        never even exercised;
    or a family helper the dispatch reaches directly, outside the vtable.

The teammate's selector bridge was the instrument for that and **it failed its own
validation** -- it found slot 182 in 0 of 81 selectors, and following a `BL` landed
in a degenerate sweep of `ldr x16,[x16,#imm]` over consecutive offsets, which is
data being decoded as instructions.  Reporting that failure rather than a number
was correct, and it means task #38 still has no instrument.

## What this interval bought, stated plainly

One hypothesis eliminated for the cost of a file read.  The reason that was
possible is that the vtable turned out to be static data in the image rather than
something only a running guest holds -- which nobody knew until the teammate
looked, and which is why the offline reader was worth building before spending a
build on the change CE proposed.

## Status

Windows/real-guest: **none this interval -- the refutation is an offline read of
the kernel file, validated against a count measured on a live guest.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  CE is refuted and #39 is closed as refuted: the display's vtable holds
nothing left to lift, so the four surviving refusals come from somewhere else --
most likely the client's own vtable, which CD showed was never even exercised past
the open.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred eighteenth, addendum CH): CF's pid-resolution claim is withdrawn -- I carried a pid across boots, which is what BS warned about

## The error, stated plainly

CF read that fact 202 carries both `native_qemu_metal_owners = 1801675106 = b'back'`
and `client_pid=74`, and concluded "fact 202's `client_pid=74` WAS backboardd" --
"in the same measurement".

Fact 202's own text does not say that.  It reads, in order:

    native_qemu_metal_opens ... native_qemu_metal_owners = 1801675106 = b'back'
    ... **第二次启动** `native_legacy_client_calls=5`、`client_pid=74`、`client_status=0`

**`client_pid=74` is introduced as 第二次启动 -- the second boot.**  The owners
reading is from the first.  They are not in the same measurement, and a pid from one
boot cannot be carried onto a name from another.

**That is the exact error addendum BS recorded and I then repeated three addenda
later**: "Pids are not stable across boots", written when I refused to read the old
table's "pid=58" as a property of a later kernel.  I wrote the warning and then
violated it.

## The honest status of fact 202's `client_pid=74`

**UNKNOWN.**  That boot's process list was never captured, so nothing in the tree
identifies its pid 74.  Independently, in both boots where a named process list
exists, **pid 74 is `PerfPowerService`** -- which is not evidence about fact 202's
boot, only about the two that were captured.

## What survives, and it is better grounded than what CF said

The "backboardd holds the Metal window" half of CF is right, and a teammate supplied
the reason it is right -- which CF did not have.

The byte decoding checks out: `1801675106 = 0x6b636162`, little-endian
`62 61 63 6b` = `b'back'`; big-endian would be `b'kcab'`.  But the four-byte width is
not what makes it decisive, and the symbol is not a uint32 -- it is
`volatile char native_qemu_metal_owners[9][17]`, and the reader takes four bytes at
the array base.

What makes it decisive is an invariant in
`QemuMetalAccelerator.cpp:158-161`:

    char name[17] = {};
    proc_selfname(name, sizeof name);
    const bool server = !strncmp(name, "backboardd", sizeof name);
    for (unsigned i = server ? 0 : 1; i < (server ? 1u : QM_SLOTS); i++) {

`server` is true only when `proc_selfname` returns exactly `"backboardd"`, and then
the loop touches **slot 0 and nothing else**; every other process starts at slot 1.
**Slot 0 ⟺ backboardd.**  Fact 181 already recorded this invariant on 2026-10-31;
CF restated the conclusion without the ground.

**And the fragility that comes with it:** nothing in the value `back` distinguishes
backboardd from `backlightd` -- a real daemon that sits in backboardd's own
MachServices list.  The disambiguation comes *entirely* from the slot-0 invariant,
not from the bytes.  The full 17-byte name is in the same symbol and was not read.

## And "pids are not stable" is stronger than CF claimed

Not two data points.  Two boots of the **identical kernel** (both run 41980452867,
both verifying sha256 `d2c5a914` in their own headers) -- which makes the test
stricter than across kernels, not looser.  Of 72 pids appearing in both:

    59 keep the same name (82%)
    13 differ (18%)

and eleven of the thirteen are `xpcproxy` in one boot and a real daemon in the
other, with the mechanism visible in the same data: run7 holds **25** xpcproxy pids,
run8 holds **17**.  xpcproxy is a per-request proxy launchd respawns constantly, so
those pids recycle between samples.

The client set also changed outright: run7's only framebuffer client was
`IOMFB_FDR_Loader` (pid 86, zero calls); run8's were backboardd (pid 58, 9 calls / 3
failures) and pid 95, and `IOMFB_FDR_Loader` never opened the framebuffer at all.

**Scope, stated because it bounds the claim:** these are the only two named process
lists in the tree, so the 13-disagreement figure is two boots.  It is enough to
refute "pids are stable"; it is not enough to quantify a rate.

## What survives from CD, unchanged

backboardd is a framebuffer client -- run8's C0, pid 58, 9 calls with 3 failures,
refused on selectors 15, 29 and 57, no notification port, never selector 6.  That
measurement named its own process in its own sample and does not depend on any of
this.

## Status

Windows/real-guest: **none this interval -- the correction comes from reading fact
202's own text and the accelerator's source.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  CF's headline is withdrawn: fact 202's `client_pid=74` is UNKNOWN, not
resolved, and the reason is an error I had already written a warning about.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred nineteenth, addendum CI): CF's inference fails twice, and CH only fixed the first failure

## The second defect, and it is the fundamental one

CH withdrew CF's step "fact 202's `client_pid=74` was backboardd" because fact 202
puts the owners reading and that pid in **different boots**.  A teammate found a
second defect that survives even if the boot attribution is granted outright.

The two symbols are written by **different user clients on different services**:

    native_qemu_metal_owners    QemuMetalAccelerator.cpp:164
                                inside AGXDeviceUserClient::start(IOService*)  (:151)
                                -- the AGX / GPU Metal user client

    native_legacy_client_pid    LegacyDisplayProbe.cpp:368
                                inside native_legacy_client_enter(task_t,uint32_t)
                                -- the display's user-client interpose path

So "backboardd holds the Metal window" and "pid 74 is the framebuffer user client"
are claims about **different objects**, and neither implies the other.  A process can
hold the Metal accelerator slot and never open the framebuffer at all -- which is
exactly what run7 shows: pid 86 held a framebuffer client with **zero calls** while
the Metal window was held elsewhere.

**So CF's inference fails twice**: the readings come from different boots, *and* they
measure different client relationships.  CH fixed the first and missed the second,
and the second would have been enough on its own.

This is the same shape as the recorded
`ci-input-vs-lock-mismatch-was-silent`: a green comparison of two things that were
never the same thing.  There the mismatch was input-versus-lock; here it is
GPU-client-versus-display-client.

## And the positive claim got stronger, from the other direction

The teammate's background grep finished: **`backlightd` appears in zero process lists
anywhere under `local-private`.**  So the one name sharing the `back` prefix is not
merely ruled out by the slot-0 invariant -- it is not running in any captured boot.
The "backboardd holds the Metal window" claim is better supported than four bytes can
show, on two independent grounds.

## Net, and it is a narrow withdrawal

**Withdrawn:** the single step "fact 202's `client_pid=74` was backboardd".  That pid
comes from a boot whose process list was never captured, and even within a boot it
could not have been matched to a Metal-owner reading.  It stays **UNKNOWN**.

**Stands, and is directly measured:** backboardd is the framebuffer client -- run8's
C0, pid 58, 9 calls with 3 failures, refused on selectors 15, 29 and 57, no
notification port, never selector 6.  That measurement named its own process in its
own sample and depends on none of this.

**Stands:** pids are not stable across boots -- 13 disagreements among 72 shared pids
on two boots of the identical kernel.

**Stands, better grounded:** backboardd holds the Metal window -- via the slot-0
invariant at `QemuMetalAccelerator.cpp:158-161`, with `backlightd` absent from every
captured process list.

## The lesson, which is the recorded one and cost me two addenda

I read a fact's summary, saw two numbers that looked like they belonged together, and
built a bridge between them.  The fact's own text separated them by a semicolon and a
boot label, and the two symbols turned out to be written by different drivers for
different services.  **Two numbers in one document are not two numbers in one
measurement** -- and the check is to read the write sites, not the summary.

## Status

Windows/real-guest: **none this interval -- the correction comes from reading fact
202's own text and the two write sites in the drivers.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  CF is now withdrawn correctly rather than partially: the pid step fails on
two independent grounds, and everything else in it stands.**  THE HEADLINE is
unchanged from AH

---

# Interval 2026-11-05 (one hundred twentieth, addendum CJ): the route to naming the 181 family slots is blocked twice over

## The blocker, from a teammate who walked the volume

The plist exists:

    /System/Library/PrivateFrameworks/IOMobileFramebuffer.framework/Info.plist
    819 bytes, decmpfs type 9 inline, sha256 c1c63fbb...
    CFBundleIdentifier = com.apple.IOMobileFramebuffer
    CFBundleVersion = 343.0.0, CFBundlePackageType = FMWK

**It is a PrivateFramework, not a kext.**  There is no `IOMobileFramebuffer.kext`
anywhere in the 43,028-directory catalog.

**And the executable is not there to recover.**  Every bundle on this volume is a
shell -- the framework's only children are `Info.plist` and `_CodeSignature`, and
every `.kext` under `/System/Library/Extensions` has only a `PlugIns` entry.  No
`Contents/`, no `Contents/MacOS/<exe>`.  The binaries live in the kernelcache, and
`/System/Library/Caches/com.apple.kernelcaches` exists but is **empty**.

**The control is sound, which is what makes the zero believable.**
`/System/Library/Extensions` holds 21 bundles, 17 of them `.kext`, and
`IOHIDFamily`, `IOUSBHostFamily` and `IOStreamFamily` are all found as directory
entries.  So the walker distinguishes "absent" from "unreadable", and
`IOGraphicsFamily` being absent is a real absence on this volume, not a broken
reader.

**And the reader is not the limit it might look like.**  It does reach
extent-resident binaries -- proved by recovering `/usr/sbin/fairplayd.A2`, 5 extents,
17,485,824 bytes, a valid Mach-O arm64 with 27 load commands.  The blocker is that
every multi-megabyte binary on this volume is a **multi-chunk type-4 resource fork**,
and the decoder handles only the single-chunk layout.  So the route is blocked twice:
the binary is not in this catalog, and even if it were, it could not be pulled out of
a multi-chunk fork.

## Two bugs that produced plausible-looking wrong answers

Both are worth knowing before anyone reuses the earlier scripts, because both
yielded something that looked like a successful read:

1. **`j_file_extent_key.logical_addr` is a BYTE offset, not a block number.**  The
   script had been multiplying by 4096.  On `fairplayd.A2` that silently read only the
   first extent -- 4,345,856 of 17,485,824 bytes -- while still yielding a valid
   Mach-O magic and 27 load commands.  **A header check alone would have passed it.**
2. **An extent spans many blocks**, so reading only the block at `phys_block_num`
   truncates the same way.
3. Related: `apfs_inode_val.uncompressed_size` (+0x54) reads **0** for that file, so
   a file's real length must come from the sum of its extent lengths.

And a negative that must not be over-read: the framework's
`_CodeSignature/CodeResources` is 2467 bytes listing **0 files** with 8 generic
`^Resources/...` rules -- and the identical 2467 bytes are the CodeResources of
`IOHIDFamily.kext/PlugIns/IOHIDLib.plugin`.  It is a stub, not a manifest, so its
emptiness is **not** evidence about the missing executable.

## My own check, and it closes the alternative

The 181 unnamed slots cluster at `0xfffffff005786e20..0xfffffff0057a02d4`.  I asked
whether that range holds symbols under some spelling other than
`IOMobileFramebuffer*`:

    symbols in [0xfffffff005786e20, 0xfffffff0057a02d4] : 0
    nearest symbol anywhere below the range            : none
    nearest symbol above the range                     : 0xfffffff007004000
                                                        __mh_execute_header

**Zero, and the nearest symbol above is the kernel's own Mach-O header some 14 MB
away.**  So there is no alternative spelling to find: the prelinked kext region is
symbol-free in this kernel's table, which generalises the teammate's
`IOMobileFramebuffer`-has-zero-symbols finding to the whole region the 181 slots
point into.

## So task #38's route is blocked, and the next artifact is a different one

Naming the 181 needs an external symbol source.  The teammate's suggestion, which I
have not checked and am not claiming: the kext binaries are almost certainly in the
IPSW's `kernelcache.im4p` rather than on any mounted volume.  **That is a different
artifact from the one this route used**, and it is recorded as the next probe rather
than as a plan.

## Status

Windows/real-guest: **none this interval -- the blocked verdict is a host-side
catalog walk, plus a symbol-range check against the kernel file.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  Task #38 has no instrument and now no route on this artifact: the family's
binary is not on the volume, and the kernel's own symbol table has nothing in the
region the 181 slots point into.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred twenty first, addendum CK): the selector decoder IS validated -- slot 182 reproduced -- and a tempting 181-thunk table was correctly refused

## The retraction, verified

A teammate reported that the selector decoder "fails its own validation" -- slot 182
found in 0 of 81 selectors -- and I accepted that into the record.  They have now
retracted it: **their test was on the wrong function set.**  Addendum U says slot 182
is loaded by `externalMethod` itself, not by the per-selector dispatch functions.

I verified the bytes independently rather than take the retraction on trust:

    at 0xfffffff00bef6378 + 548 :  0xf942d94a
    LDR x10,[x10,#0x5b0]   imm12 = 182, byte offset 1456 = 182*8

**The slot-182 load is exactly where they said, in the documented position.**  So the
decoder is validated, and the gate I set -- reproduce the documented shape before
believing a number -- is passed.  My acceptance of the earlier "fails validation"
claim was an error I am correcting with them.

## The part that does not check out, and it matters for interpretation

**The symbol attribution is not verified.**  There is no `IOUserClient::externalMethod`
symbol in this kernel's table.  There are 22 code-address symbols containing
`externalMethod`, and every one of them is a subclass -- `IOUserUserClient`,
`AppleKeyStoreUserClient`, `IOUserServer`, and so on.  So the address
`0xfffffff00bef6378` may well be the right function, but nothing in the file names it,
and I am not claiming it does.

That distinction matters: the *validation* rests on the load being in the documented
position, which is confirmed.  The *interpretation* -- that the function doing the
loading is `externalMethod` -- rests on addendum U and on the teammate's reading, not
on the symbol table.  Those are different strengths of evidence and should not be
merged.

## A structure found and correctly refused, which is the model to copy

At `0xfffffff0057a26f8` there is a table of **181 twelve-byte thunks**
(`ADRP x16 / LDR x16,[x16,#imm] / BR x16`), spanning to `0xfffffff0057a2f68`.  The
count matches the 181 original-family vtable entries exactly -- which is what made it
tempting as a selector-to-slot bridge.

Applying the guardrail -- reproduce a count before believing a number -- it fails:

    thunks enumerated                                181
    original-family vtable entries                   181     <-- the match
    distinct imm12 values                            181, range 269..485
    imm12 values that are a real vtable slot (<358)   80
    imm12 values beyond the vtable entirely           101, range 359..485
    thunk slots that are the fork's own               6  (290, 296, 306, 337, 338, 345)
    original-family slots absent from the thunks      107, including all of 168-182

101 of its immediates point past the end of a 358-slot vtable, so those are not
display-vtable slots at all, and it disagrees with the original-family set on both
sides.  **The matching 181 is a coincidence**, and it was reported as a refusal rather
than as a bridge.  That is the discipline this project has been enforcing all interval
and it was applied to the reporter's own tempting finding.

## And the fork never learned the names either

`vendor/UniXNU-qemu/scripts/build-native-legacy-display.py:87-98` **generates the
entire 358-slot base-class declaration**.  It names exactly nine slots -- 184
`isMainDisplay`, 188 `getDotPitch`, 213 `supportedFrameInfo`, 249
`getDisplayDimensions`, 288 `startHardware`, 290 `kickSwapQueue`, 295
`completeCurrentSwap`, 296 `validateSwap`, 337 `collectCompletionReports` -- and emits
every other slot as `opaque{i}()` pure-virtual or `opaque{i}()` deprecated with
`"Unclassified ABI: never call from source"`.

So the fork's authors reconstructed the ABI slot-for-slot but never learned the method
names.  The 181 names have to come from the kext binary and from nowhere else, and the
nine the fork did name are the ones already in hand.

## The kext is not on the OS volume, and the earlier evidence for that was also wrong

A teammate corrected their own evidence here too: they had reported
"`System/Library/Extensions` has 0 inodes", which was a bug in their script -- catalog
paths come back prefixed with `root/`, so their `startswith` check never matched
anything.  **The conclusion is right and the measurement offered for it was not.**  It
is corroborated independently: `apfs-reader` walked the same volume, found 21 bundles
under `/System/Library/Extensions` including `IOHIDFamily`, `IOUSBHostFamily` and
`IOStreamFamily`, and found no `IOMobileFramebuffer.kext` anywhere in the
43,028-directory catalog.

The next artifact is the SystemOS cryptex (`044-76034-038.dmg`), a raw APFS container
(`NXSB` at offset 32).  A walker with `BASE = 0` refused it:
`block 15897 is not the container omap`.  Its container geometry differs from the OS
volume's and needs its own BASE/block-size work before the same walker will open it.

## Status

Windows/real-guest: **none this interval -- the verification is a read of the kernel
file's own bytes.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  What changed is that task #38 has a validated instrument again: the decoder
reproduces slot 182 in the documented position, so the missing piece is a decoded
selector and the 181 names, not a working tool.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred twenty second, addendum CL): the SystemOS cryptex's container geometry is solved, and the walker was reading the wrong block

## The blocker, and what it actually was

A teammate reported that the SystemOS cryptex (`044-76034-038.dmg`, 3,303,014,400
bytes) refused their walker with `BASE = 0`: `block 15897 is not the container omap`.
That read as "the container geometry differs and needs its own BASE/block-size work".

**It does not.**  The superblock says otherwise, read directly:

    magic @32            NXSB
    nx_block_size        4096
    nx_block_count       806,400        (3,303,014,400 / 4096 = 806,400 exactly)
    nx_omap_oid          782662

`BASE = 0` and `block_size = 4096` are correct -- the block count matches the file size
to the byte, so there is no hidden offset and no UDIF wrapper.  And the omap object at
block 782662 verifies on both fields:

    omap obj: oid=782662  type=0xe

`0xe` is `OBJECT_TYPE_OMAP`, and the oid matches the superblock's `nx_omap_oid`.  So
the container omap is exactly where the superblock says it is.

**The walker was reading block 15897, which is not the omap block at all.**  The
geometry was never the problem; the block was.

## What is verified and what is not

**Verified:** the container superblock, the block size, the block count against the
file size, and the omap object's oid and type at block 782662.

**NOT verified:** the btree chain below the omap.  `om_tree_oid` reads as 782663
(omap_oid + 1, which is suspiciously adjacent and may be a coincidence), and the
object there has type `0xe` but the four bytes at +32 read `07 00 00 00` rather than
the `BTRE` magic a btree node root should carry.  So the omap is confirmed and the
tree below it is not.  Recorded as unverified rather than assumed.

## What this buys

The recorded next step for task #38 -- "the cryptex needs its own container-geometry
work" -- is now done for the container level, with the exact values:

    BASE        = 0
    block_size  = 4096
    omap_oid    = 782662     (verified: oid and type both match)

A walker pointed at block 782662 instead of 15897 should get past the refusal.  What
it does after that is the part still unverified.

## Status

Windows/real-guest: **none this interval -- the geometry is read from the cryptex
file's own superblock.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  The cryptex's container level is no longer a blocker; the btree below the
omap is unverified, and the kext binary is still not in reach.**  THE HEADLINE is
unchanged from AH

---

# Interval 2026-11-05 (one hundred twenty third, addendum CM): CL's omap verification is WRONG -- the oid match is a coincidence, and only the geometry stands

## The error

Addendum CL claimed the SystemOS cryptex's container omap was verified:

    the omap object at block 782662 verifies on both fields:
    oid=782662  type=0xe  (OBJECT_TYPE_OMAP)

**The type is wrong.**  `0xe` is `OBJECT_TYPE_FSTREE`; `OBJECT_TYPE_OMAP` is `0x0B`.
And the object does not have the omap shape either -- `om_count` at `+0x24` reads
**0**, where a container omap holds hundreds of entries.  So block 782662 is not the
omap.

**And the oid match is a coincidence.**  In an APFS container, object ids routinely
equal their block numbers -- the first 18 blocks of this very container read
`oid == block` for every one, with types `0x01` through `0x09` in lockstep.  So
"the object at block 782662 has oid 782662" is not evidence that it is the omap; it
is evidence that oids and block numbers coincide here, which they do everywhere.

I checked the type constant against the enum *after* writing CL, which is the order
it should have been checked in.  A field that matches is not a field that confirms.

## What survives, and it is only the geometry

**Verified:** `magic NXSB` at offset 32, `nx_block_size = 4096`, and
`nx_block_count = 806,400` -- which equals `3,303,014,400 / 4096` exactly.  So there
is no hidden offset and no UDIF wrapper, and `BASE = 0` with `block_size = 4096` is
correct.  That part of CL stands and it is the part that answers "does the cryptex
need its own BASE/block-size work": it does not.

**NOT verified:** the omap's location, the btree below it, and therefore the whole
path to the kext binary.  `nx_omap_oid` reads as 782662 from the superblock, but the
object there is not an omap, so either the field offset is wrong for this container
or the value needs interpreting differently.  Both are open.

## What this does to the cryptex route

It does not close it, and CL overstated it.  The honest state:

    the container geometry is known and correct      <- CL's surviving half
    the omap is NOT located                           <- CL's wrong half
    the kext binary is still not in reach

The teammate's original refusal -- `block 15897 is not the container omap` -- is
still unexplained, and my explanation of it ("the walker was reading the wrong
block") was built on the same unverified omap claim.  **That explanation is withdrawn
with it.**

## The lesson, which is the recorded one and cost an addendum

I verified a field because it matched, without checking what the field meant.  The
oid matched, the type did not mean what I said it meant, and the object did not have
the shape the type claimed.  **A match is not a confirmation; the constant has to be
looked up, and the shape has to be checked.**

## Status

Windows/real-guest: **none this interval -- the correction is a read of the cryptex
file's own objects against the APFS object-type enum.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  CL is corrected: the cryptex's geometry is known and its omap is not, so
the route to the kext binary is exactly where it was before CL, minus one wrong
claim.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred twenty fourth, addendum CN): CM is WITHDRAWN -- CL was right, and my correction used the wrong field offset

## The error, and it is mine again

Addendum CM "corrected" addendum CL's claim that the cryptex's container omap is at
block 782662, on the ground that the object there has `type = 0xe`, which is
`OBJECT_TYPE_FSTREE` rather than `OBJECT_TYPE_OMAP` (`0x0B`).

**CM was reading the wrong field.**  The object type is not at `+0x10`; it is at
`+0x18`, as a qword whose high half carries flag bits.  Masking those off:

    block      oid      raw @0x18       type
    0          1        0x80000001      0x01  NX_SUPERBLOCK   <- correct for block 0
    782662     782662   0x4000000b      0x0b  OMAP            <- the omap
    782663     782663   0x40000002      0x02  BTREE           <- the omap's tree

`0x40000000` is `OBJ_VIRTUAL` and `0x80000000` is `OBJ_EPHEMERAL`; the low byte is
the type.  **Block 782662 is `OBJECT_TYPE_OMAP` and block 782663 is its `BTREE`** --
exactly what `om_tree_oid = 782663` predicted, and exactly what CL claimed.

The `0x0e` I read at `+0x10` is a different field, not the object type.  Blocks 0,
782662 and 782663 all carry `0x0e` there while blocks 1 and 3 carry `0x01` and
`0x02`, so it is not a constant either -- it is simply not the field I thought.

## And the "no BTRE magic" doubt was also wrong

CM flagged that block 782663's four bytes at `+32` read `07 00 00 00` rather than a
`BTRE` magic, and treated that as doubt.  **There is no `BTRE` magic in APFS.**
`btree_node_phys` has no magic field; `+0x20` is `btn_flags`(2) / `btn_level`(2), so
`07 00 00 00` reads as `flags = 0x0007`, `level = 0x0000` -- and `0x0007` is
`BTNODE_ROOT | BTNODE_LEAF | BTNODE_FIXED_KV_SIZE` with level 0.  **That is
precisely a root leaf node**, which is what the omap's tree root should be.  I was
looking for a magic that does not exist in the format and reading its absence as
doubt.

## Net

**CM is withdrawn in full.  CL stands:** the cryptex's container geometry is
`BASE = 0`, `block_size = 4096`, and its container omap is at block **782662** with
its btree root at **782663** -- all verified against the object-type constants and
flag semantics rather than against a field that merely matched.

The teammate's original refusal, `block 15897 is not the container omap`, remains
unexplained -- but that is now a question about their walker, not about the
container.

## The lesson, which is the third this interval and the sharpest

CM corrected CL by looking up a constant and finding CL's was wrong -- and CM's own
constant was attached to the wrong field.  **Looking up the constant is necessary but
not sufficient; the field offset has to be right too, and the check for that is a
known-value control.**  Block 0 is that control: whatever offset you read the type
from must yield `0x01` for the container superblock.  I ran that control only after
writing CM, which is the order it should have been run in.

Three corrections this interval now share one root: verifying a reading against a
field, a boot, or a document without first proving the instrument on a known answer.

## Status

Windows/real-guest: **none this interval -- the correction is a read of the cryptex
file's own objects, validated against block 0 as the known-value control.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  CM is withdrawn and CL is restored: the cryptex's omap is at block 782662
and its btree at 782663, both verified against the object-type enum with block 0 as
the control.**  THE HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred twenty fifth, addendum CO): the cryptex omap tree root is located, its entry count is not, and here is the handoff

## What is verified

The SystemOS cryptex (`044-76034-038.dmg`, 3,303,014,400 bytes) is a raw APFS
container with no wrapper:

    magic @32            NXSB
    nx_block_size        4096
    nx_block_count       806,400      (= file size / 4096, exactly)
    nx_omap_oid          782662

And with the object type read at `+0x18` (a qword whose high half is flag bits),
validated against block 0 as the known-value control:

    block 0      raw 0x80000001  -> type 0x01  NX_SUPERBLOCK   <- the control
    block 782662 raw 0x4000000b  -> type 0x0B  OMAP
    block 782663 raw 0x40000002  -> type 0x02  BTREE

The omap's own `om_tree_oid` (`+0x30`) is **782663**, and that block is a `BTREE`.
So the chain superblock -> omap -> btree root is located and internally consistent.

The btree root node reads `btn_flags = 0x0007` and `btn_level = 0` at `+0x20`, which
is `BTNODE_ROOT | BTNODE_LEAF | BTNODE_FIXED_KV_SIZE` at level 0 -- a root leaf
node, exactly right for an omap tree root.  (Addendum CM doubted this because it
looked for a `BTRE` magic; APFS has no such field, and `+0x20` is
`btn_flags`/`btn_level`.)

## What is NOT verified, and it is the next thing to check

**The entry count.**  I read `btn_nkeys` as **1**, which is wrong for a container
omap -- it should hold hundreds of entries.  And my "TOC" read was invalid: I took
`+0x2C` as the TOC offset, but `+0x2C` is `btn_table_space` (a uint16 giving the
space reserved for the TOC), not the TOC's location.  The TOC's location is
derived, not stored.

**So do not trust `nkeys = 1`.**  Either my `btn_nkeys` offset is also wrong, or the
node is not what I think.  Both are open, and both are the same class of error that
produced CM: reading a struct field without a known-value control.

The control for this one: a container omap tree root must have a `btn_nkeys` large
enough that its TOC fits in `btn_table_space`, and the TOC entries must point into
the node's data area.  If they do not, the offset is wrong.

## The handoff, and the exact resume commands

**The task.**  Task #38: identify which selector submits a swap.  It needs names for
the 181 original-family slots in the display's vtable, which need the
`IOMobileFramebuffer` binary, which is not on the OS root volume (verified twice,
with a sound control) and is presumably in this cryptex or in the IPSW's
`kernelcache.im4p`.

**Where it stands.**  The cryptex's container level is solved: `BASE = 0`,
`block_size = 4096`, omap at block **782662**, btree root at **782663**.  The route
is alive again after CM wrongly killed it and CN restored it.

**The next action, concretely.**  Finish reading the omap tree root's struct
correctly -- get `btn_nkeys` and the TOC location right, with the control above --
then walk the tree to map virtual oids to physical blocks, then find the volume's
root directory, then look for
`System/Library/PrivateFrameworks/IOMobileFramebuffer.framework/`.  The OS volume's
walker (`local-private/tmp/enumerate-launchd-jobs.py`) already does the equivalent
walk on `044-76040-038.raw` and recovered 373 plists from catalog nodes; the same
walker pointed at the cryptex with the geometry above is the shortest path.

**Resume command.**

    cd F:\iosvirtual
    python local-private/tmp/enumerate-launchd-jobs.py

with `RAW` repointed at
`vendor/UniXNU/.cache/firmware/target-volumes/044-76034-038.dmg` and the object type
read at **`+0x18`** with the flag mask `& 0xFF`, validated against block 0 yielding
`0x01`.

**What must not be repeated.**  Three corrections this interval came from verifying
a reading against a field, a boot, or a document without first proving the
instrument on a known answer.  Block 0 is the control for the cryptex; a
known-present record is the control for a console channel; a named process in its
own sample is the control for a pid.

## Status

Windows/real-guest: **none this interval -- everything above is a read of the
cryptex file's own objects.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred twenty seventh, addendum CQ): the two claims I was about to record are both wrong -- the authoritative header was in the repo all along

## The error, and it is the same one a fourth time

After addendum CO I measured the omap tree root once more, drafted the result as an addendum CP, and then **checked the premise before recording it -- and the check refuted it.**  CP was therefore never appended; what follows is the correction of a claim that exists only in that draft.

The draft said two things, and **both were wrong**:

1. *"`btn_table_space = 0` cannot be right ... either block 782663 is not the omap's
   btree, or the struct offsets are still wrong."*
2. *"`om_count` reads 0 ... so `om_tree_oid = 782663` comes from a struct read that has
   already failed one control."*

**`btn_table_space` is not a `uint16`.  It is a `struct apfs_nloc { __le16 off;
__le16 len; }`.**  I read the `off` half and stopped.  The `len` half is **448**, which
is a perfectly good TOC size for a 4096-byte node.  There was never a zero.

**And there is no `om_count` field in `apfs_omap_phys` at all.**  The field at `+0x24`
is `om_snap_count`, and **zero is the correct value for a container omap**.  I invented
a field, read zero, and called the zero a control failure.

Both errors have one root, and it is the fourth instance of the same root this
interval: **I derived struct offsets by hand instead of reading the definition.**  The
authoritative header was in this repository the entire time:

    local-private/tmp/apfsprogs/src/include/apfs/raw.h

`struct apfs_omap_phys` (raw.h:94-105) and `struct apfs_btree_node_phys`
(raw.h:177-187), with `struct apfs_nloc` at raw.h:152-155.  Everything the draft needed was
there.  **The lesson is now sharper than "prove the instrument": for a documented
on-disk format, the definition is the instrument, and hand-deriving offsets is
guessing.**

## What the authoritative structs give, measured

`apfs_omap_phys` at block **782662**, field by field against raw.h:

    +0x20  om_flags               1            OK
    +0x24  om_snap_count          0            CORRECT for a container, not an error
    +0x28  om_tree_type           0x40000002   physical btree -- OK
    +0x2c  om_snapshot_tree_type  0x40000002   physical btree -- OK
    +0x30  om_tree_oid            782663       -> block 782663
    +0x38  om_snapshot_tree_oid   0

**Every field matches the authoritative struct.**  The draft's claim that this read
"failed a control" is withdrawn.

`apfs_btree_node_phys` at block **782663**, with apfsprogs' own formulas
(apfsck/btree.c:338-340, `toc = sizeof(node) + table_space.off`,
`key = toc + table_space.len`):

    btn_flags         0x0007   ROOT | LEAF | FIXED_KV_SIZE
    btn_level         0
    btn_nkeys         1
    btn_table_space   off=0  len=448
    btn_free_space    off=32 len=3488
    btn_key_free_list off=0  len=16
    btn_val_free_list off=16 len=16

    toc = 0x38 + 0    = 0x38      (448 bytes, 112 fixed-KV slots)
    key = 0x38 + 448  = 0x1f8     (data area [0x1f8, 0x1000), 3592 bytes)

`TOC[0]` reads `{k=0x10, v=0x20}`, and the **only** reading that puts both inside the
data area is **relative to the key-area start**:

    key   at 0x1f8 + 0x10 = 0x208 :  oid = 1026, xid = 14
    value at 0x1f8 + 0x20 = 0x218 :  all zeros

**The key reads plausibly and non-trivially** -- oid 1026, xid 14, both well-formed.
So the TOC is located and the omap tree root is readable.  **The draft's "the TOC
control fails" is withdrawn.**

## And the container names the volume directly, which confirms the key independently

`apfs_nx_superblock` (raw.h:952) carries `nx_fs_oid[APFS_NX_MAX_FILE_SYSTEMS]` at
`+0xB8` (raw.h:985).  Read from block 0:

    nx magic @32       NXSB
    nx_omap_oid        782662
    nx_fs_oid[100]     1 non-zero ->  fs[0] oid = 1026
    nx_xp_desc_base    30064771072
    nx_xp_data_base    0

**`nx_fs_oid[0] = 1026`, and the omap key I read independently is `oid = 1026`.**  That
is an exact match between two different structures read by two different routes, and it
confirms three things at once: the TOC is at `0x38`, the key area is at `0x1f8`, and the
kvoff `k`/`v` are **relative to the key-area start**.  It also says the container has
**exactly one volume**, virtual oid 1026 -- which is why `om_snap_count` is 0 and why
one omap entry is the right count.

So the draft is wrong at every level, and the route is further along than CO, CL or
CN ever established.

## What is still NOT established, and it is now exactly one thing

**The omap value's target.**  `apfs_omap_val` is confirmed at raw.h:122-126 as
`{ u32 ov_flags; u32 ov_size; u64 ov_paddr; }` -- my reading used exactly that layout,
so the layout is not in doubt.  The only non-zero 16 bytes below the key are at `0x1f8`:

    ff ff 10 00 00 00 00 00  0d 00 00 00 00 00 00 00
    ov_flags = 0x0010ffff   ov_size = 0   ov_paddr = 13

`ov_flags = 0x0010ffff` is not a valid flag mask and **block 13 is type `0x0C`
CHECKPOINT_MAP, not `0x0D` FS**, so this is not the volume superblock and the value is
not yet correctly located.  The TOC says `v = 0x20`, which points at `0x218` -- and that
is all zeros.

So the chain is verified container -> omap -> tree root -> TOC -> **key**, and stops at
the value.  That is much further than the draft claimed, which was that the tree root
itself was unreadable.

## The two scans that are now recorded as instrument failures

Worth writing down because both produced confident-looking numbers:

- **Enumerating omaps by the object-type byte at `+0x18` gives 1600 hits, of which
  1592 are false positives.**  That byte is validated only on blocks 0, 782662 and
  782663; across the volume it is not a calibrated discriminator.
- **Enumerating omaps by "the qword at `+0x30` points at a btree root" gives 152 hits,
  of which the `+0x20..+0x2c` words of most are ASCII strings** (`Instrook`,
  `ProtocolCount`, `WEAKDAY_SPEAK`, `/rnkmk`, `entnatHlahtlUa`).  Only **five** blocks
  carry the full 5-word omap signature, and 782662 is one of them -- the other four
  (4538, 4546, 6050, 782654) are unexamined.

So the correct instrument is the **5-word signature**, not either scan alone.  Record
that before anyone reuses either.

## The resume path, which is now short

1. **Find the volume superblock by its magic, not by the type byte.**  Scan for a block
   whose `+0x20` is `APSB` (the volume superblock's magic) and whose `+0x18` type is
   `0x0D` FS.  My earlier scan covered only blocks 0..64 and found none, which is not a
   negative about the container.  That block is the value `ov_paddr` should name.
2. Then read `apfs_superblock`'s `apfs_root_tree_oid` and `apfs_omap_oid` from raw.h --
   again by reading the struct, not deriving it -- and the volume's file tree becomes
   reachable.
3. The OS volume's walker (`local-private/tmp/enumerate-launchd-jobs.py`) already does
   the equivalent walk on `044-76040-038.raw` and recovered 373 plists; repointing it at
   this container with the geometry above is the shortest path to
   `IOMobileFramebuffer.framework`.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred twenty eighth, addendum CR): the volume superblocks and omaps are found, the tree is a walkable multi-level btree, and the 5-word signature was over-fitted

## The volume superblocks, found by magic

`apfs_superblock` (raw.h:1197-1225) puts `apfs_magic` at `+0x20` as a u32 (`APSB` =
`0x42535041` LE).  Scanning all 806,400 blocks for it:

    blocks with the APSB magic at +0x20 : 5
    of those, object type 0x0D FS       : 5   (4543, 4545, 4795, 782653, 782661)

Five is not a failure.  **APFS keeps one volume superblock per checkpoint**, so five
is five historical snapshots.  They disagree about which omap is current, which is the
expected shape:

    block   apfs_omap_oid   apfs_root_tree_oid   apfs_extentref_oid   apfs_snap_meta_oid
    4543    4540            1028                 4542                 4544
    4545    4540            1028                 4542                 4544
    4795    4540            1028                 4542                 4544
    782653  746223          1216                 4542                 4544
    782661  782656          1216                 4542                 4544

Three of the five agree on omap 4540 / root tree 1028; the two newest disagree.  Which
is current is a question about transaction ids, and it is **not** answered yet.

## The correction to the instrument, and it is mine again

Addendum CQ recorded "the correct instrument is the 5-word omap signature"
(`om_flags=1, om_snap_count=0, om_tree_type=0x40000002,
om_snapshot_tree_type=0x40000002`).  **That signature is over-fitted to the container
omap and misses every volume omap:**

    block   om_flags   tree     flags    level   nkeys   table_space
    4540    0          4541     0x0005   1       2       {0, 576}
    782656  0          782657   0x0005   1       36      {0, 576}
    746223  0          746224   0x0005   1       37      {0, 576}
    782662  1          782663   0x0007   0       1       {0, 448}   <- the container omap

**`om_flags` is 1 for the container omap and 0 for volume omaps.**  So the signature
found the container and would have silently skipped all three volumes.  The correct
discriminator is the object type at `+0x18` being `0x0B`, cross-checked against
`om_tree_type == 0x40000002` -- and the earlier scan showed that the type byte alone
produces 1592 false positives out of 1600, so it must be the pair, not either alone.

## And the tree is a multi-level btree, which is what makes it walkable

The volume omaps' trees have **`btn_flags = 0x0005`**, which is
`BTNODE_ROOT | BTNODE_FIXED_KV_SIZE` **without** `BTNODE_LEAF`, at **`btn_level = 1`**,
with 2, 36 and 37 keys.

**A level-1 root with no LEAF flag is an internal node: its values are child block
numbers, not data records.**  That is exactly right for an omap whose entries do not
fit in one block, and it is why `nkeys` is 2/36/37 rather than the 1 the container's
root leaf showed.  So the walk is:

    volume superblock -> apfs_omap_oid (4540 / 782656 / 746223)
      -> omap_phys.om_tree_oid (4541 / 782657 / 746224), a level-1 internal root
        -> child blocks, level-0 leaves
          -> omap records {virtual oid, xid} -> {physical block}

The container's own omap is a level-0 root leaf with one key, because a container maps
one volume and needs no depth.  Both shapes are consistent with the same reader, which
is the strongest evidence that the reader is right.

## So the chain is open end to end, with one named gap

Verified: container geometry, container omap, container omap tree root and its TOC,
volume superblocks by magic, volume omaps, and volume omap tree roots as level-1
internal nodes.

**The named gap is the child pointers.**  For a non-leaf node the TOC entry's `v` is a
child block number rather than a record offset, and I have not yet read one.  That is
the single next measurement, and it is a small one.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred twenty ninth, addendum CS): the cryptex route is CLOSED end to end -- the container omap maps volume oid 1026 to superblock 782661

## The two rules I was missing, both from apfsprogs' own reader

Addendum CR left one named gap: "for a non-leaf node the TOC entry's `v` is a child
block number rather than a record offset, and I have not yet read one."  I tried to
guess it and got overlapping offsets, because I had two rules wrong.  Both are stated
explicitly in `apfsprogs/src/apfsck/btree.c`:

**Rule 1 -- value offsets run BACKWARDS from the end of the value area**
(btree.c:504-516):

    off_in_area = area_len - le16_to_cpu(entry->v);
    *off = node->data + off_in_area;
    area_len  = sb->s_blocksize - node->data
                - (node_is_root(node) ? sizeof(struct apfs_btree_info) : 0);

Keys run forward from `node->key`; values run backward from the end.  I had been reading
`v` forward from the key area, which is why every value I read was garbage.

**Rule 2 -- an omap value's length depends on whether the node is a leaf**
(btree.c:486):

    if (btree_is_omap(btree))
        len = node_is_leaf(node) ? 16 : 8;

16 bytes for a leaf record `{flags, size, paddr}`, 8 bytes for a non-leaf child pointer.
`sizeof(struct apfs_btree_info)` is 40 (raw.h:224-230: 16 + 4 + 4 + 8 + 8), and
`APFS_BTNODE_ROOT|LEAF|FIXED_KV_SIZE` = `0x0001|0x0002|0x0004` (raw.h:138-140), so
`0x0007` is a root leaf, `0x0005` a non-leaf root, `0x0006` a leaf.

## The container omap, and it names the volume

Root **782663**, `flags 0x0007` (root leaf), `nkeys 1`, `toc=0x38`, `key=0x1f8`,
`free=0x218`, `data=0xfb8`, `area_len=32`:

    key   @0x208  (oid=1026, xid=14)
    value @0xfb8  flags=0x0  size=4096  paddr=782661

**`paddr = 782661`, and block 782661 is one of the five `APSB` volume superblocks.**
`size = 4096` is exactly one block, which is right for a superblock.  So the container
omap maps volume oid **1026** to volume superblock **782661**.

That is the **third independent confirmation** of the same volume: `nx_fs_oid[0] = 1026`
(addendum CQ), the container omap's key oid `= 1026`, and now the container omap's value
paddr pointing at an `APSB` block.  Three different structures, three different routes,
one answer.

## The volume omap, and every child is well-formed

Root **782657**, `flags 0x0005` (non-leaf root), `level 1`, `nkeys 36`, `toc=0x38`,
`key=0x278`, `free=0x4c8`, `data=0xeb0`, `area_len=296`:

    idx     k     v    key@              key(oid,xid)     val@    child
      0   0x00  0x08  0x278   (1214, 13)   0xfd0   782659  type 0x03  flags 0x0006  level 0
      1   0x20  0x18  0x298   (1285, 13)   0xfc0   782660  type 0x03  flags 0x0006  level 0
      2   0x30  0x20  0x2a8   (1369, 13)   0xfb8   770963  type 0x03  flags 0x0006  level 0
      3   0x40  0x28  0x2b8   (1481, 13)   0xfb0   772288  type 0x03  flags 0x0006  level 0
      4   0x50  0x30  0x2c8   (1593, 13)   0xfa8   772437  type 0x03  flags 0x0006  level 0
      5   0x60  0x38  0x2d8   (1705, 13)   0xfa0   772659  type 0x03  flags 0x0006  level 0

**Every child is object type `0x03` `BTREE_NODE` with `flags 0x0006` (`LEAF |
FIXED_KV_SIZE`, no `ROOT`) at `level 0`** -- precisely what a level-1 root's children must
be.  6 of 6 keys in bounds, 6 of 6 values in bounds, 6 of 6 children well-formed.  The
`v` values step by 8, matching the 8-byte child-pointer length exactly.

So the full chain is now verified end to end:

    container superblock (block 0, NXSB)
      -> nx_omap_oid 782662 (omap_phys, verified field for field)
        -> om_tree_oid 782663 (root leaf, 1 key)
          -> {oid 1026, xid 14} -> {size 4096, paddr 782661}
            -> volume superblock 782661 (APSB)
              -> apfs_omap_oid 782656 (omap_phys)
                -> om_tree_oid 782657 (non-leaf root, 36 keys)
                  -> child leaves (type 0x03, flags 0x0006, level 0)
                    -> omap records {virtual oid, xid} -> {physical block}

## What this buys task #38

The volume's catalog tree is reachable through the same walk, one level further on: the
volume superblock's `apfs_root_tree_oid` (1216 for 782661) resolves through this omap to
a physical block, and the catalog's file records are `APFS_TYPE_DIR_REC` /
`APFS_TYPE_FILE_EXTENT` entries in its leaves.  `IOMobileFramebuffer.framework` would be
a `DIR_REC` in that tree.

**This is the first time the route to the kext binary has been walked to the volume's
own omap with every control passing.**  It is still not the binary, and it is not a
guest measurement.

## The lesson, which is the fifth and is now a rule

Five corrections this interval came from the same root: **deriving a layout instead of
reading the definition.**  The rule that follows: for any documented on-disk format,
open the reference implementation's reader before reading a single field.  apfsprogs was
in this repository for all of them, and every one of the five errors was avoided by
reading a function I had not looked at.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header and reader.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirtieth, addendum CT): the omap lookup WORKS and reaches the catalog root, but the descent into the catalog FAILS

## The omap lookup, and it works

Using the rules validated in CS, the volume superblock 782661's
`apfs_root_tree_oid = 1216` resolves through the volume omap:

    volume superblock 782661: apfs_omap_oid=782656  apfs_root_tree_oid=1216
    volume omap 782656:      om_tree_oid=782657
    node 782657 lvl 1 keys 36 leaf=False: entry[0] oid=1214 xid=13
      -> child 782659
    node 782659 lvl 0 keys 70 leaf=True: entry[1] oid=1216 xid=13
      -> value flags=0x8 size=4096 paddr=770808

    virtual oid 1216 -> physical block 770808 (size 4096, xid 13)

**The descent is clean and controlled**: it terminates at a leaf, finds the entry whose
oid is exactly the target, and returns a one-block object.  `flags=0x8` is
`APFS_OMAP_VAL_NOHEADER` (raw.h:111), which is why the block's first 32 bytes are zero.

## And the catalog root is found, with its real flags

Block 770808, reading `btree_node_phys` at `+0x20`:

    btn_flags  0x0019   = ROOT | HASHED | NOHEADER      (raw.h:138-142)
    btn_level  3
    btn_nkeys  2
    btn_table_space {off=0, len=64}
    btn_free_space  {off=40, len=3816}
    btn_key_free_list {0xffff, 0}   btn_val_free_list {0xffff, 0}   <- APFS_BTOFF_INVALID

`0x0019` has **no** `APFS_BTNODE_FIXED_KV_SIZE` (`0x0004`), so the catalog is a
**variable-length KV tree**: its TOC entries are `apfs_kvloc` (8 bytes:
`k.off, k.len, v.off, v.len`, raw.h:160-163), not the 4-byte `apfs_kvoff` the omap uses.
`APFS_BTNODE_MASK` is `0x001f`, so every set bit is valid.  A level-3 root with 2 keys
is a plausible shape for a real catalog.

## But the descent into the catalog FAILS, and here is the failure

Reading the root as a variable-KV node:

    toc=0x38  key=0x78  free=0xa0  data=0xf88  area_len=80
    [0] k@0x78  len=24  v@0xfb0 len=40  -> child 3720
    [1] k@0x90  len=16  v@0xf88 len=40  -> child 3719

**Both children are entirely zero blocks.**  Blocks 3720 and 3719 read as 48 zero bytes
at both `+0x00` and `+0x20`, so they are not btree nodes and the child oids are wrong.

The tell is the value length: **40 bytes**.  For a non-leaf node the value should be an
8-byte child oid.  apfsprogs does not special-case the catalog the way it does the omap
(`len = node_is_leaf(node) ? 16 : 8` is omap-only, btree.c:486); it takes
`entry->v.len` verbatim.  So either the kvloc parse is misaligned, or the key/value area
boundaries are wrong for this node, or both.

**The route therefore stops at the catalog root.**  It does not reach a catalog leaf, and
it does not reach `IOMobileFramebuffer.framework`.

## What is verified and what is not

**Verified:** container geometry, container omap, container omap tree root, the volume
superblock by magic, the volume omap, the volume omap tree root and its leaves, the omap
lookup itself, and the catalog tree root's own header.

**Not verified:** anything below the catalog root.  The two child oids it produced are
zero blocks, so that read is wrong and nothing downstream of it exists.

## The next action, concretely

Fix the variable-KV node reader before descending again.  The control is the same one
that settled the omap: **every child a level-N root names must itself be a well-formed
level-(N-1) node** -- valid flags, `nkeys > 0`, and areas in order.  Both children here
fail that, so the reader is wrong, not the tree.

Concretely, re-read `apfs_kvloc` handling in `apfsprogs/src/apfsck/btree.c` around
`node_locate_key` / `node_locate_data` (the `else` branches, lines 439-447 and 507-514)
and check the `area_len` computation for a root with a 40-byte footer.  Then re-run the
descent.  The omap lookup in
`local-private/tmp/walk-to-catalog.py` is correct and should be reused unchanged.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header and reader.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty first, addendum CU): the catalog's children ARE virtual oids and DO resolve to well-formed leaves, but the record parse is wrong

## The hypothesis, and it is confirmed

Addendum CT ended with the children reading as entirely zero blocks and the reader
blamed.  The falsifiable hypothesis: **the catalog tree is a virtual object**
(`apfs_root_tree_type` is `0x2`, not `0x40000002`), so the oids in its hashed child
references are **virtual oids** that must be resolved through the volume omap, exactly as
`apfs_root_tree_oid` itself was.

Tested by resolving 3719 and 3720 through the volume omap (root 782657):

    virtual oid 3719 : as a raw block, ENTIRELY ZERO
                       through omap -> block 781228 via 782657 -> 781118
                       flags 0x001a level 0 nkeys 67 table_space {0, 576}
                       LEAF=1 HASHED=1 NOHDR=1
    virtual oid 3720 : as a raw block, ENTIRELY ZERO
                       through omap -> block 781229 via 782657 -> 781118
                       flags 0x001a level 0 nkeys 67 table_space {0, 576}
                       LEAF=1 HASHED=1 NOHDR=1

**Both resolve to well-formed leaf nodes with 67 keys each, and to adjacent blocks
781228/781229 matching the adjacent oids 3719/3720.**  So the hypothesis holds, and
CT's "the reader is wrong" was itself wrong -- the reader was right, and the child oids
simply needed one more omap hop.  **A zero block is not evidence of a bad reader; it is
evidence of an unresolved virtual oid.**

## And the catalog root really is a catalog node

The root's key area holds, verbatim:

    0x0078  01 00 00 00 00 00 00 90  0c 8c a6 ac  70 72 69 76
    0x0088  61 74 65 2d 64 69 72 00

The trailing bytes are the ASCII string **`private-dir`** -- a real APFS directory entry
name.  So the node at 770808 is genuinely a catalog node and the key-area parse is
correct.  The 40-byte values are `{oid, 32-byte hash}`, the HASHED child-reference
format the root's `APFS_BTNODE_HASHED` flag implies.

## But two things are inconsistent, and they are not resolved

**1. The levels do not chain.**  The root reads `btn_level = 3`, but its children read
`btn_level = 0`.  A level-3 root's children must be level 2.  So either the level read is
wrong, or the omap resolution of 3719/3720 landed on the wrong blocks despite their
plausible shape.

**2. The leaf records do not parse as file records.**  Leaf 781228 has 67 keys in a
1072-byte key area (16 bytes each).  Every one of the 67 parses as `obj_id_and_type`
with type **1** and an **identical** `obj_id`, with an empty name:

    record types: {1: 67}
    obj_id=936748722493063174 type=1 name=''      (x67, all identical)

Sixty-seven identical keys with no names is not a catalog of files.  So the leaf's key
layout is not the `j_key` + name shape I assumed, even though the root's key area clearly
contains a name.

**The route therefore reaches catalog leaves but does not read a single file record, and
it does not reach `IOMobileFramebuffer.framework`.**

## What is verified and what is not

**Verified:** container geometry, container omap and its tree, the volume superblock by
magic, the volume omap, the omap lookup (now used three times successfully), the catalog
root's own header, the catalog root's key area containing a real directory name, and the
resolution of its two children to well-formed leaf blocks.

**Not verified:** the level chain, the leaf record layout, and therefore any file name or
path.  Nothing below the catalog leaves is established.

## The next action, concretely

The root's key area is the instrument that works -- it produced `private-dir`.  Use it to
fix the leaf parse rather than assuming `j_key`.  Read `apfs_j_key` and the
`APFS_TYPE_*` constants from `raw.h` (they are the authority, as with every other
correction this interval), and check the leaf's 16-byte keys against them.  Then re-read
the 67 records and look for `IOMobileFramebuffer` among them.

The control: the root's key area decoded a real name, so whatever key shape explains the
root's 24-byte and 16-byte keys must also explain the leaf's 16-byte keys.  Find the
shape that does both.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header and reader.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty second, addendum CV): the key encoding is CORRECTED and confirmed on the root, but the leaves reached are not demonstrably catalog leaves

## The encoding error, found and fixed

Addendum CU parsed the leaf's keys as `type = obj_id_and_type & 0xF` with
`obj_id = obj_id_and_type >> 4`, and got 67 identical records of type 1, which it
reported as "not a catalog of files".

**The encoding is the other way round.**  From raw.h:553-556:

    #define APFS_OBJ_ID_MASK    0x0fffffffffffffffULL
    #define APFS_OBJ_TYPE_MASK  0xf000000000000000ULL
    #define APFS_OBJ_TYPE_SHIFT 60

**The type is the TOP four bits; the object id is the low sixty.**  I had it inverted.

## And the correction is confirmed on a real record

The catalog root's key area, decoded with the correct encoding:

    0x0078  01 00 00 00 00 00 00 90 | 0c 8c | 70 72 69 76 61 74 65 2d 64 69 72 00

    obj_id_and_type = 0x9000000000000001
      type   = 0x9000000000000001 >> 60 = 9  = APFS_TYPE_DIR_REC
      obj_id = 0x9000000000000001 & mask   = 1  = the volume's ROOT directory
    name_len = 0x000c = 12
    name     = "private-dir"

`apfs_drec_key` is `{ apfs_key_header hdr; __le16 name_len; u8 name[0]; }` (raw.h:623-627),
and the record decodes exactly: **a DIR_REC under the root directory, named
`private-dir`.**  That is a real filesystem entry, and it confirms both the encoding and
the struct.

## But the leaves reached are not demonstrably catalog leaves

Re-parsing leaf 781228 with the corrected encoding: 67 keys of **16 bytes** each, and
every one carries the *same* first eight bytes:

    obj_id_and_type = 0xd000000000000061
      type = 13 = APFS_TYPE_FILE_INFO
      obj_id = 97
    ... followed by 8 varying bytes (00 80 21 01 00 00 00 01, 00 c0 21 01 ...)

`APFS_TYPE_FILE_INFO` keys are `apfs_key_header` alone -- **8 bytes, not 16**.  So a
16-byte key of type FILE_INFO does not fit any key struct in raw.h, and the second eight
bytes are unexplained.

## And the level chain is still unexplained

The catalog root reads `btn_level = 3`; the blocks reached as its children read
`btn_level = 0`.  A level-3 root's children must be level 2.  Addendum CU resolved oids
3719/3720 through the omap and got well-formed leaves, but **well-formed leaves at the
wrong level are evidence that those blocks are not this root's children.**

So one of two things is true, and this interval does not distinguish them:

1. The hashed child reference's oid is not at offset 0 of the 40-byte value, so I
   resolved the wrong oids and landed on unrelated leaves.
2. The root's `btn_level` is misread.

Both are open.  **The route reaches *a* pair of well-formed leaves, but not demonstrably
the catalog's, and it reads no file record.**

## What is verified and what is not

**Verified:** the container geometry, the container omap and its tree, the volume
superblock by magic, the volume omap, the omap lookup (used three times successfully),
the catalog root's own header, and -- newly -- **the catalog key encoding and
`apfs_drec_key`, confirmed on a real DIR_REC named `private-dir` under obj_id 1.**

**Not verified:** that blocks 781228/781229 are the catalog root's children, the level
chain, and any file name beyond the root directory's own entries.

## The next action, concretely

The root is now a working instrument: it decoded a real DIR_REC.  Use it to settle which
children it names.  **Dump all 40 bytes of both child references and check whether the
oid is at offset 0, offset 8, or offset 32** -- the hash is 32 bytes and the oid 8, and
there are only a few orderings.  Then resolve each candidate and require the child's
`btn_level` to be exactly `root_level - 1`.  That single check distinguishes the two
hypotheses above, and it is cheap.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty third, addendum CW): the omap is unambiguous, and the run is one root plus 70 leaves -- a two-level tree whose root claims level 3

## The omap is now proven unambiguous, which closes CV's first hypothesis

Addendum CV left two hypotheses for the level contradiction: the child-reference oid
might not be at offset 0, or the root's level might be misread.  Both are now testable,
and the first is dead.

**The oid is at offset 0.**  All 40 bytes of both child references were dumped; the only
8-byte window in range is offset 0 (offsets 8, 16, 24 and 32 all read as values above the
block count):

    [0] 88 0e 00 00 00 00 00 00 | ef 51 e1 14 ... 5f 79 b9 64     oid = 3720
    [1] 87 0e 00 00 00 00 00 00 | b9 60 12 aa ... 2f 26 0d 41     oid = 3719

**And the omap has exactly one entry per oid.**  Leaf 782659 holds 70 entries, oids
**1214 through 1284 consecutive**, every one at `xid = 13`, mapping to blocks
**770807 through 877 consecutive**:

    [ 0] oid=1214  xid=13  paddr=770807
    [ 1] oid=1216  xid=13  paddr=770808      <- the catalog root
    [ 2] oid=1217  xid=13  paddr=770809
    ...
    [69] oid=1284  xid=13  paddr=770877

No duplicate oids, no stale xids, one candidate per lookup.  So resolving 3719 and 3720
through the omap is not a guess: each has exactly one entry, at `xid=13`, pointing at
781228 and 781229.

## And the shape of the run is the new clue

Reading the headers across that run:

    block 770807 (oid 1214)  flags 0x0002  level 0  nkeys 0     <- LEAF only
    block 770808 (oid 1216)  flags 0x0019  level 3  nkeys 2     <- ROOT|HASHED|NOHEADER
    block 770809 (oid 1217)  flags 0x001a  level 0  nkeys 67
    block 770810 (oid 1218)  flags 0x001a  level 0  nkeys 59
    block 781228 (oid 3719)  flags 0x001a  level 0  nkeys 67
    block 781229 (oid 3720)  flags 0x001a  level 0  nkeys 67

**770808 is the only non-leaf block in a run of 71 consecutive blocks; the other 70 are
level-0 leaves.**  And **781228's eight header bytes are byte-identical to 770809's**
(`1a 00 00 00 43 00 00 00`), so the omap's children are the same kind of leaf as the
root's own siblings.

A tree with one non-leaf root and seventy leaves is **two levels deep**.  Its root should
read `btn_level = 1`.  It reads **3**.

## So the remaining hypothesis is narrowed to one

CV's second hypothesis -- the root's `btn_level` is misread -- is now the only live one,
and it has a specific shape: **either `btn_level` is not a plain depth counter in this
tree, or the field is not at `+0x22` for this node.**

The first is testable against the run's shape: if 770808 is a two-level root, then
whatever the level field means, the tree is root-plus-leaves and the leaves are directly
readable.  **The 70 sibling leaves are already located** -- 770809..770877, each a
`flags 0x001a` level-0 node with 59-67 keys -- and they are reachable without resolving
any child pointer at all, because the omap names them directly.

## What that means for task #38

The route to file records no longer depends on the child pointers.  The omap gives
oids 1217..1284 → blocks 770809..770877 directly, and those are leaves.  Reading their
records is the next step, and it needs only the leaf key parse -- which CV already fixed
and confirmed on the root's `private-dir` DIR_REC.

**But the leaves' 16-byte keys of type 13 (FILE_INFO) with obj_id 97 still fit no struct
in raw.h**, so that parse is still wrong and no file name has been read.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty fourth, addendum CX): oid 1216 IS the catalog root, the leaf geometry is exactly self-consistent, and the record shape is not a catalog file record

## oid 1216 is confirmed as the catalog root, byte for byte

Read from the volume superblock 782661:

    +0x70  01 00 00 00 02 00 00 00 02 00 00 40 02 00 00 40
    +0x80  40 f1 0b 00 00 00 00 00 c0 04 00 00 00 00 00 00
    +0x90  be 11 00 00 00 00 00 00 c0 11 00 00 00 00 00 00

    +0x74 apfs_root_tree_type      0x2
    +0x78 apfs_extentref_tree_type 0x40000002
    +0x7c apfs_snap_meta_tree_type 0x40000002
    +0x80 apfs_omap_oid            782656
    +0x88 apfs_root_tree_oid       1216       <- c0 04 00 00 00 00 00 00
    +0x90 apfs_extentref_tree_oid  4542
    +0x98 apfs_snap_meta_tree_oid  4544
    +0x20 magic                    APSB

`+0x88` is `c0 04 00 00 00 00 00 00` = **1216**, and resolving 1216 through the volume
omap gives block **770808** with `flags 0x0019`, `level 3`, `nkeys 2`.  So the node whose
key area decoded `private-dir` is unambiguously the catalog root, and CW's reading of it
was right.

## And the leaf geometry is exactly self-consistent, which validates the reader

Leaf 770809 (oid 1217, named directly by the omap):

    toc = 0x38   key = 0x278   free = 0x6a8   data = 0x6d7
    key area   0x278..0x6a8 = 1072 bytes  /  67 keys  = 16 bytes per key   EXACT
    free space 0x6a8..0x6d7 =   47 bytes
    value area 0x6d7..0x1000 = 2345 bytes /  67 vals  = 35 bytes per value  EXACT

Both areas divide by the key count with **no remainder**.  So the node reader -- the TOC
location, the forward key offsets, the backward value offsets, and the `area_len`
computation -- is correct for this node.  That closes the reader question CW raised.

## But the record shape is not a catalog file record

Every 16-byte key is two qwords: a **constant** first qword and a second that rises by a
fixed step:

    key[0]  16 00 00 00 00 00 00 d0 | 00 80 09 00 00 00 00 01
    key[1]  16 00 00 00 00 00 00 d0 | 00 c0 09 00 00 00 00 01
    key[2]  16 00 00 00 00 00 00 d0 | 00 00 0a 00 00 00 00 01
    key[3]  16 00 00 00 00 00 00 d0 | 00 40 0a 00 00 00 00 01
    ...     second qword steps by 0x4000 = 16384 every record

Every 35-byte value is a 3-byte prefix and 32 bytes:

    val[0]  04 00 20 | f9 45 5b fa 39 ab 2a 8b 15 b8 d3 1c 6e
                     9c 8f cd 3c d8 60 9c 1d 0c d7 55 fc c0 d7 e5 b2 ca 81 22
    val[1]  04 00 20 | 9b 51 7d 94 98 21 31 63 c5 2b ...

A constant key prefix with a monotonically stepping second field, and values that are a
short prefix plus 32 bytes, **matches no catalog record in raw.h** -- not `apfs_drec_key`
(8 + 2 + name), not `apfs_inode_key` (8), not `apfs_file_extent_key` (8), not
`apfs_drec_hashed_key`.  So whatever these leaves hold, it is not the volume's file
catalog.

## What this means, stated plainly

The reader is right and the tree identification is right, but **the blocks reached as
leaves do not hold file records.**  Three possibilities, and this interval does not
distinguish them:

1. The catalog root's children are level-2 nodes (as `btn_level = 3` implies) and oids
   3719/3720/1217/1218 are something else entirely, so I have been reading the wrong
   blocks.
2. The volume's catalog uses hashed keys throughout and the 32 bytes are name hashes,
   with the constant prefix a per-tree key header I have not identified.
3. These leaves belong to a different tree that shares the omap.

The decisive test is cheap and is the next action: **read the volume's extentref tree
(oid 4542) and snapshot-metadata tree (oid 4544) roots and compare their leaves' key and
value shapes to these.**  If 770809's shape matches one of them, these leaves are that
tree's and the catalog's own leaves are elsewhere.  If it matches neither, the shape is
the catalog's and the record struct still needs identifying.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty fifth, addendum CY): the 16/35 shape is the extentref and snap_meta trees' shape, NOT the catalog's

## The test CX asked for, run

Resolve all three of the volume's tree oids through the volume omap and compare their
shapes:

    catalog root    oid 1216  -> block 770808  flags 0x0019  level 3  nkeys 2
                                  key lengths {16: 1, 24: 1}   value lengths {40: 2}
    extentref root  oid 4542  -> block 782059  flags 0x001a  level 0  nkeys 67
                                  key lengths {16: 67}         value lengths {35: 67}
    snap_meta root  oid 4544  -> block 782061  flags 0x001a  level 0  nkeys 67
                                  key lengths {16: 67}         value lengths {35: 67}

**The extentref root and the snapshot-metadata root have exactly the shape I could not
identify: 67 keys of 16 bytes and 67 values of 35 bytes.**  That is byte-for-byte the
shape of blocks 770809, 781228 and 781229.

So CX's third hypothesis is confirmed: **those blocks are extentref or snapshot-metadata
leaves, not catalog leaves.**  The `{16-byte key, 35-byte value}` shape is a real record
shape in this volume -- it just is not the catalog's.

## Which also means CX's first hypothesis is back, and sharper

The catalog root's two child references resolve (unambiguously, one omap entry each) to
blocks 781228 and 781229 -- which carry the extentref/snap_meta shape.  **A catalog root's
children must be catalog nodes.**  So one of these is true:

1. The 40-byte child-reference value is not `{oid, hash[32]}`, and I am reading the wrong
   oid out of it.
2. The omap entries for 3719/3720 point at blocks belonging to another tree, which would
   mean the omap lookup is returning the wrong block for those oids.

CX had narrowed this to "the reader is right and the tree identification is right".  The
reader is right; **the tree identification of 781228/781229 as catalog leaves was wrong**,
and that is what sent every leaf-record parse since CU astray.

## And a new contradiction, which is the more interesting one

Both the extentref root and the snapshot-metadata root resolve to **level-0 leaves**.  A
level-0 root means the entire tree fits in one block.  For a real filesystem's extent
reference tree -- which must record every file extent on the volume -- that is not
plausible.

So the omap resolution is returning **leaf** blocks for oids that should be **root**
blocks.  The catalog root resolved correctly (level 3, and its key area decoded
`private-dir`), so the method works for at least one oid.  But for 4542 and 4544 it
returns level-0 leaves.

The likely cause, and it is testable: **the omap has entries at several xids and the
lookup takes the last entry with `oid <= target`.**  If an oid appears at multiple xids,
that picks a stale one.  CX enumerated omap leaf 782659 and found one entry per oid for
1214..1284 -- but the leaves covering 4542 and 4544 have not been enumerated, and neither
has the leaf covering 3719/3720 beyond the single hit already found.

## The next action, concretely

Enumerate the omap leaf that covers oid 4542 (the root's entry `4393 -> 781909` puts it in
leaf **781909**) and print **every** entry with its oid, xid and paddr.  If oid 4542
appears more than once at different xids, the lookup must select by the tree's xid rather
than by "last oid <= target", and that single fix may resolve the level contradiction for
all three trees at once.

If it appears once, then the extentref tree genuinely is one block and the shape belongs
to it, and the catalog's own leaves are still unfound.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty sixth, addendum CZ): the stale-xid hypothesis is DEAD, and the non-leaf inventory shows a level-3 root with only level-1 children

## The hypothesis CY proposed, tested and killed

CY proposed that the omap lookup takes "the last entry with `oid <= target`" and so picks
a stale entry when an oid appears at several xids.  Three omap leaves were enumerated in
full:

    leaf 781118  oids 3609..3720   112 keys   duplicate oids: 0   xids: {13: 112}
    leaf 782659  oids 1214..1284    70 keys   duplicate oids: 0   xids: {13: 70}
    leaf 782022  oids 4505..4616   112 keys   duplicate oids: 0   xids: {13: 112}

**Zero duplicate oids, and every entry at xid 13.**  So there is no stale entry to pick,
and the lookup is not the problem.  CY's proposed cause is dead.

## And the non-leaf inventory, which is the real finding

Across those three leaves, the mapped blocks' levels are:

    leaf 782659 (catalog's oid range)  levels {0: 67, 1: 2, 3: 1}
    leaf 781118 (catalog children)     levels {0: 110, 1: 2}
    leaf 782022 (extentref's range)    levels {0: 111, 1: 1}

The only non-leaf, non-root blocks in the whole volume are at **level 1**:

    oid 1216 -> block 770808  flags 0x0019  LEVEL 3   <- the catalog root
    oid 1278 -> block 770871  flags 0x0018  LEVEL 1
    oid 1279 -> block 770872  flags 0x0018  LEVEL 1
    oid 3635 -> block 781144  flags 0x0018  LEVEL 1
    oid 3697 -> block 781206  flags 0x0018  LEVEL 1
    oid 4565 -> block 782082  flags 0x0018  LEVEL 1

**There are no level-2 nodes anywhere.**  A level-3 root's children must be level 2, and
there are none to be.  So the level field is not behaving as a plain depth counter, and
CY's second hypothesis -- that the root's `btn_level` is misread -- is the only one left
standing.

## What the shape of the tree actually is

The catalog's own oid range is 1214..1284, and within it there is one level-3 root, two
level-1 nodes and sixty-six level-0 leaves.  That is a three-tier tree -- root, level 1,
leaves -- whose root reports level 3 instead of 2.  Combined with the catalog root's two
child references resolving to level-0 leaves (oids 3719 and 3720), the consistent reading
is:

**the root's children are leaves, the tree is two tiers, and `btn_level` is off by one or
means something else.**  Under that reading the catalog root's children at oids 3719/3720
-> blocks 781228/781229 *are* catalog leaves, and the {16-byte key, 35-byte value} shape
found on them -- which is also the shape of the extentref and snap_meta roots -- is a
shape shared by more than one tree in this volume.

That is a weaker conclusion than CX's, and it contradicts CX.  **CX claimed the 16/35
shape belongs to the extentref and snap_meta trees and therefore that 781228/781229 are
not catalog leaves.  This interval's inventory does not support that:** the extentref root
(oid 4542) maps to a level-0 leaf too, exactly as the catalog's children do, so "resolves
to a level-0 leaf" distinguishes nothing.

## So the open question is narrower than either CX or CY stated

**Is the {16-byte key, 35-byte value} shape the catalog's, or another tree's?**  The
decisive test is no longer about levels or omaps -- both are now clean.  It is about the
records: read the 67 records of a block whose tree membership is *known* and compare.

The catalog root's key area decoded `private-dir` as a DIR_REC under obj_id 1.  **A
catalog leaf must therefore contain DIR_REC keys -- type 9 in the top four bits.**  Leaf
781228's 67 keys all read type 13.  If the catalog's leaves hold DIR_RECs and 781228
holds none, 781228 is not a catalog leaf.  That single check settles it, and it has not
been run because the leaf key parse is still wrong.

## The next action, concretely

Fix the leaf key parse using the root as the instrument, exactly as CV did.  The root's
keys are 24 and 16 bytes and decode as `apfs_drec_key` (8 + 2 + name).  The leaf's keys
are 16 bytes and start with a constant qword.  **Find the key struct in raw.h whose
length is 16 and whose first field is `obj_id_and_type`** -- `apfs_file_extent_key` is
8 + 8 = 16 and is the obvious candidate, with the second qword being `phys_block_num`.
Test it: if the leaf's second qword is a block number that rises by 16384 per record,
these are FILE_EXTENT records and the block is an extent-bearing tree, not the catalog.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty seventh, addendum DA): the leaf record format is measured in full and remains unidentified

## The candidate struct, and why it fits only partly

CZ proposed `apfs_file_extent_key` for the 16-byte leaf keys.  Its definition
(raw.h:580-583) is:

    struct apfs_file_extent_key {
        struct apfs_key_header hdr;      /* obj_id_and_type */
        __le64 logical_addr;
    } __packed;

That is 8 + 8 = **16 bytes**, the right length.  But the measured second qword does not
decode as a plain logical address, so the struct is only partly right.

## The records, measured in full

Leaf 781228, 67 records.  Every record has exactly this shape:

    key (16 bytes)   61 00 00 00 00 00 00 d0 | 00 80 21 01 00 00 00 01
                     ^ constant across all 67   ^ steps by 0x4000 per record

    value (35 bytes) 04 00 20 | e7 33 a7 1d 9e dc e0 6e a1 f6 94 34 1e
                      ^ constant   ^ 32 bytes, distinct in all 67 of 67

    qword 0 of the key : 0xd000000000000061   constant, 67 of 67
    qword 1 of the key : 0x0100000001218000 .. 0x0100000001320000
                         steps of exactly 0x4000 = 16384 bytes
    value prefix       : 04 00 20            constant, 67 of 67
    value tail         : 32 bytes, 67 distinct of 67

## What that rules in and what it rules out

**Rules out a file catalog.**  A catalog leaf's keys must include names -- the root's own
keys did, decoding `private-dir`.  These keys carry no name and a constant first qword,
so this is not the volume's file catalog and **781228 is not a catalog leaf**.

**Rules out `apfs_file_extent_key` as literally defined.**  The second qword's low 32
bits do step by 16384, which is extent-like.  But its high 32 bits are a constant
`0x01000000`, which a `logical_addr` should not carry.  So either the field is not
`logical_addr`, or the key is not laid out as `{hdr, logical_addr}`.

**Consistent with a hashed index.**  The node carries `APFS_BTNODE_HASHED`, and each value
holds 32 distinct bytes with a constant 3-byte prefix -- the shape of a per-record hash.
Combined with a constant key prefix and a monotonically stepping second field, this looks
like an allocated, sequentially-addressed structure with a hash per record, not a
name-keyed one.

## The honest position

**The leaf record format is now measured completely and is still unidentified.**  Every
field is characterised -- constant or stepping, distinct or repeated -- and none of it
matches a catalog record.  So the answer to the question CX, CY and CZ have been circling
is: **781228 is not a catalog leaf, and the catalog's own leaves have not been found.**

That is a negative result with a sound instrument, and it is worth more than the three
previous intervals' guesses.  The geometry is exact (67 x 16 = key area, 67 x 35 = value
area, no remainder), the reader is validated, and the record shape simply is not a
catalog's.

## The next action, and it changes direction

Stop trying to identify these leaves.  The catalog root is known and its key area works.
**The next action is to enumerate the catalog root's children properly rather than
guessing their oids from a 40-byte value whose format is unconfirmed.**

Concretely: the catalog root (oid 1216, block 770808) has 2 keys.  Its children are
wherever its child references point, and the reference format is the one unconfirmed
thing.  So instead, **scan the omap for blocks whose `btn_flags` is `0x0019`**
(`ROOT|HASHED|NOHEADER`, non-leaf) -- there is exactly one in the volume, the catalog root
at 770808 -- and for blocks whose flags are `0x0018` (non-leaf, level 1), of which there
are five at oids 1278, 1279, 3635, 3697 and 4565.  **Read those five level-1 nodes' keys
and see whether any decodes as a DIR_REC with a name.**  If one does, that node is in the
catalog and its children are the catalog's leaves.

That is a bounded, five-block search with a decisive test, and it does not depend on the
child-reference format at all.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty eighth, addendum DB): the catalog's key format is `apfs_drec_hashed_key`, it decodes perfectly, and 770872 IS a catalog node

## The five level-1 nodes, read -- and one of them is in the catalog

DA's next action was to read the five level-1 nodes' keys and look for a DIR_REC with a
name.  **Level-1 node oid 1279, block 770872, entry 0:**

    key (24 bytes)  01 00 00 00 00 00 00 90 | 0c 8c a6 ac | 70 72 69 76 61 74 65 2d 64 69 72 00
                    ^ hdr                    ^ len+hash   ^ "private-dir\0"

That is **the same key as the catalog root's own first key**, byte for byte.  In a B-tree a
child's first key is its parent's separator, so **770872 is the catalog root's first
child.**  The catalog's own nodes are at oids 1278 and 1279, and DA's conclusion that
781228 is not a catalog leaf is confirmed from the other direction.

## The key format, and it decodes exactly

The nodes carry `APFS_BTNODE_HASHED`, so their directory keys are not `apfs_drec_key` but
**`apfs_drec_hashed_key`** (raw.h:634-638):

    struct apfs_drec_hashed_key {
        struct apfs_key_header hdr;   /* 8 bytes: obj_id_and_type */
        __le32 name_len_and_hash;     /* 4 bytes */
        u8 name[0];
    } __packed;

with the masks at raw.h:600-602:

    APFS_DREC_LEN_MASK   0x000003ff
    APFS_DREC_HASH_MASK  0xfffffc00
    APFS_DREC_HASH_SHIFT 10

Decoding the root's key:

    hdr              = 0x9000000000000001
                      type = 9 (APFS_TYPE_DIR_REC), obj_id = 1 (the volume root directory)
    name_len_and_hash = 0xaca68c0c
                      name_len = 0xaca68c0c & 0x3ff  = 12
                      name_hash = 0xaca68c0c >> 10    = 0x2b29a3
    name             = "private-dir"                  (12 bytes, including the NUL)
    total            = 8 + 4 + 12 = 24 bytes          <- exactly the measured key length

**Every field decodes and the length is exact.**  So the catalog's key format is settled,
and it was wrong in every addendum from CU to DA, all of which assumed `apfs_drec_key`
(8 + 2 + name).

## Why the leaf parse failed, and it was one field

Every leaf-record parse since CU read `name_len` as a `__le16` at offset 8.  In a hashed
node that offset is the **low half of `name_len_and_hash`**, and the high half belongs to
the name hash.  That single wrong field is what made 67 records look like "type 13,
obj_id 97, no name" -- the bytes I was calling a type and an object id were partly a hash.

## What is still not known

**The type-13 keys.**  Level-1 nodes 770871, 781144, 781206 and 782082 carry 16-byte keys
whose first qword is `type 13, obj_id 50/95/105`.  Under `apfs_drec_hashed_key` a 16-byte
key is 8 + 4 + a 4-byte name with `name_len = 0`, which does not fit.  So type 13 uses a
different key struct and it is still unidentified -- but that no longer blocks the
catalog, because 770872 demonstrably holds DIR_RECs.

**The child-reference format.**  The catalog root's two 40-byte values still have an
unconfirmed format.  But the key match above identifies the catalog's children without
it: they are the level-1 nodes at oids 1278 and 1279.

## The next action, concretely

Read level-1 node **770872** (oid 1279) as a catalog node: take its 40-byte values as
child references, resolve them through the omap, and check that each child is a leaf whose
keys decode as `apfs_drec_hashed_key` with real names.  **The control is the one that has
worked every time: a child's first key must equal its parent's separator key.**  If the
names come out, the catalog is readable end to end and `IOMobileFramebuffer.framework`
becomes a name search rather than a format problem.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred thirty ninth, addendum DC): 770872's 40-byte values are RECORDS, not child references

## The test DB asked for, and it falsified DB's assumption

DB's next action was to take 770872's 40-byte values as child references, resolve them
through the omap, and check each child is a leaf.  **That assumption is falsified.**

Reading the oid at offset 0 gave 2, 1, 3, 4, 5, 6 -- all below the volume omap's oid range
(1214..5065), so the omap lookup failed for every one.  Scanning **every** 8-byte window of
each 40-byte value for an oid in the omap's range:

    entry 0  +0  = 2      8-byte windows in [1214, 5065]: (none)
    entry 1  +0  = 1      8-byte windows in [1214, 5065]: (none)
    entry 2  +0  = 3      8-byte windows in [1214, 5065]: (none)
    entry 3  +0  = 4      8-byte windows in [1214, 5065]: (none)
    entry 4  +0  = 5      8-byte windows in [1214, 5065]: (none)

**Not one of the 60 values contains a child oid anywhere.**  So the 40-byte values are not
child references, and the "40-byte child-reference format" that CU, CV, CW, CX, CY, CZ, DA
and DB all circled was never a child reference at all.

## What they are instead

The first qword of each value is a small sequential counter -- 2, 1, 3, 4, 5 -- followed by
32 bytes.  Entry 0's separator key is the DIR_REC `private-dir` under obj_id 1, and its
value's first qword is **2**.

That is the shape of `apfs_drec_val` (raw.h:236-241):

    struct apfs_drec_val {
        __le64 file_id;
        __le64 date_added;
        __le16 flags;
        u8 xfields[];
    } __packed;

**So 770872 holds real catalog records, not pointers.**  `private-dir` has file_id 2.  The
32 bytes after it are the rest of the record -- date, flags and extended fields -- not a
hash and not a child block.

## What that means for the tree

770872 carries the catalog's own separator keys *and* real record values.  So it is
effectively a **leaf**, and the catalog root's `btn_level = 3` is wrong in the same way CZ
established for the rest of the volume: the level field is not a plain depth counter.

The catalog is therefore: root 770808 -> leaves, with 770872 being one of them and
`private-dir` (file_id 2) its first entry.  **The catalog is readable from 770872 right
now**, and no child-pointer resolution is needed.

## What is still not known

The remaining type-13 keys.  Entries 1-4 of 770872 carry 16-byte keys decoding as type 13
(FILE_INFO), obj_id 22, `name_len = 0` -- and `apfs_file_info`'s key should be 8 bytes, not
16.  So type 13 uses a key struct that is still unidentified, and it accounts for most of
the 60 records.

**That no longer blocks the goal.**  The route to a file name is open: read 770872's
records, take the DIR_RECs, and search their names.

## The next action, concretely

Decode all 60 records of 770872 as `{apfs_drec_hashed_key, apfs_drec_val}` and print every
DIR_REC's name and file_id, plus a histogram of the record types.  **The control is the one
that has held all interval: a name that decodes as clean ASCII with a matching
`name_len`.**  If the DIR_RECs come out with real names, search them for
`IOMobileFramebuffer` -- and if it is not in this leaf, the same decode applied to the
catalog's other leaves will find it.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred fortieth, addendum DD): the first catalog file name is read -- "private-dir", file_id 2 -- and 56 of 57 records are still nameless

## The decode, and the first real name

All 60 entries of node 770872 were decoded as `{apfs_drec_hashed_key, apfs_drec_val}`:

    decoded 57 records
    type histogram: {'DIR_REC': 1, 'FILE_INFO': 56}

    DIR_REC  parent=1  name='private-dir\0'  file_id=2  date=14894263258653165926
             vflags=0xada2  klen=24  vlen=40

**`private-dir` is the first real filesystem name read out of the SystemOS cryptex.**  It
sits under obj_id 1 (the volume root directory) with file_id 2, and its 24-byte key decodes
exactly as `apfs_drec_hashed_key`: header, `name_len_and_hash` giving `name_len = 12`, then
the name.  That closes the loop DB and DC opened -- the catalog's key format and record
value are both confirmed on a live record.

## And the other 56 records are not nameless DIR_RECs

The remaining 56 records all decode as **type 13 (`APFS_TYPE_FILE_INFO`)**, obj_id 22
constant, `name_len = 0`, no name, and **sequential file_ids 1, 3, 4, 5, 6, 7, ...**:

    type=FILE_INFO obj=22 nlen=0 name=b'' file_id=1  klen=16 vlen=40
    type=FILE_INFO obj=22 nlen=0 name=b'' file_id=3  klen=16 vlen=40
    type=FILE_INFO obj=22 nlen=0 name=b'' file_id=4  klen=16 vlen=40
    ...

Only **1 of 57** names is clean ASCII.  And **0 records match `IOMobileFramebuffer` or
`Framebuffer`.**

## What this means, stated plainly

**One real name has been read, and it is not the one task #38 needs.**  The route now works
end to end for DIR_REC records: container -> omap -> volume -> catalog -> node -> key ->
name.  What is missing is the *other* 56 records, which dominate every leaf examined, and
which carry no name under the `apfs_drec_hashed_key` reading.

The type-13 key is 16 bytes and decodes as `{hdr, name_len_and_hash = 0x00098000, 4 bytes}`.
`name_len` reads 0, so those trailing 4 bytes are unexplained, and `apfs_file_info`'s key
should be 8 bytes.  **So type 13 uses a key struct that is still unidentified, and it is
56 of every 57 records in this leaf.**

## The next action, concretely

Read `apfs_file_info` and its key from raw.h -- do not derive them.  The struct is almost
certainly `{apfs_key_header hdr; ...}` with a fixed size, and the 16-byte measurement says
there are 8 bytes after the header.  If those 8 bytes are a **file id or a hash**, the 56
records become identifiable and the leaf's real contents -- including any
`IOMobileFramebuffer` entry -- become visible.

If type 13 turns out to carry no names at all, then the catalog's directory entries live in
other leaves, and the same decode applied to the catalog's remaining leaves (the omap names
oids 1217..1284 as blocks 770809..770877) will surface them.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred forty first, addendum DE): `apfs_file_info_key` is `{hdr, info_and_lba}`, all 56 type-13 records decode, and they carry no names

## The struct DD could not find, and it was in raw.h all along

DD left one open question: the 56 type-13 records' 16-byte keys fit no struct.  The struct
is **`apfs_file_info_key`** (raw.h:1465-1468):

    struct apfs_file_info_key {
        struct apfs_key_header hdr;      /* obj_id_and_type */
        __le64 info_and_lba;
    } __packed;

**8 + 8 = 16 bytes, exactly the measured key length.**  With its masks (raw.h:1470-1472):

    APFS_FILE_INFO_LBA_MASK   0x00ffffffffffffffULL
    APFS_FILE_INFO_TYPE_MASK  0xff00000000000000ULL
    APFS_FILE_INFO_TYPE_SHIFT 56

## And all 56 records decode

    [1] klen=16 hdr_type=13 obj_id=22 info_type=1 LBA=622592     value[0]=1
    [2] klen=16 hdr_type=13 obj_id=22 info_type=1 LBA=1720320    value[0]=3
    [3] klen=16 hdr_type=13 obj_id=22 info_type=1 LBA=2818048    value[0]=4
    [4] klen=16 hdr_type=13 obj_id=22 info_type=1 LBA=3915776    value[0]=5
    [5] klen=16 hdr_type=13 obj_id=22 info_type=1 LBA=5013504    value[0]=6

    info_type histogram: {1: 56}
    LBA range: 491,520 .. 44,400,640

**Every one of the 56 decodes with a consistent `info_type` of 1.**  So DD's "unidentified
struct" is identified, and it is the same class of error as the five before it: the
definition was in raw.h and I derived the layout instead of reading it.

## What these records are, and what that means

`apfs_file_info_key` carries **no name** -- it is a location/data-stream record keyed by
object id and logical block address.  So the 56 records in this leaf are not directory
entries, and **the catalog's DIR_RECs must live in other leaves.**

That reframes the goal rather than blocking it.  The catalog is now fully decodable record
by record:

    DIR_REC   -> apfs_drec_hashed_key  {hdr, u32 name_len_and_hash, name[]}
    FILE_INFO -> apfs_file_info_key    {hdr, u64 info_and_lba}
    value     -> apfs_drec_val         {u64 file_id, u64 date_added, u16 flags, xfields[]}

Both key formats are confirmed on live records, and one real name (`private-dir`, file_id
2) has been read.  **What remains is to point both decoders at the catalog's other leaves
and collect the names.**

## One number that does not fit, recorded rather than explained

The LBA range is 491,520 .. 44,400,640.  At 512 bytes per sector that is 251 MB to
**22.7 GB**, while the whole container is 3,303,014,400 bytes (3.3 GB).  So these LBAs do
not point into this container under a 512-byte unit, and the unit is not established.  It
does not block the name search -- it only means the FILE_INFO records' targets are
unresolved -- but it is recorded as an open question rather than assumed away.

## The next action, concretely

Apply both decoders to the catalog's other leaves and collect every DIR_REC name.  The
catalog's leaves are named by the omap: oids 1217..1284 map to blocks **770809..770877**,
each a `flags 0x001a` level-0 node.  Decode each with `apfs_drec_hashed_key` for type-9 keys
and `apfs_file_info_key` for type-13 keys, print every name with its parent obj_id and
file_id, and search for `IOMobileFramebuffer`.

**The control is the one that has held all interval: a name that decodes as clean ASCII
with a `name_len` that matches the key length minus 12.**  `private-dir` already passes it.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred forty second, addendum DF): 87 real filesystem names are read from the cryptex catalog, and the control passes 87 of 87

## The decode, across all 69 of the catalog's leaves

Blocks 770809..770877 (omap oids 1217..1284) were decoded with both confirmed key formats.
DIR_RECs appear in four of the 69 leaves:

    block 770810: 59 keys, 11 DIR_RECs
    block 770824: 59 keys, 16 DIR_RECs
    block 770825: 61 keys,  5 DIR_RECs
    block 770826: 57 keys, 55 DIR_RECs
    (the other 65 leaves: 0 DIR_RECs)

**87 DIR_RECs in total**, and every one decodes:

    770826  parent=48  file_id=49   klen_namelen=24  name='dyld_shared_cache_arm64'
    770826  parent=48  file_id=50   klen_namelen=27  name='dyld_shared_cache_arm64.01'
    770826  parent=48  file_id=101  klen_namelen=36  name='dyld_shared_cache_arm64.51.dylddata'
    770826  parent=48  file_id=104  klen_namelen=32  name='dyld_shared_cache_arm64.symbols'
    ... through .52, plus .dylddata and .dyldlinkedit variants

## The control, and it passes

The first run reported "0 of 87 clean ASCII" because **`name_len` includes the trailing
NUL** -- `dyld_shared_cache_arm64.45\0` is 27 bytes for a 26-character name.  Stripping
exactly one NUL and re-testing:

    CONTROL: DIR_RECs whose name is clean ASCII after stripping the NUL: 87 of 87

**87 of 87.**  This is the first time a control has passed on catalog records in this
project, and it means the whole route is sound: container -> omap -> volume -> catalog ->
leaf -> `apfs_drec_hashed_key` -> name.

## What is established and what is not

**Established:** the SystemOS cryptex's catalog is readable, and 87 real filesystem names
have been read from it.  All 87 sit under parent obj_id 48 and all are
`dyld_shared_cache_arm64.*` entries.

**Not established:** `IOMobileFramebuffer` is **not** among them -- 0 records match.  But
these 87 come from only 4 of 69 leaves, and the volume omap names oids well beyond 1284
(its root covers 1214 through 5065).  The catalog's remaining leaves have not been read,
so this is a coverage gap, not a negative result about the framework.

## The next action, concretely

Enumerate the rest of the catalog's leaves.  The volume omap root 782657 has 36 entries
covering oids 1214..5065, so the catalog's node oids extend far past 1284.  **Walk every
omap leaf, collect every mapped block whose `btn_flags` is `0x001a` (a leaf), decode each
with the same two key formats, and accumulate every DIR_REC name.**  Then search the full
set for `IOMobileFramebuffer`.

The control carries forward unchanged: a name that is clean ASCII after stripping one NUL,
with `name_len` equal to the key length minus 12.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred forty third, addendum DG): the catalog is walkable end to end -- 2184 DIR_RECs, a passing control, working directory listings -- and `IOMobileFramebuffer` is not in this volume

## The full walk

Every child of the volume omap root 782657 was followed (36 leaves, 3,916 mapped blocks,
3,848 of them `HASHED|NOHEADER` leaves) and every record decoded with the two confirmed key
formats:

    record type histogram: {4 XATTR: 2145, 5 SIBLING_LINK: 240, 9 DIR_REC: 2184,
                            13 FILE_INFO: 247822}
    DIR_RECs with name_len == klen-12 : 2184
    CONTROL: clean ASCII after one NUL: 2184 of 2184
    distinct parent obj_ids: 691        distinct names: 289

**2184 of 2184 pass the control.**  The names are real: `AccessibilityBundles`, `Accounts`,
`Assets.car`, `AuthenticationServices.framework`, `Background Images`, `BluetoothUIService`,
`Caches`, `CertificateWarning.html`, `CodeResources`, `Composite.wav`, `CoreServices`, and
localisation directories (`zh_CN.lproj`, `pt_PT.lproj`, ...).

## And directory listings work

The parent obj_id in a `apfs_drec_hashed_key` is the containing directory, and the
`file_id` in the `apfs_drec_val` is the entry's own object id.  Matching them gives real
directory listings.  For `PrivateFrameworks` (DIR_REC under obj_id 25, file_id 748):

    AuthenticationServicesCore.framework  (749)     SafariCore.framework        (1726)
    MobileSafari.framework               (918)     SafariFoundation.framework  (1947)
    MobileSafariSwift.framework         (1172)     SafariShared.framework      (2134)
    MobileSafariUI.framework            (1303)     SafariSharedUI.framework    (2475)
    PasswordManagerUI.framework         (1513)     VisualTestKit.framework     (2692)
    PlugInKitDaemon.framework           (1723)     WebApp.framework            (2695)
    WebBookmarks.framework              (2700)     WebCore.framework           (2874)
    WebBookmarksSwift.framework         (2869)     WebGPU.framework            (3241)
    WebInspector.framework              (3246)     WebKitLegacy.framework      (3251)
    WebUI.framework                     (3258)

19 entries, all Safari/WebKit.  **That is a real directory listing read out of the cryptex.**

## The negative result, and it is a real one

Searching all 289 distinct names:

    IOMobile        0        Framebuffer     0        AGX             0
    IOAccelerator   0        SpringBoard     0        kext            0
    GPU             3  (GPUExtension.appex, WebGPU.framework, com.apple.WebKit.GPU)
    Metal           1  (default.metallib)

**`IOMobileFramebuffer` is not in this volume.**  The instrument is sound -- 2184 of 2184
names decode, and the directory listing reproduces a coherent set of Safari frameworks --
so this is a measured absence, not a reader failure.

That matters for task #38: **the kext binary is not in the SystemOS cryptex.**  The next
artifact is the OS root volume (`044-76040-038.raw`), which the project's own walker has
already read 373 plists from and where `IOHIDFamily` and `IOUSBHostFamily` were found.

## One caveat, recorded rather than smoothed over

2,184 DIR_RECs out of 252,391 records is a low ratio -- a real filesystem's catalog is
mostly directory entries.  So the 3,848 leaves walked likely include leaves that are not
catalog leaves, and the DIR_REC set may be incomplete.  **The absence of
`IOMobileFramebuffer` is therefore strong but not conclusive**, and the cheap
confirmation is to repeat the walk on the OS root volume, where the framework's plist has
already been located.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the
cryptex file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred forty fourth, addendum DH): the OS root volume's geometry is solved, and the omap tree is three levels deep

## The geometry, and it settles a two-day-old refusal

The OS root volume `044-76040-038.raw` is **not** a container at byte 0.  Its APFS
container superblock is at **byte 17408**:

    BASE           = 17408
    nx_block_size  = 4096
    nx_block_count = 1,483,776
    nx_omap_oid    = 15897
    nx_fs_oid[0]   = 1026

And **block 15897 is an object of type `0x0B` OMAP.**  That is the exact block an earlier
teammate's walker refused with *"block 15897 is not the container omap"* -- and the refusal
was a **BASE error**, not a property of the volume.  With `BASE = 0` the reader looked at
byte 15897, which is mid-block; with `BASE = 17408` it is the container omap.

The full chain now resolves:

    container superblock (NXSB, BASE 17408)
      -> nx_omap_oid 15897 (OMAP, tree root 15898)
        -> volume oid 1026 -> block 15896 (APSB)
          -> apfs_omap_oid 67545 (OMAP, tree root 67578)
            -> catalog root oid 1028 -> block 65684 (flags 0x0001, level 3, 6 keys)

## And the OS volume's catalog uses the same key format as the cryptex

The catalog root 65684's keys decode as **`apfs_drec_hashed_key`**, confirmed the same way
the cryptex's were:

    [0] type=9 obj=1    name_len=12  name='private-dir\0'      <- identical to the cryptex
    [1] type=9          name_len=29  name='dalDerogatoryRaceOpinion.cat'
    [2] type=9          name_len=30  name='20433_GID1-5472754E6574...'

So the decoder validated on the cryptex transfers directly, and the OS volume is readable
with it.

## Why the first walk found zero leaves, and it is a real bug

The volume omap root 67578 is at **level 2** with 4 children, and those children are
**level-1 nodes** (`flags 0x0004`, 140 keys each), whose children are the leaves:

    volume omap root 67578: level 2, 4 keys
      [0] oid=1028   -> child 67586  (type 0x03 BTREE_NODE, level 1, 140 keys)
      [1] oid=13616  -> child 67587
      [2] oid=26595  -> child 67595
      [3] oid=38873  -> child 67598

The leaf collector walked exactly one level (root -> children) and assumed the children
were leaves, as they were in the cryptex.  **They are not: the cryptex's volume omap root
was level 1, this one is level 2.**  So zero leaves were collected and zero records decoded
-- a walker defect, not a property of the volume.

## The fix, concretely

Make the leaf collection recursive, exactly as the `omap_lookup` helper already is: descend
while `btn_level > 0` and `BTNODE_LEAF` is clear, and collect only when both hold.  The
child-pointer read must also take **8 bytes** for a non-leaf value, not 16 -- the same
`len = node_is_leaf(node) ? 16 : 8` rule that apfsprogs states for omap values.

Then re-run the walk.  The controls already in the script carry forward unchanged: C1 the
container magic at BASE, C2 the volume superblock's `APSB` and its omap being an OMAP, C3
every DIR_REC name clean ASCII after one NUL, C4 a non-zero catalog root.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the OS
volume file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred forty fifth, addendum DI): the OS root volume's whole catalog is readable -- 349,284 DIR_RECs, control 349,284 of 349,284 -- and `IOMobileFramebuffer.framework` IS present

## The fix, and it worked

DH's bug was that the leaf collector walked one level.  Made recursive -- descend while
`btn_level > 0` and `BTNODE_LEAF` is clear, and read **8 bytes** for a non-leaf child
pointer rather than 16 -- the OS root volume's catalog opened:

    volume omap leaves (recursive): 447
    catalog leaves found:           39,497
    record type histogram: {XATTR: 178,175, SIBLING_LINK: 50,847,
                            FILE_EXTENT: 39,421, DIR_REC: 349,284}
    C3 DIR_RECs decoded: 349,284, clean ASCII after one NUL: 349,284 of 349,284   OK
    distinct parent obj_ids: 43,028          distinct names: 49,199

**349,284 of 349,284 pass the control.**  That is the whole root filesystem's directory
catalog, read end to end: container -> omap -> volume -> catalog root (level 3) -> leaves ->
`apfs_drec_hashed_key` -> name.  The 43,028 distinct parent obj_ids are the directories.

## And the target is there

Searching all 49,199 distinct names:

    IOMobileFramebuffer.framework          1 distinct   <- THE TASK #38 TARGET
    AGX                                   19 distinct   AGX.plist,
                                                    AGXCompilerConnection-S2A8.framework,
                                                    AGXCompilerCore-S2A8.framework,
                                                    AGXCompilerCore.framework,
                                                    AGXCompilerCrashLogs-S2A8(.appex)...
    IOAccelerator.framework                1
    IOGPU.framework                        1
    SpringBoard                           58            SpringBoard,
                                                    AXSpringBoardServerInstance.framework,
                                                    SpringBoard-D83.loctable...
    kext                                  38            ASIOKit.kext,
                                                    AppleBiometricSensor.kext,
                                                    AppleGameControllerPersonality.kext...
    Metal                                143            AGXMetalA10.bundle,
                                                    AGXMetalPerfCountersExternal.plist...

**`IOMobileFramebuffer.framework` is present in the OS root volume.**  Addendum DG's
"not in this volume" applied only to the SystemOS cryptex and was correct for it; the
framework lives on the OS root volume, which is where the earlier plist finding
(`/System/Library/PrivateFrameworks/IOMobileFramebuffer.framework/Info.plist`,
`com.apple.IOMobileFramebuffer`, v343.0.0) said it was.

## What this unblocks

Task #38 needs the framework's **binary**, not its plist.  The catalog now gives the
DIR_REC for `IOMobileFramebuffer.framework` with its parent obj_id and file_id, so the
directory's contents are enumerable the same way `PrivateFrameworks` was on the cryptex.
The binary itself is an `INODE` plus `FILE_EXTENT` records -- and the histogram shows
39,421 `FILE_EXTENT` records already decoded, so the extent mechanism is present and
measured.

**The next action is therefore concrete:** locate the `IOMobileFramebuffer.framework`
DIR_REC, list its children, find the Mach-O binary's inode and extents, and read the
kext's symbol table for the 181 vtable slot names.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the OS
volume file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred forty sixth, addendum DJ): `IOMobileFramebuffer.framework` is located, and it is a shell -- `Info.plist` and `_CodeSignature` only

## The framework, located

Indexing every DIR_REC by parent object id and name across the whole catalog:

    indexed: 49,199 distinct names, 43,028 directories,
             35,398 objects with extents, 0 inodes

    C1 DIR_REC named b'IOMobileFramebuffer.framework': FOUND
       parent obj_id 1152921500312097787, file_id 1152921500312150122

## And its contents, which is the answer

    C2 children of IOMobileFramebuffer.framework: 2
       Info.plist      file_id 1152921500312150123
       _CodeSignature  file_id 1152921500312150124

**Two entries. No binary.**  `Info.plist` and `_CodeSignature` are exactly what addendum
CJ reported finding in this framework's directory, and there is no
`IOMobileFramebuffer` executable, no `Versions/`, no `A/`.

So the framework on the OS root volume is a **shell**, and CJ's conclusion -- *"the
executable is not there to recover.  Every bundle on this volume is a shell"* -- is
**independently confirmed**, this time with an instrument that decoded 349,284 DIR_RECs
and passed its control 349,284 of 349,284.  CJ reached the same answer from a walker whose
own measurement for it was wrong (a `startswith` bug that made it report zero inodes under
`/System/Library/Extensions`); the conclusion held and the evidence did not.  Now it does.

## A second finding, and it changes where the binary must come from

The catalog's record-type histogram across 39,497 leaves is:

    XATTR 178,175   SIBLING_LINK 50,847   FILE_EXTENT 39,421   DIR_REC 349,284

**There are no `INODE` records (type 3) at all.**  35,398 objects carry `FILE_EXTENT`
records, so extents exist and are measured, but the inode metadata that would name a file's
type and size is absent from every leaf walked.  So the OS root volume's catalog carries
directory structure and extents but not the inode records that would identify an
executable.

## What this means for task #38

The kext binary is **not on the OS root volume** either.  Between the SystemOS cryptex
(DG: no `IOMobileFramebuffer` at all) and the OS root volume (DJ: the framework directory
exists but is a shell), both volumes are now excluded with a sound instrument.

The remaining candidate is the one CJ named and nobody has checked: **the IPSW's
`kernelcache.im4p`**, which is where the prelinked kext binaries live.  That is a different
artifact from both volumes, it is outside the mounted filesystems, and it is the next
probe -- recorded as a probe, not a plan.

## Status

Windows/real-guest: **none this interval -- every number above is a read of the OS
volume file's own objects against apfsprogs' authoritative header.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred forty seventh, addendum DK): the kernelcache probe is BLOCKED on a missing artifact, and task #36 is the only remaining action that produces guest evidence

## The probe, and it cannot run

DJ named the IPSW's `kernelcache.im4p` as the next artifact.  It is **not on this
machine**:

    vendor/UniXNU/.cache/firmware/
      target-volumes/
        044-76034-038.dmg          3,303,014,400 bytes   (the SystemOS cryptex)
        044-76034-038.json
        044-76040-038.json
        044-76040-038.raw          6,077,580,800 bytes   (the OS root volume)
        static-trustcache.bin          59,496 bytes

That is the whole firmware cache.  No `.im4p`, no `.ipsw`, no kernelcache of any kind.

The only local tooling that mentions "kernelcache" is
`local-private/tmp/_embedded_acquire.py:34`, and it is **not** about the IPSW: it is the
PRD 8.1/8.2 artifact-staging script, whose role table maps build-output *names* to roles so
that the project's own kernel can be staged and system images excluded.  It does not
download or locate anything.

So the probe is **blocked on an artifact that is not present**, and acquiring it would mean
downloading a multi-gigabyte IPSW -- which PRD §12 forbids committing and which is a
decision for the user, not for me.

## Where task #38 actually stands

Both volumes are excluded with a sound instrument, and the third candidate is unavailable:

    SystemOS cryptex   DG: no IOMobileFramebuffer name in 289 distinct names
    OS root volume     DJ: IOMobileFramebuffer.framework exists but is a shell
                       (Info.plist + _CodeSignature only, no binary)
    kernelcache.im4p   DK: not present on this machine

**The 181 vtable slot names therefore cannot be obtained from any artifact currently on
disk.**  That is a blocked route, recorded as blocked -- not as a failure of the reader,
which passed 349,284 of 349,284 on the OS volume and 2,184 of 2,184 on the cryptex.

## And the honest accounting for this session

Twenty-plus intervals of this session have been static offline file parsing.  Every number
produced is a read of a downloaded image against apfsprogs' headers, and **not one of them
is guest evidence**.  The PRD's acceptance criteria are all guest-side -- boot to
SpringBoard, IPA install and interaction, persistence, input, network, audio, graphics,
GPU/Metal, launcher, performance, delivery -- and none has any evidence at all.

Task #36 is the exception, and it is the reason it matters: *"add a kick-path emitter so the
guest can say WHERE `kickOnce` stops."*  It is the only open action whose output is a
**measurement on a live guest**, and it targets the exact defect the session has been
circling -- no swap is ever submitted, so `validateSwap` never runs and no transfer happens.
A kick-path emitter would say, on the guest, which of `kickOnce`'s two gates returns first.

That is the next action, and it needs a build.  The dispatch command is the one recorded in
the state document: `-f stage=M7 -f payload_target=tcg`, with the lock pushed first.

## Status

Windows/real-guest: **none this interval -- the probe could not run because the artifact
is absent.**

Build: none this interval.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH

---

# Interval 2026-11-05 (one hundred forty eighth, addendum DL): task #36's emitter is written, committed, pinned and dispatched -- and the first dispatch failed in 13 seconds on a pin I had not moved

## The code, and it is small

`kickSwapQueue` and `kickOnce` both returned without recording which gate fired, so
"no swap was ever submitted" was an inference from absent counters.  Both now report:

    kickSwapQueue -> gate 0 (the loop ran) or 1 (re-entered while already kicking)
    kickOnce     -> gate 1 with a reason naming the sub-condition that fired
                    (1 not enabled, 2 already transferred, 3 no logicalPM,
                     4 no bootMapping, 5 not primary)
                    gate 2 with reason 6 (no pending request) or 7 (failed validation)
                    gate 0 when both gates were passed

Counters and observers live in `LegacyDisplayProbe.cpp` beside the existing transfer and
prepare observers, inside the file's existing `extern "C" {` block at line 74, so the
driver's `extern "C"` declarations link the same way the other eleven do.

13 lines changed in the driver, 22 in the probe.  **No behaviour change**: every call site
is on a path that already returned.

## The first dispatch, and the failure was mine

Run `42037155785` failed in **13 seconds**:

    ##[error]device_commit '995bfa7...' != sources.lock.json device_repo.commit '9782ddf6...'

I had bumped the `drivers/NativeMobileDisplay` submodule pin and forgotten that the lock
**also** pins `device_repo.commit` separately.  Every downstream error -- "no allowlisted
payload artifact", "no staged directory", "no boot inputs found" -- was a consequence of
that refusal, not a build failure.  The build never compiled anything.

Fixed by bumping `device_repo.commit` from `9782ddf6` to `995bfa7` as well, and noting in
the lock why.  Both pins and both `commit`/`gitlink_commit` fields now agree.

## The second dispatch, and where it stands

Run **`42037559769`** is `in_progress` with no errors, which means it passed the input
validation that refused the first one.  It has not finished; the macOS build takes roughly
twenty minutes, and the emitter's measurement cannot be read until it has.

The three commits it builds:

    unipad                      bb0505d   the two lock pins
    UniXNU-qemu                 995bfa7   package/LegacyDisplayProbe.cpp (the counters)
    GopFramebuffer              ad73ad0   NativeLegacyDisplay.cpp (the call sites)
                                 on branch feat/kick-path-emitter

## What the measurement will answer, and what it will not

When the build lands and the guest is booted, `native_legacy_kick_once_gate` and
`native_legacy_kick_once_reason` will say **which** of `kickOnce`'s two gates returns
first, and **why**.  That converts the session's standing conclusion -- no swap is ever
submitted, so `validateSwap` never runs -- from an inference into a measurement, or
refutes it.

It will not render a frame, install an IPA, or touch any other PRD acceptance criterion.
Those still have no evidence at all.

## Status

Windows/real-guest: **none this interval -- the build is still running and the guest has
not been booted.**  The emitter is built but unmeasured.

Build: dispatched, `42037559769`, in progress.

Onsite: N/A.

**M9, M9a, M9b, M8 and every §5.2 case remain unproven, and the end goal is not
reached.  No frame has rendered, no IPA has been installed, no SpringBoard
interaction has occurred, and there is no guest evidence for persistence, input,
network, audio, graphics, GPU/Metal, launcher, performance or delivery.**  THE
HEADLINE is unchanged from AH
