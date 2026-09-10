"""Compatibility alias for :mod:`aurix_vpn.provider_backends`."""

import sys as _sys

from aurix_vpn import provider_backends as _module

_sys.modules[__name__] = _module
