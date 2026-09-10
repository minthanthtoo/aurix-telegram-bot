"""Compatibility alias for :mod:`aurix_vpn.identity`."""

import sys as _sys

from aurix_vpn import identity as _module

_sys.modules[__name__] = _module
