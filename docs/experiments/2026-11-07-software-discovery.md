# Accelerator discovery experiment

The corrected selector build reaches a real running launchd, SpringBoard and
backboardd on Windows. At its 1,210-second sample, backboardd still has zero
SwapBegin/SwapEnd calls. QemuMetal/shared-window stage and all drawing counts
are zero. Gamma now returns success through the experimental refusal lift;
the live slot points to that success function. This changes no swap behavior.
The capture remains in progress until its full 1,800-second window ends.

## Reference boundary

[HakiOS MiniIOMobileFramebuffer.cpp at 2631f5d, lines 1998–2033](https://github.com/oosexgang/hakios-xnu-kernel/blob/2631f5d7d4f9d7322aca60991b34ba465dec343b/xnu/files/iokit/Kernel/MiniIOMobileFramebuffer.cpp#L1998)
publishes a bare accelerator to remove CoreAnimation's wait for such a service.
Without its optional plugin properties, accelerated Metal/GL discovery finds
no corresponding driver. That establishes service discovery, not a renderer.

[The same reference's inject.c, lines 175–198](https://github.com/oosexgang/hakios-xnu-kernel/blob/2631f5d7d4f9d7322aca60991b34ba465dec343b/userland/src/inject.c#L175)
calls the original renderer method and remembers NULL after its first failure.
Its hook does not create a software renderer. The original QuartzCore software
path supplies any software drawing. The file header identifies Darwin userland
16.5 / 20F66 / iPhone10,3. Neither this behavior nor its private addresses are
established for the project's iPadOS 17 artifact.

## Implemented probe

Device branch `codex/software-discovery`, commit
`172b7888d50563ee16d55ef0acb760c458f8bd1b`, adds an independent,
**default-off** `unipad-software-discovery=1` boot option. The late probe calls
its publisher before launchd. It loads the original IOGPUFamily, qualifies the
actual IOAcceleratorES parent and registers a dedicated empty subclass through
the existing delayed metaclass lifecycle.

The audited parent and child are 136 bytes, with 168 virtual entries. The build
checks every compiled child entry: only destructor/metaclass slots 0/1/7 may
belong to the new class; all operational slots must match original IOService
targets. Four exact 32-byte original constructor/destructor/metaclass
fingerprints are checked against the matched original family. New compilation
does not make the old whole-candidate hash a current kernel hash.

The service carries the reference name `sgx` and the diagnostic marker
`UniPadDiscoveryOnly`. It publishes no Metal/GL plugin properties and implements
no GPU user client, shader compiler, compositor or shared-window protocol. It
inherits all operational behavior from the audited original base.

Eight host counter specifications record stage, status, parent/child extent,
inheritance, Metal property mask, live object and registry ID. Stage 4 means
registry publication only. Declaration parsing checks each counter's actual
width; pointers/registry IDs are eight bytes, other fields four.

## Validation and next runtime comparison

The six existing corrected-selector ABI checks pass against the new script.
Source counter/declaration checks pass. All four original-base fingerprints
match kernel `791a5926…`; four independently corrupted entry reads are rejected.
The new class's real compiled table and macOS kernel link still require the
next build. Its live service publication and any renderer behavior remain
unverified.

The reopened transaction set at `.scratch/prd-transaction/software-discovery/`
contains the baseline and modified source ZIPs, `DIFF_FILE`, `VERIFICATION.txt`
and executable `ROLLBACK.sh`. Exact commands, inputs, literal outputs and
statuses are recorded there. Rollback on another bundle restores the absent
service/counters and original source behavior; the modified bundle and source
remain changed.

After a successful build, compare the default-off and enabled boots of the
same exact kernel. First verify actual registry publication, inheritance and
plugin absence. Then measure default-surface/SwapBegin/SwapEnd calls, rendering
activity, actual IOSurface pixels and input feedback. Service presence, NULL
Metal discovery or gamma success alone passes no display milestone.
