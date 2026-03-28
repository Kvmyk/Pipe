# VPS Management Agent

Autonomiczny agent AI do zarządzania serwerem VPS (Mikrus). Agent działa **bezpośrednio na serwerze** i wykonuje komendy lokalnie przez subprocess.

```
Twój laptop
    ├── cli.py ──[SSH tunnel]──────────────────────┐
    │              ssh -L 7379:127.0.0.1:7379       │
    └── Telegram ──────────────────────────────┐   │
                                               ▼   ▼
                                         Serwer (Mikrus)
                                         backend/server.py
                                         ├── Unix socket: /tmp/vps-agent.sock  ← Telegram bot
                                         └── TCP 127.0.0.1:7379                ← SSH tunnel z laptopa
                                                     ↓
                                         core/agent.py (LLM + tool calling)
                                                     ↓
                                         core/executor.py (subprocess — lokalnie)
                                                     ↓
                                         system operacyjny Mikrusa
```

---

## Quick Start

> 📖 Szczegółowa instrukcja krok po kroku: [QUICKSTART.md](./QUICKSTART.md)

### Scenariusz 1: Postaw backend na serwerze

Wykonaj na serwerze (Mikrus):

```bash
git clone https://github.com/user/vps-agent
cd vps-agent/backend
cp .env.example .env
# Uzupełnij LLM_API_KEY w .env (darmowy: https://aistudio.google.com/)
docker-compose up -d
```

### Scenariusz 2: Podłącz CLI ze swojego laptopa

Na laptopie — skrypt automatycznie doda skrót `pipe` do Twojego terminala:

```powershell
# Windows
.\install.ps1
```

```bash
# Linux / macOS
bash install.sh
```

Od teraz w każdym terminalu wpisujesz:

```
pipe
```

CLI pyta o hasło SSH, łączy się z agentem na serwerze i czeka na Twoje polecenia.

### Scenariusz 3: Postaw Telegram bota na serwerze

Telegram bot działa na tym samym serwerze co backend (łączy się przez Unix socket):

```bash
# Na serwerze
cd vps-agent/clients/telegram
cp .env.example .env
# Uzupełnij TELEGRAM_BOT_TOKEN i TELEGRAM_ALLOWED_USER_IDS w .env
pip install -r requirements.txt
python telegram.py
```

---

## Architektura

| Komponent | Gdzie to działa | Jak się łączy z backendem |
|-----------|----------------|--------------------------|
| `backend/` | **Serwer (Mikrus)** | — to jest backend |
| `clients/cli/` | **Twój laptop** | SSH tunnel → TCP `127.0.0.1:7379` |
| `clients/telegram/` | **Serwer (Mikrus)** | Unix socket `/tmp/vps-agent.sock` |
| `clients/discord/` | — | Placeholder — PR welcome |
| `clients/webui/` | — | Placeholder — PR welcome |

**Komunikacja:** prosty protokół JSON (linia po linii):
- Żądanie: `{"message": "tekst", "session_id": "uuid", "interface": "cli"}`
- Odpowiedź: `{"response": "tekst", "status": "ok|confirm|error", "done": true}`

---

## Moduły backendu

| Moduł | Opis |
|-------|------|
| `core/agent.py` | Pętla LLM z tool calling, max 10 iteracji |
| `core/executor.py` | `LocalExecutor` — subprocess z 30s timeoutem |
| `core/security.py` | Klasyfikacja komend: `safe` / `confirm` / `forbidden` |
| `core/audit.py` | Append-only audit log |
| `core/tools.py` | Definicje narzędzi OpenAI function calling |
| `config/settings.py` | Konfiguracja z `.env` |
| `config/prompts.py` | System prompt (niemodyfikowalny) |

---

## Contributing — jak dodać nowy interfejs

Każdy klient implementuje ten sam protokół socket JSON:

1. Nawiąż połączenie: `asyncio.open_unix_connection(socket_path)`
2. Wyślij żądanie (linia JSON + `\n`):
   ```json
   {"message": "tekst użytkownika", "session_id": "uuid", "interface": "moj-klient"}
   ```
3. Odbieraj odpowiedzi (JSON lines) do czasu gdy `"done": true`
4. Obsłuż potwierdzenia: gdy `"status": "confirm"`, zapytaj użytkownika i wyślij:
   ```json
   {"confirm": true, "session_id": "uuid"}
   ```

Szczegóły: `clients/discord/README.md`

---

## Bezpieczeństwo

- Agent działa jako **dedykowany user bez sudo** (`vpsagent`)
- Trzy poziomy klasyfikacji komend: `safe` → `confirm` → `forbidden`
- Każda operacja zapisywana do audit logu
- Zawartość pliku nigdy nie trafia do audit logu (może zawierać sekrety)
- Lista bezwzględnie zakazanych wzorców (`rm -rf /`, fork bomb, `curl | bash`, itp.)

---

## Linki

- [Backend README](./backend/README.md)
- [CLI README](./clients/cli/README.md)
- [Telegram README](./clients/telegram/README.md)
