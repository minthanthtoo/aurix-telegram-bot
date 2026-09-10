"""Compatibility alias for :mod:`aurix_vpn.node_agent`."""

import sys as _sys

from aurix_vpn import node_agent as _module

_sys.modules[__name__] = _module
