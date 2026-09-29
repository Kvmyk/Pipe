"""
Pierwsze 5 minut — powitanie po instalacji.

Gdy Pipe wystartuje na nowym serwerze i podlaczy sie pierwszy klient
(bot Telegram subskrybuje zdarzenia), czuwanie wysyla raz powitanie:
mape infrastruktury (obraz), ocene bezpieczenstwa z trzema najwazniejszymi
poprawkami i to, czego Pipe od teraz pilnuje. Bez LLM. Na zadanie: komenda
`welcome` (CLI pokazuje je przy pierwszym polaczeniu z danym serwerem).
Flaga "wyslano" lezy w DATA_DIR/welcome.json.
"""

from __future__ import annotations

import json
import time
from typing import Any

from backend.core import memory


def flag_path():
    return memory.data_dir() / "welcome.json"


def already_sent() -> bool:
    return flag_path().is_file()


def mark_sent() -> None:
    memory._write_atomic(flag_path(), json.dumps({"sent": time.time()}) + "\n")


def _domains(found: Any) -> list[str]:
    from backend.core import checks

    return checks.domains_from(found.routes, found.containers)


async def build(*, checks_report: Any = None, digest_time: str = "07:00", cert_days: int = 14) -> dict[str, Any]:
    """Zdarzenie {"type": "welcome", "title", "sections", "attachment"?}."""
    from backend.core import diagram, infra, posture
    from backend.core.handlers.diagram import attachment_for

    found = await infra.discover()
    audit = await posture.audit_now(report=checks_report, cert_days=cert_days)
    containers = found.containers or []
    running = sum(1 for c in containers if c.state == "running")
    projects = sorted({c.project for c in containers if c.project})
    domains = _domains(found)
    public = sorted({s.port for s in found.listeners if s.public and s.proto.startswith("tcp")})
    try:
        backups = [e.path for e in memory.load_directory() if e.kind == "backup"]
    except OSError:
        backups = []

    stands = []
    if found.containers is None:
        stands.append("Docker: brak dostepu")
    else:
        stands.append(f"kontenery: {running}/{len(containers)} dziala"
                      + (f", projekty compose: {', '.join(projects[:6])}" if projects else ""))
    if domains:
        stands.append(f"domeny: {', '.join(domains[:6])}" + (" ..." if len(domains) > 6 else ""))
    if public:
        stands.append("porty publiczne: " + ", ".join(str(p) for p in public[:12]))
    if found.services:
        stands.append(f"uslugi systemd: {', '.join(found.services[:8])}")

    top = audit.sorted()[:3]
    security = [f"ocena: {audit.score}/100 ({audit.grade})"]
    security += [f"{i}. {f.title}" for i, f in enumerate(top, start=1)]
    if top:
        security.append("napisz \"napraw 1\" — przygotuje poprawke do zatwierdzenia (z kopia i mozliwoscia /cofnij)")

    watching = ["co 2 min: dyski, RAM, obciazenie, kontenery, nowe porty, konta i klucze SSH, logowania SSH"]
    if domains:
        watching.append(f"co godzine: certyfikaty i odpowiedz {len(domains)} domen")
    if backups:
        watching.append(f"swiezosc backupow: {', '.join(backups[:3])}")
    else:
        watching.append("backupy: powiedz mi, gdzie leza (np. \"backupy bazy sa w /var/backups/pg\"), a bede ich pilnowal")
    if digest_time and digest_time.lower() not in ("off", "0"):
        watching.append(f"raport codziennie o {digest_time}")

    sections = [
        {"title": "Co tu stoi", "lines": stands or ["jeszcze nic nie znalazlem"]},
        {"title": "Bezpieczenstwo", "lines": security},
        {"title": "Pilnuje", "lines": watching},
        {"title": "Na poczatek", "lines": ["/audyt — pelny audyt z poprawkami", "/zmiany — co sie zmienilo",
                                           "/wykres — obciazenie", "/cofnij — cofnij ostatnia zmiane",
                                           "albo po prostu napisz, co chcesz zrobic"]},
    ]
    event: dict[str, Any] = {"type": "welcome", "title": f"Czesc! Jestem Pipe na {found.hostname or 'tym serwerze'}.",
                             "sections": sections, "score": audit.score, "grade": audit.grade}
    try:
        rendered = await diagram.render(infra.to_mermaid(found, "Mapa infrastruktury"))
        attachment = attachment_for(rendered, "Mapa infrastruktury", "mapa-infrastruktury")
        if attachment is not None:
            event["attachment"] = attachment.to_wire()
    except diagram.DiagramError:
        pass
    event["text"] = event["title"] + "\n" + "\n".join(
        f"\n{s['title']}:\n" + "\n".join(f"- {l}" for l in s["lines"]) for s in sections)
    return event
