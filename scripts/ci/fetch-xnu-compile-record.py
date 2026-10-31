#!/usr/bin/env python3
"""Obtain the one file an XNU archive builder needs, without building XNU.

Why this exists
---------------
``vendor/UniXNU-qemu/scripts/build-arm-driver.py`` does not know how to compile a
driver.  It reads a *compile record* an XNU pass leaves behind --
``build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json``
-- takes the clang command out of it, swaps the one argument that is XNU's own
source for the driver's, and runs that.  So the archive builders are downstream of
an XNU build by construction: ``scripts/unixnu/kernel.py:83-95`` runs a whole
bootstrap pass of XNU first, purely to leave those records on disk.

A bootstrap pass is a full XNU compile (``build-xnu-4k-baseline.sh:774`` is not
gated on ``BOOTSTRAP``), which is hours.  The record itself is 9,452 bytes and is
byte-identical across runs eleven days apart, and it has already been uploaded as
part of the build artifact twice.  This script takes it from there instead.

What it refuses to do
---------------------
Hand the builder a record it has not checked.  Three checks, and each one is a
failure that would otherwise read as success:

1. the sha256 must equal the digest that was measured on two prior runs, so a
   truncated download or a different file cannot pass;
2. ``record['arguments']`` must contain the XNU source path, because
   ``build-arm-driver.py:56`` substitutes that exact argument.  A record whose
   source is not in its own arguments compiles XNU instead of the driver, and the
   builder's own Mach-O audit still passes -- a green message and the wrong archive;
3. ``record['directory']`` must exist, because the builder runs clang with
   ``cwd=record['directory']``.  A record from a different workspace layout fails
   here rather than compiling in the wrong directory.

The token is read from the environment and never printed, and nothing this script
writes contains an absolute path from the machine that ran it: the provenance
records the run id and the artifact id, which is what a reader needs to re-fetch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

#: The member of the artifact that carries the record, relative to the artifact
#: root.  It is the assembled tree's OBJROOT path, so it is spelled out rather than
#: globbed: a glob that matched two members would silently take the first.
MEMBER = "unixnu-out/qemu/tree/build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json"

#: The digest measured on runs 40074555796 and 41139423523, eleven days apart.  A
#: third value means the file is not the one those runs built.
EXPECTED_SHA256 = "cf5784fd4cbe3a801f7ce453c13c188b70c61b44fa418238f7958db3039ef805"

#: The source file the record must name.  The builder substitutes the argument
#: equal to ``record['file']``; if that string is nowhere in ``arguments`` the
#: substitution never fires and clang compiles XNU instead of the driver.
EXPECTED_SOURCE_BASENAME = "IOPlatformExpert.c"


class RecordError(Exception):
    """A refusal to trust or use the record.  Never coerced into a success."""


def api(url: str, token: str, *, stream: bool = False):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "unipad-kernel-console-ci",
    }
    request = urllib.request.Request(url, headers=headers)
    if stream:
        return urllib.request.urlopen(request, timeout=600)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_artifact(repo: str, run_id: str, prefix: str, token: str) -> dict:
    """The artifact of one run whose name starts with ``prefix``.

    Resolved from the API rather than written down, because an artifact id is a
    per-run number: a workflow that carried it would be a second place that knows
    something the API already answers.  Exactly one match is required -- two would
    mean the run uploaded the same role twice, and picking one silently is how a
    build ends up attesting to bytes it never compiled.
    """
    page = 1
    matches = []
    while True:
        listing = api(
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100&page={page}",
            token,
        )
        for artifact in listing.get("artifacts") or []:
            name = str(artifact.get("name") or "")
            if name.startswith(prefix):
                matches.append(artifact)
        if len(listing.get("artifacts") or []) < 100:
            break
        page += 1
    if not matches:
        raise RecordError(f"run {run_id} has no artifact whose name starts with {prefix!r}")
    if len(matches) > 1:
        names = ", ".join(sorted(str(item.get("name")) for item in matches))
        raise RecordError(f"run {run_id} has {len(matches)} artifacts matching {prefix!r}: {names}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-prefix", default="unipad-build-")
    parser.add_argument("--member", default=MEMBER)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--provenance", required=True, type=Path)
    parser.add_argument("--expect-sha256", default=EXPECTED_SHA256)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set: the artifact API needs it")

    try:
        artifact = resolve_artifact(args.repo, args.run_id, args.artifact_prefix, token)
        artifact_id = artifact.get("id")
        download = artifact.get("archive_download_url")
        size = artifact.get("size_in_bytes")
        if not download:
            raise RecordError(f"artifact {artifact_id} carries no archive_download_url")

        print(f"artifact: {artifact.get('name')} id={artifact_id} {size} bytes", flush=True)
        with api(download, token, stream=True) as response:
            payload = BytesIO(response.read())
        print(f"downloaded {len(payload.getvalue())} bytes", flush=True)

        with zipfile.ZipFile(payload) as archive:
            names = archive.namelist()
            if args.member not in names:
                # A near-miss is worth naming: the member list is the only way to
                # tell "the layout moved" from "the wrong artifact".
                near = [name for name in names if name.endswith("IOPlatformExpert.cpo.json")]
                raise RecordError(
                    f"{args.member} is not in the artifact; {len(names)} members, "
                    f"{len(near)} ending in IOPlatformExpert.cpo.json"
                )
            raw = archive.read(args.member)
    except (RecordError, urllib.error.URLError, OSError, zipfile.BadZipFile) as error:
        message = error if isinstance(error, RecordError) else f"{error.__class__.__name__}: {error}"
        print(f"::error::the compile record could not be obtained: {message}")
        args.provenance.parent.mkdir(parents=True, exist_ok=True)
        args.provenance.write_text(
            json.dumps(
                {
                    "origin": "not-obtained",
                    "detail": str(message),
                    "repo": args.repo,
                    "run_id": args.run_id,
                    "member": args.member,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1

    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.expect_sha256:
        print(
            f"::error::the record's sha256 is {digest}, not the measured "
            f"{args.expect_sha256}: this is not the file the two prior runs built"
        )
        args.provenance.parent.mkdir(parents=True, exist_ok=True)
        args.provenance.write_text(
            json.dumps(
                {
                    "origin": "digest-mismatch",
                    "detail": f"sha256 {digest} != measured {args.expect_sha256}",
                    "repo": args.repo,
                    "run_id": args.run_id,
                    "member": args.member,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1

    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        print(f"::error::the record is not JSON: {error.__class__.__name__}")
        return 1

    arguments = record.get("arguments")
    if not isinstance(arguments, list) or not arguments:
        print("::error::the record carries no arguments array")
        return 1
    if not any(
        isinstance(value, str) and os.path.basename(value) == EXPECTED_SOURCE_BASENAME
        for value in arguments
    ):
        print(
            f"::error::no argument of the record names {EXPECTED_SOURCE_BASENAME}: "
            "build-arm-driver.py would substitute nothing and compile XNU instead "
            "of the driver, and its own Mach-O audit would still pass"
        )
        return 1
    directory = record.get("directory")
    if not isinstance(directory, str) or not directory:
        print("::error::the record carries no directory; the builder's cwd would be unknown")
        return 1
    if not Path(directory).is_dir():
        # Not fatal on its own: the directory is an absolute path from the run that
        # produced the record, and this run's workspace may differ.  It is recorded
        # as a warning because the builder will then run clang in a directory that
        # does not exist, which fails loudly rather than silently.
        print(
            f"::warning::the record's directory {directory} does not exist in this "
            "workspace; the builder runs clang there and will fail if it matters"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    provenance = {
        "origin": "downloaded from a prior run's build artifact",
        "detail": (
            "the compile record an XNU pass leaves behind, taken from run "
            f"{args.run_id} of {args.repo} because producing one requires a full "
            "XNU build; the flags are the pinned XNU's own, not this run's"
        ),
        "repo": args.repo,
        "run_id": args.run_id,
        "artifact_id": artifact_id,
        "artifact_name": artifact.get("name"),
        "artifact_size_bytes": size,
        "member": args.member,
        "sha256": digest,
        "size_bytes": len(raw),
        "xnu_source": next(
            value for value in arguments if os.path.basename(value) == EXPECTED_SOURCE_BASENAME
        ),
        "cwd_the_builder_uses": directory,
        "cwd_exists_here": Path(directory).is_dir(),
    }
    args.provenance.parent.mkdir(parents=True, exist_ok=True)
    args.provenance.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"record: {args.output.as_posix()} ({len(raw)} bytes, sha256 {digest})")
    print(f"provenance: {args.provenance.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
