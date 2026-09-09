"""Compatibility alias for :mod:`aurix_vpn.connectivity`."""

import sys as _sys

from aurix_vpn import connectivity as _module

_sys.modules[__name__] = _module
