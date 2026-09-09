"""Compatibility alias for :mod:`aurix_vpn.vpn_dashboard`."""

import sys as _sys

from aurix_vpn import vpn_dashboard as _module

_sys.modules[__name__] = _module
