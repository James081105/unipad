#!/usr/bin/env bash
# prepare-image.sh — UniPad IPSW/RootFS preparation entry (PRD 8.1, 12)
#
# macOS ONLY. Apple IPSW extraction relies on macOS disk tooling; this script
# must never run on Windows and must never write into the git repo. All
# material stays under local-private/ (PRD 12: never committed, never
# published as a public artifact).
set -euo pipefail

IPSW=""
OUT_DIR=""
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Resolve a Python interpreter: python3 on macOS, python on Windows/Git Bash.
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else die "no python interpreter found"; fi

die() { echo "prepare-image: ERROR: $*" >&2; exit 2; }
info() { echo "prepare-image: $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ipsw) IPSW="${2:-}"; shift 2 ;;
    --out) OUT_DIR="${2:-}"; shift 2 ;;
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$IPSW" ]] || die "--ipsw is required (path under local-private/)"
[[ -n "$OUT_DIR" ]] || die "--out is required (must NOT be inside the repo)"
[[ -f "$IPSW" ]] || die "IPSW not found: $IPSW"

case "$OUT_DIR" in
  "$REPO_ROOT"*) die "--out must not be inside the repository" ;;
esac

# --- 1. verify the IPSW against the pinned hash ------------------------------
"$PY" - "$IPSW" <<'PY' || die "IPSW hash verification failed"
import hashlib, json, sys
path = sys.argv[1]
lock = json.load(open('config/sources.lock.json', encoding='utf-8'))
expected = lock.get('ipsw', {}).get('sha256')
if not expected:
    print('prepare-image: sources.lock.json ipsw.sha256 is null', file=sys.stderr)
    sys.exit(1)
h = hashlib.sha256()
with open(path, 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)
if h.hexdigest() != expected:
    print(f'prepare-image: sha256 mismatch: got {h.hexdigest()} want {expected}', file=sys.stderr)
    sys.exit(1)
print('prepare-image: IPSW sha256 matches the pinned lock')
PY

# --- 2. extract (macOS tooling) ----------------------------------------------
mkdir -p "$OUT_DIR"
info "extracting $(basename "$IPSW") into $OUT_DIR"

# TODO(macos-only): the extraction steps depend on the pinned IPSW layout and
# Apple tooling (e.g. pkgexpand/aa + dmount-style mounts). They are recorded
# per experiment; the host-side verification of the result (M6/M7) is a
# separate Windows-side gate and must not be claimed from this script.
info "STUB: IPSW extraction for $(basename "$IPSW")"

# --- 3. write a local manifest (hashes only, no repo writes) -----------------
"$PY" - "$OUT_DIR" <<'PY'
import hashlib, json, os, sys
out = sys.argv[1]
entries = []
for root, _dirs, files in os.walk(out):
    for name in sorted(files):
        p = os.path.join(root, name)
        h = hashlib.sha256()
        with open(p, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        entries.append({'path': os.path.relpath(p, out), 'sha256': h.hexdigest(),
                        'size_bytes': os.path.getsize(p)})
with open(os.path.join(out, 'image-manifest.json'), 'w', encoding='utf-8') as f:
    json.dump({'schema_version': 1, 'entries': entries}, f, indent=2, sort_keys=True)
print(f'prepare-image: image manifest written ({len(entries)} files)')
PY

info "image prepared at $OUT_DIR (stays in local-private; never committed)"
