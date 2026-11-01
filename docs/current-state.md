# Current state — task, evidence, next actions, resume commands

**This file is the working state document.** It is updated as work proceeds, and
it is the first thing to read after a context compaction or a session restart.
The numbered facts behind every claim here live in
[`implementation-plan.md`](implementation-plan.md); this file says where things
stand and what to run next, not why.

Last updated: 2026-11-01 (fact 227). Main repo at `51b7e04`.

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
compiler that has ever seen them (fact 225). And they are now **byte-proven to
be inside the kernel the guest boots**: the ESP kernel's sha256 equals
`payload.json`'s `payload_kernel_sha256` exactly, and all five literals are
present at offsets and occurrence counts the driver's own source predicts
(fact 227). The only step left is the guest capture, which is waiting on
`payload.img` — 47% of that member is already assembled.

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
| 16 | **All five records are in the bytes of the kernel the guest boots** | fact 227 — artifact `13074462047`'s ESP kernel, 125,480,960 bytes, sha256 `359eba3f…` **equal** to `payload.json`'s `payload_kernel_sha256`; `iokit.service_match` at 242932 and `iokit.driver_probe` at 243047 (1 each), `xnu.page_tables` at 243159 (1), `xnu.exception_vectors` at 243244 (**2**), `xnu.interrupts` at 243439 (**4**). The instrument refuses to report unless the two hashes agree. The counts were predicted from the source first: two static strings per fact, and four DAIF × CNTFRQ combinations because the gate keeps only the first record per event |
| 17 | **The kernel was obtained without transferring 310 MB** | fact 227 — the member is 78,650,816 bytes DEFLATED over `[2,354,995, 81,005,811)`, a range the partial straight download already covered, so extraction moved `fetched 0 bytes`. The extractor was validated offline first against `bundle14.zip` (a complete zip whose ESP kernel is already on disk), reproducing that known kernel byte for byte; its first live run **refused**, correctly, because it had been handed one artifact's bytes against another's central directory |

## What is NOT done, and why

| # | Gap | Why it is still open |
|---|---|---|
| A | **No guest-side consumer in the kernel.** `NativeOfflineGPU` is the only code that reads the console port, and it is source-only | fact 219 — `XNU_NATIVE_OFFLINE_GPU` defaults to 0 in `build-xnu-4k-baseline.sh:306`; `board.toml`'s `[kernel]` section has only `defconfig` and `archives`; all ten `native_gpu_broker_*` symbols are absent from the kernel |
| B | **No guest agent for install/launch/persist.** The host's `protocol.py` has all six ops; nothing in the guest reads them | `grep -rln native_console_receive` over `vendor/UniXNU/` returns only `NativeOfflineGPU.cpp` and the console driver itself |
| C | **The host→guest arrival question is unmeasured** | fact 218 — the RX counters count pulls, not arrivals; the arrival round read the wrong register offsets (the queue rings are at 0x80/0x90/0xa0, not 0x30-0x44) |
| D | **No real IPA has been installed, launched or persisted** | Blocked by A and B |
| E | **No SpringBoard interaction evidence** | Blocked by D |
| F | **No audio, network, launcher or performance acceptance** | Not started |
| G | **The three XNU records are pinned, compiled and now byte-proven, but not yet seen on a guest** | fact 225 — run 41281382063 is **success** (34/34 steps), so the macro survives `-Werror` and the ARM64/iOS 17 object audit. Fact 227 — the records are in the ESP kernel's bytes, at the offsets and counts the source predicts. What is still open is the guest side only: `payload.img` (47% assembled), then the capture, then the gate |
| H | **Nothing else stands between these records and M4+M5** | fact 224 — measured by running the gate's own probes: M5's `missing` is empty, M4's `missing` is exactly the three XNU requirements, and `p.el1_entry` / `p.failure_forensics` / `p.unixnu_kernel_image` / `p.source_pins` are all already True. **Corrected by fact 226**: it is not just M4+M5 — `xnu_initialised` also blocks M6, M7, M8, M9, M9a and M9b, so the same three records move eight milestones |

## Active work

**The XNU emitter is pinned, compiled and byte-proven.** Run 41281382063
concluded **success**, so the driver with all ten records survived `-Werror` on
the macOS runner — the first real compiler to see the macro, and this host has
none. The records are then **in the kernel the guest boots**: fact 227 ties the
ESP kernel's sha256 to `payload.json`'s `payload_kernel_sha256` and finds all
five literals at the offsets and counts the source predicts.

**The 310 MB download was the wrong shape for this job.** The pre-signed URL is
link-bound at ~26-30 KB/s and dies after ~1.5 MB, so a straight serial download
is a three-hour job — and the only file needed to prove the records are in the
kernel is 79 MB DEFLATED at offset 2.35 MB, a range the partial download had
already covered. `local-private/tmp/extract-one-member.py` pulls one member by
byte range instead: it cost **zero** new bytes for the kernel and 688 bytes for
`payload.json`. The same trick is now assembling `payload.img` (92.6 MB
compressed, 46% reused from the partial), which the guest capture needs for the
data ramdisk — without it the guest sits in the boot menu repeating
`cannot read initrd ... scratch-data.img: Not Found` while every liveness signal
still says "alive" (measured on bundle12).

**A note on the proxy.** The user's proxy at `127.0.0.1:7897` works fine for
`api.github.com`, `github.com` and `objects.githubusercontent.com`, but its
upstream **refuses `*.blob.core.windows.net`** — verified three ways: `curl -v`
shows the CONNECT tunnel establish (200) and then the TLS handshake inside it
fail; `-k` changes nothing; plain HTTP returns 502. So the proxy cannot be
applied to this artifact, and the range-extraction method above is what replaced
it.

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

1. **Finish `payload.img`** — running now via
   `local-private/tmp/extract-one-member.py` into `local-private/tmp/bundle15/`
   (92,633,980 bytes compressed over `[81,007,447, 173,641,427)`; 46% reused
   from the partial, ~48 MB to go). Alongside it, `kernel.macho.gpuaddon`
   (12,048,506 bytes at offset 173,642,189). Log:
   `local-private/tmp/extract-payloadimg.log`. The four small members
   (`UniXNU.efi`, `unixnu.toml`, `kernel.macho.adt`, `DarwinBoot.efi`) are
   **already staged** and `kernel.macho` is extracted and verified.
2. **Byte-level presence is DONE** (fact 227). All five records are in the ESP
   kernel, sha256 tied to `payload.json`, counts matching the source's
   prediction. Evidence file:
   `local-private/tmp/xnu-records-in-kernel-evidence.txt`.
3. **Run `local-private/tmp/iokit-capture.py local-private/tmp/bundle15`** — it
   boots, waits for the hand-off, captures the channel into
   `local-private/boot41/xnu-records/guest.log` (a fresh path, deliberately, and
   it refuses to overwrite), and parses it with the gate's own criterion. Then
   read the three XNU events, not just the two IOKit ones.
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
# Byte-level proof the records are in the kernel the guest boots.
python -u local-private/tmp/verify-records-in-kernel.py local-private/tmp/bundle15-check

# Pull ONE member out of the artifact by byte range (no 310 MB download).
python -u local-private/tmp/extract-one-member.py local-private/tmp/bundle15.zip \
  "payload-esp/EFI/UniXNU/Payloads/kernel.macho" local-private/tmp/b15-kernel.macho

# ...and validate the extractor offline against a known answer first.
python -u local-private/tmp/validate-extractor.py

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
