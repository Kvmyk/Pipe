# CLI -- Pipe

Pipe v0.29.0

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
```

The same process as the CLI: an SSH tunnel (or `--kube`, `--no-tunnel`), and a local page instead of the REPL. The
backend token stays in the `pipe web` process; the browser only gets a one-time key in the address. The page listens
only on `127.0.0.1` and refuses requests with a foreign `Host` or `Origin` header.

On the page: the conversation, a live map of the server (three levels: servers, inside a server, a compose project;
a *Follow the agent* button), a timeline of actions, a *Changes* tab with diffs and undo, an *Alerts* tab with live
events, a *Skills* tab with preview and run. The paperclip next to the message box (or dragging a file in, or pasting
a screenshot) attaches files to the message, and the microphone records a voice message. Typing `/` in the message
box opens the list of commands and skills, the same commands as below. On the first visit the page asks for an LLM
provider (API key and model), and the switcher above the message box jumps between providers that have a key
(`/provider`). Unlike the REPL, the page receives alerts and reminders immediately. Details: `clients/webui/README.md`.

## MCP bridge (`--mcp`)

`pipe --mcp --host root@server` turns the CLI into an MCP server (stdio) for Claude Code, Cursor and other agents:
Pipe's tools through the same SSH tunnel and token. Only the MCP protocol goes to stdout; the tunnel takes its own free
port and never asks for a password (`BatchMode`, so an SSH key is required). Configuration, for example in Claude Code:

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
| `/status` | Server status |
| `/update [check]` | Update Pipe itself on the server (after confirmation); `check` only shows the versions; after the backend restarts the CLI reconnects by itself (up to 3 min), no need to start it again |
| `/report` | The morning report on demand: health, alerts, changes since yesterday, certificates, backups, updates, LLM cost + chart |
| `/changes [24h\|3d]` | What changed on the server: packages, container images, ports, cron, accounts, SSH keys, configs |
| `/chart [load\|ram\|disk] [24h\|7d]` | Chart from the watcher's history (PNG in `~/.pipe/diagrams/`) |
| `/health` | TLS certificates, site responses, DNS and backup freshness |
| `/audit` | Host security score 0-100 with ready fixes (write *"fix 1"*) |
| `/incidents` | Incident memory: what happened, what was found, what helped |
| `/approvals` | Operations of external agents (MCP) waiting for approval, with the safety-fuse plan |
| `/mcp` | MCP servers Pipe uses and their state |
| `/journal` | Approved changes with backups (what, when, whether it can be undone) |
| `/undo [id]` | Undoes the last (or the given) change: a preview of the diff and inverse commands, then a YES/NO question. Works without the LLM |
| `/cost` | LLM token usage today and over the last days (with cost when prices are in `.env`) |
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
| `/alerts` | Active watcher alerts (the alerts themselves arrive on Telegram) |
| `/routines` | Tasks run on a schedule |
| `/reminders [cancel <id>]` | One-off reminders; the CLI prints them above the prompt by itself (it checks every 10 s), including the result of `/update`; those set from the CLI also go to Telegram and `pipe web` |
| `/targets` | Remote servers, containers and clusters |
| `/vibe` | Note about your conversation style; `/vibe reset` clears it |
| `/history` | Latest audit-log entries |
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
