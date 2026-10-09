"""
Tryb obserwacji (PIPE_OBSERVE=1, `install-server.sh --profile observe`): Pipe czyta i pilnuje, ale niczego nie
zmienia na hoscie. Komenda zmieniajaca stan, zapis pliku, /cofnij, zgody MCP — odmowa bez pytania o TAK.
Rejestry i pamiec Pipe (DATA_DIR) dzialaja dalej, z potwierdzeniem jak zwykle.
"""

import asyncio
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import audit, runtime
from backend.core.agent import VPSAgent
from backend.core.session import Session
from backend.tests.fakes import FakeClient, assert_history_valid, completion

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setenv("PIPE_OBSERVE", "1")
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    return tmp_path


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


def texts(events):
    return [e for e in events if isinstance(e, str)]


def test_flag_values(monkeypatch):
    for value, expected in (("1", True), ("true", True), ("0", False), ("", False), ("nie", False)):
        monkeypatch.setenv("PIPE_OBSERVE", value)
        assert runtime.observe() is expected


class TestAgent:

    def test_prompt_tells_the_model(self, monkeypatch):
        assert "TRYB OBSERWACJI" in Session().system_prompt
        monkeypatch.setenv("PIPE_OBSERVE", "0")
        assert "TRYB OBSERWACJI" not in Session().system_prompt

    def test_state_changing_command_is_refused_without_asking(self, isolated):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p nowy-katalog"})]),
            completion("Nie moge"),
        ]))
        events = run(agent.chat("s", "zrob katalog"))
        session = agent._sessions["s"]
        assert session.pending_confirmation is None
        assert not any("[POTWIERDZ]" in e for e in texts(events))
        assert any(e.startswith("[ODMOWA]") and "obserwacji" in e for e in texts(events))
        result = next(m for m in session.messages if m.get("tool_call_id") == "c1")
        assert "trybie obserwacji" in result["content"]
        assert not (isolated / "host" / "nowy-katalog").exists()
        assert_history_valid(session.messages)

    def test_yolo_does_not_bypass_it(self, isolated):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p yolo-katalog"})]),
            completion("ok"),
        ]))
        agent.get_or_create_session("s").yolo = True
        run(agent.chat("s", "zrob"))
        assert not (isolated / "host" / "yolo-katalog").exists()
        assert_history_valid(agent._sessions["s"].messages)

    def test_reads_still_run(self, isolated):
        (isolated / "host" / "plik.txt").write_text("tresc")
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "read_file", {"path": "/plik.txt"})]),
            completion("ok"),
        ]))
        run(agent.chat("s", "pokaz"))
        result = next(m for m in agent._sessions["s"].messages if m.get("tool_call_id") == "c1")
        assert "tresc" in result["content"]

    @pytest.mark.parametrize("name,args", [
        ("write_file", {"path": "/x.conf", "content": "a"}),
        ("journal", {"operation": "undo"}),
        ("cron_manage", {"operation": "add", "schedule": "* * * * *", "command": "true"}),
        ("mcp_manage", {"operation": "add", "name": "x", "command": "x"}),
        ("pipe_update", {"operation": "apply"}),
    ])
    def test_host_writes_blocked_upfront(self, isolated, name, args):
        agent = VPSAgent(client=FakeClient([completion(tool_calls=[("c1", name, args)]), completion("ok")]))
        events = run(agent.chat("s", "zrob"))
        assert any(e.startswith("[ODMOWA]") for e in texts(events))
        assert not (isolated / "host" / "x.conf").exists()
        assert_history_valid(agent._sessions["s"].messages)

    def test_pipe_registries_keep_working(self):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "routine_manage", {"operation": "add", "name": "dyski", "schedule": "0 7 * * *",
                                                             "task": "sprawdz dyski"})]),
        ]))
        events = run(agent.chat("s", "codziennie sprawdzaj dyski"))
        session = agent._sessions["s"]
        assert session.pending_confirmation is not None and session.pending_confirmation.action is not None
        assert any("[POTWIERDZ]" in e for e in texts(events))


class TestGateways:

    def test_mcp_run_command_is_refused_not_queued(self, isolated):
        from backend.core.approvals import get_approvals
        from backend.core.mcp import server as mcp_server
        from backend.core.mcp.server import Caller

        get_approvals()._items.clear()
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "run_command", "arguments": {"command": "touch /tmp/x"}}}
        response = asyncio.run(mcp_server.handle(request, Caller("token:laptop", "admin")))
        result = response["result"]
        assert result["isError"] and "obserwacji" in result["content"][0]["text"]
        assert not get_approvals().pending()

    def test_approval_cannot_be_approved(self, monkeypatch):
        from backend.core.approvals import ApprovalError, get_approvals

        monkeypatch.setenv("PIPE_OBSERVE", "0")
        approvals = get_approvals()
        approvals._items.clear()
        approval = approvals.create(command="touch /tmp/x", inner="touch /tmp/x", target="local",
                                    requested_by="laptop", reason="", plan=None)
        monkeypatch.setenv("PIPE_OBSERVE", "1")
        with pytest.raises(ApprovalError, match="obserwacji"):
            asyncio.run(approvals.decide(approval.id, True, "admin"))
        assert asyncio.run(approvals.decide(approval.id, False, "admin")).status == "denied"

    def test_server_undo_execute_is_refused(self, monkeypatch):
        import backend.server as server
        from backend.tests.test_server_commands import FakeAgent, exchange

        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(server, "get_agent", lambda: FakeAgent())
        [[refused]] = exchange([{"command": "undo", "id": "abc", "execute": True}])
        assert refused["status"] == "error" and "obserwacji" in refused["response"]


class TestCompose:

    def test_overlay_drops_docker_socket_and_root_write(self):
        text = (ROOT / "backend" / "docker-compose.observe.yml").read_text(encoding="utf-8")
        agent = text.split("  vps-agent:")[1].split("  docker-proxy:")[0]      # sama usluga, bez komentarzy
        assert "docker.sock" not in agent and "/root:/hostfs/root" not in agent
        assert "PIPE_OBSERVE=1" in agent and "DOCKER_HOST=tcp://docker-proxy:2375" in agent
        assert "volumes: !override" in agent and "cap_drop: [ALL]" in agent
        assert "docker.sock:/var/run/docker.sock:ro" in text and "POST=0" in text and "internal: true" in text

    def test_installer_knows_the_profile(self):
        script = (ROOT / "scripts" / "install-server.sh").read_text(encoding="utf-8")
        assert "--profile" in script and "docker-compose.observe.yml" in script and "PIPE_OBSERVE" in script
