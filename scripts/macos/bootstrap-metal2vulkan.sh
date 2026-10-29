#!/usr/bin/env bash
# Bootstrap the metal2vulkan translator source, and the toolchain that compiles
# it, for the Adreno Metal plugin build.
#
# The chain this feeds
# -------------------
# scripts/macos/build-gpu-addon.sh runs seven rings after the two bootstraps,
# and ring 4 is build-native-metal-bridge.py -- the pinned metal2vulkan runtime
# behind a C ABI, as a static library.  That script reads four things nothing
# in this repository produces, and it refuses rather than guessing at any of
# them:
#
#   vendor/UniXNU/.cache/gpu-reference/metal2vulkan
#       a checkout whose `git rev-parse HEAD` equals
#       scripts/data/gpu-metal-vulkan-inputs.json's translator.commit, and
#       whose `git diff HEAD --` is the pinned revision plus the seven
#       recorded patches
#   llvm-as   LLVM 21 or newer, to assemble the runtime's two AIR fixtures
#   cargo     proxying the +1.98.0 toolchain, with the aarch64-apple-ios
#             target's std, because the bridge cross-builds for iOS
#   (nothing else -- the manifest is generated, not committed)
#
# The same source directory has four readers, which is why it is placed by a
# bootstrap rather than fetched by whoever gets there first:
# build-native-metal-bridge.py:17, check-metal-vulkan-probe.py:23,
# probe-metal-vulkan-host.py:101 and check-priming-cache-host.py:167 all
# resolve `.cache/gpu-reference/metal2vulkan` from
# `Path(__file__).resolve().parents[1]`.  A producer that put it anywhere else
# would satisfy itself and starve the four builds it feeds.  It also keeps
# every byte this script downloads inside vendor/, which .gitignore:39 already
# excludes -- a cache under the repository root is stageable by a bare
# `git add -A`.
#
# Deliberately NOT under `set -e`, for the reason bootstrap-mesa-turnip.sh
# gives: a bootstrap that fails must say WHICH of the four it was, and `set -e`
# would abort on the first one.  `set -u` IS load-bearing -- an unset variable
# is a typo, not an empty string -- and `set` is alone on its line, which is
# what test_set_is_alone_on_its_line enforces.  `set -o pipefail` too: a
# failing python piped through `tr` must not read as a passing one.

set -uo pipefail

REPO="${1:?usage: bootstrap-metal2vulkan.sh REPO_ROOT}"

#: The driver checkout.  Its own scripts read the pin and the caches relative
#: to themselves, so this is where the whole stage lives.  RESOLVED TO AN
#: ABSOLUTE PATH, and that is load-bearing for the same reason
#: bootstrap-mesa-turnip.sh:39-46 gives: `git -C "$SOURCE" apply
#: "$PATCHES_ROOT/$patch"` resolves the patch path against $SOURCE, not against
#: this script's working directory, so a relative $UP makes every patch
#: unfindable.  Every other path below is derived from this one.
UP="$(cd "$REPO" 2>/dev/null && pwd)/vendor/UniXNU"
[[ -d "$UP/.git" ]] || { echo "no $REPO/vendor/UniXNU; the driver checkout is where this stage starts" >&2; exit 1; }

# Resolved rather than assumed, for the reason bootstrap-mesa-turnip.sh:50-56
# gives: the interpreter is not called python3 on every host that runs this
# script.  A bootstrap that dies on `python3: command not found` names the
# wrong cause.
if command -v python3 >/dev/null 2>&1; then PY='python3'
elif command -v python  >/dev/null 2>&1; then PY='python'
else PY=''; fi
[[ -n "$PY" ]] || { echo "no python interpreter on PATH" >&2; exit 1; }

LOCK="$REPO/config/sources.lock.json"
[[ -f "$LOCK" ]] || { echo "no $LOCK; the driver pin is where this stage starts" >&2; exit 1; }

PIN="$UP/scripts/data/gpu-metal-vulkan-inputs.json"
[[ -f "$PIN" ]] || { echo "no $PIN; the translator pin is where this stage starts" >&2; exit 1; }

#: The build script whose PATCHES list and patch directory this bootstrap
#: reads, rather than restating either of them.  A second list is a second
#: place that knows the order, and the order is what
#: build-native-metal-bridge.py:46-49 verifies against.
BRIDGE="$UP/scripts/build-native-metal-bridge.py"
[[ -f "$BRIDGE" ]] || { echo "no $BRIDGE; the Metal bridge build is what this stage feeds" >&2; exit 1; }

SOURCE="$UP/.cache/gpu-reference/metal2vulkan"

#: The log, resolved to an absolute path, for the reason
#: bootstrap-mesa-turnip.sh:67-75 records at length: the workflow spells it
#: relatively ("build-output/bootstrap-metal2vulkan.log"), and a relative path
#: breaks the moment this script changes directory -- MEASURED on run
#: 40816437974, where the failure's own output went to a file the cleanup step
#: then deleted, so the uploaded evidence stopped one line before the reason.
LOG="${BOOTSTRAP_LOG:-$UP/.cache/bootstrap-metal2vulkan.log}"
case "$LOG" in
    /*) ;;
    *) LOG="$PWD/$LOG" ;;
esac

mkdir -p "$UP/.cache/gpu-reference" "$(dirname "$LOG")"
: > "$LOG" || { echo "cannot write the log $LOG" >&2; exit 1; }

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }
# A refusal carries the log's own tail, so the reader does not have to go and
# find the file to learn what happened.
die() { log "FAIL $*"; log "---- last 12 lines of $LOG ----"; tail -12 "$LOG" | tee -a "$LOG" >&2; exit 1; }

#: One string out of the pin, or an empty line when the key is absent.  Empty
#: rather than a traceback, because the two halves of this pin differ: `mesa`
#: carries a `mirror` and `translator` does not, so asking for a key that is
#: not there has to be a question, not an exception.  The caller decides which
#: keys are required, which is what keeps a missing pin a refusal rather than
#: a silent empty string.
#:
#: Every value read out of python is piped through `tr -d` with a CR class, and
#: that is load-bearing rather than tidiness.  Python translates a newline to
#: os.linesep when stdout is a pipe, so on Windows the value arrives as text
#: followed by CR LF; command substitution strips the newline and keeps the
#: carriage return.  The result is a value that looks clean in a log line and
#: compares equal to itself, and is fatal the moment it is used as a path.
#: That is how bootstrap-mesa-turnip.sh's first real run died, and the two
#: checks that compare one python value against another would both have PASSED
#: with the CR present, because both sides carry it -- a guard that cannot fail
#: is worse than no guard.
read_pin() {
    "$PY" - "$PIN" "$1" "$2" <<'PY' | tr -d '\r'
import json, sys
section = json.loads(open(sys.argv[1], encoding="utf-8").read()).get(sys.argv[2])
value = section.get(sys.argv[3]) if isinstance(section, dict) else None
print(value if isinstance(value, str) else "")
PY
}

# The driver checkout must be the one the lock pins, and this is checked BEFORE
# the pin inside it is trusted.  The translator pin is a file in that checkout,
# so a stale checkout is a stale pin -- and a stale pin is indistinguishable
# from a correct one until the commit it names moves, which is exactly the
# shape this repository has been bitten by before (implementation-plan.md fact
# 122, and again as fact 135: a stale copy that is byte-identical to the
# correct one for as long as the input does not change).  The rationale is
# bootstrap-mesa-turnip.sh:112-127 in full; it is restated here rather than
# extracted into the library because it is about THIS repository's lock, not
# about how a pinned upstream is checked out.
driver_head="$(git -C "$UP" rev-parse HEAD 2>/dev/null)" || driver_head=""
driver_pin="$("$PY" - "$LOCK" <<'PY' | tr -d '\r'
import json, sys
print(json.loads(open(sys.argv[1], encoding="utf-8").read())["unixnu"]["commit"])
PY
)" || die "cannot read unixnu.commit from $LOCK"
[[ "$driver_head" == "$driver_pin" ]] \
    || die "the driver checkout is at ${driver_head:0:12}, the lock pins ${driver_pin:0:12}; a translator pin read from a stale checkout is not this build's pin"

M2V_COMMIT="$(read_pin translator commit)"
[[ -n "$M2V_COMMIT" ]] || die "cannot read translator.commit from $PIN"
# The mirror, when the pin names one, and the repository otherwise.  The
# translator section names only `repository` today, but the shape is the one
# the Mesa pin uses and a pin that grows a mirror later should not need this
# script edited to prefer it.
M2V_MIRROR="$(read_pin translator mirror)"
[[ -n "$M2V_MIRROR" ]] || M2V_MIRROR="$(read_pin translator repository)"
[[ -n "$M2V_MIRROR" ]] || die "$PIN names no translator.mirror and no translator.repository"

log "driver ${driver_head:0:12} (the lock's pin)"
log "translator commit $M2V_COMMIT"
log "translator remote $M2V_MIRROR"

#: The four digests the pin carries, read once into "<name> <sha256>" lines.
#: Sorted by name so the log is a function of the pin rather than of the JSON
#: object's insertion order.
FILE_HASHES="$("$PY" - "$PIN" <<'PY' | tr -d '\r'
import json, sys
for name, digest in sorted(json.loads(open(sys.argv[1], encoding="utf-8").read())["translator"]["files"].items()):
    print(name, digest)
PY
)" || die "cannot read the translator's file digests from $PIN"
[[ -n "$FILE_HASHES" ]] || die "$PIN records no translator.files digests to verify against"

#: The digest tool, resolved HERE rather than inside a function called from a
#: command substitution: `die` in a subshell exits the subshell, so a missing
#: tool would surface as an empty digest and a message about the wrong thing.
if command -v shasum >/dev/null 2>&1; then SUM='shasum -a 256'
elif command -v sha256sum >/dev/null 2>&1; then SUM='sha256sum'
else die "no sha256 tool on PATH (shasum or sha256sum)"; fi

#: Verify the files the pin carries digests for, against the checkout at the
#: pinned commit.  Run BEFORE the patches are applied, which is the only order
#: that makes the claim precise: these four are the pinned inputs, and the
#: patch set is a separate claim about everything else.  None of the seven
#: recorded patches touches any of these four paths (their files are under
#: crates/metal2vulkan-air/src/canon/ and
#: crates/metal2vulkan{,-runtime}/src/), so the same digests hold either side
#: -- and checking before is still the order that names a wrong checkout as a
#: wrong checkout rather than as a patch failure three lines later.
#:
#: The digest of what is actually on disk, not of what the pin claims.  A
#: commit id says which change was fetched; these say what the source is.
verify_pinned_files() {
    local name expected got
    while IFS=' ' read -r name expected; do
        [[ -n "$name" ]] || continue
        if [[ ! -f "$SOURCE/$name" ]]; then
            log "FAIL $SOURCE/$name is missing from the pinned checkout"
            return 1
        fi
        got="$($SUM "$SOURCE/$name" | cut -d' ' -f1)"
        if [[ "$got" != "$expected" ]]; then
            log "FAIL $name is $got, the pin says $expected"
            return 1
        fi
        log "pinned file $name $got"
    done <<FILES
$FILE_HASHES
FILES
}

# --- 1. the translator source ---------------------------------------------
# The checkout, the commit proof, the file digests and the patch set are
# scripts/macos/lib/pinned-source.sh, shared with bootstrap-mesa-turnip.sh.
# Same four steps, different upstream; see that file for what each guard is
# and the measurement behind it.
#
# The library is resolved from THIS file's own location, not from $REPO or
# $PWD: the workflow invokes this as `bash
# scripts/macos/bootstrap-metal2vulkan.sh .` from the repository root, and a
# relative path would break the moment either of those changed.
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/pinned-source.sh"
[[ -f "$LIB" ]] || { echo "no $LIB; the shared pinned-source library is missing" >&2; exit 1; }
. "$LIB"

# No tree pin: gpu-metal-vulkan-inputs.json's translator section records
# `commit` and `files`, and no `tree`.  The four file digests are this pin's
# answer to the same question -- a commit id can be re-published with different
# content, and the digests are what notice.  An empty tree argument SKIPS the
# comparison rather than comparing against "".
pinned_source_checkout "$SOURCE" "$M2V_MIRROR" "$M2V_COMMIT" "" verify_pinned_files "metal2vulkan source"

# --- 2. the recorded patches ----------------------------------------------
# The list and its directory are READ from build-native-metal-bridge.py:25-29
# rather than restated here.  That assignment is the order the build applies
# and verifies against, so a second list in this script is a second place that
# knows it -- the divergence class this repository keeps hitting.  The names
# and the directory are recovered from the assignment's own AST, so a
# reordering upstream changes what this script applies without anyone editing
# this file; if the assignment changes shape, this returns nothing and the
# caller refuses rather than guessing.
bridge_patches() {
    "$PY" - "$BRIDGE" <<'PY' | tr -d '\r'
import ast, sys
tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
for node in ast.walk(tree):
    if not isinstance(node, ast.Assign):
        continue
    if not any(isinstance(target, ast.Name) and target.id == "PATCHES" for target in node.targets):
        continue
    value = node.value
    if not isinstance(value, ast.ListComp) or not isinstance(value.generators[0].iter, ast.List):
        continue
    # `ROOT / 'drivers/NativeMetal/bridge/patches' / name`: walk the BinOp
    # chain and keep every literal segment, outermost last.
    folder = ""
    inner = value.elt
    while isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div):
        if isinstance(inner.right, ast.Constant) and isinstance(inner.right.value, str):
            folder = inner.right.value if not folder else inner.right.value + "/" + folder
        inner = inner.left
    names = [elt.value for elt in value.generators[0].iter.elts
             if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
    print(folder)
    for name in names:
        print(name)
    break
PY
}

# Two lines of shape: the directory on the first, then one name per line.
# Split with the shell's own prefix/suffix removal rather than a second read.
PATCH_INFO="$(bridge_patches)" || die "cannot read the patch list from $BRIDGE"
case "$PATCH_INFO" in
    *$'\n'*) ;;
    *) die "$BRIDGE names no PATCHES list to apply" ;;
esac
PATCHES_ROOT_REL="${PATCH_INFO%%$'\n'*}"
PATCH_LIST="${PATCH_INFO#*$'\n'}"
[[ -n "$PATCHES_ROOT_REL" ]] || die "cannot read the patch directory from $BRIDGE"
[[ -n "$PATCH_LIST" ]] || die "cannot read the patch names from $BRIDGE"
PATCHES_ROOT="$UP/$PATCHES_ROOT_REL"
[[ -d "$PATCHES_ROOT" ]] || die "$PATCHES_ROOT is not in the driver checkout"
log "patch directory $PATCHES_ROOT_REL, read from $BRIDGE"

# From a clean tree, every time, and no per-patch idempotence -- see
# pinned_source_apply_patches for why `apply --reverse --check` is unsound when
# several patches touch one file.
pinned_source_apply_patches "$SOURCE" "$PATCHES_ROOT" "$PATCH_LIST" "metal2vulkan" "$M2V_COMMIT"

# --- 3. the toolchain ------------------------------------------------------
# build-native-metal-bridge.py:54-56 probes /opt/homebrew/opt/llvm/bin/llvm-as
# and then PATH, and dies with "llvm-as (LLVM 21 or newer) is required to
# assemble the fixture" when neither answers.  A GitHub macOS runner ships
# Xcode's clang and no Homebrew LLVM; the workflow's own "Install LLVM" step
# puts the keg on PATH for lld-link, so on a normal run the probe already
# answers -- this is for the run where it does not.
LLVM_AS=''
for candidate in /opt/homebrew/opt/llvm/bin/llvm-as /usr/local/opt/llvm/bin/llvm-as; do
    if [[ -x "$candidate" ]]; then LLVM_AS="$candidate"; break; fi
done
if [[ -z "$LLVM_AS" ]]; then
    found="$(command -v llvm-as 2>/dev/null || true)"
    if [[ -n "$found" ]]; then LLVM_AS="$found"; fi
fi
if [[ -z "$LLVM_AS" ]]; then
    log "installing llvm (llvm-as)"
    brew install llvm 2>>"$LOG" || die "cannot install llvm"
    for candidate in /opt/homebrew/opt/llvm/bin/llvm-as /usr/local/opt/llvm/bin/llvm-as; do
        if [[ -x "$candidate" ]]; then LLVM_AS="$candidate"; break; fi
    done
fi
[[ -n "$LLVM_AS" ]] || die "no llvm-as on PATH and no llvm keg on disk after brew install llvm"
# The version is checked rather than assumed, and the threshold is not
# decorative: the two fixtures are assembled to AIR and embedded into the
# library, so an assembler older than the one the pinned runtime was recorded
# against produces bitcode the loader may not accept -- a failure that surfaces
# as a shader-cache miss rather than as an assembler error.
LLVM_VERSION="$("$LLVM_AS" --version 2>&1 | head -1 | grep -o '[0-9][0-9.]*' | head -1)"
LLVM_MAJOR="${LLVM_VERSION%%.*}"
[[ "$LLVM_MAJOR" =~ ^[0-9]+$ ]] || die "cannot read llvm-as's version from $LLVM_AS"
[[ "$LLVM_MAJOR" -ge 21 ]] || die "llvm-as is $LLVM_VERSION; build-native-metal-bridge.py:56 requires LLVM 21 or newer"
log "llvm-as $LLVM_VERSION ($LLVM_AS)"

# build-native-metal-bridge.py:82 spells the Rust pin as `cargo +1.98.0`, so
# the check is the same invocation: if the toolchain is not installed, cargo
# answers with its own message about the toolchain, which is more use than
# anything this script could invent.  The `+toolchain` spelling is also why
# this is rustup and not `brew install rust` -- brew's rust formula ships cargo
# but not rustup, so it cannot proxy a named toolchain at all, and
# docs/implementation-plan.md fact 127 records the Rust toolchain as entirely
# absent from the runner.
CARGO_TOOLCHAIN='1.98.0'
if ! command -v cargo >/dev/null 2>&1 || ! cargo "+$CARGO_TOOLCHAIN" --version >/dev/null 2>&1; then
    if command -v rustup >/dev/null 2>&1; then
        log "installing the $CARGO_TOOLCHAIN rust toolchain"
        rustup toolchain install "$CARGO_TOOLCHAIN" 2>>"$LOG" \
            || die "cannot install the $CARGO_TOOLCHAIN rust toolchain"
    else
        log "installing rustup"
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o "$UP/.cache/rustup-init.sh" 2>>"$LOG" \
            || die "cannot download rustup-init"
        sh "$UP/.cache/rustup-init.sh" -y --default-toolchain stable --profile minimal 2>>"$LOG" \
            || die "cannot install rustup"
        rm -f "$UP/.cache/rustup-init.sh"
        # rustup-init puts cargo in ~/.cargo/bin and, on a host that never had
        # rust, that directory is not on PATH.  Prepended here rather than
        # sourced from ~/.cargo/env so this shell's PATH is the only thing that
        # changes.
        if [[ -d "$HOME/.cargo/bin" ]]; then PATH="$HOME/.cargo/bin:$PATH"; fi
    fi
fi
command -v cargo >/dev/null 2>&1 || die "cargo is absent after the rustup install"
cargo "+$CARGO_TOOLCHAIN" --version >/dev/null 2>&1 \
    || die "cargo +$CARGO_TOOLCHAIN does not answer; build-native-metal-bridge.py:82 needs it"
log "cargo $(cargo "+$CARGO_TOOLCHAIN" --version)"
# The cross-compile target's std, for the reason fact 127 names: `--target
# aarch64-apple-ios` needs it, and a runner with the toolchain but not the
# target fails inside cargo after the bridge's dependencies have already been
# fetched and compiled -- forty minutes in, with a message about a missing
# std rather than about the missing target.
if command -v rustup >/dev/null 2>&1; then
    rustup +"$CARGO_TOOLCHAIN" target add aarch64-apple-ios 2>>"$LOG" \
        || die "cannot add aarch64-apple-ios to the $CARGO_TOOLCHAIN toolchain"
    log "rustup targets: $(rustup +"$CARGO_TOOLCHAIN" target list --installed | tr '\n' ' ')"
else
    log "note: rustup is not on PATH, so the aarch64-apple-ios target could not be added here"
fi

# --- 4. the driver's own verifier ------------------------------------------
# Verified with the driver's own verifier rather than with a second
# implementation of it -- the discipline bootstrap-mesa-turnip.sh:239-264 uses
# for Mesa's patch state.  build-native-metal-bridge.py:30-51 is the check the
# build runs: it compares `git diff HEAD --` against the concatenation of its
# PATCHES in that file's order, and refuses when the tree is not one of the
# states that concatenation can produce.  So a bootstrap that applied a subset,
# a different order, or one patch too many is caught HERE, before the Rust
# toolchain spends forty minutes discovering it.
#
# --manifest-only is the same run with the cargo half skipped: it still
# assembles the two AIR fixtures with llvm-as and still writes the Cargo.toml
# a later `cargo fetch` needs.  That is why the toolchain is installed above
# rather than below.
#
# The git environment is isolated (GIT_CONFIG_NOSYSTEM and a HOME with no
# .gitconfig) for the reason bootstrap-mesa-turnip.sh:239-251 gives: the script
# runs `git apply` itself, so on a host whose global core.autocrlf is true the
# verdict would be a property of the machine rather than of the tree.  The
# directory is shared with the Mesa bootstrap's verifier, and neither writes to
# it -- it exists only to hide the host's gitconfig.
NEUTRAL_HOME="$UP/.cache/git-neutral-home"
mkdir -p "$NEUTRAL_HOME"
GIT_CONFIG_NOSYSTEM=1 HOME="$NEUTRAL_HOME" "$PY" "$BRIDGE" --manifest-only 2>>"$LOG" \
    || die "the translator source is not the pinned revision plus its recorded patches; see $LOG"
log "translator patch state verified by build-native-metal-bridge.py itself"

log "PASS the translator is at $M2V_COMMIT, its recorded patches are applied and verified, and the Metal bridge's toolchain answers"
exit 0
