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
Hand the builder a record it has not checked.  The checks, and each one is a
failure that would otherwise read as success:

1. the sha256 must equal the digest measured on two prior runs, so a truncated
   download or a different file cannot pass;
2. the record's ``file`` must be the translation unit the builder's recipe path
   names, AND must appear verbatim among its ``arguments``, because
   ``build-arm-driver.py:56`` substitutes the one argument equal to it.  A record
   whose source is not in its own arguments compiles XNU instead of the driver,
   and the builder's own Mach-O audit still passes -- a green message and the
   wrong archive;
3. the ``directory`` the builder runs clang in has to exist, because the command
   carries a relative ``-I .``.  A run that installed the headers without
   compiling an object does not have it, so it is created here and recorded.

The token is read from the environment and never printed, and nothing this script
writes contains an absolute path from the machine that ran it: the provenance
records the run id and the artifact id, which is what a reader needs to re-fetch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

#: The member of the artifact that carries the record, relative to the artifact
#: root.  It is the assembled tree's OBJROOT path, so it is spelled out rather than
#: globbed: a glob that matched two members would silently take the first.
MEMBER = "unixnu-out/qemu/tree/build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json"

#: The digest measured on runs 40074555796 and 41139423523, eleven days apart.  A
#: third value means the file is not the one those runs built.
EXPECTED_SHA256 = "cf5784fd4cbe3a801f7ce453c13c188b70c61b44fa418238f7958db3039ef805"

#: The translation unit the record must describe.  Matched as a STEM plus a source
#: suffix, not as an exact filename: the first revision required the literal
#: ``IOPlatformExpert.c`` and refused the real record, whose source is
#: ``iokit/Kernel/IOPlatformExpert.cpp`` -- measured by extracting the member from
#: the artifact with three HTTP range requests, which also reproduced its sha256
#: cf5784fd... exactly.  A guard stricter than the thing it guards is a broken
#: instrument, not a strict one.
EXPECTED_SOURCE_STEM = "IOPlatformExpert"

#: What a translation unit looks like.  XNU compiles this one as C++ even though
#: the archive it produces is named for a driver, so the suffix is not fixed.
SOURCE_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++", ".C", ".c", ".m", ".mm")


class RecordError(Exception):
    """A refusal to trust or use the record.  Never coerced into a success."""


def parse_record(raw: bytes) -> dict:
    """The record's JSON, normalized the way ``build-arm-driver.py:27`` normalizes it.

    XNU's ``-MJ`` writes one JSON object per compile and appends a comma separator
    when the same output file is written again, so the file on disk ends ``},``.
    ``build-arm-driver.py`` handles that with ``.strip().rstrip(',')``; a plain
    ``json.loads`` here raises "Extra data" -- measured on run 41218257835, where
    the builder parsed the file successfully and this script refused it.  A second
    parser of the same bytes is a divergence, so the same normalization is used.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RecordError(f"the record is not UTF-8: {error}") from None
    try:
        return json.loads(text.strip().rstrip(","))
    except ValueError as error:
        raise RecordError(f"the record is not JSON even after the trailing comma is removed: {error}") from None


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


def download_with_curl(url: str, token: str) -> Path:
    """The artifact zip, fetched by curl into a private temporary file.

    The whole archive is fetched even though one 9 KB member is wanted: the GitHub
    API offers no partial download of an artifact, and the artifact is ~1.1 GB.  It
    is written to a temporary file and LEFT THERE, returned as a path, because
    reading 1.1 GB into a Python bytes object on a runner that also holds a build
    tree is how a job dies of the OOM killer with a message about something else.
    The caller deletes it.
    """
    import subprocess
    import tempfile

    curl = shutil.which("curl")
    if not curl:
        raise RecordError("curl is not on PATH; the artifact download needs it")
    with tempfile.NamedTemporaryFile(prefix="unipad-artifact-", suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    command = [
        curl,
        "--silent",
        "--show-error",
        "--location",
        "--fail-with-body",
        "--retry", "3",
        "--retry-delay", "2",
        "--max-time", "900",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
        "-o", str(temporary),
        url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        temporary.unlink(missing_ok=True)
        raise RecordError(f"curl exited {result.returncode} downloading the artifact: {detail}")
    return temporary


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
    # The zip is ~1.1 GB of fetched material on a runner that also holds a build
    # tree.  It is handed back through a holder so that this finally -- which runs
    # on success, on refusal and on an unexpected exception alike -- can delete it.
    # An earlier revision kept it in a local of the function that raised, so the
    # file survived every failure path.
    holder: dict = {}
    try:
        return obtain(args, token, holder)
    finally:
        fetched = holder.get("payload")
        if fetched is not None:
            fetched.unlink(missing_ok=True)


def obtain(args, token: str, holder: dict) -> int:
    try:
        artifact = resolve_artifact(args.repo, args.run_id, args.artifact_prefix, token)
        artifact_id = artifact.get("id")
        download = artifact.get("archive_download_url")
        size = artifact.get("size_in_bytes")
        if not download:
            raise RecordError(f"artifact {artifact_id} carries no archive_download_url")

        print(f"artifact: {artifact.get('name')} id={artifact_id} {size} bytes", flush=True)
        # curl, not urllib, and the reason is a measurement rather than a
        # preference.  The artifact API answers with a 302 to a blob URL carrying a
        # SAS token.  urllib follows the redirect WITH the Authorization header
        # still attached, and Azure Blob Storage rejects a request that carries
        # both a SAS token and an Authorization header -- "Server failed to
        # authenticate the request", HTTP 401.  Measured on run 41218257835: the
        # listing succeeded and the download did not, 0.5 s apart.  curl drops
        # custom headers on a cross-host redirect unless --location-trusted is
        # given, which is exactly the behaviour wanted here.
        holder["payload"] = download_with_curl(download, token)
        print(f"downloaded {holder['payload'].stat().st_size} bytes", flush=True)

        with zipfile.ZipFile(holder["payload"]) as archive:
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
        record = parse_record(raw)
    except RecordError as error:
        print(f"::error::{error}")
        return 1

    source = record.get("file")
    if not isinstance(source, str) or not source:
        print("::error::the record carries no file; build-arm-driver.py would substitute nothing")
        return 1
    basename = os.path.basename(source)
    if not (basename.startswith(EXPECTED_SOURCE_STEM) and basename.endswith(SOURCE_SUFFIXES)):
        print(
            f"::error::the record's source is {source!r}, which is not "
            f"{EXPECTED_SOURCE_STEM}<a source suffix>: this is the record for a "
            "different translation unit"
        )
        return 1
    # The substitution itself.  build-arm-driver.py:56 replaces the one argument
    # EQUAL to record['file']; if that string is not among the arguments the
    # substitution never fires, clang is pointed at XNU's own source, and the
    # builder's Mach-O audit still passes -- a green message and the wrong archive.
    if source not in arguments:
        print(
            f"::error::{source!r} does not appear in the record's own arguments: "
            "build-arm-driver.py would substitute nothing and compile XNU instead "
            "of the driver, and its own Mach-O audit would still pass"
        )
        return 1
    directory = record.get("directory")
    if not isinstance(directory, str) or not directory:
        print("::error::the record carries no directory; the builder's cwd would be unknown")
        return 1
    # The builder runs clang with cwd=record['directory'], and the command carries a
    # relative `-I .`, so the directory has to exist.  A run that installed the XNU
    # headers without compiling any object has OBJROOT but not this subdirectory, so
    # it is created here -- and recorded, because "a directory was made" is a fact
    # about this run and not something to leave implicit.  It is safe: in the real
    # build that directory holds only .cpo objects, never headers.
    created_cwd = False
    if not Path(directory).is_dir():
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            created_cwd = True
            print(
                f"::notice::created {directory}: the record's cwd, which a run that "
                "installed the XNU headers without compiling an object does not have. "
                "It holds only objects in the real build, never headers."
            )
        except OSError as error:
            print(f"::error::cannot create the record's directory {directory}: {error}")
            return 1

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
        "xnu_source": source,
        "compiler_the_record_names": arguments[0],
        "cwd_the_builder_uses": directory,
        "cwd_created_here": created_cwd,
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
