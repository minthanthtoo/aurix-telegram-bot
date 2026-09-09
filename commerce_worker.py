"""Compatibility alias for :mod:`aurix_vpn.commerce_worker`."""

import sys as _sys

from aurix_vpn import commerce_worker as _module

_sys.modules[__name__] = _module
