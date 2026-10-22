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

Nothing in this package talks to real hardware unless a caller starts a real
QEMU process; simulation tests use the fakes under ``tests/fakes/``.
"""

from __future__ import annotations

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
]
