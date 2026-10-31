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
# The chain has nine rings and this script is rings 1..8; ring 0 is
# scripts/macos/bootstrap-mesa-turnip.sh (plus bootstrap-metal2vulkan.sh), which
# the workflow runs first.  Every ring's output log lands in build-output/ so a
# failure can be read from the artifact rather than re-run.
#
# The ninth is metal-bridge-host, added after run 40943937160: ring 2's own last
# check links the HOST copy of the metal bridge, and the chain had been building
# only the iOS one.  The count is written down because "rings 1..7" in this
# comment was already wrong once -- a comment describing a structure that has
# moved is the same defect as one describing a guard that does not exist.
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

# One record per ring, written to $SUMMARY at the end.  The log already says
# everything in it; what the log cannot do is be READ by the next step, and the
# next step is where the loss was.
#
# The defect this exists for: ``ring_best_effort`` returns 0 when a ring goes
# DEGRADED, by design -- the artifact exists and the chain must continue on it.
# So this script exits 0, and the workflow's ``if bash build-gpu-addon.sh .``
# therefore set ``built=true`` and moved on.  A run in which an assertion was
# given up was indistinguishable from a clean one in everything a reader looks
# at: exit status, GITHUB_OUTPUT, and the artifact itself.  The log said
# DEGRADED; nothing consumed it.
#
# So the state is written where a consumer can reach it, and the consumer is
# required to surface it separately.  Derived from the rings that ran rather
# than hand-listed, so a ring added next month is recorded without anyone
# remembering to.
RINGS=()
SUMMARY="$LOGDIR/addon-summary.json"

ring_record() {
    # ring_record NAME STATUS SECONDS [ARTIFACT] [DETAIL]
    local name="$1" status="$2" seconds="$3" artifact="${4:-}" detail="${5:-}"
    RINGS+=("$name|$status|$seconds|$artifact|$detail")
}

# ring_summary -- write $SUMMARY from the accumulated records.
#
# A JSON document rather than more log lines, for the same reason
# build-macos.sh writes build-report.json: the log is for a human reading a
# failure, and this is for the step that has to decide what to report.  Both are
# needed and they are not the same artifact.
ring_summary() {
    local first=1 entry name status seconds artifact detail
    local j_name j_status j_artifact j_detail
    {
        printf '{\n  "rings": ['
        for entry in ${RINGS[@]+"${RINGS[@]}"}; do
            IFS='|' read -r name status seconds artifact detail <<<"$entry"
            (( first )) || printf ','
            first=0
            printf '\n    {"name":"%s","status":"%s","seconds":%s,"artifact":"%s","detail":"%s"}' \
                "$(json_escape "$name")" "$(json_escape "$status")" "$seconds" \
                "$(json_escape "$artifact")" "$(json_escape "$detail")"
        done
        printf '\n  ],\n'
        printf '  "degraded": ['
        first=1
        for entry in ${RINGS[@]+"${RINGS[@]}"}; do
            IFS='|' read -r name status seconds artifact detail <<<"$entry"
            [[ "$status" == degraded ]] || continue
            (( first )) || printf ','
            first=0
            printf '"%s"' "$(json_escape "$name")"
        done
        printf '],\n'
        printf '  "failed": ['
        first=1
        for entry in ${RINGS[@]+"${RINGS[@]}"}; do
            IFS='|' read -r name status seconds artifact detail <<<"$entry"
            [[ "$status" == failed ]] || continue
            (( first )) || printf ','
            first=0
            printf '"%s"' "$(json_escape "$name")"
        done
        printf ']\n}\n'
    } >"$SUMMARY"
    log "ring summary written to $SUMMARY"
}

# json_escape STRING -- STRING as a JSON string body, without the quotes.
#
# Ring names and artifacts are ASCII paths, but the DETAIL field carries the
# failing assertion's text, which is upstream's and not guaranteed to be.  A
# quote or a backslash in it would otherwise produce a summary nobody can parse
# -- and a summary that fails to parse is read as "no degraded rings", which is
# the exact failure this whole mechanism exists to prevent.
json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' | tr -d '\n\r'
}

# Run one ring whose failure is survivable, because the ring's OUTPUTS are what
# the next ring needs and the failure is in an assertion AFTER them.
#
# This exists for ring 2 specifically, and the reasoning is measured rather than
# convenient.  Across runs 40932083150 / 40939171888 / 40943937160 / 40948699070
# the host simulation went from two compile errors, to a link error, to five of
# eight deferred-BO modes, to ALL EIGHT plus the whole priming cache -- and then
# failed on its LAST assertion, the plugin watchdog:
#     The plugin watchdog did not sample the stalled worker as expected
#     watchdog samples: 0
# ``plugin_smoke`` and ``libhostiokit.dylib`` are written at :57-61, long before
# that assertion, and they are the only two things ring 3 reads
# (gen-turnip-replay.py:23 skips the whole check when plugin_smoke exists).  So a
# non-zero exit from ring 2 after this point costs one assertion, not the chain.
#
# The trade is recorded rather than hidden: the log names the ring, the
# assertion, and exactly what is being given up.  A ring that fails BEFORE its
# outputs exist is still fatal -- that is what the second argument is for.
ring_best_effort() {
    local name="$1" artifact="$2"; shift 2
    local file="$LOGDIR/addon-$name.log"
    local started ended status=0
    started="$(date +%s)"
    log "=== ring: $name (best effort -- its outputs, not its verdict, are what the next ring needs) ==="
    log "+ $*"
    "$@" >"$file" 2>&1 || status=$?
    ended="$(date +%s)"
    if [[ "$status" -eq 0 ]]; then
        log "PASS $name in $((ended - started))s (its log: $file)"
        ring_record "$name" pass "$((ended - started))" "$artifact" ""
        return 0
    fi
    if [[ ! -f "$artifact" ]]; then
        log "FAIL $name after $((ended - started))s (exit $status) and $artifact was not produced; its log: $file"
        tail -25 "$file" | tee -a "$LOG" >&2
        ring_record "$name" failed "$((ended - started))" "$artifact" "exit $status before producing it"
        ring_summary
        die "$name failed before producing $artifact"
    fi
    # DEGRADED, and it is recorded as its own status rather than folded into
    # pass.  The chain continues on $artifact -- that is what this function is
    # for -- but "an assertion was given up" has to stay visible to the step
    # that decides what to report, and the exit status cannot carry it.
    log "DEGRADED $name after $((ended - started))s (exit $status): it failed an assertion AFTER producing $artifact"
    log "DEGRADED the chain continues on $artifact; what is given up is that assertion and everything after it"
    log "DEGRADED the ring's own log is $file -- the failing assertion is in its tail"
    local detail
    detail="$(grep -E "upstream check failed|FAIL|SKIPPED" "$file" | tail -3 | tr '\n' ';' | cut -c1-160)"
    ring_record "$name" degraded "$((ended - started))" "$artifact" "$detail"
    return 0
}

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
        ring_record "$name" pass "$((ended - started))" "" ""
        return 0
    fi
    log "FAIL $name after $((ended - started))s (exit $status); its log: $file"
    tail -25 "$file" | tee -a "$LOG" >&2
    ring_record "$name" failed "$((ended - started))" "" "exit $status"
    ring_summary
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

#: The bridge script, which is where the cargo toolchain pin is read from rather
#: than spelled a second time here.  DEFINED BEFORE ITS FIRST USE, and that
#: ordering is load-bearing: MEASURED on run 40928765061, the cargo check below
#: read $BRIDGE thirty-eight lines above this definition, so under `set -u` the
#: preflight died on an unbound variable the instant it reached it -- right after
#: the Vulkan-header install, which is where the log simply stops.  A variable
#: used before it is assigned is not a lint nit; it is a preflight that dies
#: halfway through provisioning and leaves a log that ends mid-sentence.
BRIDGE="$UP/scripts/build-native-metal-bridge.py"
[[ -f "$BRIDGE" ]] || die "no $BRIDGE; the driver checkout is where this stage starts"

# The Rust toolchain pin.  READ FROM THE BRIDGE SCRIPT rather than spelled here:
# build-native-metal-bridge.py:82 invokes `cargo +1.98.0`, and a second copy of
# that pin in this script is a second place for it to drift.  MEASURED on run
# 40921780876 why the reading matters: this check queried the DEFAULT toolchain's
# installed targets while the bridge builds with the pinned one, so it logged
# "aarch64-apple-ios is not in `rustup target list --installed`" on a runner where
# bootstrap-metal2vulkan.sh had just installed it into the pinned toolchain -- its
# own log says `rustup targets: aarch64-apple-darwin aarch64-apple-ios`.  A
# negative answer from an instrument pointed at the wrong subject is worse than no
# answer, because it is believed.
CARGO_TOOLCHAIN="$("$PY" - "$BRIDGE" <<'PY' | tr -d '\r'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
found = re.search(r"'cargo', '\+([0-9][0-9.]*)'", text)
if not found:
    raise SystemExit('build-native-metal-bridge.py no longer spells a cargo +<version> toolchain pin')
print(found.group(1))
PY
)" || die "cannot read the cargo toolchain pin from $BRIDGE"
[[ -n "$CARGO_TOOLCHAIN" ]] || die "the cargo toolchain pin read from $BRIDGE is empty"

# The same invocation the bridge uses, so a missing toolchain is answered by
# cargo's own message rather than by anything this script could invent.
cargo "+$CARGO_TOOLCHAIN" --version >>"$LOG" 2>&1 \
    || die "cargo +$CARGO_TOOLCHAIN does not answer; rustup install the pinned toolchain ($BRIDGE)"
log "cargo $(cargo "+$CARGO_TOOLCHAIN" --version 2>&1 | head -1)"

# And the target, from the SAME toolchain.  `rustup target list --installed`
# without a `+toolchain` answers for rustup's default, which on this runner is
# not the pinned one -- that is exactly the mismatch measured above.
if rustup "+$CARGO_TOOLCHAIN" target list --installed 2>/dev/null | grep -qx 'aarch64-apple-ios'; then
    log "cargo target aarch64-apple-ios is installed for toolchain $CARGO_TOOLCHAIN"
else
    log "note: aarch64-apple-ios is NOT installed for toolchain $CARGO_TOOLCHAIN; ring 4 will say so if it needs it"
fi

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
# Ring 2 compiles the real driver sources against the host simulation's stub IOKit
# and links them into a dylib.  In the pinned tree that no longer works, and every
# gap is in the SIMULATION rather than in the driver -- MEASURED across runs
# 40932083150 (two compile errors: `unknown type name 'IODirection'` and
# `use of undeclared identifier 'kIOMemoryPersistent'`) and 40939171888 (a link
# error: `Undefined symbols for architecture arm64: "_proc_selfpid"`).  All of it
# lives in files the tablet never sees; the shim's own header says "Never built
# for the tablet".
#
# They are applied by scripts/macos/apply-host-sim-divergences.py rather than
# edited into the checkout: the workflow's acquisition step resets vendor/UniXNU
# to its pin on every run, so an in-tree edit would vanish and the next run would
# fail identically with nothing in this repository saying why.  Each divergence
# names the anchor it extends and refuses when that anchor is not present exactly
# once -- an edit applied to a file it has never seen is how a two-declaration
# change becomes a corrupted header.  The tool's own tests pin both directions.
log "applying the recorded host-simulation divergences"
if ! "$PY" "$REPO/scripts/macos/apply-host-sim-divergences.py" "$UP" >>"$LOG" 2>&1; then
    tail -20 "$LOG" | tee -a "$LOG" >&2
    die "the host-simulation divergences could not be applied; see $LOG"
fi
grep -E "^(APPLIED|PRESENT|REFUSED|MISSING)" "$LOG" | tail -8 | tee -a "$LOG" >&2

# Ring 2's LAST check links the HOST copy of the metal bridge:
# check-priming-cache-host.py:41 reads build/native-metal-bridge/host/
# libnative_metal_bridge.a and :46 compiles priming_cache_host.cpp against it.
# Nothing in this chain built that -- ring 4 builds the iOS one -- so MEASURED on
# run 40943937160 ring 2 got as far as five of the eight deferred-BO modes and
# then died on exactly that compile.  A real Vulkan device had been created and a
# real submission retired by then, so this is the last thing between here and a
# complete ring 2, and it is a gap in THIS script rather than in the pinned tree.
#
# The host target needs nothing ring 4 does not: the bootstrap already assembled
# the two AIR fixtures (it verifies the patch state with --manifest-only, which
# stops just before cargo) and `aarch64-apple-darwin` is in the pinned
# toolchain's installed targets.
ring metal-bridge-host "$PY" "$BRIDGE" --target host

ABSENT_QUARTZCORE="$UP/scripts/check-quartzcore-turnip-cache-host.py"
# Best effort, for the reason ring_best_effort's own comment gives: on run
# 40948699070 this ring passed every one of the eight deferred-BO modes and the
# whole priming cache, and then failed its final watchdog assertion -- long after
# writing the two artifacts ring 3 reads.
ring_best_effort turnip-host-check "$UP/build/turnip-darwin-host/plugin_smoke" \
    "$PY" "$REPO/scripts/macos/run-upstream-check.py" \
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

# The summary, written whether or not anything degraded, so its absence is
# itself a signal rather than a silent "nothing to report".
ring_summary

# A degraded ring does not change the exit status -- the artifact exists and the
# chain must continue on it -- but it is stated once more here, at the end, in
# the same words the summary uses.  A reader who got here from a green workflow
# run should not have to open a JSON file to learn an assertion was given up.
if grep -q '"degraded": \[[^]]' "$SUMMARY" 2>/dev/null; then
    log "DEGRADED SUMMARY: this chain produced $OUTPUT with at least one ring degraded;"
    log "DEGRADED SUMMARY: the rings named in $SUMMARY's \"degraded\" array gave up an assertion each;"
    log "DEGRADED SUMMARY: the artifact is usable and the assertion is not -- do not read this as a clean build."
fi
