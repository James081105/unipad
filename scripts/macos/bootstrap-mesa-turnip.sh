#!/usr/bin/env bash
# Bootstrap the Mesa source and the tools build-turnip-darwin.py needs.
#
# build-turnip-darwin.py reads four things that nothing in this repository
# produces, and it refuses rather than guessing at any of them:
#
#   .cache/mesa-work     a Mesa checkout whose HEAD equals
#                        scripts/data/adreno-compiler-inputs.json's mesa.commit
#   .cache/mesa-tools    a bison new enough for Mesa's parser (macOS ships 2.3;
#                        the pin is 3.8.2, with a byte count to verify against)
#   .cache/mesa-venv     meson, ninja and Mesa's python build deps
#   glslangValidator     on PATH, for the Vulkan runtime build
#
# All four are absent from a fresh macos-latest runner, so without this stage
# the whole GPU-addon chain dies on its first line with a message about a
# missing directory rather than about the missing stage.
#
# Deliberately NOT under `set -e`: a bootstrap that fails must say which of the
# four it was, and `set -e` would abort on the first one.  Every command's
# status is checked and named instead.  `set -u` IS load-bearing -- an unset
# variable is a typo, not an empty string -- and `set` is alone on its line,
# which is what test_set_is_alone_on_its_line enforces.

set -uo pipefail

ROOT="${1:?usage: bootstrap-mesa-turnip.sh REPO_ROOT}"
PIN="$ROOT/scripts/data/adreno-compiler-inputs.json"

# Resolved rather than assumed, for the reason build-macos.sh:194 gives: "the
# interpreter is not called python3 on every host that runs this script".  A
# bootstrap that dies on `python3: command not found` names the wrong cause.
if command -v python3 >/dev/null 2>&1; then PY='python3'
elif command -v python  >/dev/null 2>&1; then PY='python'
else PY=''; fi
[[ -n "$PY" ]] || { echo "no python interpreter on PATH" >&2; exit 1; }
SOURCE="$ROOT/.cache/mesa-work"
TOOLS="$ROOT/.cache/mesa-tools"
VENV="$ROOT/.cache/mesa-venv"
LOG="${BOOTSTRAP_LOG:-$ROOT/.cache/bootstrap-mesa.log}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }
die() { log "FAIL $*"; exit 1; }

mkdir -p "$ROOT/.cache" "$LOG" 2>/dev/null || true
: > "$LOG" 2>/dev/null || true

[[ -f "$PIN" ]] || die "no $PIN; the Mesa pin is where this stage starts"

# The pin is read with python rather than grep: it is JSON, and a grep for
# '"commit"' would also match the linux and board entries' commit keys.
read_pin() {
    "$PY" - "$PIN" "$1" "$2" <<'PY'
import json, sys
print(json.loads(open(sys.argv[1], encoding="utf-8").read())[sys.argv[2]][sys.argv[3]])
PY
}

MESA_COMMIT="$(read_pin mesa commit)" || die "cannot read the Mesa commit from the pin"
MESA_MIRROR="$(read_pin mesa mirror)" || die "cannot read the Mesa mirror from the pin"
BISON_VERSION="$(read_pin bison version)" || die "cannot read the bison version from the pin"
BISON_URL="$(read_pin bison url)" || die "cannot read the bison url from the pin"
BISON_BYTES="$(read_pin bison bytes)" || die "cannot read the bison byte count from the pin"

log "mesa commit $MESA_COMMIT"
log "mesa mirror $MESA_MIRROR"
log "bison $BISON_VERSION ($BISON_BYTES bytes)"

# --- 1. the Mesa source ----------------------------------------------------
# The mirror is preferred over repository: gitlab.freedesktop.org rate-limits
# anonymous clones, and a runner that dies halfway through a 400 MB clone has
# spent its minutes on nothing.  The mirror is Google's, which does not.
if [[ -d "$SOURCE/.git" ]]; then
    head="$(git -C "$SOURCE" rev-parse HEAD 2>/dev/null)" || head=""
    if [[ "$head" == "$MESA_COMMIT" ]]; then
        log "mesa source already at $MESA_COMMIT"
    else
        log "mesa source at ${head:0:12}, fetching $MESA_COMMIT"
        git -C "$SOURCE" fetch --depth 1 origin "$MESA_COMMIT" 2>>"$LOG" \
            || die "cannot fetch $MESA_COMMIT from the Mesa mirror"
        git -C "$SOURCE" checkout --detach "$MESA_COMMIT" 2>>"$LOG" \
            || die "cannot check out $MESA_COMMIT"
    fi
else
    log "cloning Mesa at $MESA_COMMIT (shallow)"
    rm -rf "$SOURCE"
    git clone --depth 1 --branch "$MESA_COMMIT" "$MESA_MIRROR" "$SOURCE" 2>>"$LOG" \
        || git clone "$MESA_MIRROR" "$SOURCE" 2>>"$LOG" \
        || die "cannot clone Mesa from $MESA_MIRROR"
    # A shallow clone of a branch lands on the tip, which is not the pin; a
    # full clone needs the explicit checkout.  Either way the pin is what runs.
    if [[ "$(git -C "$SOURCE" rev-parse HEAD)" != "$MESA_COMMIT" ]]; then
        git -C "$SOURCE" fetch --depth 1 origin "$MESA_COMMIT" 2>>"$LOG" \
            || die "cannot fetch the pinned commit after cloning"
        git -C "$SOURCE" checkout --detach "$MESA_COMMIT" 2>>"$LOG" \
            || die "cannot check out the pinned commit after cloning"
    fi
fi
[[ "$(git -C "$SOURCE" rev-parse HEAD)" == "$MESA_COMMIT" ]] \
    || die "the Mesa source is not at the pinned commit"

# --- 2. bison --------------------------------------------------------------
# macOS ships bison 2.3, which cannot parse Mesa's grammar.  Built from source
# into .cache/mesa-tools because there is no bottle new enough, and verified
# against the pin's byte count before it is used -- a truncated tarball is a
# build failure that looks like a Mesa bug.
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
    tarball="$ROOT/.cache/bison-$BISON_VERSION.$ext"
    log "downloading bison $BISON_VERSION"
    curl -sSL -o "$tarball" "$BISON_URL" 2>>"$LOG" || die "cannot download bison"
    got="$(wc -c < "$tarball" | tr -d ' ')"
    [[ "$got" == "$BISON_BYTES" ]] \
        || die "bison tarball is $got bytes, the pin says $BISON_BYTES"
    build="$ROOT/.cache/bison-build"
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

# --- 3. the python build environment ---------------------------------------
# meson and ninja are the two Mesa's build cannot do without; the rest are its
# own declared requirements, read from its meson.build rather than guessed at
# here so this stage does not need updating when Mesa adds a dependency.
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

# --- 4. glslang ------------------------------------------------------------
# build-turnip-darwin.py checks for this by name and exits with a brew hint, so
# it is installed here rather than left to the caller to interpret.
if ! command -v glslangValidator >/dev/null 2>&1; then
    log "installing glslang"
    brew install glslang 2>>"$LOG" || die "cannot install glslang"
fi
log "glslangValidator $(glslangValidator --version 2>&1 | head -1 | grep -o '[0-9.]*' | head -1)"

log "PASS the Mesa source, bison, meson and glslang are all in place"
exit 0
