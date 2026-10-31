// Compiles QemuMetalSelector.h with MSVC and runs the three cases the fix has
// to distinguish. Run from the repository root:
//   python scripts/windows/test-selector-logic.py
//
// Why this exists rather than a source-text guard: the bug this fixed was
// invisible to one. The broken loop called tunnel(), returned a value, and had
// its return value compared -- a guard reading the source sees all three and
// passes. What separates them is executing the loop with a probe that admits
// different selectors.

#include <cstdio>
#include <cstdlib>

#include "QemuMetalSelector.h"

using qemu_metal_selector::admittedSelector;
using qemu_metal_selector::kNoSelector;

namespace {

int failures = 0;

// What the probe admits, and how many times it was asked. The count matters:
// the loop has to stop at the first success, and a scan that kept going would
// be asking a selector it had already found.
struct Case {
    uint32_t admitted;      // the one selector this case admits
    bool admitNone;         // true when nothing is admitted
    int asked = 0;
    uint32_t firstAsked = 0;
};

bool probe(uint32_t selector, void *context) {
    Case *state = static_cast<Case *>(context);
    if (state->asked == 0) state->firstAsked = selector;
    state->asked += 1;
    if (state->admitNone) return false;
    return selector == state->admitted;
}

void check(const char *name, uint32_t got, uint32_t wanted) {
    if (got == wanted) {
        std::printf("  ok   %s -> %u\n", name, got);
        return;
    }
    std::printf("  FAIL %s -> %u, wanted %u\n", name, got, wanted);
    failures += 1;
}

}  // namespace

int main() {
    // 1. The profile admits selector 0. This is the case the broken loop got
    //    wrong twice over: it probed selector 0 (by accident, correctly), got
    //    success, returned 0 -- and the caller's `if (!selector)` read that 0
    //    as "nothing is admitted" and never served a frame.
    {
        Case state{0, false};
        const uint32_t found = admittedSelector(probe, &state);
        check("selector 0 is admitted", found, 0u);
        check("  ...asked exactly once", static_cast<uint32_t>(state.asked), 1u);
        check("  ...asked selector 0 first", state.firstAsked, 0u);
    }

    // 2. Only a non-zero selector is admitted. The broken loop probed selector
    //    0 two hundred and fifty-six times here, all failing, and then returned
    //    0 -- which the caller read as "nothing is admitted". The number of
    //    asks is the assertion that catches it: 256 asks of the same selector
    //    is the bug, and this case is where it was visible.
    {
        Case state{7, false};
        const uint32_t found = admittedSelector(probe, &state);
        check("only selector 7 is admitted", found, 7u);
        check("  ...asked 0 through 7", static_cast<uint32_t>(state.asked), 8u);
        check("  ...asked selector 0 first", state.firstAsked, 0u);
    }

    // 3. Nothing is admitted. kNoSelector, and NOT 0 -- because 0 is a legal
    //    selector and the two answers have to stay distinguishable all the way
    //    out to the log line.
    {
        Case state{0, true};
        const uint32_t found = admittedSelector(probe, &state);
        check("nothing is admitted", found, kNoSelector);
        check("  ...asked every selector", static_cast<uint32_t>(state.asked), 256u);
    }

    // 4. The highest selector is reachable. A loop bounded at 255 rather than
    //    256 would silently lose it, and the profile's own choice of selector
    //    is not this code's to make.
    {
        Case state{255, false};
        const uint32_t found = admittedSelector(probe, &state);
        check("selector 255 is admitted", found, 255u);
        check("  ...asked every selector", static_cast<uint32_t>(state.asked), 256u);
    }

    // 5. No probe at all is "nothing admitted", not a crash. A null function
    //    pointer is a programming error, but answering kNoSelector keeps the
    //    caller's one comparison correct instead of dereferencing it.
    {
        const uint32_t found = admittedSelector(nullptr, nullptr);
        check("no probe", found, kNoSelector);
    }

    if (failures) {
        std::printf("\n%d FAILURE(S)\n", failures);
        return 1;
    }
    std::printf("\nall selector cases pass\n");
    return 0;
}
