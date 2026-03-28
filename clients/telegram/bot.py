"""
Telegram Bot — interfejs Telegram dla VPS Management Agent.

Łączy się z backendem przez Unix socket.
Używa python-telegram-bot w trybie async.

Funkcje:
  - Whitelist użytkowników (TELEGRAM_ALLOWED_USER_IDS)
  - Osobna sesja per user_id
  - /start — przywitanie i status serwera
  - /historia — ostatnie 10 wpisów z audit logu
  - InlineKeyboard dla potwierdzeń (✅ TAK / ❌ NIE)
  - MarkdownV2 z escape pomocnikiem

Konfiguracja w .env (clients/telegram/.env):
  TELEGRAM_BOT_TOKEN=your_bot_token
  TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
  AGENT_SOCKET=/tmp/vps-agent.sock
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Załaduj .env z katalogu bota
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
    print("Błąd: zainstaluj zależności: pip install -r requirements.txt")
    sys.exit(1)

# ─── Konfiguracja ─────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip().isdigit()
}
AGENT_SOCKET: str = os.getenv("AGENT_SOCKET", "/tmp/vps-agent.sock")

STARTUP_STATUS_MESSAGE = (
    "Sprawdź stan serwera: wykonaj hostname && uptime && df -h / && free -h "
    "i podsumuj wyniki po polsku."
)

SERVER_STATUS_MESSAGE = (
    "Zbierz z serwera dane: Uptime, Obciążenie CPU, Zużycie Pamięci RAM i Wolne miejsce na dysku (najlepiej sprawdź /hostfs albo wewnetrzne info). "
    "Odpowiedz ZWIĘZŁĄ i elegancką listą zgodną z Twoimi instrukcjami do Telegram MarkdownV2."
)


# ─── MarkdownV2 helper ────────────────────────────────────────────────────────

_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_markdown(text: str) -> str:
    """
    Escapuje znaki specjalne MarkdownV2 Telegrama.

    Używaj do surowych stringów (np. output komend) przed wklejeniem
    do odpowiedzi — żeby znaki specjalne nie psuły parsowania.
    """
    return _MDV2_SPECIAL.sub(r"\\\1", text)


def safe_md(text: str) -> str:
    """Alias dla escape_markdown — dla czytelności kodu."""
    return escape_markdown(text)


# ─── Socket Client ────────────────────────────────────────────────────────────

class TelegramSocketClient:
    """Klient Unix socket dla Telegram bota — per user_id."""

    def __init__(self, socket_path: str, session_id: str) -> None:
        self.socket_path = socket_path
        self.session_id = session_id

    async def _send(self, data: dict) -> list[dict]:
        """Wysyła żądanie i zbiera odpowiedzi do done=true."""
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


# ─── Zarządzanie sesjami ──────────────────────────────────────────────────────

_clients: dict[int, TelegramSocketClient] = {}


def get_client(user_id: int) -> TelegramSocketClient:
    """Zwraca istniejący klient sesji lub tworzy nowy."""
    if user_id not in _clients:
        _clients[user_id] = TelegramSocketClient(
            socket_path=AGENT_SOCKET,
            session_id=str(user_id),
        )
    return _clients[user_id]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_allowed(user_id: int | None) -> bool:
    """Sprawdza czy user_id jest na whiteliście."""
    if not user_id:
        return False
    return user_id in ALLOWED_USER_IDS


def _confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Tworzy klawiaturę inline z przyciskami TAK/NIE."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ TAK", callback_data=f"confirm:yes:{user_id}"),
            InlineKeyboardButton("❌ NIE", callback_data=f"confirm:no:{user_id}"),
        ]
    ])


def _collect_response_text(responses: list[dict]) -> tuple[str, bool]:
    """
    Zbiera tekst odpowiedzi z listy fragmentów.

    Returns:
        (text, needs_confirmation)
    """
    parts: list[str] = []
    needs_confirm = False

    for resp in responses:
        text = resp.get("response", "")
        status = resp.get("status", "ok")
        if text:
            # Uładnianie tagów statusowych dla Telegrama
            text = text.replace("[SUKCES]", "✅ *SUKCES:*")
            text = text.replace("[BLAD]", "❌ *BŁĄD:*")
            text = text.replace("[POTWIERDZ]", "⚠️ *WYMAGA POTWIERDZENIA:*")
            text = text.replace("[ODMOWA]", "🚫 *ODMOWA:*")
            
            parts.append(text)
        if status == "confirm":
            needs_confirm = True

    return "\n\n".join(parts), needs_confirm


async def _send_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    responses: list[dict],
    user_id: int,
) -> None:
    """Wysyła odpowiedź do użytkownika (z obsługą potwierdzeń)."""
    text, needs_confirm = _collect_response_text(responses)

    if not text:
        return

    # Podziel długie wiadomości (Telegram limit: 4096 znaków)
    chunks = _split_message(text)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        reply_markup = _confirm_keyboard(user_id) if (needs_confirm and is_last) else None

        try:
            await update.effective_message.reply_text(
                chunk,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup,
            )
        except Exception:
            # Fallback — wyślij bez Markdown jeśli parsowanie się nie powiodło
            try:
                await update.effective_message.reply_text(
                    escape_markdown(chunk),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup,
                )
            except Exception:
                await update.effective_message.reply_text(
                    chunk,
                    reply_markup=reply_markup,
                )


def _split_message(text: str, max_length: int = 4000) -> list[str]:
    """Dzieli długą wiadomość na kawałki <= max_length znaków."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        # Znajdź ostatni znak nowej linii przed limitem
        split_at = text.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ─── Handlery komend ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — przywitanie bez automatycznego statusu."""
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        return  # Milcz dla nieautoryzowanych

    welcome = (
        "👋 *Witaj, tutaj Pipe\\!*\n\n"
        "Jestem autonomicznym agentem AI do zarządzania Twoim serwerem VPS\\.\n"
        "Komunikuję się po polsku i wykonuję komendy bezpośrednio na serwerze\\.\n\n"
        "*Dostępne komendy:*\n"
        "/status \\- szybki przegląd obciążenia serwera\n"
        "/historia \\- ostatnie 10 wpisów z audit logu\n\n"
        "Możesz też pisać do mnie bezpośrednio \\— np\\. "
        "_\"ile mam wolnego miejsca na dysku?\"_"
    )

    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — pobiera obciążenie serwera."""
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        return

    client = get_client(user.id)
    try:
        await update.message.reply_text(escape_markdown("🔄 Analizuję obciążenie i stan MIKRUSA..."), parse_mode=ParseMode.MARKDOWN_V2)
        responses = await client.chat(SERVER_STATUS_MESSAGE)
        await _send_response(update, context, responses, user.id)
    except Exception as exc:
        await update.message.reply_text(
            escape_markdown(f"❌ Błąd połączenia z backendem: {exc}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def cmd_historia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/historia — ostatnie 10 wpisów z audit logu."""
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        return

    # Pobierz historię przez agenta
    client = get_client(user.id)
    try:
        responses = await client.chat("Pokaż ostatnie 10 wpisów z audit logu.")
        await _send_response(update, context, responses, user.id)
    except Exception as exc:
        await update.message.reply_text(
            escape_markdown(f"❌ Błąd: {exc}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Obsługuje dowolną wiadomość tekstową."""
    user = update.effective_user
    if not _is_allowed(user.id if user else None):
        return  # Milcz dla nieautoryzowanych

    text = update.message.text or ""
    if not text.strip():
        return

    client = get_client(user.id)
    try:
        # Pokaż "pisze..." 
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )
        responses = await client.chat(text)
        await _send_response(update, context, responses, user.id)
    except FileNotFoundError:
        await update.message.reply_text(
            escape_markdown(
                "❌ Backend niedostępny. "
                "Upewnij się, że vps-agent jest uruchomiony."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        await update.message.reply_text(
            escape_markdown(f"❌ Błąd: {exc}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Obsługuje przyciski inline (potwierdzenia TAK/NIE)."""
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

    # Tylko oryginalny użytkownik może potwierdzić
    user = update.effective_user
    if not user or user.id != requesting_user_id:
        await query.answer("⛔ Nie możesz potwierdzać cudzych operacji.", show_alert=True)
        return

    confirmed = action == "yes"
    client = get_client(user.id)

    # Usuń przyciski z wiadomości
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    status_text = "✅ Operacja zatwierdzona\\." if confirmed else "❌ Operacja anulowana\\."
    await query.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN_V2)

    try:
        responses = await client.confirm(confirmed)
        await _send_response(update, context, responses, user.id)
    except Exception as exc:
        await query.message.reply_text(
            escape_markdown(f"❌ Błąd: {exc}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    """Punkt wejścia Telegram bota."""
    if not BOT_TOKEN:
        print("Błąd: TELEGRAM_BOT_TOKEN nie jest ustawiony w .env", file=sys.stderr)
        sys.exit(1)

    if not ALLOWED_USER_IDS:
        print(
            "Ostrzeżenie: TELEGRAM_ALLOWED_USER_IDS jest pusty — "
            "nikt nie będzie mógł używać bota.",
            file=sys.stderr,
        )

    print(f"[Telegram Bot] Uruchamiam bota...")
    print(f"[Telegram Bot] Dozwoleni użytkownicy: {ALLOWED_USER_IDS}")
    print(f"[Telegram Bot] Backend socket: {AGENT_SOCKET}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("historia", cmd_historia))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("[Telegram Bot] Gotowy. Ctrl+C aby zatrzymać.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
