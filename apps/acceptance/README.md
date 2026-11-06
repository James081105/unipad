# PRD 5.1 acceptance apps

`main.m` builds two independent Objective-C/UIKit iPad Device apps:

| IPA | Bundle ID | Observable behavior |
| --- | --- | --- |
| HelloUniPad.ipa | com.unipad.hello | Run challenge, editable challenge field, Count tap button and visible counter |
| UniPadNotes.ipa | com.unipad.notes | Run challenge, text input, explicit Save and Reload, Documents/note.txt readback |

Both use only UIKit/Foundation, target arm64 iOS 17.0, and request no extra
permissions or entitlements. There are no embedded frameworks or network calls.
Each process starts with a fresh UUID challenge. Enter the host's random value
and tap **Apply challenge** to bind the visible screen to that test run. The URL
schemes also accept `unipad-hello://challenge?value=HOST_NONCE` and
`unipad-notes://challenge?value=HOST_NONCE` (percent-encode the value). A guest
launcher has to deliver that URL; a protocol request alone does not change it.

On a Mac with Xcode selected:

```sh
python3 scripts/macos/build-acceptance-apps.py --out build/acceptance-apps-RUN
```

The manual `build-acceptance-apps` Actions workflow uses the same available
`macos-latest` runner family as the system build. Its resolved Xcode/SDK versions
are recorded; the runner label itself is not a frozen toolchain. The legacy
application lifecycle targets iPadOS 17; newer-SDK deprecations remain visible
compiler warnings. Each build preserves compiler commands, SDK/tool versions, source hashes,
commit, codesign verification, Mach-O load commands, IPA hashes and package
precheck results in `manifest.json`. It fails on a Simulator platform, wrong
architecture, encrypted executable or missing signature. Use a new output
directory for every run.

The packages carry **ad hoc signatures**. Package checks and codesign integrity
do not establish that the guest's installer or execution policy accepts them.
The manifest leaves guest trust, installation and interaction unverified.
These packages do not include a provisioning profile or distribution signing
identity; the actual guest trust route remains part of M10a.

For HelloUniPad, apply a new host challenge, take a frame, tap Count tap ten
times, verify Count: 10, go Home and reopen. The count is process memory; it is
not a persistence witness. For UniPadNotes, enter a new random text marker,
tap Save, change the field and tap Reload, then close/reopen. Finally shut
down the guest normally, terminate QEMU and boot a new QEMU process. Only
reading the same note in that new boot establishes Data persistence.

Save reports success only after an atomic UTF-8 write and exact file readback.
Read/write failures remain visible. Building these samples alone passes no
M9/M9a/M9b/M10–M12 guest milestone.
