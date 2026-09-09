"""Compatibility alias for :mod:`aurix_vpn.telegram_commands`."""

import sys as _sys

from aurix_vpn import telegram_commands as _module

_sys.modules[__name__] = _module
