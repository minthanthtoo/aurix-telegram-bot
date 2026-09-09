"""Compatibility alias for :mod:`aurix_vpn.telegram_transport`."""

import sys as _sys

from aurix_vpn import telegram_transport as _module

_sys.modules[__name__] = _module
