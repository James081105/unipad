#!/usr/bin/env bash
# Bootstrap the Mesa source and the tools build-turnip-darwin.py needs.
#
# build-turnip-darwin.py reads four things that nothing in this repository
# produces, and it refuses rather than guessing at any of them:
#
#   vendor/UniXNU/.cache/mesa-work     a Mesa checkout whose HEAD equals
#                                      scripts/data/adreno-compiler-inputs.json's
#                                      mesa.commit
#   vendor/UniXNU/.cache/mesa-tools    a bison new enough for Mesa's parser
#                                      (macOS ships 2.3; the pin is 3.8.2, with
#                                      a sha256 to verify against)
#   vendor/UniXNU/.cache/mesa-venv     meson, ninja and Mesa's python build deps
#   glslangValidator                   on PATH, for the Vulkan runtime build
#
# All four are absent from a fresh macos-latest runner, so without this stage
# the whole GPU-addon chain dies on its first line with a message about a
# missing directory rather than about the missing stage.
#
# Every path here is under vendor/UniXNU, and that is not a stylistic choice:
# build-turnip-darwin.py:17 is `ROOT = Path(__file__).resolve().parents[1]`, so
# it reads its pin from vendor/UniXNU/scripts/data/ and its caches from
# vendor/UniXNU/.cache/.  A bootstrap that put them anywhere else would satisfy
# itself and starve the build it feeds.  It also keeps every byte this script
# downloads inside vendor/, which .gitignore:39 already excludes -- a cache
# under the repository root is stageable by a bare `git add -A`, and that is
# how a 500 MB Mesa clone ends up in a commit.
#
# Deliberately NOT under `set -e`: a bootstrap that fails must say which of the
# four it was, and `set -e` would abort on the first one.  Every command's
# status is checked and named instead.  `set -u` IS load-bearing -- an unset
# variable is a typo, not an empty string -- and `set` is alone on its line,
# which is what test_set_is_alone_on_its_line enforces.

set -uo pipefail

REPO="${1:?usage: bootstrap-mesa-turnip.sh REPO_ROOT}"

#: The driver checkout.  Its own scripts read the pin and the caches relative
#: to themselves, so this is where the whole stage lives.  RESOLVED TO AN
#: ABSOLUTE PATH, and that is load-bearing: `git -C "$SOURCE" apply "$UP/$patch"`
#: resolves the patch path against $SOURCE, not against this script's working
#: directory, so a relative $UP makes every patch unfindable -- measured, as
#: "can't open patch './vendor/UniXNU/patches/mesa/...'".  Every other path
#: below is derived from this one, so making it absolute makes them all
#: independent of where the script was invoked from.
UP="$(cd "$REPO" 2>/dev/null && pwd)/vendor/UniXNU"
[[ -d "$UP/.git" ]] || { echo "no $REPO/vendor/UniXNU; the driver checkout is where this stage starts" >&2; exit 1; }

# Resolved rather than assumed, for the reason build-macos.sh:194 gives: "the
# interpreter is not called python3 on every host that runs this script".  A
# bootstrap that dies on `python3: command not found` names the wrong cause.
if command -v python3 >/dev/null 2>&1; then PY='python3'
elif command -v python  >/dev/null 2>&1; then PY='python'
else PY=''; fi
[[ -n "$PY" ]] || { echo "no python interpreter on PATH" >&2; exit 1; }

LOCK="$REPO/config/sources.lock.json"
[[ -f "$LOCK" ]] || { echo "no $LOCK; the driver pin is where this stage starts" >&2; exit 1; }

PIN="$UP/scripts/data/adreno-compiler-inputs.json"
[[ -f "$PIN" ]] || { echo "no $PIN; the Mesa pin is where this stage starts" >&2; exit 1; }

SOURCE="$UP/.cache/mesa-work"
TOOLS="$UP/.cache/mesa-tools"
VENV="$UP/.cache/mesa-venv"
#: The log, resolved to an absolute path.  The workflow spells it relatively
#: ("build-output/bootstrap-mesa.log"), and a relative path breaks the moment
#: this script changes directory -- which it does, in the bison build's
#: subshell.  MEASURED on run 40816437974: the bison build failed, and the
#: failure's own output had gone to <build>/build-output/bootstrap-mesa.log,
#: a file the cleanup step then deleted, so the uploaded log ended at
#: "downloading bison 3.8.2 / FAIL cannot build bison 3.8.2" with nothing in
#: between.  A stage whose failure reason is deleted by its own cleanup is a
#: stage that cannot be diagnosed from its evidence.
LOG="${BOOTSTRAP_LOG:-$UP/.cache/bootstrap-mesa.log}"
case "$LOG" in
    /*) ;;
    *) LOG="$PWD/$LOG" ;;
esac

mkdir -p "$UP/.cache" "$(dirname "$LOG")"
: > "$LOG" || { echo "cannot write the log $LOG" >&2; exit 1; }

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }
# A refusal carries the log's own tail, so the reader does not have to go and
# find the file to learn what happened.  Twelve lines is what the driver's own
# build failure messages quote, and it is enough to name a configure error.
die() { log "FAIL $*"; log "---- last 12 lines of $LOG ----"; tail -12 "$LOG" | tee -a "$LOG" >&2; exit 1; }

# Every value read out of python is piped through `tr -d` with a CR class, and
# that is load-bearing rather than tidiness.  Python translates a newline to
# os.linesep when stdout is a pipe, so on Windows the value arrives as text
# followed by CR LF; command substitution strips the newline and keeps the
# carriage return.  The result is a value that looks clean in a log line and
# compares equal to itself, and is fatal the moment it is used as a path.
#
# That is exactly how this script's first real run died: `[[ -f "$UP/$patch" ]]`
# refused a patch that was sitting right there, because the name carried a
# trailing CR.  Worse, the two checks that compare one python value against
# another -- the driver checkout against the lock, the Mesa tree against the
# pin -- would both have PASSED with the CR present, because both sides carry
# it.  A guard that cannot fail is worse than no guard, so the CR is stripped at
# the point of capture rather than at the point of use.
read_pin() {
    "$PY" - "$PIN" "$1" "$2" <<'PY' | tr -d '\r'
import json, sys
print(json.loads(open(sys.argv[1], encoding="utf-8").read())[sys.argv[2]][sys.argv[3]])
PY
}

# The driver checkout must be the one the lock pins, and this is checked BEFORE
# the pin inside it is trusted.  The Mesa pin is a file in that checkout, so a
# stale checkout is a stale pin -- and a stale pin is indistinguishable from a
# correct one until the commit it names moves, which is exactly the shape this
# repository has been bitten by before (implementation-plan.md fact 122: a stale
# copy that is byte-identical to the correct one for as long as the input does
# not change).  Reading the lock rather than hardcoding a sha is the same rule
# the workflow's acquisition step follows.
driver_head="$(git -C "$UP" rev-parse HEAD 2>/dev/null)" || driver_head=""
driver_pin="$("$PY" - "$LOCK" <<'PY' | tr -d '\r'
import json, sys
print(json.loads(open(sys.argv[1], encoding="utf-8").read())["unixnu"]["commit"])
PY
)" || die "cannot read unixnu.commit from $LOCK"
[[ "$driver_head" == "$driver_pin" ]] \
    || die "the driver checkout is at ${driver_head:0:12}, the lock pins ${driver_pin:0:12}; a Mesa pin read from a stale checkout is not this build's pin"

MESA_COMMIT="$(read_pin mesa commit)" || die "cannot read the Mesa commit from the pin"
MESA_MIRROR="$(read_pin mesa mirror)" || die "cannot read the Mesa mirror from the pin"
MESA_TREE="$(read_pin mesa tree)" || die "cannot read the Mesa tree from the pin"
BISON_VERSION="$(read_pin bison version)" || die "cannot read the bison version from the pin"
BISON_URL="$(read_pin bison url)" || die "cannot read the bison url from the pin"
BISON_SHA256="$(read_pin bison sha256)" || die "cannot read the bison sha256 from the pin"

log "driver ${driver_head:0:12} (the lock's pin)"
log "mesa commit $MESA_COMMIT"
log "mesa mirror $MESA_MIRROR"
log "bison $BISON_VERSION ($BISON_SHA256)"

# --- 1. the Mesa source ----------------------------------------------------
# The mirror is preferred over repository: gitlab.freedesktop.org rate-limits
# anonymous clones, and a runner that dies halfway through a 500 MB fetch has
# spent its minutes on nothing.  The mirror is Google's, which does not.
#
# The checkout, the commit proof, the tree proof and the patch set are
# scripts/macos/lib/pinned-source.sh, shared with bootstrap-metal2vulkan.sh.
# That is the same four steps applied to a different upstream, and every guard
# in them was paid for here first -- see the comments in that file for what
# each one is, and the ones below for why this caller is the shape it is.
#
# The library is resolved from THIS file's own location, not from $REPO or
# $PWD: the workflow invokes this as `bash
# scripts/macos/bootstrap-mesa-turnip.sh .` from the repository root, and a
# relative path would break the moment either of those changed.
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/pinned-source.sh"
[[ -f "$LIB" ]] || { echo "no $LIB; the shared pinned-source library is missing" >&2; exit 1; }
. "$LIB"

pinned_source_checkout "$SOURCE" "$MESA_MIRROR" "$MESA_COMMIT" "$MESA_TREE" "" "mesa source"

# --- 2. the recorded patches ----------------------------------------------
# build-turnip-darwin.py:35 calls validate_recorded_patches(), which does not
# apply anything: it reverse-applies every recorded patch and requires the
# result to equal HEAD.  So the source must arrive here with all nine already
# applied, and with nothing else changed -- an unrecorded edit is a hard exit.
# Applying them here, in the pin's order, is what makes that verifier pass.
#
# The list is read once into a variable and fed through a here-document, rather
# than through `done < <(... <<'PY')`: test_no_heredoc_inside_a_process_
# substitution records run 40386064322, which died on exactly that construct
# because macOS's bash 3.2 refuses to parse it and `bash -n` on a bash 5 host
# accepts it happily.
mesa_patches() {
    "$PY" - "$PIN" <<'PY' | tr -d '\r'
import json, sys
for name in json.loads(open(sys.argv[1], encoding="utf-8").read())["patches"]:
    print(name)
PY
}
PATCH_LIST="$(mesa_patches)" || die "cannot read the patch list from the pin"

pinned_source_apply_patches "$SOURCE" "$UP" "$PATCH_LIST" "mesa" "$MESA_COMMIT"

# Verified with the driver's own verifier rather than with a second
# implementation of it.  This is the check build-turnip-darwin.py will run, so
# passing it here means the build cannot be surprised by it three steps later.
#
# It runs with an isolated git environment (GIT_CONFIG_NOSYSTEM and a HOME with
# no .gitconfig), and that is measured rather than defensive.  The verifier
# builds a throwaway `git init` tree under .cache and reverse-applies the
# patches there, so it inherits whatever the HOST's git config says -- and on a
# Windows host whose global core.autocrlf is true, that tree comes back with
# CRLF and every patched file fails its byte comparison against HEAD, after the
# patches themselves applied cleanly.  Isolating the environment makes the
# verdict a property of the tree rather than of the machine, which is the only
# kind of verdict worth recording.
NEUTRAL_HOME="$TOOLS/../git-neutral-home"
mkdir -p "$NEUTRAL_HOME"
GIT_CONFIG_NOSYSTEM=1 HOME="$NEUTRAL_HOME" "$PY" - "$UP" "$SOURCE" "$PIN" <<'PY' 2>>"$LOG" \
    || die "the recorded Mesa patches do not verify; see $LOG"
import json, sys
from pathlib import Path
root, source, pin = (Path(a) for a in sys.argv[1:4])
sys.path.insert(0, str(root / "scripts"))
from mesa_patch_identity import validate_recorded_patches
validate_recorded_patches(root, source, json.loads(pin.read_text()))
print("the recorded patches reconstruct the pinned source")
PY
log "mesa patch state verified by the driver's own verifier"

# --- 3. bison --------------------------------------------------------------
# macOS ships bison 2.3, which cannot parse Mesa's grammar.  Built from source
# into .cache/mesa-tools because there is no bottle new enough, and verified
# against the pin's sha256 before it is used -- a truncated tarball is a build
# failure that looks like a Mesa bug, and a byte count alone would not catch a
# same-length corruption.
BISON_PREFIX="$TOOLS"
if [[ -x "$BISON_PREFIX/bin/bison" ]]; then
    have="$("$BISON_PREFIX/bin/bison" --version | head -1 | grep -o '[0-9.]*' | head -1)"
    if [[ "$have" == "$BISON_VERSION" ]]; then
        log "bison $have already built"
    else
        log "bison $have present but $BISON_VERSION pinned; rebuilding"
        rm -rf "$BISON_PREFIX"
    fi
fi
if [[ ! -x "$BISON_PREFIX/bin/bison" ]]; then
    # The extension comes from the URL rather than being assumed: the pin's
    # bison URL ends .tar.xz, and `tar -xzf` on an xz archive fails with a
    # message about the magic bytes rather than about the compression.
    ext="${BISON_URL##*.}"
    tarball="$UP/.cache/bison-$BISON_VERSION.$ext"
    log "downloading bison $BISON_VERSION"
    curl -sSL -o "$tarball" "$BISON_URL" 2>>"$LOG" || die "cannot download bison"
    # The digest of what was actually downloaded, not of what the URL claims.
    # Resolved in this shell rather than inside a function called from a command
    # substitution: `die` in a subshell exits the subshell, so a missing tool
    # would surface as an empty digest and a message about the wrong thing.
    if command -v shasum >/dev/null 2>&1; then SUM='shasum -a 256'
    elif command -v sha256sum >/dev/null 2>&1; then SUM='sha256sum'
    else die "no sha256 tool on PATH (shasum or sha256sum)"; fi
    got="$($SUM "$tarball" | cut -d' ' -f1)"
    [[ "$got" == "$BISON_SHA256" ]] \
        || die "bison tarball is $got, the pin says $BISON_SHA256"
    build="$UP/.cache/bison-build"
    rm -rf "$build"; mkdir -p "$build"
    # -x with no compression flag: both BSD tar (macOS) and GNU tar detect it,
    # so this does not need to know which one the runner has.
    tar -xf "$tarball" -C "$build" --strip-components=1 2>>"$LOG" \
        || die "cannot unpack bison"
    (
        cd "$build" || exit 1
        ./configure --prefix="$BISON_PREFIX" --quiet 2>>"$LOG" || exit 1
        make -j"$(sysctl -n hw.ncpu)" --quiet 2>>"$LOG" || exit 1
        make install --quiet 2>>"$LOG" || exit 1
    ) || die "cannot build bison $BISON_VERSION"
    rm -rf "$build" "$tarball"
    log "bison $("$BISON_PREFIX/bin/bison" --version | head -1) installed"
fi

# --- 4. the python build environment ---------------------------------------
# meson and ninja are the two Mesa's build cannot do without; the rest are Mesa's
# own declared requirements.
#
# MEASURED on run 40921780876, which is the first run that ever got this far: the
# venv carried meson and ninja and nothing else, so ring 1 died seven seconds in
# with
#     vendor/UniXNU/.cache/mesa-work/meson.build:1141:2: ERROR: Problem
#     encountered: One of Python (3.x) packaging or distutils module is required.
# The comment this step used to carry promised that the dependency list came from
# Mesa's own build file and would therefore need no updating.  It did not: the code
# installed meson and ninja and nothing else, so the promise was an intention.  A
# comment describing a guard that does not exist is worse than no comment, because
# it stops the next reader from looking -- which is exactly how this survived a
# bootstrap that had already been verified on a runner.
#
# So the requirement is now met rather than described: the three modules Mesa's
# meson.build probes for are installed and then IMPORTED to prove it.  The module
# names and the meson.build lines that require them are in the table below, so a
# reader can check the claim against the source in one jump.  If Mesa adds a
# fourth, its configure fails and the failure names the module -- which is the
# same outcome as today, one ring later.
MESA_PYTHON_MODULES='mako:mako packaging:packaging yaml:PyYAML'
if [[ ! -x "$VENV/bin/meson" ]]; then
    log "creating the build virtualenv"
    "$PY" -m venv "$VENV" 2>>"$LOG" || die "cannot create $VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip 2>>"$LOG" \
        || die "cannot upgrade pip in the build virtualenv"
    "$VENV/bin/pip" install --quiet meson ninja 2>>"$LOG" \
        || die "cannot install meson and ninja"
fi
"$VENV/bin/meson" --version >/dev/null 2>&1 || die "meson is not runnable"
log "meson $("$VENV/bin/meson" --version)"

# Installed unconditionally rather than inside the venv-creation branch: a venv
# left by an earlier revision of this script has meson and ninja and is missing
# these, which is exactly the state run 40921780876 found.
for pair in $MESA_PYTHON_MODULES; do
    module="${pair%%:*}"
    package="${pair#*:}"
    if "$VENV/bin/python" -c "import $module" >/dev/null 2>&1; then
        log "python module $module is present"
        continue
    fi
    log "installing $package (Mesa's meson.build imports $module)"
    "$VENV/bin/pip" install --quiet "$package" 2>>"$LOG" \
        || die "cannot install $package, which provides the $module module Mesa's meson.build requires"
    "$VENV/bin/python" -c "import $module" >/dev/null 2>&1 \
        || die "$package is installed but 'import $module' still fails"
done
# Mesa also requires mako >= 0.8.0 (meson.build:1118-1119), and a version that
# imports is not a version that satisfies.  The instrument answers a question it
# can only answer by being right, so the answer is trusted.
"$VENV/bin/python" - <<'PY' 2>>"$LOG" || die "the venv's mako is older than the 0.8.0 Mesa's meson.build:1118 requires"
import sys
import mako
parts = (mako.__version__.split(".") + ["0", "0"])[:3]
if tuple(int(p) for p in parts if p.isdigit()) < (0, 8, 0):
    sys.exit(f"mako {mako.__version__} is older than 0.8.0")
PY
log "mako $("$VENV/bin/python" -c 'import mako; print(mako.__version__)') (Mesa requires >= 0.8.0)"

# --- 5. glslang ------------------------------------------------------------
# build-turnip-darwin.py checks for this by name and exits with a brew hint, so
# it is installed here rather than left to the caller to interpret.
if ! command -v glslangValidator >/dev/null 2>&1; then
    log "installing glslang"
    brew install glslang 2>>"$LOG" || die "cannot install glslang"
fi
log "glslangValidator $(glslangValidator --version 2>&1 | head -1 | grep -o '[0-9.]*' | head -1)"

log "PASS the Mesa source, its recorded patches, bison, meson, Mesa's python modules and glslang are all in place"
exit 0
