#!/usr/bin/env python3
"""Build the two PRD 5.1 UIKit Device IPAs using the selected Xcode SDK.

Ad hoc signatures are recorded as signatures, never as guest installation or
trust evidence. The output contains only our apps and their build metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import plistlib
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "apps/acceptance/main.m"
APPS = (("HelloUniPad", "com.unipad.hello", "unipad-hello", False),
        ("UniPadNotes", "com.unipad.notes", "unipad-notes", True))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str], commands: list[dict]) -> str:
    result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
    commands.append({"argv": argv, "exit_status": result.returncode,
                     "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode:
        raise RuntimeError(f"{argv[0]} exited {result.returncode}: {result.stderr}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True,
                        help="new output directory; existing build evidence is preserved")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error(f"output directory is not empty: {out}")
    if platform.system() != "Darwin":
        parser.error("building requires macOS with the Xcode iPhoneOS SDK")
    out.mkdir(parents=True, exist_ok=True)
    commands: list[dict] = []
    manifest: dict = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                      "signature_mode": "ad-hoc", "guest_installation": "unverified",
                      "guest_trust": "unverified", "guest_interaction": "unverified",
                      "source_sha256": {str(SOURCE.relative_to(ROOT)): digest(SOURCE),
                                        str(Path(__file__).relative_to(ROOT)): digest(Path(__file__))},
                      "apps": [], "commands": commands}
    try:
        manifest["commit"] = run(["git", "rev-parse", "HEAD"], commands)
        manifest["xcode"] = run(["xcodebuild", "-version"], commands)
        manifest["sdk_version"] = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-version"], commands)
        sdk = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], commands)
        compiler = run(["xcrun", "--sdk", "iphoneos", "--find", "clang"], commands)
        manifest["compiler"] = run([compiler, "--version"], commands)
        sys.path.insert(0, str(ROOT / "src"))
        from unipad.ipa.package import open_ipa
        from unipad.ipa.precheck import run_precheck

        with tempfile.TemporaryDirectory(prefix="app-build-", dir=out) as work:
            for name, bundle_id, scheme, notes in APPS:
                app = Path(work) / name / "Payload" / f"{name}.app"
                app.mkdir(parents=True)
                info = {
                    "CFBundleExecutable": name, "CFBundleIdentifier": bundle_id,
                    "CFBundleName": name, "CFBundleDisplayName": name,
                    "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
                    "CFBundleShortVersionString": "0.1.0", "CFBundleInfoDictionaryVersion": "6.0",
                    "CFBundleSupportedPlatforms": ["iPhoneOS"], "DTPlatformName": "iphoneos",
                    "MinimumOSVersion": "17.0", "UIDeviceFamily": [2],
                    "LSRequiresIPhoneOS": True, "UIRequiresFullScreen": True,
                    "UILaunchScreen": {},
                    "UISupportedInterfaceOrientations": ["UIInterfaceOrientationPortrait"],
                    "CFBundleURLTypes": [{"CFBundleURLName": bundle_id, "CFBundleURLSchemes": [scheme]}],
                }
                (app / "Info.plist").write_bytes(plistlib.dumps(info))
                executable = app / name
                run([compiler, "-target", "arm64-apple-ios17.0", "-isysroot", sdk,
                     "-fobjc-arc", "-O2", "-Wall", "-Wextra", "-Werror",
                     "-Wno-error=deprecated-declarations",
                     f"-DUNIPAD_NOTES={int(notes)}", str(SOURCE),
                     "-framework", "UIKit", "-framework", "Foundation",
                     "-o", str(executable)], commands)
                run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(app)], commands)
                run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], commands)
                run(["codesign", "--display", "--verbose=4", str(app)], commands)
                run(["xcrun", "otool", "-l", str(executable)], commands)
                ipa = out / f"{name}.ipa"
                with zipfile.ZipFile(ipa, "w", zipfile.ZIP_DEFLATED) as archive:
                    for item in sorted(app.rglob("*")):
                        if item.is_file():
                            archive.write(item, item.relative_to(app.parent.parent).as_posix())
                package = open_ipa(ipa)
                precheck = run_precheck(ipa)
                slices = package.macho.slices
                if (not precheck.accepted or package.bundle_id != bundle_id or len(slices) != 1
                        or slices[0].cputype != 0x0100000C or slices[0].platform != 2
                        or not slices[0].has_code_signature or package.encrypted):
                    raise RuntimeError(f"Device IPA verification failed for {name}: {precheck.to_json()}")
                manifest["apps"].append({"path": ipa.name, "sha256": digest(ipa),
                                         "executable_sha256": digest(executable),
                                         "signature_mode": "ad-hoc", "precheck": precheck.to_dict()})
                print(f"{name}: arm64 iOS Device, signed, unencrypted, precheck=accept", flush=True)
        manifest["build_status"] = "passed"
        return 0
    except Exception as exc:
        manifest["build_status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        print(manifest["error"], file=sys.stderr)
        return 1
    finally:
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
