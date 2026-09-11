"""
Testy zadan {"command": ...} w backend/server.py — prawdziwy serwer na Unix
sockecie, agent podmieniony na atrape (bez LLM).
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

import backend.server as server
from backend.config import settings
from backend.config.prompts import SCAN_SERVER_CREATE, SCAN_SERVER_UPDATE
from backend.core import memory


class FakeAgent:
    def __init__(self):
        self.messages: list[tuple[str, str, str]] = []

    async def chat(self, session_id, message, interface="cli"):
        self.messages.append((session_id, message, interface))
        yield "odpowiedz agenta"

    async def confirm(self, session_id, confirmed):
        yield "potwierdzono"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    fake = FakeAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)
    return fake


def exchange(requests: list[dict]) -> list[list[dict]]:
    """Wysyla zadania po kolei na jednym polaczeniu, zbiera odpowiedzi do done=true."""
    async def run():
        # Krotka sciezka — Unix socket ma limit ~108 znakow.
        sock = Path(tempfile.mkdtemp(prefix="p")) / "s.sock"
        srv = await asyncio.start_unix_server(server.handle_client, path=str(sock))
        reader, writer = await asyncio.open_unix_connection(str(sock))
        replies = []
        for req in requests:
            writer.write((json.dumps({"session_id": "s1", **req}) + "\n").encode())
            await writer.drain()
            frames = []
            while True:
                frame = json.loads(await reader.readline())
                frames.append(frame)
                if frame.get("done"):
                    break
            replies.append(frames)
        writer.close()
        srv.close()
        await srv.wait_closed()
        return replies
    return asyncio.run(run())


class TestListSkills:

    def test_returns_skills_with_commands(self, agent):
        memory.save_skill("odnow-certyfikat", "Odnowienie TLS", "certbot renew")
        [[frame]] = exchange([{"command": "list_skills"}])
        assert frame["done"] and frame["status"] == "ok"
        assert frame["data"]["skills"] == [
            {"name": "odnow-certyfikat", "description": "Odnowienie TLS", "command": "odnow_certyfikat"}
        ]
        assert agent.messages == []  # bez LLM

    def test_empty(self, agent):
        [[frame]] = exchange([{"command": "list_skills"}])
        assert frame["data"]["skills"] == []


class TestServerMd:

    def test_missing_returns_empty_content(self, agent):
        [[frame]] = exchange([{"command": "server_md"}])
        assert frame["data"]["content"] == ""

    def test_returns_content(self, agent):
        memory.write_server_md("## Przeglad\nDebian 13")
        [[frame]] = exchange([{"command": "server_md"}])
        assert "Debian 13" in frame["data"]["content"]


class TestScanServer:

    def test_creates_when_missing(self, agent):
        [frames] = exchange([{"command": "scan_server", "interface": "telegram:1"}])
        assert agent.messages == [("s1", SCAN_SERVER_CREATE, "telegram:1")]
        assert frames[-1]["done"] and frames[0]["response"] == "odpowiedz agenta"

    def test_updates_when_exists(self, agent):
        memory.write_server_md("## Przeglad\nx")
        exchange([{"command": "scan_server"}])
        assert agent.messages[0][1] == SCAN_SERVER_UPDATE


class TestRunSkill:

    @pytest.mark.parametrize("name", ["odnow_certyfikat", "/odnow_certyfikat", "odnow-certyfikat"])
    def test_by_command_or_name(self, agent, name):
        memory.save_skill("odnow-certyfikat", "Odnowienie TLS", "certbot renew")
        [frames] = exchange([{"command": "run_skill", "name": name, "args": "tylko dla example.com"}])
        message = agent.messages[0][1]
        assert "'odnow-certyfikat'" in message and "/odnow_certyfikat" in message
        assert "tylko dla example.com" in message
        assert frames[-1]["done"]

    def test_without_args(self, agent):
        memory.save_skill("backup", "Kopia", "pg_dump")
        exchange([{"command": "run_skill", "name": "backup"}])
        assert "Dodatkowe" not in agent.messages[0][1]

    def test_unknown_skill_is_error_without_llm(self, agent):
        [[frame]] = exchange([{"command": "run_skill", "name": "nie-ma"}])
        assert frame["status"] == "error" and frame["done"]
        assert agent.messages == []


class TestProtocol:

    def test_unknown_command_is_error(self, agent):
        [[frame]] = exchange([{"command": "zrob_kawe"}])
        assert frame["status"] == "error" and "Nieznana komenda" in frame["response"]

    def test_token_required_for_commands(self, agent, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "tajny")
        memory.save_skill("backup", "Kopia", "pg_dump")
        [[bad], [good]] = exchange([
            {"command": "list_skills"},
            {"command": "list_skills", "token": "tajny"},
        ])
        assert bad["status"] == "error" and "data" not in bad
        assert good["data"]["skills"][0]["name"] == "backup"

    def test_plain_messages_still_work(self, agent):
        [frames] = exchange([{"message": "czesc", "interface": "cli"}])
        assert agent.messages == [("s1", "czesc", "cli")]
        assert frames[-1]["done"]


class TestSkillCommandsMapping:

    def test_hyphens_become_underscores(self):
        entries = memory.skill_commands([memory.Skill("odnow-certyfikat", "d", "c")])
        assert entries[0]["command"] == "odnow_certyfikat"

    def test_reserved_names_get_no_command(self):
        entries = memory.skill_commands([memory.Skill("status", "d", "c"), memory.Skill("server", "d", "c")])
        assert [e["command"] for e in entries] == ["", ""]

    def test_truncation_collision_first_wins(self):
        a = "a" * 32 + "-jeden"
        b = "a" * 32 + "-dwa"
        entries = memory.skill_commands([memory.Skill(a, "d", "c"), memory.Skill(b, "d", "c")])
        assert entries[0]["command"] == "a" * 32
        assert entries[1]["command"] == ""

    def test_command_is_telegram_safe(self):
        cmd = memory.command_name("Ab-Cd-" + "x" * 40)
        assert len(cmd) <= 32 and all(ch.islower() or ch.isdigit() or ch == "_" for ch in cmd)

    def test_find_by_name_command_and_slash(self):
        entries = memory.skill_commands([memory.Skill("deploy-app", "d", "c")])
        for token in ("deploy-app", "deploy_app", "/deploy_app", " Deploy_App "):
            assert memory.find_skill_command(token, entries)["name"] == "deploy-app"
        assert memory.find_skill_command("", entries) is None
        assert memory.find_skill_command("nie-ma", entries) is None

    def test_reserved_skill_not_reachable_by_empty_command(self):
        entries = memory.skill_commands([memory.Skill("status", "d", "c")])
        assert memory.find_skill_command("status", entries)["name"] == "status"  # po nazwie tak
        assert memory.find_skill_command("", entries) is None

    def test_canonical_name_is_directory_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "skills" / "moj-skill").mkdir(parents=True)
        (tmp_path / "skills" / "moj-skill" / "SKILL.md").write_text("---\nname: Recznie Zmieniona Nazwa\ndescription: d\n---\ntresc")
        assert [s.name for s in memory.list_skills()] == ["moj-skill"]
        assert memory.read_skill("moj-skill").name == "moj-skill"
