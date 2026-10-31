# Workers, targets, routines, watching, diagrams and server history -- Pipe

Pipe v0.50.0

This document describes the features that set Pipe apart from general-purpose agents:
an agent that watches the server by itself, sees its architecture and manages many machines at once.

---

## Pipe writes first

You don't have to ask what is going on. Pipe writes on its own -- on Telegram, in `pipe web` and in the CLI -- when
something needs attention, and gives a button that deals with it right away. Everything it sends also lands in the
**inbox** (`backend/data/feed.json`), so nothing is lost when nobody was connected.

| Message | When | Buttons |
|---------|------|---------|
| Alert | the problem is already there: disk, memory, container, site, certificate, vulnerabilities | *Free up space*, *Patch*, *Investigate* |
| Findings | a critical problem: Pipe checks the cause itself (a worker, reads only) and writes what it found | *Fix it*: routine fixes run at once, anything important waits for your yes |
| Suggestion | a problem is coming or something can be improved (table below) | the action, *Later*, *Fewer like this* |
| Morning report | at `DIGEST_TIME` | -- |
| Resolved | the problem went away | -- |

**Suggestions** are computed without a language model, from data Pipe collects anyway (every `INSIGHTS_INTERVAL`, 1 h):

| Suggestion | From |
|------------|------|
| *Disk / will be full in about 4 days* | the growth rate from the metric history (a regression over the last 48 h) |
| *I can free about 6 GB* | untagged Docker images, build cache, downloaded apt packages |
| *Container X restarted 4 times within a day* | the restart counter (restart loops raise an alert) |
| *The server has been waiting 5 days for a reboot* | `/run/reboot-required` |
| *Memory keeps growing* | RAM use growing for a day (a possible leak) |

**Investigating on its own.** When a critical problem appears (a container stopped, a site does not answer, the disk
is full), Pipe does not wait for *Investigate*: a worker checks the logs, resources and the changes of the last day
(reads only), and the findings arrive right after the alert, with a *Fix it* button. `AUTO_INVESTIGATE=critical`
(default), `all` or `off`; at most `AUTO_INVESTIGATE_DAILY` (4) investigations a day, the same problem at most once
every 6 h.

**When to notify.** The level (`NOTIFY_LEVEL`): `all` -- everything; `important` (default) -- problems, warnings, the
report and important suggestions, informational ones wait in the inbox; `critical` -- only what cannot wait. Quiet
hours (`QUIET_HOURS=22:00-07:00`): at night only critical matters come through, the rest arrives in the morning
together with the report. At most `INSIGHTS_PER_DAY` (3) suggestions a day with a notification. *Fewer like this*
mutes that kind of suggestion (it stays in the inbox without a notification), *Later* snoozes it for a day. You can
change the same settings while Pipe runs: `/notifications` (Telegram -- with buttons, CLI --
`/notifications important quiet 22-7 daily 3`, `pipe web` -- the bell on the dashboard).

- `/news` (`/nowosci`): the inbox with actions (in the CLI you type e.g. `1a`). A short status without a model: `/status`.
- The backend builds the buttons -- a client only sends back the action number (`feed_act`). *Fix it* and *Patch* are
  for the administrator.
- Clients notify only about messages with `push` (level, quiet hours, limit); the CLI prints them above the prompt,
  `pipe web` as a pop-up, Telegram as a message.

---

## The workshop: projects Pipe sets up on its own

Write *"set this app up for me: https://github.com/acme/notebook"* or *"write a script that counts the orders in the
logs"*, and Pipe sees it through on its own. Projects live in one host directory -- the **workshop** (`WORKSPACE_DIR`,
`/root/pipe-workspace` by default), each in its own subdirectory and in `backend/data/projects.json`.

| Step | What Pipe does | Asks? |
|------|----------------|-------|
| `clone` | `git clone --depth 1` from an https or `git@` address (the server's SSH key); no hooks, submodules, LFS; a repository that is too big (`PROJECT_MAX_MB`) is removed | no |
| `inspect` | what it is and how to run it -- without a model: compose, Dockerfile, Node, Python (Django, FastAPI, Flask, Streamlit), Go, PHP, a static site; ports, needed variables, README and AGENTS.md | no |
| writing files | `write_file` in the workshop directory (e.g. a Dockerfile, a script) | no |
| `exec` | a command in a one-off container: only the project directory as `/work`, no `docker.sock` and no privileges (`--cap-drop ALL`, `no-new-privileges`), memory, process and time limits | no* |
| `run` | a long-running service: container `pipe-<name>` (or compose project `pipe-<name>`), the port on `127.0.0.1` only | no -- the plan and progress show in the conversation |
| `logs`, `stop`, `start`, `pull` | logs, stopping, resuming, `git pull --ff-only` | no |
| `remove` | the directory, containers and the built image | **yes** |

\* After reading content from outside (the README of a cloned repository, a web page) Pipe asks for approval of `exec`
until your next message -- like YOLO: text from someone else's repository cannot run code by itself.

- Code from a repository never runs in Pipe's process or container (that is where `docker.sock` is). It is installed
  and run in containers -- no Node.js, Python or libraries pile up on the server.
- `run` is in the change journal: `/undo` stops and removes the deployment. Running it again keeps the same port.
- A compose project with risky settings (`privileged`, `docker.sock`, host network, mounting `/etc`) or ports open to
  the world -- the confirmation says so plainly.
- When `inspect` does not know how to run a project, the agent writes a Dockerfile in its directory and uses
  `run strategy=dockerfile`.
- The app's address is `http://127.0.0.1:<port>` on the server (from a laptop: `ssh -L <port>:127.0.0.1:<port> <server>`);
  publishing it under a domain through the reverse proxy is a separate request (a normal confirmation).
- `/projects` (`/projekty`) in the CLI and on Telegram, project cards with logs and stopping on the `pipe web`
  dashboard. `WORKSPACE_AUTONOMY=0` -- approval for every operation. Observe mode and the viewer role -- list and logs only.

---

## Saving tokens

Watching, suggestions, reports, `/status`, `/changes`, `/audit`, `/cleanup` and the dashboard work without a language
model -- zero tokens. When the conversation does go to the model, Pipe sends less:

- **Tools on demand** (`TOOLS_MODE=lazy`). The descriptions of the 28 tools are ~6k tokens with every step of the
  agent. A conversation starts with the basic ones (~2.1k), and the rest turns on by itself when a message needs it
  (*"remind me tomorrow"* -> reminders, *"is the server secure"* -> audit) or when the model asks (`load_tools`). An
  enabled group stays until the end of the conversation, so the provider's prompt cache keeps working.
  `TOOLS_MODE=all` -- all of them, always.
- **Trimming old results** (`CONTEXT_KEEP_TURNS`, 2). Tool results older than two turns go to the model as their
  beginning with a note (*"if you need it, run it again"*). JetBrains research (*The Complexity Trap*, NeurIPS 2025)
  showed that this cuts an agent's cost by more than half without losing effectiveness. The history in the session
  stays complete.
- **A shorter prompt**: no repeated tool list and no mandatory directory prefix in every answer.
- `/cost` and the dashboard show how many tokens Pipe **did not send** (tools on demand, trimmed results) and how much
  of the input came from the provider's cache.

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

> Experimental: it works and has tests, but has seen less use on real servers than the rest of Pipe.
> If something is off, please open an issue.

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

> Experimental: it works and has tests, but has seen less use on real servers than the rest of Pipe.
> If something is off, please open an issue.

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
  The worker only reads, so Pipe sets it up without asking (after confirmation with `AUTONOMY=0`).
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
- An alert has buttons (**Investigate**, for the disk **Free up space**, for vulnerabilities **Patch**); Pipe
  investigates a critical one itself and sends the findings (section *Pipe writes first*).
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
- **Vulnerabilities** -- how many server packages have a fix (how many of them urgent and actively exploited), the
  three most dangerous with a description, whether the server waits for a reboot, remote servers, images and
  applications with fixes; at the end `/vulns` and `/patch`
- **Routines** -- the last status of each routine
- **LLM** -- token usage from the previous day

A load chart for the last day is attached. `/report` -- the report on demand.

---

## LLM costs

Every request to the provider (conversation, workers, routines, VIBE) is counted in `backend/data/usage.json`.

- `/cost` -- tokens today (split by user, workers, routines, VIBE and model), recent days, the month, plus how many
  tokens Pipe did not send and how much input came from the cache (section *Saving tokens*).
- The cost in USD shows up once you give the model prices: `LLM_PRICE_IN` and `LLM_PRICE_OUT` (USD per million
  tokens), and separately `WORKER_PRICE_IN/OUT` for `WORKER_MODEL`.
- `DAILY_TOKEN_LIMIT` / `DAILY_COST_LIMIT` -- once exceeded, the agent answers with a limit message until midnight
  and sends no requests to the provider. Watching, the report and commands without the LLM (`/map`, `/changes`,
  `/chart`) keep working.

---

## Changes with a safety fuse and `/undo`

Pipe makes routine changes itself: restarting and reloading services, `docker compose up -d`, config edits,
packages from the system repository, `git pull`. They go through the same plan as a change after YES, and the
conversation shows them marked *without asking*. It always asks about deleting data, access to the server (SSH,
users, firewall), autostart (cron, systemd), code and commands it does not recognise -- the full list:
`docs/security.md` (*Autonomy*). `AUTONOMY=0` brings back the question for every change.

Every change gets a **plan** -- for important ones shown in the confirmation before you press YES:

```
I need your approval
$ sed -i 's/8080/8081/' /etc/nginx/sites-enabled/shop && systemctl reload nginx
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
| Vulnerabilities | known vulnerabilities with a fix in system packages, container images, remote servers and applications (OSV.dev, CISA KEV) -- see below |
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
`ld.so.preload` raises a critical alert. Changes made by Pipe (they are in the journal) don't alert. Neither do package
updates: a SUID program or a config whose content matches the checksum in the package database (dpkg `md5sums` and
`Conffiles`, apk) is a patch, not a swap. A binary swapped after installation still raises an alert.

**SSH log** (`/var/log/auth.log` or `/var/log/secure`, read incrementally): a series of failed logins, a successful
password login from an address that was guessing passwords before, a login from a new address.

**Package vulnerabilities** (Debian, Ubuntu, Alpine). Pipe reads the package list from the dpkg/apk database (it runs
nothing) and asks [OSV.dev](https://osv.dev) about known vulnerabilities for the exact versions. Only what has a fix
counts -- Pipe leaves out vulnerabilities without a fix, because a plain Debian has hundreds and nothing can be done about
them. The report starts with "What to do about it": one line for the server, the images and the applications, each with
a concrete step. Only package
names and versions are sent to OSV; `VULN_INTERVAL=0` turns the scan off.

- When: whenever the package list changes (a new application, an update -- then OSV only gets the changed packages)
  and every `VULN_INTERVAL` (6 h) for all of them, because new vulnerabilities show up in old versions. `/vulns`,
  `/audit` and `/patch` read the list of packages, images and dependencies afresh on every call: changed
  ones go to OSV right away, everything at most every 10 minutes, so it usually takes a few seconds. When the scan
  takes longer than 45 s, you see the last result with the time it comes from.
- An alert comes only about **new urgent** ones: actively exploited (the CISA KEV catalog) or critical and high.
  The alert arrives on its own (Telegram, `pipe web`) and says in plain words what each vulnerability is about and
  what it risks, with the CVE number at the end in brackets. Pipe first waits (up to 2 minutes) for the model's
  explanations, without them it sends the OSV description. Known vulnerabilities don't come back after a Pipe
  restart. The alert ends with what the *Patch* button will do and what it will not patch: fixes only in Ubuntu Pro,
  containers without compose and Pipe's own containers (`/update` refreshes those). The rest is in the morning report
  and in `/audit`; asking the agent about vulnerabilities also gives a description of each one.
- In `/audit` vulnerabilities take at most three short points (KEV, critical and high, the rest) with the package
  names and a pointer to `/patch`, which upgrades only those packages. The point about actively exploited ones comes
  first, and a package `/patch` will not install now has the reason next to its name (*Ubuntu Pro only*, *unchanged
  by the last patch*). What could not be checked (for example a remote server without a connection) is in the *Not
  checked* line. A separate message with the vulnerabilities follows the audit: each package with a description of
  the most serious one. The alert and the report always fit one Telegram message, the rest is counted. In docker
  mode Pipe installs the patches on the host through a helper container (see *Host commands* in security); on
  Kubernetes you install the node's patches in its shell.
- **`/patch` patches right away.** Plain `/patch` (or the *Patch* button on the alert in Telegram and `pipe web`)
  approves the whole patch: Pipe does not show commands or ask about each one. It installs this server's package
  fixes, refreshes the container images from compose and patches the remote servers, in that order, with live steps.
  `/patch images` does only the images, `/patch server` only this server's packages, `/patch servers` only the remote
  servers, `/patch web2` one of them. The server builds the commands from the current scan, without the LLM. Each part
  goes through the safety fuse (sites before and after, a journal entry); when the verification after a change fails,
  Pipe does not patch the remaining parts. Then it scans again and says what it patched, what stayed unchanged and
  why, and what is left to do by hand. Patching runs in the background: closing the client does not stop it, and the
  result then arrives as a message.
- apt gets the full tidy-up: first finishing an interrupted install (`dpkg --configure -a`), waiting for the lock
  instead of failing while unattended-upgrades runs, and removing downloaded packages and old kernels that apt itself
  considers unneeded (the running and the newest stay, so `/boot` does not fill up). Then `--only-upgrade` of just the
  vulnerable packages and the same cleanup once more. Pipe upgrades the kernel through its metapackage
  (`linux-image-generic`, `linux-image-amd64`...), because a new kernel is a new package. After the patch it also
  removes untagged images, build cache older than a day and the npm, pip, yarn and composer caches, and the result
  says how much space it freed.
- A package apt did not upgrade and an image nothing changed are not tried again for a day; the report says why.
  Fixes that exist only in Ubuntu Pro (ESM) are not tried at all until the subscription is on (`pro attach`). `/patch`
  does not recreate Pipe's own containers (that is `/update`).
- System packages can't be reliably rolled back (the old version is often gone from the repository), so `/undo`
  doesn't cover them. Libraries and the kernel apply after a service or server restart: when the system is waiting
  for a reboot, the report says so.
- The kernel has thousands of CVE records, so for it Pipe only takes distribution advisories (USN, DSA, DLA). The
  newest installed kernel counts: old ones stay until autoremove and are no longer reported. The first scan takes
  about a minute, later ones reuse the saved details.
- The agent in a conversation patches with the same tool (`security_patch`): "patch the server" patches right
  away, without a question for every apt and docker command (with `AUTONOMY=0` one confirmation with a list of
  what will be patched).
- **Container images.** The same scan covers the packages in the images of running containers (Debian, Ubuntu,
  Alpine): Pipe reads their package database through the container's `/proc/<pid>/root` or from the overlay layers,
  without running anything in the container. One result per image, alert `vuln:images`. Images without a system
  (distroless, scratch) are listed as not checked. `/patch images` first keeps the previous image under a
  `pipe-rollback-*` tag (cleanup does not remove it and `/undo` goes back to it), then pulls newer registry images
  under the same tag (`pull --ignore-buildable`) and rebuilds services with `build:` on the current base image
  (`build --pull`). When `FROM` in such a service's Dockerfile pins a version and the same line has a newer one
  (`node:20.11-slim` -> `node:20.20-slim`, `postgres:15.4` -> `postgres:15.19`; only the last number moves, never the
  major version), Pipe first bumps that tag (tags from Docker Hub, the Dockerfile backed up in the journal -- `/undo`
  restores it). A floating tag (`node:20`), a digest-pinned image, a `FROM` with a variable and registries other than
  Docker Hub stay as they are. An image that has no newer version (a pinned tag, a base image not rebuilt yet) gets the fixes
  from its system's repository: a temporary container from that image installs them as root and `docker commit`
  saves the result under the same tag with its user, ENTRYPOINT and CMD (like `copa patch`). Then `up -d` of just
  those services and a check of the project's containers. Containers without compose only get a hint.
- **Remote servers.** ssh targets from `target_manage` are in the same scan: Pipe reads their package database over
  one ssh connection (os-release, dpkg/apk, reboot-required), and `/patch` installs the fixes on them with the same apt
  steps, as root or through `sudo -n` (sudo with a password will not work, the result says what to add to sudoers).
  A server without a connection is listed in the report; the `vuln:servers` alert comes about new urgent ones.
- **Application dependencies.** Pipe looks for lockfiles in DIRECTORY directories, in the project directories of
  running compose containers and in git repositories under `/root`, `/home`, `/srv`, `/opt`, `/var/www` and `/data`,
  subdirectories included (e.g. `backend/`, `frontend/`), without `node_modules`, `vendor` and build directories. It
  reads `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `requirements*.txt` (only `==`), `poetry.lock`,
  `Pipfile.lock`, `uv.lock`, `go.mod`, `Cargo.lock`, `composer.lock`, `Gemfile.lock`. The result is in `/audit` with the
  version that removes the vulnerabilities; the `vuln:apps` alert only for actively exploited and critical ones, with
  a *Patch* button.
- **Patching application dependencies** (`/patch`, only this: `/patch apps`). Pipe bumps the vulnerable packages in the
  dependency files with the language's package manager, run in a one-off container (only the app directory, no
  `docker.sock` or privileges, as the directory's owner, no package install scripts):

  | File | What Pipe does |
  |------|----------------|
  | `package-lock.json` | dependencies from `package.json`: `npm install <package>@<version> --package-lock-only` (an exact pin stays exact); transitive ones: `npm audit fix --package-lock-only`, and what stays vulnerable gets `overrides` in `package.json` (only when the lockfile holds a single major version of the package) |
  | `yarn.lock` (yarn 2+), `pnpm-lock.yaml` | `yarn up` / `pnpm update` in the lockfile only |
  | `requirements*.txt` | the new version after `==` on the same line (a file with `--hash` stays -- the hashes would not match) |
  | `poetry.lock`, `uv.lock` | `poetry update --lock`, `uv lock --upgrade-package` |
  | `go.mod`, `Cargo.lock` | `go get <module>@v<version>` + `go mod tidy`, `cargo update -p --precise` |
  | `composer.lock`, `Gemfile.lock` | `composer update --no-install --no-scripts --no-plugins`, `bundle lock --update` |

  The changed files are backed up in the journal (one entry per app), and when a compose project is built from the
  directory, Pipe runs `build` and `up -d` and checks the containers -- `/undo` restores the files and rebuilds the
  project. What the manager did not bump (a version constraint in `package.json`, `pyproject.toml`,
  `composer.json`...) shows in the result and in `/vulns`; Pipe does not try it again for a day. Left to do by hand:
  yarn 1 (it cannot change `yarn.lock` alone), `Pipfile.lock` and the Go version itself.
- **`/vulns`** shows everything in one place: the package in bold, the version and the fixed version, below
  it the vulnerabilities with their severity and a short explanation in Pipe's language -- what an attacker can do
  and when it is a risk. The model (`WORKER_MODEL`) writes the explanations from the OSV description, once per
  vulnerability, and Pipe remembers them; without a model you see the original OSV description. In `pipe web` the
  report is in cards (server, remote servers, images, applications) with one *Patch* button, and patching shows the
  steps live and then the state after the patch.

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

**Skills and plugins from other ecosystems.** `/download skill <owner/repo|URL> [filter]` and `/download plugin <...>`
(`/pobierz`) take ready sets from a git repository -- the same `SKILL.md` format as Claude Code, and plugins in the
Claude Code plugin layout (`skills/`, `.mcp.json`, marketplace). First a preview: which skills go into memory (text
only, scripts are skipped), which MCP servers are added (in an isolated container, secrets to add to `backend/.env` as
`MCP_...`) and what is skipped -- with the reason (a skill that is too long, a server that needs the plugin's files,
hooks, agents). Installation only after *Install*. The same name in several plugins gets the plugin's prefix
(`telegram-access`). In a conversation: *"download the skills from anthropics/skills"*.

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
resources, changes in the last day), the same way as problems Pipe finds itself: the findings arrive shortly after the
alert with a *Fix it* button; the same alert at most once in 6 h, 10 webhook investigations a day (separate from
`AUTO_INVESTIGATE_DAILY`).

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

> Experimental: it works and has tests, but has seen less use on real servers than the rest of Pipe.
> If something is off, please open an issue.

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

**The dashboard** (the panel's first tab) answers *"is everything working?"* without a model: a headline in plain words
(*"Everything works"* or *"Needs attention: ..."*), rings for CPU, memory and disk with the last day's trend, the *From
Pipe* inbox (messages with action buttons, new ones marked with a dot; the bell in the top bar shows how many), cards of
the workshop projects (logs, stopping, *From GitHub*) and today's model usage (tokens, cost, cache, how much Pipe
saved). The bell next to the inbox opens the notification settings. Resources, the inbox and projects fold with a click
on the section header (folded resources stay as percentages on one line), and a single message with a click on its
title. The browser remembers it; the bell in the top bar and `/news` unfold the inbox. The empty conversation screen suggests tasks in
plain words (*Is everything working?*, *Free up disk space*, *Run an app from GitHub*, *Write me a script*); the ones
marked *no AI* work instantly and cost nothing.

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

**On a phone** chat and dashboard switch with the buttons in the top bar, and the view, language and theme are in the *...* menu. On a narrow screen the diagram is laid out top to bottom (readable without zooming): one finger pans, two zoom, a double tap on empty space zooms in, the frame button goes back to the whole picture. Element details slide up from the bottom. The message field stays above the keyboard.

**The side panel** takes half the screen by default. You can drag its edge to make it narrower or wider, up to half
the screen; a double click on the edge restores the default width, and dragging it to the right edge of the window
hides the panel. A button in its tab bar hides it too; a button in the top bar hides it and brings it back. With the
panel hidden the conversation takes the whole window (the content in a readable column in the middle), and a command
or a notification that opens a tab (for example `/alerts`) brings the panel back by itself. The width and state of the
panel are kept in the browser. From the keyboard: focus the edge, the arrows change the width, Enter hides it.

**Agent steps in the conversation.** Under the answer you see what the agent did: every command (or a description,
e.g. *reads /etc/nginx/nginx.conf*) and a few lines of its output -- on an error the error lines, on success the end.
*Full output* expands up to 60 lines. While the agent works the block is open; after the answer it collapses to
*Steps: N* (unless something failed or an approval card is waiting). A change made without asking is marked
*without asking*. It is the same output the model got, with secrets hidden. The simple view has no such blocks. The
CLI shows the same as grey lines under the command, Telegram in the progress message.

**Changes.** Before you approve a file write you see the diff in the confirmation card. After it runs, the "before ->
after" diff appears in the conversation and in the *Changes* tab, also when a command changed the file (for example
`sed`), because the safety fuse takes a backup. Secrets in diffs are redacted. Every entry has an undo button. There
are no diffs for directories and data in databases.

**Commands and skills.** Typing `/` in the message box opens the list of commands (`/status`, `/news`, `/report`,
`/audit`...) and skills with descriptions; pick with the arrows or a click, Tab completes the command so you can add
arguments. `/skills` shows saved procedures in the conversation, with a content preview and a run button. The panel has
three tabs: *Dashboard*, *Map* (a dot next to the name while the agent is working) and *Changes*; the last one you
picked stays in the browser.

**LLM provider.** On the first visit the page asks which provider Pipe should work with: you pick it from the list,
paste the API key (it is checked with the provider before it is saved) and pick a model from its current list. You
can skip this screen; the provider from the installation stays. Above the message box there is a switcher showing who
answers; it opens the list of providers that have a key and switches without a restart, in the middle of a
conversation. `/provider` opens the screen again (another key, a different model, removing a key). Keys are stored on
the server in `DATA_DIR/llm_keys.json` (0600) and never go back to the browser. The choice applies to the whole agent:
after switching, Telegram, the CLI, routines and workers use the new provider too; `WORKER_MODEL` and prices from
`.env` apply only to the base provider. You can also put several keys straight into `backend/.env`
(`OPENAI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`...).

## Pipe in the browser on your phone and computer (`/web`)

`pipe web` runs on your computer through an SSH tunnel. To open Pipe on your phone or on any computer without a
tunnel, write one command in Telegram, the CLI or `pipe web`:

```
/web on
```

Pipe puts an HTTPS server in front of itself (Caddy in the `pipe-web` container) and answers with an address, e.g.
`https://203-0-113-9.sslip.io` -- a free name that points to your server's IP, so the Let's Encrypt certificate is
issued on its own, without buying a domain. With your own domain: `/web on pipe.mycompany.com` (the DNS record must
already point to the server). The port is 443, and when it is taken (e.g. by nginx) -- 8443; your own: `/web on 9443`.

**On mikr.us** Pipe recognises the server (the `/klucz_api` file) and uses mikr.us subdomains: the page listens on
IPv6 and mikr.us itself gives the address `https://<server>-7443.mikrus.cloud` with a certificate. Nothing to open or
sign up for, the address does not change, and traffic goes through nobody but mikr.us. To force it: `/web on mikrus`.

**When the provider lets no traffic in on 80/443** (other servers on a shared address, a home server
behind a router, a cloud panel with blocked ports), no certificate can be issued. Pipe checks this itself: if the
public address is not the server's own address or the certificate is not issued, it switches to a **Cloudflare
tunnel** and answers with an address like `https://quiet-river-1a2b.trycloudflare.com`. The tunnel is a `cloudflared`
container that connects to Cloudflare on its own -- no port to open and no account. The price: traffic passes through
Cloudflare (it encrypts the connection to the browser), a quick tunnel has no uptime guarantee, notifications on the
page arrive every few seconds instead of instantly, and the address changes after a server restart -- Pipe notices the
new one and sends it in a message (devices are let in once more then). To force it: `/web on tunnel` or
`/web on direct`.

Before Pipe gives you the link, it opens it from the internet itself and checks that the access-request page
answers. If not, it says why and shows the last lines from the Caddy or `cloudflared` logs.

**How you get in.** The page has no password and no secret link. You open the address, press *Ask for access* and
see a code, e.g. `K7P-4QX`. At the same moment Pipe writes to you (Telegram, `pipe web` on another device, the CLI):
*"Someone wants to open Pipe in a browser -- iPhone, Safari, IP ..., code K7P-4QX"* with *Let in*, *View only* and
*Reject* buttons (or you write `/web approve K7P-4QX`). You check that the code matches and press *Let in* -- the page
opens by itself. You let each device in once: the phone separately, the computer separately. After that it stays
signed in for 30 days after its last use.

- `/web` -- the address, the device list (which device, full access or view only, when last used) and requests
  waiting for approval.
- `/web revoke 2` or `/web revoke all` -- the device loses access at once, an open page goes back to the access
  request.
- `/web off` -- stops Caddy or the tunnel and the bridge, closes the ports Pipe opened in ufw. Devices stay in case you turn it on
  again.

A device let in with *View only* has the viewer role: it chats, reads reports and the diagram, but changes nothing.
Only an administrator can let devices in. The page on a phone is the same interface as `pipe web` -- chat and
dashboard switch with the buttons in the top bar. Added to the home screen (Safari: *Share* → *Add to Home Screen*,
Chrome: *Add to Home screen*) it shows the Pipe icon.

When ufw runs on the server, Pipe opens the page's port in it (and 80 when the page is not on 443 -- Let's Encrypt
checks the server there while issuing the certificate). Pipe cannot see the hosting provider's firewall (cloud
panel): if the page does not respond, Pipe says which port to open. Works in docker and native mode (Docker is needed
-- Caddy is a container), not in observe mode or Kubernetes (use an Ingress there). Security details:
`docs/security.md` (*Pipe on the internet*).

---

## Views: simple and technical

Pipe has two views, like one app for two jobs. **Simple** is for non-technical people: chat in plain words, reminders,
files, skills and add-ons for external apps (MCP servers: GitHub, a calendar, email). The agent picks how to do the
task itself, doesn't ask you to type commands and asks about important things with a plain yes/no question. **Technical** has
the same plus the server: the live map, the change journal with undo, vulnerabilities and patching, containers,
remote servers.

On the first run Pipe asks what you want to do. You can answer in a few words (*"reminders and emails"*, *"keep an eye
on the shop server"*) or pick a view. The view is shared by the whole agent, like the language: `/view simple`,
`/view technical` (`/widok`) in the CLI, in `pipe web` (also a button in the header) and on Telegram (buttons).
Changing it in one channel switches the others. It is saved in `DATA_DIR/view.json` and only an admin changes it.
Until someone chooses, the technical view applies, which is what Pipe showed so far.

| Where | Simple view | Technical view |
|-------|-------------|----------------|
| `pipe web` | *Dashboard* (without charts and projects) and *Add-ons* tabs, everyday suggestions | *Dashboard*, *Map*, *Changes*, *Add-ons* |
| CLI | `/help` and `/` suggestions without server commands | all commands |
| Telegram | `/` menu, `/help` and keyboard: status, news, skills, help | menu and keyboard as before |
| Agent | a *SIMPLE VIEW* block in the prompt: plain words, no jargon | unchanged |

Server commands (`/patch`, `/map`, `/audit` and others) work in both views when typed; the simple view only doesn't
suggest them. The *Add-ons* tab in `pipe web` gathers skills (run, preview), connected MCP servers and adding new ones
from GitHub (`/download`, `/mcp add`).

**First run without a server.** `pipe web` without `--host` and without a saved connection opens a wizard: first the
view, then the place. *On this computer* starts the backend in Docker from this repository (on Linux Pipe manages this
computer, on Windows and macOS it works in its own Docker Desktop container, `backend/docker-compose.desktop.yml`).
*On my server* connects over SSH with an address and a password or key, clones the repository into `/opt/pipe` and
runs `install-server.sh` without questions (without a model; you add the API key on the page afterwards). The
password stays in the `pipe web` process: it doesn't go to disk, to the backend or to the model. After a password
login Pipe adds its own key on the server (`~/.pipe/keys`) and uses it from then on. The connection is saved in
`~/.pipe/app.json`, so the next `pipe web` and plain `pipe` connect on their own. The wizard comes back with
`pipe web --setup` or by clicking the server address in the header.

---

## Language: English or Polish

`PIPE_LANG=en` (default) or `PIPE_LANG=pl` switches the whole product, not just the language of the model's answers.
You can also change the language while working, without a restart: `/language pl` / `/language en` (`/jezyk`) in the
CLI, in `pipe web` (the command or the flag button in the header) and on Telegram. The choice is shared by the whole
agent (the other channels switch by themselves), it is saved in `DATA_DIR/language.json` and holds until `PIPE_LANG`
in `.env` is changed by hand. Only an admin can change it.

| Layer | What changes |
|-------|--------------|
| Agent | system prompt, tool descriptions, prompts for workers, routines, the server scan and VIBE |
| Messages | confirmation questions, the safety-fuse plan, tool results, validation errors |
| Watching | alert titles, the morning report, `/changes`, `/health`, `/audit` with fixes, the welcome |
| MCP | titles and descriptions of the MCP server's tools, instructions for the external agent |
| Clients | CLI (`--lang pl`; without it the CLI and `pipe web` follow the server), the Telegram bot (`/` menu, buttons, labels), the wizard and the installer |
| Skills | built-in skills in the chosen language |

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

**Connecting from a laptop** -- the stdio bridge in the CLI, through the same SSH tunnel and tokens. A ready
configuration for Claude Code, Claude Desktop, Cursor, Codex, Hermes Agent and OpenClaw:
`pipe connect <app> --host root@server` (`docs/cli.md`). By hand:

```json
{"mcpServers": {"pipe": {"command": "pipe", "args": ["--mcp", "--host", "root@server"],
                         "env": {"AGENT_TOKEN": "<token>"}}}}
```

A separate token for the agent: `python3 -m backend.tokens add claude-code --role admin` (or `--role viewer`: the
agent only reads and can't ask for approvals). **On the server** -- a Streamable HTTP endpoint: `MCP_PORT=7381`,
`http://127.0.0.1:7381/mcp`, the token in `Authorization: Bearer`.

### Pipe as an MCP client

Pipe also uses other MCP servers -- e.g. email, a calendar, GitHub or Grafana -- like its own tools.

**The `/mcp` window.** `/mcp` without arguments opens a window (like `/mcp` in Claude Code): in the CLI a frame you move
through with the arrows, in `pipe web` a separate panel, on Telegram a list with buttons. It shows the connected servers
with their state and, once you pick one, what exactly runs, its tools (which ones ask first) and *Reconnect*,
*Disable*, *Remove* buttons.

**Ready add-ons** (`backend/addons/`, nothing is on by default):

| Add-on | What it gives | What it needs |
|---|---|---|
| `outlook` | mail, calendar and contacts of a Microsoft account (outlook.com, hotmail) | nothing -- after adding write *"log me in to Outlook"*: a microsoft.com/devicelogin link and a code, the login survives restarts |
| `microsoft365` | Outlook and Teams of a work or school account | the same code login; some organisations require admin consent |
| `google` | Gmail (reading, drafts), Calendar (read), Drive, Docs, Sheets | your own OAuth client in Google Cloud and a refresh token -- the window walks you through it |
| `github` | repositories, issues, pull requests, Actions | a GitHub token (fine-grained) |

Keys are entered in the `/mcp` window in `pipe web` or the CLI (a hidden field). They live in
`backend/data/mcp_secrets.json` (0600) and never reach the model or the browser; Telegram does not take keys. It also
works in a conversation: *"connect my Outlook"* -- the agent knows the catalog (`mcp_manage catalog`) and adds the
add-on after one YES. Add-ons with a login (`state: true`) get their own Docker volume for tokens; removing the add-on
removes it together with the saved login. A new add-on is one JSON file -- see `backend/addons/README.md`.

Your own server, outside the catalog:

```
/mcp add github npx -y @modelcontextprotocol/server-github GITHUB_PERSONAL_ACCESS_TOKEN=${MCP_GITHUB_TOKEN}
/mcp add fetch uvx mcp-server-fetch
/mcp add docs https://mcp.example.com/mcp TOKEN=${MCP_DOCS}
/mcp                      # list and state;  /mcp remove github;  /mcp reload
```

Or in a conversation: *"connect the GitHub MCP server"* -- then one YES with a preview of what Pipe will run.

| | |
|---|---|
| Where it runs | `npx`, `uvx` and a Docker image (including `docker run ...` from other apps' `.mcp.json`) -- always in a separate container: no server files, no `docker.sock`, no privileges (`--cap-drop ALL`, user `nobody`), memory and process limits. The server sees only the network. HTTP servers -- at the given address |
| Secrets | only as `${MCP_NAME}` entered in the `/mcp` window or in `backend/.env` (e.g. `MCP_GITHUB_TOKEN=...`) -- do not type the token in the chat; it reaches the container as a variable, never in the arguments |
| Approvals | a tool the server marks as a read -- without asking; any other -- a YES with the arguments shown |
| Model tokens | the server's tools are the group `mcp:<name>`, loaded on demand: an ordinary conversation does not send them, a message naming the server (*"on github..."*) turns them on |
| Result | outside content: YOLO is off for this turn; an argument with a secret does not go to the server |
| Observe mode, viewer | read tools only; a new server cannot be added |

Configuration: `backend/data/mcp_servers.json` (0600). A stdio server starts in ten seconds to a minute (`npx` / `uvx`
install the package in the container); Pipe connects in the background after a restart.

Supported protocol versions (both directions): 2026-07-28 (`server/discover`, the version in `_meta`,
`Mcp-Method`/`Mcp-Name` headers) and older ones with `initialize` (2025-11-25, 2025-06-18, 2025-03-26).

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
and whether a newer one exists. The agent checks the remote repository and starts a separate
helper container that runs `git pull --ff-only` in the install directory and swaps Pipe's services for the signed
release image from GHCR (or, when there is none, rebuilds them: `docker compose up -d --build`). A separate container is needed because the agent can't replace the container it
runs in; the process would die halfway.

- The backend is unavailable for a minute or a few. The conversation comes back after the restart (sessions are kept
  in `DATA_DIR/sessions`), but an operation waiting for YES at that moment is dropped and the agent proposes it again.
- After the restart, a message with the result goes to whoever requested the update. If pulling or building fails,
  the old version keeps running and the message contains the end of the log.
- Only services that were already running are rebuilt (the Telegram bot won't start if you don't use it).
- Local changes to repository files can block `git pull`; the agent warns about them when checking.
- Docker mode only. In native mode: `git pull` and `sudo systemctl restart pipe`; in Kubernetes: a new image.
