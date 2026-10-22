"""UniPad: Windows controller for QEMU AArch64 TCG + UniXNU iPadOS experiments.

Package layout (PRD section 9):

- ``unipad.errors``   stable error codes (PRD section 9.1 / 10 fault taxonomy)
- ``unipad.models``   shared data models (experiment records, evidence, manifests)
- ``unipad.controller`` experiment state machine, budget, verifier, CLI
- ``unipad.host``      Windows-side QEMU/QMP/image/log/input adapters
- ``unipad.ipa``       package precheck, install state machine, transfer
- ``unipad.guest``     guest-side protocol models, service client, in-memory fake

Runtime has zero third-party dependencies so the controller runs on a bare
Windows host; ``psutil`` is an optional extra for richer host inventory.
"""

__version__ = "0.1.0"
