"""Compatibility alias for :mod:`aurix_vpn.ssconfig`."""

from aurix_vpn.ssconfig import (
    SsconfError,
    SsconfProfileService,
    parse_shadowsocks_uri,
    render_outline_ssconf_document,
    render_ssconf_uri,
    select_outline_route,
)

__all__ = [
    "SsconfError",
    "SsconfProfileService",
    "parse_shadowsocks_uri",
    "render_outline_ssconf_document",
    "render_ssconf_uri",
    "select_outline_route",
]
