"""Compatibility alias for the relocated :mod:`aurix_ai.api_keys` module."""

import sys as _sys

from aurix_ai import api_keys as _module

_sys.modules[__name__] = _module
