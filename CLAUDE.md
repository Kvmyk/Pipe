# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**PipeClaw v0.2** — an autonomous LLM-powered agent for Linux VPS server management. Users interact via CLI (SSH tunnel), Telegram bot, or planned Discord/WebUI clients. The backend runs on a VPS, uses an OpenAI-compatible LLM API (typically Google Gemini), and executes shell commands with a three-tier security classifier.

All code comments, error messages, documentation, and LLM prompts are in **Polish**.

## Commands

**Run tests:**
```bash
pip install -r backend/requirements.txt
pytest backend/tests/
```

**Run a single test file:**
```bash
pytest backend/tests/test_security.py -v
```

**Start backend locally (requires `backend/.env`):**
```bash
python -m backend.server
```

**Start backend via Docker (production):**
```bash
cd backend
docker-compose up -d
docker-compose logs -f
```

**Setup CLI (Windows):**
```powershell
.\install.ps1
. $PROFILE
pipeclaw
```

**Setup CLI (Linux/macOS):**
```bash
bash install.sh
source ~/.bashrc
pipeclaw
```

## Architecture

### Data Flow

```
CLI / Telegram Bot
  → TCP 127.0.0.1:7379 (SSH tunnel) or Unix socket /tmp/vps-agent.sock
    → backend/server.py          # async listener, dispatches to sessions
      → backend/core/agent.py   # LLM tool-calling loop (max 10 iterations)
        → backend/core/security.py  # classifies each command (safe/confirm/forbidden)
          → backend/core/executor.py  # async subprocess, 30s timeout
            → backend/core/audit.py  # append-only log
```

### Key Modules

| File | Role |
|------|------|
| `backend/server.py` | Accepts Unix socket + TCP connections; manages sessions by UUID |
| `backend/core/agent.py` | LLM interaction loop; 9 tool handlers; streaming response |
| `backend/core/security.py` | `classify_command()` / `classify_file_write()` returning `safe\|confirm\|forbidden` |
| `backend/core/executor.py` | `LocalExecutor.run()` — async subprocess with timeout |
| `backend/core/tools.py` | OpenAI function-calling schema for all 9 tools |
| `backend/core/audit.py` | Thread-safe append-only audit log |
| `backend/config/settings.py` | Loads `.env`; exposes `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` etc. |
| `backend/config/prompts.py` | System prompt for the LLM agent (Polish) |
| `clients/cli/cli.py` | SSH tunnel manager + `rich`-based interactive REPL |
| `clients/telegram/bot.py` | Async Telegram bot; mirrors confirm/streaming protocol |

### Agent Tools

The 9 tools the LLM can call: `execute_command`, `read_file`, `write_file`, `change_directory`, `git_command`, `system_stats`, `docker_manage`, `network_info`, `cron_manage`.

Each tool invocation passes through `classify_command()` / `classify_file_write()` before execution. `confirm` classification pauses execution and sends the pending command to the client for approval.

### Wire Protocol

JSON lines (one object per line) over TCP/Unix socket:
- Client → Server: `{"message": "...", "session_id": "...", "response": "yes|no"}` 
- Server → Client: `{"status": "ok|confirm|error", "message": "...", "done": true|false}`

Streaming: multiple `done: false` chunks followed by one `done: true`.

### Security Model

- **safe** — read-only operations (`ls`, `cat`, `docker ps`, `git log`, …) execute immediately
- **confirm** — mutating operations (`rm`, `chmod`, `git push`, `docker stop`, …) require explicit user approval
- **forbidden** — destructive patterns (`rm -rf /`, fork bombs, `curl|bash`, …) are blocked unconditionally

### Docker Layout

`backend/docker-compose.yml` runs two services:
- `vps-agent` — mounts `/:/hostfs` (full host filesystem) and `/var/run/docker.sock`
- `telegram` — communicates with `vps-agent` over the shared Unix socket

## Environment Variables

Copy `backend/.env.example` → `backend/.env`:

| Variable | Purpose |
|----------|---------|
| `LLM_BASE_URL` | OpenAI-compatible endpoint (e.g. Gemini) |
| `LLM_API_KEY` | API key |
| `LLM_MODEL` | Model name |
| `AUDIT_LOG_PATH` | Path for audit log (default `/app/audit.log`) |
| `AGENT_SOCKET` | Unix socket path (default `/tmp/vps-agent.sock`) |
| `TCP_HOST` / `TCP_PORT` | TCP listener (default `127.0.0.1:7379`) |

Telegram also requires `clients/telegram/.env` with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS`.

## Testing

Tests live in `backend/tests/` and use `pytest-asyncio`. There is no CI pipeline — tests are run manually. Discord and WebUI clients in `clients/` are placeholder stubs with no implementation.
