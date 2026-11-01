# Current state — task, evidence, next actions, resume commands

**This file is the working state document.** It is updated as work proceeds, and
it is the first thing to read after a context compaction or a session restart.
The numbered facts behind every claim here live in
[`implementation-plan.md`](implementation-plan.md); this file says where things
stand and what to run next, not why.

Last updated: 2026-11-01 (fact 225). Main repo at `51b7e04`.

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

**Where the XNU records stand:** pinned at `c3c9ce2`/`2bff001`/`51b7e04`, and
run 41281382063 concluded **success** — so all ten records survive `-Werror` and
the ARM64/iOS 17 object audit on the macOS runner, which is the only real
compiler that has ever seen them (fact 225). The boot-inputs bundle is
downloading; once it lands, the byte-level ESP-kernel check and the guest
capture are the only two steps left before M4 and M5 can be re-read.

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

## What is NOT done, and why

| # | Gap | Why it is still open |
|---|---|---|
| A | **No guest-side consumer in the kernel.** `NativeOfflineGPU` is the only code that reads the console port, and it is source-only | fact 219 — `XNU_NATIVE_OFFLINE_GPU` defaults to 0 in `build-xnu-4k-baseline.sh:306`; `board.toml`'s `[kernel]` section has only `defconfig` and `archives`; all ten `native_gpu_broker_*` symbols are absent from the kernel |
| B | **No guest agent for install/launch/persist.** The host's `protocol.py` has all six ops; nothing in the guest reads them | `grep -rln native_console_receive` over `vendor/UniXNU/` returns only `NativeOfflineGPU.cpp` and the console driver itself |
| C | **The host→guest arrival question is unmeasured** | fact 218 — the RX counters count pulls, not arrivals; the arrival round read the wrong register offsets (the queue rings are at 0x80/0x90/0xa0, not 0x30-0x44) |
| D | **No real IPA has been installed, launched or persisted** | Blocked by A and B |
| E | **No SpringBoard interaction evidence** | Blocked by D |
| F | **No audio, network, launcher or performance acceptance** | Not started |
| G | **The three XNU records are pinned, compiled and measured on paper, not yet on a guest** | fact 225 — run 41281382063 is **success** (34/34 steps), so the macro survives `-Werror` and the ARM64/iOS 17 object audit. What is still open is the guest side: byte-level presence in the ESP kernel, then the capture, then the gate |
| H | **Nothing else stands between these records and M4+M5** | fact 224 — measured by running the gate's own probes: M5's `missing` is empty, M4's `missing` is exactly the three XNU requirements, and `p.el1_entry` / `p.failure_forensics` / `p.unixnu_kernel_image` / `p.source_pins` are all already True. **Corrected by fact 226**: it is not just M4+M5 — `xnu_initialised` also blocks M6, M7, M8, M9, M9a and M9b, so the same three records move eight milestones |

## Active work

**The XNU emitter is pinned, compiled and downloading.** Run 41281382063
concluded **success**, so the driver with all ten records survived `-Werror` on
the macOS runner — the first real compiler to see the macro, and this host has
none. The boot-inputs bundle is on disk at ~5% and growing at ~39 KB/s (about
two hours ten minutes); the pre-signed URL expires mid-transfer, so the
downloader resumes with a fresh one per attempt.

The two IOKit records already cleared their own gate (measured on a real guest,
below), but M5 is still BLOCKED on `xnu_initialised`, which needs three more
guest events. The console driver is the only code in the guest that can put
bytes on that port, so the same fork carries them.

`start()` already reads `SCTLR_EL1` at its first line, so the page-table fact
was free. `VBAR_EL1`, `DAIF` and `CNTFRQ_EL0` are read the same way, right
after the IOKit records — inside IOKit, long past XNU's early architecture
setup. So the three records measure XNU's live state; they are not inferences
from the fact that this driver is running.

Two static strings per fact and a ternary, not `snprintf`: the driver builds
with `-Werror` into a kernel, and guessing wrong about which header declares
`snprintf` costs a 40-minute build to discover. An earlier draft also wrote
`"vbar_el1":0` into a literal while holding the real value in a variable,
which would have put a false statement into the very evidence the gate reads.

Four static combinations for the interrupt fact, not two records, because the
gate keeps only the FIRST record per event (`_guest_events` uses `setdefault`)
— a second `xnu.interrupts` record would be silently dropped, and a reader of
the log would never see the timer.

| Level | Repo | Branch | Commit |
|---|---|---|---|
| driver fork | `James081105/VirtioMmioConsole` | `unipad/console-announcement` | `c3c9ce2` |
| UniXNU fork | `James081105/UniXNU` | `unipad/gpu-addon-ngpu-dir` | `2bff001` |
| main repo | `James081105/unipad` | `main` | `51b7e04` — both lock entries moved |

Build **run 41281382063** dispatched with experiment
`exp-2026-11-01-console-xnu-records`, stage M5, system build 21H461, payload
target tcg. **It concluded success** (34/34 steps; fact 225) — step 14's macOS
build is the first real compiler to see the macro, and `-Werror` let it through.

## Next actions, in order

1. **Download `boot-inputs` from run 41281382063** — artifact id `13074462047`,
   310,354,885 bytes, already running into `local-private/tmp/bundle15.zip` via
   `local-private/tmp/fetch-artifact.py` (a fresh URL per attempt — the
   pre-signed one expires mid-transfer and `curl` exits 0 on the truncated
   result). Expect ~2h10m at ~39 KB/s.
2. **Verify byte-level** that the three XNU records are in the ESP kernel — the
   same check that proved the announcement (fact 217) and the two IOKit records
   (fact 223). `local-private/tmp/verify-records-in-kernel.py` already does this
   for all five events and refuses to report unless the ESP kernel's sha256
   equals `payload.json`'s `payload_kernel_sha256`. It was validated against a
   known answer (bundle14: two present, three correctly absent) after an earlier
   version reported a false negative.
3. **Run `local-private/tmp/iokit-capture.py`** against that bundle. It boots,
   waits for the hand-off, captures the channel into
   `local-private/boot40/iokit-records/guest.log`, and parses it with the gate's
   own criterion. Then read the three XNU events, not just the two IOKit ones.
4. **Re-run the gate** and read M4 together with M5-M9b. Fact 226 corrected the
   baseline: `xnu_initialised` gates **seven** milestones (M5, M6, M7, M8, M9,
   M9a, M9b), not two — so the same capture moves eight. Re-measure rather than
   assume — `evaluate(mid)` on `default_view()` answers in seconds, and
   `unmet_blockers` prints which one is still red. **Unblocked is not passed**:
   M6-M9b each still need their own `pass_evidence` from the guest.
5. **Then the broker, if the render path is next.** That is the Part A + Part B
   + Part C change fact 221 describes, and it is a device-repo re-pin. It is not
   on M5's path.
6. **In parallel, `guest_install_service`** — the blocker for M10/M11/M12, and
   gap B (no guest agent for install/launch/persist). It is independent of
   `xnu_initialised`, so it does not have to wait for step 4.

## Resume commands

Everything below runs from `F:\iosvirtual`. `bundle13` is the boot-inputs bundle
whose kernel carries the announcement; it is already extracted at
`local-private/tmp/bundle13`.

```bash
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
