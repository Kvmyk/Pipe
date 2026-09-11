"""
Formatowanie odpowiedzi backendu dla Telegrama (parse_mode=HTML).

Modul jest bez zaleznosci od python-telegram-bot, zeby dalo sie go testowac
zwyklym `pytest backend/tests/`.

Zasady:
  - Tekst w backtickach (`inline` i ```blok```) jest DOSLOWNY. Tak backend
    pokazuje komendy do potwierdzenia, wiec uzytkownik musi zobaczyc dokladnie
    to, co zostanie wykonane — bez zamiany *gwiazdek* na pogrubienie itp.
  - Tagi dozwolone przez Telegram zostaja, caly pozostaly tekst jest escapowany.
    Telegram odrzuca wiadomosc, w ktorej '<', '>' lub '&' nie sa czescia tagu
    albo encji (https://core.telegram.org/bots/api#html-style).
"""

from __future__ import annotations

import html
import re

# Tagi HTML obslugiwane przez Telegram.
_ALLOWED_TAG = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|span|tg-spoiler|tg-emoji|blockquote)"
    r"(?:\s[^<>]*)?>",
    re.IGNORECASE,
)
# Otoczki jak w CommonMark: dowolnie dlugi ciag backtickow, zamykany ciagiem
# tej samej dlugosci — dzieki temu komenda moze sama zawierac backticki.
# Blok wymaga nowej linii po otwarciu, inaczej ```x``` w linii to kod inline.
_CODE_BLOCK = re.compile(r"(`{3,})[\w-]*\n(.*?)\n?\1(?!`)", re.DOTALL)
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")

TELEGRAM_MAX_LENGTH = 4000


def _escape_text(text: str) -> str:
    """Escapuje tekst poza tagami. unescape najpierw — encje wstawione przez LLM nie sa dublowane."""
    return html.escape(html.unescape(text), quote=False)


def _sanitize_html(text: str) -> str:
    """Zostawia dozwolone tagi Telegrama, reszte tekstu escapuje."""
    out: list[str] = []
    pos = 0
    for match in _ALLOWED_TAG.finditer(text):
        out.append(_escape_text(text[pos:match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_escape_text(text[pos:]))
    return "".join(out)


def to_telegram_html(text: str) -> str:
    """
    Zamienia odpowiedz backendu (HTML od LLM + ewentualne resztki Markdowna
    + komunikaty protokolu z komendami w backtickach) na poprawny HTML Telegrama.
    """
    # 1. Wytnij fragmenty kodu, zanim cokolwiek je zmieni. Zawartosc jest
    #    escapowana bez unescape — komenda ma byc pokazana znak w znak.
    code: list[str] = []

    def _stash(tag: str):
        def replace(match: re.Match[str]) -> str:
            content = match.group(2)
            # CommonMark: jedna spacja z obu stron to czesc otoczki, nie tresci.
            if tag == "code" and len(content) > 2 and content[0] == content[-1] == " " and content.strip():
                content = content[1:-1]
            code.append(f"<{tag}>{html.escape(content, quote=False)}</{tag}>")
            return f"\x00{len(code) - 1}\x00"
        return replace

    text = _CODE_BLOCK.sub(_stash("pre"), text)
    text = _INLINE_CODE.sub(_stash("code"), text)

    # 2. Resztki Markdowna, ktore LLM moze wygenerowac mimo instrukcji.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!\\])", r"\1", text)  # escape'y MarkdownV2
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 3. Escapuj wszystko poza dozwolonymi tagami, potem wstaw kod z powrotem.
    text = _sanitize_html(text)
    return _PLACEHOLDER.sub(lambda m: code[int(m.group(1))], text)


def to_plain_text(text: str) -> str:
    """Fallback, gdy Telegram mimo wszystko odrzuci HTML: usun tagi, rozwin encje."""
    return html.unescape(_ALLOWED_TAG.sub("", text))


def collect_response_text(responses: list[dict]) -> tuple[str, bool]:
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


def split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
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
