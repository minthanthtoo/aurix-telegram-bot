"""Compatibility alias for :mod:`aurix_vpn.route_failover`."""

import sys as _sys

from aurix_vpn import route_failover as _module

_sys.modules[__name__] = _module
