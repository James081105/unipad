#!/usr/bin/env python3
"""Freeze a boot-artifact directory: hash every file, copy the records, write a manifest.

The build outputs under ``local-private/`` are the only evidence a real macOS runner
ever produced, and they are the inputs of every later boot attempt.  Losing the
record of *which* bytes they were is losing the run: a later kernel rebuild changes
``kernel.macho`` in place, and nothing in the filename says so.

This tool therefore does not move or rename anything.  It reads a directory, writes
``<archive>/manifest.json`` with the sha256, size and role of every file it found, and
copies the small text records (``*.json``, ``*.log``, ``*.txt``) next to the manifest
so the archive is self-describing without the originals.

Usage:
    python scripts/archive-boot-artifacts.py SOURCE --label LABEL [--root DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Files that are copied verbatim into the archive.  Everything else is described by
#: the manifest only: a 51 MB Mach-O and a 14 MB manifest are records in their own
#: right, and duplicating them per archive would fill the disk for no new evidence.
RECORD_SUFFIXES = frozenset({".json", ".log", ".txt", ".md"})

#: What each known record is, so the manifest can name a file's role without the
#: reader having to guess from its name.  A file that matches nothing is still
#: recorded, with ``role: "other"``.
ROLES = {
    "kernel.macho": "kernel (Mach-O arm64, built on the macOS runner)",
    "DarwinBoot.efi": "bootloader (EFI application, built on the macOS runner)",
    "kernel.macho.adt": "device tree (Apple ADT, QEMU virt)",
    "build-report.json": "build report of the runner stage",
    "stage-result.json": "per-step ledger of the runner stage",
    "artifact-manifest.json": "content-addressed inventory of the build tree",
    "boot.zip": "the whole build tree as one archive",
}


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of ``path``, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan(source: Path) -> list[dict]:
    """Return one record per regular file under ``source``, sorted by relative path.

    Sorting is what makes the manifest byte-stable across runs over the same tree, so
    two archives of an unchanged directory diff cleanly.
    """
    records = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        records.append(
            {
                "path": relative,
                "role": ROLES.get(path.name, "other"),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="directory to freeze")
    parser.add_argument("--label", required=True, help="archive label, e.g. boot5-m4")
    parser.add_argument("--root", type=Path, default=None, help="archive root (default: SOURCE/../archive)")
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_dir():
        print(f"not a directory: {source}", file=sys.stderr)
        return 2

    root = (args.root or source.parent / "archive").resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / f"{stamp}-{args.label}"
    target.mkdir(parents=True, exist_ok=False)

    records = scan(source)
    copied = []
    for record in records:
        if Path(record["path"]).suffix not in RECORD_SUFFIXES:
            continue
        destination = target / record["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / record["path"], destination)
        copied.append(record["path"])

    manifest = {
        "schema_version": 1,
        "label": args.label,
        "frozen_at": stamp,
        "source": str(source),
        "file_count": len(records),
        "total_bytes": sum(record["bytes"] for record in records),
        "records_copied": copied,
        "records": records,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    for record in records:
        if record["role"] != "other":
            print(f"{record['bytes']:>12}  {record['sha256'][:16]}  {record['role'].split(' (')[0]:<28} {record['path']}")
    print(f"\n{len(records)} files, {manifest['total_bytes']} bytes; {len(copied)} records copied to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
