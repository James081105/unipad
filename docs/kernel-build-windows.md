# kernel.macho build feasibility on the Windows host

- **Date:** 2026-10-23
- **Host:** Windows 11 Home China 10.0.26200, MSYS2 ucrt64 at
  `F:\iosvirtual\local-private\tools\msys64\ucrt64\bin` (clang/clang++ 23.1.3, ld.lld 23.1.3,
  **ld64.lld 23.1.3**, ar/ranlib/nm/strip/objdump/objcopy, llvm-libtool-darwin,
  llvm-install-name-tool, unifdef 2.12, dsymutil). GNU Make 4.4.1 is
  `F:\iosvirtual\local-private\tools\msys64\usr\bin\make.exe`. Core utils (`flex`, `bison`,
  `m4`, `pax`, `tar`, `gzip`) are in `...\msys64\usr\bin`. **None of these are on `PATH`.**
- **XNU source:** `F:\iosvirtual\local-private\tmp\kernel-macho\xnu-xnu-10063.141.1`
  (xnu-10063.141.1 = the `unixnu.lock` commit)
- **Board build script:** `F:\iosvirtual\local-private\upstream\UniXNU\scripts\build-xnu-4k-baseline.sh`
- **Driver:** `F:\iosvirtual\local-private\upstream\UniXNU\scripts\unixnu\kernel.py`
- **Workspace / evidence:** `F:\iosvirtual\local-private\out\kernel-build\`

## Verdict

`kernel.macho` **cannot be produced on this host by the shipped build path today**. The
blockers are, in dependency order: (1) `iig` — required by the kernel `build` target, not
only `installhdrs`; (2) `csh` — required by `doconf`, which runs on the `build` target for
every component `conf` directory; (3) the six self-built `SETUP/*` host tools, which need
Darwin-only headers and a `codesign` substitute; (4) `ditto` / `python3` called directly by
`build-xnu-4k-baseline.sh`; (5) the Darwin freestanding libc/SDK header set; (6) the two
Apple-clang-only builtins `__builtin_xnu_type_summary` /
`__builtin_xnu_types_compatible`, which MSYS2 clang does not implement. **`mig` is
NOT a blocker** — the earlier record was wrong, and `migcom.exe` produces 73/73 of the
kernel's MIG outputs on this host.

The XNU *source itself* is portable enough that MSYS2 clang compiles real kernel
translation units to genuine `Mach-O 64-bit arm64` objects on Windows. The furthest point
reached is **61 of the 123 `osfmk/kern` translation units compiled to Mach-O arm64
objects**, including `osfmk/kern/bsd_kern.c` (138 KB, 174 symbols), using a hand-built
freestanding header shim plus the `config`-generated option set. A sweep of the whole
`osfmk/kern` directory is in `F:\iosvirtual\local-private\out\kernel-build\sweep-osfmk\`.

### Prerequisite that outranks all six: two required XNU patches are NOT applied

Checked 2026-10-23 against the build tree at
`F:\iosvirtual\local-private\tmp\kernel-macho\xnu-xnu-10063.141.1`:

| Patch | Target | Symbol it adds | Applied? |
|---|---|---|---|
| `xnu-10063.141.1-sparse-index.patch` | `osfmk/kern/kalloc.c` | `kalloc_sparse_prelink_index_version` | **NO** — `grep -c` returns 0 |
| `xnu-10063.141.1-security-init.patch` | `libkern/c++/OSKext.cpp` | `native_prelink_security_init_version` | **NO** |

Both apply cleanly with `patch -p1 --dry-run` (rc=0, no fuzz, no offset) from the build-tree root.
`git apply` is the wrong tool here: the tree lives under `local-private/`, so git resolves the patch's
`a/osfmk/...` paths against the repository toplevel `F:/iosvirtual` instead of the tree, and reports
`Skipped patch` — which reads as "already applied" and is not.

**Why this matters more than any of the six blockers:** `emit_prelink.py:121-123` refuses a kernel
without `kalloc_sparse_prelink_index_version`, and `:140-142` refuses one without the security-init
symbol. Those two gates are exactly what rule out the Apple kernelcache as a donor. A kernel built
from the unpatched tree would therefore be rejected by our own prelink step after a full build — the
work would be complete and useless. **Apply both patches before any build attempt, and assert the two
symbols are present in the linked kernel before running `emit_prelink`.**

Ordering note: the sparse-index patch also changes `kalloc_type_view_parse` to read a `__macho_list`
section, so the kernel's section layout changes. That does not affect the component classification
in the minimal-build question, but it does mean the patched and unpatched trees are not
interchangeable test inputs.

### Four-field status

| Field | Status |
|---|---|
| 实现状态 | Investigation complete; build shims (MIG driver, config-defines generator, freestanding headers) implemented under `out\kernel-build\` |
| Windows/模拟测试状态 | MIG 73/73 OK (Windows/simulation). `doconf` pipeline reproduced under bash: 145 options emitted (Windows/simulation). 61 of 123 `osfmk/kern` TUs compiled to Mach-O arm64 objects (Windows/simulation). Full kernel link **not** achieved. |
| 现场验证状态 | UNVERIFIED — no macOS host, no Xcode, no iphoneos SDK in this environment |
| 阻断原因 | `iig` missing; `csh` missing; six `SETUP/*` host tools unbuildable; `ditto`/`python3` missing from the board script's environment; Darwin freestanding SDK headers absent |

---

## Q1 — Is `iig` needed for a kernel-only build? **YES.**

The earlier record ("`iig` is only referenced by `iokit/DriverKit/Makefile` and
`bsd/sys/Makefile:330`, therefore installhdrs-only") is **wrong about the conclusion**.
`DriverKit` is not in `iokit/Makefile`'s `COMP_SUBDIRS`, but it is reached one level deeper
through the **`doconf`-generated** Makefile, and `build_all` runs `doconf`.

### Make recursion trace (every hop cited)

| # | Hop | Evidence |
|---|---|---|
| 1 | `make -C .cache/xnu-work build SDKROOT=iphoneos ARCH_CONFIGS=ARM64 MACHINE_CONFIGS=BCM2837 KERNEL_CONFIGS=DEVELOPMENT …` | `scripts/build-xnu-4k-baseline.sh:774` — `XNU_BUILD_TARGET` defaults to `build` and is never overridden in the script |
| 2 | `build` ∈ `TOP_TARGETS`; `$(TOP_TARGETS): … -f $(MakeInc_top) $@` | `xnu-xnu-10063.141.1/Makefile:220`, `:233-235` |
| 3 | `build: build_all_bootstrap` | `makedefs/MakeInc.top:527` |
| 4 | `build_all` bootstrap depends on `setup exporthdrs` | `makedefs/MakeInc.top:519` |
| 5 | per-config recipe re-invokes make in `$OBJROOT/DEVELOPMENT_ARM64_BCM2837/..` with `CURRENT_BUILD_CONFIG=DEVELOPMENT^ARM64^BCM2837 … build_all` | `makedefs/MakeInc.top:333-347` |
| 6 | `COMP_SUBDIRS_ARM64 = $(ALL_SUBDIRS)` and `ALL_SUBDIRS` contains `iokit` | `Makefile:237`, `:242-251`, `:271-274`, `:283-285` |
| 7 | `$(eval $(call RECURSIVE_BUILD_RULES_template,build_all,$(COMP_SUBDIRS) $(COMP_SUBDIRS_$(CURRENT_ARCH_CONFIG)),do_build_all,1))` | `makedefs/MakeInc.dir:84` |
| 8 | `build_all_recurse_into_iokit` → `make -C iokit -f iokit/Makefile … build_all` | `makedefs/MakeInc.dir:26-38` |
| 9 | `iokit/Makefile:13` `COMP_SUBDIRS = conf` | `iokit/Makefile:13` |
| 10 | `iokit/conf/Makefile:39` `do_build_all:: do_all`; `do_all` runs `doconf` then `make -C $(TARGET)/DEVELOPMENT -f …/Makefile … build_all` | `iokit/conf/Makefile:24-37` |
| 11 | the generated `iokit/DEVELOPMENT/Makefile` carries `COMP_SUBDIRS = DriverKit Exclaves` — `mkmakefile.c` copies `Makefile.template` **verbatim** (`fprintf(ofp, "%s", line)` at `SETUP/config/mkmakefile.c:216-241`; `grep COMP_SUBDIRS mkmakefile.c` → no hits) | `iokit/conf/Makefile.template:31-32` |
| 12 | `build_all_recurse_into_DriverKit` → `make -C DriverKit -f iokit/DriverKit/Makefile … build_all` | `makedefs/MakeInc.dir:26-38` |
| 13 | `do_build_all:: $(COMP_FILES)` where `COMP_FILES = $(GENERATED_HEADERS) $(GENERATED_IMPL)` | `iokit/DriverKit/Makefile:66`, `:56` |
| 14 | **`$(IIG) --def $< --header $@ --impl … --framework-name DriverKit …`** | `iokit/DriverKit/Makefile:59-62` |

### The IIG rule is live for `SDKROOT=iphoneos`

`iokit/DriverKit/Makefile:58` wraps the rule in
`ifeq ($(filter 1,$(EXCLAVEKIT) $(EXCLAVECORE)),)`. `EXCLAVEKIT ?= 1` /
`EXCLAVECORE ?= 1` are set **only** when `PLATFORM` is `ExclaveKit`/`ExclaveCore`
(`makedefs/MakeInc.cmd:183-186`, `:197-211`), and `PLATFORM` is derived from
`xcrun -sdk iphoneos -show-sdk-platform-path` (`makedefs/MakeInc.cmd:145-153`) →
`iPhoneOS`. Both variables are empty, `$(filter 1,)` is empty, the `ifeq` is **true**,
and the rule runs.

### Independent corroboration that the iig output is a link input

- `iokit/conf/files:11-30` — 18 entries `./DriverKit/*.iig.cpp optional iokitcpp`, under
  the comment `# iig generated implementation files`.
- `iokit/conf/Makefile.template:201` — `IODMACommand.iig.cpo_CXXWARNFLAGS_ADD += -Wno-duplicate-decl-specifier`,
  i.e. `IODMACommand.iig.cpo` is an expected member of `OBJS`.
- `SETUP/config/mkmakefile.c:589-598` (`put_source_file_name`) and `:729-760`
  (`do_rules`): a leading `./` makes the source relative to the **build dir**, emitting
  `IODMACommand.iig.cpo: DriverKit/IODMACommand.iig.cpp` — exactly where the IIG rule
  writes (`makedefs/MakeInc.dir:27` does `MKDIR $(CURDIR)/DriverKit`).
- No `.iig.cpp` / `.iig.h` exists in the source tree (`find` → empty), so they must be
  generated.
- `config/MASTER.arm64.bcm2837:43` `IOKIT_BASE = [ iokit iokitcpp … ]` → `iokitcpp` is on,
  so the `optional iokitcpp` entries are active.

### The `bsd/sys/Makefile:330` comment is accurate but incomplete

`xnu-xnu-10063.141.1/bsd/sys/Makefile:330` reads verbatim:

```
# generated headers needed early (used by iig during installhdrs of iokit/DriverKit)
```

It describes the `installhdrs` path (`iokit/Makefile:9` `INSTINC_SUBDIRS = IOKit DriverKit
System IOKitUser Exclaves` → `build_installhdrs_mi_recurse_into_DriverKit`). It says
nothing about `build_all`, which reaches iig through the `conf` template. Note that
`build_setup` (which produces `_posix_availability.h` / `_symbol_aliasing.h`) is a
dependency of **both** `installhdrs` and `build` (`makedefs/MakeInc.top:384/393`, `:420`,
`:467`, `:519`).

### `iig` availability on this host

`find /f/iosvirtual/local-private/tmp/kernel-macho -iname 'iig'` → nothing. The only
`iig` executable anywhere on this machine is a Python probe substitute at
`F:\iosvirtual\local-private\out\real-boot\availability-pl-and-xnu\followup\shim\iig`
(created Oct 23 01:34), whose docstring cites `iokit/DriverKit/Makefile:59-62`. It is not
a functional iig.

**Conclusion: `iig` is a hard requirement of `make build`, not just `make installhdrs`.**
`iokit/DriverKit/Makefile` also *installs* its generated headers, so `installhdrs` needs
iig too — iig is required twice.

---

## Q2 — What does `build-xnu-4k-baseline.sh` actually require?

### 2a. Tools the shell script itself calls, before `make`

| Tool | Line(s) | Class |
|---|---|---|
| `git` | 16, 18, 23, 40, 45, 47 | (b) on PATH |
| `make` | 30, 115, 123 | (b) `msys64\usr\bin\make.exe` |
| `cmake`, `ninja` | 32 | (b) `msys64\ucrt64\bin` |
| `ditto` | **93, 94, 95** | **(c) unavailable** — `ditto refer/libplatform/include …` |
| `unifdef` | 105 | (b) `msys64\ucrt64\bin\unifdef` |
| **`python3`** | 51, 70, 72, 99, 736, 785 | **(c) unavailable** — only `python` (CPython 3.12) exists; `which python3` → not found |
| `bash` | 123 | (b) Git Bash |

`ditto` and `python3` are called by the script **before any make invocation**, so the build
cannot even reach the XNU Makefiles on this host without shims for them.

### 2b. `SDKROOT` resolution — the root of the macOS-only chain

`makedefs/MakeInc.cmd`:

```
119  SDKROOT ?= macosx
125-131  SDKROOT_RESOLVED := $(shell $(XCRUN) -sdk $(SDKROOT) -show-sdk-path)
         ifeq ($(strip $(SDKROOT)_$(SDKROOT_RESOLVED)),/_)  →  /
         override SDKROOT = $(SDKROOT_RESOLVED)
133-139  same for HOST_SDKROOT; the guard there is $(strip …) so empty → /
141-143  SDKVERSION := $(XCRUN) -sdk $(SDKROOT) -show-sdk-version
145-153  PLATFORMPATH := … -show-sdk-platform-path; PLATFORM via sed/tr; empty → MacOSX
```

The target guard at `:127` requires the literal string `/`; an **empty** result does not
trigger it. The prior `shim/xcrun` (`tmp\kernel-macho\shim\xcrun`, 107 bytes) exits 1 with
empty stdout, so with `SDKROOT=iphoneos`:

- `SDKROOT_RESOLVED = ""` → `override SDKROOT =` **empty**
- `HOST_SDKROOT_RESOLVED = ""` → guard at `:135` true → `/`
- Therefore **`CC, CXX, MIG, MIGCOM, IIG, STRIP, LIPO, LIBTOOL, NM, UNIFDEF, DSYMUTIL,
  NMEDIT, GIT, CTF*, PYTHON, DOCC` are all empty**
- `SDKVERSION = ""`, `PLATFORM = MacOSX`

This is recorded empirically by the prior run's `tmp\kernel-macho\xcrun.log`: 28 target-SDK
lookups (`-sdk -find clang, clang++, mig, migcom, iig, strip, lipo, libtool, otool, nm,
unifdef, dsymutil, nmedit, git, scan-build, ctf_insert, ctfconvert, ctfmerge, ctfdump,
docc, python3` plus the four `-show-*` calls), then the host-SDK set (`-sdk / -find clang,
flex, bison, gm4, codesign_allocate`) — the `-sdk /` prefix proves `HOST_SDKROOT_RESOLVED`
fell back to `/` while the target one stayed empty.

`$(SDKROOT)/$(KPINCDIR)/platform` expands to
`<SDK>/System/Library/Frameworks/Kernel.framework/Versions/A/PrivateHeaders/platform`
(`makedefs/MakeInc.def:1036`, `:1069-1073`, `:915`). The board script populates exactly
that path at `build-xnu-4k-baseline.sh:95`, and `usr/local/lib/kernel/libfirehose_kernel.a`
at `:122-124`/`:548` — but **`KERNEL_SDKROOT` is never passed as `SDKROOT`** to make
(`:91` exports it; it is used only as `DSTROOT` at `:119` and as a `-L` path at `:548`).

### 2c. Full tool census for the `build` target

| Tool | Referenced at | Make var | Class | Notes |
|---|---|---|---|---|
| `xcrun` | `MakeInc.cmd:105,110` + 33 uses | `XCRUN` | (a) shimmed (`tmp\kernel-macho\shim\xcrun`) | everything |
| `sw_vers` | `MakeInc.cmd:344` | `HOST_OS_VERSION` | (b) msys64 `usr/bin/sw_vers` | returns `10.0.26100` (the Windows build) → invalid `-mmacosx-version-min` |
| `clang` | `MakeInc.cmd:218` | `CC` | (b) ucrt64 clang 23.1.3 | build |
| `clang++` | `MakeInc.cmd:221` | `CXX` | (b) | build (`.cpo`, link) |
| `mig` | `MakeInc.cmd:224`; 24 sites | `MIG` | **(a) shimmed** — `bootstrap_cmds-cross_platform\migcom.tproj\mig.sh` + `migcom.exe` | see Q3 |
| `migcom` | `MakeInc.cmd:227` | `MIGCOM` | (a) shimmed — native PE, 258,005 bytes | driven by `mig.sh` |
| `iig` | `MakeInc.cmd:233`; `iokit/DriverKit/Makefile:62` | `IIG` | **(c) unavailable** | see Q1 |
| `strip` | `MakeInc.cmd:236`; `MakeInc.kernel:137-138,159` | `STRIP` | (b) `strip` / `llvm-strip` | final kernel |
| `lipo` | `MakeInc.cmd:239`; `MakeInc.kernel:392,444,486`; `config/Makefile:162`; `MakeInc.san:56-66` | `LIPO` | (b) `llvm-libtool-darwin` | universal merge |
| `libtool` | `MakeInc.cmd:242`; `MakeInc.kernel:186,251,253,256` | `LIBTOOL` | (b) `llvm-libtool-darwin` | archive build |
| `otool` | `MakeInc.cmd:245` | `OTOOL` | defined, **never invoked** | non-blocking |
| `nm` | `MakeInc.cmd:248`; `config/Makefile:118` | `NM` | (b) `nm` / `llvm-nm` | build |
| `unifdef` | `MakeInc.cmd:251`; `MakeInc.rule:85,89,121,126` | `UNIFDEF` | (b) ucrt64 `unifdef` | **installhdrs + build** |
| `dsymutil` | `MakeInc.cmd:254`; `MakeInc.kernel:172-180` | `DSYMUTIL` | (b) ucrt64 `dsymutil` | `.dSYM` |
| `nmedit` | `MakeInc.cmd:257` | `NMEDIT` | defined, never invoked | non-blocking |
| `git` | `MakeInc.cmd:260`; `config/newvers.pl:106` (called directly) | `GIT` | (b) on PATH | `version.c` |
| `scan-build` | `MakeInc.cmd:263`; `Makefile:369` | `SCAN_BUILD` | (c) | `make analyze` only |
| `ctf_insert` / `ctfconvert` / `ctfmerge` / `ctfdump` | `MakeInc.cmd:266,269,272,278`; `MakeInc.kernel:133,143,146,149,161-168` | `CTF*` | (c) unavailable, **guarded** | `MakeInc.cmd:273-275` `ifeq (,$(wildcard $(CTFMERGE))) export DO_CTFMERGE := 0`; `MakeInc.def:897-904` also forces 0 for RELEASE/non-MacOSX. Degrades gracefully **if** the `$(shell …)` yields empty rather than a bogus path. |
| `python3` | `MakeInc.cmd:284`; `MakeInc.rule:133` (`syntax_checker.py`) | `PYTHON` | (c) | see 2a |
| `plutil` | `MakeInc.cmd:335`; `SETUP/config/Makefile:102,113` | `PLUTIL` = `/usr/bin/plutil` | **(c) unavailable** — absent from both msys64 trees | on the `build_setup` path |
| `pax` | `MakeInc.cmd:327`; `MakeInc.top:672`; `Makefile:95,142,159,176` | `PAX` | (b) msys64 `usr/bin/pax` | `installsrc` only |
| `patch` | `MakeInc.cmd:336` | `PATCH` | (b) defined, unused | — |
| **`csh`** | `SETUP/config/doconf:1` (`#!/bin/csh -f`); invoked by `bsd/conf/Makefile:24,26`, `iokit/conf`, `libkern/conf`, `libsa/conf`, `osfmk/conf` | — | **(c) unavailable** — no `csh`/`tcsh` in msys64 or Git Bash | **hard blocker for `build`**, because `do_build_all:: do_all` runs `doconf` (`iokit/conf/Makefile:39`) |
| `sysctl` | `Makefile:146,147,163,164,196,197`; `MakeInc.top:288` | inline `$(shell …)` | (c) | parallelism only; `MakeInc.top:299` builds `[ $(MEMORY_SIZE) -le … ]`, malformed when empty |
| `flex` / `bison` / `gm4` | `MakeInc.cmd:350,353,356`; `SETUP/config/Makefile:31,35` | `HOST_FLEX/BISON/GM4` | (b) `flex`, `bison`, `m4` all exist; **`gm4` does not** | `SETUP/config/` ships only `parser.y` and `lexer.l` — no pre-generated `parser.c`/`lex.yy.c` (verified by `ls`). `gm4` needs a rename/symlink or `HOST_GM4=m4`. |
| `codesign` / `codesign_allocate` | `MakeInc.cmd:359` (**hardcoded `/usr/bin/codesign`**, not via xcrun), `:362` | `HOST_CODESIGN`, `HOST_CODESIGN_ALLOCATE` | **(c) unavailable** | every self-built SETUP tool links with `$(HOST_CODESIGN) -s -` |
| `expr`, `sort`, `od`, `realpath` | `Makefile:148,165,198`; `MakeInc.top:49,299`; `SETUP/newvers:19,20`; `MakeInc.kernel:200`; `mig.sh:69,71` | — | (b) msys64 / Git Bash | `od` is LTO-only; `BUILD_LTO=0` is passed |

### 2d. The six self-built `SETUP/*` host tools

`makedefs/MakeInc.cmd:309-314` — `setsegname`, `kextsymboltool`, `decomment`, `newvers.pl`,
`installfile`, `replacecontents`. All six **failed** in the prior
`installhdrs` dry run (`tmp\kernel-macho\installhdrs-dryrun.txt`) with
`No rule to make target 'externs.o'|'kextsymboltool.o'|'setsegname.o'|'decomment.o'|
'installfile.o'|'replacecontents.o'` from `makedefs/MakeInc.dir:59`.

Root cause of the dry-run failure: `MakeInc.def:1001` `export VPATH = .:$(SOURCE)` plus the
`%.o: %.c` pattern in `SETUP/config/Makefile:25-27`. `$(SOURCE)` is non-empty only because
`MakeInc.dir:34` passes it on the sub-make command line. `Makefile:25` `MAKEFLAGS+=r`
disables only GNU make's *built-in* rules, not this explicit pattern rule.

Even with `VPATH` fixed, each tool has Apple-only dependencies:

| Tool | Apple-only source deps |
|---|---|
| `setsegname` | `<libc.h>` (`:23`), `<mach-o/swap.h>` (`:30`) |
| `kextsymboltool` | `<libc.h>` (`:23`), `<mach/mach_init.h>` (`:27`), `<mach-o/{arch,fat,loader,nlist,swap}.h>` (`:33-37`), `<uuid/uuid.h>` (`:39`), `__private_extern__`; `-lstdc++` (`SETUP/kextsymboltool/Makefile:15`) |
| `decomment` | `<libc.h>` (`:33`) |
| `installfile` | `<copyfile.h>` (`:37`) + `fcopyfile()` (`:108,111`), `<err.h>`, `<sysexits.h>`, `<sys/fcntl.h>` (`:33`) |
| `replacecontents` | `<err.h>`, `<sysexits.h>`, `<sys/fcntl.h>` (`:34`) |
| `newvers.pl` | shells out to `date` (`:58`), `whoami` (`:62`), `git describe --dirty` (`:106`) |

`SETUP/config/config` itself is portable (`externs.c` uses `stdio, sys/types, sys/param,
stdlib, string` via `config.h:57-61`) but needs `bison`+`flex`+`gm4` to generate
`parser.c`/`lex.yy.c`.

### 2e. The link step has no standalone linker

`makedefs/MakeInc.def:644` `LD = $(KC++) -nostdlib` with `KC++ = $(CCACHE) $(CXX)`
(`:120`). The kernel is linked by the **clang++ driver** with
`LDFLAGS_KERNEL_GEN` (`:653-668`, incl. `-Wl,-new_linker`, `-Wl,-pagezero_size,0x0`,
`-Wl,-headerpad,152`), `LDFLAGS_KERNEL_SDK` (`:670`) =
`-L$(SDKROOT)/usr/local/lib/kernel -L$(SDKROOT)/usr/local/lib/kernel/platform`,
`LD_KERNEL_LIBS = -lcc_kext` (`:890`), `LD_KERNEL_ARCHIVES = … -lfirehose_kernel` (`:891`);
recipe at `MakeInc.kernel:234-236`. MSYS2 ships `ld64.lld`, which is the right tool for
`-Wl,-new_linker` / `-Wl,-segaddr` / `-Wl,-image_base`.

### 2f. Verified NOT present anywhere in the tree

`create_boot_archive`, `create_actb`, `ktool`, `shift_cmap`, `reorder_files`,
`seg_addr_table`, `eu-elfcompress`, `ldid`, `mtools`, `install_name_tool`, `ranlib`, `ar`,
`ld64`, `ld_classic`, `gunzip`, `gzip`, `rsync`, `LIBPLATFORM`, `EXTERNAL_HEADERS_DIR`,
`KERNEL_EXTENSION_HEADER`, `makedefs/MakeInc.lib`, `bsd/kern/Makefile`.
`ditto` and the other `codesign` hits live only under `tools/tests/*` and `tests/*`,
reachable solely via `make xnu_tests`.

---

## Q3 — Can MIG produce the whole `osfmk/mach/*.defs` kernel set? **YES, 73/73.**

### The prior `migtest` was incomplete and partly wrong

`tmp\kernel-macho\migtest\run-all.sh` only ran the **install/export header** form
(`-server … -user … -header …`) over `$X/osfmk/mach/*.defs`. It never produced the
`_user.c` / `_server.c` sources the kernel actually compiles
(`osfmk/mach/Makefile:349-369` `MIG_KUSRC`, `:394-414` `MIG_KSSRC`).

It also ran with the wrong flags, which produced two spurious results:

- `migtest/all/upl.log`: `upl.defs, line 78: type 'upl_t' not defined` → fatal.
  `upl_t` lives inside `#ifdef KERNEL_PRIVATE` in `osfmk/mach/mach_types.defs:79-93`.
  `run-all.sh` passed no `-DKERNEL_PRIVATE`, so the type was invisible. **Not a mig defect.**
- `mach_types.log`, `clock_types.log`, `std_types.log`: `no SubSystem declaration` —
  expected: these are the `MIG_TYPES` (`osfmk/mach/Makefile:21-24`) and carry no subsystem.
  `upl.log`'s failure was the only real one, and it was a flag artifact.

### What the kernel build actually needs from mig

`osfmk/mach/Makefile`:

- `:438-447` `MIG_KUSRC` — 20 files: `mig $(MIGFLAGS) $(MIGKUFLAGS) -user X_user.c -header X.h -server /dev/null -sheader /dev/null <defs>`
- `:448-456` `MIG_KSSRC` — 20 files: `mig $(MIGFLAGS) $(MIGKSFLAGS) -user /dev/null -header /dev/null -server X_server.c -sheader X_server.h <defs>`
- `:303-310` `MIG_UUHDRS` — 19 headers: `mig $(MIGFLAGS) -server /dev/null -user /dev/null -header X.h <defs>`
- `:312-320` `MIG_USHDRS` — 14 headers: `mig $(MIGFLAGS) -server /dev/null -user /dev/null -header /dev/null -sheader X_server.h <defs>`

Flag sources:

- `makedefs/MakeInc.def:106` `DEFINES = -DAPPLE -DKERNEL -DKERNEL_PRIVATE -DXNU_KERNEL_PRIVATE -DPRIVATE -D__MACHO__=1 -Dvolatile=__volatile -DXNU_KERN_EVENT_DATA_IS_VLA -DCURRENT_MACHINE_CONFIG_LC=bcm2837`
- `makedefs/MakeInc.def:922` `MIGFLAGS = $(DEFINES) $(INCFLAGS) -novouchers $(ARM64 CFLAGS_) $(ARM64 ARCH_FLAGS_) $(DEPLOYMENT_TARGET_FLAGS)`
- `makedefs/MakeInc.def:928` `MIGKSFLAGS = -DMACH_KERNEL_PRIVATE -DKERNEL_SERVER=1 -mach_msg2`
- `makedefs/MakeInc.def:936` `MIGKUFLAGS = -DMACH_KERNEL_PRIVATE -DKERNEL_USER=1 -maxonstack 1024`
- `makedefs/MakeInc.def:243/276` `ARCH_FLAGS_ARM64 = -arch arm64` (because the board script passes `ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG=arm64`, `build-xnu-4k-baseline.sh:776`)
- `build-xnu-4k-baseline.sh:778` `DEPLOYMENT_TARGET_FLAGS="-miphoneos-version-min=17.0 -DXNU_TARGET_OS_IOS"`

### The one real portability problem, and its fix

`migcom.exe` is a **native Windows PE** binary, so `fopen("/dev/null")` fails
(`migcom.tproj/mig.c:418` `realname = "/dev/null"` inside `myfopen`). Every
`osfmk/mach/Makefile` rule above discards at least one output through `/dev/null`, and
`mig.c:349` additionally opens `InternalHeaderFileName` — which defaults to
`strNULL` → `/dev/null` — whenever `IsKernelServer` is true. `IsKernelServer` is set by
`parser.c:1477-1479` when the defs' subsystem is named `KernelServer`; that is 19 of the 20
`MIG_KSSRC` inputs (only `mach_notify.defs` is not). On macOS this is invisible; on Windows it aborts with
`fatal: "exc.defs", line 131: fopen(/dev/null): No such file or directory`.

Fix (implemented in `out\kernel-build\gen-mig-kernel.sh`): substitute a real scratch file
for every `/dev/null` output, and pass `-iheader <scratch>` so `migcom` writes the internal
header somewhere it can open.

### Result

```
=== KERNEL MIG TOTAL ok=40 fail=0 ===      (20 MIG_KUSRC + 20 MIG_KSSRC)
=== EXPORT HDRS ok=33 fail=0 ===           (19 MIG_UUHDRS + 14 MIG_USHDRS)
```

**73/73, zero failures.** Largest outputs: `task_server.c` 236,281 B,
`mach_port_server.c` 179,473 B, `vm_map_server.c` 129,800 B, `vm_map_user.c` 136,118 B.
Artefacts: `F:\iosvirtual\local-private\out\kernel-build\mig\mach\` (120 files).
Reproduce with `bash F:\iosvirtual\local-private\out\kernel-build\gen-mig-kernel.sh`.

`mig.sh` itself runs unmodified on this host: `uname -s` is `MINGW64_NT-*`, so the `-arch`
branch converts to `-target arm64-apple-darwin` (`mig.sh` `-arch` case) and MSYS2 clang
preprocesses the `.defs` for the Darwin target.

---

## Q4 — How far does the actual compile get?

### 4a. The toolchain can emit genuine Mach-O arm64 objects

```
$ /f/iosvirtual/local-private/tools/msys64/ucrt64/bin/clang -target arm64-apple-darwin -mkernel -nostdlibinc -c t.c -o t.o
$ file t.o
t.o: Mach-O 64-bit arm64 object, flags:<|SUBSECTIONS_VIA_SYMBOLS>
$ llvm-readelf -h t.o
  Magic: Magic64 (0xFEEDFACF)   CpuType: Arm64 (0x100000C)   FileType: Relocatable (0x1)
```

### 4b. `doconf` — a csh script whose body is a pure POSIX pipeline — runs under bash

`SETUP/config/doconf` has `#!/bin/csh -f` and csh syntax, but its core is
`cat $MASTER $MASTER_CPU - $MASTER $MASTER_CPU | unifdef -t -DPLATFORM_… -DCPU_… -DSOC_CONFIG_… -DSYS_… | sed -n … | awk … > config.$SYSID`.
Running that pipeline directly under Git Bash (with
`MASTER=config/MASTER`, `MASTER_CPU=config/MASTER.arm64.bcm2837`, `SYS=DEVELOPMENT`,
`cpu=arm64`, `SOC=bcm2837`, `PLATFORM=iPhoneOS`) produces **162 option lines**:

```
options INET / MACH / IOKIT / IOKITCPP / MACH_BSD / DEVELOPMENT / CONFIG_EMBEDDED /
CONFIG_PMAP_PPL / CONFIG_SCHED_MULTIQ / CONFIG_CLUTCH / MACH_KDP / CONFIG_XNUPOST /
SERIAL_CONSOLE / VIDEO_CONSOLE / CONFIG_EXT_PANICLOG / …
machine "arm64"
makeoptions OSFMK_MACHINE = "arm64"
```

Output: `F:\iosvirtual\local-private\out\kernel-build\config.DEVELOPMENT_ARM64`.

This matters because `config`'s `SETUP/config/mkheaders.c:131` turns each enabled option
into `#define MACRO 1` inside a per-option header, listed from `meta_features.h`; and
`SETUP/config/config` (the binary that does this) needs `bison`+`flex`+`gm4`. The
equivalent 145 `-D` tokens are materialised at
`out\kernel-build\config-defines.txt` and `out\kernel-build\meta_features.h` +
`out\kernel-build\optinc\`.

### 4c. The per-component CFLAGS come from the `doconf`-generated Makefile

- `osfmk/conf/Makefile.template:19` `CFLAGS+= -include meta_features.h -DMACH_KERNEL_PRIVATE -DMACH_KERNEL`
- `bsd/conf/Makefile.template:41` and `iokit/conf/Makefile.template:19` `… -DDRIVER_PRIVATE`
- `libkern/conf/Makefile.template:19` `… -DLIBKERN_KERNEL_PRIVATE -DOSALLOCDEBUG=1`
- `libsa/conf/Makefile.template:19` `… -DLIBSA_KERNEL_PRIVATE`

`MACH_KERNEL_PRIVATE` is **not** in `makedefs/MakeInc.def:106`'s `DEFINES`; it only
appears in `MIGKSFLAGS`/`MIGKUFLAGS` (`:929`, `:937`). Without it,
`osfmk/kern/task.h:588` `task_get_proc_raw`, `osfmk/kern/thread.h:364` `struct thread`
and everything guarded by `#ifdef MACH_KERNEL_PRIVATE` is invisible.

### 4d. `EXPORT_HDRS` is a plain copy — no unifdef

`makedefs/MakeInc.rule:440-452`:
`EXPORT_MI_INC_FILES = $(addprefix $(OBJROOT)/$(EXPDIR)/$(EXPORT_MI_DIR)/, $(EXPORT_MI_LIST))`
(`EXPDIR = EXPORT_HDRS/$(COMPONENT)`, `MakeInc.def:1127`) installed with
`$(INSTALL) $(DATA_INSTALL_FLAGS_RO) $< $@`. So compiling against the source tree is
equivalent to compiling against `EXPORT_HDRS` — the unifdef machinery
(`MakeInc.rule:74-99` `INSTALLHDRS_RULE_template`, which needs `unifdef` + `decomment` +
`installfile` + `replacecontents`) is only for the **DSTROOT** install, not for
`INCFLAGS_GEN`/`INCFLAGS_IMPORT`.

### 4e. What is genuinely missing from this host: the Darwin freestanding SDK headers

XNU compiles with `-nostdlibinc` (`MakeInc.def:310`) and **no `-isysroot`** (verified:
`grep -rn isysroot makedefs/` → no hits). It still needs, from the SDK:
`<string.h>` (144 uses), `<sys/types.h>`/`<sys/_types.h>`, `<sys/cdefs.h>`,
`<uuid/uuid.h>`, `<TargetConditionals.h>`, `<stdint.h>`, `<stdarg.h>`, `<stdlib.h>`,
`<stdio.h>`, `<assert.h>`, `<limits.h>`, `<ctype.h>`, `<errno.h>`, `<unistd.h>`,
`<time.h>`, `<math.h>`, `<inttypes.h>`, `<MacTypes.h>`, `<mach_ldebug.h>`,
`<sys/_types/_size_t.h>`.

Available in the XNU tree itself:
`EXTERNAL_HEADERS/{stdint,stdarg,stdbool,stddef,stdatomic}.h`, `EXTERNAL_HEADERS/{AssertMacros,Availability,AvailabilityInternal,AvailabilityMacros,ptrauth,ptrcheck}.h`,
`bsd/sys/cdefs.h`, `bsd/uuid/uuid.h`, `bsd/arm/_types.h`, `bsd/sys/types.h`,
`bsd/sys/_types/*.h`, `pexpert/pexpert/arm64/board_config.h`.

`refer/libplatform` on this host is the **real** libplatform-316.100.10 tarball
(`out\xnu-port\refer\lp.tgz`, 169 entries) and genuinely does **not** contain
`include/string.h` or `include/os/base.h` — so those come from elsewhere
(`os/base.h` from XNU's own `libkern/os/`, which is on `INCFLAGS_IMPORT` after
`exporthdrs`).

The shim set built for this investigation is at
`F:\iosvirtual\local-private\out\kernel-build\hdr\` (13 files) plus
`sdkroot\` (libplatform copy) and `exporthdr\san\` (the `san` component's exported
headers). `hdr\machine\types.h` is a copy of XNU's own `bsd/arm/_types.h` plus the ISO
scalar aliases and the `__darwin_*` scalars it does not define.

### 4f. The furthest point reached

**`osfmk/kern/bsd_kern.c` compiles cleanly.**

```
$ bash F:\iosvirtual\local-private\out\kernel-build\compile-one.sh osfmk kern/bsd_kern.c
$ ls -la F:\iosvirtual\local-private\out\kernel-build\obj\osfmk\bsd_kern.c.o
-rw-r--r-- 1 … 138000 Oct 23 04:49 bsd_kern.c.o
$ file bsd_kern.c.o
bsd_kern.c.o: Mach-O 64-bit arm64 object, flags:<|SUBSECTIONS_VIA_SYMBOLS>
$ llvm-nm bsd_kern.c.o | wc -l
174
```

Command, flags and the one externally-injected define are all recorded in
`out\kernel-build\compile-one.sh`.

**`-DHAS_APPLE_PAC=1` is an externally-injected define, not in the XNU tree.**
`grep -rn "HAS_APPLE_PAC" xnu-…` finds 20+ *uses* but **no `#define` anywhere in the
source tree**, and nothing in `config/MASTER`, `config/MASTER.arm64`,
`config/MASTER.arm64.bcm2837`, or `makedefs/`. Without it, `osfmk/arm/pmap/pmap.h`
fails with `error: duplicate member 'reserved10'` — line 431 (`#if defined(CONFIG_ROSETTA)`
→ else) and line 476 (`#if HAS_APPLE_PAC` → else) both emit `reserved10`. On macOS this
define is supplied by the Xcode/XBS build environment.

### 4g. Sweep of the whole `osfmk/kern` directory — **61 / 123 TUs compile**

`bash out\kernel-build\sweep.sh osfmk out\kernel-build\list-osfmk-kern.txt` (123 TUs, all of
`osfmk/kern/*.c`). Result:

```
=== SWEEP osfmk ok=61 fail=62 total=123 ===
```

Per-TU logs: `F:\iosvirtual\local-private\out\kernel-build\sweep-osfmk\*.log`;
failure list: `sweep-osfmk\failed.txt`. 61 genuine `Mach-O 64-bit arm64` objects are in
`F:\iosvirtual\local-private\out\kernel-build\obj\osfmk\`.

Failure-cause histogram (first error per failing TU):

| Cause | Count | Nature |
|---|---|---|
| `use of unknown builtin '__builtin_xnu_type_summary'` | 20 | **hard blocker** — Apple-Clang-only builtin, absent from MSYS2 clang 23.1.3 |
| `use of undeclared identifier 'caddr_t'` | 11 | header-shim gap (fixable) |
| `call to undeclared function 'bzero'` | 6 | header-shim gap (fixable) |
| `use of unknown builtin '__builtin_xnu_types_compatible'` | 5 | **hard blocker** — same |
| `fatal error: 'TrustCache/API.h'` | 4 | `EXTERNAL_HEADERS/TrustCache/` is **absent from the source tarball** |
| `#error unsupported arch` (`osfmk/kern/hv_support.h:39`) | 2 | macro/config gap |
| `#error ARM/ARM64 not supported` (`osfmk/kern/copyout_shim.h:38`) | 1 | macro/config gap |
| `fatal error: 'device/device_server.h'` | 1 | MIG-generated by `osfmk/device/Makefile:52` — not in my generator's scope |
| `fatal error: 'libkern/version.h'` | 1 | generated by `config/newvers.pl` from `config/version.c.template` |
| `no member named 'kpc_buf' in 'struct thread'` | 1 | config-option gap |
| `use of undeclared identifier 'max_unsafe_rt_quanta'` | 1 | config-option gap |
| `call to undeclared function 'mt_core_snap'` | 1 | header gap |
| `call to undeclared function 'memcmp_zero_ptr_aligned'` | 1 | header-shim ordering |
| `use of undeclared identifier 'RSIZE_MAX'` / `'user64_addr_t'` / `'bcopy'` | 3 | header-shim gaps |
| `too many arguments provided to function-like macro invocation` (`osfmk/kern/spl.c:38`) | 1 | header-shim gap |

**25 of the 62 failures (40%) are the two Apple-Clang-only builtins.**
`osfmk/kern/kalloc.h:765` uses `__builtin_xnu_type_summary` and `:827` uses
`__builtin_xnu_types_compatible`, **unguarded**, and `kalloc.h` is included almost
everywhere. Minimal reproduction:

```
$ cat tb.c
struct s { int a; };
unsigned f(void){ return (unsigned)__builtin_xnu_type_summary(struct s); }
$ clang -target arm64-apple-darwin -mkernel -nostdlibinc -fsyntax-only tb.c
tb.c:2:36: error: use of unknown builtin '__builtin_xnu_type_summary' [-Wimplicit-function-declaration]
```

These builtins exist only in Apple's clang fork. On the Windows host they can only be
supplied by (a) patching `kalloc.h`, or (b) building a clang that implements them.

Note that 11 of the remaining failures are plain `caddr_t` / `bzero` gaps in my
hand-written `hdr\` shim — i.e. the sweep number is a **lower bound** on what the host
toolchain can do, and an **upper bound** on what the current shim set achieves. Neither
number is a kernel.

### 4i. The only prior "kernel" artefact is a stub, not XNU

`F:\iosvirtual\local-private\out\xnu-port\out-obj\kernel-d.macho` (66,336 bytes,
`Mach-O 64-bit arm64 executable, flags:<NOUNDEFS|DYLDLINK|TWOLEVEL|PIE>`) links
`build_config.o`, `kern_priv.o`, `source_pthread_bootstrap.o`, `kcformat.o`,
`KernelConfigTables.cpo` and `startstub.o`, where `startstub.c` is literally
`void __start(void) { for (;;) { } }` (`out\xnu-port\bounded-link3.sh`). It has
`__text` of 0x0c bytes. **It is not an XNU kernel and must not be presented as one.**

---

## Q5 — The single next blocker

There are two, and they are on different paths:

> **(A) To get the *build system* running:** build `SETUP/config/config` (needs `bison` +
> `flex` + `gm4` → `m4`) and run the `doconf` pipeline without `csh`, so that
> `meta_features.h`, the per-option headers and the per-component `CFLAGS +=` lines are
> generated by the real tool instead of by the hand-built equivalents in
> `out\kernel-build\`.

> **(B) To get the *compile* further:** supply `__builtin_xnu_type_summary` and
> `__builtin_xnu_types_compatible`, or patch them out of `osfmk/kern/kalloc.h:765` and
> `:827`. MSYS2 clang 23.1.3 does not implement them; `kalloc.h` is included almost
> everywhere; they alone account for 25 of the 62 `osfmk/kern` failures. This is the wall
> the sweep hit, and no amount of build-system shimming clears it.

Concretely, the next actionable steps, in order:

1. **Symlink/alias `gm4` → `m4`** (`HOST_GM4=m4`, or a 2-line wrapper in `shim/`).
   `bison` and `flex` already exist at `msys64\usr\bin`.
2. **Port `SETUP/config/doconf` to bash.** Its body is already a pure POSIX pipeline
   (proved in 4b); only the option parsing and the `foreach`/`switch` wrappers need
   translating. This removes the `csh` blocker for **every** component `conf` directory
   (`osfmk`, `bsd`, `iokit`, `libkern`, `libsa`, `pexpert`, `san`, `security`).
3. **Build the six `SETUP/*` host tools** with a `HOST_CC` that has `<libc.h>`,
   `<mach-o/*.h>`, `<uuid/uuid.h>`, `<copyfile.h>`, `<err.h>`, `<sysexits.h>` shims and a
   no-op `codesign`/`codesign_allocate`. `setsegname`, `decomment`, `installfile`,
   `replacecontents` are small and mostly self-contained.
4. **Provide `iig`.** It is required by `make build` (Q1), not just `installhdrs`. The
   18 `.iig` files are in `iokit/DriverKit/`; the interface is
   `iig --def <file> --header <out.h> --impl <out.cpp> --framework-name DriverKit -- <cflags>`
   (`iokit/DriverKit/Makefile:62`). Apple's `iig` source is in `DriverKit`'s
   `iig.tproj`; it is a Clang-based tool and would need to be built against the MSYS2
   clang.
5. **Provide `ditto` and `python3`** for `build-xnu-4k-baseline.sh:93-95` and
   `:51,70,72,99,736,785` (`python3` is a one-line `python` alias; `ditto` is a
   `cp -R` equivalent).
6. **Make `xcrun` resolve.** Either a real Apple SDK (impossible on Windows) or an
   `xcrun` shim that answers `-show-sdk-path` with a local tree and `-find <tool>` with
   the MSYS2 equivalents, plus explicitly exporting `SDKROOT` so clang's Darwin driver
   picks up the sysroot.
7. **Finish the freestanding header set** (`hdr\`) to cover `<stdlib.h>`, `<stdio.h>`,
   `<unistd.h>`, `<time.h>`, `<math.h>`, `<inttypes.h>`, `<MacTypes.h>`, and confirm the
   remaining `-D` gaps of the kind `HAS_APPLE_PAC` exposed.
8. **Supply `__builtin_xnu_type_summary` and `__builtin_xnu_types_compatible`, or patch
   them out of `osfmk/kern/kalloc.h:765` and `:827`.** MSYS2 clang 23.1.3 does not
   implement them (they are Apple-clang fork builtins); `kalloc.h` is included almost
   everywhere, so this alone blocks ~40% of `osfmk/kern`. Without a decision here, the
   compile cannot get past `osfmk/kern` at all.

---

## Artefacts (all under `F:\iosvirtual\local-private\out\kernel-build\`)

| Path | What |
|---|---|
| `gen-mig-kernel.sh` | Reproduces all 73 MIG kernel outputs. Prints `ok=40 fail=0` and `ok=33 fail=0`. |
| `mig\mach\` | 120 generated files (20 `_user.c`, 20 `_server.c`, 33 export headers, logs) |
| `config-defines.py` | Parses `config/MASTER.arm64.bcm2837` + `config/MASTER` (superseded by the direct `doconf` pipeline) |
| `config.DEVELOPMENT_ARM64` | 162 option lines, produced by running `doconf`'s own pipeline under bash |
| `config-defines.txt` | 145 `-D` tokens derived from it |
| `meta_features.h`, `optinc\*.h` | Equivalent of `SETUP/config/mkheaders.c` output |
| `compile-one.sh` | Single-TU compile with the exact upstream flag set; prints the full clang command |
| `sweep.sh`, `list-osfmk-kern.txt`, `sweep-osfmk\` | 123-TU sweep harness, logs, `failed.txt` |
| `hdr\` | 13-file freestanding Darwin header shim |
| `sdkroot\` | libplatform-derived `usr/local/include` + `Kernel.framework/.../PrivateHeaders/platform` |
| `exporthdr\san\` | the `san` component's exported headers |
| `obj\osfmk\bsd_kern.c.o` | 138 KB Mach-O 64-bit arm64 object, 174 symbols — the furthest verified artefact |

## Read-only sources consulted (not modified)

`local-private\tmp\kernel-macho\` (prior evidence), `local-private\upstream\`,
`local-private\out\xnu-port\` (a prior bounded-compile attempt).

---

## Reproduction

All commands assume Git Bash. `X` below is
`/f/iosvirtual/local-private/tmp/kernel-macho/xnu-xnu-10063.141.1`.

```bash
# Q3 — regenerate every MIG kernel output (expect ok=40 fail=0 and ok=33 fail=0)
bash /f/iosvirtual/local-private/out/kernel-build/gen-mig-kernel.sh

# Q4b — run doconf's own pipeline under bash (no csh), producing the option set
X=/f/iosvirtual/local-private/tmp/kernel-macho/xnu-xnu-10063.141.1
cd "$X"
echo +DEVELOPMENT | cat config/MASTER config/MASTER.arm64.bcm2837 - \
    config/MASTER config/MASTER.arm64.bcm2837 \
  | /f/iosvirtual/local-private/tools/msys64/ucrt64/bin/unifdef -t \
      -DPLATFORM_iPhoneOS -DCPU_arm64 -DSOC_CONFIG_bcm2837 -DSYS_DEVELOPMENT \
      -USOC_IS_VIRTUALIZED - \
  | sed -n -e "/^+/{" -e "s;[-+];#&;gp" -e 't loop' -e ': loop' -e 'n' \
      -e '/^#/b loop' -e '/^$/b loop' \
      -e 's;^\([^#]*\).*#[ \t]*<\(.*\)>[ \t]*$;\2#\1;' -e 't not' \
      -e 's;\([^#]*\).*;#\1;' -e 't not' -e ': not' -e 's;[ \t]*$;;' \
      -e 's;^\!\(.*\);\1#\!;' -e 'p' -e 't loop' -e 'b loop' -e '}' \
      -e "/^[^#]/d" -e 's;\t; ;g' \
      -e "s;^# *\([^ ]*\)[ ]*=[ ]*\[\(.*\)\].*;\1#\2;p" \
  | awk '-F#' '...' > /tmp/config.DEVELOPMENT_ARM64     # the awk program is in
                                                       # SETUP/config/doconf, verbatim

# Q4f — compile one real XNU TU to a Mach-O arm64 object
bash /f/iosvirtual/local-private/out/kernel-build/compile-one.sh osfmk kern/bsd_kern.c
file /f/iosvirtual/local-private/out/kernel-build/obj/osfmk/bsd_kern.c.o

# Q4g — the 123-TU osfmk/kern sweep
bash /f/iosvirtual/local-private/out/kernel-build/sweep.sh osfmk \
    /f/iosvirtual/local-private/out/kernel-build/list-osfmk-kern.txt
```
