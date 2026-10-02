# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Pipe v0.21.1** — an LLM-powered operations agent for Linux servers. It runs on the server permanently, knows it (SERVER.md, DIRECTORY), remembers how it changes (hourly snapshots, metric history), watches it (proactive alerts, zero-config cert/site/backup checks, morning digest), draws its architecture (Mermaid diagrams), and manages other machines through agentless *targets* and parallel *workers*. Users talk to it via CLI (SSH tunnel or `kubectl port-forward`), a local web UI (`pipe web`) or a Telegram bot; Discord is a placeholder. The backend runs in Docker (default), natively under systemd, or in Kubernetes, and talks to any OpenAI-compatible LLM API (default: Google Gemini).

Code comments, docstrings and `docs/` are in **Polish** (ASCII-transliterated in prompts/user-facing strings). Everything the user or the model sees exists in two languages, selected by `PIPE_LANG=pl|en` (default `pl`) — see *Language* below. `README.en.md` is the English README.

`AGENTS.md` predates the current code and is stale (no mention of tests, runtimes, workers). Prefer this file and the source.

## Commands

```bash
pip install -r backend/requirements.txt   # openai, python-dotenv, mermaidx, pytest, pytest-asyncio
pytest                                    # all tests (pytest.ini puts the repo root on sys.path — imports are absolute `backend.*`)
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

There is no linter or formatter. CI (`.github/workflows/ci.yml`, on push to `main` and PRs) runs pytest on Python 3.11 and 3.13 with the Telegram requirements installed, builds both Docker images without pushing, renders every kustomize overlay, and runs `bash -n` on the install scripts.

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
                                                                      └─> core/watch.py  Watcher: checks + metrics + snapshots + zero-config checks + digest + routines
                                                                                         → Notifier → {"command":"subscribe"} clients
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
| `backend/core/reminders.py` | One-off reminders (`reminders.json`): `parse_delay()`/`parse_at()`, kinds `message` (no LLM, no confirmation, `[PAMIEC]` notice) and `task` (read-only worker, confirmation); fired by `Watcher._reminder_loop()` (second precision, wakes on `reminders_changed()`), kept as `fired` when nobody subscribes and delivered on subscribe or claimed by the CLI |
| `backend/core/selfupdate.py` | Tool `pipe_update` (check / apply): Pipe cannot rebuild the container it runs in, so `start()` launches a detached helper container from the agent's own image (repo mounted at its host path, `docker.sock`) that runs `git pull --ff-only` + the standalone `/usr/local/libexec/pipe/docker-compose -p <project> up -d --build <running services>`; install data comes from the container's compose labels, requester and old version are stored as helper labels, `follow()` / `resume()` (server start) deliver the result as a `message` reminder. The tool takes no URL/branch/command. `backend/version.py` holds `VERSION` |
| `backend/core/watch.py` | `Watcher` (checks, alert lifecycle with per-group `scope`, snapshot/checks loops, clock loop: routines + digest), `Notifier` (pub/sub), `prompt_alerts()` |
| `backend/core/metrics.py` | Metric samples → `metrics.jsonl`; bucketed series → Mermaid `xychart-beta` chart + numeric summary for the model |
| `backend/core/snapshots.py` | Host snapshots (packages, containers + image id, ports, systemd, cron, users, SSH key fingerprints, config hashes), saved only on change; `diff()`, `timeline()`, `[BEZPIECZENSTWO]` marks |
| `backend/core/checks.py` | Zero-config checks: TLS expiry / HTTPS / DNS for domains from proxy config and container labels, backup freshness for DIRECTORY `[backup]` entries |
| `backend/core/digest.py` | Morning digest (no LLM): health, alerts, changes since yesterday, checks, updates, routines, LLM usage |
| `backend/core/posture.py` | Security audit (score, findings with exact fix commands; `sshd_set()` writes `sshd_config.d/00-pipe-*.conf`), sentinel (users, SSH key fingerprints, SUID, auth configs every `WATCH_INTERVAL`; paths changed by Pipe per journal don't alert), incremental SSH auth log (bruteforce, password login from a guessing IP, new user@ip) |
| `backend/core/welcome.py` | One-time post-install welcome event (infra map + audit top 3 + what is watched), flag `welcome.json` |
| `backend/core/incidents.py` | Incident memory: opened on alert `new` (via `Watcher._publish`), investigation answer stored by server `investigate` / webhook worker, closed on `resolved` with journal entries from the window; `context_for(key)` feeds the next investigation, `Alert.history` shows it |
| `backend/core/webhooks.py` | Stdlib HTTP server (`WEBHOOK_PORT`, needs `WEBHOOK_TOKEN`) for Alertmanager/Grafana/Uptime Kuma/GitHub/generic; `Watcher.external()` (firing/resolved) and rate-limited `investigate_external()` worker |
| `backend/core/voice.py` | Voice transcription via OpenAI-compatible `/audio/transcriptions` (`STT_*`, auto for openai/groq) |
| `backend/tokens.py` | Client tokens with roles (`python3 -m backend.tokens add/list/revoke`), sha256 in `tokens.json` — stdlib only |
| `backend/core/mcp/` | MCP without an SDK. `server.py`: dispatcher for both eras (2026-07-28 `server/discover` + `_meta` version; legacy `initialize`), tools `run_command` (safe → run, confirm → `approvals`, forbidden → error), `read_file` (sensitive refused), knowledge tools, `ask_pipe` (viewer role); `McpHttpServer` (Origin check, Bearer = Pipe token, header validation). `client.py`: stdio (minimal env — never Pipe secrets) and HTTP transports, era probe via `server/discover`. `registry.py`: `mcp.json` (0600), `needs_confirmation()` policy (`autoApprove`, `trustReadOnly`), `mcp__<server>__<tool>` names (≤64) |
| `backend/core/approvals.py` | In-memory approvals for MCP agents (TTL 30 min); `decide()` runs through `safety.guarded()`; event `approval` → Telegram buttons / CLI `/zgody` |
| `backend/core/auth.py` | `authorize(token)` → (identity, role), shared by the JSON-lines server and MCP HTTP |
| `backend/core/safety.py` | Safety fuse: `plan_command()` / `plan_write()` (files to back up, pre-checks, post-verification, inverse commands, auto-restore) from command lexing, no LLM; `guarded()` executes a plan and yields `Progress` + `Outcome` |
| `backend/core/journal.py` | Change journal (`journal/<hex id>/`): file backups (0600), git HEAD, crontab, inverse commands; `rollback()` backs up current state first (undo is undoable); inverse commands re-classified (forbidden skipped) |
| `backend/core/usage.py` | Token/cost accounting per day/who/model (`usage.json`), `check_budget()` for daily limits — **no `settings` import** |
| `backend/core/memory.py` | SERVER.md, DIRECTORY (`directory.json`), skills, VIBE (`vibe/<key>.md`), `find_secret()`, `redact_secrets()`, `prompt_context()` |
| `backend/core/vibe.py` | `VibeLearner` — background style distillation every `VIBE_EVERY` user messages |
| `backend/core/tools.py` | `TOOLS`: OpenAI function-calling schemas for the 24 built-in tools (MCP tools are added at runtime by `agent.tools_for_agent()`); `tools_en.py` holds the English descriptions (`english_tools()`) |
| `backend/core/i18n.py` | `lang()`, `tr(pl, en)`, `prompt(name)` (picks `config/prompts_en.py` when it has the name), `load_env_lang()` for CLIs run outside the server, `set_lang()` / `load_saved()` / `chosen()` for the runtime switch (`language.json`), `CONFIRM_PHRASES` — **stdlib only**, reads `PIPE_LANG` on every call |
| `backend/core/llm.py` | Runtime provider choice on top of `.env` (the *base* provider): `DATA_DIR/llm_keys.json` (0600) with keys added from the web UI, per-provider model, `active`, `chosen`; `resolve(base)` is what the agent uses now, `listing()` never returns keys — **no `settings` import** |
| `backend/config/providers.py` | Provider presets, user providers file, `resolve_llm_config()`, model-list filtering — **stdlib only** |
| `backend/configure.py` | Setup wizard (`--check` / `--models` / `--providers` / `--from-env`) — **stdlib only** |
| `backend/config/prompts.py` | `BASE_SYSTEM_PROMPT`, `TELEGRAM_SYSTEM_PROMPT`, `cwd_block()`, worker/routine/VIBE/scan/status prompts; `prompts_en.py` mirrors it in English — always read prompts through `i18n.prompt("NAME")` |
| `clients/cli/cli.py` | SSH tunnel or `KubePortForward`, `rich` REPL with a `prompt_toolkit` prompt (slash-command suggestions from `COMMANDS` + skills, Tab completion; plain prompt when the package is missing); shows `Progress` live, saves attachments to `~/.pipe/diagrams` |
| `backend/core/graph.py` | Infrastructure as graph DATA for the web UI: `build()` → views `fleet` / `host` / `p:<project>` with nodes, edges and `index` (where a node is visible on each level); `locate(tool, args, cwd)` maps a tool call to node ids (container name, systemd unit, file path, project dir; default `host`), `describe()` gives the timeline label (never file contents) |
| `clients/webui/server.py` | `pipe web`: stdlib HTTP server on 127.0.0.1 bridging the browser to the backend — `POST /api/request` (NDJSON stream), `GET /api/events` (SSE of `subscribe`); one-time key → HttpOnly cookie, Host/Origin checks, strict CSP, backend token never reaches the browser |
| `clients/webui/static/` | No-build frontend: `app.js` (chat, confirmations with diff, activity timeline, slash-command palette + `COMMANDS`/`HANDLERS` mirroring the CLI commands, tabs: changes / skills / alerts, provider setup sheet + switcher pill above the composer), `graph.js` (SVG layout, camera, level transitions, follow mode), `md.js` (Markdown → DOM, **never `innerHTML`**), `i18n.js`, `app.css` |
| `clients/telegram/bot.py` | Streams frames (`_run_and_reply`), photos for attachments, live progress message, `alerts_loop()` subscription with *Zbadaj* button, per-user lock with `concurrent_updates` |
| `clients/telegram/tg_format.py` | Slash-command helpers, HTML formatting (code spans literal), alert/routine/list formatting. No `telegram` import, so it is tested from `backend/tests/` |
| `deploy/` | `kubernetes/` (kustomize base + overlays operator/telegram/host-agent), `systemd/`, `cloud-init/` |

### Tool dispatch

Twenty-four built-in tools: `execute_command`, `read_file`, `write_file`, `change_directory`, `git_command`, `system_stats`, `docker_manage`, `network_info`, `cron_manage`, `diagram`, `server_md`, `directory`, `skill_manage`, `vibe`, `target_manage`, `remote_exec`, `delegate`, `routine_manage`, `reminder`, `server_history`, `journal`, `security_audit`, `mcp_manage`, `pipe_update` — plus `mcp__<server>__<tool>` from connected MCP servers, dispatched to `handlers/mcp.handle_mcp_tool` when no `handle_<name>` exists.

`_handle_tool_call()` dispatches by name reflection: `getattr(handlers_module, f"handle_{tool_name}")` (workers pass their own `dispatch` instead). **Adding a tool means three edits:** a schema in `core/tools.py`, a `handle_<name>` async generator in `core/handlers/`, and its export in `core/handlers/__init__.py` (`test_every_tool_has_handler` checks this).

Every handler has the same signature and contract:
```python
async def handle_x(agent, session, tool_call, args) -> AsyncGenerator[Event, None]
```
- Yielded events stream to the **user**: `str` (protocol messages only), `Attachment` (e.g. a diagram), `Progress` (live status). `Activity` events (tool start / wait-for-confirmation / end, with graph node ids and the journal entry) are emitted centrally by `agent._handle_tool_call()` and `confirm()` — only for sessions whose interface starts with `web` (`Session.shows_activity`), so CLI/Telegram streams are unchanged. The tool result for the **LLM** is appended by the handler itself (`common.reply()`). Every path must append exactly one tool message **or** set `session.pending_confirmation`. `_handle_tool_call()` backstops a handler that forgets or raises, and `run_loop()` answers the remaining tool calls of a turn with `SKIPPED_FOR_CONFIRMATION` once one is pending — the history sent to the provider must always be valid (`assert_history_valid`).
- Never yield raw command output — the model interprets it; yielding it too shows the user the same thing twice.
- For shell-running tools use `common.run_classified(session, tool_call, command, tool_name=..., inner=...)`: `command` is exactly what runs and is shown; `inner` is what gets classified when `command` is a wrapper (ssh/kubectl).
- Wrap any command or path shown in a protocol message with `as_code()` (`backend/core/text.py`), never literal backticks.
- A handler that yields nothing still has to be an async generator — trailing unreachable `yield` after `return`.
- Tool results are post-processed centrally: `memory.redact_secrets()` (unless `REDACT_SECRETS=0`) and `truncate_result()` (24k chars). `write_file` refuses content containing `[ZREDAGOWANO`.

### Confirmation round-trip

`confirm` never re-enters the handler. `agent._execute_tool_confirmed()` (an async generator — it streams the safety fuse's `Progress`) replays from the stored `ConfirmationRequest`: an `action` callback (registry changes such as adding a target or routine — `command` is then only a description), `write_file` (path + content), or else `ConfirmationRequest.command` as a ready-made shell command run with `CONFIRMED_COMMAND_TIMEOUT`. Everything except an action **without** `plan` (e.g. confirmed `read_file` of a secret) runs through `safety.guarded()` with `ConfirmationRequest.plan` — the plan built in `run_classified()` / `handle_write_file()` / registry handlers (`Plan(local_files=...)`) and shown in the `[POTWIERDZ]` message, so the executed plan is the one the user saw. A failed pre-check aborts without executing; a failed post-check of config files restores the backup (`SAFE_AUTO_ROLLBACK`). Remote targets (wrapped `command` != `inner`) get an empty plan (journal entry only). A new user message while a confirmation is pending answers the tool call with `ABANDONED_CONFIRMATION`.

### Runtime and path convention

`core/runtime.py` decides how the managed host is seen (`PIPE_RUNTIME`, auto-detected; `HOST_ROOT`/`HOST_PROC` override):
- **docker** — host root at `/hostfs` (ro, `/root` rw), host `/proc` at `/hostproc`, `pid: host`, `docker.sock`.
- **native** — everything direct (`host_root() == ""`, `/proc`). `settings` rewrites the Ollama preset's `host.docker.internal` to `127.0.0.1`.
- **kubernetes** — pod with a ServiceAccount; `/hostfs` only with the `host-agent` overlay.

`Session.cwd` and everything the model sees are **host** paths; `runtime.to_local()` converts to what the process sees, `runtime.to_host()` normalises model input (accepts host, prefixed and relative paths) so `cwd` never contains the prefix. Handlers never hardcode `/hostfs`/`/hostproc`. `system_stats`, `network_info`, the watcher and `infra` read host state via `hostinfo` (`/proc/1/net/*` for host sockets because the container has its own netns). `Session.system_prompt` order: base → `runtime.describe()` → memory (SERVER.md, DIRECTORY, skills, VIBE) → alerts → cwd block **last** (provider prefix caching).

### Agent memory

`memory.prompt_context(user_key)` appends SERVER.md (≤12k chars), DIRECTORY (≤60 entries), the skill index, and the user's VIBE note. Files live in `DATA_DIR` (container `/app/data`, host `backend/data/`, gitignored) together with `targets.json`, `routines.json`, `reminders.json`, `watch_state.json`, `metrics.jsonl`, `snapshots/`, `journal/`, `usage.json`, `incidents.json`, `tokens.json`, `mcp.json`, `known_hosts`, audit log. Writes are audited by path only and rejected by `find_secret()`; remote URLs are stripped of credentials. Built-in skills (`backend/skills_builtin/`) are seeded into `DATA_DIR/skills` by `memory.seed_builtin_skills()` at server start; `.builtin.json` keeps installed hashes so updates only replace untouched copies and deleted ones stay deleted. Skill save and full SERVER.md overwrite require confirmation (persistence vectors); other writes emit a `[PAMIEC]` user notice. Memory is framed as data, not instructions. Keep `memory.py` free of `settings` imports so it stays testable via `DATA_DIR`. VIBE keys come from `interface` (`cli:kuba` → `cli-kuba`); backend-built messages are marked `pipe_generated` (stripped before the provider call by `call_llm`) and never teach VIBE.

### Language (`PIPE_LANG`)

Runtime switch: the `language` command (`/jezyk` / `/language [pl|en]` in the CLI, `pipe web` — also the PL/EN header button — and Telegram; set is admin-only) calls `i18n.set_lang()`, which sets `PIPE_LANG` in the process and stores `{lang, env}` in `DATA_DIR/language.json`; `i18n.load_saved()` at server start restores it only while `.env` still has the same `PIPE_LANG` (an edited `.env` wins). The server re-seeds built-in skills and publishes a transient `language` event (`Notifier.publish(..., transient=True)` — not in history); `subscribed` carries `lang` + `lang_chosen`. Clients follow: the bot sets its `PIPE_LANG` and refreshes menus, the web bridge updates `WebBridge.lang` and the page reloads keeping its session (`sessionStorage`), the CLI adopts a chosen language on connect unless `--lang` was passed.

Startup configuration — one switch, three places: backend (`backend/.env`), Telegram bot (`clients/telegram/.env`, `tg_format.lang()/tr()`), CLI (`--lang` / `PIPE_LANG`, module-level `LANG` + `tr()` in `cli.py`). The clients cannot import `backend`, so each has its own tiny `tr()`.

- **Every new user- or model-visible string is written as `tr("polski", "english")`** next to its use (f-strings on both sides). Module-level tables that hold text get an `_EN` sibling and are picked at use time (`tr(TABLE, TABLE_EN)`), never translated at import — tests switch language with `monkeypatch.setenv`.
- Prompts and tool schemas are whole-file translations: add the constant to both `prompts.py` and `prompts_en.py`, the description to `tools.py` and `tools_en.py` (`test_every_tool_has_english_description` fails otherwise). MCP server tools: `TOOLS` + `TOOLS_EN` in `core/mcp/server.py`, served through `tools()`.
- **Protocol tags never change with language**: `[POTWIERDZ]`, `[BLAD]`, `[ODMOWA]`, `[OSTRZEZENIE]`, `[PAMIEC]`, `[ZREDAGOWANO: ...]`. `server.event_frame()` detects a confirmation by `[POTWIERDZ]` + any of `i18n.CONFIRM_PHRASES`, so an English confirmation message must contain "requires confirmation". Clients map tags to labels.
- Sentinels compared in code exist in both languages (`executor.NO_OUTPUT`).
- Slash commands: Polish names are canonical; English aliases (`COMMAND_ALIASES` in `cli.py` and `tg_format.py`) work in both languages. Telegram's `/` menu comes from `builtin_commands()`.
- Built-in skills: `backend/skills_builtin/` (pl) and `backend/skills_builtin_en/` (en); `seed_builtin_skills()` picks by language. Skills referenced from code (`swap`, `fail2ban-ssh`) keep the same name in both.
- `backend/tests/test_i18n.py` has `assert_english()` (a Polish-word detector) — use it when adding a report or message family.

### Workers, targets, routines, watch

- `targets.wrap()` builds `ssh -o BatchMode=yes ... -- 'cmd'`, `docker exec c sh -c 'cmd'`, `kubectl --context/--namespace ...` or `kubectl exec`; all fields are regex-validated and the command is one `shlex.quote`d argument. Classification is on the inner command.
- Workers run `agent.run_loop()` with `WORKER_TOOLS` (`run`) and a custom `dispatch`: `safe` executes, `confirm` becomes a proposal (never executed), `forbidden` is refused. History lives in `parent.workers[name]`.
- Routines run as workers from `Watcher._routine_loop()`; reports and alerts go through `Notifier.publish()` to subscribed clients.
- The Telegram bot always subscribes (`alerts_loop`, task kept in `app.bot_data`); `TELEGRAM_ALERTS=0` only filters `WATCH_EVENT_TYPES`, reminders and approvals always pass. `bot._subscription` tracks the channel state and `subscription_note()` surfaces it in `/przypomnienia` and `/alerty`; `handlers/reminders.delivery_note()` tells the model when nobody is subscribed so it cannot claim a message was sent. Containers take the host time zone (`/etc/localtime` mount, `TZ` override) — all schedules are server time.
- Reminders are the only way the agent can speak later: the prompt forbids promising future actions that were not scheduled with `reminder` or `routine_manage`. The `reminder` event carries `to` (the interface that set it); `tg_format.reminder_recipients()` sends it back to that Telegram user, or to admins when it was set from the CLI.
- The Watcher is deterministic (no LLM). Persistent findings (disk, memory, load, restarting/unhealthy containers) fire once and resolve; transient ones (new public port, stopped container) fire once. Alert keys are `<group>:...`; each loop passes its `scope` to `apply()` so it only resolves its own groups (`RESOURCE_SCOPE` every `WATCH_INTERVAL`, `CHECKS_SCOPE` = cert/site/dns/backup every `CHECKS_INTERVAL`). Site failures need two consecutive runs.
- `agent.active_llm()` returns (config, client) for the provider selected at runtime (`core/llm.py`; one `AsyncOpenAI` per base URL + key, an injected test client serves every choice). When it differs from the `.env` provider, `call_llm()` ignores the `model` argument (`WORKER_MODEL` only exists at the base provider), sends no `reasoning_effort`, records usage without prices, and `_for_provider()` strips provider-specific fields from assistant messages tagged with another `pipe_provider`.
- Every `call_llm()` records token usage via `usage.record()` (label = session interface → `who_from_interface`) and first calls `usage.check_budget()`; `BudgetExceeded` surfaces as a `[BLAD]` chunk. Fakes of `agent.complete()` must accept `who=`.

### Wire protocol (JSON lines, both transports)

```
client → server   {"message": "...", "session_id": "<uuid>", "interface": "cli:<user>" | "telegram:<id>", "token": "..."}
client → server   {"confirm": true|false, "session_id": "<uuid>"}
client → server   {"command": "<name>", "session_id": "<uuid>", "name"?, "args"?, "id"?}
server → client   {"response": "...", "status": "ok" | "confirm" | "error", "done": false}   × N
                  + optional "attachment" {name, mime, caption, data(base64), source, text} / "event" {type: progress|alert|routine|subscribed, ...}
server → client   {"response": "", "status": "ok", "done": true}      (+ "data" for data commands)
```
Commands: `update` (stream; `/aktualizuj` — asks the agent to call `pipe_update`, `args` `sprawdz`/`check` for a version check only), `list_skills`, `server_md`, `directory`, `vibe`, `alerts`, `targets`, `routines`, `history`, `changes`, `health`, `usage` (reply with `data`); `scan_server`, `status`, `run_skill`, `investigate` (stream; prompts built server-side from `prompts.py` — clients never compose prompts; `investigate` appends the last 24 h of snapshot changes); `diagram`, `chart`, `digest` (attachment frame + `data`, no LLM); `mcp` (MCP bridge for `cli.py --mcp`: one JSON-RPC message in, `data.rpc` out), `mcp_servers`, `approvals`, `approve` (admin), `reminders` (list / `cancel` / `claim`), `skill` (one skill's content), `graph` (infra as data), `journal_changes` (before → now diff of a journal entry, secrets redacted), `providers` / `provider_models` / `provider_set` / `provider_forget` (LLM provider choice; mutations admin-only, key in `key`, never echoed), `language` (state / switch Pipe's language, set admin-only), `incidents`, `transcribe` (voice → text), `audit`, `welcome`, `journal`, `undo` (preview, then `execute: true` + `id` — client-side TAK, no LLM so it works when the provider is down); `subscribe` (connection stays open, pushes watcher events: `alert` (with `history`), `routine`, `digest`, `welcome`, `investigation`, `approval`, `reminder`, `language`). Frames with attachments are large — both sides use `READ_LIMIT` (server 4 MiB, clients 32 MiB). Full spec: `docs/protocol.md`.

`token` is required once any token is configured (`AGENT_TOKEN` = admin, `AGENT_VIEWER_TOKEN` = viewer, or `tokens.json`), and is checked for **every** request type (including `confirm` and `subscribe`) by `server._authorize()` → (identity, role). Sessions are bound to the identity that created them (`agent.owns()`); fakes of the agent in tests need `owns()`. Viewer: server refuses `confirm: true` and `undo` execute; `agent._handle_tool_call` refuses `VIEWER_WRITE_OPERATIONS` upfront and turns any `pending_confirmation` into a refusal (buffering the handler's events so no `[POTWIERDZ]` leaks). `server.py` derives `status` by string-matching chunk text (`[POTWIERDZ]` + `wymaga potwierdzenia` → confirm, `[BLAD]`/`[ODMOWA]` → error), so those tags in handler output are load-bearing. The prompt tells the model **not** to emit `[POTWIERDZ]` itself.

### Security model

`classify_command()`: forbidden patterns on the whole string; then `split_command()` splits on `;`, `|`, `||`, `&&`, `&`, newlines **outside quotes** and reports write redirects (`>` except `/dev/null`, `2>&1`) and dynamic constructs (`$(`, `${`, `$'`, backticks, `<(`) — either forces `confirm`. Each segment: forbidden → `is_sensitive()` = `SENSITIVE_PATTERNS` on the raw **and** shlex-unquoted text (shadow, SSH keys, `.env` incl. relative, `/proc/*/environ|root` incl. `self`/globs, globs in secret dirs, `kubectl get secret -o`); `read_file` uses the same check and asks via an `action` confirmation → `CONFIRM_PATTERNS` → token-level `SAFE_PREFIXES` match (`ss` ≠ `ssh`) plus argument analysers: `_check_git` (blocks program-executing flags `-c core.pager=`, `--upload-pack`, `--exec`, `-O`), `_check_curl` (write flags, non-GET incl. `--request=`/`-X<METHOD>`, **external host, external proxy (`-x`/`--proxy`/`--socks*`), `--resolve`/`--connect-to` → confirm to stop exfiltration**; `_host_is_local` allows localhost/private/`host.docker.internal`, and decodes single-number IPs), `_kubectl_dumps_secret` (resource lists like `cm,secret`, `--template`), `find -exec/-delete`, `sort --compress-program`, `journalctl --vacuum`. Unrecognised → `confirm` (fail-closed). Forbidden results are never surfaced as retryable errors. `docker restart/start` are `confirm` (workers never run them). Regression cases: `test_security_hardening.py`, `test_security_review_fixes.py`, `test_review_fixes_v091.py`.

`validate_workspace_access()` resolves paths via `resolve_local()` (host symlinks are re-rooted under the host prefix in docker mode, `..` clamped at it; file handlers open the resolved path) and blocks outside the host root and `FORBIDDEN_HOST_PATHS` (+ `/home/*/.ssh`). File **contents** are never written to the audit log; `_format_entry` escapes `\n`/`\r` so a command can't forge log lines.

`as_code()` (`core/text.py`) neutralises control/bidi chars (ANSI, `\r`, U+202E) in anything shown for confirmation — what you see is what runs. `write_file` confirmation shows a unified diff vs current content and warns on startup files (`.bashrc`, `authorized_keys`, cron, systemd). `memory.redact_secrets()` also covers JWTs, URL credentials (`user:pass@`), shadow hashes, and doesn't cross NUL (so it can't swallow `/proc/*/environ`).

Persistence defence: `skill_manage save` and `server_md` full `write` require confirmation (via the `action` callback, with content/diff preview) — `memory.check_server_md()` / `validate_skill_content()` validate without writing so nothing lands before TAK. Every other memory write yields a `[PAMIEC]` notice; `tg_format.collect_response_text` surfaces those to Telegram even when it collapses to one fragment.

`server.py` compares the token with `hmac.compare_digest`. `settings.validate()` requires `AGENT_TOKEN` in kubernetes mode (ClusterIP reachable from any pod); the wizard, `--from-env` and the installer auto-generate one when empty (`ensure_agent_token`), and the installer syncs it into the Telegram `.env`. `deploy/kubernetes/base/networkpolicy.yaml` denies pod ingress (port-forward still works). `diagram.render` has a `RENDER_TIMEOUT`.

`docker.sock` is effectively root on the host; `docker run/exec` always require confirmation, but the real guard is the user's TAK (documented in `docs/security.md`).

## Environment

`backend/.env` is written by `python3 -m backend.configure` (or `--from-env`, or copied from `backend/.env.example`): `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, optional `LLM_BASE_URL` / `LLM_REASONING_EFFORT` / `LLM_TIMEOUT`, then `AGENT_TOKEN`, `AUDIT_LOG_PATH`, `AGENT_SOCKET`, `TCP_HOST`, `TCP_PORT`, `DATA_DIR`, and optional `PIPE_RUNTIME`, `HOST_ROOT`, `HOST_PROC`, `AGENT_MAX_ITERATIONS`, `CONFIRMED_COMMAND_TIMEOUT`, `REDACT_SECRETS`, `WORKER_MODEL`, `WORKER_MAX_ITERATIONS`, `WORKER_TIMEOUT`, `MAX_WORKERS`, `VIBE_EVERY`, `WATCH_ENABLED`, `WATCH_INTERVAL`, `WATCH_DISK_PCT`, `WATCH_MEM_PCT`, `WATCH_LOAD_FACTOR`, `METRICS_KEEP_DAYS`, `SNAPSHOT_INTERVAL`, `SNAPSHOT_KEEP_DAYS`, `CHECKS_INTERVAL`, `WATCH_SITES`, `WATCH_CERT_DAYS`, `WATCH_BACKUP_HOURS`, `WATCH_IGNORE`, `DIGEST_TIME`, `LLM_PRICE_IN/OUT`, `WORKER_PRICE_IN/OUT`, `DAILY_TOKEN_LIMIT`, `DAILY_COST_LIMIT`, `SAFE_AUTO_ROLLBACK`, `WATCH_SSH_FAILURES`, `WATCH_SSH_LOGINS`, `AGENT_VIEWER_TOKEN`, `WEBHOOK_PORT`, `WEBHOOK_HOST`, `WEBHOOK_TOKEN`, `WEBHOOK_INVESTIGATE`, `MCP_PORT`, `MCP_HOST`, `MCP_ALLOWED_ORIGINS` (all in `config/settings.py`); `PIPE_LANG` (read by `core/i18n.py`, written by the wizard and `install-server.sh --lang`); `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL`, `STT_LANGUAGE` are read from the environment by `voice.resolve()`. In Docker the whole `backend/.env` reaches the container via `env_file` in `docker-compose.yml` (the `environment:` list only overrides container-specific values). `settings` never raises at import; `settings.validate()` fails fast on an unknown provider, a missing model, a missing key, or (in kubernetes mode) a missing `AGENT_TOKEN`.

Telegram needs `clients/telegram/.env` with `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` (admins), a matching `AGENT_TOKEN` if set, optionally `TELEGRAM_VIEWER_IDS` + `AGENT_VIEWER_TOKEN` (read-only users) and `TELEGRAM_ALERTS=0`. The CLI takes the token via `--token` or `AGENT_TOKEN`; `--kube NAMESPACE` / `--kube-context` connect through `kubectl port-forward`.

`TCP_HOST` defaults to `0.0.0.0` in `settings.py` because the process runs inside the container; compose publishes `127.0.0.1:7379:7379`, native installs set `TCP_HOST=127.0.0.1`, Kubernetes uses a ClusterIP Service. Never publish that port on a public interface.

`backend/.dockerignore` keeps `.env` and `data/` out of the image — it is committed on purpose.

## Versioning and shipping

`VERSION` in the repo root is the single source of truth (`X.Y.Z`). The same string is duplicated in README, `docs/*.md` (including `features.md`, `deploy.md`), backend docstrings, `config/prompts.py`, the CLI banner and the Telegram bot — never hand-edit those; run `python scripts/bump_version.py patch|minor|major|X.Y.Z`, which rewrites every site from the `PATTERNS` list. A new hardcoded version site must be added to that list.

The `/ship` skill (`.claude/skills/ship/SKILL.md`) is the release workflow: bump version → update `docs/changelog.md` and any docs the change invalidates (keep `README.en.md` in step with `README.md`) → run tests → commit → push. Changelog entries and commit messages are Polish.

## Known limitations

- Sessions (history, pending confirmations, worker histories) are in process memory — a backend restart forgets them; the Kubernetes Deployment therefore runs one replica.
- `cron_manage` can only list host cron in docker/kubernetes mode (host root is read-only); add/remove works in native mode. Agent-run schedules belong in routines.
- `pipe_update apply` works only in docker mode started by compose; native and Kubernetes get manual steps. `docker compose` is deliberately not a CLI plugin in the image (paths under `/hostfs` would be wrong), so the agent cannot run compose on host projects.
- The CLI cannot receive watcher events (its REPL blocks on input); it shows them via `/alerty`. Telegram receives them live.
- `AGENTS.md` is stale.
