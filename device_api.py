"""Compatibility alias for :mod:`aurix_vpn.device_api`."""

import sys as _sys

from aurix_vpn import device_api as _module

_sys.modules[__name__] = _module
