"""Compatibility alias/entrypoint for :mod:`aurix_ai.web_api`."""

import sys as _sys

from aurix_ai import web_api as _module

if __name__ == "__main__":
    raise SystemExit(_module.main())

_sys.modules[__name__] = _module
