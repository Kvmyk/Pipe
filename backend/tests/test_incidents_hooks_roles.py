"""
Testy pamieci incydentow, webhookow (alerty z zewnatrz), wiadomosci glosowych,
tokenow z rolami i roli viewer (v0.13).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.core import incidents, journal, voice, watch, webhooks


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "")
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def alert(key="container:web:restarting", severity="critical", title="Kontener web restartuje sie w petli"):
    return watch.Alert("id1", key, severity, title, "obraz web:1", "2026-09-29 10:00")


# ─── Pamiec incydentow ──────────────────────────────────────────────────────

class TestIncidents:

    def test_lifecycle_with_diagnosis_and_fixes(self):
        start = time.time()
        incidents.opened(alert())
        incidents.note_investigation("container:web:restarting", "Przyczyna: brak zmiennej DB_URL po deployu.")
        entry = run(journal.begin("cli", "execute_command", "docker compose up -d web", inverse=[]))
        journal.finish(entry, "done", 0)
        closed = incidents.closed("container:web:restarting", journal.entries(), now=time.time() + 120)
        assert closed.diagnosis.startswith("Przyczyna: brak zmiennej DB_URL")
        assert closed.fixes == [f"docker compose up -d web (#{entry.id})"] and closed.started >= start - 1
        [past] = incidents.history("container:web:restarting")
        assert "pomoglo: docker compose up -d web" in past.short()
        context = incidents.context_for("container:web:restarting")
        assert "juz sie zdarzal" in context and "DB_URL" in context
        assert incidents.context_for("disk:/") == ""

    def test_escalation_keeps_start_and_secrets_redacted(self):
        incidents.opened(alert(severity="warning"))
        first = json.loads(incidents.incidents_path().read_text())["open"]["container:web:restarting"]["started"]
        incidents.opened(alert(severity="critical"))
        data = json.loads(incidents.incidents_path().read_text())["open"]["container:web:restarting"]
        assert data["started"] == first and data["severity"] == "critical"
        incidents.note_investigation("container:web:restarting", "haslo: DB_PASSWORD=supertajne123")
        assert "supertajne123" not in incidents.incidents_path().read_text()

    def test_closing_unknown_is_noop(self):
        assert incidents.closed("nie-ma") is None
        assert "pusta" in incidents.render()

    def test_watcher_attaches_history_to_repeated_alert(self):
        watcher = watch.Watcher()
        finding = watch.Finding("disk:/", "warning", "Dysk / zapelniony w 91%", "x")
        watcher._publish(watcher.apply([finding]))
        incidents.note_investigation("disk:/", "Logi nginx bez rotacji w /var/log/nginx.")
        watcher._publish(watcher.apply([], frozenset({"disk"})))
        [again] = watcher._publish(watcher.apply([finding]))
        assert "Logi nginx bez rotacji" in again.history
        assert again.to_event()["history"] == again.history

    def test_investigate_command_stores_diagnosis(self, monkeypatch):
        import backend.server as server
        from backend.tests.test_server_commands import FakeAgent, exchange

        agent = FakeAgent()
        monkeypatch.setattr(server, "get_agent", lambda: agent)

        async def no_changes(hours=24):
            return ""

        monkeypatch.setattr(server, "_recent_changes_context", no_changes)
        incidents.opened(alert(key="disk:/"))
        watcher = watch.get_watcher()
        [new] = watcher.apply([watch.Finding("disk:/", "warning", "Dysk", "x")])
        try:
            exchange([{"command": "investigate", "id": new.id}])
        finally:
            watcher.active.clear()
        stored = json.loads(incidents.incidents_path().read_text())["open"]["disk:/"]
        assert stored["diagnosis"] == "odpowiedz agenta"


# ─── Webhooki ───────────────────────────────────────────────────────────────

ALERTMANAGER = {"alerts": [
    {"status": "firing", "labels": {"alertname": "HighLatency", "severity": "critical", "instance": "shop:443"},
     "annotations": {"summary": "Wysokie opoznienia sklepu", "description": "p95 > 2 s"}},
    {"status": "resolved", "labels": {"alertname": "DiskFull", "instance": "db"}, "annotations": {}},
]}


class TestParsers:

    def test_alertmanager(self):
        firing, resolved = webhooks.parse("alertmanager", ALERTMANAGER, {})
        assert firing.key == "hook:alertmanager:highlatency-shop-443" and firing.severity == "critical"
        assert firing.title == "Wysokie opoznienia sklepu" and firing.detail == "p95 > 2 s" and firing.firing
        assert not resolved.firing and resolved.title == "DiskFull (db)"

    def test_grafana_legacy_and_unified(self):
        [a] = webhooks.parse("grafana", {"state": "alerting", "ruleName": "CPU", "message": "90%"}, {})
        [b] = webhooks.parse("grafana", {"state": "ok", "ruleName": "CPU"}, {})
        assert a.firing and not b.firing and a.key == b.key == "hook:grafana:cpu"
        assert webhooks.parse("grafana", ALERTMANAGER, {})[0].source == "grafana"

    def test_uptime_kuma(self):
        down = {"heartbeat": {"status": 0, "msg": "timeout"}, "monitor": {"name": "Sklep", "url": "https://s.pl"}}
        [a] = webhooks.parse("uptime-kuma", down, {})
        assert a.firing and a.severity == "critical" and "timeout" in a.detail
        [b] = webhooks.parse("uptime-kuma", {**down, "heartbeat": {"status": 1}}, {})
        assert not b.firing
        assert webhooks.parse("uptime-kuma", {"heartbeat": None, "msg": "test"}, {}) == []

    def test_github(self):
        run_payload = {"action": "completed", "repository": {"full_name": "acme/shop"},
                       "workflow_run": {"name": "CI", "conclusion": "failure", "head_branch": "main", "html_url": "u"}}
        [a] = webhooks.parse("github", run_payload, {"x-github-event": "workflow_run"})
        assert a.firing and "nie przeszedl" in a.title
        run_payload["workflow_run"]["conclusion"] = "success"
        [b] = webhooks.parse("github", run_payload, {"x-github-event": "workflow_run"})
        assert not b.firing and a.key == b.key
        deploy = {"repository": {"full_name": "acme/shop"}, "deployment": {"environment": "prod"},
                  "deployment_status": {"state": "failure", "description": "healthcheck"}}
        [c] = webhooks.parse("github", deploy, {"x-github-event": "deployment_status"})
        assert c.severity == "critical" and c.detail == "healthcheck"

    def test_generic_and_errors(self):
        [a] = webhooks.parse("generic", {"title": "Backup", "message": "nie wykonal sie", "severity": "high"}, {})
        assert a.severity == "critical" and a.firing
        with pytest.raises(ValueError):
            webhooks.parse("generic", [1, 2], {})

    def test_auth(self):
        body = b'{"a": 1}'
        signature = "sha256=" + hmac.new(b"sekret", body, hashlib.sha256).hexdigest()
        assert webhooks.authorized("sekret", {"authorization": "Bearer sekret"}, {}, body)
        assert webhooks.authorized("sekret", {}, {"token": ["sekret"]}, body)
        assert webhooks.authorized("sekret", {"x-hub-signature-256": signature}, {}, body)
        assert not webhooks.authorized("sekret", {"x-hub-signature-256": signature}, {}, b"{}")
        assert not webhooks.authorized("sekret", {"authorization": "Bearer zly"}, {}, body)
        assert not webhooks.authorized("", {"authorization": "Bearer "}, {}, body)


async def http(port, method, path, body=b"", headers=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
    for name, value in (headers or {}).items():
        head += f"{name}: {value}\r\n"
    writer.write(head.encode() + b"\r\n" + body)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    status = int(raw.split(b" ", 2)[1])
    return status, json.loads(raw.split(b"\r\n\r\n", 1)[1] or b"{}")


class TestWebhookServer:

    def test_end_to_end(self, monkeypatch):
        investigated = []

        async def scenario():
            watcher = watch.Watcher()

            async def fake_investigate(alert):
                investigated.append(alert.key)

            watcher.investigate_external = fake_investigate
            queue = watcher.notifier.subscribe()
            server = await webhooks.start(watcher, "127.0.0.1", 0, "", True)
            assert server is None                                      # bez portu — wylaczone
            server = await asyncio.start_server(webhooks.WebhookServer(watcher, "tok").handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            auth = {"Authorization": "Bearer tok"}
            results = [
                await http(port, "GET", "/health"),
                await http(port, "POST", "/hook/alertmanager", json.dumps(ALERTMANAGER).encode()),
                await http(port, "POST", "/hook/alertmanager", json.dumps(ALERTMANAGER).encode(), auth),
                await http(port, "POST", "/hook/nieznane", b"{}", auth),
                await http(port, "GET", "/hook/generic", b"", auth),
                await http(port, "POST", "/hook/generic", b"{zly", auth),
                await http(port, "POST", "/hook/github", b"{}", {**auth, "X-GitHub-Event": "ping"}),
            ]
            await asyncio.sleep(0)
            active = dict(watcher.active)
            resolved = {"alerts": [{**ALERTMANAGER["alerts"][0], "status": "resolved"}]}
            await http(port, "POST", "/hook/alertmanager?token=tok", json.dumps(resolved).encode())
            events = [queue.get_nowait() for _ in range(queue.qsize())]
            server.close()
            await server.wait_closed()
            return results, active, events

        results, active, events = run(scenario())
        assert [status for status, _ in results] == [200, 401, 202, 404, 405, 400, 200]
        assert list(active) == ["hook:alertmanager:highlatency-shop-443"]
        assert [e["state"] for e in events] == ["new", "resolved"]
        assert investigated == ["hook:alertmanager:highlatency-shop-443"]

    def test_body_limit(self, monkeypatch):
        monkeypatch.setattr(webhooks, "MAX_BODY", 10)

        async def scenario():
            server = await asyncio.start_server(webhooks.WebhookServer(watch.Watcher(), "tok").handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            result = await http(port, "POST", "/hook/generic", b"x" * 50, {"Authorization": "Bearer tok"})
            server.close()
            return result

        assert run(scenario())[0] == 413

    def test_investigation_rate_limit(self):
        server = webhooks.WebhookServer(watch.Watcher(), "tok")
        assert server._may_investigate("a", now=1000)
        assert not server._may_investigate("a", now=2000)          # ten sam alert w ciagu godziny
        assert server._may_investigate("a", now=5000)
        for i in range(8):
            server._may_investigate(f"k{i}", now=5000)
        assert not server._may_investigate("nowy", now=5000)        # limit dzienny

    def test_investigate_external_publishes_report(self, monkeypatch):
        from backend.core import workers

        async def fake_worker(agent, parent, name, target, task, progress=None, **kwargs):
            assert "Wysokie opoznienia" in task and target.kind == "local"
            return workers.WorkerResult(name, "local", report="USTALENIA: php-fpm ma max_children=5")

        monkeypatch.setattr(workers, "run_worker", fake_worker)
        watcher = watch.Watcher(agent=object())
        queue = watcher.notifier.subscribe()
        incidents.opened(alert(key="hook:alertmanager:x"))
        run(watcher.investigate_external(watch.Alert("1", "hook:alertmanager:x", "critical",
                                                     "Wysokie opoznienia", "p95", "t")))
        event = queue.get_nowait()
        assert event["type"] == "investigation" and "max_children" in event["report"]
        assert "max_children" in json.loads(incidents.incidents_path().read_text())["open"]["hook:alertmanager:x"]["diagnosis"]


# ─── Glos ───────────────────────────────────────────────────────────────────

class TestVoice:

    def test_resolve(self):
        llm = SimpleNamespace(provider_id="groq", base_url="https://api.groq.com/openai/v1", api_key="k")
        assert voice.resolve({}, llm).model == "whisper-large-v3-turbo"
        gemini = voice.resolve({}, SimpleNamespace(provider_id="gemini", base_url="x", api_key="k", model="gemini-flash"))
        assert (gemini.mode, gemini.model) == ("chat", "gemini-flash")
        assert voice.resolve({}, SimpleNamespace(provider_id="mistral", base_url="x", api_key="k", model="m")) is None
        custom = voice.resolve({"STT_BASE_URL": "http://stt:8000/v1", "STT_MODEL": "large-v3", "STT_LANGUAGE": "en"},
                               None)
        assert (custom.base_url, custom.model, custom.language, custom.api_key) == \
            ("http://stt:8000/v1", "large-v3", "en", "brak-klucza")

    def test_transcribe(self, monkeypatch):
        import openai

        calls = {}

        class FakeTranscriptions:
            async def create(self, **kwargs):
                calls.update(kwargs)
                return SimpleNamespace(text=" sprawdz dyski ")

        class FakeClient:
            def __init__(self, **kwargs):
                calls["client"] = kwargs
                self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

        monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
        config = voice.SttConfig("https://api.groq.com/openai/v1", "k", "whisper-large-v3-turbo", "pl")
        assert run(voice.transcribe(b"OggS...", "voice.ogg", config)) == "sprawdz dyski"
        assert calls["file"] == ("voice.ogg", b"OggS...") and calls["language"] == "pl"
        with pytest.raises(voice.TranscriptionError, match="STT_BASE_URL"):
            run(voice.transcribe(b"x", "v.ogg", None))
        with pytest.raises(voice.TranscriptionError, match="za dlugie"):
            run(voice.transcribe(b"x" * (voice.MAX_AUDIO_BYTES + 1), "v.ogg", config))

    def test_server_command(self, monkeypatch):
        import backend.server as server
        from backend.tests.test_server_commands import FakeAgent, exchange

        monkeypatch.setattr(server, "get_agent", lambda: FakeAgent())

        async def fake(data, filename, config, **kwargs):
            assert data == b"nagranie" and filename == "voice.ogg"
            return "ile mam miejsca"

        monkeypatch.setattr(voice, "transcribe", fake)
        [[ok], [bad]] = exchange([
            {"command": "transcribe", "audio": base64.b64encode(b"nagranie").decode(), "filename": "voice.ogg"},
            {"command": "transcribe", "audio": "!!nie-base64!!"}])
        assert ok["data"]["text"] == "ile mam miejsca" and bad["status"] == "error"


# ─── Tokeny i role ──────────────────────────────────────────────────────────

class TestTokens:

    def test_add_match_revoke(self, capsys):
        from backend import tokens

        token = tokens.add("laptop", "viewer")
        assert tokens.match(token) == ("laptop", "viewer") and tokens.match("zly") is None
        assert oct(tokens.tokens_path().stat().st_mode & 0o777) == "0o600"
        assert token not in tokens.tokens_path().read_text()               # tylko skrot
        assert tokens.main(["list"]) == 0 and "laptop" in capsys.readouterr().out
        assert tokens.revoke("laptop") and tokens.match(token) is None
        with pytest.raises(ValueError):
            tokens.add("Zla Nazwa", "admin")

    def test_authorize_matrix(self, monkeypatch):
        from backend import server, tokens

        assert server._authorize({}) == ("open", "admin")
        monkeypatch.setattr(settings, "AGENT_TOKEN", "admintok")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "viewtok")
        personal = tokens.add("kuba", "admin")
        assert server._authorize({"token": "admintok"}) == ("admin", "admin")
        assert server._authorize({"token": "viewtok"}) == ("viewer", "viewer")
        assert server._authorize({"token": personal}) == ("token:kuba", "admin")
        assert server._authorize({"token": "zly"}) is None
        assert server._authorize({"token": "zażółć"}) is None               # bez wyjatku dla nie-ASCII
        assert server._authorize({}) is None

    def test_tokens_file_alone_closes_open_access(self):
        from backend import server, tokens

        tokens.add("kuba", "admin")
        assert server._authorize({}) is None

    def test_settings_validate_viewer_needs_admin(self, monkeypatch):
        from dataclasses import replace

        if settings.LLM is None:
            pytest.skip("brak konfiguracji LLM w srodowisku testow")
        monkeypatch.setattr(settings, "LLM", replace(settings.LLM, api_key="k"))
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "v")
        with pytest.raises(ValueError, match="wymaga AGENT_TOKEN"):
            settings.validate()


class TestViewerRole:

    def _agent(self, script):
        from backend.core.agent import VPSAgent
        from backend.tests.fakes import FakeClient

        return VPSAgent(client=FakeClient(script))

    def test_change_turns_into_refusal(self, monkeypatch):
        from backend.tests.fakes import assert_history_valid, completion

        monkeypatch.setattr(settings, "VIBE_EVERY", 0)
        monkeypatch.setenv("PIPE_RUNTIME", "native")
        agent = self._agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "systemctl restart nginx"}),
                                   ("c2", "write_file", {"path": "/tmp/x", "content": "y"}),
                                   ("c3", "server_md", {"operation": "update_section", "section": "A", "content": "b"}),
                                   ("c4", "execute_command", {"command": "echo hej"})]),
            completion("opisalem, co trzeba zrobic")])

        async def talk():
            return [e async for e in agent.chat("s", "zrestartuj nginx", "telegram:5", owner="viewer", role="viewer")]

        events = run(talk())
        session = agent._sessions["s"]
        assert session.pending_confirmation is None
        assert not any("[POTWIERDZ]" in e for e in events if isinstance(e, str))
        assert sum(1 for e in events if isinstance(e, str) and e.startswith("[ODMOWA]")) == 3
        results = {m["tool_call_id"]: m["content"] for m in session.messages if m.get("role") == "tool"}
        assert all("role viewer" in results[c] for c in ("c1", "c2", "c3")) and "hej" in results["c4"]
        assert "ROLA UZYTKOWNIKA" in agent._client.calls[0]["messages"][0]["content"]
        assert_history_valid(session.messages)

    def test_server_refuses_viewer_confirm_and_undo_and_foreign_session(self, monkeypatch):
        import backend.server as server
        from backend.tests.test_server_commands import exchange

        monkeypatch.setattr(settings, "AGENT_TOKEN", "admintok")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "viewtok")
        agent = self._agent([])
        agent.get_or_create_session("s1", "cli", owner="admin")
        monkeypatch.setattr(server, "get_agent", lambda: agent)
        [[foreign], [confirm], [undo]] = exchange([
            {"message": "pokaz historie", "token": "viewtok"},
            {"confirm": True, "session_id": "inna", "token": "viewtok"},
            {"command": "undo", "id": "abcd1234", "execute": True, "session_id": "inna", "token": "viewtok"},
        ])
        assert "innego klienta" in foreign["response"]
        assert "viewer" in confirm["response"] and "viewer" in undo["response"]


def test_telegram_formatting():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
    from tg_format import format_alert, format_investigation

    text = format_alert({"severity": "critical", "title": "Dysk", "detail": "x", "history": "2026-09-01 <nginx>"})
    assert "<b>Poprzednio:</b> 2026-09-01 &lt;nginx&gt;" in text
    report = format_investigation({"title": "Sklep <down>", "report": "USTALENIA: <b>x</b>"})
    assert "Sklep &lt;down&gt;" in report and "&lt;b&gt;x&lt;/b&gt;" in report
