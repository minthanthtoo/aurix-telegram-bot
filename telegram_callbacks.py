"""Compatibility alias for :mod:`aurix_vpn.telegram_callbacks`."""

import sys as _sys

from aurix_vpn import telegram_callbacks as _module

_sys.modules[__name__] = _module
