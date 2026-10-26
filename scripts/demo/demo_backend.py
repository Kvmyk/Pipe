"""
Atrapa backendu Pipe do nagrania demo (GIF w README) — bez serwera i bez LLM.

Mowi protokolem JSON lines (docs/protocol.md) na 127.0.0.1:<port>, wiec `pipe web` (clients/webui/server.py)
laczy sie z nia jak z prawdziwym backendem. Ramki buduje z prawdziwych typow Pipe: Progress / Activity
(core/events.py) przez server.event_frame(), schemat przez core/graph.build() z przykladowej infrastruktury,
plan bezpiecznika przez safety.Plan.describe(). Odpowiedzi agenta sa ZAPLANOWANE (scenariusz ponizej),
nie generowane przez model.

    python scripts/demo/demo_backend.py --port 7390 --lang pl

Nagranie: scripts/demo/record.py (uruchamia te atrape, `pipe web` i przegladarke).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Nazwy w scenariuszu zaleza od jezyka (configure()): "sklep" po polsku, "shop" po angielsku.
PROJECT = HOST = DOMAIN = CONF = COMMAND = ""


def names(lang: str) -> dict[str, str]:
    project = "shop" if lang == "en" else "sklep"
    return {"project": project, "host": f"vps-{project}", "domain": f"{project}.example.com",
            "conf": f"/etc/nginx/sites-enabled/{project}.conf"}


def configure(lang: str) -> None:
    global PROJECT, HOST, DOMAIN, CONF, COMMAND
    n = names(lang)
    PROJECT, HOST, DOMAIN, CONF = n["project"], n["host"], n["domain"], n["conf"]
    COMMAND = f"sed -i 's/proxy_read_timeout 60s;/proxy_read_timeout 120s;/' {CONF} && systemctl reload nginx"


configure("pl")
ENTRY = "a1b2c3d4"


def L(pl: str, en: str) -> str:
    return en if os.environ.get("PIPE_LANG", "pl").startswith("en") else pl


def infra():
    from backend.core.hostinfo import Socket
    from backend.core.infra import Container, Infra, PortMap, Route

    def c(name, image, project, service, ports=(), health="healthy"):
        return Container(name=name, image=image, state="running", health=health, project=project, service=service,
                         workdir=f"/srv/{project}", ports=[PortMap("127.0.0.1", p, p) for p in ports],
                         networks=[f"{project}_default"])

    return Infra(
        hostname=HOST, os_name="Ubuntu 24.04 LTS",
        containers=[
            c("shop-app", "ghcr.io/acme/shop:2.4.1", PROJECT, "app", (8080,)),
            c("shop-worker", "ghcr.io/acme/shop:2.4.1", PROJECT, "worker"),
            c("shop-db", "postgres:16", PROJECT, "db"),
            c("shop-redis", "redis:7-alpine", PROJECT, "redis"),
            c("uptime-kuma", "louislam/uptime-kuma:1", "monitoring", "kuma", (3001,)),
        ],
        routes=[Route("nginx", [DOMAIN, "www." + DOMAIN], "127.0.0.1:8080", CONF),
                Route("nginx", ["status.example.com"], "127.0.0.1:3001", "/etc/nginx/sites-enabled/status.conf")],
        listeners=[Socket("tcp", "0.0.0.0", 80), Socket("tcp", "0.0.0.0", 443), Socket("tcp", "0.0.0.0", 22)],
        services=["nginx", "ssh", "cron", "docker", "fail2ban"],
    )


def plan_text() -> str:
    from backend.core.safety import Check, Plan
    plan = Plan(files=[CONF], pre=[Check("nginx -t")], post=[Check(L("usluga nginx aktywna", "nginx service active"))],
                sites=True, auto_restore=True, reload_after_restore=["nginx"])
    return plan.describe()


def frame(event) -> dict:
    from backend.server import event_frame
    return event_frame(event)


async def send(writer, payload: dict) -> None:
    writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()


async def done(writer, data: dict | None = None) -> None:
    payload = {"response": "", "status": "ok", "done": True}
    if data is not None:
        payload["data"] = data
    await send(writer, payload)


async def tool(writer, call_id: str, name: str, args: dict, steps: list[str], seconds: float, *, ok: bool = True) -> None:
    """Jedno wywolanie narzedzia tak, jak widzi je interfejs webowy: start, postep, koniec."""
    from backend.core import graph
    from backend.core.events import Activity, Progress
    activity = Activity(call_id, "start", name, graph.describe(name, args), tuple(graph.locate(name, args, f"/srv/{PROJECT}")))
    await send(writer, frame(activity))
    for step in steps:
        await asyncio.sleep(seconds / max(1, len(steps)))
        await send(writer, frame(Progress(step)))
    await asyncio.sleep(seconds / max(1, len(steps)) if steps else seconds)
    from dataclasses import replace
    await send(writer, frame(replace(activity, phase="end", ok=ok)))


async def chunked(writer, text: str, pause: float = 0.0) -> None:
    await asyncio.sleep(pause)
    await send(writer, frame(text))


async def investigate(writer) -> None:
    await asyncio.sleep(0.8)
    await tool(writer, "t1", "execute_command", {"command": "docker logs --tail 200 shop-app"},
               [L("logi kontenera shop-app", "shop-app container logs")], 1.4)
    await tool(writer, "t2", "execute_command", {"command": "tail -n 80 /var/log/nginx/error.log"}, [], 1.2)
    await tool(writer, "t3", "web_search", {"query": "nginx upstream timed out 110 reading response header"},
               [L("Szukam w sieci: nginx upstream timed out 110 reading response header",
                  "Searching the web: nginx upstream timed out 110 reading response header")], 1.8)
    await tool(writer, "t4", "web_fetch", {"url": "https://nginx.org/en/docs/http/ngx_http_proxy_module.html"},
               [L("Czytam strone: nginx.org/en/docs/http/ngx_http_proxy_module.html",
                  "Reading page: nginx.org/en/docs/http/ngx_http_proxy_module.html")], 1.6)
    await tool(writer, "t4b", "execute_command", {"command": "nginx -T | grep -n proxy_read_timeout"}, [], 0.9)
    from backend.core import graph
    from backend.core.events import Activity
    args = {"command": COMMAND}
    await send(writer, frame(Activity("t5", "start", "execute_command", graph.describe("execute_command", args),
                                      tuple(graph.locate("execute_command", args, "/")))))
    await asyncio.sleep(0.4)
    await send(writer, frame(L(
        f"[POTWIERDZ] Operacja wymaga potwierdzenia: `{COMMAND}`\n{plan_text()}",
        f"[POTWIERDZ] Operation requires confirmation: `{COMMAND}`\n{plan_text()}")))
    await send(writer, frame(Activity("t5", "wait", "execute_command", graph.describe("execute_command", args),
                                      tuple(graph.locate("execute_command", args, "/")))))


async def apply_fix(writer) -> None:
    from backend.core import graph
    from backend.core.events import Activity, Progress
    args = {"command": COMMAND}
    activity = Activity("t5", "start", "execute_command", graph.describe("execute_command", args),
                        tuple(graph.locate("execute_command", args, "/")))
    await send(writer, frame(activity))
    for text in (L(f"kopia: {CONF}", f"backup: {CONF}"), L("sprawdzam przed zmiana: nginx -t", "checking before the change: nginx -t"),
                 L(f"sprawdzam strony przed zmiana: {DOMAIN}", f"checking sites before the change: {DOMAIN}"),
                 L("weryfikuje: usluga nginx aktywna", "verifying: nginx service active"),
                 L("sprawdzam strony po zmianie", "checking sites after the change")):
        await asyncio.sleep(0.6)
        await send(writer, frame(Progress(text)))
    await asyncio.sleep(0.5)
    from dataclasses import replace
    await send(writer, frame(replace(activity, phase="end", ok=True, entry=ENTRY)))
    await chunked(writer, L(
        f"Naprawione — **{DOMAIN}** znów odpowiada.\n\n"
        "**Przyczyna:** generowanie raportu PDF w `shop-app` trwa ok. 75 s, a nginx czekał na odpowiedź tylko 60 s "
        "(`proxy_read_timeout`) i zwracał 502 (`upstream timed out (110)` w `error.log`).\n\n"
        f"**Zmiana:** `proxy_read_timeout 120s` w `{PROJECT}.conf`. `nginx -t` przeszedł, nginx przeładowany, strona "
        "odpowiada 200. Kopia jest w dzienniku — cofniesz to przez `/cofnij`.\n\n"
        "Docelowo raport warto generować w tle (`shop-worker`), zamiast trzymać żądanie HTTP.\n\n"
        "Źródła: nginx.org — ngx_http_proxy_module (`proxy_read_timeout`), serverfault.com/q/1156287",
        f"Fixed — **{DOMAIN}** responds again.\n\n"
        "**Cause:** generating the PDF report in `shop-app` takes about 75 s, while nginx waited only 60 s for the "
        "response (`proxy_read_timeout`) and returned 502 (`upstream timed out (110)` in `error.log`).\n\n"
        f"**Change:** `proxy_read_timeout 120s` in `{PROJECT}.conf`. `nginx -t` passed, nginx reloaded, the site answers "
        "200. The backup is in the journal — undo it with `/undo`.\n\n"
        "Long term the report should be generated in the background (`shop-worker`) instead of holding the HTTP request.\n\n"
        "Sources: nginx.org — ngx_http_proxy_module (`proxy_read_timeout`), serverfault.com/q/1156287"), 0.3)


def journal_changes() -> dict:
    diff = "\n".join([
        "@@ -14,7 +14,7 @@",
        "     location / {",
        "         proxy_pass http://127.0.0.1:8080;",
        "         proxy_set_header Host $host;",
        "-        proxy_read_timeout 60s;",
        "+        proxy_read_timeout 120s;",
        "         proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "     }",
    ])
    return {"id": ENTRY, "when": "2026-10-04 14:32", "tool": "execute_command", "command": COMMAND, "status": "done",
            "summary": COMMAND, "undoable": True, "files": [{"path": CONF, "status": "modified", "diff": diff, "note": ""}],
            "inverse": [], "notes": [], "verify": L("nginx -t: ok; usługa nginx aktywna; strony odpowiadają",
                                                    "nginx -t: ok; nginx service active; sites respond")}


class Demo:
    def __init__(self, full: bool = False) -> None:
        self.fixed = False
        self.full = full          # --full: przykladowe skille, alerty, skrzynka, projekty i dziennik (do testow interfejsu)
        self.seen = False
        self.done_actions: dict[str, str] = {}
        self.prefs = {"level": "important", "quiet": "", "per_day": 3}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            request = json.loads(line or b"{}")
            await self.route(request, writer)
        except (ConnectionError, json.JSONDecodeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def route(self, request: dict, writer) -> None:
        from backend.core import graph
        lang = "en" if L("pl", "en") == "en" else "pl"
        command = request.get("command")
        if "message" in request:
            await investigate(writer)
            await done(writer)
        elif "confirm" in request:
            if request.get("confirm"):
                self.fixed = True
                await apply_fix(writer)
            await done(writer)
        elif command == "subscribe":
            await send(writer, {"response": "", "status": "ok", "done": False,
                                "event": {"type": "subscribed", "lang": lang, "lang_chosen": True}})
            await asyncio.sleep(3600)
        elif command == "graph":
            await done(writer, graph.build(infra()))
        elif command == "providers":
            await done(writer, {"providers": [], "chosen": True, "can_edit": True,
                                "active": {"id": "gemini", "name": "Google Gemini", "model": "gemini-3.8-flash"}})
        elif command == "yolo":
            await done(writer, {"yolo": False, "can_edit": True, "text": ""})
        elif command == "journal":
            entries = [{"id": ENTRY, "summary": f"2026-10-04 14:32 [done] execute_command: {COMMAND}", "undoable": True,
                        "status": "done"}] if self.fixed or self.full else []
            if self.full:
                entries += [{"id": f"{i:08x}", "summary": f"2026-10-0{i} 09:1{i} [done] write_file: /srv/{PROJECT}/.env.example",
                             "undoable": True, "status": "done"} for i in range(1, 6)]
            await done(writer, {"entries": entries, "text": ""})
        elif command == "journal_changes":
            await done(writer, journal_changes())
        elif command == "alerts":
            active = [{"id": "disk:/", "key": "disk:/", "severity": "warning", "title": L("Dysk / zapelniony w 91%", "Disk / is 91% full"),
                       "detail": L("Zostalo 4,1 GB.", "4.1 GB left.")}] if self.full else []
            recent = [{"type": "routine", "name": "poranny-przeglad", "status": "ok", "at": f"2026-10-0{i} 07:00",
                       "report": L("Backupy swieze, certyfikaty wazne.", "Backups fresh, certificates valid.")}
                      for i in range(1, 5)] if self.full else []
            await done(writer, {"active": active, "recent": recent})
        elif command == "list_skills":
            skills = [{"name": name, "command": name, "description": L(f"Procedura {name}.", f"The {name} procedure.")}
                      for name in ("swap", "fail2ban-ssh", "certbot-renew", "docker-cleanup", "postgres-backup")]
            await done(writer, {"skills": skills if self.full else []})
        elif command in ("overview", "status"):
            data = overview_data(self.full)
            if command == "status":
                await send(writer, {"response": data["text"], "status": "ok", "done": False})
            await done(writer, data)
        elif command == "feed":
            if str(request.get("args", "")) == "seen":
                self.seen = True
            await done(writer, feed_data(self.full, self.seen, self.done_actions))
        elif command == "feed_act":
            item_id, index = str(request.get("id", "")), int(request.get("action") or 0)
            item = next((i for i in feed_data(self.full, self.seen, {})["items"] if i["id"] == item_id), None)
            action = item["actions"][index] if item and index < len(item["actions"]) else {"label": "?", "kind": "dismiss"}
            self.done_actions[item_id] = action["label"]
            if action["kind"] in ("snooze", "mute", "dismiss"):
                await done(writer, {"id": item_id, "done": action["label"], "text": L("Odlozone o dobe.", "Snoozed for a day.")})
            else:
                from backend.core.events import Progress
                await send(writer, frame(Progress(L("sprzatam: obrazy bez tagu", "cleaning: untagged images"), source="sprzatanie")))
                await asyncio.sleep(1.2)
                await done(writer, {"freed": 6.4e9, "text": L("Zwolnione miejsce: 6.0 GB.\nZrobione: obrazy bez tagu; cache budowania.",
                                                               "Space freed: 6.0 GB.\nDone: untagged images; build cache.")})
        elif command == "prefs":
            for key in ("level", "quiet", "per_day"):
                if request.get(key) not in (None, ""):
                    self.prefs[key] = request[key] if key != "per_day" else int(request[key])
                    if key == "quiet" and request[key] == "off":
                        self.prefs["quiet"] = ""
            await done(writer, {**self.prefs, "muted": [], "can_edit": True, "text": ""})
        elif command == "projects":
            await done(writer, {"projects": overview_data(self.full)["projects"], "workspace": "/root/pipe-workspace",
                                "text": L("Warsztat /root/pipe-workspace:\n- notatnik [node] dziala — http://127.0.0.1:18000",
                                          "Workshop /root/pipe-workspace:\n- notebook [node] running — http://127.0.0.1:18000"),
                                "can_edit": True})
        else:
            await done(writer, {})


def overview_data(full: bool) -> dict:
    """Pulpit demo: realistyczne liczby, przebieg doby z lagodna fala (bez losowosci — nagranie jest powtarzalne)."""
    import math
    from backend.core import overview

    cpu = [round(18 + 9 * math.sin(i / 5) + (14 if 30 < i < 34 else 0), 1) for i in range(48)]
    mem = [round(52 + i * 0.12 + 3 * math.sin(i / 7), 1) for i in range(48)]
    disk = [round(71 + i * 0.4, 1) for i in range(48)]
    data = {
        "hostname": HOST, "status": "warning" if full else "ok",
        "headline": L("Do sprawdzenia: dysk / zapelni sie za ok. 3 dni", "Worth a look: disk / will be full in about 3 days")
        if full else L("Wszystko dziala", "Everything works"),
        "facts": [L("dziala od 23 dni", "up for 23 days"), L("kontenery: 5 z 5 dziala", "containers: 5 of 5 running"),
                  L("strony: 2/2 odpowiada", "sites: 2/2 responding")],
        "gauges": [
            {"id": "cpu", "label": L("Procesor", "CPU"), "pct": 23, "level": "ok", "value": L("obciazenie 0,9 / 4 rdz.", "load 0.9 / 4 cores")},
            {"id": "mem", "label": L("Pamiec", "Memory"), "pct": 58, "level": "ok", "value": L("4,6 z 7,8 GB", "4.6 of 7.8 GB")},
            {"id": "disk:/", "label": L("Dysk /", "Disk /"), "pct": 89 if full else 64, "level": "warn" if full else "ok",
             "value": L("wolne 8,4 GB", "8.4 GB free")},
        ],
        "series": {"cpu": cpu, "mem": mem, "disk": disk},
        "alerts": [], "containers": {"running": 5, "total": 5}, "sites": {"ok": 2, "total": 2},
        "projects": [{"name": L("notatnik", "notebook"), "stack": ["node"], "url": "http://127.0.0.1:18000", "status": "running",
                      "strategy": "node", "source": "https://github.com/acme/notebook"}] if full else [],
        "unread": 3 if full else 0,
        "usage": {"tokens": 41200, "cost": 0.031, "cached": 21000, "prompt": 33800, "saved": 118400, "calls": 14},
    }
    data["text"] = overview.text(data)
    return data


def feed_data(full: bool, seen: bool, done_actions: dict) -> dict:
    """Skrzynka demo: podpowiedz z akcja, ustalenia z badania alertu, rozwiazany problem i raport poranny."""
    if not full:
        return {"items": [], "unread": 0, "text": "", "prefs": {"level": "important", "quiet": "", "per_day": 3, "muted": []}}
    items = [
        {"id": "f1", "type": "insight", "severity": "warning", "at": "2026-10-25 09:12", "title":
         L("Dysk / zapelni sie za ok. 3 dni", "Disk / will be full in about 3 days"),
         "text": L("Teraz 89%, przybywa 2,1 GB na dobe. Przy tym tempie miejsce skonczy sie za ok. 3 dni — wtedy przestana "
                   "dzialac bazy danych, logi i wdrozenia. Moge od razu usunac to, co odtwarzalne (stare obrazy Dockera, "
                   "cache budowania, pobrane paczki), albo sprawdzic, co tak rosnie.",
                   "Now 89%, 2.1 GB more every day. At this rate the space runs out in about 3 days — then databases, "
                   "logs and deployments stop working. I can remove what is reproducible right away (old Docker images, "
                   "build cache, downloaded packages) or check what keeps growing."),
         "actions": [{"label": L("Zwolnij miejsce", "Free up space"), "kind": "command", "command": "cleanup", "admin": True, "primary": True, "index": 0},
                     {"label": L("Co tak rosnie?", "What is growing?"), "kind": "message", "index": 1},
                     {"label": L("Pozniej", "Later"), "kind": "snooze", "index": 2},
                     {"label": L("Mniej takich", "Fewer like this"), "kind": "mute", "index": 3}]},
        {"id": "f2", "type": "investigation", "severity": "critical", "at": "2026-10-25 03:41",
         "title": L("Ustalilem: Kontener shop-worker przestal dzialac", "What I found: Container shop-worker stopped"),
         "text": L("Worker padl o 03:38, bo zabraklo mu pamieci (limit 512 MB, kolejka zamowien urosla po nocnym imporcie). "
                   "Kontener sam wstal po minucie, zamowienia sie nie zgubily.\n\n- OOMKilled o 03:38\n- import produktow o 03:30 "
                   "dodal 12 tys. zadan\n\nProponuje podniesc limit pamieci workera do 1 GB.",
                   "The worker died at 03:38 because it ran out of memory (512 MB limit, the order queue grew after the "
                   "nightly import). The container came back after a minute, no orders were lost.\n\n- OOMKilled at 03:38\n"
                   "- the product import at 03:30 added 12k jobs\n\nI suggest raising the worker memory limit to 1 GB."),
         "actions": [{"label": L("Napraw", "Fix it"), "kind": "message", "admin": True, "primary": True, "index": 0}]},
        {"id": "f3", "type": "alert", "severity": "ok", "at": "2026-10-25 03:39",
         "title": L("Rozwiazane: Kontener shop-worker przestal dzialac", "Resolved: Container shop-worker stopped"),
         "text": "", "actions": []},
        {"id": "f4", "type": "digest", "severity": "info", "at": "2026-10-25 07:00",
         "title": L(f"Raport {HOST} — 25.10.2026 07:00", f"Report for {HOST} — 25.10.2026 07:00"),
         "text": L("**Stan**\n- dziala od 23 dni\n- RAM 58% (4,6 z 7,8 GB)\n\n**Zmiany od wczoraj**\n- pakiety: 3 zmiany (openssl, libssl3, curl)",
                   "**Health**\n- up for 23 days\n- RAM 58% (4.6 of 7.8 GB)\n\n**Changes since yesterday**\n- packages: 3 changes (openssl, libssl3, curl)"),
         "actions": []},
    ]
    for item in items:
        item["unread"] = not seen and item["id"] in ("f1", "f2", "f4")
        if item["id"] in done_actions:
            item["done"] = done_actions[item["id"]]
    return {"items": items, "unread": 0 if seen else 3, "text": "", "prefs": {"level": "important", "quiet": "", "per_day": 3, "muted": []}}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Pipe demo backend (scripted)")
    parser.add_argument("--port", type=int, default=7390)
    parser.add_argument("--lang", choices=["pl", "en"], default="pl")
    parser.add_argument("--full", action="store_true", help="przykladowe skille, alerty i dziennik (testy interfejsu)")
    args = parser.parse_args()
    os.environ["PIPE_LANG"] = args.lang
    configure(args.lang)
    from backend.core import graph
    import backend.server  # noqa: F401 — import trwa kilka sekund; nie w trakcie nagrania
    graph.build(infra())               # graph.locate() potrzebuje ostatniej infrastruktury
    demo = Demo(full=args.full)
    server = await asyncio.start_server(demo.handle, "127.0.0.1", args.port, limit=32 * 1024 * 1024)
    print(f"demo backend on 127.0.0.1:{args.port} ({args.lang})", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
