"""AuriX AI gateway package.

The package owns model routing, bounded context construction, external API
authentication, usage accounting, and the AI web entrypoint. VPN and commerce
modules do not import this package.
"""

__all__ = ["api_keys", "router", "web_api"]
