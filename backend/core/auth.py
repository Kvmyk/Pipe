"""
Uwierzytelnienie klientow Pipe — wspolne dla protokolu JSON lines i MCP (HTTP).

Kolejnosc: AGENT_TOKEN (admin), AGENT_VIEWER_TOKEN (viewer), tokeny klientow
z `python3 -m backend.tokens`. Bez zadnego skonfigurowanego tokenu — dostep
otwarty (chroni go tunel SSH / uprawnienia socketu), rola admin.
"""

from __future__ import annotations

import hmac


def authorize(token: str) -> tuple[str, str] | None:
    """(tozsamosc, rola) dla tokenu albo None. Porownanie na bajtach, w stalym czasie."""
    from backend import tokens
    from backend.config import settings

    raw = (token or "").encode("utf-8", errors="replace")
    if settings.AGENT_TOKEN and hmac.compare_digest(raw, settings.AGENT_TOKEN.encode()):
        return "admin", "admin"
    if settings.AGENT_VIEWER_TOKEN and hmac.compare_digest(raw, settings.AGENT_VIEWER_TOKEN.encode()):
        return "viewer", "viewer"
    found = tokens.match(token or "")
    if found:
        return f"token:{found[0]}", found[1]
    # Urzadzenia pipe web (web-..., core/webaccess.py) nie zamykaja trybu otwartego: CLI i Telegram bez tokenu
    # dzialaja dalej, a do strony w internecie i tak wchodzi sie tylko z tokenem urzadzenia (pilnuje tego most).
    clients = [t for t in tokens.load() if not str(t.get("name", "")).startswith("web-")]
    if not settings.AGENT_TOKEN and not settings.AGENT_VIEWER_TOKEN and not clients:
        return "open", "admin"
    return None
