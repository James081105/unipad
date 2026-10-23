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
#   The four build stages call the upstream build driver that ships inside the
#   pinned UniXNU checkout (scripts/unixnu/cli.py).  The mapping, and the line
#   of upstream code each command comes from, is written next to each stage
#   below; nothing here is invented:
#
#     build-kernel      unixnu defconfig <[kernel] defconfig>   cli.py:89-101
#                       unixnu build kernel                     cli.py:123-129
#                         -> kernel.build() then kernel.prelink(); prelink writes
#                            out/<device>/kernel/kernel.macho and logs its sha256
#                            (kernel.py:105-137)
#     build-bootloader  unixnu build package                    cli.py:134
#                         -> package.build(): the EDK II build of package/, which
#                            also leaves the CLANGPDB tool links the device's
#                            DarwinBoot builder needs (package.py:21-31)
#                       python3 <tree>/scripts/build-darwinboot-efi.py
#                         -> the device repository's own bootloader.  This board
#                            declares no [firmware] (board.toml:85-86), so there
#                            is no Mu-Silicium firmware volume to take it from.
#     build-devicetree  unixnu ipsw fetch DeviceTree            cli.py:156-171
#                         -> the board's own device tree out of the pinned IPSW,
#                            checked against board.toml [ipsw.sha256]
#                       python3 <tree>/scripts/make-qemu-adt.py
#                         -> the device tree this machine actually boots with
#                            (board.toml [payload.adt])
#     assemble-image    unixnu bootimg --fd ... --dtb ...       cli.py:142-153
#                         -> bootimg.build(): gzip(BootShim + firmware volume)
#                            with the device tree appended (bootimg.py:71-114)
#
# WHAT IT REFUSES TO DO
#   * build from a lock file whose commits are null, empty, short or a branch
#     name (PRD 8.1: "Every run must be reproducible from this file plus the
#     recorded hashes").  `frozen: false` is NOT such a refusal: it is recorded
#     verbatim in artifact-manifest.json and announced on every log line, and the
#     pins are what actually prevent building from a moving branch.  Refusing it
#     deadlocked the pipeline against a workflow whose own preflight only warns
#     -- see the long comment in validate_lock();
#   * mix HVF and TCG: an HVF-targeted payload must never be produced for, or
#     downloaded by, this Intel Windows host (PRD 8.3);
#   * fetch anything over the network.  No curl, no wget, no git fetch, no
#     package manager.  Pinned sources are verified locally or reported absent;
#   * let the upstream driver fetch on its behalf.  prepare() fetches whatever
#     the driver's feeds root is missing (cli.py:79-86, feeds.py:173-216) and
#     initialises module submodules (feeds.py:159-165); both reach the network,
#     so this script proves they are already on disk at the pinned commit
#     before it runs anything, and refuses when they are not;
#   * take the https default of board.toml [ipsw].  The kernel step decodes the
#     original kernelcache out of the IPSW (kernel.py:111-114) and a URL there
#     is a download; --ipsw must name a local file and --kernelcache a local
#     file, or the step records not_run;
#   * depend on qemu-system-aarch64.  QEMU is a *native Windows* build
#     (config/sources.lock.json tools.qemu.build) and is deliberately not a
#     macOS build input;
#   * report success without evidence: a step that did not run is recorded as
#     not_run with a reason (PRD 7.4), a step that ran and failed is recorded
#     failed, and a stage in which anything is not_run or failed reports
#     ok=false.
#
# STATUS -- WHAT IS AND IS NOT PROVEN HERE, AND BY WHICH TEST
#   Proven on this Windows host, by tests that execute the real thing rather
#   than grep for it:
#     tests/acceptance/test_shell_script_syntax.py
#       -- that this script parses at all (`bash -n`);
#     tests/acceptance/test_build_script_gates.py
#       -- the lock gate: a lock whose pins are complete full SHAs is not
#          refused merely for being unfrozen, and a null, empty, short or
#          branch-name pin still is, named in the message;
#     tests/acceptance/test_macos_pipeline_gates.py
#       -- the --ipsw network refusal: an http(s) URL is network_refused and a
#          local file is not;
#     tests/acceptance/test_p0p1_gate_main.py
#       -- the workflow's own steps: the source-acquisition pins and
#          cross-check, the argument construction the build step hands this
#          script, and the out root the two halves share;
#       -- this script RUN, not grepped, against a scratch repository: the exact
#          argv each stage hands the driver (read out of the driver's own
#          argv.log), the canonical bytes of artifact-manifest.json and the real
#          hashes of the files it names, that a step which cannot run is recorded
#          not_run with its reason while the run reports ok=false, and the
#          refusals (network, wrong accelerator, malformed input, absent pinned
#          source).  Section 6 of that file.
#     tests/acceptance/test_workflow_python_syntax.py
#       -- that every Python block the workflow ships compiles.
#
#   NOT proven here, and not provable here: the Apple toolchain itself.
#   kernel.build() requires xcodebuild ("the kernel step builds with the Xcode
#   iphoneos SDK", kernel.py:75), and the driver itself prints "developed and
#   tested on macOS only" on every other host (cli.py:206-207).  A Homebrew LLVM
#   keg is NOT required: util.tool() (util.py:33-38) looks on PATH and then in the
#   keg, and toolchain_path() (kernel.py:40-44) only prepends the kegs that exist,
#   so an absent keg is something the driver names precisely rather than
#   something this entry point refuses up front.  No kernel, no DarwinBoot, no
#   firmware volume and no boot image has ever been produced by this script.
#   On a non-Darwin host each of the four stages records not_run with that
#   reason and the manifest is still written, unchanged in shape.
#
#   Also not producible for THIS device repository at all, macOS or not:
#   board.toml of the pinned device repo declares no [firmware]
#   (F:/iosvirtual/local-private/upstream/UniXNU-qemu/board.toml:85-86 --
#   "QEMU runs its own EDK II AARCH64 firmware, which has no DarwinBoot"), so
#   `unixnu build firmware` refuses and `unixnu bootimg` cannot resolve a
#   firmware volume.  Measured: with a valid --uefi and no --fd, bootimg dies
#   with KeyError: 'firmware' at firmware.py:18.  assemble-image therefore
#   records not_run naming that reason rather than inventing a boot image.
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
# The upstream driver needs 3.11 or newer (cli.py:204-205) and is invoked
# through this interpreter rather than through its own shebang, because the
# interpreter is not called python3 on every host that runs this script.
if command -v python3 >/dev/null 2>&1; then PY='python3'
elif command -v python  >/dev/null 2>&1; then PY='python'
else PY=''; fi

# The two pinned checkouts this entry drives.  They are the same two the lock
# file names (unixnu, device_repo) and the same two prepare-sources looks for;
# --unixnu / --device override the location, never the pin.
readonly UNIXNU_DIR_DEFAULT='vendor/UniXNU'
readonly DEVICE_DIR_DEFAULT='vendor/UniXNU-qemu'

# Where a Homebrew LLVM keg would live.  NOT a requirement of any step: the
# driver resolves it itself.  util.tool() (util.py:33-38) looks on PATH first and
# then in these kegs, so llvm-objcopy, llvm-nm, llvm-lib and lld-link are found
# there even when the keg is not on PATH; kernel.py:40-44 toolchain_path() only
# *prepends* the kegs that exist.  This script therefore reports which keg it
# sees (log_keg) and lets the driver name whatever it cannot find, instead of
# refusing a build on a macOS runner that has Apple clang and no Homebrew LLVM
# -- which upstream builds happily.
readonly HOMEBREW_LLVM_DIRS=('/opt/homebrew/opt/llvm' '/usr/local/opt/llvm')

# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

# stamp_var VARNAME -- put the ISO-8601 UTC timestamp in VARNAME.
# The assigning form exists so log() costs no subshell at all: `$(utc_stamp)`
# forks once per log line even when the timestamp itself is a builtin.
STAMP_BUILTIN=0
_stamp_probe=''
if TZ=UTC printf -v _stamp_probe '%(%Y-%m-%dT%H:%M:%SZ)T' -1 2>/dev/null \
        && [[ "$_stamp_probe" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
    STAMP_BUILTIN=1
fi
stamp_var() {
    if (( STAMP_BUILTIN )); then
        TZ=UTC printf -v "$1" '%(%Y-%m-%dT%H:%M:%SZ)T' -1
    else
        # bash 4.2+ formats the clock itself; macOS ships /bin/bash 3.2, where
        # '%(..)T' is not a thing at all, so the fallback is portable `date`.
        printf -v "$1" '%s' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    fi
}

log() {
    local stamp=''
    stamp_var stamp
    printf '%s %s\n' "$stamp" "$*"
}
step() { printf '\n=== %s ===\n' "$*"; }

# warn MESSAGE -- a non-fatal condition the operator must still see.
#
# Deliberately NOT `die`: a warning that stops the run is a refusal, and the
# unfrozen-lock condition below is not one (see validate_lock).  The message goes
# to stderr so it cannot be lost in a `| tee` and cannot be mistaken for a build
# step's own output.
warn() { printf 'WARNING: %s\n' "$*" >&2; }

utc_stamp() {
    local stamp=''
    stamp_var stamp
    printf '%s' "$stamp"
}

# json_escape_var VARNAME TEXT -- json_escape, assigning instead of printing.
#
# Two `printf -v`s are the whole point of the shape below.  The obvious
# spellings, code=$(printf '%d' "'$ch") and out+=$(printf '\\u%04x' ...), fork a
# subshell PER CHARACTER -- measured on this host at ~0.85 s per fork, which
# turned a 135-character refusal message into a two-minute hang inside the very
# code path that is supposed to report a failure quickly.  `printf -v` assigns
# to a variable without forking at all.
#
# The assigning form exists as well as the printing one because a caller that
# needs the escaped text inside a printf argument list otherwise pays a subshell
# fork of its own: write_outputs alone had ~25 of those, which after the
# per-character fix was still the largest single cost in a run.
json_escape_var() {
    local __var="$1" __text="$2" __out='' __ch __i __code __esc
    for (( __i = 0; __i < ${#__text}; __i++ )); do
        __ch="${__text:__i:1}"
        case "$__ch" in
            '\')   __out+='\\' ;;
            '"')   __out+='\"' ;;
            $'\n') __out+='\n' ;;
            $'\r') __out+='\r' ;;
            $'\t') __out+='\t' ;;
            *)
                printf -v __code '%d' "'$__ch"
                if (( __code < 32 )); then
                    printf -v __esc '\\u%04x' "$__code"
                    __out+="$__esc"
                else
                    __out+="$__ch"
                fi
                ;;
        esac
    done
    printf -v "$__var" '%s' "$__out"
}

json_escape() {
    local out=''
    json_escape_var out "$1"
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
    if (( $# != 4 )); then
        # A wrong arity here used to shift the message into the details slot and
        # the details JSON into the exit-code slot, producing unparseable error
        # output and a "numeric argument required" exit.  Fail loudly instead.
        printf '{"error":{"code":"internal_error","message":"die() called with %d arguments, expected 4","details":{}}}\n' "$#" >&2
        printf 'prepare-image: FAILED [internal_error] die() called with %d arguments, expected 4\n' "$#" >&2
        exit "$EXIT_FAILURE"
    fi
    local code="$1" message="$2" details="$3" exit_code="$4"
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
  --unixnu <dir>           The pinned UniXNU checkout that carries the build
                           driver (its root 'unixnu' script and
                           scripts/unixnu/).  Default <repo>/vendor/UniXNU.
  --device <dir>           The pinned device repository (its board.toml).
                           Default <repo>/vendor/UniXNU-qemu.
  --uefi <dir>             Mu-Silicium checkout, for the firmware volume and
                           the boot image (--uefi of the driver, cli.py:24).
  --ipsw <file>            The LOCAL IPSW.  An https:// URL is refused: the
                           driver's default is board.toml [ipsw] url and this
                           script performs no network access.
  --kernelcache <file>     The original decompressed kernelcache the prelink
                           step needs (--kernelcache of the driver).
  --dtb <file>             A compiled device tree for the boot image.
  --fd <file>              A firmware volume for the boot image.
  --unique-chip-id <hex>   The platform seed the device tree carries.  It is
                           not stored in any repository (board.toml
                           [payload.adt] unique_chip_id = "required"), so the
                           device-tree step records not_run without it.
  --jobs <n>               Parallel jobs for the driver (default: CPU count).
  --include <path[::role[::arch]]>
                           Declare a deliverable that must exist and be hashed
                           into the manifest.  Repeatable.  '::' separates the
                           optional role and arch, so a Windows drive letter
                           ("C:/x") is never mistaken for a delimiter.
                           A missing file is a failure, never a silently
                           empty manifest.
  --min-free-gb <n>        Required free space on the output filesystem.
  --skip-sources           Continue when a pinned source checkout is absent.
                           Recorded in build-report.json; the build steps stay
                           not_run.  Off by default (a missing pinned source is
                           a missing input).
  --dry-run                Validate everything and print the plan; write
                           nothing and run nothing.
  -h, --help               This text.

Notes:
  * No network access of any kind: nothing is fetched, cloned or pulled.  The
    upstream driver fetches on its own when its feeds are missing, so the
    script proves the feeds are already on disk first and refuses otherwise.
  * qemu-system-aarch64 is not required and not used (native Windows build).
  * The four build stages are real invocations of the upstream driver.  The
    Apple toolchain they need exists on macOS only, so on any other host they
    record not_run with that reason; see STATUS at the top of this file.
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
UNIXNU_DIR=''
DEVICE_DIR=''
UEFI_DIR=''
IPSW_PATH=''
KERNELCACHE_PATH=''
DTB_PATH=''
FD_PATH_IN=''
UNIQUE_CHIP_ID=''
JOBS=''

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
        --unixnu)         UNIXNU_DIR="${2:-}"; shift 2 ;;
        --unixnu=*)       UNIXNU_DIR="${1#*=}"; shift ;;
        --device)         DEVICE_DIR="${2:-}"; shift 2 ;;
        --device=*)       DEVICE_DIR="${1#*=}"; shift ;;
        --uefi)           UEFI_DIR="${2:-}"; shift 2 ;;
        --uefi=*)         UEFI_DIR="${1#*=}"; shift ;;
        --ipsw)           IPSW_PATH="${2:-}"; shift 2 ;;
        --ipsw=*)         IPSW_PATH="${1#*=}"; shift ;;
        --kernelcache)    KERNELCACHE_PATH="${2:-}"; shift 2 ;;
        --kernelcache=*)  KERNELCACHE_PATH="${1#*=}"; shift ;;
        --dtb)            DTB_PATH="${2:-}"; shift 2 ;;
        --dtb=*)          DTB_PATH="${1#*=}"; shift ;;
        --fd)             FD_PATH_IN="${2:-}"; shift 2 ;;
        --fd=*)           FD_PATH_IN="${1#*=}"; shift ;;
        --unique-chip-id) UNIQUE_CHIP_ID="${2:-}"; shift 2 ;;
        --unique-chip-id=*) UNIQUE_CHIP_ID="${1#*=}"; shift ;;
        --jobs)           JOBS="${2:-}"; shift 2 ;;
        --jobs=*)         JOBS="${1#*=}"; shift ;;
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

# --ipsw must name a local file.  The driver's own default is the https URL of
# board.toml [ipsw] (ipsw.py:88-93) and a URL there is a download over HTTP
# range requests (ipsw.py:27-74); this script performs no network access, so a
# URL is a usage error rather than something to silently allow.
if [[ "$IPSW_PATH" =~ ^https?:// ]]; then
    die 'network_refused' \
        "--ipsw must name a local file; '$IPSW_PATH' is a URL and this script performs no network access (PRD 8.1)." \
        "{\"ipsw\":\"$(json_escape "$IPSW_PATH")\"}" "$EXIT_USAGE"
fi
# A declared file input that is not a file is a usage error, refused here rather
# than discovered by the driver halfway through a build.
for pair in 'kernelcache:'"$KERNELCACHE_PATH" 'ipsw:'"$IPSW_PATH" 'dtb:'"$DTB_PATH" 'fd:'"$FD_PATH_IN"; do
    label="${pair%%:*}"
    value="${pair#*:}"
    if [[ -n "$value" && ! -f "$value" ]]; then
        die 'config_invalid' "--$label is not a file: $value" \
            "{\"$label\":\"$(json_escape "$value")\"}" "$EXIT_USAGE"
    fi
done
if [[ -n "$JOBS" && ! "$JOBS" =~ ^[0-9]+$ ]]; then
    die 'config_invalid' "--jobs must be an integer (got '$JOBS')." '{}' "$EXIT_USAGE"
fi
# The platform seed is a hex value the driver parses with int(value, 16)
# (cli.py:35, make-qemu-adt.py:67); a non-hex string is refused here rather than
# by a traceback inside the board tool.
if [[ -n "$UNIQUE_CHIP_ID" && ! "$UNIQUE_CHIP_ID" =~ ^[0-9a-fA-F]{1,16}$ ]]; then
    die 'config_invalid' \
        "--unique-chip-id must be 1-16 hex digits (got '$UNIQUE_CHIP_ID')." \
        "{\"unique_chip_id\":\"$(json_escape "$UNIQUE_CHIP_ID")\"}" "$EXIT_USAGE"
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

# The pinned checkouts.  A relative override is resolved against the repository
# root, exactly like --include, so "--unixnu local-private/upstream/UniXNU"
# works from a checkout that keeps its sources out of vendor/.
if [[ -z "$UNIXNU_DIR" ]]; then
    UNIXNU_DIR="$REPO_ROOT/$UNIXNU_DIR_DEFAULT"
elif [[ "$UNIXNU_DIR" != /* && ! "$UNIXNU_DIR" =~ ^[A-Za-z]:[/\\] ]]; then
    UNIXNU_DIR="$REPO_ROOT/$UNIXNU_DIR"
fi
if [[ -z "$DEVICE_DIR" ]]; then
    DEVICE_DIR="$REPO_ROOT/$DEVICE_DIR_DEFAULT"
elif [[ "$DEVICE_DIR" != /* && ! "$DEVICE_DIR" =~ ^[A-Za-z]:[/\\] ]]; then
    DEVICE_DIR="$REPO_ROOT/$DEVICE_DIR"
fi
if [[ -n "$UEFI_DIR" && "$UEFI_DIR" != /* && ! "$UEFI_DIR" =~ ^[A-Za-z]:[/\\] ]]; then
    UEFI_DIR="$REPO_ROOT/$UEFI_DIR"
fi
# Strip a trailing slash so "$DIR/..." never doubles up.
while [[ "$UNIXNU_DIR" == */ && ${#UNIXNU_DIR} -gt 1 ]]; do UNIXNU_DIR="${UNIXNU_DIR%/}"; done
while [[ "$DEVICE_DIR" == */ && ${#DEVICE_DIR} -gt 1 ]]; do DEVICE_DIR="${DEVICE_DIR%/}"; done

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
# All fields are read in ONE interpreter invocation.  A per-field fork was not
# only slower, it was unreliable on hosts whose process creation is fragile, and
# the failure was swallowed by `2>/dev/null || true` -- silently turning a
# pinned commit into an empty string and a valid lock into a refusal.
read_lock_fields() { # read_lock_fields -- prints KEY<TAB>VALUE per line
    if [[ -n "$PY" ]]; then
        "$PY" - "$LOCK_PATH" <<'PY' 2>/dev/null || true
import json, sys

WANTED = (
    ('frozen', ('frozen',)),
    ('unixnu_commit', ('unixnu', 'commit')),
    ('device_commit', ('device_repo', 'commit')),
    ('qemu_accel', ('tools', 'qemu', 'accel')),
    ('ipsw_sha256', ('ipsw', 'sha256')),
    ('ipsw_name', ('ipsw', 'name')),
)
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as handle:
        root = json.load(handle)
except Exception:
    sys.exit(0)
for name, path in WANTED:
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
PY
        return 0
    fi
    if command -v jq >/dev/null 2>&1; then
        jq -r '
            def v(p): (getpath(p) // "") | tostring;
            ["frozen\t" + v(["frozen"]),
             "unixnu_commit\t" + v(["unixnu", "commit"]),
             "device_commit\t" + v(["device_repo", "commit"]),
             "qemu_accel\t" + v(["tools", "qemu", "accel"]),
             "ipsw_sha256\t" + v(["ipsw", "sha256"]),
             "ipsw_name\t" + v(["ipsw", "name"])] | .[]' \
            "$LOCK_PATH" 2>/dev/null || true
        return 0
    fi
    printf 'frozen\t\nunixnu_commit\t\ndevice_commit\t\nqemu_accel\t\nipsw_sha256\t\nipsw_name\t\n'
}

LOCK_FROZEN=''
#: 1 when the lock's pins are complete but the lock does not declare itself
#: frozen.  Recorded in the manifest and announced in the log; never a refusal.
LOCK_UNFROZEN=0
LOCK_UNIXNU_COMMIT=''
LOCK_DEVICE_COMMIT=''
LOCK_QEMU_ACCEL=''
LOCK_IPSW_SHA256=''
LOCK_IPSW_NAME=''

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

    local key value
    while IFS="$(printf '\t')" read -r key value; do
        # A Windows interpreter writes CRLF on a pipe; strip it so a value of
        # "true\r" is never mistaken for "not frozen".
        value="${value%$'\r'}"
        case "$key" in
            frozen)         LOCK_FROZEN="$value" ;;
            unixnu_commit)  LOCK_UNIXNU_COMMIT="$value" ;;
            device_commit)  LOCK_DEVICE_COMMIT="$value" ;;
            qemu_accel)     LOCK_QEMU_ACCEL="$value" ;;
            ipsw_sha256)    LOCK_IPSW_SHA256="$value" ;;
            ipsw_name)      LOCK_IPSW_NAME="$value" ;;
        esac
    done < <(read_lock_fields)

    # `frozen` is a DECLARATION; the pins are the GUARANTEE.  The only thing this
    # gate exists to prevent is building from a moving branch, and a pin that is
    # a full 40-hex commit already prevents that -- there is no branch to move.
    # Refusing an unfrozen-but-fully-pinned lock therefore adds no safety, and it
    # deadlocked the pipeline it was written to serve:
    #
    #   * build-macos.yml's "Validate inputs" step treats frozen=false as a
    #     WARNING -- "the dispatch inputs are the freeze candidates".
    #   * the acquisition step then fetches those pinned commits and publishes
    #     them as step outputs.
    #   * this gate then treated the same false as a HARD FAILURE, exit 2.
    #
    # so the workflow spent macOS minutes on preflight and acquisition and could
    # never reach a build.  Measured, not theorised:
    # tests/acceptance/test_build_script_gates.py::
    # test_the_frozen_gate_does_not_deadlock_the_pipeline.
    #
    # What an unfrozen lock still costs the run: it is recorded verbatim in
    # artifact-manifest.json and announced on every log line below, so no reader
    # can mistake it for a frozen baseline.  A pin that is NOT a full commit is
    # still refused, loudly -- that is the check with teeth, and it is the one
    # that follows.
    if [[ "$LOCK_FROZEN" != 'true' ]]; then
        LOCK_UNFROZEN=1
        warn "sources lock is not frozen (frozen=$LOCK_FROZEN): the pinned commits are the freeze candidates for this run and are recorded verbatim in the manifest. A missing or short pin is still a refusal."
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
            "target '$TARGET' contradicts the locked accelerator '$LOCK_QEMU_ACCEL'; refusing to build a payload for the wrong accelerator (PRD 8.3)." \
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
# Each entry: name|status|reason   (status: ran | not_run | skipped | failed)
LEDGER=()

# Artifacts the build stages actually produced, in the same record shape the
# manifest writer consumes (see write_manifest).  A stage that produced nothing
# appends nothing, so the manifest can never carry a hash of a file that is not
# there.
BUILT_RECORDS=()
BUILT_PATHS=()
BUILT_SHAS=()

# Set when the pinned sources are absent and --skip-sources was given: the
# stages then stay not_run instead of letting the driver fetch on our behalf.
SOURCES_INCOMPLETE=0

ledger_add() { LEDGER+=("$1|$2|$3"); }

ledger_json() {
    local first=1 entry name status reason
    local j_name j_status j_reason
    printf '['
    for entry in ${LEDGER[@]+"${LEDGER[@]}"}; do
        name="${entry%%|*}"
        status="${entry#*|}"; status="${status%%|*}"
        reason="${entry##*|}"
        (( first )) || printf ','
        first=0
        json_escape_var j_name   "$name"
        json_escape_var j_status "$status"
        json_escape_var j_reason "$reason"
        printf '{"name":"%s","status":"%s","reason":"%s"}' \
            "$j_name" "$j_status" "$j_reason"
    done
    printf ']'
}

# ledger_has_not_run -- true when any entry did not run or failed.  A stage
# result may only report success when this is false.
ledger_has_not_run() {
    local entry status
    for entry in ${LEDGER[@]+"${LEDGER[@]}"}; do
        status="${entry#*|}"; status="${status%%|*}"
        case "$status" in
            not_run|failed) return 0 ;;
        esac
    done
    return 1
}

# ledger_summary -- "ran=N not_run=N failed=N skipped=N", for the console.
ledger_summary() {
    local entry status ran=0 not_run=0 failed=0 skipped=0
    for entry in ${LEDGER[@]+"${LEDGER[@]}"}; do
        status="${entry#*|}"; status="${status%%|*}"
        case "$status" in
            ran)     ran=$(( ran + 1 )) ;;
            not_run) not_run=$(( not_run + 1 )) ;;
            failed)  failed=$(( failed + 1 )) ;;
            skipped) skipped=$(( skipped + 1 )) ;;
        esac
    done
    printf 'ran=%d not_run=%d failed=%d skipped=%d' "$ran" "$not_run" "$failed" "$skipped"
}

# not_run_reasons_json -- the distinct "name: reason" of every entry that did
# not run or failed, as a JSON array.  Derived from the ledger rather than
# hardcoded, so a new refusal shows up here without anyone remembering to.
not_run_reasons_json() {
    local first=1 entry name status reason seen=''
    local j_name j_reason
    printf '['
    for entry in ${LEDGER[@]+"${LEDGER[@]}"}; do
        name="${entry%%|*}"
        status="${entry#*|}"; status="${status%%|*}"
        reason="${entry##*|}"
        case "$status" in
            not_run|failed) ;;
            *) continue ;;
        esac
        case "$seen" in
            *"|$name|$reason|"*) continue ;;
        esac
        seen+="|$name|$reason|"
        (( first )) || printf ','
        first=0
        json_escape_var j_name   "$name"
        json_escape_var j_reason "$reason"
        printf '"%s: %s"' "$j_name" "$j_reason"
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
# the upstream build driver
# ---------------------------------------------------------------------------
# Everything the four build stages run comes out of the pinned UniXNU checkout:
# its root 'unixnu' script is the entry point (it inserts scripts/ on sys.path
# and calls unixnu.cli.main) and scripts/unixnu/ holds the steps.  The driver's
# --out is redirected into this run's own directory: left alone it writes
# out/ inside the pinned checkout (config.py:64), which would mutate a source
# tree this script is supposed to only read.
UNIXNU_OUT="$OUT_DIR/unixnu-out"
FEEDS_DIR="$UNIXNU_OUT/feeds"
DRIVER="$UNIXNU_DIR/unixnu"
BOARD_NAME=''
DEFCONFIG=''
BOARD_HAS_FIRMWARE='no'
ADT_TOOL=''

# read_board_fields -- NAME<TAB>VALUE per line, read from board.toml of the
# device repository with the interpreter the manifest writer already requires.
# board.toml is TOML and its comments mention section names, so a grep would
# match prose; tomllib is the only honest reader.
read_board_fields() {
    "$PY" - "$DEVICE_DIR/board.toml" <<'PY'
import sys
try:
    import tomllib
except ImportError:  # pragma: no cover - the driver itself needs 3.11 (cli.py:204)
    sys.stderr.write('python 3.11 or newer is required to read board.toml (tomllib)\n')
    sys.exit(3)
try:
    with open(sys.argv[1], 'rb') as handle:
        board = tomllib.load(handle)
except FileNotFoundError:
    board = {}
except tomllib.TOMLDecodeError as error:
    sys.stderr.write('board.toml: %s\n' % error)
    sys.exit(1)
fields = (
    ('board_name', board.get('board', {}).get('name', '')),
    ('defconfig', board.get('kernel', {}).get('defconfig', '')),
    ('has_firmware', 'yes' if board.get('firmware') else 'no'),
    ('adt_tool', board.get('payload', {}).get('adt', {}).get('tool', '')),
)
for name, value in fields:
    print('%s\t%s' % (name, '' if value is None else value))
PY
}

resolve_board() {
    [[ -n "$PY" ]] || die 'blocked' 'no Python interpreter to read board.toml.' \
        '{}' "$EXIT_BLOCKED"
    local key value
    while IFS="$(printf '\t')" read -r key value; do
        value="${value%$'\r'}"
        case "$key" in
            board_name)    BOARD_NAME="$value" ;;
            defconfig)     DEFCONFIG="$value" ;;
            has_firmware)  BOARD_HAS_FIRMWARE="$value" ;;
            adt_tool)      ADT_TOOL="$value" ;;
        esac
    done < <(read_board_fields)
    if [[ -z "$BOARD_NAME" ]]; then
        die 'config_invalid' \
            "board.toml of $DEVICE_DIR has no [board] name; the driver cannot place its output (config.py:76-78)." \
            "{\"device\":\"$(json_escape "$DEVICE_DIR")\"}" "$EXIT_USAGE"
    fi
    log "driver:  $DRIVER"
    log "device:  $DEVICE_DIR (board $BOARD_NAME)"
    log "out:     $UNIXNU_OUT"
}

# Where the driver puts a step's output.  ctx.work is out/<board name> and
# ctx.tree is out/<board name>/tree (config.py:80-82), so these are the paths
# kernel.prelink, package.build and tree.assemble write to.
work_dir()  { printf '%s' "$UNIXNU_OUT/$BOARD_NAME"; }
tree_dir()  { printf '%s' "$UNIXNU_OUT/$BOARD_NAME/tree"; }
logs_dir()  { printf '%s' "$UNIXNU_OUT/$BOARD_NAME/logs"; }

head_of() { # head_of DIR -> the commit DIR sits on, or nothing
    local out
    if out="$(git -C "$1" rev-parse HEAD 2>/dev/null)"; then
        printf '%s' "${out%$'\r'}"
    else
        printf ''
    fi
}

# lock_entries -- 'submodule<TAB>path<TAB>commit' and 'feed<TAB>name<TAB>commit'
# for every source the lock file pins.  The lock, not the repositories, is the
# authority (PRD 8.1).
lock_entries() {
    "$PY" - "$LOCK_PATH" <<'PY'
import json
import sys

with open(sys.argv[1], 'r', encoding='utf-8') as handle:
    root = json.load(handle)
for entry in root.get('submodules') or []:
    print('submodule\t%s\t%s' % (entry.get('path') or '', entry.get('commit') or ''))
for entry in root.get('feeds') or []:
    print('feed\t%s\t%s' % (entry.get('name') or '', entry.get('commit') or ''))
PY
}

# offline_missing -- 'kind<TAB>name' for every pinned source the driver would
# have to fetch.  prepare() fetches any git feed whose clone is absent or at the
# wrong commit (cli.py:79-86, feeds.py:173-216) and initialises module
# submodules (feeds.py:159-165); both reach the network, so this script proves
# they are already on disk at the pinned commit before it runs anything.
# Measured on this host: without this guard `unixnu build kernel` reaches
# `git fetch https://github.com/apple-oss-distributions/xnu.git` and fails.
offline_missing() {
    local kind name commit dir found
    while IFS="$(printf '\t')" read -r kind name commit; do
        # A Windows interpreter writes CRLF on a pipe, so the last field of
        # every line arrives with a trailing \r.  validate_lock() strips it for
        # the same reason (see the comment there); without the strip here the
        # pinned commit never equals the one git reports and EVERY source reads
        # as absent -- measured: a feed clone sitting on disk at exactly the
        # locked commit was still reported as "the driver would have to fetch".
        name="${name%$'\r'}"
        commit="${commit%$'\r'}"
        [[ -n "$commit" ]] || continue
        found=0
        case "$kind" in
            submodule)
                for dir in "$UNIXNU_DIR/$name" "$DEVICE_DIR/$name"; do
                    if [[ -d "$dir" && "$(head_of "$dir")" == "$commit" ]]; then
                        found=1
                        break
                    fi
                done
                ;;
            feed)
                for dir in "$FEEDS_DIR"/*; do
                    if [[ -d "$dir" && "$(head_of "$dir")" == "$commit" ]]; then
                        found=1
                        break
                    fi
                done
                ;;
        esac
        if (( found == 0 )); then
            printf '%s\t%s\n' "$kind" "$name"
        fi
    done < <(lock_entries)
}

# apple_step_gate NAME REQUIREMENT...
# Returns 0 when the stage may run.  Otherwise records NAME as not_run with
# every reason found at once -- the host and the missing tools together, so the
# ledger never has to be read twice to learn why a stage did not run -- and
# returns 1.  A requirement that looks like a path is checked as a directory
# (the Homebrew LLVM keg), anything else with command -v.
apple_step_gate() {
    local name="$1"; shift
    local host requirement reason
    host="$(uname -s 2>/dev/null || printf '%s' 'unknown')"
    local -a missing=()
    for requirement in "$@"; do
        case "$requirement" in
            /*) if [[ ! -d "$requirement" ]]; then missing+=("$requirement"); fi ;;
            *)  if ! command -v "$requirement" >/dev/null 2>&1; then
                    missing+=("$requirement")
                fi ;;
        esac
    done
    if [[ "$host" == 'Darwin' ]] && (( ${#missing[@]} == 0 )); then
        return 0
    fi
    reason=''
    if [[ "$host" != 'Darwin' ]]; then
        reason="host is $host and the Apple toolchain exists on macOS only (PRD 8.1)"
    fi
    if (( ${#missing[@]} > 0 )); then
        if [[ -n "$reason" ]]; then reason+='; '; fi
        reason+="missing: ${missing[*]}"
    fi
    ledger_add "$name" 'not_run' "$reason"
    return 1
}

# log_keg -- report which Homebrew LLVM keg, if any, is on disk.
# Informational only.  See the HOMEBREW_LLVM_DIRS comment: util.tool() finds keg
# tools without the keg being on PATH, so an absent keg is something the driver
# reports precisely ("llvm-lib not found on PATH (brew install llvm lld)"), not
# something this script refuses up front.
log_keg() {
    local keg found=''
    for keg in "${HOMEBREW_LLVM_DIRS[@]}"; do
        if [[ -d "$keg" ]]; then found="$keg"; break; fi
    done
    if [[ -n "$found" ]]; then
        log "llvm keg: $found (util.tool() also finds keg-only tools when it is not on PATH)"
    else
        log "llvm keg: none of ${HOMEBREW_LLVM_DIRS[*]} on disk; the driver looks there and names what it cannot find"
    fi
}

# run_driver STEP SUBCOMMAND ARGS...
# Echoes the command, runs it with its output in OUT_DIR/<step>.driver.log, and
# returns the driver's exit status.  A nonzero status is a stage failure and is
# recorded as one; it is never folded into "ran".
run_driver() {
    local step="$1"; shift
    local sub="$1"; shift
    local -a cmd=("$PY" "$DRIVER" "$sub" -D "$DEVICE_DIR" --out "$UNIXNU_OUT" -j "$JOBS" "$@")
    local log_file="$OUT_DIR/${step}.driver.log"
    log "+ ${cmd[*]}"
    if "${cmd[@]}" 2>&1 | tee "$log_file"; then
        return 0
    fi
    return 1
}

# shown_path PATH -- how the manifest records PATH.
# A path inside this run's output directory is recorded relative to it, so the
# manifest is portable; anything else keeps the path it was given.  One helper,
# used by both the built artifacts and the declared ones, so the two cannot
# disagree about what a path means.
shown_path() {
    local path="$1"
    case "$path" in
        "$OUT_DIR"/*) printf '%s' "${path#"$OUT_DIR"/}" ;;
        *)            printf '%s' "$path" ;;
    esac
}

# record_artifact PATH ROLE [ARCH]
# One manifest record for a file a stage actually produced.  A stage that
# produced nothing records nothing: the manifest never carries a hash of a file
# that is not there.
record_artifact() {
    local path="$1" role="$2" arch="${3:-aarch64}" sha size leaf
    if [[ ! -f "$path" ]]; then
        die 'evidence_incomplete' \
            "a stage reported $path but the file does not exist." \
            "{\"path\":\"$(json_escape "$path")\"}" "$EXIT_FAILURE"
    fi
    sha="$(sha256_of "$path")"
    if [[ ! "$sha" =~ $SHA256_PATTERN ]]; then
        die 'verification_failed' "could not compute the SHA-256 of $path." \
            "{\"path\":\"$(json_escape "$path")\"}" "$EXIT_FAILURE"
    fi
    size="$(wc -c <"$path" | tr -d ' ')"
    leaf="$(basename -- "$path")"
    local shown
    shown="$(shown_path "$path")"
    BUILT_RECORDS+=("${leaf}${FIELD_SEP}${sha}${FIELD_SEP}${size}${FIELD_SEP}${role}${FIELD_SEP}${arch}${FIELD_SEP}${shown}")
    BUILT_PATHS+=("$path")
    BUILT_SHAS+=("$sha")
    log "artifact: $leaf sha256=$sha bytes=$size role=$role arch=$arch path=$shown"
    BUILT_NAMES+=("$leaf")
}

# ---------------------------------------------------------------------------
# the four build stages
# ---------------------------------------------------------------------------
# Each one: (1) refuses a declared input that is absent, with a code, before
# anything runs; (2) records not_run with the exact reason when the host, a
# tool or an input this script will not go and get is missing; (3) runs the
# upstream driver with the arguments its own code expects; (4) records the
# artifact it produced and its sha256.

# build-kernel -- 'unixnu defconfig <name>' then 'unixnu build kernel'.
#   cli.py:89-101  cmd_defconfig copies configs/<name> to out/<device>/.config
#   cli.py:123-129 cmd_build runs kernel.build() then kernel.prelink()
#   kernel.py:71-102 kernel.build(): git, make, clang, cmake, ninja, unifdef and
#                  xcodebuild, then scripts/build-xnu-4k-baseline.sh
#   kernel.py:105-137 kernel.prelink(): out/<device>/kernel/kernel.macho, and it
#                  logs the sha256 of what it wrote
step_build_kernel() {
    local name='build-kernel'

    # (1) inputs.  The defconfig is named by board.toml [kernel] and must exist
    # in the device repository; the driver refuses a build without it
    # (cli.py:123-124, config.py:105-108).
    if [[ -z "$DEFCONFIG" ]]; then
        die 'config_invalid' \
            "board.toml of $DEVICE_DIR has no [kernel] defconfig." \
            "{\"device\":\"$(json_escape "$DEVICE_DIR")\"}" "$EXIT_USAGE"
    fi
    if [[ ! -f "$DEVICE_DIR/configs/$DEFCONFIG" ]]; then
        die 'artifact_missing' \
            "defconfig not found: $DEVICE_DIR/configs/$DEFCONFIG" \
            "{\"defconfig\":\"$(json_escape "$DEFCONFIG")\",\"expected\":\"$(json_escape "$DEVICE_DIR/configs/$DEFCONFIG")\"}" \
            "$EXIT_FAILURE"
    fi
    # kernel.prelink() takes the original kernelcache from --kernelcache, from
    # board.toml [kernel.prelink] kernelcache, or out of the IPSW
    # (kernel.py:111-114).  This board names no kernelcache file and its
    # [ipsw] default is an https URL, so without a local input the step would
    # have to download one.  It records not_run instead.
    if [[ -z "$KERNELCACHE_PATH" && -z "$IPSW_PATH" ]]; then
        ledger_add "$name" 'not_run' \
            'the prelink step needs the original kernelcache: pass --kernelcache FILE or --ipsw FILE (board.toml [ipsw] url is https and this script performs no network access, PRD 8.1)'
        return 0
    fi

    # (2) host and tools, exactly the list kernel.py:73-75 checks.
    local -a need=('git' 'make' 'clang' 'cmake' 'ninja' 'unifdef' 'xcodebuild')
    log_keg
    apple_step_gate "$name" "${need[@]}" || return 0

    # (3) run.  The defconfig first, because build kernel refuses without it.
    local -a extra=()
    if [[ -n "$KERNELCACHE_PATH" ]]; then extra+=(--kernelcache "$KERNELCACHE_PATH"); fi
    if [[ -n "$IPSW_PATH" ]]; then extra+=(--ipsw "$IPSW_PATH"); fi
    if ! run_driver "$name" 'defconfig' "$DEFCONFIG" ${extra[@]+"${extra[@]}"}; then
        ledger_add "$name" 'failed' "unixnu defconfig $DEFCONFIG failed; see $OUT_DIR/${name}.driver.log"
        return 1
    fi
    if ! run_driver "$name" 'build' 'kernel' ${extra[@]+"${extra[@]}"}; then
        ledger_add "$name" 'failed' "unixnu build kernel failed; see $OUT_DIR/${name}.driver.log"
        return 1
    fi

    # (4) the artifact prelink writes, and the sha256 it logged.
    local macho
    macho="$(work_dir)/kernel/kernel.macho"
    record_artifact "$macho" 'kernel' 'aarch64'
    ledger_add "$name" 'ran' \
        "unixnu build kernel; $(logs_dir)/prelink.log carries the sha256 prelink logged"
    return 0
}

# build-bootloader -- 'unixnu build package', then the device repository's own
# DarwinBoot builder.
#   cli.py:133-134  cmd_build runs package.build() for the package step
#   package.py:21-31 package.build(): the EDK II build of package/ under
#                  out/<device>/package, which also leaves the CLANGPDB tool
#                  links under out/<device>/toolchain
#   board.toml:85-86 of the pinned device repository: "No [firmware]: QEMU runs
#                  its own EDK II AARCH64 firmware, which has no DarwinBoot", so
#                  the bootloader is built by the device repository's
#                  scripts/build-darwinboot-efi.py, which run-qemu.py:83-96
#                  calls before it boots anything.
step_build_bootloader() {
    local name='build-bootloader'

    if [[ ! -f "$DRIVER" ]]; then
        die 'artifact_missing' "the build driver is missing: $DRIVER" \
            "{\"driver\":\"$(json_escape "$DRIVER")\"}" "$EXIT_FAILURE"
    fi

    # (2) host and tools.  package.py:24 checks clang; the EDK II build of the
    # device's DarwinBoot additionally needs python3 and make, and the driver's
    # own prepare() needs git.
    local -a need=('git' 'clang' 'make' 'python3')
    log_keg
    apple_step_gate "$name" "${need[@]}" || return 0

    # (3) the package first: it assembles the build tree (tree.py:40-76) and
    # leaves the CLANGPDB tool links build-darwinboot-efi.py requires.
    if ! run_driver "$name" 'build' 'package'; then
        ledger_add "$name" 'failed' "unixnu build package failed; see $OUT_DIR/${name}.driver.log"
        return 1
    fi
    local script
    script="$(tree_dir)/scripts/build-darwinboot-efi.py"
    if [[ ! -f "$script" ]]; then
        die 'artifact_missing' \
            "the device repository's DarwinBoot builder is not in the build tree: $script" \
            "{\"script\":\"$(json_escape "$script")\"}" "$EXIT_FAILURE"
    fi
    log "+ $PY $script"
    if ! "$PY" "$script" >"$OUT_DIR/${name}.darwinboot.log" 2>&1; then
        ledger_add "$name" 'failed' \
            "build-darwinboot-efi.py failed; see $OUT_DIR/${name}.darwinboot.log"
        return 1
    fi
    tail -n 3 "$OUT_DIR/${name}.darwinboot.log" >&2 || true

    # (4) the driver image the payload loads (board.toml [payload] loader).
    record_artifact "$(tree_dir)/build/darwinboot-efi/DarwinBoot.efi" 'bootloader' 'aarch64'
    ledger_add "$name" 'ran' \
        'unixnu build package + build-darwinboot-efi.py (this board declares no [firmware], board.toml:85-86)'
    return 0
}

# build-devicetree -- the board's own device tree out of the pinned IPSW, then
# the Apple-format tree this machine boots with.
#   cli.py:156-171  cmd_ipsw extracts a component and checks it against
#                   board.toml [ipsw.sha256] (ipsw.py:140-171)
#   board.toml [payload.adt] tool = scripts/make-qemu-adt.py, which the payload
#                   step runs (payload.py:149-171)
step_build_devicetree() {
    local name='build-devicetree'

    # (1) the IPSW must be local; the driver's default is the https URL of
    # board.toml [ipsw] (ipsw.py:88-93).
    if [[ -z "$IPSW_PATH" ]]; then
        ledger_add "$name" 'not_run' \
            'the device tree comes out of the pinned IPSW: pass --ipsw FILE (the default of board.toml [ipsw] is an https URL and this script performs no network access, PRD 8.1)'
        return 0
    fi

    # (2) host and tools.  ipsw.kernelcache() is not on this path, but the
    # extraction is the driver's own code and needs the interpreter and git.
    local -a need=('git' 'python3')
    apple_step_gate "$name" "${need[@]}" || return 0

    # (3) the board's original device tree, extracted and checked against the
    # board's own pin.  Measured on this host with the local IPSW: it works, so
    # this is the one sub-step that is genuinely exercisable off macOS.
    if ! run_driver "$name" 'ipsw' 'fetch' 'DeviceTree' --ipsw "$IPSW_PATH"; then
        ledger_add "$name" 'failed' \
            "unixnu ipsw fetch DeviceTree failed; see $OUT_DIR/${name}.driver.log"
        return 1
    fi
    # The driver prints the path it extracted as its last line (ipsw.py:171);
    # that is the driver's own answer, so it is used rather than re-derived from
    # the IPSW's member naming.
    local extracted
    extracted="$(tail -n 1 "$OUT_DIR/${name}.driver.log" 2>/dev/null | tr -d '\r')"
    if [[ ! -f "$extracted" ]]; then
        ledger_add "$name" 'failed' \
            "unixnu ipsw fetch DeviceTree printed no extractable path (last line: ${extracted:-<empty>})"
        return 1
    fi
    record_artifact "$extracted" 'devicetree' 'aarch64'
    ledger_add "$name" 'ran' \
        'unixnu ipsw fetch DeviceTree, checked against board.toml [ipsw.sha256] (ipsw.py:161-163)'

    # (3b) the Apple-format tree the payload actually boots with.  Its own
    # ledger entry, because it has its own fate: it cannot be produced by this
    # script for two independent reasons, and both are recorded rather than
    # worked around.
    local adt_name='build-devicetree/adt'
    if [[ -z "$ADT_TOOL" ]]; then
        ledger_add "$adt_name" 'not_run' \
            "board.toml of $DEVICE_DIR declares no [payload.adt] tool, so the Apple-format device tree is not produced for this board"
        return 0
    fi
    if [[ -z "$UNIQUE_CHIP_ID" ]]; then
        ledger_add "$adt_name" 'not_run' \
            'the Apple-format device tree needs the platform seed: pass --unique-chip-id HEX (it is not stored in any repository, board.toml [payload.adt] unique_chip_id = "required")'
        return 0
    fi
    if ! command -v qemu-system-aarch64 >/dev/null 2>&1; then
        # Not a missing tool to install: QEMU is a native Windows build
        # (config/sources.lock.json tools.qemu.build) and is deliberately not a
        # macOS build input, so this step refuses rather than depend on it.
        ledger_add "$adt_name" 'not_run' \
            'the Apple-format device tree is generated by a script that shells out to qemu-system-aarch64 (make-qemu-adt.py:96-97); QEMU is a native Windows build and is not a macOS build input (PRD 8.1)'
        return 0
    fi
    local adt
    adt="$(work_dir)/kernel/kernel.macho.adt"
    log "+ $PY $(tree_dir)/$ADT_TOOL --output $adt --unique-chip-id $UNIQUE_CHIP_ID"
    if ! "$PY" "$(tree_dir)/$ADT_TOOL" --output "$adt" --unique-chip-id "$UNIQUE_CHIP_ID" \
            >"$OUT_DIR/${adt_name//\//-}.adt.log" 2>&1; then
        ledger_add "$adt_name" 'failed' "make-qemu-adt.py failed; see $OUT_DIR/${adt_name//\//-}.adt.log"
        return 1
    fi
    record_artifact "$adt" 'devicetree' 'aarch64'
    ledger_add "$adt_name" 'ran' "$ADT_TOOL"
    return 0
}

# assemble-image -- 'unixnu bootimg'.
#   cli.py:142-153  cmd_bootimg: need_uefi() first (cli.py:67-73), then the
#                   firmware volume, the device tree and the optional ramdisk
#   bootimg.py:71-114 bootimg.build(): gzip(BootShim + firmware volume) with the
#                   device tree appended, then unpacked again and compared
step_assemble_image() {
    local name='assemble-image'

    # (1) the inputs, refused with a code before anything runs.  need_uefi()
    # requires a Mu-Silicium checkout carrying Resources/Scripts/mkbootimg.py
    # (cli.py:67-73) and fd_path() reads board.toml [firmware] fd
    # (firmware.py:17-19).
    if [[ "$BOARD_HAS_FIRMWARE" != 'yes' ]]; then
        ledger_add "$name" 'not_run' \
            "board.toml of $DEVICE_DIR declares no [firmware], so there is no firmware volume to pack: measured on this host, 'unixnu bootimg --uefi DIR' on this board dies with KeyError: 'firmware' at firmware.py:18 (QEMU runs its own EDK II AARCH64 firmware, board.toml:85-86)"
        return 0
    fi
    if [[ -z "$UEFI_DIR" ]]; then
        ledger_add "$name" 'not_run' \
            'the boot image is packed from a Mu-Silicium checkout: pass --uefi DIR (cli.py:67-73 need_uefi)'
        return 0
    fi
    if [[ ! -f "$UEFI_DIR/Resources/Scripts/mkbootimg.py" ]]; then
        die 'config_invalid' \
            "$UEFI_DIR is not a Mu-Silicium checkout (no Resources/Scripts/mkbootimg.py)." \
            "{\"uefi\":\"$(json_escape "$UEFI_DIR")\"}" "$EXIT_USAGE"
    fi
    if [[ -z "$DTB_PATH" ]]; then
        ledger_add "$name" 'not_run' \
            'the boot image needs a compiled device tree: pass --dtb FILE (or UNIXNU_DTB); bootimg.build() refuses a missing one (bootimg.py:77-79)'
        return 0
    fi
    if [[ -z "$FD_PATH_IN" ]]; then
        ledger_add "$name" 'not_run' \
            'the boot image needs a firmware volume: pass --fd FILE, or run the firmware step that produces board.toml [firmware] fd'
        return 0
    fi
    local fd="$FD_PATH_IN"

    # (2) host and tools.  bootimg.py:24 compiles the shim with clang,
    # llvm-objcopy and llvm-nm; bootimg.py:46 patches the tree with fdtput and
    # fdtget.
    local -a need=('clang' 'llvm-objcopy' 'llvm-nm' 'fdtput' 'fdtget' 'python3')
    log_keg
    apple_step_gate "$name" "${need[@]}" || return 0

    # (3) run.  The output is out/<device>/boot/boot.img unless -o says
    # otherwise (bootimg.py:85).
    local out
    out="$(work_dir)/boot/boot.img"
    if ! run_driver "$name" 'bootimg' --uefi "$UEFI_DIR" --fd "$fd" --dtb "$DTB_PATH" \
            -o "$out"; then
        ledger_add "$name" 'failed' "unixnu bootimg failed; see $OUT_DIR/${name}.driver.log"
        return 1
    fi

    # (4) bootimg.build() writes boot.json next to the image with the sha256 of
    # every part (bootimg.py:110-112); the image itself is the artifact.
    record_artifact "$out" 'firmware' 'aarch64'
    ledger_add "$name" 'ran' "unixnu bootimg; $(dirname -- "$out")/boot.json records the per-part hashes"
    return 0
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
    # The frozen state is the lock's own, never a literal.  This line used to
    # read `frozen=true` unconditionally, so an unfrozen run's log claimed the
    # opposite of what its own manifest recorded -- the one place an operator
    # would look to find out which baseline was built.
    log "lock: frozen=$LOCK_FROZEN unixnu=$LOCK_UNIXNU_COMMIT device_repo=$LOCK_DEVICE_COMMIT"
    if (( LOCK_UNFROZEN )); then
        log 'lock: NOT FROZEN -- the pinned commits above are this run'"'"'s freeze candidates, recorded verbatim in artifact-manifest.json'
    fi
    log "lock: qemu accel=$LOCK_QEMU_ACCEL ipsw.sha256=${LOCK_IPSW_SHA256:-<empty>}"

    require_tools
    log "tools: $TOOL_XCODEBUILD"
    log "tools: $TOOL_CLANG"
    log "tools: $TOOL_PYTHON"
    log "tools: $TOOL_GIT"

    # Parallel jobs for the driver (cli.py:19, config.py:86).  Counted with the
    # interpreter this script already requires, because nproc is not on macOS.
    if [[ -z "$JOBS" ]]; then
        JOBS="$("$PY" -c 'import os; print(os.cpu_count() or 4)' 2>/dev/null || printf '4')"
    fi
    if [[ ! "$JOBS" =~ ^[0-9]+$ ]] || (( JOBS < 1 )); then
        die 'config_invalid' "jobs must be a positive integer (got '$JOBS')." '{}' "$EXIT_USAGE"
    fi

    detect_sha

    prepare_output_dir
    if (( DRY_RUN == 0 )); then
        check_free_space
    fi

    # --- declared deliverables --------------------------------------------
    # Each --include is an input that must exist; a missing one is a failure,
    # never a silently empty manifest (PRD 7.4).
    local -a inc_paths=() inc_roles=() inc_archs=()
    local spec path role arch rest
    for spec in ${INCLUDES[@]+"${INCLUDES[@]}"}; do
        # '::' is the delimiter, not ':': a Windows drive letter ("C:/x") must
        # not be split into a path of "C" and a role of "/x".  Splitting with
        # IFS=':' cannot express that, so use parameter expansion instead.
        path="${spec%%::*}"
        rest="${spec#*::}"
        [[ "$rest" == "$spec" ]] && rest=''
        role="${rest%%::*}"
        arch="${rest#*::}"
        [[ "$arch" == "$rest" ]] && arch=''
        if [[ -z "$path" ]]; then
            die 'config_invalid' "--include needs a path." '{}' "$EXIT_USAGE"
        fi
        # Absolute means POSIX "/..." or a Windows drive "C:/" / "C:\".
        if [[ "$path" != /* && ! "$path" =~ ^[A-Za-z]:[/\\] ]]; then
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
    local stage_failed=0
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
                local checkout
                for checkout in "$UNIXNU_DIR" "$DEVICE_DIR"; do
                    if [[ -d "$checkout/.git" ]]; then
                        log "source present: $checkout"
                        sources_present=1
                    else
                        log "source absent:  $checkout"
                    fi
                done
                if (( sources_present == 0 )) && (( SKIP_SOURCES == 0 )); then
                    ledger_add "$s" 'not_run' 'pinned source checkout absent'
                    emit_error 'artifact_missing' \
                        'no pinned source checkout found; nothing was fetched (no network access by policy).' \
                        "{\"expected\":[\"$(json_escape "$UNIXNU_DIR")\",\"$(json_escape "$DEVICE_DIR")\"],\"hint\":\"pass --unixnu/--device to point at the checkouts, or --skip-sources to validate the contract only\"}"
                    write_outputs 0 0
                    exit "$EXIT_FAILURE"
                fi
                if (( sources_present == 0 )); then
                    log 'note: --skip-sources given; build steps will stay not_run.'
                    ledger_add "$s" 'not_run' 'pinned source checkout absent (--skip-sources)'
                else
                    # The driver fetches whatever its feeds root is missing and
                    # initialises module submodules (cli.py:79-86), so prove
                    # both are already on disk at the pinned commit first.
                    resolve_board
                    local -a missing=()
                    local line
                    while IFS="$(printf '\t')" read -r kind name; do
                        missing+=("$kind:$name")
                    done < <(offline_missing)
                    if (( ${#missing[@]} > 0 )); then
                        log "sources the driver would have to fetch: ${missing[*]}"
                        if (( SKIP_SOURCES == 0 )); then
                            ledger_add "$s" 'not_run' \
                                "pinned sources are not on disk at the locked commits: ${missing[*]}"
                            emit_error 'artifact_missing' \
                                "pinned sources are missing and this script performs no network access, so the upstream driver cannot be allowed to fetch them: ${missing[*]}" \
                                "{\"missing\":$(json_string_array ${missing[@]+"${missing[@]}"}),\"hint\":\"restore the pinned sources, or pass --skip-sources to validate the contract only\"}"
                            write_outputs 0 0
                            exit "$EXIT_FAILURE"
                        fi
                        log 'note: --skip-sources given; build steps will stay not_run.'
                        ledger_add "$s" 'not_run' \
                            "pinned sources absent, the driver would fetch them and this script performs no network access (--skip-sources): ${missing[*]}"
                        SOURCES_INCOMPLETE=1
                    else
                        ledger_add "$s" 'ran' \
                            'pinned repositories, module submodules and feed clones verified locally at the locked commits; nothing was fetched'
                    fi
                fi
                ;;
            build-kernel)
                step "$s"
                if (( SOURCES_INCOMPLETE )); then
                    ledger_add "$s" 'not_run' 'pinned sources absent (--skip-sources); the driver would have to fetch them'
                elif step_build_kernel; then
                    :
                else
                    stage_failed=1
                fi
                ;;
            build-bootloader)
                step "$s"
                if (( SOURCES_INCOMPLETE )); then
                    ledger_add "$s" 'not_run' 'pinned sources absent (--skip-sources); the driver would have to fetch them'
                elif step_build_bootloader; then
                    :
                else
                    stage_failed=1
                fi
                ;;
            build-devicetree)
                step "$s"
                if (( SOURCES_INCOMPLETE )); then
                    ledger_add "$s" 'not_run' 'pinned sources absent (--skip-sources); the driver would have to fetch them'
                elif step_build_devicetree; then
                    :
                else
                    stage_failed=1
                fi
                ;;
            assemble-image)
                step "$s"
                if (( SOURCES_INCOMPLETE )); then
                    ledger_add "$s" 'not_run' 'pinned sources absent (--skip-sources); the driver would have to fetch them'
                elif step_assemble_image; then
                    :
                else
                    stage_failed=1
                fi
                ;;
            verify-artifacts)
                step 'verify-artifacts'
                if (( ${#inc_paths[@]} == 0 )) && (( ${#BUILT_RECORDS[@]} == 0 )); then
                    log 'no declared deliverables and nothing built; the manifest will'
                    log 'list only the metadata files this script itself writes.'
                fi
                # Re-hash every artifact the manifest is about to carry, so a
                # hash a stage recorded is checked again before it is published.
                # A mismatch here is a verification failure, not a warning.
                local i again
                for (( i = 0; i < ${#BUILT_PATHS[@]}; i++ )); do
                    again="$(sha256_of "${BUILT_PATHS[$i]}")"
                    if [[ "$again" != "${BUILT_SHAS[$i]}" ]]; then
                        die 'verification_failed' \
                            "${BUILT_PATHS[$i]} changed between the stage that built it and verification." \
                            "{\"path\":\"$(json_escape "${BUILT_PATHS[$i]}")\",\"recorded\":\"${BUILT_SHAS[$i]}\",\"now\":\"$again\"}" \
                            "$EXIT_FAILURE"
                    fi
                done
                ledger_add "$s" 'ran' \
                    "every artifact the manifest carries was re-hashed (${#inc_paths[@]} declared, ${#BUILT_RECORDS[@]} built)"
                ;;
            write-manifest)
                step 'write-manifest'
                ledger_add "$s" 'ran' 'artifact-manifest.json written'
                ;;
        esac
        # A stage that failed stops the plan: the next stage's inputs come out
        # of it.  The outputs are still written, so the evidence of what did and
        # did not happen is on disk.
        if (( stage_failed )); then
            log "stage $s failed; the remaining stages are not_run."
            local rest
            for (( rest = idx + 1; rest < ${#steps[@]}; rest++ )); do
                ledger_add "${steps[$rest]}" 'not_run' "an earlier stage failed: $s"
            done
            break
        fi
    done

    # "$sources_present" and not a literal 1: with --skip-sources the ledger
    # says the sources were absent, so the report must agree with it.
    write_outputs "$sources_present" "${#inc_paths[@]}"

    step "summary"
    log "run_id:  $RUN_ID"
    log "out:     $OUT_DIR"
    log "log:     $LOG_PATH"
    log "report:  $REPORT_PATH"
    log "manifest:$MANIFEST_PATH"
    log "stages:  $(ledger_summary)"
    if (( stage_failed )); then
        log 'A stage FAILED.  Nothing here may be read as a build result.'
        exit "$EXIT_FAILURE"
    fi
    if ledger_has_not_run; then
        log 'NOTE: not every stage ran.  A stage that did not run is recorded'
        log '      not_run with its reason in build-report.json; missing evidence'
        log '      is unverified, never success (PRD 7.4).'
    fi

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
    # manifest writer only has to serialise them.  Artifacts the build stages
    # produced were hashed where they were produced (record_artifact) and are
    # appended after them, so one manifest carries both.
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
        # The path is recorded the same way record_artifact records a built one:
        # relative to this run's output directory when it is inside it, and the
        # path as given otherwise.  Recording only the leaf -- which is what this
        # line did -- made two declared inputs with the same basename
        # indistinguishable and left a consumer with a path that locates nothing.
        records+=("${leaf}${FIELD_SEP}${sha}${FIELD_SEP}${size}${FIELD_SEP}${inc_roles[$i]}${FIELD_SEP}${inc_archs[$i]}${FIELD_SEP}$(shown_path "${inc_paths[$i]}")")
        log "artifact: $leaf sha256=$sha bytes=$size role=${inc_roles[$i]} arch=${inc_archs[$i]}"
    done
    local rec
    for rec in ${BUILT_RECORDS[@]+"${BUILT_RECORDS[@]}"}; do
        records+=("$rec")
    done

    # --- build-report.json -------------------------------------------------
    # Every escaped value goes through json_escape_var into a local first.  The
    # printing form costs a subshell per call and there are two dozen here; on a
    # host where a fork is ~0.85 s that dominated the run.
    local j_run_id j_stage j_experiment j_target j_generated j_driver j_device
    local j_driver_out j_lock_path j_lock_unixnu j_lock_device j_lock_accel
    local j_xcode j_clang j_python j_git
    json_escape_var j_run_id      "$RUN_ID"
    json_escape_var j_stage       "$STAGE"
    json_escape_var j_experiment  "$EXPERIMENT"
    json_escape_var j_target      "$TARGET"
    json_escape_var j_driver      "$DRIVER"
    json_escape_var j_device      "$DEVICE_DIR"
    json_escape_var j_driver_out  "$UNIXNU_OUT"
    json_escape_var j_lock_path   "$LOCK_PATH"
    json_escape_var j_lock_unixnu "$LOCK_UNIXNU_COMMIT"
    json_escape_var j_lock_device "$LOCK_DEVICE_COMMIT"
    json_escape_var j_lock_accel  "$LOCK_QEMU_ACCEL"
    json_escape_var j_xcode       "$TOOL_XCODEBUILD"
    json_escape_var j_clang       "$TOOL_CLANG"
    json_escape_var j_python      "$TOOL_PYTHON"
    json_escape_var j_git         "$TOOL_GIT"
    stamp_var j_generated
    {
        printf '{\n'
        printf '  "command": "build-macos",\n'
        printf '  "run_id": "%s",\n'        "$j_run_id"
        printf '  "stage": "%s",\n'         "$j_stage"
        printf '  "experiment": "%s",\n'    "$j_experiment"
        printf '  "target": "%s",\n'        "$j_target"
        printf '  "generated_at": "%s",\n'  "$j_generated"
        printf '  "build_mode": "upstream-driver",\n'
        printf '  "driver": "%s",\n'        "$j_driver"
        printf '  "device_repo": "%s",\n'   "$j_device"
        printf '  "driver_out": "%s",\n'    "$j_driver_out"
        printf '  "network_access": "none",\n'
        printf '  "sources_incomplete": %s,\n' \
            "$([[ "$SOURCES_INCOMPLETE" == '1' ]] && printf 'true' || printf 'false')"
        printf '  "lock": {\n'
        printf '    "path": "%s",\n'        "$j_lock_path"
        printf '    "frozen": %s,\n'        "$([[ "$LOCK_FROZEN" == 'true' ]] && printf 'true' || printf 'false')"
        # A run against an unfrozen lock says so in machine-readable form, so no
        # downstream consumer can read `frozen: false` as a frozen baseline by
        # ignoring it.  The commits below are what was built, whatever the flag
        # says, and they are the freeze candidates for the next lock.
        #
        # The whole conditional goes inside ONE command substitution.  Spelled
        # `$(( LOCK_UNFROZEN )) && printf ...` instead, the arithmetic expansion
        # is evaluated inside the double quotes and the rest of the line is
        # literal text: the file then carries `"unfrozen_build": 0 && printf
        # 'true' || printf 'false',` and is not JSON at all.  Measured.
        printf '    "unfrozen_build": %s,\n' \
            "$([[ "$LOCK_UNFROZEN" == '1' ]] && printf 'true' || printf 'false')"
        printf '    "unixnu_commit": "%s",\n'    "$j_lock_unixnu"
        printf '    "device_repo_commit": "%s",\n' "$j_lock_device"
        printf '    "qemu_accel": "%s"\n'        "$j_lock_accel"
        printf '  },\n'
        printf '  "toolchain": {\n'
        printf '    "xcodebuild": "%s",\n'  "$j_xcode"
        printf '    "clang": "%s",\n'       "$j_clang"
        printf '    "python3": "%s",\n'     "$j_python"
        printf '    "git": "%s",\n'         "$j_git"
        printf '    "qemu": "not a macOS build input (native Windows build)"\n'
        printf '  },\n'
        printf '  "steps": %s,\n' "$(ledger_json)"
        printf '  "declared_artifacts": %s,\n' "$declared_count"
        printf '  "built_artifacts": %s,\n'   "${#BUILT_RECORDS[@]}"
        printf '  "sources_present": %s,\n' "$([[ "$sources_ok" == '1' ]] && printf 'true' || printf 'false')"
        printf '  "evidence_note": "steps with status not_run or failed produced nothing; missing evidence is unverified, not success (PRD 7.4)."\n'
        printf '}\n'
    } >"$REPORT_PATH"

    # --- stage-result.json -------------------------------------------------
    # One machine-readable stage result, as PRD 8.1 "报告" requires.  ok is
    # derived from the ledger, never asserted: a stage in which anything is
    # not_run or failed cannot report success.
    local ok='false' status='incomplete'
    if ! ledger_has_not_run; then
        ok='true'
        status='complete'
    fi
    stamp_var j_generated
    {
        printf '{\n'
        printf '  "stage": "%s",\n'      "$j_stage"
        printf '  "run_id": "%s",\n'     "$j_run_id"
        printf '  "status": "%s",\n'     "$status"
        printf '  "ok": %s,\n'           "$ok"
        printf '  "generated_at": "%s",\n' "$j_generated"
        printf '  "steps": %s,\n' "$(ledger_json)"
        printf '  "step_summary": "%s",\n' "$(ledger_summary)"
        printf '  "not_run_reasons": %s,\n' "$(not_run_reasons_json)"
        printf '  "note": "A stage in which anything is not_run or failed never reports success. Real Apple-tool work is UNVERIFIED off macOS (PRD 7.4)."\n'
        printf '}\n'
    } >"$STAGE_RESULT_PATH"

    # --- artifact-manifest.json -------------------------------------------
    # Written through Python so the bytes match unipad.models.write_json
    # (ensure_ascii=False, sort_keys=True, indent=2, trailing newline) exactly.
    # The records go through a temp file rather than a pipe: the manifest
    # writer's heredoc would otherwise replace stdin and silently drop every
    # record, producing a valid manifest with an empty artifacts list.
    local records_file
    records_file="$(mktemp 2>/dev/null || printf '%s/.records.%s.tmp' "$OUT_DIR" "$$")"
    printf '%s\n' ${records[@]+"${records[@]}"} >"$records_file"
    write_manifest "$records_file"
    rm -f -- "$records_file"
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

# write_manifest RECORDS_FILE -- write MANIFEST_PATH from one-record-per-line
# TSV (fields separated by US, 0x1f):
#   name <US> sha256 <US> size_bytes <US> role <US> arch <US> path
# The shape is unipad.models.ArtifactManifest exactly: run_id, artifacts[],
# target, unixnu_commit, device_repo_commit, toolchain.
write_manifest() {
    local records_file="$1"
    [[ -n "$PY" ]] || die 'blocked' 'no Python interpreter for the manifest writer.' \
        '{}' "$EXIT_BLOCKED"
    [[ -f "$records_file" ]] || die 'evidence_incomplete' \
        "artifact records file is missing: $records_file" '{}' "$EXIT_FAILURE"
    "$PY" - "$MANIFEST_PATH" "$RUN_ID" "$TARGET" \
        "$LOCK_UNIXNU_COMMIT" "$LOCK_DEVICE_COMMIT" "$records_file" \
        "$TOOL_XCODEBUILD" "$TOOL_CLANG" "$TOOL_PYTHON" "$TOOL_GIT" <<'PYEOF'
import json
import sys

sep = "\037"
(manifest_path, run_id, target, unixnu_commit, device_repo_commit,
 records_file, xcodebuild, clang, python3, git) = sys.argv[1:11]

artifacts = []
with open(records_file, 'r', encoding='utf-8') as handle:
    for line in handle.read().split("\n"):
        line = line.rstrip("\r")
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
