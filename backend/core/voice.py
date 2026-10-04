"""
Wiadomosci glosowe — transkrypcja przez endpoint zgodny z OpenAI (/audio/transcriptions).

Telegram wysyla nagranie OGG/Opus; bot przekazuje je komenda `transcribe`,
backend zamienia je na tekst, a bot wysyla tekst agentowi jak zwykla wiadomosc.
Whisper API przyjmuje OGG bez konwersji (bez ffmpeg w obrazie).

Konfiguracja (STT_*):
  - provider openai albo groq — dziala od razu (ten sam klucz; whisper-1 /
    whisper-large-v3-turbo),
  - provider gemini — dziala od razu: nagranie idzie do samego modelu jako wejscie audio
    w Chat Completions (`input_audio`) z poleceniem dokladnej transkrypcji,
  - inny provider — ustaw STT_BASE_URL, STT_API_KEY i STT_MODEL, np. darmowy
    klucz Groq albo lokalny serwer zgodny z OpenAI (faster-whisper / speaches).
`pipe web` nagrywa w przegladarce i wysyla WAV (16 kHz mono) — przyjmuja go oba tryby.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.i18n import tr

MAX_AUDIO_BYTES = 10 * 1024 * 1024    # jak limit zalacznika; ~5 min WAV 16 kHz albo godzina Opusa

DEFAULT_STT = {
    "openai": "whisper-1",
    "groq": "whisper-large-v3-turbo",
}
# Providerzy, ktorych modele czatu przyjmuja dzwiek (input_audio) — transkrypcja bez osobnego STT.
CHAT_AUDIO_PROVIDERS = {"gemini"}
AUDIO_FORMATS = {"wav": "wav", "mp3": "mp3", "ogg": "ogg", "oga": "ogg", "opus": "ogg", "flac": "flac",
                 "aac": "aac", "m4a": "aac", "aiff": "aiff", "aif": "aiff", "webm": "webm"}
CHAT_PROMPT = ("Przepisz dokladnie, slowo w slowo, co mowi osoba w nagraniu (jezyk: {language}). "
               "Odpowiedz wylacznie transkrypcja, bez komentarzy. Gdy w nagraniu nie ma mowy, odpowiedz pusto.")
CHAT_PROMPT_EN = ("Transcribe exactly, word for word, what the person in the recording says (language: {language}). "
                  "Reply with the transcript only, no comments. If there is no speech, reply with nothing.")


@dataclass(frozen=True)
class SttConfig:
    base_url: str
    api_key: str
    model: str
    language: str
    # whisper — /audio/transcriptions; chat — model czatu z wejsciem audio (Gemini)
    mode: str = "whisper"


class TranscriptionError(RuntimeError):
    """Transkrypcja niedostepna albo nieudana (komunikat dla uzytkownika)."""


def resolve(env: dict[str, str], llm) -> SttConfig | None:
    """Konfiguracja STT z .env albo z providera LLM (openai/groq). None — brak transkrypcji."""
    language = env.get("STT_LANGUAGE", "pl").strip()
    base = env.get("STT_BASE_URL", "").strip()
    if base:
        model = env.get("STT_MODEL", "").strip() or "whisper-1"
        return SttConfig(base, env.get("STT_API_KEY", "").strip() or "brak-klucza", model, language)
    if llm is not None and llm.provider_id in DEFAULT_STT:
        model = env.get("STT_MODEL", "").strip() or DEFAULT_STT[llm.provider_id]
        return SttConfig(llm.base_url, llm.api_key, model, language)
    if llm is not None and llm.provider_id in CHAT_AUDIO_PROVIDERS:
        model = env.get("STT_MODEL", "").strip() or llm.model
        return SttConfig(llm.base_url, llm.api_key, model, language, mode="chat")
    return None


async def transcribe(data: bytes, filename: str, config: SttConfig | None, *, timeout: float = 60,
                     on_usage=None) -> str:
    """Tekst nagrania. `on_usage(model, usage)` — zuzycie tokenow w trybie chat (licznik kosztow)."""
    if config is None:
        raise TranscriptionError(tr(
            "Wiadomosci glosowe wymagaja transkrypcji: ustaw w backend/.env STT_BASE_URL, STT_API_KEY i STT_MODEL "
            "(np. darmowy klucz Groq: https://api.groq.com/openai/v1, whisper-large-v3-turbo) albo uzyj providera "
            "openai/groq.",
            "Voice messages need transcription: set STT_BASE_URL, STT_API_KEY and STT_MODEL in backend/.env "
            "(e.g. a free Groq key: https://api.groq.com/openai/v1, whisper-large-v3-turbo) or use the "
            "openai/groq provider."))
    if not data:
        raise TranscriptionError(tr("Puste nagranie.", "Empty recording."))
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscriptionError(tr(f"Nagranie za dlugie ({len(data) // 1000} kB, limit {MAX_AUDIO_BYTES // 1000} kB).",
                                    f"Recording too long ({len(data) // 1000} kB, limit {MAX_AUDIO_BYTES // 1000} kB)."))
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key, timeout=timeout)
    try:
        if config.mode == "chat":
            text = await _chat_transcribe(client, data, filename, config, on_usage)
        else:
            result = await client.audio.transcriptions.create(
                model=config.model, file=(filename or "voice.ogg", data), language=config.language or None)
            text = getattr(result, "text", "") or ""
    except Exception as exc:
        raise TranscriptionError(tr(f"Transkrypcja nie powiodla sie ({type(exc).__name__}: {exc})",
                                    f"Transcription failed ({type(exc).__name__}: {exc})")) from exc
    text = text.strip()
    if not text:
        raise TranscriptionError(tr("Nie rozpoznalem mowy w nagraniu.", "No speech recognised in the recording."))
    return text


def audio_format(data: bytes, filename: str) -> str:
    """Format nagrania dla input_audio — po sygnaturze, potem po rozszerzeniu."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return AUDIO_FORMATS.get(extension, "ogg")


async def _chat_transcribe(client, data: bytes, filename: str, config: SttConfig, on_usage) -> str:
    import base64

    language = config.language or "pl"
    instruction = tr(CHAT_PROMPT, CHAT_PROMPT_EN).format(language=language)
    response = await client.chat.completions.create(model=config.model, messages=[{"role": "user", "content": [
        {"type": "text", "text": instruction},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(data).decode("ascii"),
                                                "format": audio_format(data, filename)}},
    ]}])
    if on_usage is not None:
        on_usage(config.model, getattr(response, "usage", None))
    return response.choices[0].message.content or ""
