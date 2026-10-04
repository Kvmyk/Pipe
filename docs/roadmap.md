# Plan prac -- Pipe

## Nastepne: zalaczniki (pliki, zdjecia) i glosowki

Ustalone 2026-10-04, po wydaniu v0.25.2. Od tego zaczynamy kolejna sesje.

**Cel:** wysylanie zalacznikow do agenta -- Telegram (pliki, zdjecia, glosowki), `pipe web` (pliki, zdjecia,
glosowki), CLI (tylko pliki, bez glosowek).

### Stan wyjsciowy

- Telegram: glosowki juz dzialaja (`bot.handle_voice` -> komenda `transcribe` -> `core/voice.py`), ale transkrypcja
  konfiguruje sie sama tylko dla providerow openai/groq; na domyslnym Gemini trzeba recznie ustawic `STT_*`.
- Plikow i zdjec nie ma nigdzie: protokol (`message`) niesie tylko tekst, agent nie wysyla obrazow do modelu.

### Zakres

| Czesc | Co |
|-------|----|
| Protokol + backend | pole `attachments: [{name, mime, data(base64)}]` w zadaniu `message` (`docs/protocol.md`); obrazy -> tresc multimodalna dla modelu (`image_url` z data URL); pliki tekstowe (logi, konfiguracje) -> wklejone do wiadomosci po `memory.redact_secrets()` i przycieciu; limity rozmiaru (`READ_LIMIT` serwera 4 MiB i `MAX_BODY` mostu web do podniesienia); obrazy nie zostaja w historii na zawsze (po turze zastapione opisem/znacznikiem) |
| Glosowki na Gemini | transkrypcja przez samo Gemini (wejscie audio w Chat Completions), zeby dzialala bez `STT_*` |
| `pipe web` | spinacz, przeciaganie plikow, wklejanie zrzutow ekranu, miniatury przed wyslaniem, przycisk mikrofonu (MediaRecorder -> `transcribe`) |
| Telegram | zdjecia i dokumenty (podpis = tresc wiadomosci) |
| CLI | `/plik <sciezka>` albo `@sciezka` w wiadomosci, z podpowiadaniem sciezek w prompt_toolkit |
| Bezpieczenstwo, testy, dokumentacja PL/EN | tresc pliku = dane, nie polecenia (jak tresc z internetu -- rozwazyc wstrzymanie YOLO); rola viewer; testy limitow i redakcji sekretow; `docs/security.md`, `docs/features.md`, `docs/telegram.md`, `docs/cli.md`, `CLAUDE.md` |

Szacunek: jedna dluzsza sesja (2-3 h) z testami, dokumentacja i wydaniem (minor).

### Decyzje do potwierdzenia z uzytkownikiem przed startem (propozycje)

1. Pliki ani obrazowe, ani tekstowe (archiwa, binarne): zapis w `DATA_DIR/uploads/`, agent dostaje sciezke;
   przeniesienie gdzie indziej na serwerze -- zwykla zmiana z potwierdzeniem.
2. Model bez obslugi obrazow (np. czesc modeli Ollamy): jasny komunikat "ten model nie widzi obrazow" zamiast
   cichego pominiecia.
3. Limit: 10 MB na plik (Telegram ma wlasny limit 20 MB dla botow).
