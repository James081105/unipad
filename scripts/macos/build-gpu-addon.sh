#!/usr/bin/env bash
# Build the GPU addon blob (vendor/UniXNU/build/gpu-addon.ngpu) from the pinned
# sources.  Runs on a macOS GitHub runner; the Windows host cannot run it.
#
# Why this exists
# ---------------
# The kernel mounts this blob when the guest is booted with `sheng-gpu-addon=1`
# (vendor/UniXNU-qemu/board.toml).  Until it exists the addon mount is attempted
# and refused ENOENT -- measured on a stage-10 boot as native_gpu_addon_stage=1 /
# native_gpu_addon_status=2 -- which means the guest has no userspace client for
# the display at all: frame_count=0, copy_bytes_sum=0, transfer_calls=0,
# gpu_gpu_offers=0 across every boot so far.  The blob is the missing client.
#
# The chain has eight rings and this script is rings 1..7; ring 0 is
# scripts/macos/bootstrap-mesa-turnip.sh (plus bootstrap-metal2vulkan.sh), which
# the workflow runs first.  Every ring's output log lands in build-output/ so a
# failure can be read from the artifact rather than re-run.
#
# Why the rings are sequential
# ----------------------------
# Ring 4 (the Rust bridge) needs nothing from the Mesa build -- its inputs are
# the pinned translator source and cargo -- so it could run alongside rings 1..3
# and save perhaps a fifth of the wall clock.  It does not, deliberately.  Two
# heavy builds sharing a three-core runner is not a 2x win, and the failure mode
# of a background job whose log interleaves is a run that fails for a reason
# nothing in the artifact explains.  The job budget is 180 minutes and the chain
# is estimated well inside it; every ring now logs its own elapsed seconds, so if
# that estimate turns out to be wrong the numbers will say which ring to
# parallelise and it can be done against a measurement rather than a hunch.
#
# What this script does NOT do, stated because each is a real gap and not an
# oversight:
#   * the wallpaper overlay (make-wallpaper-overlay.py) -- needs the mounted IPSW
#     OS volume and `sips`; the blob is built without --wallpaper, so the guest
#     keeps its sealed animation asset.
#   * --prime-list -- build/prime-list.txt is produced by a step that has not run.
#   * the two host checks the pinned tree does not contain.  One of them
#     (check-quartzcore-turnip-cache-host.py) is skipped explicitly and loudly by
#     scripts/macos/run-upstream-check.py; the other (check-quartzcore-blur-host.py,
#     called from check-native-metal-frontend-host.py:43) is not on this chain.
set -uo pipefail

# Paths that arrive from the environment or argv are normalised to forward
# slashes.  This script runs on macOS, where every path is already POSIX and the
# substitution is a no-op; it exists because a Windows host invoking it is not a
# hypothetical -- this repository is developed on one, and a test does exactly
# that.  MEASURED: passing a Windows path through unchanged made `$LOG` become
# "$PWD/C:\Users\...\logs/build-gpu-addon.log", which is a path no mkdir can
# create, so the script died with "cannot write the log" and a message that
# showed the mangling rather than naming the cause.
to_posix() { printf '%s' "$1" | tr '\\' '/'; }

REPO="$(to_posix "${1:?usage: build-gpu-addon.sh REPO_ROOT [OUTPUT]}")"
OUTPUT="$(to_posix "${2:-vendor/UniXNU/build/gpu-addon.ngpu}")"

# Absolute, and load-bearing for the same reason bootstrap-mesa-turnip.sh gives:
# every upstream script resolves its own paths from __file__, so they are already
# absolute; this one is not, and a relative REPO breaks the moment the script
# changes directory.
UP="$(cd "$REPO" 2>/dev/null && pwd)/vendor/UniXNU"
[[ -d "$UP/.git" ]] || { echo "no $REPO/vendor/UniXNU; ring 0 (the bootstraps) runs first" >&2; exit 1; }

if command -v python3 >/dev/null 2>&1; then PY='python3'
elif command -v python  >/dev/null 2>&1; then PY='python'
else PY=''; fi
[[ -n "$PY" ]] || { echo "no python interpreter on PATH" >&2; exit 1; }

LOGDIR="${ADDON_LOG_DIR:-$REPO/build-output}"
LOGDIR="$(to_posix "$LOGDIR")"
mkdir -p "$LOGDIR" || { echo "cannot create $LOGDIR" >&2; exit 1; }
LOG="$LOGDIR/build-gpu-addon.log"
# Absolute is /* on POSIX and X:/ on Windows, and both are left alone.  A
# relative path is resolved against $PWD, because a log that moves when the
# script moves is a log nobody can find -- the neighbouring bootstrap lost a
# failure's own output that way (its bison build's subshell), and the lesson
# generalises.
case "$LOG" in /*|[A-Za-z]:/*) ;; *) LOG="$PWD/$LOG" ;; esac
: > "$LOG" || { echo "cannot write the log $LOG" >&2; exit 1; }

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }
die() { log "FAIL $*"; log "---- last 25 lines of $LOG ----"; tail -25 "$LOG" | tee -a "$LOG" >&2; exit 1; }

# Run one ring.  The ring's own stdout/stderr goes to its own log file AND to the
# shared log, so the artifact carries both the per-ring detail and the sequence.
# `|| die` rather than `set -e`: the failure message has to name the ring, and a
# bare `set -e` exit would leave the reader with a nonzero status and no sentence.
#
# Each ring reports how long it took.  That is not decoration: the workflow's
# `timeout-minutes` is an estimate until a run has produced these numbers, and a
# chain that silently overruns a job budget fails at the job boundary with no
# indication of which ring was still going.
ring() {
    local name="$1"; shift
    local file="$LOGDIR/addon-$name.log"
    local started ended status=0
    started="$(date +%s)"
    log "=== ring: $name ==="
    log "+ $*"
    "$@" >"$file" 2>&1 || status=$?
    ended="$(date +%s)"
    if [ "$status" -eq 0 ]; then
        log "PASS $name in $((ended - started))s (its log: $file)"
        return 0
    fi
    log "FAIL $name after $((ended - started))s (exit $status); its log: $file"
    tail -25 "$file" | tee -a "$LOG" >&2
    die "$name failed"
}

# ---------------------------------------------------------------------------
# Tool preflight.  Every tool is named with the ring that needs it, because
# "command not found" from ring 6 after forty minutes of compiling is the
# expensive way to learn a formula was missing.
# ---------------------------------------------------------------------------
# `glslc` is the one that is easy to get wrong: it is NOT in Homebrew's `glslang`
# formula (that one ships glslangValidator and the spirv-* tools, and the Mesa
# bootstrap already installs it).  glslc comes from `shaderc`.  Rather than
# assert that from memory, this installs shaderc and then asks all three tools a
# question they can only answer by existing -- the instrument is validated before
# its answer is trusted.
log "preflight: the tools each ring needs"
if ! command -v glslc >/dev/null 2>&1; then
    log "glslc is absent; installing shaderc (gen-turnip-smoke-shaders.py:15 calls glslc directly)"
    brew install shaderc >>"$LOG" 2>&1 || die "cannot install shaderc, which is what provides glslc"
fi
for tool in glslc spirv-val; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is absent; gen-turnip-smoke-shaders.py:15-16 needs it and no formula was found that provides it"
done
glslc --version >>"$LOG" 2>&1 || die "glslc is on PATH but does not answer --version"
spirv-val --version >>"$LOG" 2>&1 || die "spirv-val is on PATH but does not answer --version"
log "preflight: glslc and spirv-val both answer"

# The Vulkan headers at /opt/homebrew/include.  check-turnip-darwin-host.py:55
# and :75 compile vulkan_smoke.c and deferred_bo.cpp with `-I
# /opt/homebrew/include`, and vulkan_smoke.c:9 includes <vulkan/vulkan.h>.
# Nothing puts those headers there by default: `brew install glslang`, which is
# what the Mesa bootstrap installs, provides glslangValidator and the spirv-*
# tools and NOT the Vulkan headers -- they come from vulkan-headers.  Without
# this the ring fails on its fifth compile, after the four ArmSmmu4K objects and
# the IOKit shim have already been built, which is the expensive place to learn
# a formula was missing.
#
# And a header that is present is not the same as a header that is usable, so it
# is proved by compiling a translation unit against it rather than by testing
# for the file.  The instrument answers a known-present question before its
# answer is trusted.
if ! printf '#include <vulkan/vulkan.h>\nint main(void){return 0;}\n' \
        | xcrun clang -x c -I /opt/homebrew/include -o /dev/null - >>"$LOG" 2>&1; then
    log "the Vulkan headers are not usable at /opt/homebrew/include; installing vulkan-headers"
    brew install vulkan-headers >>"$LOG" 2>&1 || die "cannot install vulkan-headers, which provides /opt/homebrew/include/vulkan/vulkan.h"
    printf '#include <vulkan/vulkan.h>\nint main(void){return 0;}\n' \
        | xcrun clang -x c -I /opt/homebrew/include -o /dev/null - >>"$LOG" 2>&1 \
        || die "vulkan-headers is installed but <vulkan/vulkan.h> still does not compile against /opt/homebrew/include"
fi
log "preflight: <vulkan/vulkan.h> compiles against /opt/homebrew/include"

command -v codesign >/dev/null 2>&1 || die "codesign is absent; build-adreno-metal-plugin.py signs the bundle and the Turnip library with it"
command -v xcrun    >/dev/null 2>&1 || die "xcrun is absent; every ring that compiles resolves its SDK through it"
# build-adreno-metal-plugin.py:47 and :62 call `strip -x` on the bundle
# executable and on the Turnip library.  It ships with the Xcode command-line
# tools, so in practice it is always there -- which is exactly why it is worth
# naming, because a runner image without the CLT fails here in one line instead
# of forty minutes into ring 6.
command -v strip    >/dev/null 2>&1 || die "strip is absent; build-adreno-metal-plugin.py:47,62 strip the bundle executable and the Turnip library with it"
command -v git      >/dev/null 2>&1 || die "git is absent; the pin checks in this script and in build-turnip-darwin.py:34 resolve through it"

# The Rust toolchain pin is spelled in build-native-metal-bridge.py:82 as
# `cargo +1.98.0`, so the check is the same invocation: if the pin is not
# installed, cargo answers with its own message about the toolchain, which is
# more use than anything this script could invent.
cargo +1.98.0 --version >>"$LOG" 2>&1 || die "cargo +1.98.0 does not answer; rustup install the pinned toolchain (build-native-metal-bridge.py:82)"
rustup target list --installed 2>/dev/null | grep -qx 'aarch64-apple-ios' \
    || log "note: aarch64-apple-ios is not in `rustup target list --installed`; ring 4 will say so if it needs it"

LLVM_AS="$(command -v llvm-as || true)"
[[ -z "$LLVM_AS" && -x /opt/homebrew/opt/llvm/bin/llvm-as ]] && LLVM_AS=/opt/homebrew/opt/llvm/bin/llvm-as
[[ -n "$LLVM_AS" ]] || die "llvm-as is absent; build-native-metal-bridge.py:54-56 probes /opt/homebrew/opt/llvm/bin/llvm-as then PATH (brew install llvm)"
log "preflight: llvm-as is $LLVM_AS"

# The Metal bridge reads a second pinned source that ring 0's bootstraps fetch.
# Checking it here rather than letting cargo fail is what makes the failure name
# the missing bootstrap instead of a Rust path error.
TRANSLATOR="$UP/.cache/gpu-reference/metal2vulkan"
[[ -d "$TRANSLATOR/.git" ]] || die "no $TRANSLATOR; ring 0's bootstrap-metal2vulkan.sh has not run"
TRANSLATOR_HEAD="$(git -C "$TRANSLATOR" rev-parse HEAD 2>/dev/null | tr -d '\r')"
TRANSLATOR_PIN="$("$PY" - "$UP/scripts/data/gpu-metal-vulkan-inputs.json" <<'PY' | tr -d '\r'
import json, sys
print(json.loads(open(sys.argv[1], encoding="utf-8").read())["translator"]["commit"])
PY
)"
[[ "$TRANSLATOR_HEAD" == "$TRANSLATOR_PIN" ]] \
    || die "the translator checkout is ${TRANSLATOR_HEAD:0:12}, the pin is ${TRANSLATOR_PIN:0:12}; run bootstrap-metal2vulkan.sh"
log "preflight: the translator is at its pin ${TRANSLATOR_PIN:0:12}"

# ---------------------------------------------------------------------------
# The rings.  Order is the dependency order documented in
# docs/implementation-plan.md: 1 host Turnip, 2 the host kernel-stack simulation
# (which is where the one absent upstream check is skipped), 3 the replay header,
# 4 the Rust bridge, 5 the iOS Turnip cross-build, 6 the plugin bundle, 7 the blob.
# ---------------------------------------------------------------------------
ring turnip-host  "$PY" "$UP/scripts/build-turnip-darwin.py" --target host

# Ring 2 is the only ring that needs the wrapper.  check-turnip-darwin-host.py:93
# runs check-quartzcore-turnip-cache-host.py, which the pinned tree does not
# contain -- verified absent in vendor/UniXNU, vendor/UniXNU-qemu and upstream
# UniXNU main, while docs/drivers/native-adreno.md:138 still documents it.  The
# wrapper skips that one call, announces it on stdout, and lets every other call
# and every nonzero exit through; its own tests pin that behaviour.  What this
# buys is ring 3: the script writes build/turnip-darwin-host/plugin_smoke at
# line 57-61, BEFORE the failing line, and gen-turnip-replay.py:23 skips the
# whole check when that binary exists.
ABSENT_QUARTZCORE="$UP/scripts/check-quartzcore-turnip-cache-host.py"
ring turnip-host-check "$PY" "$REPO/scripts/macos/run-upstream-check.py" \
    "$UP/scripts/check-turnip-darwin-host.py" "$ABSENT_QUARTZCORE"

# Ring 3 is a no-op call when plugin_smoke exists (gen-turnip-replay.py:23), and
# a real run otherwise.  Either way it is the script's own decision, not ours.
ring turnip-replay "$PY" "$UP/scripts/gen-turnip-replay.py"

ring metal-bridge "$PY" "$UP/scripts/build-native-metal-bridge.py" --target ios

ring turnip-ios "$PY" "$UP/scripts/build-turnip-darwin.py" --target ios

# Ring 6 runs gen-turnip-smoke-shaders.py and build-native-metal-bridge.py again
# itself (its lines 28 and 31); both are idempotent, and the script is the one
# that knows its own order, so it is invoked as-is rather than pre-run piecemeal.
ring metal-plugin "$PY" "$UP/scripts/build-adreno-metal-plugin.py"

# Ring 7.  --publish is not cosmetic: without NativeAdrenoMetal.publish beside the
# bundle, backboardd does not return the native Metal device to the original
# discovery and the render server never composites through it
# (docs/drivers/native-adreno.md:36), which is the symptom this whole chain is
# meant to answer.  --clients '*' is the documented spelling for "every process"
# (:37); the exact iPadOS 17 process list is not known here, and a narrower list
# would make every unlisted process keep the software renderer -- the failure
# mode we are trying to measure past.  Narrow it once the boot shows which
# processes actually open a device.
[[ -e "$REPO/$OUTPUT" ]] && die "$OUTPUT already exists; make-gpu-addon.py:62 refuses to overwrite, so remove it first"
ring gpu-addon "$PY" "$UP/scripts/make-gpu-addon.py" --publish --clients '*' "$REPO/$OUTPUT"

ADDON="$REPO/$OUTPUT"
[[ -f "$ADDON" ]] || die "the chain reported success but $ADDON is not there"
log "PASS the GPU addon chain produced $ADDON ($(wc -c <"$ADDON" | tr -d ' ') bytes)"
log "note what this blob does NOT carry: no --wallpaper overlay and no --prime-list (see the header)"
