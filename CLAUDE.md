# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Pipe v0.9.0** — an LLM-powered operations agent for Linux servers. It runs on the server permanently, knows it (SERVER.md, DIRECTORY), watches it (proactive alerts), draws its architecture (Mermaid diagrams), and manages other machines through agentless *targets* and parallel *workers*. Users talk to it via CLI (SSH tunnel or `kubectl port-forward`) or a Telegram bot; Discord/WebUI are placeholders. The backend runs in Docker (default), natively under systemd, or in Kubernetes, and talks to any OpenAI-compatible LLM API (default: Google Gemini).

All code comments, error messages, documentation, and LLM prompts are in **Polish**. Source files are mostly ASCII-transliterated Polish (no diacritics) in prompts/user-facing strings; docstrings use full Polish.

`AGENTS.md` predates the current code and is stale (no mention of tests, runtimes, workers). Prefer this file and the source.

## Commands

```bash
pip install -r backend/requirements.txt   # openai, python-dotenv, mermaidx, pytest, pytest-asyncio
pytest backend/tests/                     # all tests (run from repo root — imports are absolute `backend.*`)
pytest backend/tests/test_security.py -v  # single file
pytest backend/tests/test_security.py::TestClassifyCommandSafe -v   # single class
python -m backend.server                  # local run; needs backend/.env (PIPE_RUNTIME=native outside Docker)
```

Telegram tests (`test_telegram_bot.py`) are skipped unless `python-telegram-bot` is installed; diagram render tests are skipped without `mermaidx`. `backend/tests/conftest.py` forces `PIPE_RUNTIME=docker` for every test (tests describe the default deployment); tests for other modes override it. `backend/tests/fakes.py` has a scripted fake OpenAI client (`FakeClient`, `completion()`) and `assert_history_valid()` for agent-loop tests.

Production:
```bash
sudo bash scripts/install-server.sh                 # docker (default) — installs Docker, runs wizard, compose up
sudo bash scripts/install-server.sh --mode native   # venv + deploy/systemd/pipe.service
LLM_PROVIDER=gemini LLM_API_KEY=... bash scripts/install-server.sh -y   # non-interactive (uses configure --from-env)
kubectl apply -k deploy/kubernetes/overlays/telegram                     # see deploy/kubernetes/README.md
cd backend && docker compose logs -f vps-agent      # or: journalctl -u pipe -f
```

CLI install (client machine): `.\install.ps1` (Windows) or `bash install.sh` (Linux/macOS), then `pipe`.

There is no linter, formatter, or CI configured; tests are run manually.

## Architecture

### Data flow

```
CLI (laptop) --SSH tunnel / kubectl port-forward--> TCP :7379 ─┐
Telegram bot (same host / sidecar) --Unix socket----------------┤
                                                                └─> backend/server.py  (JSON lines; events -> frames)
                                                                      ├─> core/agent.py  VPSAgent.run_loop() (AGENT_MAX_ITERATIONS)
                                                                      │     └─> core/handlers/*  one async generator per tool
                                                                      │           ├─> core/security.py  classify → safe | confirm | forbidden
                                                                      │           │     └─> core/executor.py  subprocess (30 s reads / CONFIRMED_COMMAND_TIMEOUT)
                                                                      │           ├─> core/workers.py → targets.wrap() → ssh / docker exec / kubectl
                                                                      │           └─> core/infra.py + core/diagram.py → Mermaid → PNG Attachment
                                                                      └─> core/watch.py  Watcher: checks + routines → Notifier → {"command":"subscribe"} clients
```

### Key modules

| File | Role |
|------|------|
| `backend/server.py` | Unix socket + TCP; `event_frame()` maps events → frames; `_handle_command()` for client commands; `_subscribe()`; starts the Watcher |
| `backend/core/agent.py` | `VPSAgent`: `chat()`, `confirm()`, `run_loop()` (parametrised: prompt/tools/model/dispatch — reused by workers), `call_llm()`, `complete()`, `_execute_tool_confirmed()`; redacts + truncates tool results; trims history |
| `backend/core/session.py` | `Session` (history, `cwd`, `pending_confirmation`, `workers`, `learns_vibe`, `user_key`) and `ConfirmationRequest` (incl. optional `action` callback); `Session.system_prompt` |
| `backend/core/events.py` | Stream events: `str`, `Attachment` (file, e.g. PNG), `Progress` |
| `backend/core/handlers/` | Tool implementations; `common.py` has `reply()`, `format_result()`, `run_classified()` (forbidden/confirm/safe flow) |
| `backend/core/runtime.py` | `kind()` docker/native/kubernetes, `host_root()`, `host_proc()`, `to_local()`, `to_host()`, `describe()` — **no `settings` import** |
| `backend/core/security.py` | `classify_command()`, `split_command()`, `classify_file_write()`, `validate_workspace_access()` |
| `backend/core/hostinfo.py` | Host state from `/proc` (memory, load, cpus, disks, sockets) without running commands |
| `backend/core/infra.py` | Discovery (docker inspect, nginx/Caddy/Traefik routes, systemd, git repos, Kubernetes) + `to_mermaid()` |
| `backend/core/diagram.py` | Mermaid → PNG/ASCII via `mermaidx` (offline); `DiagramError` goes back to the model |
| `backend/core/targets.py` | Remote targets registry (`targets.json`), validation, `wrap()` |
| `backend/core/workers.py` | Sub-agents: `run_worker()`, `run_many()` — read-only by construction |
| `backend/core/routines.py` | Cron parser (`parse_schedule`), routine registry (`routines.json`) |
| `backend/core/watch.py` | `Watcher` (checks, alert lifecycle, routine loop), `Notifier` (pub/sub), `prompt_alerts()` |
| `backend/core/memory.py` | SERVER.md, DIRECTORY (`directory.json`), skills, VIBE (`vibe/<key>.md`), `find_secret()`, `redact_secrets()`, `prompt_context()` |
| `backend/core/vibe.py` | `VibeLearner` — background style distillation every `VIBE_EVERY` user messages |
| `backend/core/tools.py` | `TOOLS`: OpenAI function-calling schemas for the 18 tools |
| `backend/config/providers.py` | Provider presets, user providers file, `resolve_llm_config()`, model-list filtering — **stdlib only** |
| `backend/configure.py` | Setup wizard (`--check` / `--models` / `--providers` / `--from-env`) — **stdlib only** |
| `backend/config/prompts.py` | `BASE_SYSTEM_PROMPT`, `TELEGRAM_SYSTEM_PROMPT`, `cwd_block()`, worker/routine/VIBE/scan/status prompts |
| `clients/cli/cli.py` | SSH tunnel or `KubePortForward`, `rich` REPL; shows `Progress` live, saves attachments to `~/.pipe/diagrams` |
| `clients/telegram/bot.py` | Streams frames (`_run_and_reply`), photos for attachments, live progress message, `alerts_loop()` subscription with *Zbadaj* button, per-user lock with `concurrent_updates` |
| `clients/telegram/tg_format.py` | Slash-command helpers, HTML formatting (code spans literal), alert/routine/list formatting. No `telegram` import, so it is tested from `backend/tests/` |
| `deploy/` | `kubernetes/` (kustomize base + overlays operator/telegram/host-agent), `systemd/`, `cloud-init/` |

### Tool dispatch

Eighteen tools: `execute_command`, `read_file`, `write_file`, `change_directory`, `git_command`, `system_stats`, `docker_manage`, `network_info`, `cron_manage`, `diagram`, `server_md`, `directory`, `skill_manage`, `vibe`, `target_manage`, `remote_exec`, `delegate`, `routine_manage`.

`_handle_tool_call()` dispatches by name reflection: `getattr(handlers_module, f"handle_{tool_name}")` (workers pass their own `dispatch` instead). **Adding a tool means three edits:** a schema in `core/tools.py`, a `handle_<name>` async generator in `core/handlers/`, and its export in `core/handlers/__init__.py` (`test_every_tool_has_handler` checks this).

Every handler has the same signature and contract:
```python
async def handle_x(agent, session, tool_call, args) -> AsyncGenerator[Event, None]
```
- Yielded events stream to the **user**: `str` (protocol messages only), `Attachment` (e.g. a diagram), `Progress` (live status). The tool result for the **LLM** is appended by the handler itself (`common.reply()`). Every path must append exactly one tool message **or** set `session.pending_confirmation`. `_handle_tool_call()` backstops a handler that forgets or raises, and `run_loop()` answers the remaining tool calls of a turn with `SKIPPED_FOR_CONFIRMATION` once one is pending — the history sent to the provider must always be valid (`assert_history_valid`).
- Never yield raw command output — the model interprets it; yielding it too shows the user the same thing twice.
- For shell-running tools use `common.run_classified(session, tool_call, command, tool_name=..., inner=...)`: `command` is exactly what runs and is shown; `inner` is what gets classified when `command` is a wrapper (ssh/kubectl).
- Wrap any command or path shown in a protocol message with `as_code()` (`backend/core/text.py`), never literal backticks.
- A handler that yields nothing still has to be an async generator — trailing unreachable `yield` after `return`.
- Tool results are post-processed centrally: `memory.redact_secrets()` (unless `REDACT_SECRETS=0`) and `truncate_result()` (24k chars). `write_file` refuses content containing `[ZREDAGOWANO`.

### Confirmation round-trip

`confirm` never re-enters the handler. `agent._execute_tool_confirmed()` replays from the stored `ConfirmationRequest`: an `action` callback (registry changes such as adding a target or routine — `command` is then only a description), `write_file` (path + content), or else `ConfirmationRequest.command` as a ready-made shell command run with `CONFIRMED_COMMAND_TIMEOUT`. A new user message while a confirmation is pending answers the tool call with `ABANDONED_CONFIRMATION`.

### Runtime and path convention

`core/runtime.py` decides how the managed host is seen (`PIPE_RUNTIME`, auto-detected; `HOST_ROOT`/`HOST_PROC` override):
- **docker** — host root at `/hostfs` (ro, `/root` rw), host `/proc` at `/hostproc`, `pid: host`, `docker.sock`.
- **native** — everything direct (`host_root() == ""`, `/proc`). `settings` rewrites the Ollama preset's `host.docker.internal` to `127.0.0.1`.
- **kubernetes** — pod with a ServiceAccount; `/hostfs` only with the `host-agent` overlay.

`Session.cwd` and everything the model sees are **host** paths; `runtime.to_local()` converts to what the process sees, `runtime.to_host()` normalises model input (accepts host, prefixed and relative paths) so `cwd` never contains the prefix. Handlers never hardcode `/hostfs`/`/hostproc`. `system_stats`, `network_info`, the watcher and `infra` read host state via `hostinfo` (`/proc/1/net/*` for host sockets because the container has its own netns). `Session.system_prompt` order: base → `runtime.describe()` → memory (SERVER.md, DIRECTORY, skills, VIBE) → alerts → cwd block **last** (provider prefix caching).

### Agent memory

`memory.prompt_context(user_key)` appends SERVER.md (≤12k chars), DIRECTORY (≤60 entries), the skill index, and the user's VIBE note. Files live in `DATA_DIR` (container `/app/data`, host `backend/data/`, gitignored) together with `targets.json`, `routines.json`, `watch_state.json`, `known_hosts`, audit log. Writes are audited by path only and rejected by `find_secret()`; remote URLs are stripped of credentials. Skill save and full SERVER.md overwrite require confirmation (persistence vectors); other writes emit a `[PAMIEC]` user notice. Memory is framed as data, not instructions. Keep `memory.py` free of `settings` imports so it stays testable via `DATA_DIR`. VIBE keys come from `interface` (`cli:kuba` → `cli-kuba`); backend-built messages are marked `pipe_generated` (stripped before the provider call by `call_llm`) and never teach VIBE.

### Workers, targets, routines, watch

- `targets.wrap()` builds `ssh -o BatchMode=yes ... -- 'cmd'`, `docker exec c sh -c 'cmd'`, `kubectl --context/--namespace ...` or `kubectl exec`; all fields are regex-validated and the command is one `shlex.quote`d argument. Classification is on the inner command.
- Workers run `agent.run_loop()` with `WORKER_TOOLS` (`run`) and a custom `dispatch`: `safe` executes, `confirm` becomes a proposal (never executed), `forbidden` is refused. History lives in `parent.workers[name]`.
- Routines run as workers from `Watcher._routine_loop()`; reports and alerts go through `Notifier.publish()` to subscribed clients.
- The Watcher is deterministic (no LLM). Persistent findings (disk, memory, load, restarting/unhealthy containers) fire once and resolve; transient ones (new public port, stopped container) fire once.

### Wire protocol (JSON lines, both transports)

```
client → server   {"message": "...", "session_id": "<uuid>", "interface": "cli:<user>" | "telegram:<id>", "token": "..."}
client → server   {"confirm": true|false, "session_id": "<uuid>"}
client → server   {"command": "<name>", "session_id": "<uuid>", "name"?, "args"?, "id"?}
server → client   {"response": "...", "status": "ok" | "confirm" | "error", "done": false}   × N
                  + optional "attachment" {name, mime, caption, data(base64), source, text} / "event" {type: progress|alert|routine|subscribed, ...}
server → client   {"response": "", "status": "ok", "done": true}      (+ "data" for data commands)
```
Commands: `list_skills`, `server_md`, `directory`, `vibe`, `alerts`, `targets`, `routines`, `history` (reply with `data`); `scan_server`, `status`, `run_skill`, `investigate` (stream; prompts built server-side from `prompts.py` — clients never compose prompts); `diagram` (infra map without LLM); `subscribe` (connection stays open, pushes watcher events). Frames with attachments are large — both sides use `READ_LIMIT` (server 4 MiB, clients 32 MiB). Full spec: `docs/protocol.md`.

`token` is required only when `AGENT_TOKEN` is set, and is checked for **every** request type (including `confirm` and `subscribe`). `server.py` derives `status` by string-matching chunk text (`[POTWIERDZ]` + `wymaga potwierdzenia` → confirm, `[BLAD]`/`[ODMOWA]` → error), so those tags in handler output are load-bearing. The prompt tells the model **not** to emit `[POTWIERDZ]` itself.

### Security model

`classify_command()`: forbidden patterns on the whole string; then `split_command()` splits on `;`, `|`, `||`, `&&`, `&`, newlines **outside quotes** and reports write redirects (`>` except `/dev/null`, `2>&1`) and dynamic constructs (`$(`, backticks, `<(`) — either forces `confirm`. Each segment: forbidden → `SENSITIVE_PATTERNS` (shadow, SSH keys, `.env`, `/proc/<pid>/environ|root`, globs in secret dirs, `kubectl get secret -o`) → `CONFIRM_PATTERNS` → token-level `SAFE_PREFIXES` match (`ss` ≠ `ssh`) plus argument analysers: `_check_git` (blocks program-executing flags `-c core.pager=`, `--upload-pack`, `--exec`, `-O`), `_check_curl` (write flags, non-GET incl. `--request=`/`-XPOST`, **and external host → confirm to stop exfiltration**; `_host_is_local` allows localhost/private/`host.docker.internal`), `_kubectl_dumps_secret`, `find -exec/-delete`, `sort --compress-program`, `journalctl --vacuum`. Unrecognised → `confirm` (fail-closed). Forbidden results are never surfaced as retryable errors. Regression cases: `test_security_hardening.py`, `test_security_review_fixes.py`.

`validate_workspace_access()` resolves paths and blocks outside the host root and `FORBIDDEN_HOST_PATHS` (+ `/home/*/.ssh`). File **contents** are never written to the audit log; `_format_entry` escapes `\n`/`\r` so a command can't forge log lines.

`as_code()` (`core/text.py`) neutralises control/bidi chars (ANSI, `\r`, U+202E) in anything shown for confirmation — what you see is what runs. `write_file` confirmation shows a unified diff vs current content and warns on startup files (`.bashrc`, `authorized_keys`, cron, systemd). `memory.redact_secrets()` also covers JWTs, URL credentials (`user:pass@`), shadow hashes, and doesn't cross NUL (so it can't swallow `/proc/*/environ`).

Persistence defence: `skill_manage save` and `server_md` full `write` require confirmation (via the `action` callback, with content/diff preview) — `memory.check_server_md()` / `validate_skill_content()` validate without writing so nothing lands before TAK. Every other memory write yields a `[PAMIEC]` notice; `tg_format.collect_response_text` surfaces those to Telegram even when it collapses to one fragment.

`server.py` compares the token with `hmac.compare_digest`. `settings.validate()` requires `AGENT_TOKEN` in kubernetes mode (ClusterIP reachable from any pod); the wizard, `--from-env` and the installer auto-generate one when empty (`ensure_agent_token`), and the installer syncs it into the Telegram `.env`. `deploy/kubernetes/base/networkpolicy.yaml` denies pod ingress (port-forward still works). `diagram.render` has a `RENDER_TIMEOUT`.

`docker.sock` is effectively root on the host; `docker run/exec` always require confirmation, but the real guard is the user's TAK (documented in `docs/security.md`).

## Environment

`backend/.env` is written by `python3 -m backend.configure` (or `--from-env`, or copied from `backend/.env.example`): `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, optional `LLM_BASE_URL` / `LLM_REASONING_EFFORT` / `LLM_TIMEOUT`, then `AGENT_TOKEN`, `AUDIT_LOG_PATH`, `AGENT_SOCKET`, `TCP_HOST`, `TCP_PORT`, `DATA_DIR`, and optional `PIPE_RUNTIME`, `HOST_ROOT`, `HOST_PROC`, `AGENT_MAX_ITERATIONS`, `CONFIRMED_COMMAND_TIMEOUT`, `REDACT_SECRETS`, `WORKER_MODEL`, `WORKER_MAX_ITERATIONS`, `WORKER_TIMEOUT`, `MAX_WORKERS`, `VIBE_EVERY`, `WATCH_ENABLED`, `WATCH_INTERVAL`, `WATCH_DISK_PCT`, `WATCH_MEM_PCT`, `WATCH_LOAD_FACTOR` (all in `config/settings.py`). `settings` never raises at import; `settings.validate()` fails fast on an unknown provider, a missing model, a missing key, or (in kubernetes mode) a missing `AGENT_TOKEN`.

Telegram needs `clients/telegram/.env` with `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, a matching `AGENT_TOKEN` if set, and optionally `TELEGRAM_ALERTS=0`. The CLI takes the token via `--token` or `AGENT_TOKEN`; `--kube NAMESPACE` / `--kube-context` connect through `kubectl port-forward`.

`TCP_HOST` defaults to `0.0.0.0` in `settings.py` because the process runs inside the container; compose publishes `127.0.0.1:7379:7379`, native installs set `TCP_HOST=127.0.0.1`, Kubernetes uses a ClusterIP Service. Never publish that port on a public interface.

`backend/.dockerignore` keeps `.env` and `data/` out of the image — it is committed on purpose.

## Versioning and shipping

`VERSION` in the repo root is the single source of truth (`X.Y.Z`). The same string is duplicated in README, `docs/*.md` (including `features.md`, `deploy.md`), backend docstrings, `config/prompts.py`, the CLI banner and the Telegram bot — never hand-edit those; run `python scripts/bump_version.py patch|minor|major|X.Y.Z`, which rewrites every site from the `PATTERNS` list. A new hardcoded version site must be added to that list.

The `/ship` skill (`.claude/skills/ship/SKILL.md`) is the release workflow: bump version → update `docs/changelog.md` and any docs the change invalidates → run tests → commit → push. Changelog entries and commit messages are Polish.

## Known limitations

- Sessions (history, pending confirmations, worker histories) are in process memory — a backend restart forgets them; the Kubernetes Deployment therefore runs one replica.
- `cron_manage` can only list host cron in docker/kubernetes mode (host root is read-only); add/remove works in native mode. Agent-run schedules belong in routines.
- The CLI cannot receive watcher events (its REPL blocks on input); it shows them via `/alerty`. Telegram receives them live.
- `AGENTS.md` is stale.
