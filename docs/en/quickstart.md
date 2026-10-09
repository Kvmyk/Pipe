# Quick start -- step by step

Pipe v0.28.0

## Prerequisites

1. **On the server** -- the agent backend running
2. **On your laptop** -- Python 3.11+ and SSH access

---

## Step 1: Start the backend on the server

Log in to the server over SSH and run:

```bash
git clone https://github.com/Kvmyk/pipe
cd pipe
sudo bash scripts/install-server.sh --lang en  # or --mode native (without Docker)
```

The wizard asks for the LLM provider and API key, fetches the current list of models, checks that the chosen model
supports tool calling, and writes `backend/.env`. No key? Pick **Google Gemini** -- you can create a free key at
https://aistudio.google.com/apikey (on the free tier Google may learn from your prompts, so on production enable
billing). You can also pick **No model**: Pipe watches the server and sends alerts and reports, and nothing leaves the
server; you turn chat on later ([backend.md](backend.md#no-model-llm_providernone)).

The script installs Docker (if it is missing) and starts the backend. The same by hand:
`python3 -m backend.configure && cd backend && docker compose up -d --build`.

Check that it works:
```bash
cd backend && docker compose logs vps-agent
# Should show: [VPS Agent] Server ready.
```

You can create a new cloud server with Pipe on it right away -- [cloud-init](../../deploy/cloud-init/user-data.yaml).
Kubernetes: [deploy/kubernetes](../../deploy/kubernetes/README.md).

---

## Step 2: Install the CLI on your laptop

```bash
git clone https://github.com/Kvmyk/pipe
cd pipe
```

### Windows (PowerShell)

```powershell
.\install.ps1
# The script asks for the server address, e.g. root@server.example.com
```

### Linux / macOS (bash/zsh)

```bash
bash install.sh
# The script asks for the server address, e.g. root@server.example.com
```

The script:
- installs the Python dependencies (`rich`)
- adds a `pipe` function to your terminal profile
- from then on, typing `pipe` in any terminal is enough

---

## Step 3: Connect to the agent

```
pipe --lang en
```

The CLI automatically:
1. Sets up the SSH tunnel (asks for a password if needed)
2. Connects to the agent on the server
3. Waits for your requests

To begin: `/map` (a diagram of what runs on the server) and `/server` (the agent explores the server and writes down
what it learned in SERVER.md and DIRECTORY).

---

## Example session

```
 pipe   Autonomous AI agent for managing a Linux server

 server    root@server.example.com
 commands  /status  /report  /changes  /map  /server  /skills  /help  /exit

Connected to the agent on root@server.example.com
──────────────────────────────────────────────────────────────────────

› how much free disk space do I have?

The disk is 79% full -- about 4.2 GB left.
The largest directories are /var/log (1.1 GB) and /home (2.3 GB).
Do you want to clean up the logs? (`journalctl --vacuum-time=7d`)

› show nginx errors from the last hour

› restart nginx

┌─  Requires confirmation  ────────────────────────────────────────────┐
│                                                                      │
│  The operation requires confirmation:                                │
│                                                                      │
│    $ systemctl restart nginx                                         │
│                                                                      │
│  Safety fuse                                                         │
│   ✓  check before                   nginx -t                         │
│      (failure = I won't run it)                                      │
│   ✓  verify after                   nginx service active             │
│                                     sites that work now must         │
│                                     still work after the change      │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘

Do you want to run this operation? [y/n] (n): y
✓ Operation approved -- running...
nginx restarted, verification passed.

› exit
Goodbye!
```

The command in the panel is exactly what will run. Below it is the safety-fuse plan: what Pipe checks before and after
the change (details: [Changes with a safety fuse](features.md#changes-with-a-safety-fuse-and-undo)).

---

## `pipe` shortcut options

You can also pass extra options:

```bash
pipe --ssh-port 2222       # non-standard SSH port
pipe --key ~/.ssh/id_rsa   # a specific SSH key
pipe --session my-session  # keep the session history
pipe --lang en             # English CLI messages
```

---

## Problems

| Problem | Solution |
|---------|----------|
| The agent doesn't answer / model error | `python3 -m backend.configure --check` checks the key, the model and tool calling |
| "Model ... is not on the list" in the logs | The model was retired, pick a new one: `python3 -m backend.configure` |
| The SSH tunnel doesn't respond | Check that the backend runs: `docker logs vps-agent` on the server |
| No 'ssh' command | Windows: install OpenSSH Client (Settings -> Apps -> Optional features) |
| SSH asks for a password every time | Add an SSH key: `ssh-copy-id user@server` |
