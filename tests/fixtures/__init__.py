"""Shared test fixtures for the UniPad code-stage test suite.

This package holds the *builders* that other tests import; it deliberately
contains no ``test_*`` modules and no assertions of its own (its own
self-check is run by ``tests/`` owners as a scratch script, see
``docs/testing-strategy.md`` D3: fakes must be deterministic).

What lives here
---------------
* :mod:`fixtures.macho_builder` -- structurally valid thin Mach-O images and
  FAT/universal containers (``LC_BUILD_VERSION``, ``LC_ENCRYPTION_INFO_64``,
  ``LC_LOAD_DYLIB``, ``LC_RPATH``, ``LC_CODE_SIGNATURE``).
* :mod:`fixtures.ipa_builder` -- real ``.ipa`` zip archives (valid Device
  build plus the six corrupt variants the precheck must reject).

Import contract
---------------
The public API is re-exported here, so a test may write either::

    from fixtures.ipa_builder import build_ipa, hello_ipa
    from fixtures import build_ipa, hello_ipa

**Verified with pytest 8.4.2 (default ``prepend`` import mode) on this
host.** Which spelling resolves depends on the ``tests/`` layout, and this
package's ``__init__.py`` alone does *not* put ``tests/`` on ``sys.path``:

===========================================  ==============  ==================
Layout of the importing test module           ``fixtures``   ``tests.fixtures``
===========================================  ==============  ==================
``tests/unit/test_x.py``, no ``__init__.py``   no              no
``tests/unit/test_x.py`` + ``unit/__init__``   no              no
``tests/__init__.py`` + ``unit/__init__.py``   no              **yes**
``tests/test_x.py`` (directly in ``tests/``)   **yes**         no
any layout + ``tests/conftest.py`` that        **yes**         yes
inserts ``tests/`` into ``sys.path``
===========================================  ==============  ==================

With the package-tree layout (row 3) a sibling test may also use a relative
import -- ``from ..fixtures.ipa_builder import build_ipa`` from
``tests/unit/test_x.py`` -- which needs no ``sys.path`` help at all.

Recommended for this suite: a ``tests/conftest.py`` that inserts ``tests/``
into ``sys.path`` (then ``from fixtures... import ...`` works everywhere),
or a full ``tests/__init__.py`` package tree with ``from tests.fixtures...``
imports.  Either file is owned by whoever owns ``tests/`` -- this package
only provides the builders.

No secrets, no host state, no clock, no randomness: every fixture is
byte-reproducible (PRD 9.2, testing-strategy D3).
"""

from __future__ import annotations

from .ipa_builder import (
    CORRUPT_VARIANTS,
    DEFAULT_APP_NAME,
    DEFAULT_BUNDLE_ID,
    DEFAULT_DEVICE_DYLIBS,
    DEFAULT_EXECUTABLE,
    FRAMEWORK_INSTALL_NAME,
    FRAMEWORK_MEMBER,
    NOTES_CONTAINER_MARKER,
    NOTES_CONTAINER_MARKER_BYTES,
    PKGINFO_MEMBER,
    build_app_members,
    build_ipa,
    hello_ipa,
    notes_ipa,
    valid_device_ipa,
)
from .macho_builder import (
    ARM64,
    ARM64E_SUBTYPE,
    FAT_MAGIC,
    MH_DYLIB,
    MH_EXECUTE,
    MH_MAGIC_64,
    PLATFORM_IOS,
    PLATFORM_IOSSIMULATOR,
    PLATFORM_MACOS,
    X86_64,
    align_up,
    arm64e_slice,
    build_fat,
    build_thin_macho,
    default_device_slice,
    device_fat,
    dylib_slice,
    mixed_platform_fat,
    pack_version,
    simulator_slice,
    x86_64_ios_slice,
)

__all__ = [
    # macho_builder
    "ARM64",
    "ARM64E_SUBTYPE",
    "X86_64",
    "FAT_MAGIC",
    "MH_MAGIC_64",
    "MH_EXECUTE",
    "MH_DYLIB",
    "PLATFORM_MACOS",
    "PLATFORM_IOS",
    "PLATFORM_IOSSIMULATOR",
    "build_thin_macho",
    "build_fat",
    "pack_version",
    "align_up",
    "default_device_slice",
    "simulator_slice",
    "x86_64_ios_slice",
    "arm64e_slice",
    "dylib_slice",
    "device_fat",
    "mixed_platform_fat",
    # ipa_builder
    "CORRUPT_VARIANTS",
    "DEFAULT_APP_NAME",
    "DEFAULT_BUNDLE_ID",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_DEVICE_DYLIBS",
    "FRAMEWORK_INSTALL_NAME",
    "FRAMEWORK_MEMBER",
    "PKGINFO_MEMBER",
    "NOTES_CONTAINER_MARKER",
    "NOTES_CONTAINER_MARKER_BYTES",
    "build_ipa",
    "build_app_members",
    "valid_device_ipa",
    "hello_ipa",
    "notes_ipa",
]
