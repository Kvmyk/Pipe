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
    await tool(writer, "t1", "docker_manage", {"operation": "logs", "target": "shop-app"},
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
        self.full = full          # --full: przykladowe skille, alerty i wpisy dziennika (do testow interfejsu)

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
        else:
            await done(writer, {})


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
