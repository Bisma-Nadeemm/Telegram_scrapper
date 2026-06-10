from __future__ import annotations

import socks

from config import Settings


def build_telethon_proxy(settings: Settings) -> tuple | None:
    """Return Telethon/PySocks proxy tuple with remote DNS routing enabled."""
    if not settings.proxy_host or not settings.proxy_port:
        return None

    return (
        socks.SOCKS5,
        settings.proxy_host,
        settings.proxy_port,
        True,
        settings.proxy_user,
        settings.proxy_pass,
    )
