# AGENTS.md

## Domain Knowledge

**PipeClaw** (v0.3) is an autonomous AI agent for VPS/Linux server management that communicates across multiple interfaces — CLI (SSH tunnel), Telegram bot, and Discord (planned). The backend runs on a server and executes commands through an LLM agent with security classification (safe/confirm/forbidden). End users interact via their preferred interface; the system is designed for managing Linux servers with full file I/O, shell execution, Git, Docker, system stats, networking, and cron management. Key integrations: OpenAI-compatible LLM API (Gemini, OpenAI, Groq, Anthropic), Unix socket for same-machine clients, TCP for remote SSH-tunneled access, Docker socket for container management.

## Commands

> Source: Backend docker-compose, package.json-style setup via `install.sh` / `install.ps1`

| Task | Command | Notes |
|------|---------|-------|
| Start backend + Telegram | `cd backend && docker-compose up -d` | Builds and runs vps-agent + telegram bot |
| Stop all services | `cd backend && docker-compose down` | Graceful shutdown |
| View backend logs | `cd backend && docker-compose logs -f vps-agent` | Real-time agent logs |
| View Telegram logs | `cd backend && docker-compose logs -f telegram` | Real-time Telegram bot logs |
| Setup CLI (Windows) | `.\install.ps1` | Auto-configures SSH tunnel alias |
| Setup CLI (Linux/macOS) | `bash install.sh` | Auto-configures SSH tunnel alias |
| Run backend locally | `python -m backend.server` | Requires `.env` setup + dependencies |
| Install backend deps | `pip install -r backend/requirements.txt` | openai, python-dotenv |
| Install CLI deps | `pip install -r clients/cli/requirements.txt` | rich library |
| Install Telegram deps | `pip install -r clients/telegram/requirements.txt` | python-telegram-bot |

## File Map

```
pipeclaw/
├── backend/                    Python async server: Unix socket + TCP listener
│   ├── server.py              Entry point (python -m backend.server)
│   ├── config/                Settings, prompts, environment loading
│   ├── core/                  Agent logic: agent.py, executor.py, security.py, audit.py, tools.py
│   ├── requirements.txt        openai>=1.30.0, python-dotenv>=1.0.0
│   ├── Dockerfile             Python 3.11-slim, mounts /hostfs for full server access
│   └── docker-compose.yml     vps-agent + telegram services, volumes, networking
│
├── clients/
│   ├── cli/                   Interactive REPL with SSH tunnel management
│   │   ├── cli.py            Main entry point, rich terminal UI
│   │   └── requirements.txt   rich>=13.0.0
│   ├── telegram/              Async Telegram bot client
│   │   ├── bot.py            Message handling, confirmations, whitelist
│   │   ├── Dockerfile        Python 3.11-slim, Unix socket comms
│   │   └── requirements.txt   python-telegram-bot>=20.0, python-dotenv
│   ├── discord/               Placeholder (PR welcome)
│   └── webui/                 Placeholder (PR welcome)
│
├── docs/                       Documentation (Polish)
│   ├── backend.md            Backend setup, tools, user creation
│   ├── cli.md                CLI configuration, SSH tunneling
│   ├── telegram.md           Telegram bot setup, whitelist
│   ├── protocol.md           JSON request/response protocol
│   ├── security.md           Security model, command classification
│   ├── quickstart.md         Step-by-step setup guide
│   └── changelog.md          Version history
│
├── install.ps1               Windows CLI setup script
├── install.sh                Linux/macOS CLI setup script
└── README.md                 Project overview (Polish)
```

### Tech Stack

- **Backend**: Python 3.11+, asyncio, OpenAI SDK (`openai>=1.30.0`), python-dotenv
- **CLI**: Python 3.11+, Rich library for terminal UI, SSH subprocess management
- **Telegram**: Python 3.11+, python-telegram-bot, asyncio
- **Deployment**: Docker 24+, Docker Compose v2, Unix/TCP sockets
- **Supported LLMs**: OpenAI-compatible API (Google Gemini, OpenAI, Groq, Anthropic)

## Golden Samples (follow these patterns)

| For | Reference | Key patterns |
|-----|-----------|--------------|
| **Async generators** | `backend/core/agent.py:chat()` | `async def chat(...) -> AsyncGenerator[str, None]:` with `yield` and error handling |
| **Async tool handlers** | `backend/core/agent.py:_handle_execute_command()` | `async def _handle_...() -> tuple[str, str, int]:` with try/except, timeout handling |
| **Type hints** | `backend/core/executor.py` | Python 3.10+ syntax: `str \| None`, `Literal["safe", "confirm", "forbidden"]`, `dict[str, str]` |
| **Error handling** | `backend/core/executor.py:execute()` | Specific exceptions (FileNotFoundError, PermissionError, OSError), chaining with `from exc` |
| **Import order** | `backend/core/agent.py` (lines 1-20) | `from __future__ import annotations` → stdlib → third-party → local imports (absolute: `from backend.core...`) |
| **Command classification** | `backend/core/security.py` | regex patterns in SAFE_PREFIXES, CONFIRM_PATTERNS, FORBIDDEN_PATTERNS lists |
| **JSON protocol** | `clients/cli/cli.py` (request/response) | Client sends `{"message": "...", "session_id": "uuid", "interface": "cli"}`, server yields `{"response": "...", "status": "...", "done": true}` line-by-line |
| **Async subprocess** | `backend/core/executor.py:execute()` | `asyncio.create_subprocess_shell()` + `asyncio.wait_for(..., timeout=...)` for safe execution |

## Boundaries

### Always

- Show test output or audit log entries as evidence of work completion (use `tail -f audit.log`)
- Respect security classifications (safe/confirm/forbidden) — do not bypass them
- Maintain 30-second timeout for all command executions (never increase TIMEOUT_SECONDS)
- Log all changes to audit log via `backend/core/audit.py` functions

### Ask First

- Adding new LLM provider integrations or changing `LLM_*` config vars
- Modifying tool definitions or adding new tools to `backend/core/tools.py`
- Changing command classification rules (SAFE_PREFIXES, CONFIRM_PATTERNS, FORBIDDEN_PATTERNS)
- Adding new client interfaces (Discord, WebUI)
- Creating database schema or persistent storage (currently all state is ephemeral)
- Modifying Docker volumes or container permissions

### Never

- Store secrets, API keys, or passwords in code — use `.env` file only
- Bypass security classification or disable confirmation requests
- Execute dangerous commands (rm -rf, format, etc.) without explicit confirmation
- Remove or modify audit log entries
- Create test data or fixtures that clutter the server filesystem

## Codebase State

- **No test framework**: Add pytest + pytest-asyncio if implementing test suite
- **No linting**: Code follows PEP 8 manually (consider Black, ruff, mypy)
- **No CI/CD**: Consider GitHub Actions workflow for automated testing
- **Polish language**: Code comments, documentation, and error messages are in Polish to match codebase conventions
- **v0.4.1 status**: Early version; Discord and WebUI clients are placeholders
- **Known limitation**: Tool calling loop has max 10 iterations (MAX_TOOL_ITERATIONS=10) — prevents infinite loops but may require manual intervention for complex tasks
- **SSH tunnel dependency**: CLI relies on local SSH binary; will fail on restricted environments

## Terminology

| Term | Means |
|------|-------|
| **vps-agent** | Backend Docker service that runs the LLM agent loop |
| **tool calling** | LLM requesting execution of a tool (execute_command, read_file, etc.); agent calls it, yields results, repeats |
| **session_id** | UUID per user/conversation; tracks context across multiple messages |
| **classification** | Security verdict for a command: `safe` (execute immediately), `confirm` (requires user approval), `forbidden` (blocked always) |
| **Unix socket** | `/tmp/vps-agent.sock` — local IPC for Telegram bot ↔ backend (same machine) |
| **SSH tunnel** | CLI forwards TCP 7379 from laptop to server via SSH; backend listens on TCP 127.0.0.1:7379 inside container |
| **confirm** | Telegram/CLI user must approve a `confirm` classification before backend executes the command |
| **interface** | Client type: `cli` (terminal), `telegram` (Telegram bot), `discord` (planned) |
| **audit log** | Append-only file (`/app/audit.log`) tracking all executed commands with timestamp, classification, and exit code |

## When instructions conflict

Explicit user prompts override this file. If you encounter ambiguity, check `docs/security.md` for threat model or `docs/protocol.md` for wire format details.
