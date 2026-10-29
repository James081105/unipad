#!/usr/bin/env bash
# The half of the pinned-source bootstraps that is the same twice.
#
# scripts/macos/bootstrap-mesa-turnip.sh and
# scripts/macos/bootstrap-metal2vulkan.sh each place ONE upstream commit under
# vendor/UniXNU/.cache/, prove it is that commit, wipe whatever a previous run
# left in the working tree, and apply a recorded list of patches in order.
# Nothing about those four steps is specific to either upstream -- every guard
# in them was added because a measured failure happened, and both callers would
# otherwise carry the same scar twice.  CLAUDE.md 15/89: two copies of a
# checkout-and-patch procedure is two places for the next one to drift.
#
# WHAT THIS FILE EXPECTS FROM THE SHELL THAT SOURCES IT (there is no other
# interface, and it is deliberately not `set -e`):
#
#   LOG    an absolute, writable path
#   log()  a timestamped line appended to $LOG
#   die()  a message plus the log's tail, then exit 1
#   PY     a python interpreter (only used by the callers, not here)
#
# `log` and `die` stay the caller's rather than being defined here because each
# bootstrap words its own refusals, and a refusal that names the wrong upstream
# is worse than a duplicated printf.  The flip side is that this file is not
# runnable on its own: sourcing it into a shell without those four would fail
# inside the first function, not at the top.
#
# The measurements behind each guard live in bootstrap-mesa-turnip.sh, which
# was the first caller and paid for them.  What follows names only what is
# specific to being shared.
#
# This file is SOURCED, never executed, and it deliberately does not run `set`:
# a library that changed its caller's shell options would be a second place
# that decides how errors are handled.

# ---------------------------------------------------------------------------
# pinned_source_checkout <cache_dir> <mirror> <commit> <tree> <probe> <label>
#
# Places a DETACHED checkout of <commit> at <cache_dir>, fetched from <mirror>.
#
#   <cache_dir>  absolute; created if absent.  Holds .git afterwards.
#   <mirror>     the URL to fetch from.  The caller picks it (a googlesource
#                mirror where the pin names one, the repository URL otherwise);
#                this function does not know which is which.
#   <commit>     the pinned full sha.  Never a branch name.
#   <tree>       that commit's tree sha, or "" when the pin records none.  An
#                empty value SKIPS the tree comparison rather than comparing
#                against "" -- the metal2vulkan pin has no tree field, and a
#                check that cannot fail is worse than no check.
#   <probe>      a command run once the tree is at the pinned commit and before
#                anything is patched, or "" for none.  This is where the caller
#                verifies the files its pin carries sha256s for.
#   <label>      the display name used in the log and refusal messages.
#
# On return the checkout is at <commit>, clean, and UNPATCHED -- call
# pinned_source_apply_patches for the second half.
# ---------------------------------------------------------------------------
pinned_source_checkout() {
    local source="$1" mirror="$2" commit="$3" tree="${4:-}" probe="${5:-}" label="$6"
    local head

    # `git init` + explicit config + `fetch`, never `clone`.  A clone writes its
    # checkout BEFORE any later `git config` could take effect, and
    # `clone -c core.autocrlf=false` does not persist the setting either --
    # MEASURED on the Windows host whose global git sets autocrlf=true, where a
    # -c-flagged clone still produced CRLF on disk.  The consequence is not
    # cosmetic: `git show HEAD:<path>` returns LF, so a patch verifier that
    # compares the tree against HEAD refuses a CRLF tree with "recorded patches
    # do not reconstruct pinned source" -- after every patch applied cleanly.
    # The same difference would change `git diff HEAD --binary`, which the
    # driver hashes into its build profile, so a Windows-produced tree and a
    # macOS-produced one would get different source ids.
    if [[ ! -d "$source/.git" ]]; then
        rm -rf "$source"
        mkdir -p "$source"
        git -c core.autocrlf=false -c core.eol=lf init -q "$source" 2>>"$LOG" \
            || die "cannot initialise $source"
        git -C "$source" remote add origin "$mirror" 2>>"$LOG" \
            || die "cannot add $mirror as a remote for $label"
    fi
    # Both AGAIN, outside the `if`, and that is not redundancy: a checkout left
    # by an earlier revision of this library, or by the workflow's own
    # acquisition step, has no such config, so the fetch below would write it
    # with whatever the host's git says.  The setting has to be a property of
    # THIS repository, not of whoever created the directory.  Unchecked, like
    # every other `git config` here: a config that cannot be written is a git
    # that cannot fetch, and the fetch two lines down says so.
    git -C "$source" config core.autocrlf false 2>>"$LOG"
    git -C "$source" config core.eol lf 2>>"$LOG"

    head="$(git -C "$source" rev-parse HEAD 2>/dev/null)" || head=""
    if [[ "$head" == "$commit" ]]; then
        log "$label already at $commit"
    else
        # MEASURED: fetching by sha works where `clone --branch <sha>` is
        # REFUSED -- a googlesource mirror answers "Remote branch <sha> not
        # found in upstream origin", while `fetch --depth 1 origin <sha>`
        # answers "* branch <sha> -> FETCH_HEAD".  So the fetch is the only
        # mechanism here, and a shallow one keeps the runner's disk and minutes
        # down.
        log "$label at ${head:0:12}, fetching $commit"
        git -C "$source" fetch --depth 1 origin "$commit" 2>>"$LOG" \
            || die "cannot fetch $commit from $mirror"
        git -C "$source" checkout --detach "$commit" 2>>"$LOG" \
            || die "cannot check out $commit"
    fi
    [[ "$(git -C "$source" rev-parse HEAD)" == "$commit" ]] \
        || die "the $label is not at the pinned commit $commit"
    if [[ -n "$tree" ]]; then
        # The commit alone says which change was made; the tree says what the
        # source actually is.  The pin records both, and checking only the first
        # is how a rewritten history with the same subject line gets in.
        [[ "$(git -C "$source" rev-parse 'HEAD^{tree}')" == "$tree" ]] \
            || die "the $label's tree is not the pinned tree; the commit id matches but its content does not"
    fi
    if [[ -n "$probe" ]]; then
        "$probe" || die "the $label's pinned files do not verify; see $LOG"
    fi
}

# ---------------------------------------------------------------------------
# pinned_source_apply_patches <source_dir> <patches_root> <patch_list>
#                             <label> <commit>
#
# Applies every name in <patch_list>, in the order given, to the CLEAN tree at
# <source_dir>.  <patches_root> is the directory the names are relative to, and
# it must be absolute for the same reason $UP is: `git -C "$source" apply
# "$root/$patch"` resolves the patch path against $source, not against this
# script's working directory.
#
# The list is one name per line, fed through a here-document rather than
# `done < <(...)`: macOS's /bin/bash is 3.2, which refuses a here-document
# inside a process substitution while a bash 5 host's `bash -n` accepts it
# happily (measured on run 40386064322, which lost a full macOS run to exactly
# that construct).
# ---------------------------------------------------------------------------
pinned_source_apply_patches() {
    local source="$1" root="$2" list="$3" label="$4" commit="$5"
    local patch applied=0

    # From a clean tree, EVERY time.  Per-patch idempotence --
    # `apply --reverse --check` on each patch alone and skipping the ones that
    # already applied -- is NOT sound when several patches touch one file, and
    # was MEASURED failing: reverse-checking one of them against a tree that
    # also carries the others fails even though the set is complete, and the run
    # then dies on a tree that already had all of them.  Resetting costs one
    # `git checkout` and makes the outcome a function of the pin rather than of
    # whatever the previous run left behind.
    git -C "$source" reset --hard --quiet HEAD 2>>"$LOG" \
        || die "cannot reset the $label tree to the pinned commit"
    # Untracked files have to go too, because the verifier refuses them as
    # unrecorded source changes.  Without -x, so files the source's own
    # .gitignore covers are kept -- those are not in `ls-files --others
    # --exclude-standard` and the verifier never sees them.
    git -C "$source" clean -fdq 2>>"$LOG" || true

    while IFS= read -r patch; do
        [[ -n "$patch" ]] || continue
        [[ -f "$root/$patch" ]] || die "the pin names $patch and it is not in $root"
        git -C "$source" apply "$root/$patch" 2>>"$LOG" \
            || die "cannot apply $patch onto $label $commit"
        applied=$((applied + 1))
    done <<PATCHES
$list
PATCHES
    log "$label patches: $applied applied"
}
