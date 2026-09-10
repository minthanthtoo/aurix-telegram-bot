"""Compatibility alias for :mod:`aurix_vpn.connectivity_adapters`."""

import sys as _sys

from aurix_vpn import connectivity_adapters as _module

_sys.modules[__name__] = _module
