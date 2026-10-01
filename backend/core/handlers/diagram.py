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
from backend.core.i18n import tr


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
        found, source = await build_infra_diagram(title or tr("Mapa infrastruktury", "Infrastructure map"),
                                                  include_kube=args.get("include_kubernetes"))
        summary = found.summary()
        title = title or tr("Mapa infrastruktury", "Infrastructure map")
    elif mode == "mermaid":
        source = str(args.get("mermaid", "") or "")
        if not source.strip():
            reply(session, tool_call, tr("Blad: mode=mermaid wymaga parametru mermaid (kod diagramu).",
                                         "Error: mode=mermaid requires the mermaid parameter (diagram code)."))
            return
        title = title or "Diagram"
    else:
        reply(session, tool_call, tr(f"Blad: nieznany mode {mode!r}. Dostepne: infra, mermaid.",
                                     f"Error: unknown mode {mode!r}. Available: infra, mermaid."))
        return

    try:
        rendered = await diagram.render(source)
    except diagram.DiagramError as exc:
        hint = tr("Popraw kod i wywolaj diagram ponownie.", "Fix the code and call diagram again.") \
            if mode == "mermaid" else \
            tr("To blad automatycznej mapy — narysuj diagram sam (mode=mermaid) na podstawie faktow ponizej.",
               "This is an error of the automatic map — draw the diagram yourself (mode=mermaid) from the facts below.")
        reply(session, tool_call, (tr(f"Blad renderowania Mermaid: {exc}", f"Mermaid rendering error: {exc}")
                                   + f"\n{hint}\n\n{summary}").strip())
        return

    attachment = attachment_for(rendered, title, "diagram")
    if attachment is None:
        # Brak renderera (mermaidx niezainstalowany) — uzytkownik dostaje kod.
        yield f"{title}:\n```mermaid\n{rendered.source}\n```"
        reply(session, tool_call, tr("Renderer diagramow jest niedostepny — uzytkownik dostal kod Mermaid jako tekst.",
                                     "The diagram renderer is unavailable — the user got the Mermaid code as text.")
              + (tr("\n\nOdkryte fakty:\n", "\n\nDiscovered facts:\n") + summary if summary else ""))
        return

    yield attachment
    result = tr("Diagram zostal wyslany uzytkownikowi jako obraz. Opisz go krotko; nie wklejaj kodu Mermaid.",
                "The diagram was sent to the user as an image. Describe it briefly; do not paste the Mermaid code.")
    if summary:
        result += tr("\n\nOdkryte fakty:\n", "\n\nDiscovered facts:\n") + summary + \
            tr("\n\nKod diagramu:\n", "\n\nDiagram code:\n") + rendered.source
    reply(session, tool_call, result)
