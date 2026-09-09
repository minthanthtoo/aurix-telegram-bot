"""Compatibility alias/entrypoint for :mod:`aurix_vpn.vpn_web_api`."""

import sys as _sys

from aurix_vpn import vpn_web_api as _module

if __name__ == "__main__":
    raise SystemExit(_module.main())

_sys.modules[__name__] = _module
