"""
Alerty z zewnatrz — Pipe dolacza do monitoringu, ktory juz masz.

Maly serwer HTTP (bez zaleznosci) na WEBHOOK_HOST:WEBHOOK_PORT przyjmuje
POST /hook/<zrodlo> z:
  alertmanager  Prometheus Alertmanager, Grafana (unified alerting)
  grafana       starsze powiadomienia Grafany (state, ruleName, message)
  uptime-kuma   Uptime Kuma (heartbeat.status 0/1)
  github        GitHub: nieudany workflow_run albo deployment_status (podpis X-Hub-Signature-256)
  generic       {"title", "message", "severity", "status": "firing|resolved", "name"}

Alert trafia do czuwania (klucz hook:<zrodlo>:<nazwa>, rozwiazanie zamyka go),
a na Telegram z przyciskiem Zbadaj. Z WEBHOOK_INVESTIGATE=1 nowy alert od razu
bada worker (tylko odczyty) i raport przychodzi chwile po alercie — z limitem
(ten sam alert raz na godzine, MAX_INVESTIGATIONS_PER_DAY dziennie).

Uwierzytelnienie: WEBHOOK_TOKEN jest wymagany (bez niego serwer nie wystartuje) —
naglowek `Authorization: Bearer <token>`, parametr `?token=` albo podpis HMAC
GitHuba. Tresc alertu to dane od zewnetrznego systemu: trafia do modelu jako dane,
a badajacy worker wykonuje tylko odczyty.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit
from backend.core.i18n import tr

MAX_BODY = 1024 * 1024
MAX_HEADERS = 100
MAX_INVESTIGATIONS_PER_DAY = 10
INVESTIGATION_COOLDOWN = 3600
SOURCES = ("alertmanager", "grafana", "uptime-kuma", "github", "generic")
_NAME = re.compile(r"[^a-z0-9_.-]+")


@dataclass
class ExternalAlert:
    source: str
    name: str
    title: str
    detail: str = ""
    severity: str = "warning"     # warning | critical
    firing: bool = True

    @property
    def key(self) -> str:
        return f"hook:{self.source}:{_NAME.sub('-', self.name.lower()).strip('-')[:80] or 'alert'}"


def _severity(value: Any) -> str:
    return "critical" if str(value or "").lower() in ("critical", "page", "error", "high", "disaster", "fatal") \
        else "warning"


def _clip(text: Any, limit: int = 500) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] + "..." if len(text) > limit else text


# ─── Parsery ────────────────────────────────────────────────────────────────

def parse_alertmanager(payload: dict[str, Any]) -> list[ExternalAlert]:
    out = []
    for alert in payload.get("alerts") or []:
        if not isinstance(alert, dict):
            continue
        labels = alert.get("labels") or {}
        notes = alert.get("annotations") or {}
        name = labels.get("alertname") or "alert"
        instance = labels.get("instance") or labels.get("job") or ""
        title = notes.get("summary") or f"{name}" + (f" ({instance})" if instance else "")
        out.append(ExternalAlert("alertmanager", f"{name}-{instance}" if instance else name, _clip(title, 200),
                                 _clip(notes.get("description") or notes.get("message") or ""),
                                 _severity(labels.get("severity")), alert.get("status", "firing") != "resolved"))
    return out


def parse_grafana(payload: dict[str, Any]) -> list[ExternalAlert]:
    if payload.get("alerts"):
        return [ExternalAlert("grafana", a.name, a.title, a.detail, a.severity, a.firing)
                for a in parse_alertmanager(payload)]
    name = payload.get("ruleName") or payload.get("title") or "grafana"
    state = str(payload.get("state", "alerting")).lower()
    return [ExternalAlert("grafana", name, _clip(payload.get("title") or name, 200), _clip(payload.get("message")),
                          "warning", state not in ("ok", "paused", "pending"))]


def parse_uptime_kuma(payload: dict[str, Any]) -> list[ExternalAlert]:
    beat = payload.get("heartbeat")
    monitor = payload.get("monitor") or {}
    if not isinstance(beat, dict):
        return []                                  # wiadomosc testowa z Uptime Kuma
    name = monitor.get("name") or "monitor"
    down = beat.get("status") == 0
    detail = _clip(beat.get("msg") or payload.get("msg") or "")
    if monitor.get("url"):
        detail = f"{monitor['url']} — {detail}" if detail else monitor["url"]
    return [ExternalAlert("uptime-kuma", name, f"{name}: {tr('nie dziala', 'is down') if down else tr('dziala', 'is up')}", detail,
                          "critical", down)]


def parse_github(payload: dict[str, Any], event: str) -> list[ExternalAlert]:
    repo = (payload.get("repository") or {}).get("full_name", "repo")
    if event == "workflow_run":
        run = payload.get("workflow_run") or {}
        if payload.get("action") != "completed":
            return []
        conclusion = run.get("conclusion")
        name = f"{repo}-{run.get('name', 'workflow')}-{run.get('head_branch', '')}"
        if conclusion == "success":
            return [ExternalAlert("github", name, tr(f"{repo}: {run.get('name')} przeszedl", f"{repo}: {run.get('name')} passed"),
                                  firing=False)]
        if conclusion in ("failure", "timed_out", "startup_failure"):
            return [ExternalAlert("github", name,
                                  tr(f"{repo}: workflow {run.get('name')} nie przeszedl ({run.get('head_branch')})",
                                     f"{repo}: workflow {run.get('name')} failed ({run.get('head_branch')})"),
                                  _clip(run.get("html_url")))]
        return []
    if event == "deployment_status":
        status = payload.get("deployment_status") or {}
        environment = (payload.get("deployment") or {}).get("environment", "")
        name = f"{repo}-deploy-{environment}"
        state = status.get("state")
        if state == "success":
            return [ExternalAlert("github", name, tr(f"{repo}: wdrozenie {environment} udane",
                                                             f"{repo}: deployment to {environment} succeeded"), firing=False)]
        if state in ("failure", "error"):
            return [ExternalAlert("github", name, tr(f"{repo}: wdrozenie {environment} nieudane",
                                                             f"{repo}: deployment to {environment} failed"),
                                  _clip(status.get("description") or status.get("target_url")), "critical")]
    return []


def parse_generic(payload: dict[str, Any]) -> list[ExternalAlert]:
    title = payload.get("title") or payload.get("name") or "alert"
    return [ExternalAlert("generic", payload.get("name") or title, _clip(title, 200),
                          _clip(payload.get("message") or payload.get("detail")), _severity(payload.get("severity")),
                          str(payload.get("status", "firing")).lower() not in ("resolved", "ok", "up"))]


def parse(source: str, payload: Any, headers: dict[str, str]) -> list[ExternalAlert]:
    if not isinstance(payload, dict):
        raise ValueError(tr("oczekiwano obiektu JSON", "expected a JSON object"))
    if source == "alertmanager":
        return parse_alertmanager(payload)
    if source == "grafana":
        return parse_grafana(payload)
    if source == "uptime-kuma":
        return parse_uptime_kuma(payload)
    if source == "github":
        return parse_github(payload, headers.get("x-github-event", ""))
    return parse_generic(payload)


# ─── Uwierzytelnienie ───────────────────────────────────────────────────────

def authorized(token: str, headers: dict[str, str], query: dict[str, list[str]], body: bytes) -> bool:
    if not token:
        return False
    bearer = headers.get("authorization", "")
    if bearer.lower().startswith("bearer ") and hmac.compare_digest(bearer[7:].strip(), token):
        return True
    if hmac.compare_digest((query.get("token") or [""])[0], token):
        return True
    signature = headers.get("x-hub-signature-256", "")
    if signature.startswith("sha256="):
        expected = "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    return False


# ─── HTTP ───────────────────────────────────────────────────────────────────

class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").strip()
    parts = line.split()
    if len(parts) != 3:
        raise HttpError(400, tr("zly wiersz zadania", "bad request line"))
    method, target, _version = parts
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADERS + 1):
        raw = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
        if raw in ("\r\n", "\n", ""):
            break
        name, _, value = raw.partition(":")
        headers[name.strip().lower()] = value.strip()
    else:
        raise HttpError(431, tr("za duzo naglowkow", "too many headers"))
    length = int(headers.get("content-length", "0") or 0)
    if length > MAX_BODY:
        raise HttpError(413, tr("za duze zadanie", "request too large"))
    body = await asyncio.wait_for(reader.readexactly(length), 10) if length else b""
    return method, target, headers, body


async def respond(writer: asyncio.StreamWriter, status: int, data: dict[str, Any]) -> None:
    reasons = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
               405: "Method Not Allowed", 413: "Payload Too Large", 431: "Request Header Fields Too Large",
               500: "Internal Server Error"}
    body = json.dumps(data, ensure_ascii=False).encode()
    writer.write(f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()


class WebhookServer:
    def __init__(self, watcher: Any, token: str, *, investigate: bool = True) -> None:
        self.watcher = watcher
        self.token = token
        self.investigate = investigate
        self._last_investigation: dict[str, float] = {}
        self._day: tuple[str, int] = ("", 0)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                method, target, headers, body = await read_request(reader)
                status, data = await self.dispatch(method, target, headers, body)
            except HttpError as exc:
                status, data = exc.status, {"ok": False, "error": str(exc)}
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
                status, data = 400, {"ok": False, "error": tr("niepelne albo nieprawidlowe zadanie", "incomplete or invalid request")}
            await respond(writer, status, data)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def dispatch(self, method: str, target: str, headers: dict[str, str], body: bytes) -> tuple[int, dict]:
        url = urlsplit(target)
        if url.path == "/health":
            return 200, {"ok": True}
        match = re.fullmatch(r"/hook/([a-z-]+)/?", url.path)
        if not match or match.group(1) not in SOURCES:
            raise HttpError(404, tr(f"nieznany adres — uzyj /hook/<{'|'.join(SOURCES)}>",
                                    f"unknown path — use /hook/<{'|'.join(SOURCES)}>"))
        if method != "POST":
            raise HttpError(405, tr("tylko POST", "POST only"))
        if not authorized(self.token, headers, parse_qs(url.query), body):
            raise HttpError(401, tr("brak albo zly token", "missing or wrong token"))
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HttpError(400, tr("tresc nie jest JSON-em", "the body is not JSON")) from None
        if match.group(1) == "github" and headers.get("x-github-event") == "ping":
            return 200, {"ok": True, "pong": True}
        try:
            alerts = parse(match.group(1), payload, headers)
        except ValueError as exc:
            raise HttpError(400, str(exc)) from None
        events = self.watcher.external(alerts)
        for event in events:
            if event.state == "new" and self.investigate and self._may_investigate(event.key):
                asyncio.create_task(self.watcher.investigate_external(event))
        return 202, {"ok": True, "alerts": len(alerts), "events": len(events)}

    def _may_investigate(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        day, count = self._day if self._day[0] == today else (today, 0)
        last = self._last_investigation.get(key)
        if count >= MAX_INVESTIGATIONS_PER_DAY or (last is not None and now - last < INVESTIGATION_COOLDOWN):
            return False
        self._day = (day, count + 1)
        self._last_investigation[key] = now
        return True


async def start(watcher: Any, host: str, port: int, token: str, investigate: bool) -> asyncio.AbstractServer | None:
    if not port:
        return None
    if not token:
        print(tr("[Webhooki] WEBHOOK_PORT jest ustawiony, ale WEBHOOK_TOKEN pusty — serwer webhookow NIE wystartowal.",
                 "[Webhooks] WEBHOOK_PORT is set but WEBHOOK_TOKEN is empty — the webhook server did NOT start."),
              flush=True)
        return None
    server = WebhookServer(watcher, token, investigate=investigate)
    return await asyncio.start_server(server.handle, host=host, port=port, limit=MAX_BODY + 64 * 1024)
