"""
Wiadomosci glosowe — transkrypcja przez endpoint zgodny z OpenAI (/audio/transcriptions).

Telegram wysyla nagranie OGG/Opus; bot przekazuje je komenda `transcribe`,
backend zamienia je na tekst, a bot wysyla tekst agentowi jak zwykla wiadomosc.
Whisper API przyjmuje OGG bez konwersji (bez ffmpeg w obrazie).

Konfiguracja (STT_*):
  - provider openai albo groq — dziala od razu (ten sam klucz; whisper-1 /
    whisper-large-v3-turbo),
  - inny provider (np. Gemini) — ustaw STT_BASE_URL, STT_API_KEY i STT_MODEL, np. darmowy
    klucz Groq albo lokalny serwer zgodny z OpenAI (faster-whisper / speaches).
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_AUDIO_BYTES = 2_500_000     # ~ kilka minut Opusa; limit linii protokolu to 4 MiB po base64

DEFAULT_STT = {
    "openai": "whisper-1",
    "groq": "whisper-large-v3-turbo",
}


@dataclass(frozen=True)
class SttConfig:
    base_url: str
    api_key: str
    model: str
    language: str


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
    return None


async def transcribe(data: bytes, filename: str, config: SttConfig | None, *, timeout: float = 60) -> str:
    if config is None:
        raise TranscriptionError(
            "Wiadomosci glosowe wymagaja transkrypcji: ustaw w backend/.env STT_BASE_URL, STT_API_KEY i STT_MODEL "
            "(np. darmowy klucz Groq: https://api.groq.com/openai/v1, whisper-large-v3-turbo) albo uzyj providera "
            "openai/groq.")
    if not data:
        raise TranscriptionError("Puste nagranie.")
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscriptionError(f"Nagranie za dlugie ({len(data) // 1000} kB, limit {MAX_AUDIO_BYTES // 1000} kB).")
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key, timeout=timeout)
    try:
        result = await client.audio.transcriptions.create(
            model=config.model, file=(filename or "voice.ogg", data), language=config.language or None)
    except Exception as exc:
        raise TranscriptionError(f"Transkrypcja nie powiodla sie ({type(exc).__name__}: {exc})") from exc
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        raise TranscriptionError("Nie rozpoznalem mowy w nagraniu.")
    return text
