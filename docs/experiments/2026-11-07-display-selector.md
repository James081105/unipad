# Display selector correction, 2026-11-07

The Windows capture proves that backboardd opens the original framebuffer user
client but never calls the actual swap submission. The previous implementation
misidentified selector 17 / slot 200 as that submission. It is a gamma-table
operation. This correction does not establish an interactive desktop or M9 PASS.

## Matched capture

Build [42234948282](https://github.com/James081105/unipad/actions/runs/42234948282)
uses main `ecc9f0e531fdd9356ccf355a858b0ce29a02c666`, UniXNU
`953a5e57b8a098dd42eaf2abacc05dc1c5ad7037`, device
`1865bd98beeaf25789c28dd2ce1077c4b1947ea5`, and locked display driver
`8d7ad6fd9626e180598d133227033c66b3d1f244`. The build repins module submodules
through `unipad.host.source_pins`; the driver's gitlink `5b83ab7` is not the
effective source. Runtime kernel SHA-256:
`0533abe5339879ba2691560b350f861b7315b380dbb0a104bd04c5ddf4c2c9d3`.

Command, from the repository root:

```powershell
$env:TMP='F:/iosvirtual/local-private/tmp'
$env:TEMP=$env:TMP
$env:TMPDIR=$env:TMP
python -u local-private/tmp/capture-m9-client-trace.py
```

The 1,800-second capture exited 0. Evidence is retained at
`local-private/boot-m8-42234948282/display-client-trace/`, with a reopened,
hash-linked `summary.json`. Guest and streamed logs agree at 25,240 bytes.
The first sample at 608 s has zero clients. The later samples at 1,209 and
1,761 s have four clients and unchanged selector counters. PID 86
IOMFB_FDR_Loader disappears from the process table by the last sample; the
retained client record is historical. launchd and SpringBoard remain present.

The decoder matches the exact device source, entry stride 8,272 bytes, bounds
the count to eight, checks kernel pointers, and verifies both call/failure
totals against their per-selector sums. Raw client tables are retained beside
the decoded JSON. Duplicate QEMU evidence was moved to
`E:/UniPadEvidence/2026-11-07-m8/display-client-trace-qemu-evidence`; original
logs and the original QEMU scratch remain in place.

backboardd PID 58 made 16 calls, five failures, and no notification registrations.
Selector 17 was called once with a 3,084-byte structure and returned `0x2`.
Selectors 3/4/5 were never called. Selector 6, the wait operation, succeeded
once. Swap validation and scanout preparation both remain zero; the last
kick reason is still no pending request. Screenshots remain the bootloader.

## Exact artifact evidence

The following reads come from the matched kernel file using Mach-O segment
mapping and Capstone 5.0.7. Static disassembly establishes dispatch and return
behavior; the client trace establishes which paths ran.

| Operation | Exact original artifact evidence |
| --- | --- |
| Selector 17 | Row `0xfffffff005f35658`: function `0xfffffff00577a0c8`, no scalars, 3,084 input bytes, no output |
| Selector 17 helper | `0xfffffff005778040` loads provider `+0xd8`, forwards the raw input pointer, calls vtable `+0x640` = slot 200, and returns `w0` |
| Current slot 200 override | `0xfffffff00bf68094`: loads the virtual at `+0x910` = slot 290 and tail-branches to it; no defined IOReturn |
| Selector 5 | Row `0xfffffff005f35538`: function `0xfffffff005779470`, no scalars, 1,288 input bytes |
| Selector 5 helper | `0xfffffff0057772d4` calls provider vtable `+0x590` = slot 178, passing record, client, and zero |
| Actual slot 178 | `0xfffffff00578f224` references the original log string `swap_submit` at `0xfffffff0045fca0e` |

Independent historical interface corroboration is pinned at
[iokitstuff 133752a](https://github.com/userlandkernel/iokitstuff/blob/133752a6ce26088b4a5770d46b7d11e718d2d100/Zeroday/iomobileframebuffer.h):
SetGammaTable is selector 17, GetGammaTable is selector 27, and the structure
contains three arrays of 257 uint32 values, exactly 3,084 bytes. The same
header names SwapBegin=4, SwapEnd=5 and Wait=6. Its implementation passes the
gamma structure directly to selector 17. This older interface corroborates
the exact-device binary; it is not a substitute for that binary.

The earlier inference that any structure-bearing selector must submit a swap
is withdrawn. No queue insertion or request conversion belongs in slot 200.
The original slot 178 already owns the real submission path. Client
initialization before SwapBegin/SwapEnd is the remaining runtime question.

## Correction and checks

- Driver `11af5c627a0d90680c2fc9f62bf6dd7957f0c741` removes the false void
  override and preserves the inherited gamma method.
- Device `d4541e88c32f4c89b3d60dac1e6e88923e04ef86` removes slot 200 from the
  owned set and generated declaration. Four exact original-artifact checks
  guard the two dispatch rows and their distinct provider calls.
- The generated virtual count remains 190. Four independently corrupted
  artifact reads are rejected. Six regression tests pass against the corrected
  device script, including the unchanged original-family control.
- A local Clang syntax check rejects the actual removed void override against
  the observed IOReturn caller contract. The corrected inherited path passes.
  This is a focused ABI check; it does not replace a macOS kernel build.
- Preceding Windows fixture commit `b60f7a9` has a successful full CI run
  [42262403716](https://github.com/James081105/unipad/actions/runs/42262403716).

Both source edits have four reopened transaction artifacts at
`.scratch/prd-transaction/display-selector-{driver,device}/`:
`MODIFIED_FILE`, `DIFF_FILE`, `VERIFICATION.txt`, executable `ROLLBACK.sh`.
Each record contains exact baseline, modified, rollback and restored commands,
literal outputs, statuses and hashes. Rollback was tested on another copy;
the modified files remain changed.

## Build follow-through and next capture

Main `2db23e6d8b2d0a91fb2bfcfaf80e42c36c874c29` is pushed. All 17 selected
selector-contract/submodule-acquisition/repin checks pass. Its full Windows
preflight and pinned integration run
[42267182279](https://github.com/James081105/unipad/actions/runs/42267182279)
also passes. The complete CI log is retained at
`E:/UniPadEvidence/2026-11-07-m8/ci-42267182279.log`.

Build [42267229495](https://github.com/James081105/unipad/actions/runs/42267229495)
was dispatched from that exact main commit with stage=M7, payload_target=tcg,
gpu_broker=0 and the pinned UniXNU/device commits above. The full build now
concludes **success**. Boot-inputs artifact `13533046166` contains 310,401,057
bytes; the downloaded ZIP SHA-256 matches the upload log:
`428c8a3b64a9233211bee42772afa311f775e1320be407f0fb17508aa28eb670`.
All ten selected bundle members were staged, and every declared manifest and
payload digest agrees. The full build and staging logs are preserved at
`E:/UniPadEvidence/2026-11-07-m8/build-42267229495.log` and
`E:/UniPadEvidence/2026-11-07-m8/stage-42267229495.log`.

The actual new payload kernel SHA-256 is
`791a592634f413560e89585d751eb2bd99b83c1eb27bf6c40e222d14426c1360`.
Its compiled slot 200 equals original-family slot 200, `0xfffffff005795c44`,
and no `NativeLegacyDisplay::submitSwap` symbol remains. The real swap slot
178 remains `0xfffffff00578f224`. Both original selector dispatch rows match
their exact byte guards. The reopened binary check is
`E:/UniPadEvidence/2026-11-07-m8/compiled-selector-42267229495.txt`.

Original slot 200 is itself a short `kIOReturnUnsupported` stub. The retained
boot configuration has `offline-legacy-lift-refusals=1`, which may replace it
with the experiment's generic success function in the live object's vtable.
A successful selector result under that configuration does not establish
gamma processing. The new trace therefore records the effective and pre-lift
slots 178/200, the success-function address and the armed/lifted counts.

The next trace reuses the matched capture harness and its 1,800-second window.
Its optional run/bundle/device settings now select the new artifact explicitly.
Large QEMU scratch and duplicate evidence can be placed on E: through
`UNIPAD_SUSTAINED_SCRATCH` and `UNIPAD_SUSTAINED_DUPLICATE`; the channel logs,
process snapshots, client tables and summaries stay inside the project.

The trace additionally imports `unipad.host.qemu_metal.spec()` rather than
copying its counter widths, samples PM/state words and preserves the display's
registration-property XML. `SharedWindow.cpp` requires the shared-window
fw_cfg item, which the baseline command does not provide; `QemuMetalPublish.cpp`
requires that window's stage 4 before publication. GPU addon files alone do
not publish a kernel accelerator. These are source/configuration observations;
the added counters will measure the runtime state. The GPU-to-CPU scanout
fallback copies an already rendered frame and does not supply a compositor.
No Windows QM request backend is implemented in `src/unipad`; the module named
`qemu_metal.py` supplies counter specifications and parsing.

The matched Windows trace is now running at
`local-private/boot-m8-42267229495/display-client-trace/`; its QEMU scratch and
duplicate evidence are on E:. The client decoder source hash was checked
against exact device `d4541e8` and remains unchanged at
`c9548ae3c80c6f2809e878b9d7061086262bff478eb2792005365135fbe44d55`.
No new frame, interaction or late client-table result is claimed yet.
The full PRD remains active: M9/M9a, real input, runtime IPA import, app
interaction and persistent Data have not passed.
