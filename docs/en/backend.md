# Backend -- Pipe

Pipe v0.30.1

The agent backend. It runs on the server (Docker, natively with systemd or in Kubernetes -- [deploy.md](./deploy.md))
and exposes a local Unix socket and TCP port 127.0.0.1:7379 for clients.

## Requirements

- Linux, Python 3.11+
- docker mode: Docker 24+ with Compose v2 (the installer adds them from the distribution's repository)
- native mode: systemd; kubernetes mode: a cluster with PVC

## Modules

| Module | Role |
|--------|------|
| `server.py` | Unix socket + TCP, JSON lines frames, client commands, event subscription, starting the watcher |
| `core/agent.py` | LLM loop with tool calling (`AGENT_MAX_ITERATIONS`), confirmations, redacting and truncating results, history |
| `core/handlers/` | One module per tool area; `handle_<name>` = the `<name>` tool |
| `core/security.py` | Command classifier (fail-closed, token-based, quote-aware), workspace |
| `core/runtime.py` | Run mode (docker / native / kubernetes) and host path mapping |
| `core/hostinfo.py` | Host state from `/proc`: RAM, CPU, disks, sockets, without running commands |
| `core/infra.py` | Infrastructure discovery (Docker, nginx, Caddy, Traefik, systemd, git, Kubernetes) and the Mermaid map |
| `core/diagram.py` | Mermaid -> PNG + ASCII (mermaidx, offline) |
| `core/targets.py` | Remote targets: registry, validation, building ssh / docker exec / kubectl commands |
| `core/workers.py` | Workers: sub-agents on the agent loop, reads only, in parallel |
| `core/routines.py` | Routines: cron schedule, registry |
| `core/watch.py` | Watcher: checks, alerts, notifications (pub/sub), running routines and the loops below |
| `core/metrics.py` | Measurement history (load, RAM, disks) and Mermaid `xychart-beta` charts |
| `core/snapshots.py` | Host state snapshots and diffs, "what changed" |
| `core/checks.py` | Checks without configuration: certificates, sites, DNS, backup freshness |
| `core/digest.py` | Morning report (no LLM) |
| `core/usage.py` | LLM token and cost counter, daily limits |
| `core/posture.py` | Host security audit (score, fixes), security change sentinel, SSH log |
| `core/welcome.py` | Welcome after installation: map, security score, what Pipe watches |
| `skills_builtin/`, `skills_builtin_en/` | Built-in skills (Polish and English), installed into `data/skills` at startup according to `PIPE_LANG` |
| `core/i18n.py` | Pipe's language (`PIPE_LANG=pl\|en`): `tr("polski", "english")` next to the source text, `prompt()` picks `prompts_en.py` / `tools_en.py` |
| `core/selfupdate.py` | Updating Pipe itself: a helper container (`git pull`, the release image from GHCR or `docker-compose up -d --build`), report after the restart |
| `core/reminders.py` | Reminders: one-off messages and timed tasks (`reminders.json`), fired by the watcher |
| `core/incidents.py` | Incident memory: alert -> findings from "Investigate" -> what helped (journal) |
| `core/webhooks.py` | HTTP server for alerts from outside (Alertmanager, Grafana, Uptime Kuma, GitHub) |
| `core/voice.py` | Voice message transcription (an OpenAI-compatible Whisper endpoint or a Gemini model with audio input) |
| `core/attachments.py` | Message attachments: limits, image / text / binary file by content, `DATA_DIR/uploads/` |
| `tokens.py` | Client tokens with admin/viewer roles (`python3 -m backend.tokens`) |
| `core/mcp/` | MCP: `server.py` (Pipe as a server, both protocol eras, Streamable HTTP), `client.py` (stdio/HTTP), `registry.py` (`mcp.json`, policy) |
| `core/approvals.py` | Admin approvals for operations of external agents |
| `core/auth.py` | Token authentication (JSON lines and MCP HTTP) |
| `core/safety.py` | Safety fuse: the change plan (backups, check before, verification after) and guarded execution |
| `core/journal.py` | Change journal: file backups, git and crontab state, inverse commands, `/undo` |
| `core/memory.py` | SERVER.md, DIRECTORY, skills, VIBE, secret detection and redaction |
| `core/vibe.py` | Learning the conversation style in the background |
| `core/events.py` | Stream events: text, `Attachment`, `Progress` |
| `core/executor.py` | `execute` (timeout, killing the process group), `read_file`, `write_file` |
| `core/audit.py` | Append-only audit log |
| `core/tools.py` | Tool schemas (OpenAI function calling) |
| `config/` | `settings.py` (`.env` variables), `providers.py` (LLM presets), `prompts.py` |
| `configure.py` | Provider wizard (`--check`, `--models`, `--providers`, `--from-env`) |

## Configuration

### Set up the LLM provider

From the repository root run the wizard (it creates `backend/.env` from `.env.example` by itself):

```bash
python3 -m backend.configure
```

The wizard:
1. shows the list of providers and asks for the API key (with a link to where to get it),
2. fetches the **current** list of models straight from the provider's API, so new models show up right away without
   updating Pipe,
3. checks live whether the chosen model supports tool calling (without it the agent can't run any command),
4. writes `backend/.env` (creating it from `.env.example` if it doesn't exist; permissions `600`).

The wizard uses only Python's standard library, so there is nothing to install.

Useful modes:

```bash
python3 -m backend.configure --check      # test the current configuration (model list + tool calling)
python3 -m backend.configure --models     # current models of the current provider
python3 -m backend.configure --providers  # all available providers
```

### Built-in providers

All through an API compatible with OpenAI Chat Completions. Addresses verified in September 2026.

| `LLM_PROVIDER` | Provider | Default model | Notes |
|---|---|---|---|
| `gemini` | Google Gemini | `gemini-3.8-flash` | Default. Free tier for Flash models, but see below (privacy) |
| `openai` | OpenAI | `gpt-5.6-terra` | GPT-6 needs the Responses API for tool calling, use GPT-5.6 |
| `anthropic` | Anthropic Claude | `claude-sonnet-5` | Through the OpenAI SDK compatibility layer |
| `openrouter` | OpenRouter | `google/gemini-3.8-flash` | One key, hundreds of models (DeepSeek, Qwen, GLM, Kimi...) |
| `groq` | Groq | `openai/gpt-oss-120b` | Very fast inference |
| `deepseek` | DeepSeek | `deepseek-flash` | |
| `mistral` | Mistral AI | `mistral-large-latest` | EU provider |
| `xai` | xAI Grok | `grok-4.6` | |
| `zai` | Z.ai (GLM) | `glm-5.3` | |
| `kimi` | Moonshot Kimi | `kimi-k3` | |
| `together` | Together AI | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | |
| `cerebras` | Cerebras | `gpt-oss-120b` | |
| `fireworks` | Fireworks AI | -- (pick from the list) | |
| `ollama` | Ollama (local) | -- (pick from the list) | No key, see below |

The default model is only a starting point. At startup the backend checks whether the configured model is still on
the provider's list and prints a warning with suggestions if it was retired.

Privacy: Pipe sends the provider logs, configs and command output from the server (secrets are redacted). On the
free tier of the Gemini API Google may use prompt content to improve its services and humans may read it. The wizard,
`pipe web` and the CLI warn about this when you pick Gemini. On a production server enable billing in Google AI
Studio or choose another provider, for example a local Ollama.

### No model (`LLM_PROVIDER=none`)

Pipe can run without any model. Nothing leaves the server and nothing costs money, and these keep working:
watching and alerts, snapshots and "what changed", the morning digest, certificate and site checks, the security
audit, the journal and `/undo`, diagrams, webhooks (the alert arrives, nobody investigates it) and the MCP gateway
for other agents (without `ask_pipe`). Chat, routines and alert investigations answer that there is no model.
When you add a provider later in `pipe web` or `/providers`, chat turns on without a restart.

In the wizard it is the last item on the list; without questions: `LLM_PROVIDER=none bash scripts/install-server.sh -y`.

### Configuring `.env` by hand

Instead of the wizard you can edit `backend/.env` by hand:

```env
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_MODEL=            # empty = the provider's default model
```

Instead of `LLM_API_KEY` you can set a provider-specific variable, for example `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`ANTHROPIC_API_KEY`; `LLM_API_KEY` takes precedence.

The provider from `.env` is the **base** provider, the one the backend starts with. More can be added without a
restart in `pipe web` (the provider screen, `/provider`): the key goes to `DATA_DIR/llm_keys.json` (0600), and the
switcher in the conversation changes the provider of the whole agent. Preset variables set next to `LLM_API_KEY` (for
example `OPENAI_API_KEY`, `GROQ_API_KEY`) also count as ready providers in the switcher. `WORKER_MODEL`,
`LLM_REASONING_EFFORT` and the `LLM_PRICE_*` prices apply only to the base provider.

Optionally:

```env
LLM_BASE_URL=https://proxy.example.com/v1   # overrides the preset's address (e.g. a company proxy)
LLM_REASONING_EFFORT=low                     # for models that support this parameter
LLM_TIMEOUT=120                              # LLM response timeout in seconds
```

Old `.env` files (only `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`, without `LLM_PROVIDER`) keep working; the
provider is recognised by its address.

### Your own provider

You add any OpenAI-compatible endpoint (vLLM, LM Studio, LiteLLM, a company proxy...) with the wizard: pick
**Other** and give the address. The provider is saved in `backend/data/providers.json` and from then on it is
available like a built-in one (`LLM_PROVIDER=<id>`).

You can also edit this file by hand:

```json
{
  "providers": [
    {
      "id": "my-vllm",
      "name": "My vLLM",
      "base_url": "http://192.168.1.10:8000/v1",
      "default_model": "qwen3",
      "requires_key": false
    }
  ]
}
```

An entry with the `id` of a built-in provider overrides it, which is how you fix an outdated address without waiting
for a new Pipe version. The `backend/data/` directory is mounted in the container, so changes don't need an image
rebuild; `docker-compose restart vps-agent` is enough.

### Ollama (local models)

The container sees the host at `host.docker.internal` (set in `docker-compose.yml`). Ollama listens only on
`127.0.0.1` by default, so you have to start it with `OLLAMA_HOST=0.0.0.0` for the container to see it. The model
must support tool calling; the wizard checks that.

### Protecting with a token

```env
# Protection (optional, but strongly recommended)
# When set, every request, including confirming an operation, must contain
# this token. It protects the socket and the TCP interface from other processes on the server.
# Give the same token to the clients: to the CLI with --token (or the AGENT_TOKEN variable),
# to the Telegram bot with AGENT_TOKEN in clients/telegram/.env.
# Empty = no token required.
AGENT_TOKEN=your-secret-token
```

## Agent memory: SERVER.md and skills

The agent has several kinds of persistent memory: SERVER.md, skills (below), and also DIRECTORY (a map of
repositories and directories) and VIBE (conversation style), described in [features.md](./features.md). Everything
lives on the host in `backend/data/` (in the container `/app/data`, the `DATA_DIR` variable), survives a restart and
an image rebuild, and doesn't go into git. `targets.json`, `routines.json`, `reminders.json`, `watch_state.json` and
the audit log are there too.

### SERVER.md

The agent's notes about the server, like `AGENTS.md` but for a server. The agent updates them by itself (the
`server_md` tool) when it learns a lasting fact: services, containers, domains, ports, important paths, your
decisions. The whole file is added to the system prompt of every conversation (up to 12,000 characters; the file
itself can have up to 20,000).

To start you can ask: *"explore the server and create SERVER.md"*. You can also edit the file by hand:
`backend/data/SERVER.md`.

### Skills

Saved reusable procedures, in the Agent Skills format:

```
backend/data/skills/<name>/SKILL.md
```

```markdown
---
name: renew-certificate
description: Renewing the TLS certificate for nginx
---

1. `certbot renew`
2. `nginx -t && systemctl reload nginx`
3. Check the certificate's expiry date.
```

The system prompt has only the list of skills (name and description). The agent loads the full content with the
`skill_manage` tool when a task matches the description, so many skills don't clog the context. The agent creates a
skill after carrying out a multi-step procedure that will come up again, or when you ask.

Every skill also gets its own command on Telegram and in the CLI: dashes turn into `_`, and the name is shortened to
32 characters (Telegram's limit), for example `renew-certificate` -> `/renew_certificate`. A skill whose name is taken
by a built-in command (`status`, `server`, `skills`, ...) gets no command; run it through `/skills` or with plain
text.

### Memory security

- Writing to memory doesn't require confirmation, because it doesn't change the server, only the agent's notes. Every
  write goes into the audit log (only the path, without content). The exceptions (saving a skill, overwriting the
  whole SERVER.md) are described in [Security](security.md#agent-memory).
- Skills and SERVER.md don't bypass the security rules: every command from them still goes through classification
  and the confirmation requirement.
- SERVER.md is sent to the LLM provider with every request, so writing obvious secrets (private keys, API keys,
  tokens, `password=...`) is refused. The agent writes down *where* a secret is stored, not the secret itself.

## Running

```bash
docker-compose up -d
```

## Checking

```bash
docker logs vps-agent --tail 20
```

You should see:
```
[VPS Agent] Unix socket : /tmp/vps-agent.sock
[VPS Agent] TCP         : 0.0.0.0:7379 (localhost only — use an SSH tunnel)
[VPS Agent] Provider    : Google Gemini (https://generativelanguage.googleapis.com/v1beta/openai/)
[VPS Agent] Model       : gemini-3.8-flash
[VPS Agent] Runtime     : docker (host: /hostfs, proc: /hostproc)
[VPS Agent] Diagrams    : mermaidx
[VPS Agent] Watcher     : every 120 s
[VPS Agent] Server ready. Ctrl+C to stop.
```

## Agent tools

| Tool | Description | Requires confirmation |
|------|-------------|-----------------------|
| `execute_command` | Shell commands | Depends on classification |
| `read_file` | Reading files (secrets redacted) | No |
| `write_file` | Writing files | Always |
| `change_directory` | Working directory | No |
| `git_command` | Git operations | Modifying ones: yes |
| `system_stats` | CPU/RAM/disks/processes from the host's `/proc` | No |
| `docker_manage` | Containers, images, compose | Modifying ones: yes |
| `network_info` | Host ports, connections, ping, curl, DNS | No |
| `cron_manage` | Host cron (editing only in native mode) | Modifying ones: yes |
| `diagram` | Infrastructure map / your own Mermaid diagram as an image | No |
| `target_manage` | Remote target registry | Adding: yes |
| `remote_exec` | A command on a remote target | Depends on classification |
| `delegate` | Workers (reads only) | No, changes come back as proposals |
| `routine_manage` | Scheduled routines | Adding: yes |
| `reminder` | A one-off reminder or a task at a given time | Task: yes; message: no |
| `pipe_update` | Updating Pipe itself (check / apply) | apply: yes |
| `security_audit` | Security audit with a score and fix commands | No (fixes: yes) |
| `mcp_manage` | MCP servers Pipe uses; their tools `mcp__<server>__<tool>` | Adding: yes; tools by policy |
| `web_search` | Web search without a key (DuckDuckGo → Stack Exchange → Wikipedia) | No |
| `web_fetch` | Reading a page; with `question` a separate model without tools reads it | No for addresses from results and from the user, others: yes |
| `software_info` | `eol` (endoflife.date), `vulns` (OSV.dev) | No |
| `journal` | Journal of approved changes and undoing them | Undo: yes |
| `server_history` | What changed, load/RAM/disk charts, certificates/sites/DNS/backups | No |
| `server_md`, `directory`, `skill_manage`, `vibe` | Agent memory | No |

## More `.env` settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPE_RUNTIME` | `auto` | `docker` / `native` / `kubernetes` |
| `PIPE_LANG` | `pl` | Agent language: `pl` or `en` (prompts, tool descriptions, messages, alerts, reports, built-in skills, the wizard). The Telegram bot reads the same variable from its own `.env`, the CLI `--lang` / `PIPE_LANG` |
| `HOST_ROOT`, `HOST_PROC` | by mode | Overriding host paths |
| `AGENT_MAX_ITERATIONS` | 15 | Loop step limit per message |
| `SESSION_KEEP_DAYS` | 7 | How many days conversations stay in `DATA_DIR/sessions` (0600) and come back after a backend restart; 0 = not saved |
| `CONFIRMED_COMMAND_TIMEOUT` | 900 | Timeout (s) of commands approved by the user; reads have 30 s |
| `REDACT_SECRETS` | 1 | Secret redaction in tool results |
| `WEB_SEARCH` | on | Internet for the agent (`web_search`, `web_fetch`, `software_info`); `off` turns it off |
| `WORKER_MODEL` | the agent's model | Model for workers and VIBE learning |
| `WORKER_MAX_ITERATIONS`, `WORKER_TIMEOUT`, `MAX_WORKERS` | 8, 240, 6 | Worker limits |
| `VIBE_EVERY` | 6 | How many messages between VIBE refreshes (0 = off) |
| `WATCH_ENABLED`, `WATCH_INTERVAL` | 1, 120 | Watcher |
| `WATCH_DISK_PCT`, `WATCH_MEM_PCT`, `WATCH_LOAD_FACTOR` | 90, 92, 2 | Alert thresholds |
| `METRICS_KEEP_DAYS` | 8 | How long to keep measurement history (charts) |
| `SNAPSHOT_INTERVAL`, `SNAPSHOT_KEEP_DAYS` | 3600, 30 | Host state snapshots ("what changed") |
| `CHECKS_INTERVAL` | 3600 | How often (s) to check certificates, sites, DNS and backups |
| `WATCH_SITES` | 1 | 0 turns off network checks (certificates, sites, DNS) |
| `WATCH_CERT_DAYS`, `WATCH_BACKUP_HOURS` | 14, 26 | Thresholds: days until a certificate expires, age of the newest backup |
| `WATCH_SSH_FAILURES` | 60 | Alert at this many failed SSH logins in 10 min (0 = off) |
| `WATCH_SSH_LOGINS` | 1 | Alert on an SSH login from a new address |
| `WATCH_IGNORE` | -- | Domains and paths skipped by the checks (comma-separated) |
| `DIGEST_TIME` | `07:00` | Time of the morning report (server time); `off` turns it off |
| `LLM_PRICE_IN`, `LLM_PRICE_OUT` | -- | Model prices in USD per million tokens, so `/cost` shows the cost |
| `WORKER_PRICE_IN`, `WORKER_PRICE_OUT` | as LLM | Prices of `WORKER_MODEL` |
| `AGENT_VIEWER_TOKEN` | -- | Token of the viewer role (read only); needs `AGENT_TOKEN` |
| `WEBHOOK_PORT`, `WEBHOOK_HOST`, `WEBHOOK_TOKEN` | 0, 127.0.0.1, -- | Server for alerts from outside (0 = off; won't start without a token) |
| `WEBHOOK_INVESTIGATE` | 1 | A worker investigates a new webhook alert (reads only), report to Telegram; limit 1/h per alert, 10/day |
| `MCP_PORT`, `MCP_HOST`, `MCP_ALLOWED_ORIGINS` | 0, 127.0.0.1, -- | Pipe as an MCP server over HTTP (`/mcp`, Pipe token as Bearer) |
| `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL`, `STT_LANGUAGE` | by provider, pl | Voice transcription (gemini/openai/groq automatically; `STT_*` takes precedence) |
| `SAFE_AUTO_ROLLBACK` | 1 | Failed verification of a config file change -> the backup is restored automatically |
| `DAILY_TOKEN_LIMIT`, `DAILY_COST_LIMIT` | 0 | Daily token / USD cost limit (0 = no limit) |

## Stopping

```bash
docker-compose down          # docker mode
systemctl stop pipe          # native mode
```
