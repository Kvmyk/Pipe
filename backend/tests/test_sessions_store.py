"""
Sesje na dysku (core/sessions_store.py): rozmowa przetrwa restart backendu — nowy VPSAgent z tym samym
DATA_DIR kontynuuje historie. Oczekujace potwierdzenie przepada z jasna odpowiedzia dla modelu.
"""

import asyncio
import json
import os
import stat
import time

import pytest

from backend.config import settings
from backend.core import audit, sessions_store
from backend.core.agent import VPSAgent
from backend.core.session import ConfirmationRequest, Session
from backend.tests.fakes import FakeClient, assert_history_valid, completion


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(settings, "SESSION_KEEP_DAYS", 7)
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    return tmp_path


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


class TestStore:

    def test_roundtrip_without_images_yolo_or_role(self):
        session = Session(session_id="abc", interface="web:kuba", owner="token:laptop", cwd="/etc", role="viewer")
        session.yolo = True
        session.web_urls = {"https://example.org/a"}
        session.messages = [
            {"role": "user", "content": [{"type": "text", "text": "co tu jest?"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": "Wykres", "pipe_provider": "gemini"},
        ]
        session.workers = {"dyski": [{"role": "user", "content": "sprawdz"}]}
        sessions_store.save(session)
        restored = sessions_store.load("abc", 7)
        assert (restored.interface, restored.owner, restored.cwd) == ("web:kuba", "token:laptop", "/etc")
        assert restored.web_urls == {"https://example.org/a"} and restored.workers == session.workers
        assert restored.yolo is False and restored.role == "admin"
        assert "base64" not in json.dumps(restored.messages) and restored.messages[1]["pipe_provider"] == "gemini"
        assert session.messages[0]["content"][1]["type"] == "image_url"          # oryginal nietkniety

    def test_file_is_private_and_named_by_hash(self):
        sessions_store.save(Session(session_id="../../etc/passwd"))
        files = list(sessions_store.directory().glob("*.json"))
        assert len(files) == 1 and files[0].parent == sessions_store.directory()
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(sessions_store.directory().stat().st_mode) == 0o700

    def test_pending_confirmation_becomes_not_executed(self):
        session = Session(session_id="p")
        session.messages = [{"role": "user", "content": "restartuj nginx"},
                            {"role": "assistant", "content": "", "tool_calls": [
                                {"id": "c1", "type": "function",
                                 "function": {"name": "execute_command", "arguments": "{}"}}]}]
        session.pending_confirmation = ConfirmationRequest("c1", "execute_command", "systemctl restart nginx", "confirm")
        sessions_store.save(session)
        restored = sessions_store.load("p", 7)
        assert restored.pending_confirmation is None
        assert restored.messages[-1]["tool_call_id"] == "c1" and "NIE zostala wykonana" in restored.messages[-1]["content"]
        assert_history_valid(restored.messages)

    def test_expired_corrupt_and_foreign_files(self):
        sessions_store.save(Session(session_id="stara"))
        path = sessions_store._path("stara")
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        assert sessions_store.load("stara", 7) is None and not path.exists()
        sessions_store._path("zla").write_text("{nie json")
        assert sessions_store.load("zla", 7) is None
        sessions_store.save(Session(session_id="moja"))
        sessions_store._path("obca").write_text(sessions_store._path("moja").read_text())
        assert sessions_store.load("obca", 7) is None                    # session_id w pliku musi sie zgadzac

    def test_prune(self):
        for name in ("a", "b"):
            sessions_store.save(Session(session_id=name))
        old = time.time() - 30 * 86400
        os.utime(sessions_store._path("a"), (old, old))
        assert sessions_store.prune(7) == 1 and sessions_store.load("b", 7) is not None
        assert sessions_store.prune(0) == 1                              # zapis wylaczony — usun wszystko


class TestAgentRestart:

    def test_conversation_continues_after_restart(self):
        first = VPSAgent(client=FakeClient([completion("Masz 3 kontenery.")]))
        run(first.chat("s1", "ile mam kontenerow?", "cli:kuba", owner="token:laptop"))
        client = FakeClient([completion("Ten trzeci to redis.")])
        second = VPSAgent(client=client)                                  # nowy proces po restarcie
        assert second.owns("s1", "token:laptop") and not second.owns("s1", "token:obcy")
        events = run(second.chat("s1", "a ktory jest trzeci?", "cli:kuba", owner="token:laptop"))
        assert events == ["Ten trzeci to redis."]
        sent = [m.get("content") for m in client.calls[0]["messages"]]
        assert "ile mam kontenerow?" in sent and "Masz 3 kontenery." in sent
        assert_history_valid(second._sessions["s1"].messages)

    def test_pending_confirmation_lost_on_restart_keeps_history_valid(self):
        first = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p katalog"})]),
        ]))
        run(first.chat("s1", "zrob katalog"))
        assert first._sessions["s1"].pending_confirmation is not None
        second = VPSAgent(client=FakeClient([completion("Zaproponuje to jeszcze raz.")]))
        assert any("[OSTRZEZENIE]" in e for e in run(second.confirm("s1", True)))   # nie ma juz czego zatwierdzic
        run(second.chat("s1", "no i?"))
        assert_history_valid(second._sessions["s1"].messages)

    def test_disabled_with_zero_days(self, monkeypatch):
        monkeypatch.setattr(settings, "SESSION_KEEP_DAYS", 0)
        run(VPSAgent(client=FakeClient([completion("ok")])).chat("s1", "czesc"))
        assert not list(sessions_store.directory().glob("*.json"))


class TestNewChat:
    """/nowa (komenda new_chat): rozmowa znika z pamieci i z dysku, wiedza o serwerze zostaje w prompcie."""

    def test_forgets_conversation_keeps_server_knowledge(self, monkeypatch):
        import backend.server as server
        from backend.core import memory
        from backend.tests.test_server_events import exchange

        memory.write_server_md("# Serwer\n\nSklep w /srv/shop, baza postgres 16.")
        agent = VPSAgent(client=FakeClient([completion("Masz 3 kontenery.")]))
        run(agent.chat("s", "ile mam kontenerow?", "telegram:7"))
        assert list(sessions_store.directory().glob("*.json"))
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(server, "get_agent", lambda: agent)

        [frame] = exchange({"command": "new_chat", "interface": "telegram:7"})
        assert frame["data"] == {"cleared": True}
        assert "s" not in agent._sessions and not list(sessions_store.directory().glob("*.json"))
        [frame] = exchange({"command": "new_chat", "interface": "telegram:7"})
        assert frame["data"] == {"cleared": False}

        client = FakeClient([completion("Postgres 16.")])
        agent._client = client
        run(agent.chat("s", "jaka baza?", "telegram:7"))
        sent = client.calls[0]["messages"]
        assert "ile mam kontenerow?" not in [m.get("content") for m in sent]       # stara rozmowa przepadla
        assert "baza postgres 16" in sent[0]["content"]                             # SERVER.md dalej w prompcie

    def test_session_restored_from_disk_counts_as_cleared(self):
        run(VPSAgent(client=FakeClient([completion("ok")])).chat("s1", "czesc"))
        restarted = VPSAgent(client=FakeClient([]))
        assert restarted.delete_session("s1") is True and restarted.delete_session("s1") is False


class TestBackendNote:
    """Wynik /zalataj (bez modelu) trafia do rozmowy — agent zapytany pozniej nie zgaduje."""

    def test_note_keeps_history_valid_and_survives_restart(self):
        agent = VPSAgent(client=FakeClient([completion("Czesc.")]))
        run(agent.chat("s", "czesc", "telegram:7"))
        assert agent.note("s", "Uzytkownik zainstalowal latki przez /zalataj, kod wyjscia 0.")
        messages = agent._sessions["s"].messages
        assert [m["role"] for m in messages[-2:]] == ["user", "assistant"] and all(m["pipe_generated"] for m in messages[-2:])
        assert_history_valid(messages)
        client = FakeClient([completion("Latki weszly.")])
        restarted = VPSAgent(client=client)
        run(restarted.chat("s", "jak tam latanie?", "telegram:7"))
        assert any("kod wyjscia 0" in str(m.get("content")) for m in client.calls[0]["messages"])
        assert not agent.note("nie-ma-takiej", "x")

    def test_no_note_while_a_confirmation_waits(self):
        agent = VPSAgent(client=FakeClient([completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p x"})])]))
        run(agent.chat("s", "zrob katalog"))
        assert agent.note("s", "x") is False


class TestSlashCommandsInConversation:
    """Wynik /podatnosci, /zalataj, /alerty... (bez modelu) trafia do rozmowy — "zrob to sam" dotyczy tego, co widac."""

    def test_command_result_becomes_a_note_and_patch_notes_itself_once(self, monkeypatch):
        import backend.server as server
        from backend.core import watch
        from backend.tests.test_server_commands import exchange

        agent = VPSAgent(client=FakeClient([]))
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(server, "get_agent", lambda: agent)
        monkeypatch.setattr(watch, "_watcher", watch.Watcher())
        exchange([{"command": "alerts", "interface": "telegram:7"}])
        session = agent._sessions["s1"]                                # rozmowa zalozona przez sama komende
        note = session.messages[-2]["content"]
        assert note.startswith("[PIPE") and "/alerty" in note and "Brak aktywnych alertow" in note
        assert session.messages[-2]["pipe_generated"] and session.interface == "telegram:7"
        assert_history_valid(session.messages)
        before = len(session.messages)
        exchange([{"command": "patch", "interface": "telegram:7"}])
        # /zalataj notuje sie samo (tu: nic do zainstalowania) — jedna notatka, nie druga z rejestratora komend
        assert len(session.messages) == before + 2 and "/zalataj" in session.messages[-2]["content"]
        assert_history_valid(session.messages)
