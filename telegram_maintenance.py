"""Compatibility alias for :mod:`aurix_vpn.telegram_maintenance`."""

import sys as _sys

from aurix_vpn import telegram_maintenance as _module

_sys.modules[__name__] = _module
