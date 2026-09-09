"""Compatibility alias for :mod:`aurix_vpn.entitlements`."""

import sys as _sys

from aurix_vpn import entitlements as _module

_sys.modules[__name__] = _module
