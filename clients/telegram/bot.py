"""
Telegram Bot -- interfejs Telegram dla Pipe (agent do zarzadzania serwerami).

Laczy sie z backendem przez Unix socket.
Uzywa python-telegram-bot w trybie async.

Pipe v0.50.1

Funkcje:
  - Whitelist uzytkownikow (TELEGRAM_ALLOWED_USER_IDS), osobna sesja per user_id
  - Diagramy (np. /mapa albo "pokaz architekture") przychodza jako zdjecia
  - Postep workerow na zywo w jednej, aktualizowanej wiadomosci
  - Czuwanie: alerty i raporty rutyn przychodza same, z przyciskiem "Zbadaj"
  - Przypomnienia: "napisz do mnie za 10 minut" -- wiadomosc przychodzi sama o czasie (/przypomnienia)
  - InlineKeyboard dla potwierdzen (TAK / NIE)
  - Zdjecia, zrzuty ekranu i dokumenty (podpis = tresc wiadomosci, album = jedna wiadomosc) oraz glosowki
  - Pipe pisze pierwszy: alerty, podpowiedzi i ustalenia z badan przychodza same, z przyciskami akcji zbudowanymi
    w backendzie (skrzynka: /nowosci); powiadamia tylko o tym, co ma `push` (poziom, cisza nocna, limit — /powiadomienia)
  - Klawiatura pod polem wiadomosci: Stan serwera (bez LLM), Co nowego, Projekty, Pomoc
  - Komendy: /status /nowosci /projekty /sprzataj /web /powiadomienia /raport /zmiany /wykres /zdrowie /mapa /server
    /katalogi /skille /alerty /rutyny /cele /vibe /koszt /dziennik /cofnij /incydenty /historia /pomoc, kazdy skill
    ma wlasna komende /<nazwa> (angielskie aliasy: /news /projects /report /changes ... — dzialaja w obu jezykach)
  - Poranny raport przychodzi sam (DIGEST_TIME w backendzie) razem z wykresem obciazenia
  - Menu '/' ustawiane per czat dozwolonego uzytkownika (opisy skilli nie wyciekaja do obcych)
  - HTML parse mode (nie MarkdownV2) -- formatowanie w tg_format.py

Konfiguracja w .env (clients/telegram/.env):
  TELEGRAM_BOT_TOKEN=your_bot_token
  TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
  AGENT_SOCKET=/tmp/vps-agent.sock
  TELEGRAM_ALERTS=1          # 0 = nie przesylaj alertow czuwania
  TELEGRAM_VIEWER_IDS=...    # tylko odczyt (wymaga AGENT_VIEWER_TOKEN, takze w backend/.env)
  AGENT_VIEWER_TOKEN=...
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv

# Zaladuj .env z katalogu bota
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

try:
    from telegram import (
        BotCommand,
        BotCommandScopeChat,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        InputFile,
        ReplyKeyboardMarkup,
        Update,
    )
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        TypeHandler,
        filters,
    )
except ImportError:
    print("Blad: zainstaluj zaleznosci: pip install -r requirements.txt")
    sys.exit(1)

from tg_format import (
    SCAN_WORDS,
    build_menu,
    collect_response_text,
    command_names,
    feed_buttons,
    format_feed,
    format_feed_item,
    format_overview,
    format_prefs,
    format_projects,
    keyboard_command,
    keyboard_rows,
    prefs_buttons,
    project_buttons,
    format_alert,
    format_alerts,
    format_approval,
    format_audit,
    format_audit_vulns,
    format_digest,
    format_directory,
    format_help,
    format_investigation,
    markdown_html,
    MAX_MODEL_LABEL,
    format_model_picker,
    format_providers,
    match_models,
    model_page,
    provider_choices,
    format_list,
    format_listing,
    format_mcp,
    mcp_buttons,
    format_pre,
    format_reminder,
    format_routine,
    format_skill_list,
    parse_command,
    progress_text,
    activity_entry,
    reminder_recipients,
    response_data,
    split_message,
    to_plain_text,
    to_telegram_html,
    tr,
    view_buttons,
    view_question,
)

# --- Konfiguracja ---
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip().isdigit()
}
# Przegladajacy: rozmowa, diagnoza, raporty, alerty — bez zatwierdzania zmian (rola viewer w backendzie).
VIEWER_USER_IDS: set[int] = {
    int(uid.strip()) for uid in os.getenv("TELEGRAM_VIEWER_IDS", "").split(",") if uid.strip().isdigit()
} - ALLOWED_USER_IDS
AGENT_SOCKET: str = os.getenv("AGENT_SOCKET", "/tmp/vps-agent.sock")
# Token autoryzacji backendu — musi byc zgodny z AGENT_TOKEN w backend/.env.
# Pusty = backend nie wymaga tokenu.
AGENT_TOKEN: str = os.getenv("AGENT_TOKEN", "")
# Token roli viewer (AGENT_VIEWER_TOKEN w backend/.env) — dla TELEGRAM_VIEWER_IDS.
AGENT_VIEWER_TOKEN: str = os.getenv("AGENT_VIEWER_TOKEN", "")
ALERTS_ENABLED: bool = os.getenv("TELEGRAM_ALERTS", "1").strip().lower() not in ("0", "false", "no", "nie")

# Ramki z diagramami (base64) sa duze — domyslny limit linii asyncio to 64 KiB.
READ_LIMIT = 32 * 1024 * 1024
PROGRESS_EDIT_INTERVAL = 1.5  # s — Telegram ogranicza czestotliwosc edycji
# Telegram: zdjecie maks. 10 MB i szerokosc + wysokosc <= 10000 px; wieksze idzie jako plik.
MAX_PHOTO_BYTES = 10 * 1024 * 1024


# --- Socket Client ---

class TelegramSocketClient:
    """Klient Unix socket dla Telegram bota -- per user_id."""

    def __init__(self, socket_path: str, session_id: str, token: str = "") -> None:
        self.socket_path = socket_path
        self.session_id = session_id
        self.token = token

    @property
    def interface(self) -> str:
        return f"telegram:{self.session_id}"

    async def stream(self, data: dict) -> AsyncIterator[dict]:
        """Wysyla zadanie i zwraca ramki na biezaco, do done=true wlacznie."""
        if self.token:
            data = {**data, "token": self.token}

        reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=READ_LIMIT)
        try:
            writer.write((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    frame = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                yield frame
                if frame.get("done"):
                    break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def collect(self, data: dict) -> list[dict]:
        return [frame async for frame in self.stream(data)]

    def chat(self, message: str, attachments: list[dict] | None = None) -> AsyncIterator[dict]:
        request = {"message": message, "session_id": self.session_id, "interface": self.interface}
        if attachments:
            request["attachments"] = attachments      # [{name, mime, data(base64)}] — docs/protocol.md
        return self.stream(request)

    def confirm(self, confirmed: bool) -> AsyncIterator[dict]:
        return self.stream({"confirm": confirmed, "session_id": self.session_id})

    def command(self, command: str, **fields) -> AsyncIterator[dict]:
        return self.stream({"command": command, "session_id": self.session_id, "interface": self.interface, **fields})

    async def data(self, command: str, **fields) -> dict:
        """Komenda zwracajaca dane (bez LLM): list_skills, server_md, directory, vibe, alerts..."""
        return response_data([frame async for frame in self.command(command, **fields)])

    async def list_skills(self) -> list[dict]:
        return (await self.data("list_skills")).get("skills", [])


# --- Zarzadzanie sesjami ---

_clients: dict[int, TelegramSocketClient] = {}


def get_client(user_id: int) -> TelegramSocketClient:
    """Zwraca istniejacy klient sesji lub tworzy nowy."""
    if user_id not in _clients:
        _clients[user_id] = TelegramSocketClient(
            socket_path=AGENT_SOCKET,
            session_id=str(user_id),
            token=AGENT_TOKEN if _is_admin(user_id) else AGENT_VIEWER_TOKEN,
        )
    return _clients[user_id]


# --- Helpers ---

def _is_allowed(user_id: int | None) -> bool:
    """Sprawdza czy user_id jest na whiteliscie (administratorzy albo przegladajacy)."""
    if not user_id:
        return False
    return user_id in ALLOWED_USER_IDS or user_id in VIEWER_USER_IDS


def _is_admin(user_id: int | None) -> bool:
    """Tylko administratorzy zatwierdzaja zmiany i cofaja je."""
    return bool(user_id) and user_id in ALLOWED_USER_IDS


def _confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Tworzy klawiature inline z przyciskami TAK/NIE."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(tr("✅ TAK, wykonaj", "✅ YES, run it"), callback_data=f"confirm:yes:{user_id}"),
            InlineKeyboardButton(tr("NIE", "NO"), callback_data=f"confirm:no:{user_id}"),
        ]
    ])


# Alert o podatnosciach -> zakres latki (zaleznosci aplikacji nie maja latki — to zmiana w kodzie).
PATCH_SCOPES = {"vuln:system": "system", "vuln:images": "images", "vuln:servers": "servers", "vuln:apps": "apps"}


def _patch_scope(args: list[str] | None) -> str:
    """Slowo po /zalataj trafia do backendu bez zmian — to on wie, co znaczy (obrazy, serwer, nazwa serwera...)."""
    return " ".join(args or []).strip().lower()


def _alert_keyboard(event: dict, user_id: int) -> InlineKeyboardMarkup | None:
    """Zbadaj przy kazdym aktywnym alercie; przy podatnosciach administrator dostaje tez Zalataj."""
    if event.get("state") == "resolved" or not event.get("id"):
        return None
    row = [InlineKeyboardButton(tr("Zbadaj", "Investigate"), callback_data=f"investigate:{event['id']}")]
    scope = PATCH_SCOPES.get(str(event.get("key", "")))
    if scope and _is_admin(user_id):
        row.append(InlineKeyboardButton(tr("Zalataj", "Patch"), callback_data=f"patch:{scope}:{user_id}"))
    return InlineKeyboardMarkup([row])


def _alert_keyboard_without_patch(query) -> InlineKeyboardMarkup | None:
    """Przyciski alertu bez Zalataj — latanie juz idzie, drugie klikniecie niczego by nie dodalo."""
    markup = getattr(query.message, "reply_markup", None)
    rows = [[b for b in row if not str(getattr(b, "callback_data", "") or "").startswith("patch:")]
            for row in (markup.inline_keyboard if markup else [])]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(rows) if rows else None


def _patch_keyboard(scope: str, token: str, user_id: int) -> InlineKeyboardMarkup:
    """Zainstaluj / Anuluj pod podgladem — tylko dla backendu sprzed v0.38, ktory odpowiada podgladem z tokenem."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(tr("Zainstaluj", "Install"), callback_data=f"patchx:yes:{scope}:{token}:{user_id}"),
        InlineKeyboardButton(tr("Anuluj", "Cancel"), callback_data=f"patchx:no:{scope}:{token}:{user_id}"),
    ]])


async def _send_html(bot, chat_id: int, text: str, reply_markup=None) -> None:
    """Wysyla HTML z dzieleniem na czesci i fallbackiem do zwyklego tekstu."""
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            # Usun tagi i rozwin encje — tekst komendy zostaje nienaruszony
            try:
                await bot.send_message(chat_id, to_plain_text(chunk), reply_markup=markup)
            except Exception:
                await bot.send_message(chat_id, tr("Blad formatowania odpowiedzi. Sprobuj ponownie.",
                                                    "Could not format the answer. Try again."), reply_markup=markup)


async def _send_attachment(bot, chat_id: int, attachment: dict) -> None:
    """Diagram jako zdjecie; za duzy albo odrzucony przez Telegram — jako plik (pelna rozdzielczosc)."""
    data = base64.b64decode(attachment.get("data", ""))
    name = attachment.get("name") or "diagram.png"
    caption = (attachment.get("caption") or "")[:1000]
    if not data:
        return
    if attachment.get("mime", "").startswith("image/") and len(data) <= MAX_PHOTO_BYTES:
        try:
            await bot.send_photo(chat_id, photo=io.BytesIO(data), caption=caption or None)
            return
        except Exception as exc:
            print(f"[Pipe Telegram] Zdjecie odrzucone ({exc}) — wysylam jako plik", flush=True)
    await bot.send_document(chat_id, document=InputFile(io.BytesIO(data), filename=name), caption=caption or None)


async def _run_and_reply(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    frames: AsyncIterator[dict],
) -> None:
    """
    Konsumuje strumien ramek: zalaczniki wysyla od razu, postep pokazuje w jednej
    edytowanej wiadomosci, a tekst odpowiedzi — na koncu (z TAK/NIE, jesli trzeba).
    """
    # Updaty sa obslugiwane rownolegle (concurrent_updates), ale jedna sesja
    # agenta nie moze prowadzic dwoch petli naraz — kolejka per uzytkownik.
    async with _user_locks.setdefault(user_id, asyncio.Lock()):
        await _consume(context, chat_id, user_id, frames)


_user_locks: dict[int, asyncio.Lock] = {}


async def _collect(bot, chat_id: int, frames: AsyncIterator[dict]) -> list[dict]:
    """
    Ramki backendu: zalaczniki wysyla od razu, postep (kroki agenta, bezpiecznika) pokazuje w jednej edytowanej
    wiadomosci, ktora na koncu zamienia sie w zwinieta liste krokow. Zwraca pozostale ramki (tekst, dane).
    """
    responses: list[dict] = []
    progress_lines: list[str] = []
    steps: dict[str, int] = {}          # id kroku agenta -> jego wpis w progress_lines (start zamienia sie w wynik)
    progress_message = None
    last_edit = 0.0

    async def show_progress(force: bool = False) -> None:
        nonlocal progress_message, last_edit
        if not progress_lines or (not force and time.monotonic() - last_edit < PROGRESS_EDIT_INTERVAL):
            return
        text = progress_text(progress_lines)
        try:
            if progress_message is None:
                progress_message = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
            else:
                await progress_message.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        last_edit = time.monotonic()

    async for frame in frames:
        if frame.get("attachment"):
            await bot.send_chat_action(chat_id=chat_id, action="upload_photo")
            await _send_attachment(bot, chat_id, frame["attachment"])
        elif (frame.get("event") or {}).get("type") == "progress":
            progress_lines.append(frame["event"].get("text", ""))
            await show_progress()
        elif (frame.get("event") or {}).get("type") == "activity":
            # komenda i wycinek jej wyniku; widok prosty — bez technicznych krokow (wystarcza odpowiedz)
            event = frame["event"]
            entry = "" if _view_state["view"] == "simple" else activity_entry(event)
            if entry:
                index = steps.get(str(event.get("id")))
                if index is None:
                    steps[str(event.get("id"))] = len(progress_lines)
                    progress_lines.append(entry)
                else:
                    progress_lines[index] = entry
                await show_progress(force=event.get("phase") == "end" and event.get("ok") is False)
        else:
            responses.append(frame)
    # ta sama wiadomosc zamienia sie w podsumowanie ze zwinieta lista krokow — "W toku" nie zostaje w czacie
    if progress_lines:
        summary = progress_text(progress_lines, done=True)
        try:
            if progress_message is None:
                await bot.send_message(chat_id, summary, parse_mode=ParseMode.HTML)
            else:
                await progress_message.edit_text(summary, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    return responses


async def _consume(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, frames: AsyncIterator[dict]) -> None:
    bot = context.bot
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    responses = await _collect(bot, chat_id, frames)

    text, needs_confirm = collect_response_text(responses)
    if text:
        # HTML Telegrama: resztki Markdowna -> tagi, komendy w backtickach doslownie
        markup = _confirm_keyboard(user_id) if needs_confirm and _is_admin(user_id) else None
        await _send_html(bot, chat_id, to_telegram_html(text), markup)


async def _guard(update: Update) -> int | None:
    user = update.effective_user
    return user.id if user and _is_allowed(user.id) else None


async def _backend_call(update: Update, context: ContextTypes.DEFAULT_TYPE, coro) -> None:
    """Wspolna obsluga bledow polaczenia z backendem."""
    try:
        await coro
    except FileNotFoundError:
        await update.effective_message.reply_text(tr("Backend niedostepny. Upewnij sie, ze Pipe jest uruchomiony.",
                                                     "Backend unavailable. Make sure Pipe is running."))
    except Exception as exc:
        await update.effective_message.reply_text(tr("Blad: ", "Error: ") + html.escape(str(exc)))


# --- Handlery komend ---

# Widok Pipe (/widok, wspolny dla wszystkich kanalow): ze zdarzen `subscribed` / `view` backendu.
_view_state: dict = {"view": "tech", "chosen": True}


def _reply_keyboard() -> ReplyKeyboardMarkup:
    """Cztery najczestsze sprawy pod polem wiadomosci — bez znajomosci komend."""
    return ReplyKeyboardMarkup(keyboard_rows(_view_state["view"]), resize_keyboard=True, is_persistent=True,
                               input_field_placeholder=tr("Napisz, co mam zrobic…", "Tell me what to do…"))


def _inline(buttons: list[tuple[str, str]], per_row: int = 2) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start -- przywitanie z klawiatura najczestszych spraw."""
    if not await _guard(update):
        return  # Milcz dla nieautoryzowanych

    welcome = tr(
        "<b>Czesc, tu Pipe.</b>\n\n"
        "Dbam o Twoj serwer. Pilnuje go w tle i <b>sam napisze</b>, gdy cos bedzie wymagalo uwagi — z gotowym "
        "przyciskiem, ktory to naprawi. Rutynowe poprawki robie sam, z kopia, ktora cofniesz; o wazne sprawy "
        "(usuwanie danych, dostep do serwera) zapytam.\n\n"
        "Pisz do mnie normalnie, np. <i>\"czemu sklep dziala wolno?\"</i>, <i>\"postaw mi te aplikacje z GitHuba\"</i> "
        "albo <i>\"napisz skrypt, ktory co noc robi kopie bazy\"</i>. Mozesz tez nagrac glosowke.\n\n"
        "Przyciski pod polem wiadomosci: stan serwera, co nowego, projekty, pomoc.",
        "<b>Hi, this is Pipe.</b>\n\n"
        "I look after your server. I keep watch in the background and <b>write on my own</b> when something needs "
        "attention — with a ready button that fixes it. Routine fixes I make myself, with a backup you can undo; I ask "
        "about anything important (deleting data, access to the server).\n\n"
        "Just write to me, e.g. <i>\"why is the shop slow?\"</i>, <i>\"set up this app from GitHub for me\"</i> or "
        "<i>\"write a script that backs up the database every night\"</i>. You can also record a voice note.\n\n"
        "Buttons under the message field: server status, what's new, projects, help."
    )
    user_id = update.effective_user.id
    try:
        state = await get_client(user_id).data("view")
        _view_state.update(view=state.get("view") or "tech", chosen=bool(state.get("chosen")))
    except Exception:
        pass                    # starszy backend bez widokow
    if _view_state["view"] == "simple":
        welcome = tr(
            "<b>Czesc, tu Pipe.</b>\n\n"
            "Pisz do mnie normalnie, np. <i>\"przypomnij mi jutro o 9 o fakturze\"</i>, <i>\"streszcz ten plik\"</i> "
            "albo <i>\"podlacz mi kalendarz\"</i>. Mozesz tez nagrac glosowke albo wyslac zdjecie.\n\n"
            "Przyciski pod polem wiadomosci: czy wszystko dziala, co nowego, skille, pomoc.",
            "<b>Hi, this is Pipe.</b>\n\n"
            "Just write to me, e.g. <i>\"remind me tomorrow at 9 about the invoice\"</i>, <i>\"summarise this file\"</i> "
            "or <i>\"connect my calendar\"</i>. You can also record a voice note or send a photo.\n\n"
            "Buttons under the message field: is everything OK, what's new, skills, help.")
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML, reply_markup=_reply_keyboard())
    if not _view_state["chosen"] and _is_admin(user_id):
        # Pierwsze uruchomienie: co uzytkownik chce robic — przycisk albo kilka slow w nastepnej wiadomosci.
        context.user_data["view_ask"] = True
        await update.message.reply_text(view_question(), parse_mode=ParseMode.HTML, reply_markup=_inline(view_buttons()))


async def _set_view(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, value: str) -> bool:
    """Ustawia widok (nazwa albo opis zwyklymi slowami); False, gdy backend nie wie, ktory wybrac."""
    try:
        data = await get_client(user_id).data("view", args=value)
    except RuntimeError as exc:
        await _send_html(context.bot, chat_id, html.escape(str(exc)), _inline(view_buttons()))
        return False
    await _view_changed(context.application, data.get("view"))
    await context.bot.send_message(chat_id, html.escape(data.get("text", "")), parse_mode=ParseMode.HTML,
                                   reply_markup=_reply_keyboard())
    return True


async def _view_changed(app: Application, value) -> None:
    """Zdarzenie `view` z backendu (zmiana z dowolnego kanalu): menu '/' i pomoc w nowym widoku."""
    view = "simple" if value == "simple" else "tech"
    if _view_state.get("view") == view and _view_state.get("chosen"):
        return
    _view_state.update(view=view, chosen=True)
    for user_id in ALLOWED_USER_IDS | VIEWER_USER_IDS:
        await refresh_menu(app.bot, user_id)


async def cmd_widok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/widok [prosty|techniczny|opis] -- widok Pipe wspolny dla CLI, pipe web i Telegrama."""
    user_id = await _guard(update)
    if not user_id:
        return
    args = " ".join(context.args or []).strip()
    chat_id = update.effective_chat.id

    async def go() -> None:
        if args:
            await _set_view(context, chat_id, user_id, args)
            return
        data = await get_client(user_id).data("view")
        markup = _inline(view_buttons(data.get("view", ""))) if data.get("can_edit") else None
        await _send_html(context.bot, chat_id, html.escape(data.get("text", "")), markup)

    await _backend_call(update, context, go())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status -- stan serwera bez LLM: od razu i za darmo."""
    await _show_data(update, context, "overview", format_overview)


async def cmd_nowosci(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/nowosci -- skrzynka: co Pipe napisal sam; nowe wiadomosci z przyciskami akcji."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        chat_id = update.effective_chat.id
        data = await get_client(user_id).data("feed", args="seen")
        await _send_html(context.bot, chat_id, format_feed(data))
        # nowe sprawy z przyciskami (najwyzej trzy) — reszta jest na liscie
        for item in [i for i in data.get("items") or [] if i.get("unread") and i.get("actions")][:3]:
            await _send_html(context.bot, chat_id, format_feed_item(item), _inline(feed_buttons(item, _is_admin(user_id))))

    await _backend_call(update, context, go())


async def cmd_projekty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/projekty -- warsztat: projekty ze stanem; przyciski logow i zatrzymania."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        chat_id = update.effective_chat.id
        data = await get_client(user_id).data("projects")
        buttons = [b for p in (data.get("projects") or [])[:6] for b in project_buttons(p, _is_admin(user_id) and data.get("can_edit"))]
        await _send_html(context.bot, chat_id, format_projects(data), _inline(buttons))

    await _backend_call(update, context, go())


async def cmd_powiadomienia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/powiadomienia -- kiedy Pipe ma powiadamiac: poziom, cisza nocna, podpowiedzi dziennie."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        data = await get_client(user_id).data("prefs")
        rows = prefs_buttons(data) if data.get("can_edit") and _is_admin(user_id) else []
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=cb) for label, cb in row] for row in rows]) \
            if rows else None
        await _send_html(context.bot, update.effective_chat.id, format_prefs(data), markup)

    await _backend_call(update, context, go())


async def cmd_sprzataj(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sprzataj -- zwolnij miejsce (obrazy bez tagu, cache) bez LLM, z krokami na zywo."""
    user_id = await _guard(update)
    if not user_id:
        return
    if not _is_admin(user_id):
        await update.message.reply_text(tr("Sprzata tylko administrator.", "Only an administrator cleans up."))
        return

    async def go() -> None:
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        data = response_data(await _collect(context.bot, chat_id, get_client(user_id).command("cleanup")))
        await _send_html(context.bot, chat_id, format_listing(tr("Sprzatanie", "Cleanup"), data.get("text", "")))

    await _backend_call(update, context, go())


async def cmd_pobierz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pobierz skill|plugin <zrodlo> [filtr] -- podglad i przyciski Zainstaluj / Anuluj (bez LLM)."""
    user_id = await _guard(update)
    if not user_id:
        return
    if not _is_admin(user_id):
        await update.message.reply_text(tr("Skille i pluginy instaluje tylko administrator.",
                                           "Only an administrator installs skills and plugins."))
        return
    args = " ".join(context.args or [])

    async def go() -> None:
        chat_id = update.effective_chat.id
        frames = await _collect(context.bot, chat_id, get_client(user_id).command("install", args=args))
        error = next((f.get("response", "") for f in frames if f.get("status") == "error"), "")
        if error:
            await _send_html(context.bot, chat_id, format_listing(tr("Pobieranie", "Download"), error))
            return
        data = response_data(frames)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(tr("Zainstaluj", "Install"), callback_data=f"inst:yes:{data['token']}:{user_id}"),
            InlineKeyboardButton(tr("Anuluj", "Cancel"), callback_data=f"inst:no:{data['token']}:{user_id}"),
        ]]) if data.get("token") else None
        await _send_html(context.bot, chat_id, markdown_html(data.get("text", "")), keyboard)

    await _backend_call(update, context, go())


async def _handle_install_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":")
    if len(parts) != 4 or not user or not _is_admin(user.id) or str(user.id) != parts[3]:
        await query.answer(tr("To nie Twoj podglad.", "This is not your preview."), show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if parts[1] != "yes":
        await query.message.reply_text(tr("Anulowano — nic nie zainstalowalem.", "Cancelled — I installed nothing."))
        return

    async def go() -> None:
        result = response_data([f async for f in get_client(user.id).command("install", token=parts[2], execute=True)])
        await _send_html(context.bot, query.message.chat_id, format_listing(tr("Zainstalowano", "Installed"),
                                                                          result.get("text", "")))

    await _backend_call(update, context, go())


async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mcp [dodaj <nazwa> <npx ...|url> | usun <nazwa> | odswiez] -- serwery MCP, z ktorych korzysta Pipe."""
    user_id = await _guard(update)
    if not user_id:
        return
    args = " ".join(context.args or [])

    async def go() -> None:
        chat_id = update.effective_chat.id
        frames = await _collect(context.bot, chat_id, get_client(user_id).command("mcp_servers", args=args))
        error = next((f.get("response", "") for f in frames if f.get("status") == "error"), "")
        data = response_data(frames)
        if error or args:
            await _send_html(context.bot, chat_id, format_listing("MCP", error or data.get("text", "")))
            return
        await _send_html(context.bot, chat_id, format_mcp(data), _inline(mcp_buttons(data, _is_admin(user_id)), per_row=1))

    await _backend_call(update, context, go())


async def _handle_mcp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """Przycisk "Dodaj" z okna /mcp: dodatek z katalogu (klucze sa juz na serwerze)."""
    query = update.callback_query
    user = update.effective_user
    if not user or not _is_admin(user.id):
        await query.answer(tr("Dodatki dodaje tylko administrator.", "Only an administrator adds add-ons."), show_alert=True)
        return
    await query.answer(tr("Dodaje… pierwsze uruchomienie potrafi potrwac minute.", "Adding… the first start can take a minute."))
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    name = data.split(":", 2)[2]

    async def go() -> None:
        frames = await _collect(context.bot, query.message.chat_id,
                                get_client(user.id).command("mcp_servers", args=f"dodaj {name}"))
        error = next((f.get("response", "") for f in frames if f.get("status") == "error"), "")
        text = error or response_data(frames).get("text", "").split("\n\nSerwery MCP")[0].split("\n\nMCP servers")[0]
        await _send_html(context.bot, query.message.chat_id, format_listing("MCP", text))

    await _backend_call(update, context, go())


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/web [wlacz|wylacz|wpusc <kod>|odwolaj <nr>] -- Pipe w przegladarce pod publicznym adresem HTTPS."""
    user_id = await _guard(update)
    if not user_id:
        return
    args = " ".join(context.args or [])

    async def go() -> None:
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        frames = await _collect(context.bot, chat_id, get_client(user_id).command("web", args=args))
        error = next((f.get("response", "") for f in frames if f.get("status") == "error"), "")
        text = error or response_data(frames).get("text", "")
        await _send_html(context.bot, chat_id, format_listing(tr("Pipe w przegladarce", "Pipe in the browser"), text))

    await _backend_call(update, context, go())


async def cmd_aktualizuj(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/aktualizuj [sprawdz] -- aktualizacja samego Pipe (narzedzie pipe_update; potwierdzenie przyciskami)."""
    user_id = await _guard(update)
    if not user_id:
        return
    args = " ".join(context.args or []).strip()
    await _backend_call(update, context, _run_and_reply(
        context, update.effective_chat.id, user_id, get_client(user_id).command("update", args=args)))


async def cmd_mapa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mapa -- diagram infrastruktury bez udzialu LLM (szybko i za darmo)."""
    user_id = await _guard(update)
    if not user_id:
        return
    title = " ".join(context.args or []).strip()

    async def go() -> None:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
        frames = get_client(user_id).command("diagram", args=title)
        got_image = False
        async for frame in frames:
            if frame.get("attachment"):
                got_image = True
                await _send_attachment(context.bot, update.effective_chat.id, frame["attachment"])
            elif frame.get("status") == "error":
                await update.message.reply_text(to_plain_text(frame.get("response", tr("Blad", "Error"))))
            elif frame.get("response"):
                await _send_html(context.bot, update.effective_chat.id, to_telegram_html(frame["response"]))
        if got_image:
            await update.message.reply_text(tr(
                "Chcesz inny widok? Napisz np. \"narysuj przeplyw zapytania do sklepu\" albo \"pokaz tylko bazy danych\".",
                "Want a different view? Write e.g. \"draw how a request reaches the shop\" or \"show only the databases\"."))

    await _backend_call(update, context, go())


async def _command_with_image(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str, args: str,
                              render) -> None:
    """Komenda bez LLM, ktora moze zwrocic obraz (wykres) i dane do pokazania tekstem."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        frames = [frame async for frame in get_client(user_id).command(command, args=args)]
        for frame in frames:
            if frame.get("attachment"):
                await _send_attachment(context.bot, chat_id, frame["attachment"])
        text = render(response_data(frames))
        if text:
            await _send_html(context.bot, chat_id, text)

    await _backend_call(update, context, go())


async def cmd_zmiany(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zmiany [24h|3d] -- co sie zmienilo na serwerze (migawki, bez LLM)."""
    args = " ".join(context.args or []).strip()
    await _command_with_image(update, context, "changes", args,
                              lambda d: format_listing(tr(f"Zmiany na serwerze ({args or '24h'})",
                                                         f"Changes on the server ({args or '24h'})"), d.get("text", "")))


async def cmd_wykres(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/wykres [load|ram|dysk] [24h|7d] -- wykres z historii czuwania."""
    args = " ".join(context.args or []).strip()
    await _command_with_image(update, context, "chart", args,
                              lambda d: "" if d.get("image") else html.escape(d.get("summary", "")))


async def cmd_zdrowie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zdrowie -- certyfikaty, strony, DNS, backupy."""
    await _command_with_image(update, context, "health", "",
                              lambda d: format_listing(tr("Zdrowie uslug", "Service health"), d.get("text", "")))


async def cmd_podatnosci(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/podatnosci -- znane podatnosci z wyjasnieniami (raport bez narzedzi; opisy pisze model raz)."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        data = response_data([f async for f in get_client(user_id).command("vulns")])
        await _send_html(context.bot, update.effective_chat.id, markdown_html(data.get("text", "")))

    await update.message.reply_text(tr("Sprawdzam podatnosci i przygotowuje opisy...", "Checking vulnerabilities and preparing descriptions..."))
    await _backend_call(update, context, go())


async def cmd_audyt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/audyt -- audyt bezpieczenstwa hosta (bez LLM); podatnosci z opisami w drugiej, osobnej wiadomosci."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        data = response_data([frame async for frame in get_client(user_id).command("audit")])
        await _send_html(context.bot, chat_id, format_audit(data))
        vulns = format_audit_vulns(data)
        if vulns:
            await _send_html(context.bot, chat_id, vulns)

    await _backend_call(update, context, go())


async def cmd_raport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/raport -- poranny raport na zadanie."""
    await _command_with_image(update, context, "digest", "", format_digest)


async def cmd_koszt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/koszt -- zuzycie tokenow LLM."""
    await _command_with_image(update, context, "usage", "", lambda d: format_listing(tr("Koszt LLM", "LLM cost"), d.get("text", "")))


def _apply_language(value) -> None:
    """Jezyk bota w trakcie pracy — tg_format.lang() czyta PIPE_LANG przy kazdym wywolaniu."""
    os.environ["PIPE_LANG"] = "pl" if str(value or "").strip().lower().startswith("pl") else "en"


async def _language_changed(app: Application, value) -> None:
    """Zdarzenie `language` z backendu (zmiana z dowolnego kanalu): komunikaty i menu '/' w nowym jezyku."""
    _apply_language(value)
    for user_id in ALLOWED_USER_IDS | VIEWER_USER_IDS:
        await refresh_menu(app.bot, user_id)


async def cmd_jezyk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/jezyk [pl|en] -- jezyk Pipe: instrukcje agenta, raporty i komunikaty (wspolny dla wszystkich kanalow)."""
    args = " ".join(context.args or []).strip()

    def render(data: dict) -> str:
        if args and data.get("lang"):
            _apply_language(data["lang"])
        return html.escape(data.get("text", ""))

    await _command_with_image(update, context, "language", args, render)


async def cmd_yolo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/yolo [on|off] -- zmiany w tej rozmowie bez pytania o TAK (tylko administrator; domyslnie wylaczone)."""
    user_id = await _guard(update)
    if not user_id:
        return
    args = " ".join(context.args or []).strip()

    def render(data: dict) -> str:
        text = html.escape(data.get("text", ""))
        return f"🔴 <b>{text}</b>" if data.get("yolo") and args else text

    await _command_with_image(update, context, "yolo", args, render)


def _providers_keyboard(data: dict, user_id: int) -> InlineKeyboardMarkup | None:
    buttons = [InlineKeyboardButton(name, callback_data=f"prov:{provider_id}:{user_id}")
               for name, provider_id in provider_choices(data) if len(f"prov:{provider_id}:{user_id}") <= 64]
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


# Wybor modelu w toku, per uzytkownik: provider i lista modeli (callback_data niesie tylko indeks — limit 64 bajtow).
# Po restarcie bota lista przepada; przycisk mowi wtedy, zeby wpisac /providerzy ponownie.
_model_picks: dict[int, dict] = {}


def _announce_provider_html(data: dict) -> str:
    active = data.get("active") or {}
    return tr(f"Od teraz odpowiada <b>{html.escape(active.get('name', ''))}</b> · <code>{html.escape(active.get('model', ''))}</code>",
              f"From now on you are talking to <b>{html.escape(active.get('name', ''))}</b> · <code>{html.escape(active.get('model', ''))}</code>")


def _models_keyboard(user_id: int, pick: dict, page: int | None = None) -> InlineKeyboardMarkup:
    buttons, page, pages = model_page(pick["shown"], pick["current"], page)
    rows = [[InlineKeyboardButton(label, callback_data=f"pms:{index}:{user_id}")] for label, index in buttons]
    if pages > 1:
        rows.append([InlineKeyboardButton("◀", callback_data=f"pmp:{(page - 1) % pages}:{user_id}"),
                     InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"pmp:{page}:{user_id}"),
                     InlineKeyboardButton("▶", callback_data=f"pmp:{(page + 1) % pages}:{user_id}")])
    if pick["current"]:
        rows.append([InlineKeyboardButton(tr(f"Zostaw {pick['current']}", f"Keep {pick['current']}")[:MAX_MODEL_LABEL],
                                          callback_data=f"pms:-1:{user_id}")])
    return InlineKeyboardMarkup(rows)


async def _offer_models(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, provider_id: str,
                        query: str = "") -> None:
    """
    Po wyborze providera: modele z jego listy (obecny zaznaczony, "Zostaw ..." go zostawia). Fragment nazwy z komendy
    zaweza liste, a jedno trafienie przelacza od razu. Gdy listy nie da sie pobrac — przelacza z podanym albo obecnym modelem.
    """
    client = get_client(user_id)
    state = await client.data("providers")
    provider = next((p for p in state.get("providers", []) if p.get("id") == provider_id), None)
    if provider is None:
        await context.bot.send_message(chat_id, tr(f"Nie znam providera {provider_id!r}. Lista: /providerzy",
                                                   f"Unknown provider {provider_id!r}. List: /providers"))
        return
    if not provider.get("ready"):
        await context.bot.send_message(chat_id, tr(
            f"{provider.get('name')} nie ma jeszcze klucza API. Dodaj go w pipe web albo w CLI (/providerzy) — "
            "nie wysylaj kluczy przez czat.",
            f"{provider.get('name')} has no API key yet. Add it in pipe web or the CLI (/providers) — "
            "do not send keys through the chat."))
        return
    current = provider.get("model") or provider.get("default_model") or ""
    try:
        models = (await client.data("provider_models", name=provider_id)).get("models", [])
    except RuntimeError as exc:
        models = []
        await context.bot.send_message(chat_id, tr(f"Nie udalo sie pobrac listy modeli: {exc}",
                                                   f"Could not fetch the model list: {exc}"))
    shown = match_models(models, query)
    direct = query if query and (not models or query in models) else shown[0] if query and len(shown) == 1 else ""
    if not models and not direct:
        direct = current                 # bez listy: przelaczenie z modelem, ktory provider juz ma
    if direct:
        data = await client.data("provider_set", name=provider_id, model=direct)
        await context.bot.send_message(chat_id, _announce_provider_html(data), parse_mode=ParseMode.HTML)
        return
    if query and not shown:
        await context.bot.send_message(chat_id, tr(f"Zaden model nie pasuje do {query!r} — oto pelna lista.",
                                                   f"No model matches {query!r} — here is the full list."))
        shown, query = models, ""
    pick = {"provider": provider_id, "name": provider.get("name", provider_id), "shown": shown,
            "current": current if current in models else ""}
    _model_picks[user_id] = pick
    await context.bot.send_message(chat_id, format_model_picker(pick["name"], pick["current"], len(models), len(shown), query),
                                   parse_mode=ParseMode.HTML, reply_markup=_models_keyboard(user_id, pick))


async def cmd_providerzy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /providerzy -- lista providerow LLM z przyciskami; wybor providera prowadzi do wyboru modelu.
    /providerzy <id> [fragment modelu] -- od razu do modeli; /providerzy model -- model obecnego providera. Tylko administrator.
    """
    user_id = await _guard(update)
    if not user_id:
        return
    args = list(context.args or [])

    async def go() -> None:
        client = get_client(user_id)
        data = await client.data("providers")
        admin = _is_admin(user_id) and bool(data.get("can_edit"))
        if args and admin:
            wanted = args[0].lower()
            if wanted in ("model", "modele", "models"):
                wanted = (data.get("active") or {}).get("id", "")
            await _offer_models(context, update.effective_chat.id, user_id, wanted, " ".join(args[1:]))
            return
        await _send_html(context.bot, update.effective_chat.id, format_providers(data, admin=admin),
                         _providers_keyboard(data, user_id) if admin else None)

    await _backend_call(update, context, go())


async def _handle_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """Przyciski: prov (provider -> lista modeli), pmp (strona listy), pms (wybrany model; -1 = zostaw obecny)."""
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":")
    if len(parts) != 3 or not user or str(user.id) != parts[2] or not _is_admin(user.id):
        await query.answer(tr("Providera zmienia tylko administrator.", "Only an administrator can change the provider."),
                           show_alert=True)
        return
    kind, value = parts[0], parts[1]

    if kind == "prov":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _backend_call(update, context, _offer_models(context, query.message.chat_id, user.id, value))
        return

    pick = _model_picks.get(user.id)
    if pick is None:
        await query.answer(tr("Ta lista wygasla — wpisz /providerzy ponownie.", "This list has expired — type /providers again."),
                           show_alert=True)
        return
    if kind == "pmp":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=_models_keyboard(user.id, pick, int(value)))
        except Exception:
            pass                                         # ta sama strona — Telegram odrzuca edycje bez zmian
        return
    try:
        index = int(value)
        model = pick["current"] if index < 0 else pick["shown"][index]
    except (ValueError, IndexError):
        await query.answer()
        return
    await query.answer(tr("Przelaczam...", "Switching..."))
    _model_picks.pop(user.id, None)

    async def go() -> None:
        result = await get_client(user.id).data("provider_set", name=pick["provider"], model=model)
        try:
            await query.edit_message_text(_announce_provider_html(result), parse_mode=ParseMode.HTML)
        except Exception:
            await context.bot.send_message(query.message.chat_id, _announce_provider_html(result), parse_mode=ParseMode.HTML)

    await _backend_call(update, context, go())


async def cmd_incydenty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/incydenty -- pamiec incydentow: rozwiazane alerty z ustaleniami i tym, co pomoglo (bez LLM)."""
    await _command_with_image(update, context, "incidents", "",
                              lambda d: format_listing(tr("Pamiec incydentow", "Incident memory"), d.get("text", "")))


async def cmd_dziennik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dziennik -- zatwierdzone zmiany z kopiami (bez LLM)."""
    await _command_with_image(update, context, "journal", "",
                              lambda d: format_listing(tr("Dziennik zmian", "Change journal"), d.get("text", ""))
                              + tr("\n<i>/cofnij &lt;id&gt; — cofnij wybrana</i>", "\n<i>/undo &lt;id&gt; — undo the chosen one</i>"))


def _undo_keyboard(entry_id: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(tr("Cofnij", "Undo"), callback_data=f"undo:yes:{entry_id}:{user_id}"),
        InlineKeyboardButton(tr("Anuluj", "Cancel"), callback_data=f"undo:no:{entry_id}:{user_id}"),
    ]])


async def cmd_cofnij(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cofnij [id] -- podglad cofniecia i przyciski (bez LLM — dziala tez przy awarii providera)."""
    user_id = await _guard(update)
    if not user_id:
        return
    entry_id = " ".join(context.args or []).strip()
    if not _is_admin(user_id):
        await update.message.reply_text(tr("Cofanie zmian jest dostepne tylko dla administratora.",
                                           "Only an administrator can undo changes."))
        return

    async def go() -> None:
        data = response_data([f async for f in get_client(user_id).command("undo", id=entry_id)])
        await _send_html(context.bot, update.effective_chat.id, to_telegram_html(data.get("preview", "")),
                         _undo_keyboard(data["id"], user_id) if data.get("undoable") else None)

    await _backend_call(update, context, go())


async def _patch_run(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, scope: str) -> None:
    """
    Latanie od razu (bez LLM): backend sam wie, co zalatac. Kroki na zywo w jednej wiadomosci, na koncu krotki
    wynik — surowe wyjscie apt / docker tylko przy bledzie (koncowka). Backend sprzed v0.38 odpowiada podgladem
    z tokenem — wtedy jak dawniej przyciski Zainstaluj / Anuluj.
    """
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    data = response_data(await _collect(context.bot, chat_id, get_client(user_id).command("patch", scope=scope)))
    if data.get("can_execute") and data.get("token"):
        await _send_html(context.bot, chat_id, markdown_html(data.get("text", "")),
                         _patch_keyboard(data.get("scope", scope), data["token"], user_id))
        return
    await _send_html(context.bot, chat_id, format_listing(tr("Latki", "Patches"), data.get("text", "")))


async def cmd_zalataj(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zalataj [obrazy|serwer|serwery|<cel>] -- latki podatnosci od razu, z krokami na zywo (bez LLM)."""
    user_id = await _guard(update)
    if not user_id:
        return
    if not _is_admin(user_id):
        await update.message.reply_text(tr("Latki instaluje tylko administrator — podatnosci pokazuje /audyt.",
                                           "Only an administrator installs patches — /audit shows the vulnerabilities."))
        return
    await _backend_call(update, context, _patch_run(context, update.effective_chat.id, user_id, _patch_scope(context.args)))


async def _handle_patch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """patch:<scope>:<user> — Zalataj przy alercie (od razu); patchx:yes|no:<scope>:<token>:<user> — stary podglad."""
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":")
    owner = parts[-1]
    if not user or not _is_admin(user.id) or str(user.id) != owner or len(parts) not in (3, 5):
        await query.answer(tr("Latki instaluje tylko administrator.", "Only an administrator installs patches."), show_alert=True)
        return
    if parts[0] == "patch":
        await query.answer(tr("Latam...", "Patching..."))
        try:
            await query.edit_message_reply_markup(reply_markup=_alert_keyboard_without_patch(query))
        except Exception:
            pass
        await _backend_call(update, context, _patch_run(context, query.message.chat_id, user.id, parts[1]))
        return
    await query.answer(tr("Instaluje...", "Installing...") if parts[1] == "yes" else tr("Anulowano", "Cancelled"))
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if parts[1] != "yes":
        await query.message.reply_text(tr("Anulowano — nic nie zainstalowalem.", "Cancelled — I installed nothing."))
        return

    async def go() -> None:
        # instalacja trwa minuty: kroki bezpiecznika na zywo w jednej wiadomosci, na koniec krotki wynik
        # (bez surowego wyjscia apt / docker build — to jest w pipe web i przy bledzie jako ostatnie linie)
        chat_id = query.message.chat_id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        responses = await _collect(context.bot, chat_id, get_client(user.id).command(
            "patch", scope=parts[2], patch_token=parts[3], execute=True))
        result = response_data(responses)
        await _send_html(context.bot, chat_id, format_listing(tr("Latki", "Patches"), result.get("text", "")))

    await _backend_call(update, context, go())


async def _handle_feed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """fa:<id>:<indeks> — przycisk wiadomosci ze skrzynki; backend wie, co zrobic (i sprawdza role)."""
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":")
    if not user or not _is_allowed(user.id) or len(parts) != 3 or not parts[2].isdigit():
        await query.answer()
        return
    await query.answer(tr("Robie...", "On it..."))
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    async def go() -> None:
        chat_id = query.message.chat_id
        async with _user_locks.setdefault(user.id, asyncio.Lock()):
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            frames = await _collect(context.bot, chat_id, get_client(user.id).command(
                "feed_act", id=parts[1], action=int(parts[2])))
            text, needs_confirm = collect_response_text(frames)
            result = next((f.get("data") for f in reversed(frames) if isinstance(f.get("data"), dict)), None)
            if text:
                markup = _confirm_keyboard(user.id) if needs_confirm and _is_admin(user.id) else None
                await _send_html(context.bot, chat_id, to_telegram_html(text), markup)
            elif result and result.get("text"):
                await _send_html(context.bot, chat_id, format_listing("", result["text"]))
            elif result and result.get("done"):
                await query.message.reply_text("✓ " + str(result["done"]))

    await _backend_call(update, context, go())


async def _handle_project_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """pj:<logs|stop|start>:<nazwa> — projekt z warsztatu (bez LLM); zmiany tylko administrator."""
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":", 2)
    if not user or not _is_allowed(user.id) or len(parts) != 3 or parts[1] not in ("logs", "stop", "start"):
        await query.answer()
        return
    if parts[1] != "logs" and not _is_admin(user.id):
        await query.answer(tr("To moze zrobic tylko administrator.", "Only an administrator can do this."), show_alert=True)
        return
    await query.answer()

    async def go() -> None:
        result = response_data([f async for f in get_client(user.id).command("projects", args=parts[1], name=parts[2])])
        title = tr(f"Logi {parts[2]}", f"Logs of {parts[2]}") if parts[1] == "logs" else parts[2]
        await _send_html(context.bot, query.message.chat_id, format_pre(title, result.get("text", "")[-3500:]))

    await _backend_call(update, context, go())


async def _handle_prefs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """pf:<level|quiet|per_day>:<wartosc> — zmiana ustawien powiadomien (administrator), wiadomosc odswiezona."""
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":", 2)
    if not user or not _is_admin(user.id) or len(parts) != 3 or parts[1] not in ("level", "quiet", "per_day"):
        await query.answer(tr("Ustawienia zmienia administrator.", "An administrator changes the settings."), show_alert=True)
        return
    value: object = int(parts[2]) if parts[1] == "per_day" and parts[2].isdigit() else parts[2]
    try:
        result = response_data([f async for f in get_client(user.id).command("prefs", **{parts[1]: value})])
    except Exception as exc:
        await query.answer(str(exc)[:180], show_alert=True)
        return
    await query.answer(tr("Zapisano", "Saved"))
    rows = prefs_buttons(result)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=cb) for label, cb in row] for row in rows])
    try:
        await query.edit_message_text(format_prefs(result), parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:
        pass


async def _handle_undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":")
    if len(parts) != 4 or not user or not _is_admin(user.id) or str(user.id) != parts[3]:
        await query.answer(tr("Nie mozesz cofac cudzych operacji.", "You cannot undo someone else's operations."), show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if parts[1] != "yes":
        await query.message.reply_text(tr("Anulowano — nic nie zmienilem.", "Cancelled — I changed nothing."))
        return

    async def go() -> None:
        result = response_data([f async for f in get_client(user.id).command("undo", id=parts[2], execute=True)])
        await _send_html(context.bot, query.message.chat_id, format_listing(tr("Cofniecie", "Undo"), result.get("text", "")))

    await _backend_call(update, context, go())


async def _handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":")
    if len(parts) != 3 or not user or not _is_admin(user.id):
        await query.answer(tr("Zgody zatwierdza tylko administrator.", "Only an administrator can approve."), show_alert=True)
        return
    await query.answer(tr("Wykonuje...", "Running...") if parts[1] == "yes" else tr("Odrzucono", "Rejected"))
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    async def go() -> None:
        result = response_data([f async for f in get_client(user.id).command(
            "approve", id=parts[2], decision=parts[1] == "yes")])
        await _send_html(context.bot, query.message.chat_id, format_pre(tr(f"Zgoda {parts[2]}", f"Approval {parts[2]}"), result.get("text", "")))

    await _backend_call(update, context, go())


async def cmd_zgody(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zgody -- operacje agentow MCP czekajace na zgode (przyciski tylko dla administratora)."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        pending = (await get_client(user_id).data("approvals")).get("pending", [])
        if not pending:
            await update.message.reply_text(tr("Nic nie czeka na zgode.", "Nothing is waiting for approval."))
        for item in pending:
            await _send_html(context.bot, update.effective_chat.id, format_approval(item),
                             _approval_keyboard(item["id"]) if _is_admin(user_id) else None)

    await _backend_call(update, context, go())


async def cmd_historia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/historia -- ostatnie wpisy z audit logu (bez LLM)."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        entries = (await get_client(user_id).data("history")).get("entries", [])
        body = "\n".join(entries) or tr("(pusto)", "(empty)")
        await _send_html(context.bot, update.effective_chat.id,
                         f"<b>Audit log</b>\n<pre>{html.escape(body)}</pre>")

    await _backend_call(update, context, go())


async def _show_data(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str, render, **fields) -> None:
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        data = await get_client(user_id).data(command, **fields)
        await _send_html(context.bot, update.effective_chat.id, render(data))

    await _backend_call(update, context, go())


async def cmd_katalogi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_data(update, context, "directory", lambda d: format_directory(d.get("entries", [])))


async def cmd_alerty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_data(update, context, "alerts", lambda d: format_alerts(d) + subscription_note())


async def cmd_cele(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_data(update, context, "targets", lambda d: format_list(
        tr("Zdalne cele", "Remote targets"), d.get("targets", []),
        tr("Brak. Napisz np. <i>\"dodaj serwer 10.0.0.5 jako web-2 (ssh, root)\"</i>.",
           "None. Write e.g. <i>\"add the server 10.0.0.5 as web-2 (ssh, root)\"</i>.")))


async def cmd_rutyny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_data(update, context, "routines", lambda d: format_list(
        tr("Rutyny", "Routines"), d.get("routines", []),
        tr("Brak. Napisz np. <i>\"codziennie o 7 sprawdzaj backupy i certyfikaty\"</i>.",
           "None. Write e.g. <i>\"check the backups and certificates every day at 7\"</i>.")))


async def cmd_przypomnienia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/przypomnienia -- lista; /przypomnienia anuluj <id> -- anulowanie (bez LLM)."""
    args = [a.lstrip("#") for a in (context.args or [])]
    cancel = args[1] if len(args) >= 2 and args[0].lower() in ("anuluj", "cancel", "usun", "remove") else ""

    def render(data: dict) -> str:
        head = tr(f"Anulowano #{data['cancelled']}.\n", f"Cancelled #{data['cancelled']}.\n") if data.get("cancelled") else ""
        hint = tr("\n<i>Napisz np. \"przypomnij mi za 2 godziny o backupie\". Anulowanie: /przypomnienia anuluj &lt;id&gt;</i>",
                  "\n<i>Write e.g. \"remind me in 2 hours about the backup\". Cancel: /reminders cancel &lt;id&gt;</i>")
        return html.escape(head) + format_listing("", data.get("text", "")) + hint \
            + subscription_note()

    await _show_data(update, context, "reminders", render, cancel=cancel)


async def cmd_nowa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nowa rozmowa: backend usuwa historie tej sesji, wiedza o serwerze (pamiec, alerty) zostaje w prompcie."""
    def render(data: dict) -> str:
        head = tr("Zaczynamy nowa rozmowe.", "Starting a new conversation.") if data.get("cleared") \
            else tr("Rozmowa juz byla pusta.", "The conversation was already empty.")
        return head + tr(" Pamietam to, co trwale: notatki o serwerze, katalogi, skille, Twoj styl i alerty.",
                         " I keep what lasts: the server notes, directories, skills, your style and the alerts.")

    await _show_data(update, context, "new_chat", render)


async def cmd_vibe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = " ".join(context.args or []).strip()

    def render(data: dict) -> str:
        if "reset" in data:
            return tr("Wyczyscilem notatke o Twoim stylu. Zaczynam obserwowac od nowa.",
                      "I cleared the note about your style. I start observing afresh.") if data["reset"] \
                else tr("Nie mialem jeszcze notatki o Twoim stylu.", "I had no note about your style yet.")
        content = (data.get("content") or "").strip()
        if not content:
            return tr("Jeszcze nie znam Twojego stylu — ucze sie z rozmow. Mozesz tez powiedziec wprost, "
                      "np. <i>\"odpowiadaj krocej i bez wstepow\"</i>.",
                      "I do not know your style yet — I learn from our conversations. You can also say it outright, "
                      "e.g. <i>\"answer shorter and without preambles\"</i>.")
        return (tr("<b>VIBE</b> — tak sie do Ciebie dopasowuje:\n", "<b>VIBE</b> — this is how I adapt to you:\n")
                + f"<pre>{html.escape(content)}</pre>\n" + tr("<i>/vibe reset — wyczysc</i>", "<i>/vibe reset — clear it</i>"))

    await _show_data(update, context, "vibe", render, args=args)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Obsluguje dowolna wiadomosc tekstowa."""
    user_id = await _guard(update)
    if not user_id:
        return  # Milcz dla nieautoryzowanych

    text = update.message.text or ""
    if not text.strip():
        return

    # Przycisk klawiatury (Stan serwera, Co nowego, Projekty, Pomoc) — ta sama droga co komenda, bez LLM
    button = keyboard_command(text)
    if button:
        handler = {"status": cmd_status, "nowosci": cmd_nowosci, "projekty": cmd_projekty, "pomoc": cmd_pomoc,
                   "skille": cmd_skille}.get(button)
        if handler is not None:
            await handler(update, context)
            return

    # Odpowiedz na pytanie z /start "co chcesz robic?" — kilka slow wybiera widok, zamiast trafic do agenta
    if context.user_data.pop("view_ask", False) and _is_admin(user_id):
        if await _set_view(context, update.effective_chat.id, user_id, text):
            return
        context.user_data["view_ask"] = True
        return

    # Rate limiting: max 1 wiadomosc na sekunde od uzytkownika
    now = time.monotonic()
    if now - _last_message_time.get(user_id, 0.0) < 1.0:
        return  # Ignoruj spam po cichu
    _last_message_time[user_id] = now

    await _backend_call(update, context, _run_and_reply(
        context, update.effective_chat.id, user_id, get_client(user_id).chat(text)))


_last_message_time: dict[int, float] = {}
MAX_VOICE_BYTES = 10 * 1024 * 1024
# Limit backendu na plik (core/attachments.py); Bot API i tak nie pobierze wiecej niz 20 MB.
MAX_FILE_BYTES = 10 * 1024 * 1024
ALBUM_WAIT = 1.2           # album (kilka zdjec naraz) przychodzi jako osobne wiadomosci — zbieramy je razem


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wiadomosc glosowa -> transkrypcja w backendzie -> zwykla wiadomosc do agenta."""
    user_id = await _guard(update)
    if not user_id:
        return
    media = update.message.voice or update.message.audio
    if media is None:
        return
    if (media.file_size or 0) > MAX_VOICE_BYTES:
        await update.message.reply_text(tr("Nagranie jest za dlugie — nagraj krotsze albo napisz.",
                                           "The recording is too long — record a shorter one or write."))
        return

    async def go() -> None:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        file = await media.get_file()
        data = bytes(await file.download_as_bytearray())
        name = "voice.ogg" if update.message.voice else (getattr(media, "file_name", None) or "audio.mp3")
        client = get_client(user_id)
        try:
            text = response_data([f async for f in client.command(
                "transcribe", audio=base64.b64encode(data).decode("ascii"), filename=name)]).get("text", "")
        except RuntimeError as exc:
            await update.message.reply_text(str(exc).replace("[BLAD] ", ""))
            return
        await update.message.reply_text(tr(f"Uslyszalem: {text}", f"I heard: {text}"))
        await _run_and_reply(context, update.effective_chat.id, user_id, client.chat(text))

    await _backend_call(update, context, go())


_albums: dict[str, dict] = {}


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Zdjecie albo dokument -> zalacznik wiadomosci do agenta; podpis to tresc wiadomosci."""
    user_id = await _guard(update)
    if not user_id:
        return
    message = update.message
    if message.photo:
        media, name, mime = message.photo[-1], f"photo-{message.message_id}.jpg", "image/jpeg"
    elif message.document:
        media = message.document
        name = media.file_name or f"file-{message.message_id}"
        mime = media.mime_type or "application/octet-stream"
    else:
        return
    if (media.file_size or 0) > MAX_FILE_BYTES:
        await message.reply_text(tr(f"Plik jest za duzy (limit {MAX_FILE_BYTES // (1024 * 1024)} MB).",
                                    f"The file is too large (limit {MAX_FILE_BYTES // (1024 * 1024)} MB)."))
        return

    async def download() -> dict:
        file = await media.get_file()
        data = bytes(await file.download_as_bytearray())
        return {"name": name, "mime": mime, "data": base64.b64encode(data).decode("ascii")}

    group = message.media_group_id
    if not group:
        async def go() -> None:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            attachment = await download()
            await _run_and_reply(context, update.effective_chat.id, user_id,
                                 get_client(user_id).chat(message.caption or "", [attachment]))

        await _backend_call(update, context, go())
        return

    # Album: pierwsza wiadomosc czeka chwile na reszte, potem wszystko idzie jedna wiadomoscia do agenta.
    key = f"{user_id}:{group}"
    album = _albums.get(key)
    if album is not None:
        album["items"].append(download())
        if message.caption:
            album["caption"] = message.caption
        return
    album = _albums[key] = {"items": [download()], "caption": message.caption or ""}

    async def go_album() -> None:
        await asyncio.sleep(ALBUM_WAIT)
        _albums.pop(key, None)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        attachments = list(await asyncio.gather(*album["items"]))
        await _run_and_reply(context, update.effective_chat.id, user_id,
                             get_client(user_id).chat(album["caption"], attachments))

    await _backend_call(update, context, go_album())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Przyciski inline: potwierdzenia TAK/NIE i "Zbadaj" przy alertach."""
    query = update.callback_query
    user = update.effective_user
    data = query.data or ""

    if data.startswith("undo:"):
        await _handle_undo_callback(update, context, data)
        return

    if data.startswith(("patch:", "patchx:")):
        await _handle_patch_callback(update, context, data)
        return

    if data.startswith("appr:"):
        await _handle_approval_callback(update, context, data)
        return

    if data.startswith(("prov:", "pmp:", "pms:")):
        await _handle_provider_callback(update, context, data)
        return

    if data.startswith("fa:"):
        await _handle_feed_callback(update, context, data)
        return

    if data.startswith("pj:"):
        await _handle_project_callback(update, context, data)
        return

    if data.startswith("inst:"):
        await _handle_install_callback(update, context, data)
        return

    if data.startswith("mcp:add:"):
        await _handle_mcp_callback(update, context, data)
        return

    if data.startswith("pf:"):
        await _handle_prefs_callback(update, context, data)
        return

    if data.startswith("view:"):
        if not user or not _is_admin(user.id):
            await query.answer(tr("Widok zmienia tylko administrator.", "Only an administrator can change the view."),
                               show_alert=True)
            return
        await query.answer()
        context.user_data.pop("view_ask", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _backend_call(update, context, _set_view(context, query.message.chat_id, user.id, data.split(":", 1)[1]))
        return

    if data.startswith("investigate:"):
        if not user or not _is_allowed(user.id):
            await query.answer()
            return
        await query.answer(tr("Badam...", "Investigating..."))
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        frames = get_client(user.id).command("investigate", id=data.split(":", 1)[1])
        await _backend_call(update, context, _run_and_reply(context, query.message.chat_id, user.id, frames))
        return

    await query.answer()
    # Format: "confirm:yes|no:user_id"
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "confirm":
        return
    try:
        requesting_user_id = int(parts[2])
    except ValueError:
        return

    # Tylko oryginalny uzytkownik moze potwierdzic — i tylko administrator
    if not user or user.id != requesting_user_id or not _is_admin(user.id):
        await query.answer(tr("Nie mozesz potwierdzac cudzych operacji.",
                              "You cannot confirm someone else's operations."), show_alert=True)
        return

    confirmed = parts[1] == "yes"
    # decyzja dopisana do tej samej wiadomosci (bez przyciskow) — prosba i odpowiedz zostaja razem w historii
    verdict = (tr("✅ <b>Zatwierdzone</b> — wykonuje.", "✅ <b>Approved</b> — running.") if confirmed
               else tr("✖️ <b>Odrzucone</b> — nic nie zmieniam.", "✖️ <b>Rejected</b> — nothing changes."))
    try:
        await query.edit_message_text(f"{query.message.text_html}\n\n{verdict}", parse_mode=ParseMode.HTML, reply_markup=None)
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(verdict, parse_mode=ParseMode.HTML)
    await _backend_call(update, context, _run_and_reply(
        context, query.message.chat_id, user.id, get_client(user.id).confirm(confirmed)))


# --- Komendy pamieci agenta ---

async def cmd_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/server -- pokazuje SERVER.md; gdy go nie ma albo z 'aktualizuj' -- zleca skan serwera."""
    user_id = await _guard(update)
    if not user_id:
        return

    client = get_client(user_id)
    mode = " ".join(context.args or []).strip().lower()

    async def go() -> None:
        content = "" if mode in SCAN_WORDS else (await client.data("server_md")).get("content", "")
        if content.strip():
            await _send_html(context.bot, update.effective_chat.id, to_telegram_html(
                "<b>SERVER.md</b>\n\n" + content + tr("\n\n<i>/server aktualizuj -- zbadaj serwer ponownie</i>",
                                                         "\n\n<i>/server update -- explore the server again</i>")))
            return
        await update.message.reply_text(
            tr("Badam serwer i aktualizuje SERVER.md...", "Exploring the server and updating SERVER.md...")
            if mode in SCAN_WORDS
            else tr("SERVER.md jeszcze nie istnieje. Badam serwer i tworze go...",
                    "SERVER.md does not exist yet. Exploring the server to create it...")
        )
        await _run_and_reply(context, update.effective_chat.id, user_id, client.command("scan_server"))

    await _backend_call(update, context, go())


async def cmd_skille(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skille -- lista zapisanych skilli z ich komendami."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        await _send_html(context.bot, update.effective_chat.id,
                         format_skill_list(await get_client(user_id).list_skills()))

    await _backend_call(update, context, go())


async def cmd_pomoc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pomoc -- lista komend."""
    user_id = await _guard(update)
    if not user_id:
        return
    try:
        skills = await get_client(user_id).list_skills()
    except Exception:
        skills = []
    await _send_html(context.bot, update.effective_chat.id, format_help(skills, _view_state["view"]))


async def handle_other_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pozostale komendy: skill uruchamiany wlasna komenda albo podpowiedz dla nieznanej."""
    user_id = await _guard(update)
    if not user_id:
        return

    name, args = parse_command(update.message.text or "")
    client = get_client(user_id)

    async def go() -> None:
        entry = next((s for s in await client.list_skills() if s.get("command") == name), None)
        if entry is None:
            await update.message.reply_text(
                tr(f"Nieznana komenda /{html.escape(name)}. Lista komend: /pomoc, skille: /skille",
                   f"Unknown command /{html.escape(name)}. Command list: /help, skills: /skills")
            )
            return
        await _run_and_reply(context, update.effective_chat.id, user_id,
                             client.command("run_skill", name=entry["name"], args=args))

    await _backend_call(update, context, go())


# --- Czuwanie: alerty i raporty rutyn ---

def _approval_keyboard(approval_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(tr("Zatwierdz", "Approve"), callback_data=f"appr:yes:{approval_id}"),
        InlineKeyboardButton(tr("Odrzuc", "Reject"), callback_data=f"appr:no:{approval_id}"),
    ]])


async def _broadcast(app: Application, event: dict) -> None:
    """Rozsyla zdarzenie czuwania do wszystkich dozwolonych uzytkownikow."""
    kind = event.get("type")
    if kind == "approval":
        # Zgody widza i zatwierdzaja tylko administratorzy
        for user_id in sorted(ALLOWED_USER_IDS):
            try:
                await _send_html(app.bot, user_id, format_approval(event), _approval_keyboard(event["id"]))
            except Exception as exc:
                print(f"[Pipe Telegram] Nie wyslano zgody do {user_id}: {exc}", flush=True)
        return
    if kind == "login":
        # Ktos przed ekranem pipe web prosi o dostep — wpuszczaja tylko administratorzy (przyciski z backendu)
        for user_id in sorted(ALLOWED_USER_IDS):
            try:
                await _send_html(app.bot, user_id, format_feed_item(event), _inline(feed_buttons(event, True)))
            except Exception as exc:
                print(f"[Pipe Telegram] Nie wyslano prosby o dostep do {user_id}: {exc}", flush=True)
        return
    if kind == "reminder":
        for user_id in reminder_recipients(event, ALLOWED_USER_IDS, VIEWER_USER_IDS):
            try:
                await _send_html(app.bot, user_id, format_reminder(event))
            except Exception as exc:
                print(f"[Pipe Telegram] Nie wyslano przypomnienia do {user_id}: {exc}", flush=True)
        return
    if event.get("push") is False:
        return          # zostaje w skrzynce (/nowosci) bez powiadomienia: poziom, cisza nocna albo limit podpowiedzi
    if kind == "alert":
        text = format_alert(event)
    elif kind == "routine":
        text = format_routine(event)
    elif kind in ("digest", "welcome"):
        text = format_digest(event)
    elif kind == "investigation":
        text = format_investigation(event)
    elif kind in ("insight", "notice"):
        text = format_feed_item(event)
    else:
        return
    for user_id in sorted(ALLOWED_USER_IDS | VIEWER_USER_IDS):
        if event.get("feed_id"):
            markup = _inline(feed_buttons(event, _is_admin(user_id)))       # przyciski zbudowane w backendzie
        else:
            markup = _alert_keyboard(event, user_id) if kind == "alert" else None   # backend sprzed skrzynki
        try:
            await _send_html(app.bot, user_id, text, markup)
            if event.get("attachment"):
                await _send_attachment(app.bot, user_id, event["attachment"])
        except Exception as exc:
            print(f"[Pipe Telegram] Nie wyslano zdarzenia do {user_id}: {exc}", flush=True)


# Zdarzenia czuwania, ktore TELEGRAM_ALERTS=0 wycisza. Przypomnienia i prosby o zgode przychodza zawsze —
# uzytkownik sam o nie poprosil albo czeka na nie zewnetrzny agent.
WATCH_EVENT_TYPES = frozenset({"alert", "routine", "digest", "welcome", "investigation", "insight", "notice"})
# Stan kanalu zdarzen — pokazywany w /przypomnienia i /alerty, zeby cisza nie byla zagadka.
_subscription: dict = {"connected": False, "error": "", "since": 0.0}


def _subscription_down(error: str) -> None:
    if _subscription["connected"] or _subscription["error"] != error:
        print(f"[Pipe Telegram] Kanal zdarzen nieaktywny: {error}", flush=True)
    _subscription.update(connected=False, error=error)


def subscription_note() -> str:
    """Ostrzezenie HTML, gdy bot nie odbiera zdarzen backendu (pusty napis, gdy kanal dziala)."""
    if _subscription["connected"]:
        return ""
    reason = html.escape(_subscription["error"] or tr("lacze sie z backendem", "connecting to the backend"))
    return tr(f"\n\n<b>Uwaga:</b> bot nie odbiera teraz powiadomien z backendu ({reason}). Przypomnienia i alerty "
              "dotra, gdy polaczenie wroci. Sprawdz logi bota i zgodnosc AGENT_TOKEN w obu plikach .env.",
              f"\n\n<b>Warning:</b> the bot is not receiving notifications from the backend right now ({reason}). "
              "Reminders and alerts will arrive when the connection is back. Check the bot logs and that AGENT_TOKEN "
              "matches in both .env files.")


# Po zerwaniu subskrypcji backend dosyla zdarzenia nowsze niz `since`. Swiezo uruchomiony bot (np. po /aktualizuj,
# ktora wymienia tez jego kontener) pyta o ostatnie REPLAY_ON_START s, ale z tamtego okresu bierze tylko przypomnienia
# (wynik aktualizacji) — alertow sprzed startu poprzedni proces mogl juz wyslac.
REPLAY_ON_START = 180.0
_BOT_START = time.time()
_last_event_ts = 0.0


def _replayed_before_start(event: dict) -> bool:
    """Zdarzenie sprzed startu bota, ktore nie jest przypomnieniem — pomijane (mogly juz dojsc)."""
    try:
        ts = float(event.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < ts < _BOT_START and event.get("type") != "reminder"


async def alerts_loop(app: Application) -> None:
    """Trwala subskrypcja zdarzen backendu z ponawianiem polaczenia."""
    global _last_event_ts
    backoff = 2.0
    while True:
        try:
            reader, writer = await asyncio.open_unix_connection(AGENT_SOCKET, limit=READ_LIMIT)
            request = {"command": "subscribe", "session_id": "telegram-alerts", "interface": "telegram:alerts",
                       "since": _last_event_ts or _BOT_START - REPLAY_ON_START}
            if AGENT_TOKEN:
                request["token"] = AGENT_TOKEN
            writer.write((json.dumps(request) + "\n").encode("utf-8"))
            await writer.drain()
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if frame.get("status") == "error":
                    _subscription_down(tr("backend odrzucil subskrypcje: ", "the backend rejected the subscription: ")
                                       + str(frame.get("response", "")))
                    break
                event = frame.get("event") or {}
                try:
                    _last_event_ts = max(_last_event_ts, float(event.get("ts") or 0))
                except (TypeError, ValueError):
                    pass
                if _replayed_before_start(event):
                    continue
                if event.get("type") == "subscribed":
                    print("[Pipe Telegram] Subskrypcja alertow aktywna.", flush=True)
                    _subscription.update(connected=True, error="", since=time.time())
                    backoff = 2.0
                    if event.get("lang_chosen"):      # jezyk wybrany komenda /jezyk obowiazuje tez bota
                        await _language_changed(app, event.get("lang"))
                    if "view" in event:
                        _view_state["chosen"] = bool(event.get("view_chosen"))
                        if event.get("view_chosen"):
                            await _view_changed(app, event.get("view"))
                elif event.get("type") == "language":
                    await _language_changed(app, event.get("lang"))
                elif event.get("type") == "view":
                    await _view_changed(app, event.get("view"))
                elif event and (ALERTS_ENABLED or event.get("type") not in WATCH_EVENT_TYPES):
                    try:
                        await _broadcast(app, event)
                    except Exception as exc:          # jedno zle zdarzenie nie moze zerwac kanalu
                        print(f"[Pipe Telegram] Nie rozeslano zdarzenia {event.get('type')}: {exc}", flush=True)
            if _subscription["connected"]:
                _subscription_down(tr("backend zamknal polaczenie (restart?)", "the backend closed the connection (restart?)"))
            writer.close()
        except (OSError, ConnectionError) as exc:
            _subscription_down(tr(f"brak polaczenia z {AGENT_SOCKET}: {exc}", f"cannot connect to {AGENT_SOCKET}: {exc}"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _subscription_down(tr(f"blad subskrypcji: {exc}", f"subscription error: {exc}"))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


# --- Menu "/" ---

_menu_cache: dict[int, list[tuple[str, str]]] = {}


async def refresh_menu(bot, user_id: int) -> None:
    """
    Ustawia menu '/' dla czatu jednego dozwolonego uzytkownika. Zakres czatu,
    nie globalny -- inaczej opisy skilli widzialby kazdy, kto otworzy bota.
    """
    try:
        menu = build_menu(await get_client(user_id).list_skills(), _view_state["view"])
        if _menu_cache.get(user_id) == menu:
            return
        await bot.set_my_commands([BotCommand(c, d) for c, d in menu], scope=BotCommandScopeChat(user_id))
        _menu_cache[user_id] = menu
    except Exception as exc:
        print(f"[Pipe Telegram] Nie ustawiono menu dla {user_id}: {exc}", flush=True)


async def refresh_menu_after_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Po kazdej obsluzonej wiadomosci -- nowy skill pojawia sie w menu od razu."""
    user = update.effective_user
    if user and _is_allowed(user.id):
        await refresh_menu(context.bot, user.id)


async def _post_init(app: Application) -> None:
    for user_id in ALLOWED_USER_IDS | VIEWER_USER_IDS:
        await refresh_menu(app.bot, user_id)
    if ALLOWED_USER_IDS or VIEWER_USER_IDS:
        # Zawsze: TELEGRAM_ALERTS=0 wycisza tylko alerty czuwania, przypomnienia musza dochodzic.
        # Referencja w bot_data — zadanie bez referencji moze zostac usuniete przez garbage collector.
        app.bot_data["alerts_task"] = asyncio.create_task(alerts_loop(app), name="pipe-alerts")


# --- Main ---

def main() -> None:
    """Punkt wejscia Telegram bota."""
    if not BOT_TOKEN:
        print(tr("Blad: TELEGRAM_BOT_TOKEN nie jest ustawiony w .env",
                 "Error: TELEGRAM_BOT_TOKEN is not set in .env"), file=sys.stderr)
        sys.exit(1)

    if not ALLOWED_USER_IDS:
        print(
            tr("Ostrzezenie: TELEGRAM_ALLOWED_USER_IDS jest pusty -- nikt nie bedzie mogl uzywac bota.",
               "Warning: TELEGRAM_ALLOWED_USER_IDS is empty -- nobody will be able to use the bot."),
            file=sys.stderr,
        )

    print("[Pipe Telegram] Uruchamiam bota...")
    print(f"[Pipe Telegram] Dozwoleni uzytkownicy: {ALLOWED_USER_IDS}")
    if VIEWER_USER_IDS:
        print(f"[Pipe Telegram] Tylko odczyt: {VIEWER_USER_IDS}"
              + ("" if AGENT_VIEWER_TOKEN else " — UWAGA: brak AGENT_VIEWER_TOKEN, backend ich odrzuci"))
    print(f"[Pipe Telegram] Backend socket: {AGENT_SOCKET}")
    print(f"[Pipe Telegram] Alerty czuwania: {'tak' if ALERTS_ENABLED else 'nie (TELEGRAM_ALERTS=0; przypomnienia i zgody dochodza)'}")

    # concurrent_updates: dluga operacja (workery) jednego uzytkownika nie blokuje innych
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    for polish, handler in (("nowosci", cmd_nowosci), ("projekty", cmd_projekty), ("powiadomienia", cmd_powiadomienia),
                            ("sprzataj", cmd_sprzataj), ("mcp", cmd_mcp), ("pobierz", cmd_pobierz), ("web", cmd_web)):
        app.add_handler(CommandHandler(command_names(polish), handler))
    # Polskie nazwy i ich angielskie aliasy (/raport i /report) dzialaja w obu jezykach.
    for polish, handler in (
        ("raport", cmd_raport), ("audyt", cmd_audyt), ("zmiany", cmd_zmiany), ("wykres", cmd_wykres),
        ("zdrowie", cmd_zdrowie), ("koszt", cmd_koszt), ("cofnij", cmd_cofnij), ("dziennik", cmd_dziennik),
        ("zgody", cmd_zgody), ("mapa", cmd_mapa), ("historia", cmd_historia),
        ("server", cmd_server), ("katalogi", cmd_katalogi), ("skille", cmd_skille), ("alerty", cmd_alerty),
        ("cele", cmd_cele), ("rutyny", cmd_rutyny), ("vibe", cmd_vibe), ("nowa", cmd_nowa), ("pomoc", cmd_pomoc),
        ("incydenty", cmd_incydenty), ("przypomnienia", cmd_przypomnienia), ("aktualizuj", cmd_aktualizuj),
        ("jezyk", cmd_jezyk), ("widok", cmd_widok), ("providerzy", cmd_providerzy), ("yolo", cmd_yolo), ("zalataj", cmd_zalataj), ("podatnosci", cmd_podatnosci),
    ):
        app.add_handler(CommandHandler(command_names(polish), handler))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))
    # Po wbudowanych -- skille jako komendy i odpowiedz na nieznana komende
    app.add_handler(MessageHandler(filters.COMMAND, handle_other_command))
    # Grupa 1 dziala po obsludze kazdej wiadomosci
    app.add_handler(TypeHandler(Update, refresh_menu_after_update), group=1)

    print("[Pipe Telegram] Gotowy. Ctrl+C aby zatrzymac.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
