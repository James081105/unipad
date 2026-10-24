# UniPad 覆盖矩阵（需求 → 实现 → 验证 → 证据）

本文件由 `scripts/windows/build-coverage-table.py` 生成，请勿手改；改代码后重新生成。数据来源是一次真实的 `unipad.acceptance` 运行，不是对进度的转述。

- 运行 id：`coverage-matrix`
- 写入时间：2026-10-24T19:15:01+00:00
- 门禁身份：`unipad.controller.verifier` v1.1.0
- 主机：Microsoft Windows 11 CoreCountrySpecific (zh-CN)
- 加速器：tcg
- Python：3.12.9

## 总判

| 项 | 值 |
| --- | --- |
| 门禁结论 | **BLOCKED** |
| advisory_failures | M13:BLOCKED, M14:BLOCKED, M15:BLOCKED,… |
| blocking_failures | — |
| reasons | blocking_by:M10a:blocked_by:guest_insta… |
| verdict | BLOCKED |
| 证据域 `host_real` | 0 |
| 证据域 `onsite` | 0 |
| 证据域 `simulation` | 55 |

## 门禁检查（M0–M15 / WP00–WP04 / P1）

| 检查 | PRD | 需求 | 层级 | 实现状态 | Windows/模拟 | 运行器验证 | 现场（Windows 实测） | 证据路径 | 阻断原因 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `M0` | docs/PRD.md:206 | 依赖盘点与构建成立 | blocking | UNVERIFIED | FAIL | UNVERIFIED | BLOCKED | `acceptance/config/sources.lock.json`, `acceptance/artifacts/m0/ai-smoke.json` | config/sources.lock.json is absent or unparseable, so there is no pinned source identity… |
| `M10a` | docs/PRD.md:219 | IPA 接受和执行策略明确 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | there is no real guest install service on this host: install_service_unavailable is injec… |
| `M13` | docs/PRD.md:222 | PRD 6 M13, outside the first-release gate | advisory | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | probe_not_registered:M13 |
| `M14` | docs/PRD.md:223 | PRD 6 M14, outside the first-release gate | advisory | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | probe_not_registered:M14 |
| `M15` | docs/PRD.md:224 | PRD 6 M15, outside the first-release gate | advisory | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | probe_not_registered:M15 |
| `P1-1` | docs/PRD.md:86 | 通过 VirtIO Net 等路线联网，优先出站网络和自控 HTTPS 服务。 | advisory | UNVERIFIED | FAIL | BLOCKED | BLOCKED | `acceptance/src/unipad/host/qemu.py`, `acceptance/config/machine.json`, `acceptance/src/unipad/host/httpsvc.py`, `acceptance/src/unipad/guest/protocol.py` (+3 more) | guest_outbound_requires_a_real_ipados_guest |
| `P1-2` | docs/PRD.md:87 | 更多自有或获授权的 IPA 兼容性测试。 | advisory | UNVERIFIED | FAIL | BLOCKED | BLOCKED | `acceptance/src/unipad/ipa/matrix.py`, `acceptance/config/sources.lock.json` | no_recorded_compatibility_observations |
| `P1-3` | docs/PRD.md:88 | 应用卸载、更新、安装历史和清晰的错误分类。 | advisory | UNVERIFIED | SIMULATION PASS | PASS | UNVERIFIED | `acceptance/src/unipad/ipa/lifecycle.py`, `acceptance/artifacts/lifecycle/install-ledger.jsonl` | - |
| `P1-4` | docs/PRD.md:89 | 旋转、可选分辨率、音频和安装包体验。 | advisory | UNVERIFIED | SIMULATION PASS | PASS | UNVERIFIED | `acceptance/src/unipad/host/display.py`, `acceptance/config/machine.json` | - |
| `P1-5` | docs/PRD.md:90 | 真正经过验证的快照/恢复；必须说明保存的是磁盘还是整机状态。 | advisory | UNVERIFIED | SIMULATION PASS | PASS | UNVERIFIED | `acceptance/src/unipad/host/images.py`, `acceptance/artifacts/lifecycle/install-ledger.jsonl` | - |
| `P1-6` | docs/PRD.md:91 | 简洁的 Windows 启动器：开始、停止、导入 IPA、查看日志、恢复。 | advisory | UNVERIFIED | SIMULATION PASS | PASS | UNVERIFIED | `acceptance/scripts/launcher.cmd`, `acceptance/scripts/windows/UniPad-Launcher.ps1`, `acceptance/scripts/windows/Start-UniPad.ps1`, `acceptance/docs/launcher.md` | - |
| `WP00` | docs/PRD.md:232 | 基线冻结：盘点 CPU/RAM/磁盘；获取候选仓库和锁文件；审计上游 AGENTS/贡献约定 | advisory | UNVERIFIED | FAIL | BLOCKED | BLOCKED | `acceptance/host-inventory.json`, `acceptance/config/sources.lock.json`, `acceptance/local-private/upstream/UniXNU/.git/HEAD`, `acceptance/local-private/upstream/UniXNU-qemu/.git/HEAD` (+2 more) | sources.lock.json still declares frozen=false, so the pinned inputs are not a frozen base… |
| `WP01` | docs/PRD.md:233 | 干净构建：先原样构建设备仓库最小配置；记录 bootstrap、ABI 与缓存前提 | advisory | UNVERIFIED | FAIL | BLOCKED | BLOCKED | `acceptance/config/sources.lock.json`, `acceptance/docs/kernel-build-windows.md` | sources.lock.json is not frozen, and the Windows cross-build chain still lacks iig, csh,… |
| `WP02` | docs/PRD.md:234 | Windows UEFI：使用与候选版本匹配的固件和最小设备组合 | advisory | UNVERIFIED | FAIL | BLOCKED | BLOCKED | `acceptance/local-private/tools/qemu/share/edk2-aarch64-code.fd`, `acceptance/local-private/out/real-boot/prelink-substitution/followup/e1-edk2-boot/serial.log` | no UniXNU kernel image built from the pinned sources exists on this host: board.toml name… |
| `WP03` | docs/PRD.md:235 | 引导交接：跑通 UniXNU 与 DarwinBoot，不同时加入 IPA/GPU 工作 | advisory | UNVERIFIED | FAIL | BLOCKED | BLOCKED | `acceptance/local-private/out/real-boot/efi-build-windows/followup/unixnu-build/Build/Out/AARCH64-DEBUG/UniXNU.efi`, `acceptance/local-private/out/real-boot/efi-build-windows/followup/dbe/artifact/DarwinBoot.efi`, `acceptance/local-private/out/real-boot/minimal-tier1-boot/followup/build/UniXNU-qemu/build/qemu-ramfb-efi/QemuRamfb.efi`, `acceptance/local-private/out/real-boot/efi-build-windows/exp1/stock-entry/run.log` (+4 more) | no UniXNU kernel image built from the pinned sources exists on this host: board.toml name… |
| `WP04` | docs/PRD.md:236 | 内核进入：固定 TCG CPU、GIC、时钟、内存与页配置；定位首次真实阻塞 | advisory | UNVERIFIED | FAIL | BLOCKED | BLOCKED | `acceptance/config/machine.json`, `acceptance/local-private/tools/qemu/qemu-system-aarch64.exe`, `acceptance/local-private/tools/qemu/share/edk2-aarch64-code.fd`, `acceptance/local-private/out/real-boot/efi-build-windows/exp1/stock-entry/trace.log` (+1 more) | no UniXNU kernel image built from the pinned sources exists on this host: board.toml name… |
| `case1_start_new_qemu_process` | docs/PRD.md:172 | 在用户 Windows 本机启动一个全新的 QEMU 进程；保留实际配置与版本证据 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is… |
| `M1` | docs/PRD.md:207 | Windows TCG 启动 UEFI | blocking | UNVERIFIED | FAIL | BLOCKED | BLOCKED | `acceptance/local-private/out/real-boot/prelink-substitution/followup/e1-edk2-boot/serial.log`, `acceptance/config/machine.json`, `acceptance/local-private/tools/qemu/qemu-system-aarch64.exe`, `acceptance/local-private/tools/qemu/share/edk2-aarch64-code.fd` | the target-guest boot chain does not exist on this host (no built UniXNU kernel image, no… |
| `case2_reach_springboard` | docs/PRD.md:173 | 进入真实 SpringBoard，通过鼠标操作改变客体界面 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is… |
| `M2` | docs/PRD.md:208 | UniXNU 运行 | blocking | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | no UniXNU kernel image built from the pinned sources exists on this host: board.toml name… |
| `case3_import_hello_ipa` | docs/PRD.md:174 | 在桌面启动后导入 HelloUniPad.ipa，记录输入包 SHA-256；先证明它此前未安装 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | sample_ipa_not_found:HelloUniPad.ipa |
| `M3` | docs/PRD.md:209 | DarwinBoot 完成交接准备 | blocking | PARTIAL | FAIL | BLOCKED | BLOCKED | `acceptance/local-private/out/real-boot/efi-build-windows/followup/unixnu-build/Build/Out/AARCH64-DEBUG/UniXNU.efi`, `acceptance/local-private/out/real-boot/efi-build-windows/followup/dbe/artifact/DarwinBoot.efi`, `acceptance/local-private/out/real-boot/minimal-tier1-boot/followup/build/UniXNU-qemu/build/qemu-ramfb-efi/QemuRamfb.efi` | no UniXNU kernel image built from the pinned sources exists on this host: board.toml name… |
| `case4_install_hello_ipa` | docs/PRD.md:175 | 完成包检查、传输、安装、注册；SpringBoard 出现真实图标，同时获得安装结果与 bundle ID | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is… |
| `M4` | docs/PRD.md:210 | XNU 进入并初始化关键底层 | blocking | PARTIAL | FAIL | BLOCKED | BLOCKED | `acceptance/local-private/out/real-boot/efi-build-windows/exp1/stock-entry/run.log`, `acceptance/local-private/out/real-boot/efi-build-windows/exp1/stock-entry/trace.log`, `acceptance/absent/verification.json-6e937ad2e1c5f999.txt` | no UniXNU kernel image built from the pinned sources exists on this host: board.toml name… |
| `case5_launch_hello_ipa` | docs/PRD.md:176 | 点击图标启动；获得对应进程或等效可信运行证据，并显示本次挑战值 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is… |
| `M5` | docs/PRD.md:211 | IOKit/平台层可用 | blocking | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1… |
| `case6_tap_button_ten_times` | docs/PRD.md:177 | 连续点击按钮 10 次，画面计数正确变化；退出回主屏幕后能再次打开 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is… |
| `M6` | docs/PRD.md:212 | VirtIO Block 可读 | blocking | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1… |
| `case7_notes_app_save_and_reopen` | docs/PRD.md:178 | 导入 UniPadNotes.ipa，输入本次生成的字符串并保存；关闭重开 App 后可读 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | sample_ipa_not_found:UniPadNotes.ipa |
| `M7` | docs/PRD.md:213 | 正确 RootFS/SystemOS 可用 | blocking | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1… |
| `case8_shutdown_and_reboot_from_disk` | docs/PRD.md:179 | 正常关机，结束 QEMU 进程，再从磁盘启动新进程；两款 App 和保存的字符串仍存在 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is… |
| `M8` | docs/PRD.md:214 | 原始用户态 launchd 运行 | blocking | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1… |
| `M9b` | docs/PRD.md:217 | Data 持久化 | blocking | PARTIAL | NOT RUN | BLOCKED | BLOCKED | — | XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1… |
| `case9_three_cold_boots` | docs/PRD.md:180 | 重复冷启动流程三次，保存耗时和结果；测试失败时保留证据，不覆盖上次失败 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is… |
| `M9` | docs/PRD.md:215 | SpringBoard 可操作 | blocking | NOT STARTED | NOT RUN | BLOCKED | BLOCKED | — | XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1… |
| `M9a` | docs/PRD.md:216 | 软件/最小图形路径满足 UIKit | blocking | PARTIAL | NOT RUN | BLOCKED | BLOCKED | — | XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1… |
| `case10_unsupported_platform_package` | docs/PRD.md:181 | 导入一个不支持的平台包，返回明确原因；已有可用系统和应用保持可恢复 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is… |
| `M10` | docs/PRD.md:218 | IPA 安装 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | there is no real guest install service on this host: install_service_unavailable is injec… |
| `M12` | docs/PRD.md:221 | 输入与应用交互 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | there is no real guest install service on this host: install_service_unavailable is injec… |
| `M11` | docs/PRD.md:220 | IPA 启动 | blocking | UNVERIFIED | NOT RUN | BLOCKED | BLOCKED | — | there is no real guest install service on this host: install_service_unavailable is injec… |

## 端到端用例（PRD §5.2 十条）

| 用例 | PRD | 运行器验证 | 理由 | 关联检查 |
| --- | --- | --- | --- | --- |
| `case1` | docs/PRD.md:172 | BLOCKED | case1_start_new_qemu_process:host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system… | case1_start_new_qemu_process |
| `case2` | docs/PRD.md:173 | BLOCKED | case2_reach_springboard:host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system imag… | case2_reach_springboard |
| `case3` | docs/PRD.md:174 | BLOCKED | case3_import_hello_ipa:sample_ipa_not_found:HelloUniPad.ipa | case3_import_hello_ipa |
| `case4` | docs/PRD.md:175 | BLOCKED | case4_install_hello_ipa:host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system imag… | case4_install_hello_ipa |
| `case5` | docs/PRD.md:176 | BLOCKED | case5_launch_hello_ipa:host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image… | case5_launch_hello_ipa |
| `case6` | docs/PRD.md:177 | BLOCKED | case6_tap_button_ten_times:host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system i… | case6_tap_button_ten_times |
| `case7` | docs/PRD.md:178 | BLOCKED | case7_notes_app_save_and_reopen:sample_ipa_not_found:UniPadNotes.ipa | case7_notes_app_save_and_reopen |
| `case8` | docs/PRD.md:179 | BLOCKED | case8_shutdown_and_reboot_from_disk:host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no… | case8_shutdown_and_reboot_from_disk |
| `case9` | docs/PRD.md:180 | BLOCKED | case9_three_cold_boots:host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image… | case9_three_cold_boots |
| `case10` | docs/PRD.md:181 | BLOCKED | case10_unsupported_platform_package:host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no… | case10_unsupported_platform_package |

## 覆盖缺口

（无：门禁未登记任何覆盖缺口）

## 未验证字段

- M0: config/sources.lock.json is absent or unparseable, so there is no pinned source identity to inventory
- M10a: there is no real guest install service on this host: install_service_unavailable is injectable in the fake guest and has never once succeeded against a real object
- M13: probe_not_registered:M13
- M14: probe_not_registered:M14
- M15: probe_not_registered:M15
- P1-1: guest_outbound_requires_a_real_ipados_guest
- P1-2: no_recorded_compatibility_observations
- P1-3: -
- P1-4: -
- P1-5: -
- P1-6: -
- WP00: sources.lock.json still declares frozen=false, so the pinned inputs are not a frozen baseline and no clean build has been run against them
- WP01: sources.lock.json is not frozen, and the Windows cross-build chain still lacks iig, csh, the six SETUP host tools, Darwin freestanding headers and the two Apple-clang builtins (__builtin_xnu_type_summary / __builtin_xnu_types_compatible), so no clean build can be run against these pins
- WP02: no UniXNU kernel image built from the pinned sources exists on this host: board.toml names mach.development.bcm2837..., upstream/UniXNU/build/ is empty, and the Apple kernelcache is excluded as a substitute by four gates in emit_prelink.py plus its own output = bytearray(native_data)
- WP03: no UniXNU kernel image built from the pinned sources exists on this host: board.toml names mach.development.bcm2837..., upstream/UniXNU/build/ is empty, and the Apple kernelcache is excluded as a substitute by four gates in emit_prelink.py plus its own output = bytearray(native_data)
- WP04: no UniXNU kernel image built from the pinned sources exists on this host: board.toml names mach.development.bcm2837..., upstream/UniXNU/build/ is empty, and the Apple kernelcache is excluded as a substitute by four gates in emit_prelink.py plus its own output = bytearray(native_data)
- case1_start_new_qemu_process: host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is configured (disks.system_image is null); no persistent Data image is configured; no guest install service is reachable; no SpringBoard process has been observed
- M1: the target-guest boot chain does not exist on this host (no built UniXNU kernel image, no XNU initialisation), so no actual guest display can be produced for the pass evidence
- case2_reach_springboard: host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is configured (disks.system_image is null); no persistent Data image is configured; no guest install service is reachable; no SpringBoard process has been observed
- M2: no UniXNU kernel image built from the pinned sources exists on this host: board.toml names mach.development.bcm2837..., upstream/UniXNU/build/ is empty, and the Apple kernelcache is excluded as a substitute by four gates in emit_prelink.py plus its own output = bytearray(native_data)
- case3_import_hello_ipa: sample_ipa_not_found:HelloUniPad.ipa
- M3: no UniXNU kernel image built from the pinned sources exists on this host: board.toml names mach.development.bcm2837..., upstream/UniXNU/build/ is empty, and the Apple kernelcache is excluded as a substitute by four gates in emit_prelink.py plus its own output = bytearray(native_data)
- case4_install_hello_ipa: host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is configured (disks.system_image is null); no persistent Data image is configured; no guest install service is reachable; no SpringBoard process has been observed
- M4: no UniXNU kernel image built from the pinned sources exists on this host: board.toml names mach.development.bcm2837..., upstream/UniXNU/build/ is empty, and the Apple kernelcache is excluded as a substitute by four gates in emit_prelink.py plus its own output = bytearray(native_data)
- case5_launch_hello_ipa: host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is configured (disks.system_image is null); no persistent Data image is configured; no guest install service is reachable; no SpringBoard process has been observed
- M5: XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1 entry and stopped on its first undefined instruction, which proves the CPU entered kernel code and proves nothing about page tables, exception vectors, the interrupt controller or the clock
- case6_tap_button_ten_times: host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is configured (disks.system_image is null); no persistent Data image is configured; no guest install service is reachable; no SpringBoard process has been observed
- M6: XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1 entry and stopped on its first undefined instruction, which proves the CPU entered kernel code and proves nothing about page tables, exception vectors, the interrupt controller or the clock
- case7_notes_app_save_and_reopen: sample_ipa_not_found:UniPadNotes.ipa
- M7: XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1 entry and stopped on its first undefined instruction, which proves the CPU entered kernel code and proves nothing about page tables, exception vectors, the interrupt controller or the clock
- case8_shutdown_and_reboot_from_disk: host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is configured (disks.system_image is null); no persistent Data image is configured; no guest install service is reachable; no SpringBoard process has been observed
- M8: XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1 entry and stopped on its first undefined instruction, which proves the CPU entered kernel code and proves nothing about page tables, exception vectors, the interrupt controller or the clock
- M9b: XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1 entry and stopped on its first undefined instruction, which proves the CPU entered kernel code and proves nothing about page tables, exception vectors, the interrupt controller or the clock
- case9_three_cold_boots: host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is configured (disks.system_image is null); no persistent Data image is configured; no guest install service is reachable; no SpringBoard process has been observed
- M9: XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1 entry and stopped on its first undefined instruction, which proves the CPU entered kernel code and proves nothing about page tables, exception vectors, the interrupt controller or the clock
- M9a: XNU has not initialised on this host: the firmware-free route reached the kernelcache EL1 entry and stopped on its first undefined instruction, which proves the CPU entered kernel code and proves nothing about page tables, exception vectors, the interrupt controller or the clock
- case10_unsupported_platform_package: host_cannot_boot_a_guest: no built UniXNU kernel image (kernel.macho); no system image is configured (disks.system_image is null); no persistent Data image is configured; no guest install service is reachable; no SpringBoard process has been observed
- M10: there is no real guest install service on this host: install_service_unavailable is injectable in the fake guest and has never once succeeded against a real object
- M12: there is no real guest install service on this host: install_service_unavailable is injectable in the fake guest and has never once succeeded against a real object
- M11: there is no real guest install service on this host: install_service_unavailable is injectable in the fake guest and has never once succeeded against a real object

---

生成方式：`python scripts/windows/build-coverage-table.py`。四列状态互不替代：实现完成不等于模拟通过，模拟通过不等于运行器验证，运行器验证不等于 Windows 真实验收。
