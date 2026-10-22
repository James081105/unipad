#!/usr/bin/env bash
# prepare-image.sh -- UniPad IPSW / RootFS preparation entry (PRD 8.1, 12).
#
# WHY THIS EXISTS
#   PRD 8.1 lists image preparation as its own task ("镜像准备：单独任务处理必须
#   依赖 macOS 的步骤，不将其混入每次内核小修改").  IPSW expansion, Apple disk
#   images and RootFS assembly need macOS disk tooling that does not exist on
#   the Windows host, so this entry point runs on macOS only.
#
# WHAT IT DOES
#   1. validates that the IPSW exists and that its SHA-256 matches
#      config/sources.lock.json (ipsw.sha256).  A null pinned hash is a
#      refusal, never a warning: an unpinned image cannot be reproduced.
#   2. expands the IPSW into a caller-given directory.  The Apple-tool steps
#      are TODO(macos-verified-only) stubs.
#   3. writes image-manifest.json next to the result -- one entry per produced
#      file with its SHA-256 -- so the Windows side can verify what it gets.
#
# WHAT IT REFUSES TO DO
#   * write anywhere inside the git-tracked tree.  The only in-repo exception
#      is <repo>/local-private/, which .gitignore excludes and which PRD 12
#      designates for exactly this material.  Nothing produced here is ever
#      committed or published (PRD 8.2 step 4 never uploads system images).
#   * verify against an unpinned hash, or continue when the hash mismatches.
#   * claim success: the extraction is a stub, so the stage result always says
#      so.  Missing evidence is unverified, not success (PRD 7.4).
#   * print or store secrets.  Hashes and paths only.
#
# COMPATIBILITY
#   bash 3.2 (the /bin/bash shipped with macOS) and bash 4/5.  No mapfile, no
#   associative arrays, no "${arr[@]}" on a possibly empty array unguarded.
#
# Exit codes mirror unipad.errors.ExitCode: 0 ok, 1 failure, 2 usage, 3 blocked.
set -euo pipefail

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
readonly EXIT_OK=0
readonly EXIT_FAILURE=1
readonly EXIT_USAGE=2
readonly EXIT_BLOCKED=3
readonly SHA256_PATTERN='^[0-9a-fA-F]{64}$'

# Resolve a Python interpreter: python3 on macOS, python on Windows/Git Bash.
# Only the JSON, hashing and path work needs it; the Apple tool steps are stubs.
if command -v python3 >/dev/null 2>&1; then PY='python3'
elif command -v python >/dev/null 2>&1; then PY='python'
else PY=''; fi

# ---------------------------------------------------------------------------
# utilities (defined before any use: an earlier version called die() before it
# existed, which turned a clean refusal into a "command not found" exit 127)
# ---------------------------------------------------------------------------
utc_stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
info() { printf 'prepare-image: %s\n' "$*"; }
step() { printf '\n=== %s ===\n' "$*"; }

# json_escape TEXT -- emit TEXT as a JSON string body (RFC 8259).
json_escape() {
    local text="$1" out='' ch i code
    for (( i = 0; i < ${#text}; i++ )); do
        ch="${text:i:1}"
        case "$ch" in
            \\)     out+='\\' ;;
            '"')     out+='\"' ;;
            $'\n')   out+='\n' ;;
            $'\r')   out+='\r' ;;
            $'\t')   out+='\t' ;;
            *)
                code=$(printf '%d' "'$ch")
                if (( code >= 0 && code < 32 )); then
                    out+=$(printf '\\u%04x' "$code")
                else
                    out+="$ch"
                fi
                ;;
        esac
    done
    printf '%s' "$out"
}

# emit_error CODE MESSAGE [DETAIL_JSON] -- machine-readable, never a bare string.
emit_error() {
    local code="$1" message="$2" details="${3:-{\}}"
    printf '{"error":{"code":"%s","message":"%s","details":%s}}\n' \
        "$(json_escape "$code")" "$(json_escape "$message")" "$details" >&2
}

die() { # die CODE MESSAGE [DETAIL_JSON] EXIT
    if (( $# != 4 )); then
        # Guard against the classic arity slip: with a wrong count the message
        # lands in the details slot and the details JSON in the exit-code slot,
        # which corrupts the error output and the exit status.
        printf '{"error":{"code":"internal_error","message":"die() called with %d arguments, expected 4","details":{}}}\n' "$#" >&2
        printf 'prepare-image: FAILED [internal_error] die() called with %d arguments, expected 4\n' "$#" >&2
        exit "$EXIT_FAILURE"
    fi
    local code="$1" message="$2" details="$3" exit_code="$4"
    emit_error "$code" "$message" "$details"
    printf 'prepare-image: FAILED [%s] %s\n' "$code" "$message" >&2
    exit "$exit_code"
}

usage() {
    cat <<'EOF'
prepare-image.sh -- UniPad IPSW / RootFS preparation entry (PRD 8.1, 12)

Usage:
  prepare-image.sh --ipsw <file> --out <dir> [options]

Required:
  --ipsw <file>              The IPSW to prepare.  Lives under local-private/
                             and is never committed (PRD 12).
  --out <dir>                Where to expand it.  Must NOT be inside the
                             git-tracked tree; <repo>/local-private/ is the
                             only in-repo exception (.gitignore excludes it).

Options:
  --lock <path>              sources.lock.json.
                             Default <repo>/config/sources.lock.json.
  --repo-root <dir>          Repository root.  Default: derived from this
                             script's location.
  --dry-run                  Validate the IPSW and the pinned hash, print the
                             plan, write nothing and extract nothing.
  -h, --help                 This text.

Notes:
  * The Apple-tool extraction steps are TODO(macos-verified-only) stubs.
  * Exit codes: 0 ok, 1 failure, 2 usage, 3 blocked (unipad.errors.ExitCode).
EOF
}

# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------
IPSW=''
OUT_DIR=''
LOCK_PATH=''
REPO_ROOT=''
DRY_RUN=0

while (( $# > 0 )); do
    case "$1" in
        --ipsw)        IPSW="${2:-}"; shift 2 ;;
        --ipsw=*)      IPSW="${1#*=}"; shift ;;
        --out)         OUT_DIR="${2:-}"; shift 2 ;;
        --out=*)       OUT_DIR="${1#*=}"; shift ;;
        --lock)        LOCK_PATH="${2:-}"; shift 2 ;;
        --lock=*)      LOCK_PATH="${1#*=}"; shift ;;
        --repo-root)   REPO_ROOT="${2:-}"; shift 2 ;;
        --repo-root=*) REPO_ROOT="${1#*=}"; shift ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage; exit "$EXIT_OK" ;;
        *) die 'config_invalid' "unknown argument: $1" \
               "{\"argument\":\"$(json_escape "$1")\"}" "$EXIT_USAGE" ;;
    esac
done

if [[ -z "$PY" ]]; then
    die 'blocked' \
        'no Python interpreter found (python3 or python) for hashing and JSON work.' \
        '{}' "$EXIT_BLOCKED"
fi

# Repository root from this script's location, unless the caller overrides it.
if [[ -z "$REPO_ROOT" ]]; then
    REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
if [[ ! -d "$REPO_ROOT/src/unipad" ]]; then
    die 'config_invalid' \
        "repository root not found (expected src/unipad under $REPO_ROOT)." \
        "{\"repo_root\":\"$(json_escape "$REPO_ROOT")\"}" "$EXIT_USAGE"
fi
[[ -n "$LOCK_PATH" ]] || LOCK_PATH="$REPO_ROOT/config/sources.lock.json"

[[ -n "$IPSW" ]]  || die 'config_invalid' '--ipsw is required.' '{}' "$EXIT_USAGE"
[[ -n "$OUT_DIR" ]] || die 'config_invalid' '--out is required.' '{}' "$EXIT_USAGE"

# ---------------------------------------------------------------------------
# validate the output location (PRD 12 / 8.2: never write into the tracked tree)
# ---------------------------------------------------------------------------
# Normalise both sides: resolve symlinks and "..", so a path that merely looks
# outside the repo cannot sneak into it.
abs_path() { # abs_path DIR -- absolute, symlink-resolved, without requiring existence
    local target="$1"
    if [[ -d "$target" ]]; then
        (cd -- "$target" && pwd -P)
        return 0
    fi
    local parent leaf
    parent="$(dirname -- "$target")"
    leaf="$(basename -- "$target")"
    if [[ -d "$parent" ]]; then
        printf '%s/%s\n' "$(cd -- "$parent" && pwd -P)" "$leaf"
        return 0
    fi
    printf '%s\n' "$target"
}

REPO_ROOT_ABS="$(abs_path "$REPO_ROOT")"
OUT_DIR_ABS="$(abs_path "$OUT_DIR")"
PRIVATE_DIR_ABS="$REPO_ROOT_ABS/local-private"

if [[ "$OUT_DIR_ABS" == "$REPO_ROOT_ABS" ]]; then
    die 'config_invalid' \
        "--out must not be the repository root." \
        "{\"out\":\"$(json_escape "$OUT_DIR_ABS")\",\"repo_root\":\"$(json_escape "$REPO_ROOT_ABS")\"}" \
        "$EXIT_USAGE"
fi
case "$OUT_DIR_ABS" in
    "$PRIVATE_DIR_ABS"|"$PRIVATE_DIR_ABS"/*)
        # The only in-repo exception: .gitignore excludes it and PRD 12 puts
        # exactly this material there.  Never committed, never published.
        info "output is inside local-private/ (gitignored, PRD 12); nothing is committed."
        ;;
    "$REPO_ROOT_ABS"|"$REPO_ROOT_ABS"/*)
        die 'config_invalid' \
            "--out is inside the git-tracked repository tree; use a directory outside the repo, or <repo>/local-private/." \
            "{\"out\":\"$(json_escape "$OUT_DIR_ABS")\",\"repo_root\":\"$(json_escape "$REPO_ROOT_ABS")\"}" \
            "$EXIT_USAGE"
        ;;
esac

# ---------------------------------------------------------------------------
# 1. validate the IPSW against the pinned hash
# ---------------------------------------------------------------------------
step 'validate inputs'

if [[ ! -f "$LOCK_PATH" ]]; then
    die 'config_invalid' "sources lock file not found: $LOCK_PATH" \
        "{\"lock\":\"$(json_escape "$LOCK_PATH")\"}" "$EXIT_USAGE"
fi
if [[ ! -f "$IPSW" ]]; then
    die 'image_not_found' "IPSW not found: $IPSW" \
        "{\"ipsw\":\"$(json_escape "$IPSW")\"}" "$EXIT_FAILURE"
fi

# All lock fields are read in ONE interpreter invocation: a per-field fork is
# slower and, on hosts with fragile process creation, silently produced an empty
# value that turned a valid lock into a refusal.
LOCK_FROZEN=''
PINNED_SHA256=''
PINNED_NAME=''
read_lock_fields() { # prints KEY<TAB>VALUE per line
    "$PY" - "$LOCK_PATH" <<'PYEOF' 2>/dev/null || printf 'frozen\t\nipsw_sha256\t\nipsw_name\t\n'
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as handle:
        root = json.load(handle)
except Exception:
    sys.exit(0)
for name, path in (('frozen', ('frozen',)),
                   ('ipsw_sha256', ('ipsw', 'sha256')),
                   ('ipsw_name', ('ipsw', 'name'))):
    node = root
    for key in path:
        if not isinstance(node, dict) or key not in node:
            node = None
            break
        node = node[key]
    if node is None:
        value = ''
    elif isinstance(node, bool):
        value = 'true' if node else 'false'
    else:
        value = str(node)
    print('%s\t%s' % (name, value.replace('\t', ' ').replace('\n', ' ')))
PYEOF
}

IPSW_SHA256="$("$PY" - "$IPSW" <<'PYEOF' || die 'verification_failed' 'could not read the IPSW to hash it.' '{}' "$EXIT_FAILURE"
import hashlib, sys
digest = hashlib.sha256()
with open(sys.argv[1], 'rb') as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b''):
        digest.update(chunk)
print(digest.hexdigest())
PYEOF
)"
if [[ ! "$IPSW_SHA256" =~ $SHA256_PATTERN ]]; then
    die 'verification_failed' "could not compute the SHA-256 of $IPSW." \
        "{\"ipsw\":\"$(json_escape "$IPSW")\"}" "$EXIT_FAILURE"
fi
info "ipsw:     $IPSW"
info "sha256:   $IPSW_SHA256"
info "bytes:    $(wc -c <"$IPSW" | tr -d ' ')"

local_key=''
local_value=''
while IFS="$(printf '\t')" read -r local_key local_value; do
    # A Windows interpreter writes CRLF on a pipe; strip it so a pinned hash is
    # never compared against itself plus a carriage return.
    local_value="${local_value%$'\r'}"
    case "$local_key" in
        frozen)      LOCK_FROZEN="$local_value" ;;
        ipsw_sha256) PINNED_SHA256="$local_value" ;;
        ipsw_name)   PINNED_NAME="$local_value" ;;
    esac
done < <(read_lock_fields)

if [[ "$LOCK_FROZEN" != 'true' ]]; then
    die 'config_invalid' \
        "sources lock is not frozen (frozen='$LOCK_FROZEN'); an unpinned IPSW cannot be reproduced (PRD 8.1)." \
        "{\"lock\":\"$(json_escape "$LOCK_PATH")\",\"frozen\":\"$(json_escape "$LOCK_FROZEN")\"}" \
        "$EXIT_USAGE"
fi

if [[ -z "$PINNED_SHA256" ]]; then
    die 'config_invalid' \
        "ipsw.sha256 is null in $LOCK_PATH; refusing to prepare an unpinned image (PRD 8.1)." \
        "{\"lock\":\"$(json_escape "$LOCK_PATH")\",\"ipsw_name\":\"$(json_escape "$PINNED_NAME")\"}" \
        "$EXIT_USAGE"
fi

# Case-insensitive compare without ${var,,} (bash 4+ only; macOS ships 3.2).
if [[ "$(printf '%s' "$IPSW_SHA256" | tr 'A-Z' 'a-z')" != "$(printf '%s' "$PINNED_SHA256" | tr 'A-Z' 'a-z')" ]]; then
    # Hashes are not secrets, so both are printed (PRD 7.4: show the evidence).
    die 'image_hash_mismatch' \
        "IPSW SHA-256 does not match the pinned hash in $LOCK_PATH." \
        "{\"ipsw\":\"$(json_escape "$IPSW")\",\"actual\":\"$IPSW_SHA256\",\"expected\":\"$PINNED_SHA256\"}" \
        "$EXIT_FAILURE"
fi
info "pinned:   $PINNED_SHA256 (matches)"

# ---------------------------------------------------------------------------
# 2. extract (macOS tooling) -- stubbed
# ---------------------------------------------------------------------------
step 'extract'

if (( DRY_RUN )); then
    info 'dry-run: nothing was written and nothing was extracted.'
    info "plan: expand $IPSW into $OUT_DIR_ABS"
    info 'plan: write image-manifest.json with one SHA-256 per produced file'
    printf '{\n  "command": "prepare-image",\n  "dry_run": true,\n  "ipsw": "%s",\n  "ipsw_sha256": "%s",\n  "pinned_sha256": "%s",\n  "out": "%s",\n  "generated_at": "%s"\n}\n' \
        "$(json_escape "$IPSW")" "$IPSW_SHA256" "$PINNED_SHA256" \
        "$(json_escape "$OUT_DIR_ABS")" "$(utc_stamp)"
    exit "$EXIT_OK"
fi

if [[ -e "$OUT_DIR_ABS" && ! -d "$OUT_DIR_ABS" ]]; then
    die 'config_invalid' "output path exists and is not a directory: $OUT_DIR_ABS" \
        "{\"out\":\"$(json_escape "$OUT_DIR_ABS")\"}" "$EXIT_USAGE"
fi
if ! mkdir -p -- "$OUT_DIR_ABS" 2>/dev/null; then
    die 'blocked' "cannot create the output directory: $OUT_DIR_ABS" \
        "{\"out\":\"$(json_escape "$OUT_DIR_ABS")\"}" "$EXIT_BLOCKED"
fi

info "expanding $(basename -- "$IPSW") into $OUT_DIR_ABS"

# TODO(macos-verified-only): the expansion steps depend on the pinned IPSW
# layout and on Apple tooling (IPSW payload expansion, Apple disk images, and
# the RootFS assembly).  They can only be written and verified on a real macOS
# host, so this is a stub: nothing is extracted here and no RootFS is produced.
# The host-side verification of the result (M6/M7) is a separate Windows-side
# gate and must never be claimed from this script.
info 'STUB: IPSW expansion and RootFS assembly are TODO(macos-verified-only).'
info 'STUB: no image was produced; nothing downstream may treat this as a RootFS.'

# ---------------------------------------------------------------------------
# 3. image manifest -- one SHA-256 per produced file
# ---------------------------------------------------------------------------
step 'image manifest'

# Walk the output directory, excluding this script's own metadata files so a
# re-run does not hash the previous run's manifest and stage result (which are
# about to be overwritten, so their hashes would go stale immediately).
# Emits image-manifest.json in the canonical form of unipad.models.write_json.
OWN_METADATA='image-manifest.json stage-result.json'
"$PY" - "$OUT_DIR_ABS" "$IPSW_SHA256" "$PINNED_SHA256" "$OWN_METADATA" <<'PYEOF'
import hashlib
import json
import os
import sys

out_dir, ipsw_sha256, pinned_sha256, own_metadata = sys.argv[1:5]
skip = set(own_metadata.split())

entries = []
for root, dirs, files in os.walk(out_dir):
    dirs.sort()
    for name in sorted(files):
        if name in skip:
            continue
        path = os.path.join(root, name)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
        entries.append({
            'path': os.path.relpath(path, out_dir).replace(os.sep, '/'),
            'sha256': digest.hexdigest(),
            'size_bytes': os.path.getsize(path),
        })

manifest = {
    'schema_version': 1,
    'source_ipsw_sha256': ipsw_sha256,
    'pinned_ipsw_sha256': pinned_sha256,
    'extraction': 'TODO(macos-verified-only) stub; no RootFS was produced',
    'entries': entries,
}

with open(os.path.join(out_dir, 'image-manifest.json'), 'w', encoding='utf-8') as handle:
    handle.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n')

print('prepare-image: image manifest written (%d files)' % len(entries))
PYEOF

# ---------------------------------------------------------------------------
# stage result -- always explicit that the extraction did not run
# ---------------------------------------------------------------------------
step 'stage result'
"$PY" - "$OUT_DIR_ABS" "$IPSW" "$IPSW_SHA256" "$PINNED_SHA256" <<'PYEOF'
import json
import os
import sys

out_dir, ipsw, ipsw_sha256, pinned_sha256 = sys.argv[1:5]
stage = {
    'command': 'prepare-image',
    'status': 'stubbed',
    'ok': False,
    'ipsw': os.path.basename(ipsw),
    'ipsw_sha256': ipsw_sha256,
    'pinned_sha256': pinned_sha256,
    'hash_match': True,
    'steps': [
        {'name': 'validate-inputs', 'status': 'ran',
         'reason': 'IPSW exists and matches the pinned hash'},
        {'name': 'extract', 'status': 'not_run',
         'reason': 'TODO(macos-verified-only)'},
        {'name': 'image-manifest', 'status': 'ran',
         'reason': 'hashes of whatever is in the output directory'},
    ],
    'note': ('A stubbed stage never reports success. The Apple-tool extraction '
             'is UNVERIFIED from Windows; no RootFS was produced.'),
}
with open(os.path.join(out_dir, 'stage-result.json'), 'w', encoding='utf-8') as handle:
    handle.write(json.dumps(stage, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
print('prepare-image: stage-result.json written (status=stubbed, ok=false)')
PYEOF

step 'summary'
info "ipsw:      $IPSW"
info "sha256:    $IPSW_SHA256 (matches the pinned hash)"
info "out:       $OUT_DIR_ABS"
info "manifest:  $OUT_DIR_ABS/image-manifest.json"
info 'RESULT: inputs validated. Extraction is a TODO(macos-verified-only) stub;'
info '        no RootFS was produced and no gate may be claimed from this run.'

exit "$EXIT_OK"
