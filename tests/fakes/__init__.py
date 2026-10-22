"""Fake dependencies for UniPad simulation tests (docs/testing-strategy.md D3).

Everything in this package stands in for a dependency that does not exist on
the Windows development host: no ``qemu-system-aarch64`` binary, no firmware,
no IPSW and no real guest.  A green simulation run therefore proves only that
the *host side* drove the protocol correctly -- it is never evidence that the
guest, the display or the input devices exist (PRD section 7.4).  Field status
stays ``UNVERIFIED`` until real hardware evidence is collected.

Modules:
    :mod:`tests.fakes.fake_qemu` -- a fake ``qemu-system-aarch64`` process with
        a real TCP QMP server, a staged boot serial log, deterministic PPM
        screendumps and an input-event journal.
"""

from .fake_qemu import (
    ARGS_FILENAME,
    BOOT_MARKERS,
    DEFAULT_SCREEN_HEIGHT,
    DEFAULT_SCREEN_WIDTH,
    ERROR_LOG_FILENAME,
    FAKE_NAME,
    INPUT_EVENTS_FILENAME,
    PARTIAL_BOOT_MARKERS,
    QMP_GREETING,
    QMP_VERSION,
    FakeQemuProcess,
    fake_qemu_command,
    fake_qemu_launcher,
    fake_qemu_process,
    free_tcp_port,
    main,
    parse_argv,
    read_jsonl,
)

__all__ = [
    "ARGS_FILENAME",
    "BOOT_MARKERS",
    "DEFAULT_SCREEN_HEIGHT",
    "DEFAULT_SCREEN_WIDTH",
    "ERROR_LOG_FILENAME",
    "FAKE_NAME",
    "FakeQemuProcess",
    "INPUT_EVENTS_FILENAME",
    "PARTIAL_BOOT_MARKERS",
    "QMP_GREETING",
    "QMP_VERSION",
    "fake_qemu_command",
    "fake_qemu_launcher",
    "fake_qemu_process",
    "free_tcp_port",
    "main",
    "parse_argv",
    "read_jsonl",
]
