"""Compatibility alias for :mod:`aurix_vpn.commerce`."""

import sys as _sys

from aurix_vpn import commerce as _module

_sys.modules[__name__] = _module
