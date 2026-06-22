"""
Telegram Bot -- interfejs Telegram dla PipeClaw (VPS Management Agent).

Laczy sie z backendem przez Unix socket.
Uzywa python-telegram-bot w trybie async.

PipeClaw v0.3

Funkcje:
  - Whitelist uzytkownikow (TELEGRAM_ALLOWED_USER_IDS)
  - Osobna sesja per user_id
  - /start -- przywitanie
  - /status -- status serwera
  - /historia -- ostatnie 10 wpisow z audit logu
  - InlineKeyboard dla potwierdzen (TAK / NIE)
  - HTML parse mode (nie MarkdownV2 -- unikanie problemow z escapowaniem)

Konfiguracja w .env (clients/telegram/.env):
  TELEGRAM_BOT_TOKEN=your_bot_token
  TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
  AGENT_SOCKET=/tmp/vps-agent.sock
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Zaladuj .env z katalogu bota
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:
    print("Blad: zainstaluj zaleznosci: pip install -r requirements.txt")
    sys.exit(1)

# --- Konfiguracja ---
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip().isdigit()
}
AGENT_SOCKET: str = os.getenv("AGENT_SOCKET", "/tmp/vps-agent.sock")

SERVER_STATUS_MESSAGE = (
    "Uzyj narzedzia system_stats aby pobrac szczegolowe statystyki systemowe serwera. "
    "Na podstawie wynikow przygotuj zwiezle podsumowanie po polsku: "
    "uptime, obciazenie CPU (load average i/lub procent), zuzycie RAM (na podstawie sekcji MEMORY "
    "korzystajac np. z outputu free -m), wolne miejsce na dysku. Formatuj w HTML dla Telegrama."
)


# --- HTML helper ---


def _strip_markdown_artifacts(text: str) -> str:
    """
    Czysci resztki Markdowna ktore LLM moze omylem wygenerowac.
    Konwertuje na czytelny tekst z HTML.
    """
    # Zamien ```blok``` na <pre>blok</pre>
    text = re.sub(r'```[\w]*\n?(.*?)```', r'<pre>\1</pre>', text, flags=re.DOTALL)
    # Zamien `inline` na <code>inline</code>
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Zamien **bold** na <b>bold</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Zamien *bold* na <b>bold</b> (Telegram MarkdownV2 style)
    text = re.sub(r'\*(.+?)\*', r'<b>\1</b>', text)
    # Zamien _italic_ na <i>italic</i>
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text)
    # Usun MarkdownV2 escape backslashe (np. \- \. \! \( \))
    text = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', text)
    # Usun naglowki Markdowna
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    return text


# --- Socket Client ---

class TelegramSocketClient:
    """Klient Unix socket dla Telegram bota -- per user_id."""

    def __init__(self, socket_path: str, session_id: str) -> None:
        self.socket_path = socket_path
        self.session_id = session_id

    async def _send(self, data: dict) -> list[dict]:
        """Wysyla zadanie i zbiera odpowiedzi do done=true."""
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            line = json.dumps(data, ensure_ascii=False) + "\n"
            writer.write(line.encode("utf-8"))
            await writer.drain()

            responses: list[dict] = []
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    resp = json.loads(raw.decode("utf-8", errors="replace"))
                    responses.append(resp)
                    if resp.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
            return responses
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def chat(self, message: str) -> list[dict]:
        return await self._send({
            "message": message,
            "session_id": self.session_id,
            "interface": f"telegram:{self.session_id}",
        })

    async def confirm(self, confirmed: bool) -> list[dict]:
        return await self._send({
            "confirm": confirmed,
            "session_id": self.session_id,
        })


# --- Zarzadzanie sesjami ---

_clients: dict[int, TelegramSocketClient] = {}


def get_client(user_id: int) -> TelegramSocketClient:
    """Zwraca istniejacy klient sesji lub tworzy nowy."""
    if user_id not in _clients:
        _clients[user_id] = TelegramSocketClient(
            socket_path=AGENT_SOCKET,
            session_id=str(user_id),
        )
    return _clients[user_id]


# --- Helpers ---

def _is_allowed(user_id: int | None) -> bool:
    """Sprawdza czy user_id jest na whiteliscie."""
    if not user_id:
        return False
    return user_id in ALLOWED_USER_IDS


def _confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Tworzy klawiature inline z przyciskami TAK/NIE."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("TAK", callback_data=f"confirm:yes:{user_id}"),
            InlineKeyboardButton("NIE", callback_data=f"confirm:no:{user_id}"),
        ]
    ])


def _collect_response_text(responses: list[dict]) -> tuple[str, bool]:
    """
    Zbiera tekst odpowiedzi z listy fragmentow.

    Returns:
        (text, needs_confirmation)
    """
    parts: list[str] = []
    needs_confirm = False

    for resp in responses:
        text = resp.get("response", "")
        status = resp.get("status", "ok")
        if text:
            # Tagi statusowe -- tylko ODMOWA jest widoczna
            # [SUKCES] -- usuniety calkowicie
            text = text.replace("[SUKCES]", "")
            text = text.replace("[BLAD]", "<b>BLAD:</b>")
            text = text.replace("[POTWIERDZ]", "<b>WYMAGA POTWIERDZENIA:</b>")
            text = text.replace("[ODMOWA]", "<b>ODMOWA:</b>")

            parts.append(text)
        if status == "confirm":
            needs_confirm = True

    # Jeśli wiele części odpowiedzi, wybierz najbardziej zwięzłe podsumowanie
    # Preferuj fragmenty zawierające czytelne podsumowanie serwera.
    if parts:
        # Jeśli ostatni fragment jest prostym komunikatem o błędzie, zwróć go
        last = parts[-1].strip()
        if last.lower().startswith("błąd:") or last.lower().startswith("blad:"):
            return last, needs_confirm

        # W przeciwnym razie wybierz ostatni fragment który nie zaczyna się od '['
        for part in reversed(parts):
            txt = part.strip()
            if txt and not txt.startswith("["):
                return part, needs_confirm

        # Fallback: zwróć ostatni fragment
        return parts[-1], needs_confirm

    return "", needs_confirm


async def _send_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    responses: list[dict],
    user_id: int,
) -> None:
    """Wysyla odpowiedz do uzytkownika (z obsluga potwierdzen)."""
    text, needs_confirm = _collect_response_text(responses)

    if not text:
        return

    # Cleanup: usun potencjalne resztki Markdowna
    text = _strip_markdown_artifacts(text)

    # Podziel dlugie wiadomosci (Telegram limit: 4096 znakow)
    chunks = _split_message(text)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        reply_markup = _confirm_keyboard(user_id) if (needs_confirm and is_last) else None

        try:
            await update.effective_message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception:
            # Fallback -- wyslij bez formatowania jesli HTML sie nie sparsuje
            try:
                # Usun wszystkie tagi HTML i wyslij jako plain text
                clean_text = re.sub(r'<[^>]+>', '', chunk)
                await update.effective_message.reply_text(
                    clean_text,
                    reply_markup=reply_markup,
                )
            except Exception:
                await update.effective_message.reply_text(
                    "Blad formatowania odpowiedzi. Sprobuj ponownie.",
                    reply_markup=reply_markup,
                )


def _split_message(text: str, max_length: int = 4000) -> list[str]:
    """Dzieli dluga wiadomosc na kawalki <= max_length znakow."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        # Znajdz ostatni znak nowej linii przed limitem
        split_at = text.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# --- Handlery komend ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start -- przywitanie bez automatycznego statusu."""
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        return  # Milcz dla nieautoryzowanych

    welcome = (
        "<b>Witaj, tutaj PipeClaw.</b>\n\n"
        "Jestem autonomicznym agentem AI do zarzadzania Twoim serwerem VPS.\n"
        "Komunikuje sie po polsku i wykonuje komendy bezposrednio na serwerze.\n\n"
        "<b>Dostepne komendy:</b>\n"
        "/status - szybki przeglad obciazenia serwera\n"
        "/historia - ostatnie 10 wpisow z audit logu\n\n"
        "Mozesz tez pisac do mnie bezposrednio, np. "
        "<i>\"ile mam wolnego miejsca na dysku?\"</i>"
    )

    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status -- pobiera obciazenie serwera."""
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        return

    client = get_client(user.id)
    try:
        await update.message.reply_text(
            "Analizuje obciazenie i stan serwera...",
        )
        responses = await client.chat(SERVER_STATUS_MESSAGE)
        await _send_response(update, context, responses, user.id)
    except Exception as exc:
        await update.message.reply_text(
            f"Blad polaczenia z backendem: {html.escape(str(exc))}",
        )


async def cmd_historia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/historia -- ostatnie 10 wpisow z audit logu."""
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        return

    client = get_client(user.id)
    try:
        responses = await client.chat("Pokaz ostatnie 10 wpisow z audit logu.")
        await _send_response(update, context, responses, user.id)
    except Exception as exc:
        await update.message.reply_text(
            f"Blad: {html.escape(str(exc))}",
        )


import time
_last_message_time: dict[int, float] = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Obsluguje dowolna wiadomosc tekstowa."""
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        return  # Milcz dla nieautoryzowanych

    text = update.message.text or ""
    if not text.strip():
        return

    # Rate limiting: max 1 wiadomość na sekundę od użytkownika
    now = time.monotonic()
    last_time = _last_message_time.get(user.id, 0.0)
    if now - last_time < 1.0:
        return  # Ignoruj spam po cichu
    _last_message_time[user.id] = now

    client = get_client(user.id)
    try:
        # Pokaz "pisze..."
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )
        responses = await client.chat(text)
        await _send_response(update, context, responses, user.id)
    except FileNotFoundError:
        await update.message.reply_text(
            "Backend niedostepny. Upewnij sie, ze PipeClaw jest uruchomiony.",
        )
    except Exception as exc:
        await update.message.reply_text(
            f"Blad: {html.escape(str(exc))}",
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Obsluguje przyciski inline (potwierdzenia TAK/NIE)."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    # Format: "confirm:yes|no:user_id"
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "confirm":
        return

    action = parts[1]  # "yes" lub "no"
    try:
        requesting_user_id = int(parts[2])
    except ValueError:
        return

    # Tylko oryginalny uzytkownik moze potwierdzic
    user = update.effective_user
    if not user or user.id != requesting_user_id:
        await query.answer("Nie mozesz potwierdzac cudzych operacji.", show_alert=True)
        return

    confirmed = action == "yes"
    client = get_client(user.id)

    # Usun przyciski z wiadomosci
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    status_text = "Operacja zatwierdzona." if confirmed else "Operacja anulowana."
    await query.message.reply_text(status_text)

    try:
        responses = await client.confirm(confirmed)
        await _send_response(update, context, responses, user.id)
    except Exception as exc:
        await query.message.reply_text(
            f"Blad: {html.escape(str(exc))}",
        )


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

    print(f"[PipeClaw Telegram] Uruchamiam bota...")
    print(f"[PipeClaw Telegram] Dozwoleni uzytkownicy: {ALLOWED_USER_IDS}")
    print(f"[PipeClaw Telegram] Backend socket: {AGENT_SOCKET}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("historia", cmd_historia))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("[PipeClaw Telegram] Gotowy. Ctrl+C aby zatrzymac.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
