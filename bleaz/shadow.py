"""Activate the bleak shadow: make ``import bleak`` resolve to bleaz.

Importing this module prepends the bundled shim directory to ``sys.path`` so
that ``bleak`` and ``bleak_retry_connector`` — including imports inside
third-party libraries such as aiobmsble — resolve to bleaz instead of the real
BlueZ/D-Bus bleak.

Use it as the very first import of your program, before anything imports bleak::

    import bleaz.shadow  # noqa: F401  (redirect bleak -> bleaz, no D-Bus)

Idempotent and safe to import multiple times.
"""

from __future__ import annotations

import os
import sys

_SHADOW_DIR = os.path.join(os.path.dirname(__file__), "_shadow")


def _is_shadow_module(module) -> bool:
    return getattr(module, "__file__", "").startswith(_SHADOW_DIR)


def install() -> None:
    """Prepend the shim directory to sys.path (idempotent)."""
    if "bleak" in sys.modules and not _is_shadow_module(sys.modules["bleak"]):
        import warnings

        warnings.warn(
            "bleaz.shadow.install() ran after the real 'bleak' was already "
            "imported; import bleaz.shadow earlier to fully shadow it.",
            RuntimeWarning,
            stacklevel=2,
        )
    if _SHADOW_DIR not in sys.path:
        sys.path.insert(0, _SHADOW_DIR)


def is_active() -> bool:
    return _SHADOW_DIR in sys.path


install()
