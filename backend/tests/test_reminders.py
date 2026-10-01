"""
Przypomnienia: jednorazowe wiadomosci i zadania o okreslonym czasie.

Najwazniejsze: agent, ktory mowi "odezwe sie za 10 sekund", musi miec czym to zrobic —
przypomnienie odpala sie samo, dociera do wlasciwego uzytkownika i nie ginie, gdy nikt nie slucha.
"""

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import reminders, watch
from backend.core.agent import VPSAgent
from backend.core.memory import RESERVED_COMMANDS
from backend.tests.fakes import FakeClient, assert_history_valid, completion

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
import tg_format  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"), raising=False)
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(watch, "_watcher", None)


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


class TestTime:

    @pytest.mark.parametrize("text,seconds", [
        ("10s", 10), ("10", 10), ("10 sekund", 10), ("5 min", 300), ("15m", 900), ("1h30m", 5400),
        ("1 h 30 min", 5400), ("2 godziny", 7200), ("2d", 172800), ("1.5h", 5400), ("10 seconds", 10),
        ("3 tygodnie", 3 * 604800),
    ])
    def test_parse_delay(self, text, seconds):
        assert reminders.parse_delay(text) == seconds

    @pytest.mark.parametrize("text", ["", "abc", "5 parsekow", "10s potem", "za chwile"])
    def test_parse_delay_rejects_garbage(self, text):
        with pytest.raises(reminders.ReminderError):
            reminders.parse_delay(text)

    def test_parse_at_today_or_tomorrow(self):
        now = datetime(2026, 10, 1, 20, 0, 0)
        assert datetime.fromtimestamp(reminders.parse_at("20:30", now)) == datetime(2026, 10, 1, 20, 30)
        assert datetime.fromtimestamp(reminders.parse_at("09:00", now)) == datetime(2026, 10, 2, 9, 0)
        assert datetime.fromtimestamp(reminders.parse_at("2026-10-05 07:15", now)) == datetime(2026, 10, 5, 7, 15)
        with pytest.raises(reminders.ReminderError):
            reminders.parse_at("jutro rano", now)

    def test_resolve_time_limits(self):
        now = time.time()
        assert reminders.resolve_time("10s", now=now) == pytest.approx(now + 10)
        with pytest.raises(reminders.ReminderError):
            reminders.resolve_time("", "")
        with pytest.raises(reminders.ReminderError):
            reminders.resolve_time("10s", "12:00")
        with pytest.raises(reminders.ReminderError):
            reminders.resolve_time("500d")
        with pytest.raises(reminders.ReminderError):
            reminders.resolve_time("", "2020-01-01 10:00")


class TestRegistry:

    def test_add_due_and_remove(self):
        now = time.time()
        first = reminders.add(now + 5, "kawa", to="telegram:1", now=now)
        reminders.add(now + 500, "pozniej", to="cli:kuba", now=now)
        assert [r.text for r in reminders.load_reminders()] == ["kawa", "pozniej"]
        assert reminders.due(now) == [] and [r.id for r in reminders.due(now + 6)] == [first.id]
        assert reminders.next_due(now) == pytest.approx(5)
        assert reminders.remove("#" + first.id).text == "kawa" and reminders.remove(first.id) is None

    def test_secret_and_limits_rejected(self):
        with pytest.raises(reminders.ReminderError):
            reminders.add(time.time() + 5, "haslo: DB_PASSWORD=supersecretvalue")
        with pytest.raises(reminders.ReminderError):
            reminders.add(time.time() + 5, "")
        with pytest.raises(reminders.ReminderError):
            reminders.add(time.time() + 5, "x", kind="shell")
        for index in range(reminders.MAX_REMINDERS):
            reminders.add(time.time() + 100 + index, f"r{index}")
        with pytest.raises(reminders.ReminderError):
            reminders.add(time.time() + 5, "za duzo")

    def test_claim_takes_only_waiting_ones_of_the_interface(self):
        now = time.time()
        cli = reminders.add(now, "z cli", to="cli:kuba")
        telegram = reminders.add(now, "z telegrama", to="telegram:5")
        pending = reminders.add(now + 900, "jeszcze nie", to="cli:kuba")
        reminders.mark_fired(cli.id)
        reminders.mark_fired(telegram.id)
        assert [r.id for r in reminders.claim("cli")] == [cli.id]
        assert {r.id for r in reminders.load_reminders()} == {telegram.id, pending.id}
        assert reminders.claim("cli") == []

    def test_corrupt_file_is_empty_registry(self):
        reminders.reminders_path().parent.mkdir(parents=True, exist_ok=True)
        reminders.reminders_path().write_text("{nie json")
        assert reminders.load_reminders() == []


class TestTool:

    def agent(self, script):
        return VPSAgent(client=FakeClient(script))

    def test_message_reminder_needs_no_confirmation_and_notifies(self):
        agent = self.agent([
            completion(tool_calls=[("c1", "reminder", {"operation": "add", "delay": "10s", "text": "Minelo 10 sekund."})]),
            completion("Napisze o 19:53:10."),
        ])
        events = [e for e in run(agent.chat("s", "napisz do mnie za 10 sekund", "telegram:7")) if isinstance(e, str)]
        session = agent._sessions["s"]
        assert session.pending_confirmation is None
        assert any(e.startswith("[PAMIEC] Przypomnienie #") for e in events)
        (saved,) = reminders.load_reminders()
        assert saved.text == "Minelo 10 sekund." and saved.to == "telegram:7" and saved.kind == "message"
        assert 8 < saved.at - time.time() <= 10
        tool = next(m for m in session.messages if m.get("role") == "tool")["content"]
        assert saved.id in tool and "nie czekaj" in tool
        assert_history_valid(session.messages)

    def test_bad_time_goes_back_to_the_model(self):
        agent = self.agent([
            completion(tool_calls=[("c1", "reminder", {"operation": "add", "delay": "niedlugo", "text": "x"})]),
            completion("Kiedy dokladnie?"),
        ])
        run(agent.chat("s", "przypomnij niedlugo"))
        tool = next(m for m in agent._sessions["s"].messages if m.get("role") == "tool")["content"]
        assert tool.startswith("Blad:") and reminders.load_reminders() == []

    def test_task_reminder_requires_confirmation_with_full_text(self):
        task = "sprawdz, czy backup w /var/backups/pg jest z dzisiaj"
        agent = self.agent([
            completion(tool_calls=[("c1", "reminder", {"operation": "add", "delay": "1h", "text": task, "kind": "task"})]),
            completion("Zaplanowane."),
        ])
        events = [e for e in run(agent.chat("s", "za godzine sprawdz backup")) if isinstance(e, str)]
        confirm = next(e for e in events if "[POTWIERDZ]" in e)
        assert "wymaga potwierdzenia" in confirm and task in confirm
        assert reminders.load_reminders() == []                      # nic nie zapisane przed TAK
        run(agent.confirm("s", True))
        (saved,) = reminders.load_reminders()
        assert saved.kind == "task" and saved.text == task
        assert 3590 < saved.at - time.time() <= 3600                 # czas liczony od zatwierdzenia
        assert_history_valid(agent._sessions["s"].messages)

    def test_task_reminder_declined(self):
        agent = self.agent([
            completion(tool_calls=[("c1", "reminder", {"operation": "add", "delay": "1h", "text": "x", "kind": "task"})]),
            completion("OK"),
        ])
        run(agent.chat("s", "za godzine sprawdz"))
        run(agent.confirm("s", False))
        assert reminders.load_reminders() == []

    def test_viewer_cannot_schedule_task_or_cancel_others(self):
        other = reminders.add(time.time() + 600, "cudze", to="telegram:1")
        agent = self.agent([
            completion(tool_calls=[("c1", "reminder", {"operation": "add", "delay": "1h", "text": "x", "kind": "task"})]),
            completion(tool_calls=[("c2", "reminder", {"operation": "cancel", "id": other.id})]),
            completion(tool_calls=[("c3", "reminder", {"operation": "add", "delay": "5m", "text": "moje"})]),
            completion("koniec"),
        ])
        events = [e for e in run(agent.chat("s", "x", "telegram:9", role="viewer")) if isinstance(e, str)]
        assert not any("[POTWIERDZ]" in e for e in events)
        texts = sorted(r.text for r in reminders.load_reminders())
        assert texts == ["cudze", "moje"]                            # wlasna wiadomosc wolno, reszta odrzucona

    def test_list_and_cancel(self):
        saved = reminders.add(time.time() + 600, "spotkanie", to="cli:kuba")
        agent = self.agent([
            completion(tool_calls=[("c1", "reminder", {"operation": "list"})]),
            completion(tool_calls=[("c2", "reminder", {"operation": "cancel", "id": saved.id})]),
            completion("Anulowane."),
        ])
        events = [e for e in run(agent.chat("s", "anuluj", "cli:kuba")) if isinstance(e, str)]
        tools = [m["content"] for m in agent._sessions["s"].messages if m.get("role") == "tool"]
        assert "spotkanie" in tools[0] and "czas serwera" in tools[0]
        assert reminders.load_reminders() == [] and any("[PAMIEC] Anulowalem" in e for e in events)

    def test_prompt_forbids_empty_promises(self):
        from backend.config import prompts, prompts_en
        assert "NIGDY nie obiecuj" in prompts.BASE_SYSTEM_PROMPT and "reminder" in prompts.BASE_SYSTEM_PROMPT
        assert "NIGDY nie obiecuj" in prompts.TELEGRAM_SYSTEM_PROMPT
        assert "NEVER promise" in prompts_en.BASE_SYSTEM_PROMPT


class TestDelivery:

    def test_fires_on_time_to_subscriber(self):
        async def scenario():
            watcher = watch.Watcher()
            queue = watcher.notifier.subscribe()
            task = asyncio.create_task(watcher._reminder_loop())
            await asyncio.sleep(2.2)                                  # petla startuje po 2 s
            started = time.time()
            reminders.add(started + 0.4, "juz czas", to="telegram:7")
            watcher.reminders_changed()
            event = await asyncio.wait_for(queue.get(), 3)
            task.cancel()
            return event, time.time() - started
        event, elapsed = asyncio.run(scenario())
        assert event["type"] == "reminder" and event["text"] == "juz czas" and event["to"] == "telegram:7"
        assert 0.3 < elapsed < 1.5
        assert reminders.load_reminders() == []

    def test_waits_when_nobody_listens_then_delivers_on_subscribe(self):
        async def scenario():
            watcher = watch.Watcher()
            saved = reminders.add(time.time() - 60, "zalegle", to="telegram:7")   # np. po restarcie backendu
            task = asyncio.create_task(watcher._reminder_loop())
            await asyncio.sleep(2.4)
            waiting = [r.id for r in reminders.waiting()]
            queue = watcher.notifier.subscribe()
            watcher.reminders_changed()
            event = await asyncio.wait_for(queue.get(), 3)
            task.cancel()
            return saved.id, waiting, event
        saved_id, waiting, event = asyncio.run(scenario())
        assert waiting == [saved_id] and event["id"] == saved_id
        assert reminders.load_reminders() == []

    def test_task_runs_worker_once_and_reports(self, monkeypatch):
        from backend.core import workers

        calls = []

        async def fake_worker(agent, parent, name, target, task, progress=None, *, extra_instructions=""):
            calls.append((name, target.name, task))
            return workers.WorkerResult(name, target.name, report="backup jest z dzisiaj", commands=2)

        monkeypatch.setattr(workers, "run_worker", fake_worker)

        async def scenario():
            watcher = watch.Watcher(agent=object())
            queue = watcher.notifier.subscribe()
            saved = reminders.add(time.time(), "sprawdz backup", kind="task", to="cli:kuba")
            await watcher.fire_reminder(saved)
            return await asyncio.wait_for(queue.get(), 2)
        event = asyncio.run(scenario())
        assert calls == [(calls[0][0], "local", "sprawdz backup")] and calls[0][0].startswith("reminder-")
        assert event["kind"] == "task" and "backup jest z dzisiaj" in event["report"]
        assert reminders.load_reminders() == []


class TestServerCommand:

    def call(self, request, interface="cli:kuba", role="admin"):
        from backend import server

        sent = []

        async def fake_send(writer, data):
            sent.append(data)

        async def go():
            original = server._send
            server._send = fake_send
            try:
                await server._reminders(None, request, interface, role)
            finally:
                server._send = original
        asyncio.run(go())
        return sent[-1]

    def test_list_cancel_and_claim(self):
        keep = reminders.add(time.time() + 600, "zostaje", to="cli:kuba")
        gone = reminders.add(time.time() + 700, "do anulowania", to="cli:kuba")
        fired = reminders.add(time.time() - 5, "odpalilo bez odbiorcy", to="cli:kuba")
        reminders.mark_fired(fired.id)
        data = self.call({"cancel": gone.id, "claim": True})["data"]
        assert data["cancelled"] == gone.id
        assert [e["text"] for e in data["claimed"]] == ["odpalilo bez odbiorcy"]
        assert [r["id"] for r in data["reminders"]] == [keep.id] and "zostaje" in data["text"]

    def test_viewer_cancels_only_own(self):
        other = reminders.add(time.time() + 600, "cudze", to="telegram:1")
        mine = reminders.add(time.time() + 600, "moje", to="telegram:9")
        assert self.call({"cancel": other.id}, "telegram:9", "viewer")["status"] == "error"
        assert self.call({"cancel": mine.id}, "telegram:9", "viewer")["data"]["cancelled"] == mine.id
        assert self.call({"cancel": "ffffff"})["status"] == "error"


class TestTelegram:

    def test_recipients(self):
        admins, viewers = {1, 2}, {9}
        assert tg_format.reminder_recipients({"to": "telegram:2"}, admins, viewers) == [2]
        assert tg_format.reminder_recipients({"to": "telegram:9"}, admins, viewers) == [9]
        # ustawione z CLI albo przez kogos, kogo juz nie ma na liscie — do administratorow
        assert tg_format.reminder_recipients({"to": "cli:kuba"}, admins, viewers) == [1, 2]
        assert tg_format.reminder_recipients({"to": "telegram:777"}, admins, viewers) == [1, 2]

    def test_format_escapes_text(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "pl")
        text = tg_format.format_reminder({"kind": "message", "text": "sprawdz <b>dysk</b>", "set_at": "2026-10-01 19:53"})
        assert text.startswith("<b>Przypomnienie</b>") and "&lt;b&gt;dysk&lt;/b&gt;" in text
        task = tg_format.format_reminder({"kind": "task", "text": "backup", "report": "OK <x>", "set_at": ""})
        assert "<pre>OK &lt;x&gt;</pre>" in task
        monkeypatch.setenv("PIPE_LANG", "en")
        assert tg_format.format_reminder({"kind": "message", "text": "x"}).startswith("<b>Reminder</b>")

    def test_commands_registered_and_reserved(self):
        assert "przypomnienia" in {name for name, _ in tg_format.BUILTIN_COMMANDS}
        assert tg_format.COMMAND_ALIASES["reminders"] == "przypomnienia"
        assert {"przypomnienia", "reminders"} <= RESERVED_COMMANDS


class TestHonestyWhenNobodyListens:

    def test_tool_reply_and_notice_warn_on_telegram_without_subscription(self):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "reminder", {"operation": "add", "delay": "5s", "text": "siemka"})]),
            completion("ok"),
        ]))
        events = [e for e in run(agent.chat("s", "napisz za 5 sekund", "telegram:7")) if isinstance(e, str)]
        tool = next(m for m in agent._sessions["s"].messages if m.get("role") == "tool")["content"]
        assert "UWAGA: bot Telegrama nie odbiera teraz powiadomien" in tool and "NIE twierdz" in tool
        assert any("[PAMIEC]" in e and "UWAGA: bot nie odbiera" in e for e in events)

    def test_no_warning_when_bot_is_subscribed(self):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "reminder", {"operation": "add", "delay": "5s", "text": "siemka"})]),
            completion("ok"),
        ]))
        watch.get_watcher(agent).notifier.subscribe()
        events = [e for e in run(agent.chat("s", "napisz za 5 sekund", "telegram:7")) if isinstance(e, str)]
        tool = next(m for m in agent._sessions["s"].messages if m.get("role") == "tool")["content"]
        assert "UWAGA" not in tool and not any("UWAGA" in e for e in events)

    def test_cli_gets_a_plain_note(self):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "reminder", {"operation": "add", "delay": "5s", "text": "siemka"})]),
            completion("ok"),
        ]))
        run(agent.chat("s", "napisz za 5 sekund", "cli:kuba"))
        tool = next(m for m in agent._sessions["s"].messages if m.get("role") == "tool")["content"]
        assert "przy najblizszej wiadomosci" in tool
