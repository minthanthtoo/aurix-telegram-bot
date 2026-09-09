"""Compatibility alias for :mod:`aurix_vpn.runtime`."""

import sys as _sys

from aurix_vpn import runtime as _module

_sys.modules[__name__] = _module
