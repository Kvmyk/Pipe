"""
Handler narzedzia diagram — mapa infrastruktury albo wlasny diagram modelu,
wysylany uzytkownikowi jako obraz (Attachment).
"""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator

from backend.core import diagram, infra
from backend.core.events import Attachment, Event
from backend.core.handlers.common import reply
from backend.core.session import Session


def _filename(title: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
    return f"{slug or fallback}.png"


async def build_infra_diagram(title: str = "", include_kube: bool | None = None) -> tuple[infra.Infra, str]:
    found = await infra.discover(include_kube=include_kube)
    return found, infra.to_mermaid(found, title)


def attachment_for(rendered: diagram.Rendered, title: str, fallback_name: str) -> Attachment | None:
    if rendered.png is None:
        return None
    return Attachment(
        name=_filename(title, fallback_name),
        mime="image/png",
        data=rendered.png,
        caption=title,
        source=rendered.source,
        text=rendered.ascii,
    )


async def handle_diagram(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """
    mode=infra   — odkrycie infrastruktury hosta i jej mapa
    mode=mermaid — diagram z kodu Mermaid podanego przez model
    """
    mode = str(args.get("mode", "infra") or "infra").strip().lower()
    title = " ".join(str(args.get("title", "") or "").split())[:120]
    summary = ""

    if mode == "infra":
        found, source = await build_infra_diagram(title or "Mapa infrastruktury",
                                                  include_kube=args.get("include_kubernetes"))
        summary = found.summary()
        title = title or "Mapa infrastruktury"
    elif mode == "mermaid":
        source = str(args.get("mermaid", "") or "")
        if not source.strip():
            reply(session, tool_call, "Blad: mode=mermaid wymaga parametru mermaid (kod diagramu).")
            return
        title = title or "Diagram"
    else:
        reply(session, tool_call, f"Blad: nieznany mode {mode!r}. Dostepne: infra, mermaid.")
        return

    try:
        rendered = await diagram.render(source)
    except diagram.DiagramError as exc:
        hint = "Popraw kod i wywolaj diagram ponownie." if mode == "mermaid" else \
            "To blad automatycznej mapy — narysuj diagram sam (mode=mermaid) na podstawie faktow ponizej."
        reply(session, tool_call, f"Blad renderowania Mermaid: {exc}\n{hint}\n\n{summary}".strip())
        return

    attachment = attachment_for(rendered, title, "diagram")
    if attachment is None:
        # Brak renderera (mermaidx niezainstalowany) — uzytkownik dostaje kod.
        yield f"{title}:\n```mermaid\n{rendered.source}\n```"
        reply(session, tool_call, "Renderer diagramow jest niedostepny — uzytkownik dostal kod Mermaid jako tekst."
              + (f"\n\nOdkryte fakty:\n{summary}" if summary else ""))
        return

    yield attachment
    result = "Diagram zostal wyslany uzytkownikowi jako obraz. Opisz go krotko; nie wklejaj kodu Mermaid."
    if summary:
        result += f"\n\nOdkryte fakty:\n{summary}\n\nKod diagramu:\n{rendered.source}"
    reply(session, tool_call, result)
