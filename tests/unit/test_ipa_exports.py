"""The public surface of :mod:`unipad.ipa` must actually resolve.

``__all__`` is a promise.  A name listed there but never imported is a
``from unipad.ipa import *`` failure that no other test catches -- it happened
here, when the lifecycle exports were added to ``__all__`` before the import
block was updated, and every existing test still passed because they all import
from the submodule directly.

The rule this pins, in both directions: a name in ``__all__`` is an attribute
of the package, and a public attribute is in ``__all__``.
"""

from __future__ import annotations

import importlib
import types

import pytest

import unipad.ipa as ipa


def test_every_exported_name_resolves_on_the_package():
    missing = [name for name in ipa.__all__ if not hasattr(ipa, name)]
    assert missing == [], f"listed in __all__ but not importable: {missing}"


def test_every_public_attribute_of_the_package_is_exported():
    """A public name that is not exported is a promise quietly refused."""
    # ``annotations`` is the one legitimate exception: ``from __future__ import
    # annotations`` binds it as a module attribute, and it is a compiler
    # directive rather than part of the API.
    public = {
        name
        for name in dir(ipa)
        if not name.startswith("_")
        and name != "annotations"
        and not isinstance(getattr(ipa, name), types.ModuleType)
    }
    unlisted = sorted(public - set(ipa.__all__))
    assert unlisted == [], f"public but not in __all__: {unlisted}"


def test_the_lifecycle_surface_is_reachable_from_the_package_root():
    """PRD 3.2 P1 (3): the lifecycle API is part of the ipa contract."""
    module = importlib.import_module("unipad.ipa.lifecycle")
    for name in ("InstallLedger", "LifecycleAction", "classify_failure"):
        assert getattr(ipa, name) is getattr(module, name), (
            f"{name} is shadowed at the package root"
        )
