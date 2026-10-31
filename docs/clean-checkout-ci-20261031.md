# Clean-checkout CI: why `preflight`'s unit stage was red for a reason that is not the code

Date: 2026-10-31
Author: agent (clean-ci)
Scope: `tests/unit` on a checkout with no `vendor/` directory. No production code under
`src/` was changed. `vendor/`, `config/sources.lock.json`, `.github/workflows/` and
`local-private/` were not modified.

> **Integration state (2026-10-31, after this document was written).** The lead had already
> committed part of this work while it was in progress — `ed32d45` ("stop the clean-checkout
> guard from spawning itself") and `a0f1429` ("execute the selector search instead of reading
> it") carry `tests/vendor_paths.py`, the self-sufficiency liveness tests and the selector
> guard. What was still **uncommitted in the working tree** when this document was finalised:
> `tests/vendor_paths.py` (the measured count in its docstring, and the `tests/integration`
> paragraph), `tests/unit/test_gpu_addon_preflight.py` (the marker fix),
> `tests/unit/test_qemu_metal_counters.py` (the fixture guard),
> `tests/unit/test_qemu_metal_bundle_guard.py` (the `require()` wraps and the bare-assert
> replacement), `tests/unit/test_clean_checkout_selfsufficiency.py` (the bare-assert scan
> rule and its shape checks), and this file. The counts in section 4 are measured against the
> full working tree, i.e. committed **plus** uncommitted.

---

## 1. The reproduction

### 1.1 What CI actually runs

`.github/workflows/preflight.yml` declares two jobs:

| job | runner | steps | does it populate `vendor/`? |
| --- | --- | --- | --- |
| `preflight` | `windows-latest` | checkout → setup-python → pip cache → `pip install -e '.[dev]'` → compile → validate lock → **`python -m pytest tests/unit -q`** (`preflight.yml:241-259`) → doctor → report | **No.** The job never clones anything. |
| `integration` | `ubuntu-latest` | checkout → setup-python → install → **clone the two pinned repositories into `vendor/`** (`preflight.yml:465-518`) → prove HEAD == pin (`:521-566`) → `python -m pytest tests/integration -q -rs` (`:568-590`) | Yes, by design. |

So `pytest tests/unit` runs on a runner that has **no `vendor/` at all**. The workflow says
so itself, twice:

* `preflight.yml:236-240` — "the unit stage only at this point … a unit test that needs the
  real pinned upstream tool cannot live in it";
* `preflight.yml:423-425` — "the unit stage above runs in a clean checkout -- `vendor/` is
  git-excluded and empty there".

The other three workflows do not run pytest at all: `build-macos.yml` acquires `vendor/`
(`:324-330`) and then builds; `publish-artifacts.yml` and `verify-artifacts.yml` have no
`pytest` invocation.

### 1.2 What a clean clone has

* `vendor/` is excluded twice over: `.gitignore:31-36` (the rule that travels with the
  repository) and `.git/info/exclude:9` (`/vendor`, local state only). `git ls-files vendor`
  returns 0 entries and `git check-ignore -q vendor` succeeds, so the directory is fetched
  material and never committed.
* `config/sources.lock.json` names the two commits (`device_repo.commit`,
  `unixnu.commit`) and is the only thing a clone carries about them.
* The repository declares **no submodules** — `build-macos.yml:102-105` records that
  `git submodule status` is empty — so `actions/checkout` with `submodules: recursive`
  would fetch nothing anyway.

### 1.3 The measured failure

`git archive HEAD | tar -x -C <tmp>` produces exactly what a clone has (no `vendor/`, no
`.pytest_cache`, no `__pycache__`). Running the unit stage there:

```
$ python -m pytest tests/unit -q
… 32 failed, 3 errors …
EXIT=1
```

The exact error, three times over, from `tests/unit/test_qemu_metal_counters.py:40`:

```
    @pytest.fixture(scope="module")
    def declared() -> dict[str, int]:
        for relative in qm.SOURCES:
            path = REPO.joinpath(*relative)
>           assert path.is_file(), f"{path.relative_to(REPO)} is missing"
E           AssertionError: vendor\UniXNU-qemu\package\QemuMetal\QemuMetalAccelerator.cpp is missing
E           assert False
E            +  where False = is_file()
E            +    where is_file = WindowsPath('…/clean/vendor/UniXNU-qemu/package/QemuMetal/QemuMetalAccelerator.cpp').is_file
```

and from `tests/unit/test_qemu_metal_handoff_guard.py:97` (the same shape, fifteen times):

```
>       source = _text(ACCELERATOR)
E       FileNotFoundError: [Errno 2] No such file or directory: '…/vendor/UniXNU-qemu/package/QemuMetal/QemuMetalAccelerator.cpp'
```

`preflight.yml:258` turns any exit code other than 0 or 5 into `::error::pytest failed`,
so the stage is red. `preflight.yml:255` is the only escape and it is for exit code 5
("no tests collected"), which a *failing* run never produces.

### 1.4 The two other clean-checkout failures

| test | cause | vendor-related? |
| --- | --- | --- |
| `tests/unit/test_gpu_addon_preflight.py::test_the_preflight_refuses_before_any_ring_runs` | `PREFLIGHT_MARKERS` at `:66` contained the literal `"no F:/iosvirtual/vendor/UniXNU"`. `scripts/macos/build-gpu-addon.sh:67` prints `no $REPO/vendor/UniXNU; ring 0 (the bootstraps) runs first`, so the marker matched on exactly one host's absolute path. | **Yes** — the refusal it was matching *is* the vendor check. |
| `tests/unit/test_controller_cli.py::test_doctor_is_structurally_stable_across_invocations` | Failed once in the before run at `:333`; passed in isolation on the same clean checkout, passed on this host, and passed in both after runs. The pytest diff was truncated so the moving key is unidentified, and it is not the disk list — the test already reduces that at `:330-332`. The test's own comment (`:317-329`) documents free-space nondeterminism between two `doctor` invocations, and the full suite writes large files between them. | **No.** UNVERIFIED root cause; not touched. |

---

## 2. Classification

Guard state before the fix:

* **(a) real guard** — an existence check that runs before the read and fires on a clean
  checkout, producing a skip with a stated reason.
* **(b) dead guard** — the condition can never be true (or never false) on the machine the
  test runs on, so the test either never runs or always fails.
* **(c) no guard** — the read happens unconditionally.

### 2.1 Broken (fixed)

| test file | vendor path(s) read | before | after |
| --- | --- | --- | --- |
| `tests/unit/test_qemu_metal_counters.py:38-41` | `vendor/UniXNU-qemu/package/QemuMetal/{QemuMetalAccelerator,LegacyDisplayProbe,QemuMetalPublish,SharedWindow}.cpp`, `vendor/UniXNU/drivers/NativeMobileDisplay/NativeGOPPresenter.cpp` — all via `unipad.host.qemu_metal.SOURCES`, so the path never appears in the test file | **(c)** `assert path.is_file()` inside a module-scoped fixture → 3 **ERROR**s | **(a)** `require()` at `:48` → 3 skips |
| `tests/unit/test_qemu_metal_handoff_guard.py:97,114,134,161,164,187,207,224,261,262,309,322,349,384,403` | `vendor/UniXNU-qemu/package/QemuMetal/*`, `vendor/UniXNU-qemu/scripts/build-qemu-metal.py`, `vendor/UniXNU/drivers/NativeMobileDisplay/{NativeGOPWire.h,NativeGOPPresenter.cpp}` | **(c)** `_text(X)` → 15 **FAILED** | **(a)** `_text(require(X))` → 15 skips |
| `tests/unit/test_qemu_metal_handoff_guard.py:437` (parametrized over `:431`) | the 7 anchor files the guard describes | **(c)** `assert path.is_file()` → 6 **FAILED** | **(a)** `require(path)` at `:437` → 6 skips |
| `tests/unit/test_qemu_metal_bundle_guard.py:115,135,153,175,326` | `vendor/UniXNU-qemu/scripts/build-qemu-metal.py` | **(c)** `_text(BUILDER)` → 9 **FAILED**. One of the nine was a bare `assert BUILDER.is_file()` at `:325`, which the first pass of the fix missed because it is not a read -- it is an existence check with no skip behind it. | **(a)** `_text(require(BUILDER))`, plus `require(BUILDER)` at `:325` → 9 skips |
| `tests/unit/test_qemu_metal_selector.py:86,96` | `vendor/UniXNU-qemu/package/QemuMetal/QemuMetalSelector.h` | **(c)** `assert HEADER.is_file()` → 1 **FAILED**. This file landed in the working tree while this task was in progress (another agent's), and is a *fourth* instance of the same defect. | **(a)** `require(HEADER)` → skip |
| `tests/unit/test_gpu_addon_preflight.py:66` | runs `scripts/macos/build-gpu-addon.sh`, which requires `vendor/UniXNU` (`build-gpu-addon.sh:66-67`) | **(b)** the refusal marker was spelled with this host's absolute path | **(a)** marker made path-independent; the test now **passes** on a clean checkout instead of failing |

### 2.2 Already correct (untouched)

Every one of these has a real existence check before its read and skips on a clean checkout.
They are listed because the fix is a shared helper, and the question "why not migrate these
too" deserves an answer: they work, they are covered by the same liveness run, and rewriting
twelve more files would be churn with no measured benefit.

| test file | vendor path | guard |
| --- | --- | --- |
| `tests/unit/test_host_adt.py:231-233`, `:732-733` | `vendor/UniXNU-qemu/board.toml`, both `scripts/` dirs | **(a)** `if not …is_file(): pytest.skip(…)` |
| `tests/unit/test_host_unixnu_boot.py:245-246` | `vendor/UniXNU-qemu/board.toml` | **(a)** same shape |
| `tests/unit/test_host_material_manifest.py:360` | `vendor/UniXNU-qemu/scripts/build-os-root.py` | **(a)** `skipif` |
| `tests/unit/test_host_packed_ramdisk.py:61-62`, `:83` | `vendor/UniXNU/drivers/…` | **(a)** `skipif` |
| `tests/unit/test_staged_kernel_args_provenance.py:345-347` | `vendor/UniXNU-qemu/board.toml` | **(a)** `skipif` |
| `tests/unit/test_verify_data_mount_rule.py:368-371` | `vendor/UniXNU/scripts/…` | **(a)** skip before read |
| `tests/unit/test_pinned_patches_apply.py:87-88`, `:90` | `vendor/UniXNU/patches`, `kits/NativePrelink/patches` | **(a)** skip before read |
| `tests/unit/test_boot_progress_config_guard.py:56-57`, `:93-94` | `vendor/UniXNU-qemu/configs`, `vendor/UniXNU/scripts/build-xnu-4k-baseline.sh` | **(a)** skip before read |
| `tests/unit/test_kernel_archive_builder_guard.py:131-132`, `:159`, `:187`, `:255-256`, `:330-331`, `:382-383` | both `configs/`, both `scripts/` trees | **(a)** skip before read |
| `tests/unit/test_mesa_bootstrap_venv.py:33-34`, `:39-41` | `vendor/UniXNU/.cache/mesa-work/meson.build` | **(a)** skip before read |
| `tests/unit/test_gpu_addon_preflight.py:47-51`, `:226-227`, `:335-336` | `vendor/UniXNU/…` | **(a)** skip before read |
| `tests/integration/*` (all four files) | `vendor/UniXNU`, `vendor/UniXNU-qemu` | **(a)** skip before read, and the CI job treats "everything skipped" as a failure (`preflight.yml:581-585`) |

### 2.3 The historical instances

* 2026-10-24, `preflight` run 40107080802 — recorded in
  `tests/unit/test_clean_checkout_selfsufficiency.py:12-20`: `read_text()` ran before
  `if not source: pytest.skip()`, so the skip could only fire for a file that exists and is
  empty. **(b)**.
* fact 32 of `docs/implementation-plan.md` — two tests whose guard condition was false on
  every machine (`vendor/` excluded locally, absent on a clone). **(b)**.

Three instances, three different shapes. That is the argument for one shared guard.

---

## 3. What changed

### 3.1 `tests/vendor_paths.py` (new)

The shared guard. `require(*paths, root=None) -> Path`:

* resolves the vendor root from `vendor_root()` — `REPO/vendor`, or
  `UNIPAD_VENDOR_ROOT` when set;
* **skips** when the *owning checkout directory* (`vendor/UniXNU-qemu`,
  `vendor/UniXNU`) is absent, naming it in the reason;
* **fails** when the checkout is present and a file is not — so a renamed anchor stays red;
* re-bases paths onto the configured root, so a caller can build them from
  `REPO / "vendor" / …` at import time and still be redirected;
* returns the first path so it reads inline: `_text(require(ACCELERATOR))`.

The docstring states why a skip is the right answer rather than a pass: a test that cannot
read its subject has measured nothing, and "the contract holds" about a file nobody looked
at is the exact failure shape this repository's tests exist to prevent.

It lives at `tests/vendor_paths.py` rather than inside `tests/unit/` so both suites can
import it — `tests/conftest.py:40-43` puts the repository root on `sys.path`. **It is
currently imported only by `tests/unit`.** `tests/integration` reads the same pinned tree
and guards it correctly, but per file (`test_gpu_addon_blob.py:102`,
`test_pinned_source_library.py:502`, `test_upstream_flag_table.py:54`,
`test_pinned_patches_against_upstream.py:71-72`), and those guards were deliberately left
alone: that job needs the pinned clones, so a migration there is not something this change
could measure. Folding them in is the follow-up, not something this change claims.

### 3.2 `tests/unit/test_clean_checkout_selfsufficiency.py`

The static scan was not able to see any of the 2026-10-31 reads, which is why they survived
it. Three things were wrong with it, and all three are fixed:

1. **It could not see through constants.** `KIT = REPO / "vendor" / …` spells `vendor`;
   `ACCELERATOR = KIT / "QemuMetalAccelerator.cpp"` does not. `_vendor_names()` now resolves
   the chain to a fixpoint.
2. **It could not see through local helpers.** `_text(ACCELERATOR)` is the read; the
   `read_text` is inside `_text`. `_reads_through_a_helper()` now identifies those.
3. **It could not see order.** A function containing *any* existence check passed, wherever
   the check sat — the 2026-10-24 defect's own shape. `_vendor_reads_and_guards()` now
   requires the guard to be evaluated before the read, with the one exception that matters:
   in `_text(require(X))` the guard is an argument of the read, so it runs first.
4. **It read a bare ``assert`` as a guard.** `assert BUILDER.is_file()` *contains* an
   existence check, so revision 2 accepted it — and what it does on a clean checkout is
   fail. A bare assert whose test is a vendor existence check is now an offender in its own
   right. The rule is deliberately narrow: it matches a check the assert makes on its own,
   not one wrapped inside a read, and not the word "vendor" appearing in an unrelated
   assertion. The wider version was tried and flagged `assert cpu["vendor"] == "GenuineFake"`
   (a CPU field), `PREFLIGHT_MARKERS`'s vendor string, and an import guard — three false
   positives, all measured.

`test_the_scan_can_see_the_reads_it_claims_to_see` proves the scan against eight known
shapes (direct, through a helper, through a constant, guard after the read, bare assert,
guarded inline, guarded before, assert after a guard) so a scan that matches nothing cannot
read green. And the shape was checked against the real file, not only the sample:
re-introducing `assert BUILDER.is_file()` at `test_qemu_metal_bundle_guard.py:325` turns
`test_no_unguarded_read_of_the_vendor_checkout[test_qemu_metal_bundle_guard.py]` red and
names the line; removing it again restores green.

### 3.3 The liveness test

`test_the_guarded_modules_skip_rather_than_error_when_the_vendor_root_is_absent` runs every
module that imports the shared guard as a **child pytest** with
`UNIPAD_VENDOR_ROOT=<a directory that does not exist>`, and asserts:

* exit code 0 (not 1, not 5);
* no `FAILED` and no `ERROR` in the output;
* at least one `SKIPPED [` entry, so a run where nothing skipped cannot pass.

The module list is **derived** from the import rather than hand-written, so a module added
next month joins the run on its own. The file excludes itself from that list — without the
exclusion the child re-runs this module, which spawns another child, and the run dies of
`TimeoutExpired` having proved nothing. That was measured, not assumed.

`test_the_guard_does_not_skip_where_the_pin_is_present` is the other direction: on a host
that has the pin, every vendor path the guarded modules build is handed to `require` and
none may skip.

---

## 4. Measured before / after

All numbers are `python -m pytest tests/unit -q -p no:cacheprovider` on a `git archive HEAD`
tree with no `vendor/`. Counted from the progress characters and the `FAILED`/`ERROR` lines,
because this host emits **no summary line at all** when stdout is redirected — not even
through a pipe (`test_clean_checkout_selfsufficiency.py:441` records this; measured again
2026-10-31 on a five-test file, redirected and piped, with the same result).

The method was validated rather than assumed: `pytest tests/unit --collect-only` reports
**3106 tests collected** on the clean tree, and the character count of the "after" run is
3106. The two agree exactly, so the before/after table below is not an estimate.

| | before | after |
| --- | --- | --- |
| tests run | 3087 | 3106 |
| passed | 2915 | 2929 |
| skipped | 137 | 177 |
| failed | 32 | 0 |
| errors | 3 | 0 |
| **exit code** | **1** | **0** |
| vendor-related red items | 34 | 0 |

The 19 new tests are the guard's own tests (section 3.2/3.3) plus the scan's shape checks.

The same method over-counted by 4 on the real-repository run below, so that run's
pass/skip split is reported as approximate and only its exit code and zero failures are
claimed exactly.

Per-file, clean checkout (each file run on its own on the "after" tree):

| test file | before | after |
| --- | --- | --- |
| `tests/unit/test_qemu_metal_counters.py` | 3 ERROR | exit 0, **3 skipped** |
| `tests/unit/test_qemu_metal_handoff_guard.py` | 21 FAILED | exit 0, **22 skipped** |
| `tests/unit/test_qemu_metal_bundle_guard.py` | 9 FAILED | exit 0, **9 skipped** |
| `tests/unit/test_gpu_addon_preflight.py` | 1 FAILED | exit 0, 5 collected: 4 skipped, 1 passed |
| `tests/unit/test_qemu_metal_selector.py` | 1 FAILED | exit 0, 3 collected: 2 skipped, 1 passed |
| `tests/unit/test_clean_checkout_selfsufficiency.py` | (n/a) | exit 0, 89 collected: 88 passed, **1 skipped** |

Two of those numbers are larger than the "before" failure count, and that is the point: a
module with six vendor paths skips six tests where it used to fail one, because the guard
sits inside each test rather than at the module's import.

The last row is worth reading closely. `test_clean_checkout_selfsufficiency.py:579` is
`test_the_guard_does_not_skip_where_the_pin_is_present` skipping *itself*, with the reason
"`vendor/UniXNU-qemu` is not present on this host, so the 'guard does not over-skip' half of
this check is UNVERIFIED here". A check that the guard does not over-skip cannot run where
there is nothing to skip, and it says so instead of passing vacuously — the same rule the
rest of this change applies.

With the real `vendor/` present, on this host:

| run | result |
| --- | --- |
| `test_qemu_metal_bundle_guard.py` + `test_qemu_metal_counters.py` + `test_qemu_metal_handoff_guard.py` | **41 passed**, 0 failed, 0 skipped |
| the same four modules plus `test_qemu_metal_selector.py` | **44 passed**, 0 failed |
| `tests/unit/test_clean_checkout_selfsufficiency.py` | **89 passed** |
| **the whole of `tests/unit`** | **exit 0, 0 failed, 0 errors**, 3106 collected |

The last row is the one that matters for the lead: the repository as it stands, with the pin
acquired, is green end to end. Its pass/skip split (3101 passed / 9 skipped by character
count) is approximate for the reason given above; its exit code and its zero failures are
exact.

The guard is alive in both directions: absent root → skip, present root → run and pass.

### 4.1 The intermediate run, kept because it is the honest picture

The first "after" run was not the last one. It exited 1 with 3 failures, and all three are
worth recording because each was a defect the *before* run could not have shown:

| failure | what it was |
| --- | --- |
| `test_qemu_metal_bundle_guard.py::test_the_hand_copied_dependencies_are_the_ones_the_files_describe` | a bare `assert BUILDER.is_file()` at `test_qemu_metal_bundle_guard.py:325` that the migration missed; fixed, and the scan learned the bare-assert shape |
| `test_clean_checkout_selfsufficiency.py::test_the_guarded_modules_skip_rather_than_error_when_the_vendor_root_is_absent` | the liveness test failing because the row above made its child run red |
| `test_host_material_manifest.py::test_the_source_pin_check_is_on_by_default` | a simulation artifact (section 5), not a defect |

The tree was rebuilt and re-run; that run is the "after" column above. A single green run
would have been the less useful number.

### 4.2 What the guard's own tests cost, measured 2026-10-31

The lead asked whether the liveness run belongs in `tests/unit` at all, given
`preflight`'s 20-minute budget, and quoted 437 s. That figure is real but it is the cost
of the design that was deliberately **not** taken. Measured on a host carrying several
teammates' processes:

| run | cost |
| --- | --- |
| the whole of `tests/unit/test_clean_checkout_selfsufficiency.py` (89 tests) | **17 s** |
| the one subprocess child (`test_the_guarded_modules_skip_rather_than_error_when_the_vendor_root_is_absent`) | **13 s** |
| the other direction, in-process (`test_the_guard_does_not_skip_where_the_pin_is_present`) | **4 s** |

The child runs with the vendor root **absent**, so every test in it skips and nothing
compiles. The 437 s is what a *second* child run against the **real** root costs, because
`test_qemu_metal_selector.py` compiles C++ with MSVC — which is exactly why that direction
is covered in-process instead, and why `test_the_guard_does_not_skip_where_the_pin_is_present`'s
docstring records the number.

`_run()`'s `timeout=600` is per child and there is exactly one child, so it is 46× the
measured cost. If it ever fires the message is a hung child, not a slow host. On these
numbers the file costs 17 s of a 20-minute budget and belongs where it is.

---

## 5. What I could not verify

* **I did not run the GitHub Actions workflow.** There is no `act`-equivalent available here,
  and the `preflight` job is `windows-latest` with a pip cache and a 20-minute budget that
  cannot be reproduced locally. Every number above is a local `pytest` run on a tree built
  with `git archive HEAD`. That the stage's *exit code* handling (`preflight.yml:252-259`)
  maps exit 0 to "unit tests passed" is read from the file, not executed. **CI is not
  claimed fixed.**
* **The simulation is not a perfect clone.** Two things a real runner has that this tree does
  not: an editable install of the tree under test, and a `vendor/` that the `integration`
  job populates. The first mattered — `tests/unit/test_host_material_manifest.py:1219`
  failed in the first "after" run because its child subprocess imported the *host's*
  installed `unipad` (whose `config/sources.lock.json` another agent re-pinned mid-task)
  while the parent read the simulated tree's. The test passes on the real repository. That
  failure is an artifact of the simulation and of a concurrent re-pin, not a defect; it is
  recorded here so nobody chases it.
* `tests/unit/test_controller_cli.py::test_doctor_is_structurally_stable_across_invocations`
  failed once in the **before** run at `test_controller_cli.py:333` and then did not fail
  again: it passed in both "after" runs, in isolation, and in six consecutive repeats on
  the clean tree (measured 2026-10-31, exit 0 each time). It is not vendor-related and was
  not touched. The pytest diff was truncated (`Use -v to get more diff`), so I cannot name
  the key that moved — and it is **not** the disk list, which the test already reduces at
  `test_controller_cli.py:330-332`. Trigger **UNVERIFIED**; it is the kind of
  order-and-timing dependence the test's own comment (`test_controller_cli.py:317-329`)
  warns about, not a clean-checkout defect.
* The static scan cannot see a read that goes through `src/unipad`.
  `test_qemu_metal_counters.py` is guarded only because its fixture happens to check
  `unipad.host.qemu_metal.SOURCES`; a future module taking a vendor path from `src/unipad`
  without checking would pass the scan. `tests/integration` is the layer that covers it.
* `tests/integration` was not run here — it needs the pinned clones, which is what the
  `integration` job does.
* The liveness test's module list is derived from an import line. A module that reads a
  vendor path but does not import the guard is not in the run; the static scan is what
  catches that one, and the two checks are complementary rather than redundant.

---

## 6. A concurrency incident, recorded because it is the kind of thing that loses work

`tests/unit/test_qemu_metal_handoff_guard.py` was being edited by another agent while this
task ran. After a first migration pass over-applied its pattern, I reverted with
`git checkout -- <file>`, which also reverted the other agent's uncommitted selector work
(a `SELECTOR = KIT / "QemuMetalSelector.h"` constant and two assertions). The lead was told
at the time with the exact content observed.

Resolution, verified afterwards: the selector work is on disk and committed as `a0f1429`
("test(metal): execute the selector search instead of reading it"), and the guard wraps in
the same file are committed too. Nothing appears permanently lost. The lesson is the
project's own: do not run a destructive git command in a tree other agents are writing to.
