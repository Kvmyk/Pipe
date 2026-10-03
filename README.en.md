# Pipe

**v0.22.0** -- An AI agent that looks after your servers instead of just answering questions.

[Polski](./README.md) · **English**

Pipe lives on the server permanently: it knows it (SERVER.md, a map of directories), watches it and speaks up
first when something breaks. You talk to it from a terminal (CLI over an SSH tunnel) or from your phone
(Telegram) -- in English or Polish. Every command goes through a safety classifier that lives in code, not in
the prompt: reads run immediately, changes wait for your YES, destructive operations are rejected.

---

## How it differs from Claude Code, Codex or Hermes Agent

Those tools are general agents for code or "for everything". Pipe is an **operations** agent:

| | Pipe |
|---|---|
| **Writes first** | Every 2 minutes the watcher checks disks, RAM, load, containers in a restart loop and **new public ports**. The alert arrives on Telegram with an *Investigate* button. No LLM, no cost. Every morning -- a **report** with a chart. |
| **Knows what changed** | An hourly host snapshot: packages, container images, ports, cron, accounts, SSH keys, configs. *"What changed since yesterday?"* has an answer with a timestamp -- and when investigating an alert the agent gets it automatically. |
| **Zero-config monitoring** | Pipe finds the domains in nginx/Caddy/Traefik and the backups in its directory map by itself: it watches certificate expiry, site responses, DNS and backup freshness. You define nothing. |
| **Shows what it is doing** | `pipe web`: next to the chat, a map of the server that shows live where the agent works -- servers, inside a server, a project. Every change has a before -> after diff and an undo button. |
| **Sees the architecture** | `/map` draws a diagram of what runs on the server: domains -> reverse proxy -> containers -> databases, compose projects, ports exposed to the world. On Telegram it arrives as an image, in the CLI as a PNG + a terminal preview. |
| **Manages a fleet** | Remote servers (SSH), containers and Kubernetes clusters are *targets*. The agent sends **workers** to them -- sub-agents that examine every target in parallel and report to the agent, not to you. Nothing is installed on the other side. |
| **MCP gateway for other agents** | Claude Code, Cursor or your own agent connect to Pipe over MCP (`pipe --mcp --host root@server`) and work on the server through the classifier: reads immediately, changes only after your approval on Telegram -- with a backup and `/undo`. Pipe itself also uses other MCP servers (GitHub, Grafana...). |
| **Learns from incidents** | A resolved alert stays in memory with the findings and what helped. When the problem returns, the alert comes with *"previously: cause ..., what helped ..."* and the agent starts from the proven fix. Local, no extra cost. |
| **Joins your monitoring** | Webhooks from Alertmanager, Grafana, Uptime Kuma and GitHub -- the alert goes to Telegram and a worker immediately investigates the cause on the server and sends a report. |
| **Watches security** | `/audit` scores the server (0-100): SSH passwords, firewall, databases exposed to the world (also through Docker, which bypasses ufw), containers with `docker.sock`, uid 0 accounts, updates -- every finding with a ready-made fix. Every 2 minutes it looks for new accounts, SSH keys, SUID programs and suspicious logins. |
| **Changes you can undo** | Before every approved change a backup goes to the journal. `nginx -t`, `sshd -t`, `docker compose config` run **before** the reload; after the change comes verification: service active, container healthy, sites still responding. A broken config rolls back by itself, the rest -- `/undo`. |
| **Security in code** | A fail-closed classifier (unknown command = a question), the confirmation shows exactly what will run, workers only read. Secrets from files (`.env`, keys) are **redacted before they reach the LLM provider**. |
| **Remembers the server, not a repo** | `SERVER.md` (facts about the server), `DIRECTORY` (where repositories, apps, configs and backups live), skills (procedures) and **VIBE** -- over time the agent learns how you like to talk. |
| **Runs on a cheap model** | Any OpenAI-compatible endpoint: Gemini (free tier), OpenRouter, Groq, DeepSeek, local Ollama... Workers can use a cheaper model. `/cost` counts tokens and a daily limit guards the budget. |
| **Runs anywhere** | Docker on a VPS, natively with systemd, in Kubernetes (kustomize), cloud-init for any cloud. amd64 and arm64 images. |

---

## Language

Pipe speaks Polish by default; one switch turns everything to English -- the agent's prompts and tool
descriptions, confirmation messages, alerts, the morning report, the security audit, built-in skills,
the setup wizard and both clients.

| Where | How |
|-------|-----|
| Backend | `PIPE_LANG=en` in `backend/.env` (the wizard asks; `scripts/install-server.sh --lang en`) |
| Telegram bot | `PIPE_LANG=en` in `clients/telegram/.env` (the installer copies it from the backend) |
| CLI | `pipe --lang en` or `export PIPE_LANG=en` |

Slash commands have English names (`/report`, `/changes`, `/undo`...); the Polish ones (`/raport`, `/zmiany`,
`/cofnij`...) keep working in both languages. Protocol tags inside tool results (`[POTWIERDZ]`, `[BLAD]`,
`[ZREDAGOWANO: ...]`) stay language-independent -- the clients turn them into English labels.
To switch a running server, type `/language pl` or `/language en` in the CLI, in `pipe web` (or use the PL/EN button
in its header) or on Telegram -- no restart, all channels switch together (administrators only).

---

## Quick start

### 1. Put the backend on the server

```bash
git clone https://github.com/Kvmyk/pipe && cd pipe
sudo bash scripts/install-server.sh --lang en             # Docker; the wizard asks for the provider and key
# or: sudo bash scripts/install-server.sh --lang en --mode native   (no Docker, systemd)
```

The wizard fetches the current model list straight from the provider and checks that the chosen model
supports tool calling. No questions (automation):
`PIPE_LANG=en LLM_PROVIDER=gemini LLM_API_KEY=... bash scripts/install-server.sh -y`.
A new cloud server: [deploy/cloud-init/user-data.yaml](./deploy/cloud-init/user-data.yaml).
Kubernetes: [deploy/kubernetes](./deploy/kubernetes/README.md). All modes: [docs/deploy.md](./docs/deploy.md).

### 2. Connect the CLI from your laptop

```powershell
# Windows
.\install.ps1
```

```bash
# Linux / macOS
bash install.sh
```

From now on you type `pipe --lang en` in any terminal (or put `export PIPE_LANG=en` in your shell profile and
just type `pipe`). The CLI sets up an SSH tunnel and connects to the agent.

Prefer a browser? `pipe web` opens an interface with the chat, a **live map of the server** (you see which
element the agent is working on right now), a view of changes with undo and an **LLM provider switcher** (add API
keys in the browser and jump between models mid-conversation). It runs locally over the same
tunnel -- no port is opened on the server.

### 3. Telegram (optional, recommended -- alerts arrive here)

```bash
cd pipe/clients/telegram
cp .env.example .env    # TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS
sudo bash ../../scripts/install-server.sh   # also starts the bot
```

---

## What you can write

- *"show me the server architecture"* -- a diagram as an image; *"draw how a request reaches the shop"* -- the agent's own diagram
- *"why is the shop slow?"* -- a diagnosis: logs, resources, containers
- *"add the server 10.0.0.5 as web-2 (ssh, root)"*, then *"check disks and updates on all servers"* -- workers in parallel
- *"every day at 7 check the backups and certificate validity, write only when something is wrong"* -- a routine
- *"remind me tomorrow at 9 to renew the domain"*, *"in an hour check whether the backup finished"* -- the reminder arrives by itself
- *"where is the blog repository?"* -- an answer from DIRECTORY
- *"answer shorter and without preambles"* -- it saves that in VIBE
- *"the site went down overnight -- what changed?"* -- the agent starts from the change history: packages, images, ports, configs
- *"has RAM been growing for a week?"* -- a chart from the watcher's history
- *"how secure is this server?"* or `/audit`, then *"fix 1"* -- a fix with a backup and verification
- *"set up shop.example.com on port 3000 with a certificate"* -- the built-in `nginx-vhost` skill
- a voice message on Telegram: *"check why the shop is down"* -- transcription and diagnosis
- *"undo the last change"* or `/undo` -- restores files and reverses the operations from the journal

Commands in both clients: `/status` `/update` `/report` `/changes` `/chart` `/health` `/map` `/server` `/directory` `/skills`
`/audit` `/incidents` `/approvals` `/mcp` `/alerts` `/routines` `/reminders` `/targets` `/vibe` `/journal` `/undo` `/cost` `/history` `/providers` `/yolo` `/language` `/help`.

---

## Architecture

| Component | Where it runs | Connection to the backend |
|-----------|---------------|---------------------------|
| `backend/` | Server (Docker / systemd / Kubernetes) | -- this is the backend |
| `clients/cli/` | Your laptop | SSH tunnel -> TCP `127.0.0.1:7379` (or `kubectl port-forward`) |
| `clients/telegram/` | Server | Unix socket |
| `clients/webui/` | Your laptop (`pipe web`) | page on `127.0.0.1:7400`, same SSH tunnel as the CLI |
| `clients/discord/` | -- | Placeholder -- PRs welcome |

```
CLI / Telegram ──JSON lines──> server.py ──> agent (LLM loop + tools)
                                  │              ├─ handlers ─> security (safe/confirm/forbidden) ─> executor
                                  │              ├─ workers ─> targets: ssh / docker exec / kubectl
                                  │              └─ diagram ─> infra (discovery) ─> Mermaid ─> PNG
                                  └── watcher + routines ──(subscribe)──> alerts on Telegram
```

Protocol: JSON lines, frames with text, attachments (PNG diagrams), worker progress and watcher
events -- [docs/protocol.md](./docs/protocol.md).

---

## Agent tools

| Tool | Description |
|------|-------------|
| `execute_command` | A shell command on the server (through the classifier) |
| `read_file` / `write_file` | Reading (secrets redacted) and writing files (always with confirmation) |
| `change_directory` | Working directory |
| `git_command` | Git: status, log, diff (immediately); pull, commit, push (with confirmation) |
| `system_stats` | CPU, RAM, disks, processes -- from the host's `/proc` |
| `docker_manage` | Containers, images, compose projects |
| `network_info` | Host ports (public ones marked), connections, ping, curl, DNS |
| `cron_manage` | Host cron |
| `diagram` | Infrastructure map or a custom Mermaid diagram -> an image for the user |
| `target_manage` / `remote_exec` | Remote targets (SSH, containers, Kubernetes) and commands on them |
| `delegate` | Workers: parallel sub-agents, read-only, report to the agent |
| `routine_manage` | Scheduled tasks with a report on Telegram |
| `reminder` | A one-off reminder or task at a given time ("write in 10 minutes") |
| `pipe_update` | Update of Pipe itself: version check and rebuild in a separate container ("update yourself") |
| `mcp_manage` | External MCP servers and their tools (`mcp__<server>__<tool>`, confirmation by default) |
| `security_audit` | Security audit with a score and ready-made fixes |
| `journal` | Journal of approved changes with backups, and undo (`/undo`) |
| `server_history` | What changed on the server (and when), load/RAM/disk charts, certificates/sites/DNS/backups |
| `server_md` / `directory` / `skill_manage` / `vibe` | The agent's memory |

---

## Agent memory

Everything lives in `backend/data/` on the server (outside git, editable by hand):

- **SERVER.md** -- facts about the server: services, domains, decisions. In the prompt of every conversation.
- **DIRECTORY** (`directory.json`) -- a map of places: repositories (with remote and branch), app directories, compose projects, configs, data, logs, backups. A scan finds repositories and compose projects by itself.
- **Skills** (`skills/<name>/SKILL.md`) -- procedures; every skill has its own `/name` command. In English mode
  Pipe starts with: `nginx-vhost`, `swap`, `fail2ban-ssh`, `backup-postgres`, `update-container`, `harden-ssh`,
  `free-disk-space` -- you can change and delete them.
- **VIBE** (`vibe/<user>.md`) -- how to talk to you. Updated in the background every few messages; `/vibe` shows it, `/vibe reset` clears it.
- **Targets, routines, reminders** (`targets.json`, `routines.json`, `reminders.json`) and the audit log.

---

## Security

- **Fail-closed classifier** -- `safe` only for recognised reads (token-level: `ss` is not `ssh`); redirects, `$(...)`, flags like `find -delete` or `curl -o` -> a question.
- **The confirmation shows exactly what will run** -- also for commands on remote targets.
- **Secret redaction** -- API keys, tokens, passwords and private keys in tool results are replaced with `[ZREDAGOWANO: ...]` before being sent to the LLM; the agent cannot overwrite a file with redacted content.
- **Workers and routines only read** -- changes come back as proposals to approve.
- **Audit log** of every operation (without file contents). `AGENT_TOKEN` protects the socket and the port.

Details and limitations (e.g. `docker.sock` = root privileges): [docs/security.md](./docs/security.md).

---

## Documentation

The detailed documentation in `docs/` is written in Polish; this README and the product itself
(`PIPE_LANG=en`) are fully English.

- [Quick start](./docs/quickstart.md)
- [Deployment: Docker, native, Kubernetes, clouds](./docs/deploy.md)
- [Backend](./docs/backend.md)
- [Workers, targets, routines, watcher, diagrams](./docs/features.md)
- [CLI](./docs/cli.md)
- [Telegram](./docs/telegram.md)
- [Security](./docs/security.md)
- [Wire protocol](./docs/protocol.md)
- [Changelog](./docs/changelog.md)

---

## License

MIT -- see [LICENSE](./LICENSE). Pipe is and will stay a free, open-source project;
bug reports and pull requests are welcome.

---

The LLM provider logos in `pipe web` are trademarks of their respective owners and are used only to indicate whose
models the agent connects to. Pipe is not affiliated with or endorsed by these companies
(`clients/webui/static/providers/NOTICE.md`).
