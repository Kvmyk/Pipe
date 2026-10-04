# Workers, targets, routines, watching, diagrams and server history -- Pipe

Pipe v0.26.1

This document describes the features that set Pipe apart from general-purpose agents:
an agent that watches the server by itself, sees its architecture and manages many machines at once.

---

## Diagrams

The agent draws diagrams in **Mermaid** syntax and sends them as an image:

- **Telegram** -- a photo (large diagrams as a file in full resolution)
- **CLI** -- a PNG in `~/.pipe/diagrams/` + an ASCII preview in the terminal when it fits
  (`pipe --open` opens the PNG automatically; `/mermaid` shows the source of the last diagram)

The `diagram` tool has two modes:

| Mode | When | Where the data comes from |
|------|------|---------------------------|
| `infra` | *"show the architecture"*, *"what is running here"*, `/map` | automatic discovery (below) |
| `mermaid` | flows, procedures, dependencies, *"draw how the deploy works"* | source written by the agent |

**The infrastructure map** (`/map` works without the LLM, so it is fast and free) combines:

- containers from `docker inspect`: image, state, healthcheck, published ports, compose project
  (a project's containers are in a frame, app -> database joined with a dashed line)
- reverse proxy routes: `server_name` / `proxy_pass` / `upstream` from nginx (`sites-enabled`, `conf.d`),
  `reverse_proxy` blocks from a Caddyfile, Traefik labels (`Host(...)`) and `VIRTUAL_HOST`
- host ports from `/proc/1/net` -- ports open to the world are connected to *Internet*
- enabled systemd services (without the system ones)
- applications from a Kubernetes cluster (ingress -> service -> deployment) when kubectl has access
- Pipe's remote targets

A stopped or `unhealthy` container is red. Labels are escaped, so a container name or a domain can't break the
diagram syntax.

Rendering: [mermaidx](https://github.com/MohammadRaziei/mermaidx) -- the real Mermaid library in an embedded
QuickJS + resvg. No browser, no Node.js and no sending the diagram source to an external service (unlike
mermaid.ink/kroki). A syntax error goes back to the model, which fixes the source.

---

## DIRECTORY -- map of directories

A complement to SERVER.md: instead of prose, a list of concrete places with a one-sentence description.

```
- /srv/shop [repo] shop (Next.js), deployed with compose (remote https://github.com/acme/shop.git, branch main)
- /srv/shop/docker-compose.yml [compose] docker compose project 'shop'
- /var/backups/pg [backup] nightly Postgres dumps, 7-day rotation
```

- `directory operation=scan` discovers git repositories (without running git: `.git/config`, `.git/HEAD`; tokens are
  stripped from remote URLs) under `/root`, `/home`, `/srv`, `/opt`, `/var/www`, `/data` and compose project
  directories from container labels. The scan doesn't overwrite the agent's descriptions.
- The agent adds places by itself when it comes across them. When you talk about a project, it checks DIRECTORY first.
- `/directory` in both clients. File: `backend/data/directory.json`.

---

## VIBE -- conversation style

Over time the agent adapts to how you write and what you expect.

- Every `VIBE_EVERY` (6 by default) of your messages, the agent sends the current note and your recent messages to
  the LLM in the background (without slowing down answers) and saves an updated note: tone, answer length,
  technical level, habits, what to avoid.
- When you say it outright (*"keep your answers shorter"*), the agent saves it right away (the `vibe` tool).
- A separate note per user: `vibe/telegram-123.md`, `vibe/cli-kuba.md`.
- Only your own messages are learned from, not prompts built by Pipe (scan, `/status`, skills).
- VIBE is style guidance: it doesn't change the security rules, confirmations or interface format.
- `/vibe` shows the note, `/vibe reset` clears it. `WORKER_MODEL` sets a cheaper model for VIBE too.

---

## Remote targets

A target is a place where Pipe runs commands **without installing anything on the other side**:

| Kind | How Pipe runs a command | Fields |
|------|-------------------------|--------|
| `ssh` | `ssh -o BatchMode=yes ... host -- 'command'` | `host`, `user`, `port`, `identity_file` |
| `docker` | `docker exec container sh -c 'command'` | `container` |
| `kubernetes` (cluster) | `kubectl --context C --namespace N ...` | `context`, `namespace` |
| `kubernetes` (pod) | `kubectl exec pod -- sh -c 'command'` | `pod`, `pod_container` |
| `local` | this host (always available) | -- |

- Adding a target requires confirmation. Fields are validated and the command goes as one argument
  (`shlex.quote`), so neither the name nor the parameters can inject commands.
- The classifier judges the **target** command, and the confirmation shows the full command with the wrapper.
- SSH uses keys from `/root/.ssh` (read-only in Docker); the targets' `known_hosts` is in `backend/data`.
  Password authentication is not supported (BatchMode), so add Pipe's public key on the target.
- Cluster: in Docker mode uncomment the `/root/.kube` mount in `docker-compose.yml`; in Kubernetes mode Pipe uses its
  own ServiceAccount.
- `/targets` in both clients. File: `backend/data/targets.json`.

Example: *"add server 10.0.0.5 as db-backup, ssh, user root"* -> YES -> *"check connectivity"*.

---

## Workers

A worker is a sub-agent the agent talks to, not you. The `delegate` tool sends several at once:

```
You:    find out why load is rising on both www servers
Agent:  delegate [web-1: "load, top processes, nginx logs from the last hour"],
                 [web-2: the same]
          › web-1: start (web-1)
          › web-1 $ uptime
          › web-2 $ ps aux --sort=-%cpu | head
          › web-1: done (4 commands)
Agent:  On web-1 the load comes from a backup cron at 14:00, on web-2 from php-fpm; I suggest ...
```

- Each worker: one target, one task, its own conversation history with the LLM, one tool `run`.
- **Reads only.** A command that changes state is not run; it goes into the report as a proposal. The agent runs it
  through `remote_exec`, that is with your approval. That way workers can run in parallel and in the background with
  nobody at the keyboard.
- Report: FINDINGS / PROBLEMS / PROPOSALS. Progress is shown live (Telegram: one message that keeps updating).
- The same worker name in a later `delegate` continues its thread.
- Limits: `MAX_WORKERS` (6), `WORKER_TIMEOUT` (240 s), `WORKER_MAX_ITERATIONS` (8).
  `WORKER_MODEL` -- a cheaper model for workers.

---

## Routines

Tasks the agent runs by itself on a schedule and reports on:

*"every day at 7 check the backups in /var/backups and certificate validity, only write when something is wrong"*

- A cron schedule (5 fields, server time) or `@hourly` / `@daily` (7:00) / `@weekly` / `@monthly`.
- A worker runs them on the given target, reads only.
- The report starts with `STATUS: OK` or `STATUS: PROBLEM`. `notify=problems` sends only problems.
- Reports arrive on Telegram (event subscription). `/routines` shows the list and the last status.
- Adding a routine requires confirmation. *"run the morning-review routine now"* -- `routine_manage run`.

---

## Reminders

A routine repeats; a reminder fires **once**, at the given time, and disappears:

*"message me in 10 seconds"* · *"remind me tomorrow at 9 to renew the domain"* · *"in an hour check whether the backup finished"*

- Relative time (`10s`, `15m`, `2h`, `1d`, `1h30m`) or absolute (`HH:MM`, `YYYY-MM-DD HH:MM`, server time).
- **Message** (`kind=message`) -- just the text, no LLM and no confirmation; setting it shows up as a `[PAMIEC]` note.
- **Task** (`kind=task`) -- at the time, a worker runs it on the given target (reads only) and sends a report.
  Requires confirmation, because it runs unattended.
- Delivery: Telegram, through the event channel. A reminder set on Telegram goes back to the person who set it; one
  from the CLI goes to the admins. When the bot isn't connected, the reminder waits and arrives once it connects.
- The CLI doesn't subscribe to events, but while waiting for input it picks up reminders every 10 s (including the
  result of `/update`) and prints them above the prompt. A reminder set from the CLI goes to subscribers (Telegram,
  `pipe web`) and also waits to be picked up by the CLI.
- The registry `backend/data/reminders.json` survives a restart; at most 50 reminders, at most a year ahead.
- `/reminders` -- the list with server time; `/reminders cancel <id>` -- cancelling (no LLM).

The agent can't wait in the middle of an answer. Without this tool a promise like *"I'll get back to you later"* would
be empty, so the prompt forbids it when nothing was scheduled.

---

## Watching

Deterministic checks every `WATCH_INTERVAL` (120 s), without the LLM and at no cost:

| Check | Threshold | Level |
|-------|-----------|-------|
| Usage of each host disk | `WATCH_DISK_PCT` (90%), >= 97% critical | warning / critical |
| RAM | `WATCH_MEM_PCT` (92%) | warning |
| 5-minute load average | cores x `WATCH_LOAD_FACTOR` (2) | warning |
| Container in a restart loop | -- | critical |
| Container `unhealthy` | -- | warning |
| A container that was running stopped | -- | critical (once) |
| **New public port on the host** | compared to the previous check | warning (once) |

- An alert is sent once, and when the problem goes away a *RESOLVED* message arrives.
- Telegram: an alert with an **Investigate** button; the agent looks for the cause (reads only) and proposes a fix.
- Active alerts are in the agent's prompt, so when asked about the server's state it takes them into account.
  `/alerts` in both clients.
- State (previous ports and containers) in `backend/data/watch_state.json`. `WATCH_ENABLED=0` turns it off.

---

## What changed -- the server's time machine

The most common question during an outage: *"what changed that made it stop working?"*. Pipe knows the answer,
because every `SNAPSHOT_INTERVAL` (1 h) it takes a snapshot of the host, with plain file reads and `docker inspect`,
no LLM:

| Section | What you see |
|---------|--------------|
| System | kernel, server reboot (new boot) |
| Packages | installs, upgrades, removals (dpkg, apk) |
| Containers | new and removed, image change **or a new image version under the same tag**, stops |
| Ports | new and closed TCP ports on the host |
| Services | enabled/disabled systemd services, changed unit files |
| Cron | added and removed lines (secrets redacted) |
| Accounts | new accounts with a shell or uid 0 |
| SSH keys | new keys in `authorized_keys` (fingerprint + comment; the key itself is not stored) |
| Configuration | sshd, sudoers, nginx, Caddy, HAProxy, fstab, hosts, `daemon.json`, project compose files |

- A snapshot is saved only when something changed. The last 48 h are all kept, older ones one per day, for at most
  `SNAPSHOT_KEEP_DAYS` (30) days. Files: `backend/data/snapshots/`.
- `/changes [24h|3d]` shows changes grouped into windows like *"between 03:00 and 04:00"*.
- The agent starts from the change history by itself when it hears *"it stopped working"*, and for **Investigate**
  (an alert) the backend adds the last day of changes to the request.
- Changes to accounts, SSH keys, sudoers and sshd are marked **[BEZPIECZENSTWO]** (security).

---

## Charts

On every check (every 2 min) the watcher saves a sample: load, RAM, swap, disk usage. The history
(`METRICS_KEEP_DAYS`, 8 days) gives you charts without Prometheus:

- `/chart` -- load over the last day; `/chart ram 7d`, `/chart disk 3d`
- *"has RAM been growing for a week?"* -- the agent draws a chart (the `server_history` tool, `operation=chart`) and
  gets the numbers (min, max, average, threshold crossings), because it can't see the image itself
- The red line on the chart is the watcher's alert threshold.

A chart is a Mermaid `xychart-beta` rendered by the same engine as diagrams (offline).

---

## Monitoring without configuration

Pipe knows what is on the server, so it knows what to check by itself, every `CHECKS_INTERVAL` (1 h):

| Check | How Pipe knows what to check | Alert |
|-------|------------------------------|-------|
| TLS certificate validity | domains from nginx, Caddy, Traefik labels / `VIRTUAL_HOST` | `WATCH_CERT_DAYS` (14) days before expiry; critical 3 days before or when the certificate is invalid |
| HTTPS response | the same domains | 5xx or no answer **twice in a row** (a brief 502 during a deploy doesn't alert) |
| DNS | the same domains | the domain doesn't resolve; in `/health` also whether it points at this server |
| Backup freshness | `[backup]` entries in DIRECTORY | the newest file older than `WATCH_BACKUP_HOURS` (26 h), an empty or missing directory |

- `/health` -- everything at once, with the number of days until each certificate expires.
- You add a backup directory with one sentence: *"the database backups are in /var/backups/pg"* (the agent adds a
  `[backup]` entry).
- `WATCH_SITES=0` turns off network checks, `WATCH_IGNORE=a.example.com,/var/backups/old` skips chosen ones.

---

## Morning report

Every day at `DIGEST_TIME` (7:00 server time; `off` turns it off) a report arrives on Telegram, **without the LLM**:

- **Health** -- uptime, load relative to cores, RAM, disks
- **Alerts** -- active ones and events from the last day
- **Changes since yesterday** -- security changes first, then containers, ports, services; packages summarised
- **Checks** -- the soonest-expiring certificates, sites, backups
- **Updates** -- the number of packages to update and *reboot required* (Ubuntu/Debian)
- **Routines** -- the last status of each routine
- **LLM** -- token usage from the previous day

A load chart for the last day is attached. `/report` -- the report on demand.

---

## LLM costs

Every request to the provider (conversation, workers, routines, VIBE) is counted in `backend/data/usage.json`.

- `/cost` -- tokens today (split by user, workers, routines, VIBE and model), recent days, the month.
- The cost in USD shows up once you give the model prices: `LLM_PRICE_IN` and `LLM_PRICE_OUT` (USD per million
  tokens), and separately `WORKER_PRICE_IN/OUT` for `WORKER_MODEL`.
- `DAILY_TOKEN_LIMIT` / `DAILY_COST_LIMIT` -- once exceeded, the agent answers with a limit message until midnight
  and sends no requests to the provider. Watching, the report and commands without the LLM (`/map`, `/changes`,
  `/chart`) keep working.

---

## Changes with a safety fuse and `/undo`

Every operation you approve gets a **plan**, shown in the confirmation before you press YES:

```
REQUIRES CONFIRMATION: `sed -i 's/8080/8081/' /etc/nginx/sites-enabled/shop && systemctl reload nginx`
Safety fuse:
- backup before the change: /etc/nginx/sites-enabled/shop
- check before (failure = I won't run it): nginx -t
- verify after: nginx -t; nginx service active; sites that work now must still work after the change
- if verification fails: I restore the files from the backup automatically and reload again
- undo later: /undo
```

| Step | What Pipe does |
|------|----------------|
| Backup | files changed by `sed -i`, `tee`, `>`/`>>`, `cp`, `mv`, `rm`, `chmod`/`chown`, `write_file`; the repository HEAD for `git pull/checkout/reset/commit`; the crontab; Pipe's memory files when targets, routines, skills and SERVER.md change |
| Before | config validation before a (re)load: `nginx -t`, `sshd -t` (protects against locking yourself out of SSH), `caddy validate`, `apachectl configtest`, `haproxy -c`, `postfix check`, `docker compose config -q`; in a container `docker exec <nginx> nginx -t`. **A failed check = the operation doesn't run** |
| After | service `active`, container `running` and not `unhealthy`, all containers of the compose project came up, the changed file passes validation (nginx, sshd, sudoers `visudo -c`, fstab, compose, JSON), **sites that answered before the change answer after it** |
| Restore | failed verification of a config file change -> the backup comes back automatically (`SAFE_AUTO_ROLLBACK=1`) and the service is reloaded again. Other operations: the result goes to the agent, and you can `/undo` |

The plan comes from analysing the command in code (no LLM); exactly the plan you saw is executed. A program that
doesn't exist where Pipe runs (for example `sshd` in a container) is skipped and reported as *not checked*, and the
plan says so outright.

**Journal and undo.** `/journal` shows recent changes (`backend/data/journal/`, 50 entries, 14 days). `/undo` (or
*"undo the last change"*) shows file diffs and inverse commands (`docker stop` -> `docker start`,
`systemctl disable` -> `enable`, `git pull` -> `git reset --keep <previous HEAD>`, `docker compose down` -> `up -d`)
and waits for YES. `/undo` works without the LLM, so it also helps when the provider doesn't answer or the daily limit
ran out. An undo goes into the journal too, so it can be undone.

What Pipe doesn't undo automatically (and says so in the plan): package installs, changes in a Kubernetes cluster,
removed containers and volumes, `git clean`, directories removed with `rm -r`, previous image versions.

---

## Security audit and sentinel

`/audit` (or *"how secure is this server?"*) -- a score of 0-100 without the LLM, each point with a fix:

| Area | What it checks |
|------|----------------|
| SSH | password login (taking `sshd_config.d` and the "first value wins" rule into account), root with a password, keys |
| Firewall | ufw, firewalld, nftables, iptables (a firewall in the provider's panel is invisible to Pipe, and it says so) |
| Ports | databases, Redis, Elasticsearch, Docker/Kubernetes API on a public address; a port published by a container gets a fix in compose, because **Docker bypasses ufw** |
| Containers | `privileged`, mounted `docker.sock`, host network |
| Accounts | uid 0 other than root, accounts without a password (only account names are read from `/etc/shadow`) |
| Updates | unattended-upgrades, pending security fixes, reboot required |
| Protection | fail2ban / CrowdSec (more important when SSH accepts passwords) |
| Secrets | world-readable `.env` files in application directories from DIRECTORY |
| Hygiene | time sync, swap on a small machine, certificates |

*"fix 1"* -- the agent applies the fix with a normal tool, so you get it for approval with a safety-fuse plan (backup,
`sshd -t` before reloading, `/undo`). An SSH fix goes into its own `sshd_config.d/00-pipe-*.conf` file, so it wins
over cloud-init settings. Without a key in `authorized_keys` Pipe **doesn't propose** disabling passwords; the key
comes first (the `harden-ssh` skill). In docker mode the commands are marked *in the host shell* (the host file
system is read-only).

**The sentinel** (every `WATCH_INTERVAL`, no LLM) compares accounts, SSH keys, SUID/SGID programs and authentication
configs (sudoers, sshd, PAM, `/etc/passwd`, `ld.so.preload`). A new account with uid 0, a new key, a new SUID or
`ld.so.preload` raises a critical alert. Changes made by Pipe (they are in the journal) don't alert.

**SSH log** (`/var/log/auth.log` or `/var/log/secure`, read incrementally): a series of failed logins, a successful
password login from an address that was guessing passwords before, a login from a new address.

---

## The first 5 minutes

After installation, when the Telegram bot connects for the first time, Pipe writes by itself: a map of the server as
an image, a security score with the three most important fixes and a list of what it watches from now on. The CLI
shows the same on the first connection to a given server. No LLM.

---

## Built-in skills

| Skill | What it does |
|-------|--------------|
| `nginx-vhost` | a new domain in nginx as a reverse proxy + a Let's Encrypt certificate |
| `swap` | a swap file with an fstab entry (btrfs too) |
| `fail2ban-ssh` | fail2ban with an SSH jail, without banning your own address |
| `backup-postgres` | a nightly `pg_dump` with rotation, a `[backup]` entry in DIRECTORY (the watcher checks freshness) |
| `update-container` | pull + up of a compose service with a plan to go back to the previous image |
| `harden-ssh` | disabling SSH passwords in an order that won't lock you out |
| `free-disk-space` | what takes up the disk and a safe cleanup, step by step |

Installed at startup into `backend/data/skills/` like normal skills, so you have them in `/skills` and as commands
(`/nginx_vhost`). You can edit and delete them: a new version from a Pipe update replaces only a skill you haven't
changed, and doesn't bring back a deleted one.

With `PIPE_LANG=pl` the Polish versions are installed: `aktualizuj-kontener`, `utwardz-ssh`, `wolne-miejsce` (the
other four have the same name in both languages).

---

## Incident memory

Every resolved alert stays in `backend/data/incidents.json`: what happened, when and for how long, what the agent found
after *Investigate* (or a worker for a webhook alert) and what helped, meaning the journal changes made while the
problem lasted. No extra LLM requests.

When the same problem comes back, the Telegram alert has a line **Previously: 2026-09-12 (40 min) — findings: nginx
logs without rotation... — helped: journalctl --vacuum-size=200M**, and the *Investigate* request gets this history:
the agent first checks whether it is the same cause and proposes the fix that worked (still for approval).
`/incidents` shows the history; the agent reaches for it by itself (*"has this happened before?"*).

---

## Alerts from outside (webhooks)

Pipe joins the monitoring you already have instead of replacing it:

```bash
# backend/.env
WEBHOOK_PORT=7380
WEBHOOK_TOKEN=$(openssl rand -hex 24)
```

| Source | Address | Configuration on the sender's side |
|--------|---------|------------------------------------|
| Prometheus Alertmanager | `/hook/alertmanager` | `webhook_configs: - url: http://127.0.0.1:7380/hook/alertmanager` + `http_config.authorization.credentials: <token>` |
| Grafana | `/hook/grafana` | a Webhook contact point, header `Authorization: Bearer <token>` |
| Uptime Kuma | `/hook/uptime-kuma?token=<token>` | a Webhook notification (application/json) |
| GitHub | `/hook/github` | a repository webhook, *Secret* = token; events *Workflow runs*, *Deployment statuses* |
| Anything else | `/hook/generic` | `{"title", "message", "severity", "status": "firing|resolved", "name"}` |

The alert goes to the watcher (it disappears on `resolved`) and to Telegram with an *Investigate* button. With
`WEBHOOK_INVESTIGATE=1` a worker investigates a new alert right away (reads only: services, containers, logs,
resources, recent changes) and the report arrives shortly after the alert; the same alert at most once an hour, 10
investigations a day.

In Docker the port is published only on `127.0.0.1:7380`; senders outside the server go through a reverse proxy with
TLS.

---

## Attachments: files, screenshots, logs

You can send the agent a file together with a question: a screenshot of an error, a log, an nginx config. On Telegram
a photo or a document (the caption is the question, an album of several photos goes as one message), in `pipe web`
the paperclip, dragging a file onto the window or pasting a screenshot from the clipboard (thumbnails before sending),
in the CLI `/file <path> [question]` or `@path` in a normal message (Tab completes paths).

The backend (`core/attachments.py`) recognises a file by its content, not its name:

- an image (PNG, JPEG, GIF, WebP) goes to the model as an image and stays in the history for one turn only; then a
  marker replaces it so later questions don't send it again. When the model doesn't accept images (for example some
  Ollama models), Pipe says so outright: *the model cannot see images*, and answers without the image,
- text (UTF-8) is pasted into the message after secret redaction and truncation to 24k characters,
- everything else (archives, binaries, PDF) goes to `DATA_DIR/uploads/` (0600, cleaned up after 7 days), and the agent
  gets the path; moving the file somewhere else is a normal change with confirmation.

Limits: 10 MB per file, 20 MB and 10 files per message. Attachment content is data, not instructions: YOLO mode is
paused until the next message, just like after reading a web page.

## Voice messages

On Telegram just record a message: *"find out why the shop is down"*. The bot replies *I heard: ...* and passes the
text to the agent. In `pipe web` there is a microphone button: the browser turns the recording (up to 5 minutes) into
16 kHz WAV and sends it for transcription, and the text goes to the agent. Transcription: with `LLM_PROVIDER=gemini`
the Gemini model itself does it (audio input), with `openai` or `groq` that provider's Whisper endpoint does; in both
cases without configuration. With other providers set `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` (for example the free
Groq `whisper-large-v3-turbo` or a local faster-whisper); `STT_*` always takes precedence.

---

## Roles and multiple users

| Role | Can | Can't |
|------|-----|-------|
| admin | everything | -- |
| viewer | conversation, diagnosis, reads, charts, reports, audit, alerts | approve changes, `/undo`, write Pipe's memory (SERVER.md, skills, targets, routines) |

- Telegram: `TELEGRAM_ALLOWED_USER_IDS` (admins) and `TELEGRAM_VIEWER_IDS` (read only, with `AGENT_VIEWER_TOKEN`).
- CLI and other clients: a separate, revocable token per person or laptop:
  `python3 -m backend.tokens add laptop-kuba --role admin`, `list`, `revoke laptop-kuba`.
- The backend enforces roles. A session belongs to the token that created it.

---

## Web interface and live map

`pipe web` opens an interface with the conversation and a side panel in the browser. It runs on your computer, through
the same tunnel as the CLI.

**The map** builds itself from what Pipe knows about the infrastructure; nothing to draw or configure:

| Level | What you see |
|-------|--------------|
| Servers | this server and remote targets (SSH, containers, clusters) |
| Inside a server | internet, reverse proxy, compose projects, standalone containers, services and ports |
| Project | the project's containers, their ports and dependencies (app -> database), the project directory |

- When the agent does something, the element it concerns is highlighted and pulses run along the connections. An
  action waiting for your approval has a different, dashed outline. When it finishes, the element flashes green or
  red.
- **Follow the agent** (on by default) moves the view after the agent, also between levels. Any move of your own
  (pan, zoom, click) turns following off; the button turns it back on.
- Clicking an element shows its state, alerts and what the agent is doing on it now and did recently; clicking a
  project or a server goes one level down.
- Below the map is a timeline of actions, with the safety-fuse steps (backup, check, verification) and workers.

An action is assigned to an element from the command itself: a container name, a systemd unit, a file path, a project
directory. When that's not possible (for example an arbitrary script), the action is shown on the whole server.
Searching and reading pages (`web_search`, `web_fetch`, `software_info`) lights up the *Internet* element.

**The side panel** takes half the screen by default. You can drag its edge to make it narrower or wider, up to half
the screen; a double click on the edge restores the default width, and dragging it to the right edge of the window
hides the panel. A button in its tab bar hides it too; a button in the top bar hides it and brings it back. With the
panel hidden the conversation takes the whole window (the content in a readable column in the middle), and a command
or a notification that opens a tab (for example `/alerts`) brings the panel back by itself. The width and state of the
panel are kept in the browser. From the keyboard: focus the edge, the arrows change the width, Enter hides it.

**Changes.** Before you approve a file write you see the diff in the confirmation card. After it runs, the "before ->
after" diff appears in the conversation and in the *Changes* tab, also when a command changed the file (for example
`sed`), because the safety fuse takes a backup. Secrets in diffs are redacted. Every entry has an undo button. There
are no diffs for directories and data in databases.

**Commands and skills.** Typing `/` in the message box opens the list of commands (`/report`, `/changes`, `/audit`,
`/cost`...) and skills with descriptions; pick with the arrows or a click, Tab completes the command so you can add
arguments. The *Skills* tab shows saved procedures with a content preview and a run button.

**LLM provider.** On the first visit the page asks which provider Pipe should work with: you pick it from the list,
paste the API key (it is checked with the provider before it is saved) and pick a model from its current list. You
can skip this screen; the provider from the installation stays. Above the message box there is a switcher showing who
answers; it opens the list of providers that have a key and switches without a restart, in the middle of a
conversation. `/provider` opens the screen again (another key, a different model, removing a key). Keys are stored on
the server in `DATA_DIR/llm_keys.json` (0600) and never go back to the browser. The choice applies to the whole agent:
after switching, Telegram, the CLI, routines and workers use the new provider too; `WORKER_MODEL` and prices from
`.env` apply only to the base provider. You can also put several keys straight into `backend/.env`
(`OPENAI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`...).

---

## Language: Polish or English

`PIPE_LANG=pl` (default) or `PIPE_LANG=en` switches the whole product, not just the language of the model's answers.
You can also change the language while working, without a restart: `/language en` / `/language pl` (`/jezyk`) in the
CLI, in `pipe web` (the command or the PL/EN button in the header) and on Telegram. The choice is shared by the whole
agent (the other channels switch by themselves), it is saved in `DATA_DIR/language.json` and holds until `PIPE_LANG`
in `.env` is changed by hand. Only an admin can change it.

| Layer | What changes |
|-------|--------------|
| Agent | system prompt, tool descriptions, prompts for workers, routines, the server scan and VIBE |
| Messages | confirmation questions, the safety-fuse plan, tool results, validation errors |
| Watching | alert titles, the morning report, `/changes`, `/health`, `/audit` with fixes, the welcome |
| MCP | titles and descriptions of the MCP server's tools, instructions for the external agent |
| Clients | CLI (`--lang en`), the Telegram bot (`/` menu, buttons, labels), the wizard and the installer |
| Skills | built-in skills in English |

Protocol tags (`[POTWIERDZ]`, `[BLAD]`, `[ODMOWA]`, `[PAMIEC]`, `[ZREDAGOWANO: ...]`) are the same in both languages:
the server derives the frame status from them, and clients turn them into labels in their own language. Commands have
English aliases (`/report`, `/changes`, `/undo`, ...) that work alongside the Polish ones.

Data saved earlier (SERVER.md, VIBE, your own skills, journal entries) stays in the language it was written in.

---

## MCP -- Pipe as a gateway for other agents and a client of other servers

### Pipe as an MCP server

Claude Code, Cursor or your own agent gets Pipe's tools instead of a bare shell on production:

| Tool | What it does |
|------|--------------|
| `run_command` | a command through Pipe's classifier: a read runs right away; a change -> **admin approval**, then the safety fuse and the journal; forbidden -> refused |
| `get_approval` | the state of an approval and its result (only for the agent that asked for it) |
| `read_file` | a host file; files with secrets are not handed out, secrets in the content are redacted |
| `server_status`, `server_changes`, `infra_map`, `health_checks`, `security_audit`, `journal` | Pipe's knowledge about the server |
| `ask_pipe` | a question to the Pipe agent (it knows SERVER.md, DIRECTORY, the history), in a read-only role |

The approval arrives on Telegram with the command and the safety-fuse plan (backup, `nginx -t`, verification) and
*Approve* / *Reject* buttons; in the CLI, `/approvals`. The agent learns the result through `get_approval`.

**Connecting from a laptop** -- the stdio bridge in the CLI, through the same SSH tunnel and tokens:

```json
{"mcpServers": {"pipe": {"command": "pipe", "args": ["--mcp", "--host", "root@server"],
                         "env": {"AGENT_TOKEN": "<token>"}}}}
```

A separate token for the agent: `python3 -m backend.tokens add claude-code --role admin` (or `--role viewer`: the
agent only reads and can't ask for approvals). **On the server** -- a Streamable HTTP endpoint: `MCP_PORT=7381`,
`http://127.0.0.1:7381/mcp`, the token in `Authorization: Bearer`.

Supported protocol versions: 2026-07-28 (`server/discover`, the version in `_meta`, `Mcp-Method`/`Mcp-Name` headers)
and older ones with `initialize` (2025-11-25, 2025-06-18, 2025-03-26).

### Pipe as an MCP client

Tools of other MCP servers become the agent's tools (`mcp__github__create_issue`...). You add them with a sentence
(*"add the MCP server github: npx -y @modelcontextprotocol/server-github, token in GITHUB_PERSONAL_ACCESS_TOKEN,
get_* and list_* without asking"*) or in `backend/data/mcp.json`:

```json
{"servers": {
  "github":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
              "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}, "autoApprove": ["get_*", "list_*", "search_*"]},
  "grafana": {"url": "http://127.0.0.1:8000/mcp", "headers": {"Authorization": "Bearer ..."}, "trustReadOnly": true}
}}
```

- Every call needs a YES with the arguments visible, unless the tool matches `autoApprove` or the server has
  `trustReadOnly` and the tool declares `readOnlyHint`. An approved call goes into the change journal.
- Adding a server always requires confirmation. A stdio subprocess gets neither the LLM key nor Pipe's tokens.
- stdio and Streamable HTTP; the protocol version is detected automatically (2026-07-28 or `initialize`).
- `/mcp` shows the servers and their state; *"reconnect the MCP servers"* -- `mcp_manage reload`.

---

## Internet without API keys

The agent goes to the web by itself when the answer depends on knowledge from outside the server; you don't have to
ask it or give it any key:

- `web_search` -- DuckDuckGo, and when it doesn't answer: Stack Exchange (Server Fault, Stack Overflow), then Wikipedia.
- `web_fetch` -- reads a page; with a question, a separate model without tools reads the page and gives the agent only
  the answer.
- `software_info eol` -- whether a version is supported and until when (endoflife.date); `software_info vulns` --
  known vulnerabilities of a package in the installed version (OSV.dev), with the version that fixes them.

Examples: *"nginx keeps logging upstream prematurely closed, what is that?"*, *"is our PostgreSQL still supported?"*,
*"does nginx on this server have known holes?"*. The agent gives its sources. The safeguards (secrets, addresses
outside the search results, private network, pausing YOLO) are described in [Security](security.md).
`WEB_SEARCH=off` turns the internet off.

## Updating Pipe itself

Type `/update` (in Polish `/aktualizuj`) or tell the agent "update yourself"; `/update check` only shows the versions
and whether a newer one exists. The agent checks the remote repository, and after your confirmation starts a separate
helper container that runs `git pull --ff-only` in the install directory and rebuilds Pipe's services
(`docker compose up -d --build`). A separate container is needed because the agent can't replace the container it
runs in; the process would die halfway.

- The backend is unavailable for a minute or a few, and an ongoing conversation is cut off (sessions don't survive a
  restart).
- After the restart, a message with the result goes to whoever requested the update. If pulling or building fails,
  the old version keeps running and the message contains the end of the log.
- Only services that were already running are rebuilt (the Telegram bot won't start if you don't use it).
- Local changes to repository files can block `git pull`; the agent warns about them when checking.
- Docker mode only. In native mode: `git pull` and `sudo systemctl restart pipe`; in Kubernetes: a new image.
