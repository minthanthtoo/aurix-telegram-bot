"""Compatibility alias for the relocated :mod:`aurix_ai.router` module."""

import sys as _sys

from aurix_ai import router as _module

_sys.modules[__name__] = _module
