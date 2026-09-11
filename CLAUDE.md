# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Pipe v0.6.0** — an autonomous LLM-powered agent for Linux VPS server management. Users interact via CLI (SSH tunnel), Telegram bot, or planned Discord/WebUI clients. The backend runs on a VPS inside Docker, uses an OpenAI-compatible LLM API (default: Google Gemini), and executes shell commands behind a three-tier security classifier.

All code comments, error messages, documentation, and LLM prompts are in **Polish**. Source files are mostly ASCII-transliterated Polish (no diacritics) in prompts/user-facing strings; docstrings use full Polish.

`AGENTS.md` predates the current code and is partly stale (it claims there is no test framework — there is). Prefer this file and the source.

## Commands

```bash
pip install -r backend/requirements.txt   # openai, python-dotenv, pytest, pytest-asyncio
pytest backend/tests/                     # all tests (run from repo root — imports are absolute `backend.*`)
pytest backend/tests/test_security.py -v  # single file
pytest backend/tests/test_security.py::TestClassifyCommandSafe -v   # single class
python -m backend.server                  # local run; needs backend/.env
```

Production (on the VPS):
```bash
cd backend && docker-compose up -d
docker-compose logs -f vps-agent    # or: -f telegram
```

CLI install (client machine): `.\install.ps1` (Windows) or `bash install.sh` (Linux/macOS), then `pipe`.

There is no linter, formatter, or CI configured; tests are run manually.

## Architecture

### Data flow

```
CLI (laptop) --SSH tunnel--> TCP 127.0.0.1:7379 ─┐
Telegram bot (same host) --Unix /tmp/vps-agent.sock ─┤
                                                  └─> backend/server.py   (JSON-lines listener, both transports share handle_client)
                                                        └─> core/agent.py  VPSAgent singleton, LLM tool-calling loop (MAX_TOOL_ITERATIONS = 10)
                                                              └─> core/handlers/*  one async generator per tool
                                                                    └─> core/security.py  classify → safe | confirm | forbidden
                                                                          └─> core/executor.py  asyncio subprocess, TIMEOUT_SECONDS = 30
                                                                                └─> core/audit.py  append-only log
```

### Key modules

| File | Role |
|------|------|
| `backend/server.py` | Serves Unix socket + TCP concurrently; maps chunk text → protocol `status`; optional token check |
| `backend/core/agent.py` | `VPSAgent`: session store, `chat()`, `confirm()`, `_run_agent_loop()`, `_execute_tool_confirmed()` |
| `backend/core/session.py` | `Session` (history, `cwd`, `pending_confirmation`) and `ConfirmationRequest` dataclasses; `Session.system_prompt` builds the per-interface prompt |
| `backend/core/handlers/` | Tool implementations, one module per domain; `__init__.py` re-exports `handle_<tool_name>` |
| `backend/core/security.py` | `classify_command()`, `classify_file_write()`, `validate_workspace_access()` |
| `backend/core/executor.py` | Module-level `execute()`, `read_file()`, `write_file()` — not a class |
| `backend/core/tools.py` | `TOOLS`: OpenAI function-calling schemas for the 9 tools |
| `backend/config/providers.py` | Provider presets, user providers file, `resolve_llm_config()`, model-list filtering — **stdlib only** |
| `backend/configure.py` | Interactive setup wizard (`python3 -m backend.configure`, also `--check` / `--models` / `--providers`) — **stdlib only** |
| `backend/config/prompts.py` | `BASE_SYSTEM_PROMPT` + `TELEGRAM_SYSTEM_PROMPT` (Telegram variant mandates HTML, not Markdown) |
| `clients/cli/cli.py` | Spawns/manages the SSH tunnel, then a `rich` REPL |
| `clients/telegram/bot.py` | Unix-socket client, per-`user_id` session, inline TAK/NIE confirm keyboard, user-ID whitelist |
| `clients/telegram/tg_format.py` | Backend text → Telegram HTML: code spans kept literal, everything outside allowed tags escaped. No `telegram` import, so it is tested from `backend/tests/` |

### Tool dispatch

Nine tools: `execute_command`, `read_file`, `write_file`, `change_directory`, `git_command`, `system_stats`, `docker_manage`, `network_info`, `cron_manage`.

`_handle_tool_call()` dispatches by name reflection: `getattr(handlers_module, f"handle_{tool_name}")`. **Adding a tool means three edits:** a schema in `core/tools.py`, a `handle_<name>` async generator in `core/handlers/`, and its export in `core/handlers/__init__.py`. Name the function exactly `handle_<tool_name>` or dispatch silently falls through to "Nieznane narzedzie".

Every handler has the same signature and contract:
```python
async def handle_x(agent, session, tool_call, args) -> AsyncGenerator[str, None]
```
- Yielded strings stream to the **user**; the tool result for the **LLM** is appended by the handler itself to `session.messages` as `{"role": "tool", "tool_call_id": ..., "content": ...}`. Every path must append exactly one such entry, or the next LLM call fails on an unanswered tool call.
- Only yield protocol messages (`[POTWIERDZ]`, `[ODMOWA]`). Never yield raw command output — the system prompt tells the model to interpret rather than echo it, so yielding it too shows the user the same thing twice.
- Wrap any command or path shown in a protocol message with `as_code()` (`backend/core/text.py`), never with literal backticks. It picks a fence longer than any backtick run inside and uses a block for multi-line text, so both clients (CLI via `rich.Markdown`, Telegram via `tg_format`) render the value exactly. For a confirmation, show exactly the string stored in `ConfirmationRequest.command` — that is what runs after TAK.
- A handler that yields nothing still has to be an async generator — the codebase uses a trailing unreachable `yield` after `return` for this.
- Setting `session.pending_confirmation` aborts the loop; `server.py` sends `status: "confirm"` and waits for the client's confirm frame.

### LLM providers

Every provider is reached through the OpenAI Chat Completions API via one `AsyncOpenAI` client — there are no per-provider code paths, only presets (`BUILTIN_PROVIDERS` in `config/providers.py`: base URL, default model, key env vars). `resolve_llm_config()` turns env vars into an `LLMConfig`: explicit `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` override the preset chosen by `LLM_PROVIDER`; with no `LLM_PROVIDER` the provider is inferred from `LLM_BASE_URL` (pre-0.5 `.env` files); with nothing set it falls back to Gemini. User-defined providers live in a JSON file (`LLM_PROVIDERS_FILE`, default `backend/data/providers.json`, mounted at `/app/data` in the container) and override built-ins with the same `id`.

Model lists are never hardcoded: the wizard and `VPSAgent.verify_model()` (run in the background at server start) fetch `GET /models` and filter it with `chat_model_ids()` — drops non-chat models by ID markers, honours OpenRouter-style `supported_parameters`, newest first. When adding a provider, prefer relying on this over pinning model names.

`providers.py` and `configure.py` must stay **stdlib-only** — the wizard runs on a bare server before `pip install`/Docker. `_call_llm` deliberately sends no `tool_choice` (it is the default with `tools`, and some providers such as Ollama don't accept it) and passes `LLM_REASONING_EFFORT` through `extra_body` so it works on any SDK version.

### Confirmation round-trip

`confirm` never re-enters the handler. `agent._execute_tool_confirmed()` re-executes from the stored `ConfirmationRequest`: `write_file` replays path + content, and every other `tool_name` is treated as a ready-made shell command taken from `ConfirmationRequest.command`. So a handler that requests confirmation must store a runnable command string there — it does not need its own branch.

### `/hostfs` path convention

The container mounts the host root at `/hostfs` (read-only) with `/root` re-mounted read-write over it. `Session.cwd` holds the **host-side** path (starts at `/`); the prompt instructs the LLM to prefix it with `/hostfs` for file work, and `agent.py` runs commands with `cwd=f"/hostfs{session.cwd}"`. `validate_workspace_access()` resolves a path and rejects anything outside `/hostfs` or inside the blocklist (`/hostfs/boot`, `/proc`, `/sys`, `/usr/bin`, `/hostfs/root/.ssh`, …). Host `/proc` is separately mounted at `/hostproc`, and `system_stats` reads from there so figures describe the VPS, not the container; the Dockerfile also ships a shim `/usr/local/bin/free` backed by `/hostproc/meminfo`.

### Wire protocol (JSON lines, one object per line, both transports)

```
client → server   {"message": "...", "session_id": "<uuid>", "interface": "cli" | "telegram:<user_id>", "token": "..."}
client → server   {"confirm": true|false, "session_id": "<uuid>"}
server → client   {"response": "...", "status": "ok" | "confirm" | "error", "done": false}   × N
server → client   {"response": "", "status": "ok", "done": true}
```
`token` is required only when `AGENT_TOKEN` is set in `backend/.env`; it is checked for **every** request type, including `confirm`. `server.py` derives `status` by **string-matching the chunk text** (`[POTWIERDZ]`, `[BLAD]`, `[ODMOWA]`), so those literal tags in handler output and in `prompts.py` are load-bearing protocol, not cosmetics — clients key their confirm UI off the resulting `status`.

### Security model

Classification order in `classify_command()`: forbidden patterns are checked against the whole string first (to catch `curl … | bash`), then the command is split on `;`, `|`, `||`, `&&` and each segment classified independently — any forbidden segment poisons the whole command, any confirm segment forces confirm. **Unrecognized commands default to `confirm`** (fail-closed); only an explicit `SAFE_PREFIXES` match is `safe`. `classify_file_write()` forbids `/etc/passwd`, `/etc/shadow`, `/boot/`, `/dev/`; every other write is `confirm`. Forbidden results are never surfaced to the LLM as a retryable error — the agent refuses and logs via `audit.log_blocked()`.

File **contents** are deliberately never written to the audit log (they may hold secrets).

## Environment

`backend/.env` is written by `python3 -m backend.configure` (or copied from `backend/.env.example`): `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, optional `LLM_BASE_URL` / `LLM_REASONING_EFFORT` / `LLM_TIMEOUT`, then `AGENT_TOKEN`, `AUDIT_LOG_PATH`, `AGENT_SOCKET`, `TCP_HOST`, `TCP_PORT`. `settings` never raises at import; `settings.validate()` fails fast on an unknown provider, a missing model, or a missing key for a provider that requires one. `AGENT_TOKEN` is optional and empty means no auth.

Telegram needs `clients/telegram/.env` with `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` and — if the backend sets one — a matching `AGENT_TOKEN`. The CLI takes the same value via `--token` or the `AGENT_TOKEN` env var.

`TCP_HOST` defaults to `0.0.0.0` in `settings.py` because the process runs inside the container; the compose file publishes the port as `127.0.0.1:7379:7379`, so host exposure is controlled there, not in the app. Do not publish that port on a public interface — access is meant to go through the SSH tunnel.

## Versioning and shipping

`VERSION` in the repo root is the single source of truth (`X.Y.Z`). The same string is duplicated in README, `docs/*.md`, backend docstrings, `config/prompts.py`, the CLI banner and the Telegram bot — never hand-edit those; run `python scripts/bump_version.py patch|minor|major|X.Y.Z`, which rewrites every site from the `PATTERNS` list. A new hardcoded version site must be added to that list.

The `/ship` skill (`.claude/skills/ship/SKILL.md`) is the release workflow: bump version → update `docs/changelog.md` and any docs the change invalidates → run tests → commit → push. Changelog entries and commit messages are Polish.

## Known inconsistencies to be aware of

- `agent.py` still carries the pre-refactor `VPSAgent._handle_*` methods (~500 lines). Dispatch no longer reaches them — they are dead code kept for reference; prefer `core/handlers/` when changing tool behaviour.
- `cron_manage` is non-functional: the schema in `tools.py` sends `operation` / `schedule` / `command`, but the handler reads `action` / `cron_entry`, so every call falls through to `crontab -l`. The Docker image also has no `crontab` binary, and the host's cron is not reachable from the container. The add/remove branches build entry-scoped, `shlex`-quoted commands; they must never go back to `crontab -r`.
- `AGENTS.md` is stale in places (claims there is no test suite, points at `agent.py:_handle_*` as the handler pattern).
