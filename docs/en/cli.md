# CLI -- Pipe

Pipe v0.47.1

An interactive terminal for managing a VPS through an AI agent.

It runs on your laptop and connects to the backend running on the server through an automatic SSH tunnel.

## How it works

```
Your laptop
    +-- cli.py
          +-- opens an SSH tunnel automatically ->
          |       ssh -L 7379:127.0.0.1:7379 user@server
          +-- talks to the agent on the server through the tunnel
                        |
              server -- backend/server.py
                        |
              /tmp/vps-agent.sock + 127.0.0.1:7379 (localhost only)
```

The CLI sets up the SSH tunnel by itself, there is nothing to do by hand.

Once connected, the CLI shows the server's state in one line right away (without the model) and the number of new
messages from Pipe. While you sit at the prompt, messages Pipe wrote by itself (alerts, findings, suggestions) appear
above it as boxes -- the same ones Pipe notifies about on Telegram (`/notifications`). You pick a message's action in
`/news` by typing the message number and the action letter, e.g. `1a`.

## Requirements

- Python 3.11+
- `ssh` available in the terminal (`which ssh`)
- An SSH account on the server
- A running backend on the server (`docker-compose up -d` in `backend/`)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Basic: give the server address
python cli.py --host root@server.example.com

# Non-standard SSH port
python cli.py --host root@1.2.3.4 --ssh-port 2222

# SSH key (if you don't have a default one in ~/.ssh/)
python cli.py --host root@1.2.3.4 --key ~/.ssh/id_server

# Environment variable instead of a flag (handy for daily use)
export VPS_HOST=root@server.example.com
python cli.py
```

## If you already have your own SSH tunnel

```bash
# Set up the tunnel in the background yourself
ssh -N -L 7379:127.0.0.1:7379 root@server.example.com &

# CLI with the automatic tunnel turned off
python cli.py --no-tunnel --local-port 7379
```

## Pipe in Kubernetes

Instead of an SSH tunnel the CLI sets up `kubectl port-forward svc/pipe` (needs `kubectl` and access to the cluster):

```bash
pipe --kube pipe                          # the namespace Pipe runs in
pipe --kube pipe --kube-context prod      # another kubeconfig context
```

## Browser interface (`pipe web`)

```bash
pipe web                  # sets up the tunnel as usual and opens http://127.0.0.1:7400/?k=...
pipe web --no-browser     # only print the address
pipe web --web-port 8080  # another port (or the PIPE_WEB_PORT variable)
pipe web --lang en
pipe web --setup          # the wizard again: where Pipe should run
```

Without `--host` and without a saved connection `pipe web` starts with a wizard: what you want to do (the simple or
technical view) and where Pipe should run -- on this computer (Docker) or on your server (address and an SSH password
or key; the page clones the repository and runs the installer itself). The password stays on this computer. The
connection goes to `~/.pipe/app.json`, and from then on `pipe web` and `pipe` without `--host` connect on their own.
Description: `docs/en/features.md` (Views).

The same process as the CLI: an SSH tunnel (or `--kube`, `--no-tunnel`), and a local page instead of the REPL. The
backend token stays in the `pipe web` process; the browser only gets a one-time key in the address. The page listens
only on `127.0.0.1` and refuses requests with a foreign `Host` or `Origin` header.

On the page: the conversation and three side-panel tabs. *Dashboard* answers without the model whether everything works
(CPU, memory and disk rings, the *From Pipe* inbox with action buttons, workshop project cards, model usage today),
*Map* shows the server live (three levels: servers, inside a server, a compose project; a *Follow the agent* button),
*Changes* -- diffs and undo. Skills are run from the `/` list. The paperclip next to the message box (or dragging a file in, or pasting
a screenshot) attaches files to the message, and the microphone records a voice message. Typing `/` in the message
box opens the list of commands and skills, the same commands as below. On the first visit the page asks for an LLM
provider (API key and model), and the switcher above the message box jumps between providers that have a key
(`/provider`). Unlike the REPL, the page receives alerts and reminders immediately. Details: `clients/webui/README.md`.

## MCP bridge (`--mcp`)

`pipe --mcp --host root@server` turns the CLI into an MCP server (stdio) for Claude Code, Cursor and other agents:
Pipe's tools through the same SSH tunnel and token. Only the MCP protocol goes to stdout; the tunnel takes its own free
port and never asks for a password (`BatchMode`, so an SSH key is required).

A ready configuration for a given app -- to paste, with where it goes:

```bash
pipe connect claude --host root@server          # the claude mcp add command
pipe connect claude-desktop --host root@server  # claude_desktop_config.json
pipe connect cursor --host root@server          # ~/.cursor/mcp.json
pipe connect codex --host root@server           # ~/.codex/config.toml
pipe connect hermes --host root@server          # mcp_servers in Hermes Agent's config.yaml
pipe connect openclaw --host root@server        # openclaw mcp add
```

The same options as the regular CLI (`--ssh-port`, `--key`, `--kube`). The token always goes to the `AGENT_TOKEN`
variable, never to the process arguments; without `--token` the token is `<TOKEN>` with a hint on creating a separate
token for that app (`python3 -m backend.tokens add hermes --role admin`). By hand, for example in Claude Code:

```json
{"mcpServers": {"pipe": {"command": "pipe", "args": ["--mcp", "--host", "root@server"],
                         "env": {"AGENT_TOKEN": "<token from python3 -m backend.tokens add claude-code>"}}}}
```

## Diagrams

The CLI saves a diagram (for example `/map` or *"draw the architecture"*) as a PNG in `~/.pipe/diagrams/`
(`PIPE_DIAGRAMS_DIR`) and shows an ASCII preview when it fits in the terminal. `--open` opens the PNG in the default
image viewer, `/mermaid` prints the source of the last diagram (for example for a README).

Worker progress (`› web-1 $ uptime`) is printed live, before the agent answers.

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `VPS_HOST` | Server address (`user@host`) | -- |
| `AGENT_TOKEN` | Backend authorization token (same as `--token`); needed only when the backend has `AGENT_TOKEN` set | -- |
| `VPS_SSH_PORT` | SSH port | `22` |
| `VPS_SSH_KEY` | Path to the private key | default key |
| `VPS_LOCAL_PORT` | Local tunnel port; when it is taken (a second CLI, an old tunnel), the CLI picks another free one | `7379` |
| `VPS_REMOTE_SOCKET` | Socket on the server | `/tmp/vps-agent.sock` |
| `PIPE_KUBE_NAMESPACE` | Same as `--kube` | -- |
| `PIPE_KUBE_CONTEXT` | Same as `--kube-context` | current context |
| `PIPE_DIAGRAMS_DIR` | Where diagrams are saved | `~/.pipe/diagrams` |
| `PIPE_LANG` | CLI language: `pl` or `en` (same as `--lang`) | `pl` |

## Example requests in the CLI

```
> how much free disk space do I have?
> show the latest nginx errors
> restart docker compose
> check whether port 80 is open
> check RAM usage by process
> show the status of the git repositories
> list the open ports
> show the server architecture
> draw how a request reaches the shop
> where is the blog repository?
> add server 10.0.0.5 as web-2 (ssh, root) and check its disk
> check for updates on all servers
> every day at 7 check the backups, only write when something is wrong
```

Type `exit` or press `Ctrl+C` to quit.

## Commands

Typing `/` at the prompt shows the list of commands and skills with descriptions; it narrows as you type (the Polish
name, the English name and aliases all match). `Tab` inserts the first item, further `Tab` / arrows pick the next ones,
the up arrow recalls earlier input. This needs `prompt_toolkit` (it is in `requirements.txt`) in the same Python / venv
that `pipe` starts from. When it is missing, the CLI prints the install command at startup and works as before,
without suggestions.

Polish command names are canonical; the English names below work in both languages (`/raport` = `/report`).
`pipe --lang en` switches the CLI messages to English for that run only; `/language en` (`/jezyk pl`) changes the
language of all of Pipe (agent instructions, reports, messages) without a restart, also in `pipe web` and on
Telegram. The choice survives a restart and the CLI adopts it on connect (unless `--lang` was given).

| Command | What it does |
|---|---|
| `/status` | Server status at once, without AI: a plain-words headline, CPU, memory and disk bars, new messages, projects |
| `/news` | The inbox: what Pipe wrote by itself, with actions -- type the number and the letter, e.g. `1a` (`/nowosci`) |
| `/projects` | Workshop projects: state and address (`/projekty`). A new project: write e.g. *"clone https://github.com/... and run it"* |
| `/web` | Pipe in the browser on your phone and computer at an HTTPS address: `/web on [port] [domain]`, `/web off`, `/web approve <code>` (lets a device in), `/web revoke <no>`; without arguments the state and devices (`docs/features.md`) |
| `/cleanup` | Frees space without AI: untagged Docker images, old build cache, downloaded packages and npm/pip caches; admins only (`/sprzataj`) |
| `/notifications [all\|important\|critical] [quiet 22-7\|quiet off] [daily 3]` | When Pipe should notify you; without arguments shows the current settings (`/powiadomienia`) |
| `/update [check]` | Update Pipe itself on the server (after confirmation); `check` only shows the versions; after the backend restarts the CLI reconnects by itself (up to 3 min), no need to start it again |
| `/report` | The morning report on demand: health, alerts, changes since yesterday, certificates, backups, updates, LLM cost + chart |
| `/changes [24h\|3d]` | What changed on the server: packages, container images, ports, cron, accounts, SSH keys, configs |
| `/chart [load\|ram\|disk] [24h\|7d]` | Chart from the watcher's history (PNG in `~/.pipe/diagrams/`) |
| `/health` | TLS certificates, site responses, DNS and backup freshness (not suggested or in `/help` -- `/status` shows problems) |
| `/audit` | Host security score 0-100 with ready fixes (write *"fix 1"*) |
| `/incidents` | Incident memory: what happened, what was found, what helped |
| `/approvals` | Operations of external agents (MCP) waiting for approval, with the safety-fuse plan |
| `/mcp [add <add-on>\|<name> <npx ...\|uvx ...\|URL> \| remove <name> \| reload]` | Without arguments a framed window (arrows, Enter, Esc): connected servers with details and ready add-ons (Google, Outlook, Microsoft 365, GitHub); keys are entered here in a hidden field. Servers run in an isolated container. Changes admin only |
| `/journal` | Approved changes with backups (what, when, whether it can be undone) |
| `/undo [id]` | Undoes the last (or the given) change: a preview of the diff and inverse commands, then a YES/NO question. Works without the LLM |
| `/vulns` | Known vulnerabilities in packages, container images, on remote servers and in applications, each with a short explanation in Pipe's language |
| `/patch [images\|server\|apps\|servers\|<target>]` | Patches right away, without another question: this server's packages, application dependencies (bumped in the dependency files, the compose project rebuilt), container images from compose (a newer image or the fixes installed inside the image) and remote servers, then cleans up old images and caches. The steps show live, at the end what was patched, what stayed unchanged and why. Works without the LLM |
| `/cost` | LLM token usage today and over the last days (with cost when prices are in `.env`) |
| `/view [simple\|technical\|description]` | Pipe's view (`/widok`), shared by the CLI, web and Telegram: simple hides server commands from suggestions and `/help`, and the agent writes in plain words. Without an argument shows the current one; a plain-words description works too. On the first connection the CLI asks for it on its own. Admin only |
| `/language [pl\|en]` | Pipe's language: without an argument shows the current one, with an argument switches the whole agent (shared by the CLI, web and Telegram; admin only) |
| `/providers [id [model]]` | LLM providers: a list, then picking a provider and a model from the list the provider returns (current one preselected, Enter keeps it; a name fragment narrows the list). The API key is typed without echo. `/providers groq` goes straight to Groq's models, `/providers groq llama` picks a model by name fragment, `/providers model` only changes the model of the current provider, `/providers forget <id>` removes a key added from the interface. The choice applies to the whole agent; changing it is admin only |
| `/file <path> [question]` | Send the agent a file from this computer (a log, a config, a screenshot; up to 10 MB). In a normal message `@path` is enough, for example `what's wrong here? @nginx.conf`; Tab completes paths |
| `/yolo [on\|off]` | YOLO mode: changes in this conversation run without asking for YES (the safety fuse, the journal and `/undo` still work, forbidden operations are still refused). Off by default, admin only, forgotten when the backend restarts; the prompt then shows a red `YOLO` |
| `/map [title]` | Infrastructure diagram (no LLM) |
| `/mermaid` | Mermaid source of the last diagram |
| `/server` | Shows SERVER.md; when there is none, the agent explores the server and creates it |
| `/server update` | The agent explores the server again and updates SERVER.md and DIRECTORY |
| `/directory` | Map of repositories and directories (DIRECTORY) |
| `/skills` | List of saved skills with their commands |
| `/download skill\|plugin <owner/repo\|URL> [filter]` | Skills and plugins from a git repository: a preview, then a YES/NO question (`/pobierz`); admin only |
| `/alerts` | Active watcher alerts (not suggested or in `/help` -- new ones show up by themselves above the prompt and in `/news`) |
| `/routines` | Tasks run on a schedule |
| `/reminders [cancel <id>]` | One-off reminders; the CLI prints them above the prompt by itself (it checks every 10 s), including the result of `/update`; those set from the CLI also go to Telegram and `pipe web` |
| `/targets` | Remote servers, containers and clusters |
| `/new` | New conversation from a clean slate; the server notes, directories, skills, style and alerts stay |
| `/vibe` | Note about your conversation style; `/vibe reset` clears it |
| `/history` | Latest audit-log entries (not suggested or in `/help` -- `/journal` shows changes) |
| `/<skill>` | Runs a skill, for example `/renew_certificate` or `/renew-certificate`, optionally with hints |
| `/help` | Command list |
| `/exit` | Quit |

Text starting with `/` that is not a command (for example `/var/log is full?`) goes to the agent as a normal message.

## Running directly on the server

Once logged in to the server over SSH you can talk to the agent without a tunnel, because the backend publishes port
`7379` on the server's `127.0.0.1`:

```bash
pip install -r clients/cli/requirements.txt   # once
python3 clients/cli/cli.py --no-tunnel        # + --token ... if you set AGENT_TOKEN
```

`install.sh` builds a shortcut with an SSH tunnel, so on the server itself it is easier to add an alias by hand, for
example `alias pipe='python3 ~/Pipe/clients/cli/cli.py --no-tunnel'`. The agent's memory (SERVER.md, skills) is shared
by all clients: what the agent saves on Telegram shows up in the terminal and the other way round. The exception is
VIBE: the style note is separate for each user (`cli:<login>`, `telegram:<id>`).
