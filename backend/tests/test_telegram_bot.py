"""
Testy strumieniowania w bocie Telegram (clients/telegram/bot.py) na atrapie Bot API.
Pomijane, gdy python-telegram-bot nie jest zainstalowany.
"""

import asyncio
import base64
import sys
from pathlib import Path

import pytest

pytest.importorskip("telegram")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))

import bot  # noqa: E402
import tg_format  # noqa: E402


class FakeMessage:
    def __init__(self, log, text):
        self.log, self.text = log, text

    async def edit_text(self, text, **kwargs):
        self.log.append(("edit", text))


class FakeBot:
    def __init__(self):
        self.log = []

    async def send_chat_action(self, **kwargs):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        self.log.append(("message", text, kwargs.get("reply_markup")))
        return FakeMessage(self.log, text)

    async def send_photo(self, chat_id, photo, caption=None):
        self.log.append(("photo", photo.read(), caption))

    async def send_document(self, chat_id, document, caption=None):
        self.log.append(("document", caption))


class Context:
    def __init__(self):
        self.bot = FakeBot()


async def frames(*items):
    for item in items:
        yield item


def test_stream_sends_photo_progress_and_confirm_keyboard(monkeypatch):
    monkeypatch.setattr(bot, "PROGRESS_EDIT_INTERVAL", 0)
    context = Context()
    png = b"\x89PNG-dane"
    asyncio.run(bot._run_and_reply(context, 1, 42, frames(
        {"response": "", "status": "ok", "done": False, "event": {"type": "progress", "text": "web: start"}},
        {"response": "", "status": "ok", "done": False, "event": {"type": "progress", "text": "web $ df -h"}},
        {"response": "", "status": "ok", "done": False,
         "attachment": {"name": "m.png", "mime": "image/png", "caption": "Mapa", "data": base64.b64encode(png).decode()}},
        {"response": "[POTWIERDZ] Operacja wymaga potwierdzenia: `rm x`", "status": "confirm", "done": False},
        {"response": "", "status": "ok", "done": True},
    )))
    kinds = [entry[0] for entry in context.bot.log]
    assert ("photo", png, "Mapa") in context.bot.log
    assert kinds.count("message") == 2 and "edit" in kinds          # status postepu + odpowiedz
    answer = context.bot.log[-1]
    assert "rm x" in answer[1] and answer[2] is not None             # klawiatura TAK/NIE


def test_alert_broadcast_to_allowed_users(monkeypatch):
    sent = []

    async def fake_send(bot_, chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))

    monkeypatch.setattr(bot, "_send_html", fake_send)
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {1, 2})
    app = type("App", (), {"bot": None})()
    asyncio.run(bot._broadcast(app, {"type": "alert", "id": "abc", "severity": "critical", "state": "new",
                                     "title": "Dysk / 98%", "detail": "wolne 1 GB"}))
    assert {c for c, _, _ in sent} == {1, 2}
    assert sent[0][2].inline_keyboard[0][0].callback_data == "investigate:abc"
    sent.clear()
    asyncio.run(bot._broadcast(app, {"type": "alert", "state": "resolved", "title": "Dysk / 98%"}))
    assert sent[0][2] is None and "ROZWIAZANE" in sent[0][1]


def test_routine_report_is_escaped():
    text = tg_format.format_routine({"name": "backup", "status": "PROBLEM", "report": "<script> & x"})
    assert "&lt;script&gt; &amp; x" in text and "<pre>" in text
