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
