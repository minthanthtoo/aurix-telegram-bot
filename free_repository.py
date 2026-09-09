"""Compatibility alias for :mod:`aurix_vpn.free_repository`."""

import sys as _sys

from aurix_vpn import free_repository as _module

_sys.modules[__name__] = _module
