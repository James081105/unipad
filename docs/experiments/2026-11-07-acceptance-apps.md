# Real PRD 5.1 sample packages

The repository previously contained synthetic IPA fixtures and host protocol
models, but no UIKit source for the two PRD samples. Commit `6623c6e` adds
the shared Objective-C source, independent Device builds and a manual Actions
workflow. Commit `56e34d3` selects the available runner after GitHub rejected
`macos-14-arm64-us-default` before any build step ran.

## Delivered behavior and build

`apps/acceptance/main.m` builds `com.unipad.hello` and `com.unipad.notes`.
HelloUniPad displays a fresh process challenge, accepts the host's test value
through a field or URL, and increments a visible tap count. UniPadNotes accepts
text, atomically writes `Documents/note.txt`, compares the file readback before
reporting success, and reloads that file on startup or by button. A scroll view
keeps the controls reachable while the screen keyboard is shown. No additional
permissions, entitlements, embedded frameworks or network services are used.

Build [42272645714](https://github.com/James081105/unipad/actions/runs/42272645714)
completed successfully at exact main `56e34d3ccd8ec5e26d32c19a84d0fbf11ddc9c99`.
The command was:

```sh
python3 scripts/macos/build-acceptance-apps.py --out build/acceptance-apps
```

The recorded tools are Xcode 26.6, build 17F113, iPhoneOS SDK 26.5. The compiler
explicitly targets `arm64-apple-ios17.0`. Both apps pass strict codesign
verification, Mach-O inspection and the project's package precheck. The script
preserves command arguments, literal command outputs, exit statuses, source
hashes and package hashes in `manifest.json`, including failure outcomes. The
runner label `macos-latest` is not a frozen toolchain; resolved versions are
recorded rather than inferred from that label.

Artifact `13529249109` is 34,203 bytes. Its ZIP SHA-256 is
`9482461fbb4687c5c9d8a6ea780c09e7c4ae31b1cb45040ca144252ac37f6548`,
independently checked against the upload log after download. The extracted
files are at `E:/UniPadEvidence/2026-11-07-m8/acceptance-apps-42272645714/`:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| HelloUniPad.ipa | 15,353 | `f8224edda40b7d505c8a2f2ce46b9c6654caaafa4e0b72d5789a2357aefc980b` |
| UniPadNotes.ipa | 15,428 | `5d3e85e4e79cbd500d26c2150ad55ccc6dbcc39b4c70fb6234faa780439391f4` |
| manifest.json | 29,992 | `2462700849965b9eab8a76146ab4570388979fc3e90e01c51e84d0fba2814167` |

Windows independently reopened both IPA files, matched their manifest hashes
and verified one ARM64 slice, platform 2 (iOS Device), minimum version 17.0,
unencrypted executable, code signature and `precheck=accept`. Both compiler
input hashes agree with the repository's exact source bytes. All 22 existing
workflow contract checks pass locally.

## Verification boundary

These are ad hoc signatures. Integrity checks do not establish guest installer
acceptance or the execution trust policy. The manifest retains `unverified`
for guest trust, installation and interaction. Neither UIKit UI behavior nor
normal-reboot persistence has been observed in the guest. No M9/M9a/M9b or
M10–M12 milestone changes follow from this build.

The reopened four-artifact set at
`.scratch/prd-transaction/acceptance-apps/` contains `MODIFIED_FILE` (a ZIP source
bundle), `DIFF_FILE`, `VERIFICATION.txt` and executable `ROLLBACK.sh`. The empty
baseline bundle represents the absence of all four source files at `2db23e6`;
the modified bundle's compiler input hashes are matched to the real build.
Baseline, modified, rollback and restored checks all exit 0 with their literal
results recorded. Rollback restores only another archive copy and the modified
source bundle stays changed. The full macOS build log is preserved at
`E:/UniPadEvidence/2026-11-07-m8/acceptance-apps-42272645714.log`.
