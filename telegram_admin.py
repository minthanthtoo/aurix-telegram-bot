"""Compatibility alias for :mod:`aurix_vpn.telegram_admin`."""

import sys as _sys

from aurix_vpn import telegram_admin as _module

_sys.modules[__name__] = _module
