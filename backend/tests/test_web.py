"""
Interfejs webowy (`pipe web`): schemat jako dane, zdarzenia aktywnosci agenta, roznice z dziennika
i lokalny most HTTP (clients/webui/server.py).
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import graph, journal
from backend.core.agent import VPSAgent
from backend.core.events import Activity
from backend.core.infra import Container, Infra, PortMap, Route
from backend.server import event_frame
from backend.tests.fakes import FakeClient, assert_history_valid, completion

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "webui"))
import server as web  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(settings, "WATCH_SITES", False)
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"), raising=False)
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(graph, "_latest", None)


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


def sample_infra(targets=()):
    return Infra(
        hostname="vps1", os_name="Ubuntu 24.04",
        containers=[
            Container("shop-web-1", "shop:1.2", "running", project="shop", service="web", workdir="/srv/shop",
                      ports=[PortMap("127.0.0.1", 3000, 3000)]),
            Container("shop-db-1", "postgres:16", "running", project="shop", service="db", workdir="/srv/shop"),
            Container("shop-init-1", "busybox", "exited", project="shop", workdir="/srv/shop"),
            Container("redis", "redis:7", "running", ports=[PortMap("0.0.0.0", 6379, 6379)]),
        ],
        routes=[Route("nginx", ["shop.example.com"], "127.0.0.1:3000")],
        services=["nginx", "ssh"], targets=list(targets),
    )


class TestGraph:

    def test_three_levels_and_index(self):
        data = graph.build(sample_infra([("web-2", "ssh")]))
        assert data["root"] == "fleet" and set(data["views"]) == {"fleet", "host", "p:shop"}
        assert [n["id"] for n in data["views"]["fleet"]["nodes"]] == ["host", "t:web-2"]
        host_ids = {n["id"] for n in data["views"]["host"]["nodes"]}
        assert {"internet", "p:shop", "c:redis", "proxy:nginx", "systemd"} <= host_ids
        assert "c:shop-web-1" not in host_ids                       # kontenery projektu sa poziom nizej
        project = data["views"]["p:shop"]
        assert {"c:shop-web-1", "c:shop-db-1", "dir:shop"} <= {n["id"] for n in project["nodes"]}
        assert data["index"]["c:shop-web-1"] == ["host", "p:shop", "c:shop-web-1"]
        assert data["index"]["c:redis"] == ["host", "c:redis"] and data["index"]["t:web-2"] == ["t:web-2"]

    def test_edges_follow_the_proxy_route_and_public_ports(self):
        edges = {(e["from"], e["to"]): e for e in graph.build(sample_infra())["views"]["host"]["edges"]}
        assert edges[("internet", "proxy:nginx")]["kind"] == "public"
        assert edges[("proxy:nginx", "p:shop")]["label"] == "shop.example.com"
        assert ("internet", "c:redis") in edges                      # publiczny port bez proxy
        project = {(e["from"], e["to"]) for e in graph.build(sample_infra())["views"]["p:shop"]["edges"]}
        assert ("c:shop-web-1", "c:shop-db-1") in project            # aplikacja -> baza

    def test_root_is_host_without_targets_and_one_stopped_container_is_a_warning(self):
        data = graph.build(sample_infra())
        assert data["root"] == "host"
        project = next(n for n in data["views"]["host"]["nodes"] if n["id"] == "p:shop")
        assert project["state"] == "warn" and project["opens"] == "p:shop"

    def test_alerts_mark_nodes(self):
        data = graph.build(sample_infra(), [{"key": "container:shop-db-1", "severity": "critical", "title": "db pada"}])
        db = next(n for n in data["views"]["p:shop"]["nodes"] if n["id"] == "c:shop-db-1")
        assert db["alerts"][0]["title"] == "db pada"
        assert next(n for n in data["views"]["host"]["nodes"] if n["id"] == "p:shop").get("alerts")

    def test_no_docker(self):
        data = graph.build(Infra(hostname="h", containers=None, notes=["brak Dockera"]))
        assert data["root"] == "host" and data["notes"] == ["brak Dockera"]

    def test_locate_maps_actions_to_nodes(self):
        graph.remember(sample_infra())
        assert graph.locate("execute_command", {"command": "docker restart shop-web-1"}) == ["c:shop-web-1"]
        assert graph.locate("docker_manage", {"operation": "logs", "target": "redis"}) == ["c:redis"]
        assert graph.locate("execute_command", {"command": "systemctl reload nginx"}) == ["proxy:nginx"]
        assert graph.locate("write_file", {"path": "/etc/nginx/sites-available/shop"}) == ["proxy:nginx"]
        assert graph.locate("read_file", {"path": "/srv/shop/docker-compose.yml"}) == ["p:shop"]
        assert graph.locate("execute_command", {"command": "docker compose up -d"}, "/srv/shop") == ["p:shop"]
        assert graph.locate("git_command", {"command": "pull"}, "/srv/shop") == ["p:shop"]
        assert graph.locate("remote_exec", {"target": "web-2", "command": "df -h"}) == ["t:web-2"]
        assert graph.locate("delegate", {"tasks": [{"target": "a", "task": "x"}, {"target": "b", "task": "y"}]}) == ["t:a", "t:b"]
        assert graph.locate("system_stats", {}) == ["host"]
        assert graph.locate("execute_command", {"command": "echo 'niezamkniety"}) == ["host"]

    def test_describe_is_short_and_has_no_file_content(self):
        text = graph.describe("write_file", {"path": "/srv/shop/very/long/path/that/goes/on/and/on/config.yml", "content": "SECRET=1"})
        assert text == "zapisuje …/on/config.yml" and "SECRET" not in text
        assert graph.describe("execute_command", {"command": "docker   ps\n-a"}) == "docker ps -a"
        assert graph.describe("mcp__github__list_issues", {}) == "MCP github: list_issues"


class TestActivityEvents:

    def test_web_session_gets_start_and_end(self):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "execute_command", {"command": "echo pipe"})]), completion("Gotowe")]))
        events = run(agent.chat("s", "uruchom", "web:kuba"))
        activity = [e for e in events if isinstance(e, Activity)]
        assert [(a.phase, a.ok) for a in activity] == [("start", None), ("end", True)]
        assert activity[0].nodes == ("host",) and activity[0].label == "echo pipe"
        frame = event_frame(activity[1])
        assert frame["event"]["type"] == "activity" and frame["response"] == "" and frame["event"]["ok"] is True

    def test_other_clients_do_not_get_activity(self):
        for interface in ("cli:kuba", "telegram:1"):
            agent = VPSAgent(client=FakeClient([
                completion(tool_calls=[("c1", "execute_command", {"command": "echo pipe"})]), completion("Gotowe")]))
            assert run(agent.chat("s", "uruchom", interface)) == ["Gotowe"]

    def test_confirmation_goes_wait_then_start_end_with_journal_entry(self, tmp_path):
        target = tmp_path / "host" / "app.conf"
        target.write_text("workers = 4\n")
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "write_file", {"path": "/app.conf", "content": "workers = 8\n"})]),
            completion("Zmienione")]))
        first = [e for e in run(agent.chat("s", "zmien", "web:kuba")) if isinstance(e, Activity)]
        assert [a.phase for a in first] == ["start", "wait"]
        second = [e for e in run(agent.confirm("s", True)) if isinstance(e, Activity)]
        assert [a.phase for a in second] == ["start", "end"] and second[1].ok is True
        assert second[0].id == first[0].id                           # to samo dzialanie na schemacie
        entry = journal.load(second[1].entry)
        assert entry is not None and target.read_text() == "workers = 8\n"
        assert_history_valid(agent._sessions["s"].messages)

    def test_declined_confirmation_ends_without_result(self, tmp_path):
        (tmp_path / "host" / "x").write_text("a")
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "write_file", {"path": "/x", "content": "b"})]), completion("OK")]))
        run(agent.chat("s", "zmien", "web:kuba"))
        ended = [e for e in run(agent.confirm("s", False)) if isinstance(e, Activity)]
        assert [(a.phase, a.ok) for a in ended] == [("end", None)]

    def test_refused_tool_is_marked_failed(self):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "execute_command", {"command": "rm -rf /"})]), completion("Nie moge")]))
        activity = [e for e in run(agent.chat("s", "usun", "web:kuba")) if isinstance(e, Activity)]
        assert activity[-1].phase == "end" and activity[-1].ok is False

    def test_delegate_carries_worker_targets(self):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "delegate", {"tasks": [{"target": "nope", "task": "sprawdz", "name": "w1"}]})]),
            completion("brak celu")]))
        start = next(e for e in run(agent.chat("s", "sprawdz", "web:kuba")) if isinstance(e, Activity))
        assert start.to_wire()["workers"] == {"w1": "nope"} and start.nodes == ("t:nope",)


class TestJournalChanges:

    def test_diff_before_and_after_with_redaction(self, tmp_path):
        target = tmp_path / "host" / "app.env"
        target.write_text("WORKERS=4\nDB_PASSWORD=supersecretvalue\n")

        async def scenario():
            entry = await journal.begin("web:kuba", "write_file", "zapis", files=[(str(target), "/app.env")])
            target.write_text("WORKERS=8\nDB_PASSWORD=supersecretvalue\nCACHE=redis\n")
            journal.finish(entry, "done", 0)
            return journal.changes(journal.load(entry.id))
        data = asyncio.run(scenario())
        (item,) = data["files"]
        assert item["status"] == "modified" and item["path"] == "/app.env"
        assert "-WORKERS=4" in item["diff"] and "+WORKERS=8" in item["diff"] and "+CACHE=redis" in item["diff"]
        assert "supersecretvalue" not in item["diff"]                # sekret nie trafia do przegladarki

    def test_new_and_deleted_files(self, tmp_path):
        new, gone = tmp_path / "host" / "new.txt", tmp_path / "host" / "gone.txt"
        gone.write_text("stare\n")

        async def scenario():
            entry = await journal.begin("web:kuba", "execute_command", "mv", files=[(str(new), "/new.txt"), (str(gone), "/gone.txt")])
            new.write_text("nowe\n"); gone.unlink()
            journal.finish(entry, "done", 0)
            return {f["path"]: f for f in journal.changes(journal.load(entry.id))["files"]}
        files = asyncio.run(scenario())
        assert files["/new.txt"]["status"] == "added" and "+nowe" in files["/new.txt"]["diff"]
        assert files["/gone.txt"]["status"] == "deleted" and "-stare" in files["/gone.txt"]["diff"]


class TestWebBridge:
    """Most HTTP na prawdziwych gniazdach: atrapa backendu (JSON lines) + WebBridge."""

    async def _backend(self, received, frames):
        async def handle(reader, writer):
            received.append(json.loads(await reader.readline()))
            for frame in frames:
                writer.write((json.dumps(frame) + "\n").encode())
            await writer.drain()
            writer.close()
        return await asyncio.start_server(handle, "127.0.0.1", 0)

    async def _http(self, port, method, path, headers=None, body=b""):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Content-Length: {len(body)}"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items() if k.lower() != "host"]
        if headers and "Host" in headers:
            lines[1] = f"Host: {headers['Host']}"
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), 5)
        writer.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), head.decode("latin-1"), payload

    def _run(self, scenario, frames=()):
        async def go():
            received = []
            backend = await self._backend(received, list(frames))
            bridge = web.WebBridge("127.0.0.1", backend.sockets[0].getsockname()[1], "tok", lang="en",
                                   server_label="vps1", port=web.free_port(0))
            server = await asyncio.start_server(bridge.handle, "127.0.0.1", bridge.port)
            try:
                return await scenario(bridge, received)
            finally:
                server.close(); backend.close()
        return asyncio.run(go())

    def test_page_needs_the_key_and_sets_a_cookie(self):
        async def scenario(bridge, _):
            denied = await self._http(bridge.port, "GET", "/")
            wrong = await self._http(bridge.port, "GET", "/?k=zly")
            page = await self._http(bridge.port, "GET", f"/?k={bridge.key}")
            return denied[0], wrong[0], page
        denied, wrong, (status, head, body) = self._run(scenario)
        assert denied == 401 and wrong == 401 and status == 200
        assert "Set-Cookie: pipe_key=" in head
        assert "HttpOnly" in head and "SameSite=Strict" in head and "Content-Security-Policy" in head
        assert b'"lang": "en"' in body and b"__PIPE_CONFIG__" not in body
        assert b"tok" not in body                                    # token backendu nie trafia do przegladarki

    def test_stale_cookie_does_not_block_a_valid_key(self):
        """Po restarcie `pipe web` przegladarka ma ciasteczko ze starym kluczem — nowy adres musi dzialac."""
        async def scenario(bridge, _):
            stale = {"Cookie": "pipe_key=stary-klucz-z-poprzedniego-uruchomienia"}
            with_key = await self._http(bridge.port, "GET", f"/?k={bridge.key}", stale)
            without = await self._http(bridge.port, "GET", "/", stale)
            return with_key[0], without[0]
        assert self._run(scenario) == (200, 401)

    def test_foreign_host_and_origin_are_rejected(self):
        async def scenario(bridge, received):
            cookie = {"Cookie": f"pipe_key={bridge.key}"}
            rebinding = await self._http(bridge.port, "GET", f"/?k={bridge.key}", {"Host": "evil.example"})
            foreign = await self._http(bridge.port, "POST", "/api/request", {**cookie, "Origin": "http://evil.example"},
                                       b'{"command": "alerts"}')
            return rebinding[0], foreign[0], received
        rebinding, foreign, received = self._run(scenario)
        assert rebinding == 403 and foreign == 403 and received == []

    def test_request_is_proxied_with_token_and_web_interface(self):
        frames = [{"response": "", "status": "ok", "done": False, "event": {"type": "activity", "id": "1", "phase": "start"}},
                  {"response": "Gotowe", "status": "ok", "done": False}, {"response": "", "status": "ok", "done": True}]

        async def scenario(bridge, received):
            body = json.dumps({"message": "hej", "session": "abc", "token": "podmieniony", "interface": "telegram:1"}).encode()
            status, head, payload = await self._http(bridge.port, "POST", "/api/request",
                                                     {"Cookie": f"pipe_key={bridge.key}", "Origin": f"http://127.0.0.1:{bridge.port}"}, body)
            return status, head, payload, received
        status, head, payload, received = self._run(scenario, frames)
        assert status == 200 and "application/x-ndjson" in head
        assert [json.loads(line) for line in payload.decode().strip().splitlines()] == frames
        (request,) = received
        assert request["token"] == "tok" and request["interface"].startswith("web")       # pola z przegladarki nie nadpisuja
        assert request["session_id"] == "web-abc" and request["message"] == "hej"

    def test_bad_requests(self):
        async def scenario(bridge, received):
            cookie = {"Cookie": f"pipe_key={bridge.key}"}
            results = [
                (await self._http(bridge.port, "POST", "/api/request", cookie, b"{nie json"))[0],
                (await self._http(bridge.port, "POST", "/api/request", cookie, b'{"session": "x"}'))[0],
                (await self._http(bridge.port, "POST", "/api/request", cookie, b'{"command": "subscribe"}'))[0],
                (await self._http(bridge.port, "GET", "/static/..%2fserver.py", cookie))[0],
                (await self._http(bridge.port, "GET", "/static/../server.py", cookie))[0],
                (await self._http(bridge.port, "GET", "/static/app.js", cookie))[0],
                (await self._http(bridge.port, "GET", "/nope", cookie))[0],
            ]
            return results, received
        results, received = self._run(scenario)
        assert results == [400, 400, 400, 404, 404, 200, 404] and received == []

    def test_events_stream_as_sse(self):
        frames = [{"response": "", "status": "ok", "done": False, "event": {"type": "subscribed"}},
                  {"response": "", "status": "ok", "done": False, "event": {"type": "reminder", "text": "kawa"}}]

        async def scenario(bridge, received):
            status, head, payload = await self._http(bridge.port, "GET", "/api/events", {"Cookie": f"pipe_key={bridge.key}"})
            return status, head, payload, received
        status, head, payload, received = self._run(scenario, frames)
        assert status == 200 and "text/event-stream" in head
        assert 'data: {"type": "reminder", "text": "kawa"}' in payload.decode()
        assert received[0]["command"] == "subscribe" and received[0]["token"] == "tok"

    def test_static_files_exist_and_have_no_inline_scripts(self):
        index = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for name in ("app.css", "app.js", "graph.js", "md.js", "i18n.js", "theme.js", "icon.svg"):
            assert (web.STATIC_DIR / name).is_file() and f"/static/{name}" in index
        import re
        inline = [m for m in re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", index) if "application/json" not in m]
        assert inline == []                                          # CSP: script-src 'self'
        scripts = "".join((web.STATIC_DIR / name).read_text() for name in ("app.js", "md.js", "graph.js"))
        assert ".innerHTML" not in scripts and "insertAdjacentHTML" not in scripts   # tresc od modelu tylko jako tekst
