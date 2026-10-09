# Telegram Bot -- Pipe

Pipe v0.29.0

A Telegram bot for managing a VPS through an AI agent.

## Overview

The Telegram bot is a client of the agent. It connects to the backend over a Unix socket and passes messages from
Telegram to the agent. Answers are formatted as HTML and sent back to the user.

It handles confirmations with inline buttons (YES / NO), sends diagrams as photos, shows worker progress in a single
message that keeps updating and, most importantly, **writes by itself when something happens**: watcher alerts and
routine reports arrive without asking, with an *Investigate* button.

## Alerts

The bot keeps a permanent connection to the backend (`{"command": "subscribe"}`, renewed after a restart) and forwards
events to every user in `TELEGRAM_ALLOWED_USER_IDS`:

- **WARNING / CRITICAL** -- disk, RAM, load, a container in a restart loop or unhealthy, a stopped container, a new
  public port. The **Investigate** button asks the agent for a diagnosis (reads only) and a proposed fix.
- **RESOLVED** -- the problem is gone.
- **Routine X -- OK/PROBLEM** -- a report from a scheduled task.
- **Report** -- every day at `DIGEST_TIME` (7:00 by default): health, changes since yesterday, certificates, backups,
  updates and a load chart. Built without the LLM.
- **Security change** -- a new account (uid 0 = critical), a new key in `authorized_keys`, a new SUID program, a change
  to sudoers/sshd/PAM/`ld.so.preload` that Pipe did not make. Checked every 2 minutes.
- **SSH logins** -- a series of failed logins (`WATCH_SSH_FAILURES` in 10 min), a successful password login from an
  address that was guessing passwords (critical), a login from a new address.
- **I investigated the alert** -- a worker's report for an alert from Alertmanager, Grafana, Uptime Kuma or GitHub
  (webhooks).
- An alert that comes back has a **Previously:** line: what was found and what helped last time.
- **APPROVAL** -- an external agent (for example Claude Code over MCP) wants to make a change: the command verbatim,
  the safety-fuse plan and *Approve* / *Reject* buttons (admins only). It expires after 30 minutes.
- **Welcome** -- once, after installation: a map of the server, a security score and what Pipe watches.
- Alerts for certificates (expiring in 14 days / invalid), sites (not answering twice in a row), DNS and backups (the
  newest file older than 26 h), for the domains and directories Pipe found by itself.

`TELEGRAM_ALERTS=0` in `clients/telegram/.env` turns forwarding off. Thresholds: [Watching](features.md#watching).

## When notifications don't arrive

The bot gets alerts, reports and reminders through a permanent connection to the backend (an event subscription).
When it is missing, `/reminders` and `/alerts` show a warning with the reason, and a reminder is marked *waiting for
pickup* and arrives once the bot connects. The most common causes:

- `AGENT_TOKEN` in `clients/telegram/.env` differs from the one in `backend/.env`; the bot log says:
  `Kanal zdarzen nieaktywny: the backend rejected the subscription`
- the backend isn't running, or the socket (`AGENT_SOCKET`) isn't shared by both processes
- an old bot process is running (for example a second instance outside Docker)

Logs: `docker compose logs telegram | grep -i "kanal\|subskrypcja"`; when it works: `Subskrypcja alertow aktywna.`
`TELEGRAM_ALERTS=0` only silences watcher alerts and reports; reminders and approval requests always arrive.

## Voice messages

Record a voice message and the bot replies *I heard: ...* and passes the text to the agent. With the `gemini`
provider the model itself transcribes the recording, with `openai` or `groq` their Whisper does; it works right away.
For other providers set `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` in `backend/.env` (for example a free Groq key or a
local faster-whisper).

## Photos and files

Send a screenshot, a log or a config file; the caption is the question (*"why won't nginx accept this?"*). Several
photos sent at once (an album) reach the agent as one message. The limit is 10 MB per file. The model looks at images
and reads text (after secrets are hidden); other files are saved on the server and the agent gets the path. A viewer
can send images and text but not files to be saved. Details: [Attachments](features.md#attachments-files-screenshots-logs).

## Roles

- `TELEGRAM_ALLOWED_USER_IDS` -- admins: everything, including YES/NO and `/undo`.
- `TELEGRAM_VIEWER_IDS` -- read only: conversation, diagnosis, reports, charts and alerts; no YES/NO buttons and no
  `/undo`. Needs `AGENT_VIEWER_TOKEN` in `clients/telegram/.env` and the same one in `backend/.env`. The backend
  enforces roles itself: even if the bot made a mistake, a change with a viewer token would not run.

## Diagrams

`/map` or *"show the architecture"*: the diagram arrives as a photo. A diagram larger than Telegram's photo limit
(10 MB, sides adding up to 10000 px) arrives as a file in full resolution.

## Formatting answers

The bot uses Telegram's HTML mode (not MarkdownV2). The reason: MarkdownV2 needs 18 special characters escaped, which
breaks easily with dynamic content (for example container names with underscores). HTML needs only 3 characters
escaped (<, >, &), which is much simpler.

Supported HTML tags:
- `<b>bold</b>` -- for key data
- `<i>italic</i>` -- for file names
- `<code>inline code</code>` -- for values and commands
- `<pre>code block</pre>` -- for command output
- `<u>underline</u>` -- when needed

The bot converts leftover Markdown (if the LLM produces it) into the matching HTML tags.

Text in backticks, including commands in confirmations, is shown verbatim, without Markdown conversion. So before
tapping YES you see exactly the command that will run (including `*`, `_`, `\\`, `<`, `&` and backticks). The
characters `<`, `>` and `&` outside tags are escaped automatically, so Telegram doesn't reject the message.
Formatting code: `clients/telegram/tg_format.py`.

### Reports and summaries

- Reports written by workers (routines, alert investigation, one-off tasks) arrive as a normal message: the status in
  the header, the `FINDINGS:` / `PROPOSALS:` sections in bold, bulleted lists, commands in backticks verbatim. HTML
  tags in a report are shown as text: the report is written unattended, so it cannot insert a clickable link.
- Summaries from the backend (`/changes`, `/health`, `/cost`, `/journal`, `/reminders`, `/incidents`, the result of
  `/undo`) are headings and lists; identifiers (`#ab12cd`) can be copied with a tap.
- A code block stays where the content is code or a log: `/history`, file diffs, command output after approval.

## Requirements

- Python 3.11+
- A running backend (`docker-compose up -d` in `backend/`)
- Access to the Unix socket `/tmp/vps-agent.sock`
- A Telegram bot token (from @BotFather: https://t.me/botfather)

## Configuration

### 1. Create a bot with @BotFather

```
/newbot
```

Copy the bot token.

### 2. Find your user ID

Send `/start` to @userinfobot (https://t.me/userinfobot); it returns your numeric ID.

### 3. Fill in .env

```bash
cp .env.example .env
```

Edit `.env`:
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCxyz...
TELEGRAM_ALLOWED_USER_IDS=123456789
AGENT_SOCKET=/tmp/vps-agent.sock

# Must be identical to AGENT_TOKEN in backend/.env.
# Leave empty if the backend doesn't require a token.
AGENT_TOKEN=

# Bot language: pl (default) or en, the same as PIPE_LANG in backend/.env.
PIPE_LANG=en
```

You can add several IDs separated by commas: `123456789,987654321`

## Running

The bot starts automatically together with the backend with `docker-compose up -d`.

Starting it by hand:
```bash
pip install -r requirements.txt
python bot.py
```

## What you can ask

The full list is below (the *Commands* section). You can also just write, for example:
- "how much free disk space do I have?"
- "show the server architecture" / "draw how the deploy works"
- "why is the shop slow?"
- "check the disks on all servers" (workers)
- "every day at 7 check the backups" (a routine)
- "keep your answers shorter" (VIBE)

## Security

The bot stays silent for users outside the whitelist: it doesn't answer any message. That way it doesn't reveal its
existence to unauthorized people.

## Commands

With `PIPE_LANG=en` the `/` menu shows English names (`/report`, `/changes`, `/undo`...). Polish and English names
work in both languages.

| Command | What it does |
|---|---|
| `/status` | Quick overview of the server's load |
| `/update [check]` | Update Pipe itself (confirmed with buttons); `check` only shows the versions |
| `/report` | The morning report on demand: health, alerts, changes since yesterday, certificates, backups, updates, LLM cost + chart |
| `/changes [24h\|3d]` | What changed on the server: packages, container images, ports, cron, accounts, SSH keys, configs |
| `/chart [load\|ram\|disk] [24h\|7d]` | Chart from the watcher's history as a photo |
| `/health` | TLS certificates, site responses, DNS and backup freshness |
| `/audit` | Host security score 0-100 with ready fixes (write *"fix 1"*) |
| `/incidents` | Incident memory: what happened, what was found, what helped |
| `/approvals` | Operations of external agents (MCP) waiting for approval; *Approve*/*Reject* buttons for admins, with the safety-fuse plan |
| `/mcp` | MCP servers Pipe uses and their state |
| `/journal` | Approved changes with backups (what, when, whether it can be undone) |
| `/undo [id]` | Undoes the last (or the given) change: a preview of the diff and inverse commands, then an *Undo* button. Works without the LLM |
| `/cost` | LLM token usage today and over the last days (with cost when prices are in `.env`) |
| `/language [pl\|en]` | Pipe's language: switches agent instructions, reports, bot messages and the `/` menu without a restart, shared with the CLI and `pipe web`; admin only |
| `/providers [id [model]]` | LLM providers: buttons for providers that have a key (the active one too, to change its model), then the provider's models as paged buttons; the current one is marked, "Keep ..." keeps it. `/providers groq llama` picks a model by name fragment, `/providers model` changes the model of the current provider. The bot never takes API keys (they would stay in the chat history); add them in `pipe web` or the CLI. Admin only |
| `/yolo [on\|off]` | YOLO mode: changes in this conversation run without YES/NO buttons (the safety fuse, the journal and `/undo` still work; forbidden operations are refused). Off by default, admin only, forgotten when the backend restarts |
| `/map [title]` | Infrastructure diagram as a photo (no LLM, so fast and free) |
| `/server` | Shows SERVER.md. When there is none, the agent explores the server and creates it |
| `/server update` | The agent explores the server again and updates SERVER.md and DIRECTORY |
| `/directory` | Map of repositories and directories (DIRECTORY) |
| `/skills` | List of saved skills with their commands |
| `/alerts` | Active watcher alerts and recent events |
| `/routines` | Tasks run on a schedule |
| `/reminders [cancel <id>]` | One-off reminders; they arrive on time to the person who set them |
| `/targets` | Remote servers, containers and clusters |
| `/vibe` | What the agent knows about your conversation style; `/vibe reset` clears it |
| `/<skill>` | Runs a skill, for example `/renew_certificate`. You can add hints: `/renew_certificate only for example.com` |
| `/history` | Latest audit-log entries (no LLM) |
| `/help` | Command list |

The bot sets the suggestion menu shown after typing `/` at startup and refreshes it after every message, so a new
skill shows up in the menu right away. The menu is set separately for the chat of each user in
`TELEGRAM_ALLOWED_USER_IDS`, not globally, so people outside the list don't see skill names or descriptions. If you
don't see the menu, write anything to the bot (Telegram only allows setting the menu after the chat has started).

For an unknown command the bot replies with a hint instead of staying silent.
