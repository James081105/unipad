"""Windows host adapters: inventory, QEMU, QMP, images, logs and input.

PRD section 9 places ``src/host/`` as "Windows QEMU, QMP, logs, images and
input adapters".  Everything here is stdlib-only so the controller runs on a
bare Windows host, and every failure surfaces as
:class:`unipad.errors.UniPadError` with a stable code.

Modules:

- :mod:`unipad.host.inventory` -- host inventory (PRD 6 WP00, 9.2)
- :mod:`unipad.host.qemu`      -- QEMU launcher with the TCG-only guard
- :mod:`unipad.host.qmp`       -- blocking QMP client over TCP
- :mod:`unipad.host.images`    -- hashing, verification, checkpoints (FR10)
- :mod:`unipad.host.logs`      -- serial tailing and the evidence bundle
- :mod:`unipad.host.input`     -- pointer/keyboard sinks and coordinate math
- :mod:`unipad.host.display`   -- display/audio decisions and install progress
- :mod:`unipad.host.httpsvc`   -- self-controlled HTTPS package service (PRD 3.2 P1)
- :mod:`unipad.host.packed_ramdisk` -- the packed Data ramdisk format, as both the
  bootloader and the kernel driver parse it
- :mod:`unipad.host.apfs_checksum` -- APFS's object checksum, measured from the
  containers this project boots rather than transcribed

Nothing in this package talks to real hardware unless a caller starts a real
QEMU process; simulation tests use the fakes under ``tests/fakes/``.
"""

from __future__ import annotations

from unipad.host.display import (
    AUDIO_MODES,
    DISPLAY_FORMATS,
    DISPLAY_MODES,
    GUEST_AUDIO_VERIFIED,
    PINNED_DISPLAY,
    ROTATIONS,
    STAGE_ORDER,
    AudioDecision,
    DisplayDecision,
    DisplayMode,
    InstallStageProgress,
    Rotation,
    decide,
    render_progress,
    rotate_geometry,
    rotate_point,
    stage_vocabulary_drift,
)
from unipad.host.httpsvc import (
    DEFAULT_GUEST_GATEWAY,
    DEFAULT_GUEST_PORT,
    DEFAULT_LOOPBACK_HOST,
    INDEX_PATH,
    CertificateMaterial,
    PackageServer,
    generate_self_signed_certificate,
    guest_fetch_url,
    hostfwd_spec,
)
from unipad.host.images import ImageManager, KnownGoodPointer, sha256_file
from unipad.host.input import (
    InputSink,
    NullInputSink,
    QmpInputSink,
    guest_to_qemu_abs,
    qcode_for_char,
    scale_point,
)
from unipad.host.inventory import (
    INVENTORY_SCHEMA_VERSION,
    collect_host_inventory,
    write_host_inventory,
)
from unipad.host.logs import (
    EvidenceBundle,
    Screenshotter,
    SerialLogTailer,
    read_ppm_header,
)
from unipad.host.qemu import LaunchResult, QemuLauncher, QmpReadiness, StopResult
from unipad.host.qmp import QmpClient

__all__ = [
    # inventory
    "INVENTORY_SCHEMA_VERSION",
    "collect_host_inventory",
    "write_host_inventory",
    # qemu
    "QemuLauncher",
    "LaunchResult",
    "QmpReadiness",
    "StopResult",
    # qmp
    "QmpClient",
    # images
    "ImageManager",
    "KnownGoodPointer",
    "sha256_file",
    # logs
    "SerialLogTailer",
    "EvidenceBundle",
    "Screenshotter",
    "read_ppm_header",
    # input
    "InputSink",
    "QmpInputSink",
    "NullInputSink",
    "scale_point",
    "guest_to_qemu_abs",
    "qcode_for_char",
    # httpsvc
    "CertificateMaterial",
    "PackageServer",
    "generate_self_signed_certificate",
    "hostfwd_spec",
    "guest_fetch_url",
    "INDEX_PATH",
    "DEFAULT_LOOPBACK_HOST",
    "DEFAULT_GUEST_GATEWAY",
    "DEFAULT_GUEST_PORT",
    # display / audio / install progress (PRD 3.2 P1 (4))
    "PINNED_DISPLAY",
    "DISPLAY_FORMATS",
    "DISPLAY_MODES",
    "ROTATIONS",
    "AUDIO_MODES",
    "GUEST_AUDIO_VERIFIED",
    "STAGE_ORDER",
    "Rotation",
    "DisplayMode",
    "AudioDecision",
    "DisplayDecision",
    "InstallStageProgress",
    "decide",
    "rotate_geometry",
    "rotate_point",
    "render_progress",
    "stage_vocabulary_drift",
]
