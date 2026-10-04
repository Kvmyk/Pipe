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
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {42})
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


def test_viewer_never_gets_confirm_keyboard(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {1})
    monkeypatch.setattr(bot, "VIEWER_USER_IDS", {42})
    context = Context()
    asyncio.run(bot._run_and_reply(context, 1, 42, frames(
        {"response": "[POTWIERDZ] Operacja wymaga potwierdzenia: `rm x`", "status": "confirm", "done": False},
        {"response": "", "status": "ok", "done": True},
    )))
    assert context.bot.log[-1][2] is None
    assert bot._is_allowed(42) and not bot._is_admin(42) and bot._is_admin(1)


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
    assert "&lt;script&gt; &amp; x" in text and "<pre>" not in text


def test_routine_report_is_a_readable_message_not_a_code_block():
    report = ("=== WORKER backup (cel: local, komend: 4) ===\nSTATUS: PROBLEM\n"
              "USTALENIA: backup w `/var/backups/pg` ma 3 dni, **dysk** 91%\n- plik a_b.dump: 0 B\n"
              "PROPOZYCJE: `rm *.tmp`\n<a href=\"http://evil\">kliknij</a>")
    text = tg_format.format_routine({"name": "backup", "status": "PROBLEM", "report": report})
    assert text.startswith("<b>Rutyna backup</b> — ⚠️ PROBLEM\n\n<b>USTALENIA:</b>")
    assert "STATUS:" not in text and "=== WORKER" not in text and text.endswith("<i>cel: local, komend: 4</i>")
    assert "<code>/var/backups/pg</code>" in text and "<b>dysk</b>" in text and "• plik a_b.dump: 0 B" in text
    assert "<code>rm *.tmp</code>" in text                    # komenda doslownie, gwiazdka nie staje sie pogrubieniem
    assert "<a href" not in text and "&lt;a href=" in text    # link z raportu nie jest klikalny
    assert tg_format.format_routine({"name": "x", "status": "OK", "report": ""}).endswith("<i>(pusty raport)</i>")


def test_listing_uses_bullets_headers_and_literal_details():
    text = tg_format.format_listing("Zmiany (24h)", "Pakiety (2):\n  - pakiet nginx: 1.0 -> 1.2\n"
                                    "  - [BEZPIECZENSTWO] nowe konto <eve>\nCron (1):\n  - zmieniono cron root:\n"
                                    "    + c\n    - b_*x*")
    assert text.splitlines()[0] == "<b>Zmiany (24h)</b>" and "<pre>" not in text
    assert "<b>Pakiety (2):</b>\n• pakiet nginx: 1.0 -&gt; 1.2" in text
    assert "• <b>[BEZPIECZENSTWO]</b> nowe konto &lt;eve&gt;" in text
    assert "    <code>+ c</code>\n    <code>- b_*x*</code>" in text
    ids = tg_format.format_listing("", "Przypomnienia (czas serwera: 20:47):\n- #efe88c 20:57 (za 10 min): kawa")
    assert ids == "<b>Przypomnienia (czas serwera: 20:47):</b>\n• <code>#efe88c</code> 20:57 (za 10 min): kawa"
    assert tg_format.format_listing("Dziennik", "").endswith("(pusto)")


# --- Kanal zdarzen: przypomnienia dochodza zawsze, a cisza ma byc wyjasniona ---

class _App:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data = {}


def _serve(tmp_path, frames_to_send, received):
    """Atrapa backendu na sockecie Unix: przyjmuje subskrypcje i wysyla podane ramki."""
    async def handle(reader, writer):
        import json
        received.append(json.loads(await reader.readline()))
        for frame in frames_to_send:
            writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.close()
    return asyncio.start_unix_server(handle, path=str(tmp_path / "pipe.sock"))


def _run_loop(monkeypatch, tmp_path, frames_to_send, *, alerts_enabled=True, seconds=0.6):
    monkeypatch.setattr(bot, "AGENT_SOCKET", str(tmp_path / "pipe.sock"))
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {1, 2})
    monkeypatch.setattr(bot, "VIEWER_USER_IDS", set())
    monkeypatch.setattr(bot, "ALERTS_ENABLED", alerts_enabled)
    monkeypatch.setattr(bot, "_subscription", {"connected": False, "error": "", "since": 0.0})
    app, received = _App(), []

    async def scenario():
        server = await _serve(tmp_path, frames_to_send, received)
        task = asyncio.create_task(bot.alerts_loop(app))
        await asyncio.sleep(seconds)
        connected = bot._subscription["connected"]
        task.cancel()
        server.close()
        return connected
    return app, received, asyncio.run(scenario())


def _event(event):
    return {"response": "", "status": "ok", "done": False, "event": event}


def test_reminder_goes_only_to_the_user_who_set_it(monkeypatch, tmp_path):
    app, received, _ = _run_loop(monkeypatch, tmp_path, [
        _event({"type": "subscribed"}),
        _event({"type": "reminder", "id": "ab12cd", "kind": "message", "text": "siemka", "to": "telegram:2",
                "set_at": "2026-10-01 20:16"}),
    ], seconds=0.2)
    assert received[0]["command"] == "subscribe"
    assert [(kind, text) for kind, text, _ in app.bot.log] == [
        ("message", "<b>Przypomnienie</b>\nsiemka\n<i>ustawione 2026-10-01 20:16</i>")]


def test_alerts_off_still_delivers_reminders_and_approvals(monkeypatch, tmp_path):
    """TELEGRAM_ALERTS=0 wycisza czuwanie, ale przypomnienie (i zgoda) to cos, o co uzytkownik sam prosil."""
    app, _, connected = _run_loop(monkeypatch, tmp_path, [
        _event({"type": "subscribed"}),
        _event({"type": "alert", "id": "a", "severity": "warning", "state": "new", "title": "Dysk", "detail": "x"}),
        _event({"type": "routine", "name": "r", "status": "OK", "report": "ok"}),
        _event({"type": "reminder", "id": "ab12cd", "kind": "message", "text": "kawa", "to": "telegram:1"}),
    ], alerts_enabled=False, seconds=0.2)
    texts = [text for _, text, _ in app.bot.log]
    assert connected and len(texts) == 1 and "kawa" in texts[0]


def test_subscription_state_and_note(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPE_LANG", "pl")
    _, _, connected = _run_loop(monkeypatch, tmp_path, [_event({"type": "subscribed"})], seconds=0.15)
    assert connected
    # backend zamknal polaczenie -> stan "nieaktywny" z powodem, widoczny w /przypomnienia i /alerty
    assert bot._subscription["connected"] is False or bot.subscription_note() == ""
    monkeypatch.setattr(bot, "_subscription", {"connected": True, "error": "", "since": 1.0})
    assert bot.subscription_note() == ""
    monkeypatch.setattr(bot, "_subscription", {"connected": False, "error": "backend odrzucil subskrypcje: zly <token>", "since": 0})
    note = bot.subscription_note()
    assert "bot nie odbiera teraz powiadomien" in note and "zly &lt;token&gt;" in note


def test_rejected_subscription_is_recorded(monkeypatch, tmp_path):
    _, _, connected = _run_loop(monkeypatch, tmp_path, [
        {"response": "[BLAD] Blad: Nieprawidlowy token autoryzacji.", "status": "error", "done": True}], seconds=0.2)
    assert not connected and "Nieprawidlowy token" in bot._subscription["error"]


def test_missing_socket_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "AGENT_SOCKET", str(tmp_path / "nie-ma.sock"))
    monkeypatch.setattr(bot, "_subscription", {"connected": False, "error": "", "since": 0.0})

    async def scenario():
        task = asyncio.create_task(bot.alerts_loop(_App()))
        await asyncio.sleep(0.15)
        task.cancel()
    asyncio.run(scenario())
    assert "nie-ma.sock" in bot._subscription["error"]


def test_report_formatting_leaves_code_untouched():
    report = "USTALENIA: log ponizej\n```\nERROR: brak miejsca\n- linia logu\n```\n- wniosek `- x` koniec"
    text = tg_format.report_html(report)
    assert "<pre>ERROR: brak miejsca\n- linia logu</pre>" in text          # w bloku kodu nic sie nie zmienia
    assert text.startswith("<b>USTALENIA:</b>") and "• wniosek <code>- x</code> koniec" in text
    # poprawny HTML Telegrama: brak tagow wewnatrz <pre>/<code>
    import re
    assert not re.search(r"<(pre|code)>[^<]*<(b|i)>", text)


def test_language_event_switches_the_bot(monkeypatch, tmp_path):
    """Jezyk zmieniony z dowolnego kanalu (/jezyk) dociera do bota jako zdarzenie — komunikaty i menu w nowym jezyku."""
    monkeypatch.setenv("PIPE_LANG", "pl")
    refreshed = []

    async def refresh_menu(_bot, user_id):
        refreshed.append((user_id, bot.tr("pl", "en")))
    monkeypatch.setattr(bot, "refresh_menu", refresh_menu)
    app, _, _ = _run_loop(monkeypatch, tmp_path, [
        _event({"type": "subscribed", "lang": "pl", "lang_chosen": False}),
        _event({"type": "language", "lang": "en"}),
        _event({"type": "reminder", "id": "ab12cd", "kind": "message", "text": "coffee", "to": "telegram:1"}),
    ], seconds=0.2)
    assert sorted(refreshed) == [(1, "en"), (2, "en")]
    assert [text for _, text, _ in app.bot.log][0].startswith("<b>Reminder</b>")


def test_language_chosen_on_the_server_applies_on_subscribe(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPE_LANG", "pl")

    async def refresh_menu(_bot, _user_id):
        return None
    monkeypatch.setattr(bot, "refresh_menu", refresh_menu)
    _run_loop(monkeypatch, tmp_path, [_event({"type": "subscribed", "lang": "en", "lang_chosen": True})], seconds=0.15)
    assert bot.tr("pl", "en") == "en"


class FakeFile:
    def __init__(self, data):
        self.data = data

    async def download_as_bytearray(self):
        return bytearray(self.data)


class FakeMedia:
    def __init__(self, data, file_name=None, mime_type=None):
        self.data, self.file_size, self.file_name, self.mime_type = data, len(data), file_name, mime_type

    async def get_file(self):
        return FakeFile(self.data)


def file_update(message_id, *, photo=None, document=None, caption=None, group=None):
    from types import SimpleNamespace

    message = SimpleNamespace(photo=[photo] if photo else [], document=document, caption=caption,
                              media_group_id=group, message_id=message_id, replies=[])

    async def reply_text(text, **kwargs):
        message.replies.append(text)
    message.reply_text = reply_text
    return SimpleNamespace(effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=1),
                           message=message, effective_message=message)


def test_photo_and_album_go_to_the_agent_as_attachments(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {42})
    monkeypatch.setattr(bot, "ALBUM_WAIT", 0.05)
    sent = []

    class Client:
        def chat(self, message, attachments=None):
            sent.append((message, attachments))
            return frames({"response": "ok", "status": "ok", "done": True})

    monkeypatch.setattr(bot, "get_client", lambda user_id: Client())

    async def scenario():
        context = Context()
        await bot.handle_file(file_update(1, document=FakeMedia(b"log", "app.log", "text/plain"),
                                          caption="co tu nie gra?"), context)
        album = [file_update(2 + i, photo=FakeMedia(b"\xff\xd8\xff" + bytes([i])), group="g", caption="porownaj" if i else None)
                 for i in range(3)]
        await asyncio.gather(*(bot.handle_file(update, context) for update in album))
        too_big = file_update(9, document=FakeMedia(b"x", "big.iso"))
        too_big.message.document.file_size = bot.MAX_FILE_BYTES + 1
        await bot.handle_file(too_big, context)
        return too_big.message.replies

    replies = asyncio.run(scenario())
    (text, [doc]), (caption, photos) = sent
    assert text == "co tu nie gra?" and doc["name"] == "app.log" and base64.b64decode(doc["data"]) == b"log"
    assert caption == "porownaj" and len(photos) == 3 and all(p["mime"] == "image/jpeg" for p in photos)
    assert len(sent) == 2 and "za duzy" in replies[0]
