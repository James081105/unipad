#!/usr/bin/env bash
# build-macos.sh -- UniPad macOS build entry point (PRD 8.1, 8.2, 8.3).
#
# WHY THIS EXISTS
#   The UniXNU kernel / boot chain can only be produced on a macOS host: Xcode,
#   the Apple SDKs and Apple clang do not exist anywhere else (PRD 8.1 "工具信息").
#   The Windows box runs QEMU TCG and never builds; it only downloads, verifies
#   and runs what this entry point produces.
#
# WHAT IT DOES
#   preflight -> prepare-sources -> build stages -> verify -> manifest.
#   Every step is echoed, every step is recorded in build-report.json, and the
#   machine-readable artifact-manifest.json is written in the exact canonical
#   form of unipad.models.ArtifactManifest (sort_keys=True, indent=2, trailing
#   newline) so the Windows verifier consumes it without translation.
#
# WHAT IT REFUSES TO DO
#   * build from a lock file that is not frozen, or whose commits are null
#     (PRD 8.1: "Every run must be reproducible from this file plus the
#     recorded hashes");
#   * mix HVF and TCG: an HVF-targeted payload must never be produced for, or
#     downloaded by, this Intel Windows host (PRD 8.3);
#   * fetch anything over the network.  No curl, no wget, no git fetch, no
#     package manager.  Pinned sources are verified locally or reported absent;
#   * depend on qemu-system-aarch64.  QEMU is a *native Windows* build
#     (config/sources.lock.json tools.qemu.build) and is deliberately not a
#     macOS build input;
#   * report success without evidence: a step that did not run is recorded as
#     not_run with a reason (PRD 7.4).
#
# STATUS: the Apple-tool invocations are TODO(macos-verified-only) stubs.  The
#   contract around them (inputs, tools, hashes, target, paths, exit codes,
#   manifest shape) is real and enforced here, and is verifiable on any host.
#
# Exit codes mirror unipad.errors.ExitCode: 0 ok, 1 failure, 2 usage, 3 blocked.
set -euo pipefail

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
readonly SCRIPT_NAME='build-macos.sh'
readonly EXIT_OK=0
readonly EXIT_FAILURE=1
readonly EXIT_USAGE=2
readonly EXIT_BLOCKED=3

# Stage numbers understood by this entry.  M0..M15 per docs/PRD.md; the mapping
# below only decides which build steps are *planned* for a given milestone.
readonly STAGE_PATTERN='^M(0|[1-9][0-9]?)$'
readonly EXPERIMENT_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
readonly COMMIT_PATTERN='^[0-9a-fA-F]{7,64}$'
readonly SHA256_PATTERN='^[0-9a-fA-F]{64}$'

# A kernel + boot chain + devicetree build comfortably fits in 8 GiB; the
# preflight of PRD 8.1 explicitly asks for a space check.
readonly DEFAULT_MIN_FREE_GB=8

# Field separator for the artifact records handed to the manifest writer.
# ASCII unit separator cannot appear in a path a shell can pass around, and a
# newline record separator is what the writer splits on.
readonly FIELD_SEP=$'\037'

# Resolve a Python interpreter: python3 on macOS, python on Windows/Git Bash.
# The Apple-tool build steps are macOS-only stubs; the contract around them
# (inputs, hashes, manifest bytes) is verifiable on any host with Python.
if command -v python3 >/dev/null 2>&1; then PY='python3'
elif command -v python  >/dev/null 2>&1; then PY='python'
else PY=''; fi

# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

log()  { printf '%s %s\n' "$(utc_stamp)" "$*"; }
step() { printf '\n=== %s ===\n' "$*"; }

utc_stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# json_escape TEXT -- emit TEXT as a JSON string body (RFC 8259).
# Pure bash on purpose: the error path must work even when python3 is missing.
json_escape() {
    local text="$1" out='' ch i code
    for (( i = 0; i < ${#text}; i++ )); do
        ch="${text:i:1}"
        case "$ch" in
            '\')   out+='\\' ;;
            '"')   out+='\"' ;;
            $'\n') out+='\n' ;;
            $'\r') out+='\r' ;;
            $'\t') out+='\t' ;;
            *)
                code=$(printf '%d' "'$ch")
                if (( code < 32 )); then
                    out+=$(printf '\\u%04x' "$code")
                else
                    out+="$ch"
                fi
                ;;
        esac
    done
    printf '%s' "$out"
}

# json_string_array ITEM... -- emit ["a","b"].  Used for the machine-readable
# error details so a missing-tool list stays valid JSON.
json_string_array() {
    local first=1 item
    printf '['
    for item in "$@"; do
        (( first )) || printf ','
        first=0
        printf '"%s"' "$(json_escape "$item")"
    done
    printf ']'
}

# emit_error CODE MESSAGE [DETAIL_JSON]
# Always a machine-readable object on stderr; never a bare string.
emit_error() {
    local code="$1" message="$2" details="${3:-{\}}"
    printf '{"error":{"code":"%s","message":"%s","details":%s}}\n' \
        "$(json_escape "$code")" "$(json_escape "$message")" "$details" >&2
}

die() { # die CODE MESSAGE [DETAIL_JSON] EXIT
    local code="$1" message="$2" details="${3:-{\}}" exit_code="$4"
    emit_error "$code" "$message" "$details"
    log "FAILED [$code] $message"
    exit "$exit_code"
}

usage() {
    cat <<'EOF'
build-macos.sh -- UniPad macOS build entry (PRD 8.1 / 8.2 / 8.3)

Usage:
  build-macos.sh --stage <M0|M1|...> --experiment <id> [options]

Required:
  --stage <M0|M1|...>      Milestone whose build steps to plan and run.
  --experiment <id>        Experiment identifier (also the run directory name).

Options:
  --target <tcg|hvf>       Payload target.  Default tcg.  This Intel Windows
                           host consumes TCG only; hvf is refused unless the
                           lock file says so explicitly (PRD 8.3).
  --lock <path>            sources.lock.json.  Default config/sources.lock.json.
  --out <dir>              Output directory for artifacts and manifests.
                           Default <repo>/scripts/macos/build-out/<run-id>.
  --run-id <id>            Override the generated run id.
  --include <path[:role[:arch]]]
                           Declare a deliverable that must exist and be hashed
                           into the manifest.  Repeatable.
  --min-free-gb <n>        Required free space on the output filesystem.
  --skip-sources           Continue when a pinned source checkout is absent.
                           Recorded in build-report.json; the build steps stay
                           not_run.  Off by default (a missing pinned source is
                           a missing input).
  --dry-run                Validate everything and print the plan; write
                           nothing and run nothing.
  -h, --help               This text.

Notes:
  * No network access of any kind: nothing is fetched, cloned or pulled.
  * qemu-system-aarch64 is not required and not used (native Windows build).
  * Apple tool invocations are TODO(macos-verified-only) stubs.
EOF
}

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
STAGE=''
EXPERIMENT=''
TARGET='tcg'
LOCK_PATH=''
OUT_DIR=''
RUN_ID=''
MIN_FREE_GB="$DEFAULT_MIN_FREE_GB"
SKIP_SOURCES=0
DRY_RUN=0
INCLUDES=()

while (( $# > 0 )); do
    case "$1" in
        --stage)          STAGE="${2:-}"; shift 2 ;;
        --stage=*)        STAGE="${1#*=}"; shift ;;
        --experiment)     EXPERIMENT="${2:-}"; shift 2 ;;
        --experiment=*)   EXPERIMENT="${1#*=}"; shift ;;
        --target)         TARGET="${2:-}"; shift 2 ;;
        --target=*)       TARGET="${1#*=}"; shift ;;
        --lock)           LOCK_PATH="${2:-}"; shift 2 ;;
        --lock=*)         LOCK_PATH="${1#*=}"; shift ;;
        --out)            OUT_DIR="${2:-}"; shift 2 ;;
        --out=*)          OUT_DIR="${1#*=}"; shift ;;
        --run-id)         RUN_ID="${2:-}"; shift 2 ;;
        --run-id=*)       RUN_ID="${1#*=}"; shift ;;
        --include)        INCLUDES+=("${2:-}"); shift 2 ;;
        --include=*)      INCLUDES+=("${1#*=}"); shift ;;
        --min-free-gb)    MIN_FREE_GB="${2:-}"; shift 2 ;;
        --min-free-gb=*)  MIN_FREE_GB="${1#*=}"; shift ;;
        --skip-sources)   SKIP_SOURCES=1; shift ;;
        --dry-run)        DRY_RUN=1; shift ;;
        -h|--help)        usage; exit "$EXIT_OK" ;;
        *) die 'config_invalid' "unknown argument: $1" \
               "{\"argument\":\"$(json_escape "$1")\"}" "$EXIT_USAGE" ;;
    esac
done

if [[ -z "$STAGE" ]]; then
    die 'config_invalid' '--stage is required.' '{}' "$EXIT_USAGE"
fi
if [[ ! "$STAGE" =~ $STAGE_PATTERN ]]; then
    die 'config_invalid' "stage must look like M0, M1, ... M15 (got '$STAGE')." \
        "{\"stage\":\"$(json_escape "$STAGE")\"}" "$EXIT_USAGE"
fi
if [[ -z "$EXPERIMENT" ]]; then
    die 'config_invalid' '--experiment is required.' '{}' "$EXIT_USAGE"
fi
if [[ ! "$EXPERIMENT" =~ $EXPERIMENT_PATTERN ]]; then
    die 'config_invalid' \
        "experiment id must match $EXPERIMENT_PATTERN (got '$EXPERIMENT')." \
        "{\"experiment\":\"$(json_escape "$EXPERIMENT")\"}" "$EXIT_USAGE"
fi
case "$TARGET" in
    tcg|hvf) ;;
    *) die 'config_invalid' "target must be tcg or hvf (got '$TARGET')." \
        "{\"target\":\"$(json_escape "$TARGET")\"}" "$EXIT_USAGE" ;;
esac
if [[ ! "$MIN_FREE_GB" =~ ^[0-9]+$ ]]; then
    die 'config_invalid' "--min-free-gb must be an integer (got '$MIN_FREE_GB')." \
        '{}' "$EXIT_USAGE"
fi

# ---------------------------------------------------------------------------
# repository layout
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
if [[ ! -d "$REPO_ROOT/src/unipad" ]]; then
    die 'config_invalid' \
        "repository root not found above $SCRIPT_DIR (expected src/unipad)." \
        "{\"script_dir\":\"$(json_escape "$SCRIPT_DIR")\"}" "$EXIT_USAGE"
fi

if [[ -z "$LOCK_PATH" ]]; then
    LOCK_PATH="$REPO_ROOT/config/sources.lock.json"
fi

STAGE_NUMBER="${STAGE#M}"
if [[ -z "$RUN_ID" ]]; then
    RUN_ID="${EXPERIMENT}-${STAGE}-$(date -u '+%Y%m%dT%H%M%SZ')"
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    die 'config_invalid' "run id must be filesystem-safe (got '$RUN_ID')." \
        "{\"run_id\":\"$(json_escape "$RUN_ID")\"}" "$EXIT_USAGE"
fi

# Default output stays inside this script's own directory so a checkout that is
# only used to validate the contract does not touch anything else.  A real
# macOS runner passes --out pointing at its own workspace.
if [[ -z "$OUT_DIR" ]]; then
    OUT_DIR="$SCRIPT_DIR/build-out/$RUN_ID"
fi

LOG_PATH=''
STAGE_RESULT_PATH=''
REPORT_PATH=''
MANIFEST_PATH=''

# ---------------------------------------------------------------------------
# lock file validation (PRD 8.1 inputs)
# ---------------------------------------------------------------------------
# Prefer Python (a required build tool anyway); fall back to jq.
read_lock_field() { # read_lock_field JSON_PATH
    local path="$1"
    if [[ -n "$PY" ]]; then
        "$PY" - "$LOCK_PATH" "$path" <<'PY' 2>/dev/null || true
import json, sys
path = sys.argv[2].split('.')
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as handle:
        node = json.load(handle)
except Exception:
    sys.exit(0)
for key in path:
    if not isinstance(node, dict) or key not in node:
        sys.exit(0)
    node = node[key]
if node is None:
    print('')
elif isinstance(node, bool):
    print('true' if node else 'false')
else:
    print(node)
PY
        return 0
    fi
    if command -v jq >/dev/null 2>&1; then
        jq -r --arg p "$path" 'getpath($p | split(".")) | if . == null then "" else tostring end' \
            "$LOCK_PATH" 2>/dev/null || true
        return 0
    fi
    printf ''
}

LOCK_FROZEN=''
LOCK_UNIXNU_COMMIT=''
LOCK_DEVICE_COMMIT=''
LOCK_QEMU_ACCEL=''
LOCK_IPSW_SHA256=''

validate_lock() {
    if [[ ! -f "$LOCK_PATH" ]]; then
        die 'config_invalid' "sources lock file not found: $LOCK_PATH" \
            "{\"lock\":\"$(json_escape "$LOCK_PATH")\"}" "$EXIT_USAGE"
    fi
    if [[ -n "$PY" ]]; then
        if ! "$PY" -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
                "$LOCK_PATH" >/dev/null 2>&1; then
            die 'artifact_manifest_invalid' \
                "sources lock file is not valid JSON: $LOCK_PATH" \
                "{\"lock\":\"$(json_escape "$LOCK_PATH")\"}" "$EXIT_FAILURE"
        fi
    elif command -v jq >/dev/null 2>&1; then
        if ! jq -e . "$LOCK_PATH" >/dev/null 2>&1; then
            die 'artifact_manifest_invalid' \
                "sources lock file is not valid JSON: $LOCK_PATH" \
                "{\"lock\":\"$(json_escape "$LOCK_PATH")\"}" "$EXIT_FAILURE"
        fi
    else
        die 'blocked' \
            'neither python3/python nor jq is available to read the lock file.' \
            "{\"lock\":\"$(json_escape "$LOCK_PATH")\"}" "$EXIT_BLOCKED"
    fi

    LOCK_FROZEN="$(read_lock_field frozen)"
    LOCK_UNIXNU_COMMIT="$(read_lock_field unixnu.commit)"
    LOCK_DEVICE_COMMIT="$(read_lock_field device_repo.commit)"
    LOCK_QEMU_ACCEL="$(read_lock_field tools.qemu.accel)"
    LOCK_IPSW_SHA256="$(read_lock_field ipsw.sha256)"

    if [[ "$LOCK_FROZEN" != 'true' ]]; then
        die 'config_invalid' \
            "sources lock is not frozen (frozen=$LOCK_FROZEN). " \
            'Fill every null with evidence and set frozen=true before building (PRD 8.1).' \
            "{\"lock\":\"$(json_escape "$LOCK_PATH")\",\"frozen\":\"$(json_escape "$LOCK_FROZEN")\"}" \
            "$EXIT_USAGE"
    fi

    if [[ ! "$LOCK_UNIXNU_COMMIT" =~ $COMMIT_PATTERN ]]; then
        die 'config_invalid' \
            "unixnu.commit is not a pinned commit in $LOCK_PATH." \
            "{\"unixnu_commit\":\"$(json_escape "$LOCK_UNIXNU_COMMIT")\"}" "$EXIT_USAGE"
    fi
    if [[ ! "$LOCK_DEVICE_COMMIT" =~ $COMMIT_PATTERN ]]; then
        die 'config_invalid' \
            "device_repo.commit is not a pinned commit in $LOCK_PATH." \
            "{\"device_repo_commit\":\"$(json_escape "$LOCK_DEVICE_COMMIT")\"}" "$EXIT_USAGE"
    fi

    # PRD 8.3: HVF payloads must never be produced for / fetched by the TCG
    # host.  The lock file's declared accelerator is the authority.
    if [[ "$TARGET" != "$LOCK_QEMU_ACCEL" ]]; then
        die 'accelerator_not_tcg' \
            "target '$TARGET' contradicts the locked accelerator '$LOCK_QEMU_ACCEL'. " \
            'Refusing to build a payload for the wrong accelerator (PRD 8.3).' \
            "{\"requested_target\":\"$(json_escape "$TARGET")\",\"locked_accel\":\"$(json_escape "$LOCK_QEMU_ACCEL")\"}" \
            "$EXIT_USAGE"
    fi
}

# ---------------------------------------------------------------------------
# tool validation (PRD 8.1 preflight: 验证输入、锁文件、空间和工具版本)
# ---------------------------------------------------------------------------
require_tools() {
    local missing=() tool
    for tool in xcodebuild clang git; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing+=("$tool")
        fi
    done
    if [[ -z "$PY" ]]; then
        missing+=('python3-or-python')
    fi
    if (( ${#missing[@]} > 0 )); then
        die 'blocked' \
            "required macOS build tools are missing: ${missing[*]}" \
            "{\"missing\":$(json_string_array ${missing[@]+"${missing[@]}"}),\"note\":\"this entry point runs on a macOS host only\"}" \
            "$EXIT_BLOCKED"
    fi
    # qemu-system-aarch64 is deliberately NOT required: QEMU is a native
    # Windows build (config/sources.lock.json tools.qemu.build).  Requiring it
    # here would silently make the macOS build depend on a Windows artifact.
    if command -v qemu-system-aarch64 >/dev/null 2>&1; then
        log 'note: qemu-system-aarch64 is on PATH but is not a build input (native Windows build).'
    fi
    TOOL_XCODEBUILD="$(xcodebuild -version 2>/dev/null | head -1 || true)"
    TOOL_CLANG="$(clang --version 2>/dev/null | head -1 || true)"
    TOOL_PYTHON="$("$PY" --version 2>&1 | head -1 || true)"
    TOOL_GIT="$(git --version 2>/dev/null | head -1 || true)"
    [[ -n "$TOOL_XCODEBUILD" ]] || TOOL_XCODEBUILD='unavailable'
    [[ -n "$TOOL_CLANG" ]]      || TOOL_CLANG='unavailable'
    [[ -n "$TOOL_PYTHON" ]]     || TOOL_PYTHON='unavailable'
    [[ -n "$TOOL_GIT" ]]        || TOOL_GIT='unavailable'
}

# ---------------------------------------------------------------------------
# sha256 helper
# ---------------------------------------------------------------------------
SHA_CMD=()
detect_sha() {
    if command -v sha256sum >/dev/null 2>&1; then
        SHA_CMD=(sha256sum)
    elif command -v shasum >/dev/null 2>&1; then
        SHA_CMD=(shasum -a 256)
    elif command -v openssl >/dev/null 2>&1; then
        SHA_CMD=(openssl dgst -sha256 -r)
    else
        die 'blocked' 'no SHA-256 tool found (sha256sum / shasum / openssl).' '{}' "$EXIT_BLOCKED"
    fi
}

sha256_of() { # sha256_of FILE -> lowercase hex on stdout
    local file="$1" out
    out="$("${SHA_CMD[@]}" "$file" 2>/dev/null | awk '{print $1}' | tr 'A-F' 'a-f')" || out=''
    printf '%s' "$out"
}

# ---------------------------------------------------------------------------
# output directory
# ---------------------------------------------------------------------------
prepare_output_dir() {
    if [[ -e "$OUT_DIR" && ! -d "$OUT_DIR" ]]; then
        die 'config_invalid' "output path exists and is not a directory: $OUT_DIR" \
            "{\"out\":\"$(json_escape "$OUT_DIR")\"}" "$EXIT_USAGE"
    fi
    if (( DRY_RUN == 0 )); then
        if ! mkdir -p -- "$OUT_DIR" 2>/dev/null; then
            die 'blocked' "cannot create output directory: $OUT_DIR" \
                "{\"out\":\"$(json_escape "$OUT_DIR")\"}" "$EXIT_BLOCKED"
        fi
    fi
    LOG_PATH="$OUT_DIR/build.log"
    REPORT_PATH="$OUT_DIR/build-report.json"
    MANIFEST_PATH="$OUT_DIR/artifact-manifest.json"
    STAGE_RESULT_PATH="$OUT_DIR/stage-result.json"
}

check_free_space() {
    local free_kb free_gb
    free_kb="$(df -Pk -- "$OUT_DIR" 2>/dev/null | awk 'NR==2 {print $4}')" || free_kb=''
    if [[ ! "$free_kb" =~ ^[0-9]+$ ]]; then
        log 'warning: free space on the output filesystem could not be determined.'
        return 0
    fi
    free_gb=$(( free_kb / 1024 / 1024 ))
    if (( free_gb < MIN_FREE_GB )); then
        die 'blocked' \
            "not enough free space on $OUT_DIR: ${free_gb} GiB free, ${MIN_FREE_GB} GiB required." \
            "{\"free_gb\":$free_gb,\"required_gb\":$MIN_FREE_GB,\"out\":\"$(json_escape "$OUT_DIR")\"}" \
            "$EXIT_BLOCKED"
    fi
    log "free space: ${free_gb} GiB (required ${MIN_FREE_GB} GiB)"
}

# ---------------------------------------------------------------------------
# step ledger
# ---------------------------------------------------------------------------
# Each entry: name|status|reason   (status: ran | not_run | skipped)
LEDGER=()

ledger_add() { LEDGER+=("$1|$2|$3"); }

ledger_json() {
    local first=1 entry name status reason
    printf '['
    for entry in ${LEDGER[@]+"${LEDGER[@]}"}; do
        name="${entry%%|*}"
        status="${entry#*|}"; status="${status%%|*}"
        reason="${entry##*|}"
        (( first )) || printf ','
        first=0
        printf '{"name":"%s","status":"%s","reason":"%s"}' \
            "$(json_escape "$name")" "$(json_escape "$status")" "$(json_escape "$reason")"
    done
    printf ']'
}

# ---------------------------------------------------------------------------
# stage plan
# ---------------------------------------------------------------------------
# Prints the planned step names for a milestone, one per line.
plan_steps() {
    local n="$1"
    printf 'preflight\n'
    if (( n >= 1 )); then printf 'prepare-sources\n'; fi
    if (( n >= 2 )); then printf 'build-kernel\n'; fi
    if (( n >= 3 )); then printf 'build-bootloader\n'; fi
    if (( n >= 4 )); then printf 'build-devicetree\n'; fi
    if (( n >= 9 )); then printf 'assemble-image\n'; fi
    printf 'verify-artifacts\n'
    printf 'write-manifest\n'
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    step "preflight"
    log "run_id:    $RUN_ID"
    log "stage:     $STAGE"
    log "experiment:$EXPERIMENT"
    log "target:    $TARGET"
    log "lock:      $LOCK_PATH"
    log "out:       $OUT_DIR"
    if (( DRY_RUN )); then log 'mode:      dry-run (nothing written, nothing built)'; fi

    validate_lock
    log "lock: frozen=true unixnu=$LOCK_UNIXNU_COMMIT device_repo=$LOCK_DEVICE_COMMIT"
    log "lock: qemu accel=$LOCK_QEMU_ACCEL ipsw.sha256=${LOCK_IPSW_SHA256:-<empty>}"

    require_tools
    log "tools: $TOOL_XCODEBUILD"
    log "tools: $TOOL_CLANG"
    log "tools: $TOOL_PYTHON"
    log "tools: $TOOL_GIT"

    detect_sha

    prepare_output_dir
    if (( DRY_RUN == 0 )); then
        check_free_space
    fi

    # --- declared deliverables --------------------------------------------
    # Each --include is an input that must exist; a missing one is a failure,
    # never a silently empty manifest (PRD 7.4).
    local -a inc_paths=() inc_roles=() inc_archs=()
    local spec path role arch
    for spec in ${INCLUDES[@]+"${INCLUDES[@]}"}; do
        path="$spec"; role=''; arch=''
        IFS=':' read -r path role arch <<<"$spec"
        if [[ -z "$path" ]]; then
            die 'config_invalid' "--include needs a path." '{}' "$EXIT_USAGE"
        fi
        # Relative include paths resolve against the repository root.
        if [[ "$path" != /* ]]; then
            path="$REPO_ROOT/$path"
        fi
        if [[ ! -f "$path" ]]; then
            die 'artifact_missing' "declared artifact is missing: $path" \
                "{\"include\":\"$(json_escape "$spec")\"}" "$EXIT_FAILURE"
        fi
        if [[ -z "$role" ]]; then
            case "$path" in
                *kernelcache*|*kernel*) role='kernel' ;;
                *devicetree*|*.dtb|*.dts) role='devicetree' ;;
                *boot*|*loader*|*iboot*)  role='bootloader' ;;
                *)                        role='auxiliary' ;;
            esac
        fi
        [[ -n "$arch" ]] || arch='aarch64'
        inc_paths+=("$path"); inc_roles+=("$role"); inc_archs+=("$arch")
    done

    # --- plan --------------------------------------------------------------
    step "plan"
    # mapfile is bash 4+; macOS ships /bin/bash 3.2, so read the plan in a loop.
    local -a steps=()
    local s
    while IFS= read -r s; do
        [[ -n "$s" ]] && steps+=("$s")
    done < <(plan_steps "$STAGE_NUMBER")
    local idx
    for (( idx = 0; idx < ${#steps[@]}; idx++ )); do
        printf '  %s\n' "${steps[$idx]}"
    done
    ledger_add 'preflight' 'ran' 'validated inputs, lock, tools and space'

    if (( DRY_RUN )); then
        for (( idx = 0; idx < ${#steps[@]}; idx++ )); do
            [[ "${steps[$idx]}" == 'preflight' ]] && continue
            ledger_add "${steps[$idx]}" 'not_run' 'dry-run'
        done
        printf '\n'
        log "dry-run complete; run id would be $RUN_ID"
        printf '{\n  "run_id": "%s",\n  "stage": "%s",\n  "target": "%s",\n  "dry_run": true,\n  "planned_steps": [' \
            "$(json_escape "$RUN_ID")" "$(json_escape "$STAGE")" "$(json_escape "$TARGET")"
        local first=1
        for (( idx = 0; idx < ${#steps[@]}; idx++ )); do
            (( first )) || printf ', '
            first=0
            printf '"%s"' "$(json_escape "${steps[$idx]}")"
        done
        printf ']\n}\n'
        exit "$EXIT_OK"
    fi

    # --- run the plan ------------------------------------------------------
    local sources_present=0
    for (( idx = 0; idx < ${#steps[@]}; idx++ )); do
        s="${steps[$idx]}"
        case "$s" in
            preflight)
                # already recorded
                ;;
            prepare-sources)
                step "prepare-sources"
                # No network access, ever.  The pinned checkouts are verified
                # locally; an absent one is a missing input unless the caller
                # explicitly opts out.
                local vendor_dir
                for vendor_dir in "$REPO_ROOT/vendor/UniXNU" "$REPO_ROOT/vendor/UniXNU-qemu"; do
                    if [[ -d "$vendor_dir/.git" ]]; then
                        log "source present: $vendor_dir"
                        sources_present=1
                    else
                        log "source absent:  $vendor_dir"
                    fi
                done
                if (( sources_present == 0 )) && (( SKIP_SOURCES == 0 )); then
                    ledger_add "$s" 'not_run' 'pinned source checkout absent'
                    emit_error 'artifact_missing' \
                        'no pinned source checkout found under vendor/; nothing was fetched (no network access by policy).' \
                        "{\"expected\":[\"vendor/UniXNU\",\"vendor/UniXNU-qemu\"],\"hint\":\"pass --skip-sources to validate the contract only\"}"
                    write_outputs 0 0
                    exit "$EXIT_FAILURE"
                fi
                if (( sources_present == 0 )); then
                    log 'note: --skip-sources given; build steps will stay not_run.'
                    ledger_add "$s" 'not_run' 'pinned source checkout absent (--skip-sources)'
                else
                    ledger_add "$s" 'ran' 'pinned source checkouts verified locally'
                fi
                ;;
            build-kernel|build-bootloader|build-devicetree|assemble-image)
                step "$s"
                log 'TODO(macos-verified-only): the Apple toolchain invocation for this'
                log 'step can only be written and verified on a real macOS runner.'
                log 'Nothing was built and no artifact was produced here.'
                ledger_add "$s" 'not_run' 'TODO(macos-verified-only)'
                ;;
            verify-artifacts)
                step 'verify-artifacts'
                if (( ${#inc_paths[@]} == 0 )); then
                    log 'no declared deliverables; the manifest will list only'
                    log 'the metadata files this script itself writes.'
                fi
                ledger_add "$s" 'ran' 'declared deliverables checked for existence'
                ;;
            write-manifest)
                step 'write-manifest'
                ledger_add "$s" 'ran' 'artifact-manifest.json written'
                ;;
        esac
    done

    write_outputs 1 "${#inc_paths[@]}"

    step "summary"
    log "run_id:  $RUN_ID"
    log "out:     $OUT_DIR"
    log "log:     $LOG_PATH"
    log "report:  $REPORT_PATH"
    log "manifest:$MANIFEST_PATH"
    log 'NOTE: this is a stubbed build.  No firmware, bootloader, kernel or'
    log '      device tree was produced.  Do not treat it as a build result.'

    exit "$EXIT_OK"
}

# write_outputs SOURCES_OK DECLARED_COUNT
# Writes build.log content already produced by log(), build-report.json and
# artifact-manifest.json in the canonical form of unipad.models.write_json.
write_outputs() {
    local sources_ok="$1" declared_count="$2"
    local i sha size leaf

    # --- artifact entries --------------------------------------------------
    # Declared deliverables are hashed here, one record per file, so the
    # manifest writer only has to serialise them.
    local -a records=()
    for (( i = 0; i < ${#inc_paths[@]}; i++ )); do
        sha="$(sha256_of "${inc_paths[$i]}")"
        if [[ ! "$sha" =~ $SHA256_PATTERN ]]; then
            die 'verification_failed' \
                "could not compute the SHA-256 of ${inc_paths[$i]}." \
                "{\"path\":\"$(json_escape "${inc_paths[$i]}")\"}" "$EXIT_FAILURE"
        fi
        size="$(wc -c <"${inc_paths[$i]}" | tr -d ' ')"
        leaf="$(basename -- "${inc_paths[$i]}")"
        records+=("${leaf}${FIELD_SEP}${sha}${FIELD_SEP}${size}${FIELD_SEP}${inc_roles[$i]}${FIELD_SEP}${inc_archs[$i]}${FIELD_SEP}${leaf}")
        log "artifact: $leaf sha256=$sha bytes=$size role=${inc_roles[$i]} arch=${inc_archs[$i]}"
    done

    # --- build-report.json -------------------------------------------------
    {
        printf '{\n'
        printf '  "command": "build-macos",\n'
        printf '  "run_id": "%s",\n'        "$(json_escape "$RUN_ID")"
        printf '  "stage": "%s",\n'         "$(json_escape "$STAGE")"
        printf '  "experiment": "%s",\n'    "$(json_escape "$EXPERIMENT")"
        printf '  "target": "%s",\n'        "$(json_escape "$TARGET")"
        printf '  "generated_at": "%s",\n'  "$(utc_stamp)"
        printf '  "build_mode": "stubbed",\n'
        printf '  "stubbed_steps_are_macos_verified_only": true,\n'
        printf '  "network_access": "none",\n'
        printf '  "lock": {\n'
        printf '    "path": "%s",\n'        "$(json_escape "$LOCK_PATH")"
        printf '    "frozen": %s,\n'        "$([[ "$LOCK_FROZEN" == 'true' ]] && printf 'true' || printf 'false')"
        printf '    "unixnu_commit": "%s",\n'    "$(json_escape "$LOCK_UNIXNU_COMMIT")"
        printf '    "device_repo_commit": "%s",\n' "$(json_escape "$LOCK_DEVICE_COMMIT")"
        printf '    "qemu_accel": "%s"\n'        "$(json_escape "$LOCK_QEMU_ACCEL")"
        printf '  },\n'
        printf '  "toolchain": {\n'
        printf '    "xcodebuild": "%s",\n'  "$(json_escape "$TOOL_XCODEBUILD")"
        printf '    "clang": "%s",\n'       "$(json_escape "$TOOL_CLANG")"
        printf '    "python3": "%s",\n'     "$(json_escape "$TOOL_PYTHON")"
        printf '    "git": "%s",\n'         "$(json_escape "$TOOL_GIT")"
        printf '    "qemu": "not a macOS build input (native Windows build)"\n'
        printf '  },\n'
        printf '  "steps": %s,\n' "$(ledger_json)"
        printf '  "declared_artifacts": %s,\n' "$declared_count"
        printf '  "sources_present": %s,\n' "$([[ "$sources_ok" == '1' ]] && printf 'true' || printf 'false')"
        printf '  "evidence_note": "steps with status not_run produced nothing; missing evidence is unverified, not success (PRD 7.4)."\n'
        printf '}\n'
    } >"$REPORT_PATH"

    # --- stage-result.json -------------------------------------------------
    # One machine-readable stage result, as PRD 8.1 "报告" requires.
    {
        printf '{\n'
        printf '  "stage": "%s",\n'      "$(json_escape "$STAGE")"
        printf '  "run_id": "%s",\n'     "$(json_escape "$RUN_ID")"
        printf '  "status": "stubbed",\n'
        printf '  "ok": false,\n'
        printf '  "generated_at": "%s",\n' "$(utc_stamp)"
        printf '  "steps": %s,\n' "$(ledger_json)"
        printf '  "not_run_reasons": ["TODO(macos-verified-only)", "pinned source checkout absent (--skip-sources)"],\n'
        printf '  "note": "A stubbed stage never reports success. Real Apple-tool work is UNVERIFIED from Windows."\n'
        printf '}\n'
    } >"$STAGE_RESULT_PATH"

    # --- artifact-manifest.json -------------------------------------------
    # Written through Python so the bytes match unipad.models.write_json
    # (ensure_ascii=False, sort_keys=True, indent=2, trailing newline) exactly.
    # Records arrive on stdin, one per artifact, fields separated by US (0x1f):
    #   name <US> sha256 <US> size_bytes <US> role <US> arch <US> path
    printf '%s\n' ${records[@]+"${records[@]}"} | write_manifest
    if [[ ! -s "$MANIFEST_PATH" ]]; then
        die 'evidence_incomplete' "artifact manifest was not written: $MANIFEST_PATH" \
            "{\"manifest\":\"$(json_escape "$MANIFEST_PATH")\"}" "$EXIT_FAILURE"
    fi
    if [[ -n "$PY" ]]; then
        if ! "$PY" -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
                "$MANIFEST_PATH" >/dev/null 2>&1; then
            die 'artifact_manifest_invalid' \
                "artifact manifest is not valid JSON: $MANIFEST_PATH" \
                "{\"manifest\":\"$(json_escape "$MANIFEST_PATH")\"}" "$EXIT_FAILURE"
        fi
    fi
}

# write_manifest -- read artifact records on stdin, write MANIFEST_PATH.
# The shape is unipad.models.ArtifactManifest exactly: run_id, artifacts[],
# target, unixnu_commit, device_repo_commit, toolchain.
write_manifest() {
    [[ -n "$PY" ]] || die 'blocked' 'no Python interpreter for the manifest writer.' \
        '{}' "$EXIT_BLOCKED"
    "$PY" - "$MANIFEST_PATH" "$RUN_ID" "$TARGET" \
        "$LOCK_UNIXNU_COMMIT" "$LOCK_DEVICE_COMMIT" \
        "$TOOL_XCODEBUILD" "$TOOL_CLANG" "$TOOL_PYTHON" "$TOOL_GIT" <<'PYEOF'
import json
import sys

sep = "\037"
(manifest_path, run_id, target, unixnu_commit, device_repo_commit,
 xcodebuild, clang, python3, git) = sys.argv[1:10]

artifacts = []
for line in sys.stdin.read().split("\n"):
    if not line:
        continue
    name, sha256, size_bytes, role, arch, path = line.split(sep)
    artifacts.append({
        "name": name,
        "sha256": sha256,
        "size_bytes": int(size_bytes),
        "arch": arch if arch else None,
        "role": role if role else None,
        "path": path,
    })

manifest = {
    "run_id": run_id,
    "artifacts": artifacts,
    "target": target,
    "unixnu_commit": unixnu_commit,
    "device_repo_commit": device_repo_commit,
    "toolchain": {
        "xcodebuild": xcodebuild,
        "clang": clang,
        "python3": python3,
        "git": git,
    },
}

with open(manifest_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
PYEOF
}

main "$@"
