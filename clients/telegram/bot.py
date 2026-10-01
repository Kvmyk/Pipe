"""
Telegram Bot -- interfejs Telegram dla Pipe (agent do zarzadzania serwerami).

Laczy sie z backendem przez Unix socket.
Uzywa python-telegram-bot w trybie async.

Pipe v0.14.0

Funkcje:
  - Whitelist uzytkownikow (TELEGRAM_ALLOWED_USER_IDS), osobna sesja per user_id
  - Diagramy (np. /mapa albo "pokaz architekture") przychodza jako zdjecia
  - Postep workerow na zywo w jednej, aktualizowanej wiadomosci
  - Czuwanie: alerty i raporty rutyn przychodza same, z przyciskiem "Zbadaj"
  - InlineKeyboard dla potwierdzen (TAK / NIE)
  - Komendy: /status /raport /zmiany /wykres /zdrowie /mapa /server /katalogi /skille /alerty /rutyny
    /cele /vibe /koszt /dziennik /cofnij /historia /pomoc, kazdy skill ma wlasna komende /<nazwa>
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
    format_alert,
    format_alerts,
    format_approval,
    format_audit,
    format_digest,
    format_directory,
    format_help,
    format_investigation,
    format_list,
    format_pre,
    format_routine,
    format_skill_list,
    parse_command,
    progress_text,
    response_data,
    split_message,
    to_plain_text,
    to_telegram_html,
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

    def chat(self, message: str) -> AsyncIterator[dict]:
        return self.stream({"message": message, "session_id": self.session_id, "interface": self.interface})

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
            InlineKeyboardButton("TAK", callback_data=f"confirm:yes:{user_id}"),
            InlineKeyboardButton("NIE", callback_data=f"confirm:no:{user_id}"),
        ]
    ])


def _investigate_keyboard(alert_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Zbadaj", callback_data=f"investigate:{alert_id}")]])


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
                await bot.send_message(chat_id, "Blad formatowania odpowiedzi. Sprobuj ponownie.", reply_markup=markup)


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


async def _consume(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, frames: AsyncIterator[dict]) -> None:
    bot = context.bot
    responses: list[dict] = []
    progress_lines: list[str] = []
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

    await bot.send_chat_action(chat_id=chat_id, action="typing")
    async for frame in frames:
        if frame.get("attachment"):
            await bot.send_chat_action(chat_id=chat_id, action="upload_photo")
            await _send_attachment(bot, chat_id, frame["attachment"])
        elif (frame.get("event") or {}).get("type") == "progress":
            progress_lines.append(frame["event"].get("text", ""))
            await show_progress()
        else:
            responses.append(frame)
    await show_progress(force=True)

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
        await update.effective_message.reply_text("Backend niedostepny. Upewnij sie, ze Pipe jest uruchomiony.")
    except Exception as exc:
        await update.effective_message.reply_text(f"Blad: {html.escape(str(exc))}")


# --- Handlery komend ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start -- przywitanie bez automatycznego statusu."""
    if not await _guard(update):
        return  # Milcz dla nieautoryzowanych

    welcome = (
        "<b>Witaj, tutaj Pipe.</b>\n\n"
        "Jestem agentem AI do zarzadzania Twoim serwerem (i innymi, jesli je dodasz).\n"
        "Czuwam w tle: jesli cos sie zepsuje, napisze pierwszy.\n\n"
        "<b>Na poczatek:</b>\n"
        "/mapa - diagram tego, co stoi na serwerze\n"
        "/raport - stan, zmiany od wczoraj, certyfikaty i backupy (przychodzi sam co rano)\n"
        "/audyt - ocena bezpieczenstwa z gotowymi poprawkami\n"
        "/server - co wiem o serwerze (SERVER.md)\n"
        "/status - szybki przeglad obciazenia\n"
        "/pomoc - wszystkie komendy\n\n"
        "Mozesz tez pisac do mnie normalnie, np. "
        "<i>\"dlaczego sklep dziala wolno?\"</i> albo <i>\"sprawdz dyski na wszystkich serwerach\"</i>."
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status -- stan serwera (prompt buduje backend)."""
    user_id = await _guard(update)
    if not user_id:
        return
    await _backend_call(update, context, _run_and_reply(
        context, update.effective_chat.id, user_id, get_client(user_id).command("status")))


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
                await update.message.reply_text(to_plain_text(frame.get("response", "Blad")))
            elif frame.get("response"):
                await _send_html(context.bot, update.effective_chat.id, to_telegram_html(frame["response"]))
        if got_image:
            await update.message.reply_text(
                "Chcesz inny widok? Napisz np. \"narysuj przeplyw zapytania do sklepu\" albo \"pokaz tylko bazy danych\".")

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
                              lambda d: format_pre(f"Zmiany na serwerze ({args or '24h'})", d.get("text", "")))


async def cmd_wykres(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/wykres [load|ram|dysk] [24h|7d] -- wykres z historii czuwania."""
    args = " ".join(context.args or []).strip()
    await _command_with_image(update, context, "chart", args,
                              lambda d: "" if d.get("image") else html.escape(d.get("summary", "")))


async def cmd_zdrowie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zdrowie -- certyfikaty, strony, DNS, backupy."""
    await _command_with_image(update, context, "health", "",
                              lambda d: format_pre("Zdrowie uslug", d.get("text", "")))


async def cmd_audyt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/audyt -- audyt bezpieczenstwa hosta (bez LLM)."""
    await _command_with_image(update, context, "audit", "", format_audit)


async def cmd_raport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/raport -- poranny raport na zadanie."""
    await _command_with_image(update, context, "digest", "", format_digest)


async def cmd_koszt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/koszt -- zuzycie tokenow LLM."""
    await _command_with_image(update, context, "usage", "", lambda d: format_pre("Koszt LLM", d.get("text", "")))


async def cmd_dziennik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dziennik -- zatwierdzone zmiany z kopiami (bez LLM)."""
    await _command_with_image(update, context, "journal", "",
                              lambda d: format_pre("Dziennik zmian", d.get("text", "")) + "\n<i>/cofnij &lt;id&gt; — cofnij wybrana</i>")


def _undo_keyboard(entry_id: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Cofnij", callback_data=f"undo:yes:{entry_id}:{user_id}"),
        InlineKeyboardButton("Anuluj", callback_data=f"undo:no:{entry_id}:{user_id}"),
    ]])


async def cmd_cofnij(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cofnij [id] -- podglad cofniecia i przyciski (bez LLM — dziala tez przy awarii providera)."""
    user_id = await _guard(update)
    if not user_id:
        return
    entry_id = " ".join(context.args or []).strip()
    if not _is_admin(user_id):
        await update.message.reply_text("Cofanie zmian jest dostepne tylko dla administratora.")
        return

    async def go() -> None:
        data = response_data([f async for f in get_client(user_id).command("undo", id=entry_id)])
        await _send_html(context.bot, update.effective_chat.id, to_telegram_html(data.get("preview", "")),
                         _undo_keyboard(data["id"], user_id) if data.get("undoable") else None)

    await _backend_call(update, context, go())


async def _handle_undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":")
    if len(parts) != 4 or not user or not _is_admin(user.id) or str(user.id) != parts[3]:
        await query.answer("Nie mozesz cofac cudzych operacji.", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if parts[1] != "yes":
        await query.message.reply_text("Anulowano — nic nie zmienilem.")
        return

    async def go() -> None:
        result = response_data([f async for f in get_client(user.id).command("undo", id=parts[2], execute=True)])
        await _send_html(context.bot, query.message.chat_id, format_pre("Cofniecie", result.get("text", "")))

    await _backend_call(update, context, go())


async def _handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    user = update.effective_user
    parts = data.split(":")
    if len(parts) != 3 or not user or not _is_admin(user.id):
        await query.answer("Zgody zatwierdza tylko administrator.", show_alert=True)
        return
    await query.answer("Wykonuje..." if parts[1] == "yes" else "Odrzucono")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    async def go() -> None:
        result = response_data([f async for f in get_client(user.id).command(
            "approve", id=parts[2], decision=parts[1] == "yes")])
        await _send_html(context.bot, query.message.chat_id, format_pre(f"Zgoda {parts[2]}", result.get("text", "")))

    await _backend_call(update, context, go())


async def cmd_zgody(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zgody -- operacje agentow MCP czekajace na zgode (przyciski tylko dla administratora)."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        pending = (await get_client(user_id).data("approvals")).get("pending", [])
        if not pending:
            await update.message.reply_text("Nic nie czeka na zgode.")
        for item in pending:
            await _send_html(context.bot, update.effective_chat.id, format_approval(item),
                             _approval_keyboard(item["id"]) if _is_admin(user_id) else None)

    await _backend_call(update, context, go())


async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_data(update, context, "mcp_servers", lambda d: format_list(
        "Serwery MCP", d.get("servers", []),
        "Brak. Napisz np. <i>\"dodaj serwer MCP github: npx -y @modelcontextprotocol/server-github\"</i>."))


async def cmd_historia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/historia -- ostatnie wpisy z audit logu (bez LLM)."""
    user_id = await _guard(update)
    if not user_id:
        return

    async def go() -> None:
        entries = (await get_client(user_id).data("history")).get("entries", [])
        body = "\n".join(entries) or "(pusto)"
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
    await _show_data(update, context, "alerts", format_alerts)


async def cmd_cele(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_data(update, context, "targets", lambda d: format_list(
        "Zdalne cele", d.get("targets", []),
        "Brak. Napisz np. <i>\"dodaj serwer 10.0.0.5 jako web-2 (ssh, root)\"</i>."))


async def cmd_rutyny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_data(update, context, "routines", lambda d: format_list(
        "Rutyny", d.get("routines", []),
        "Brak. Napisz np. <i>\"codziennie o 7 sprawdzaj backupy i certyfikaty\"</i>."))


async def cmd_vibe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = " ".join(context.args or []).strip()

    def render(data: dict) -> str:
        if "reset" in data:
            return "Wyczyscilem notatke o Twoim stylu. Zaczynam obserwowac od nowa." if data["reset"] \
                else "Nie mialem jeszcze notatki o Twoim stylu."
        content = (data.get("content") or "").strip()
        if not content:
            return ("Jeszcze nie znam Twojego stylu — ucze sie z rozmow. Mozesz tez powiedziec wprost, "
                    "np. <i>\"odpowiadaj krocej i bez wstepow\"</i>.")
        return f"<b>VIBE</b> — tak sie do Ciebie dopasowuje:\n<pre>{html.escape(content)}</pre>\n<i>/vibe reset — wyczysc</i>"

    await _show_data(update, context, "vibe", render, args=args)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Obsluguje dowolna wiadomosc tekstowa."""
    user_id = await _guard(update)
    if not user_id:
        return  # Milcz dla nieautoryzowanych

    text = update.message.text or ""
    if not text.strip():
        return

    # Rate limiting: max 1 wiadomosc na sekunde od uzytkownika
    now = time.monotonic()
    if now - _last_message_time.get(user_id, 0.0) < 1.0:
        return  # Ignoruj spam po cichu
    _last_message_time[user_id] = now

    await _backend_call(update, context, _run_and_reply(
        context, update.effective_chat.id, user_id, get_client(user_id).chat(text)))


_last_message_time: dict[int, float] = {}
MAX_VOICE_BYTES = 2_500_000


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wiadomosc glosowa -> transkrypcja w backendzie -> zwykla wiadomosc do agenta."""
    user_id = await _guard(update)
    if not user_id:
        return
    media = update.message.voice or update.message.audio
    if media is None:
        return
    if (media.file_size or 0) > MAX_VOICE_BYTES:
        await update.message.reply_text("Nagranie jest za dlugie — nagraj krotsze albo napisz.")
        return

    async def go() -> None:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        file = await media.get_file()
        data = bytes(await file.download_as_bytearray())
        name = "voice.ogg" if update.message.voice else (getattr(media, "file_name", None) or "audio.mp3")
        client = get_client(user_id)
        text = response_data([f async for f in client.command(
            "transcribe", audio=base64.b64encode(data).decode("ascii"), filename=name)]).get("text", "")
        await update.message.reply_text(f"Uslyszalem: {text}")
        await _run_and_reply(context, update.effective_chat.id, user_id, client.chat(text))

    await _backend_call(update, context, go())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Przyciski inline: potwierdzenia TAK/NIE i "Zbadaj" przy alertach."""
    query = update.callback_query
    user = update.effective_user
    data = query.data or ""

    if data.startswith("undo:"):
        await _handle_undo_callback(update, context, data)
        return

    if data.startswith("appr:"):
        await _handle_approval_callback(update, context, data)
        return

    if data.startswith("investigate:"):
        if not user or not _is_allowed(user.id):
            await query.answer()
            return
        await query.answer("Badam...")
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
        await query.answer("Nie mozesz potwierdzac cudzych operacji.", show_alert=True)
        return

    confirmed = parts[1] == "yes"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text("Operacja zatwierdzona." if confirmed else "Operacja anulowana.")
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
                "<b>SERVER.md</b>\n\n" + content + "\n\n<i>/server aktualizuj -- zbadaj serwer ponownie</i>"))
            return
        await update.message.reply_text(
            "Badam serwer i aktualizuje SERVER.md..." if mode in SCAN_WORDS
            else "SERVER.md jeszcze nie istnieje. Badam serwer i tworze go..."
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
    await _send_html(context.bot, update.effective_chat.id, format_help(skills))


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
                f"Nieznana komenda /{html.escape(name)}. Lista komend: /pomoc, skille: /skille"
            )
            return
        await _run_and_reply(context, update.effective_chat.id, user_id,
                             client.command("run_skill", name=entry["name"], args=args))

    await _backend_call(update, context, go())


# --- Czuwanie: alerty i raporty rutyn ---

def _approval_keyboard(approval_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Zatwierdz", callback_data=f"appr:yes:{approval_id}"),
        InlineKeyboardButton("Odrzuc", callback_data=f"appr:no:{approval_id}"),
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
    if kind == "alert":
        text = format_alert(event)
        markup = _investigate_keyboard(event["id"]) if event.get("state") != "resolved" and event.get("id") else None
    elif kind == "routine":
        text, markup = format_routine(event), None
    elif kind in ("digest", "welcome"):
        text, markup = format_digest(event), None
    elif kind == "investigation":
        text, markup = format_investigation(event), None
    else:
        return
    for user_id in sorted(ALLOWED_USER_IDS | VIEWER_USER_IDS):
        try:
            await _send_html(app.bot, user_id, text, markup)
            if event.get("attachment"):
                await _send_attachment(app.bot, user_id, event["attachment"])
        except Exception as exc:
            print(f"[Pipe Telegram] Nie wyslano zdarzenia do {user_id}: {exc}", flush=True)


async def alerts_loop(app: Application) -> None:
    """Trwala subskrypcja zdarzen backendu z ponawianiem polaczenia."""
    backoff = 2.0
    while True:
        try:
            reader, writer = await asyncio.open_unix_connection(AGENT_SOCKET, limit=READ_LIMIT)
            request = {"command": "subscribe", "session_id": "telegram-alerts", "interface": "telegram:alerts"}
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
                    print(f"[Pipe Telegram] Subskrypcja odrzucona: {frame.get('response')}", flush=True)
                    break
                event = frame.get("event") or {}
                if event.get("type") == "subscribed":
                    print("[Pipe Telegram] Subskrypcja alertow aktywna.", flush=True)
                    backoff = 2.0
                elif event:
                    await _broadcast(app, event)
            writer.close()
        except (OSError, ConnectionError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Pipe Telegram] Blad subskrypcji: {exc}", flush=True)
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
        menu = build_menu(await get_client(user_id).list_skills())
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
    if ALERTS_ENABLED and (ALLOWED_USER_IDS or VIEWER_USER_IDS):
        app.create_task(alerts_loop(app))


# --- Main ---

def main() -> None:
    """Punkt wejscia Telegram bota."""
    if not BOT_TOKEN:
        print("Blad: TELEGRAM_BOT_TOKEN nie jest ustawiony w .env", file=sys.stderr)
        sys.exit(1)

    if not ALLOWED_USER_IDS:
        print(
            "Ostrzezenie: TELEGRAM_ALLOWED_USER_IDS jest pusty -- "
            "nikt nie bedzie mogl uzywac bota.",
            file=sys.stderr,
        )

    print("[Pipe Telegram] Uruchamiam bota...")
    print(f"[Pipe Telegram] Dozwoleni uzytkownicy: {ALLOWED_USER_IDS}")
    if VIEWER_USER_IDS:
        print(f"[Pipe Telegram] Tylko odczyt: {VIEWER_USER_IDS}"
              + ("" if AGENT_VIEWER_TOKEN else " — UWAGA: brak AGENT_VIEWER_TOKEN, backend ich odrzuci"))
    print(f"[Pipe Telegram] Backend socket: {AGENT_SOCKET}")
    print(f"[Pipe Telegram] Alerty czuwania: {'tak' if ALERTS_ENABLED else 'nie'}")

    # concurrent_updates: dluga operacja (workery) jednego uzytkownika nie blokuje innych
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("raport", cmd_raport))
    app.add_handler(CommandHandler("audyt", cmd_audyt))
    app.add_handler(CommandHandler("zmiany", cmd_zmiany))
    app.add_handler(CommandHandler("wykres", cmd_wykres))
    app.add_handler(CommandHandler("zdrowie", cmd_zdrowie))
    app.add_handler(CommandHandler("koszt", cmd_koszt))
    app.add_handler(CommandHandler("cofnij", cmd_cofnij))
    app.add_handler(CommandHandler("dziennik", cmd_dziennik))
    app.add_handler(CommandHandler("zgody", cmd_zgody))
    app.add_handler(CommandHandler("mcp", cmd_mcp))
    app.add_handler(CommandHandler("mapa", cmd_mapa))
    app.add_handler(CommandHandler("historia", cmd_historia))
    app.add_handler(CommandHandler("server", cmd_server))
    app.add_handler(CommandHandler("katalogi", cmd_katalogi))
    app.add_handler(CommandHandler("skille", cmd_skille))
    app.add_handler(CommandHandler("alerty", cmd_alerty))
    app.add_handler(CommandHandler("cele", cmd_cele))
    app.add_handler(CommandHandler("rutyny", cmd_rutyny))
    app.add_handler(CommandHandler("vibe", cmd_vibe))
    app.add_handler(CommandHandler(["pomoc", "help"], cmd_pomoc))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    # Po wbudowanych -- skille jako komendy i odpowiedz na nieznana komende
    app.add_handler(MessageHandler(filters.COMMAND, handle_other_command))
    # Grupa 1 dziala po obsludze kazdej wiadomosci
    app.add_handler(TypeHandler(Update, refresh_menu_after_update), group=1)

    print("[Pipe Telegram] Gotowy. Ctrl+C aby zatrzymac.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
