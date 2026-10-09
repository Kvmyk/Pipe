# Security -- Pipe

Pipe v0.30.1

## Model

Pipe gives a language model access to a server, so the security rules live **in code**, not in the prompt. The model
can make a mistake or be manipulated by the content of a file it reads (prompt injection); the classifier,
confirmations, secret redaction and worker limits work regardless of what the model "wants" to do.

| Layer | What it does |
|-------|--------------|
| Command classifier | `safe` / `confirm` / `forbidden`, fail-closed |
| Confirmation | shows exactly the string that will run |
| Secret redaction | tool results are cleaned of keys and passwords before they are sent to the LLM |
| Workers and routines | run only `safe`; everything else comes back as a proposal |
| Workspace | file tools see only the host and can't see SSH keys, `/etc/shadow`, `/proc`... |
| Audit log | every operation, without file contents |
| Token | `AGENT_TOKEN` required for every request, including confirmations and subscriptions |

---

## Command classification (`backend/core/security.py`)

Order:

1. **FORBIDDEN** on the whole string, which catches `curl ... | bash` and `$(rm -rf ~)`.
2. Splitting into segments on `;`, `|`, `||`, `&&`, `&` and newlines, **respecting quotes**
   (`grep 'a|b'` is one segment).
3. Each segment: forbidden -> sensitive files (confirm) -> confirm patterns -> recognised read (safe).
   Matching is **token-based**: `ss` doesn't match `ssh`, `ps` doesn't match `psql`, `id` doesn't match `idiotic-cmd`.
4. A write redirect (`>`, `>>` other than `/dev/null` and `2>&1`) or a dynamic construct (`$(...)`,
   `${...}`, `$'...'`, backtick, `<(...)`) -> **confirm**, even when the segments are reads.
5. Anything the classifier doesn't recognise -> **confirm** (fail-closed).

### SAFE -- recognised reads

Files and text (`cat`, `grep`, `ls`, `find`, `du`, `df`, `jq`...), system (`ps`, `free`, `uptime`,
`systemctl status`, `journalctl`), network (`ss`, `ip addr`, `ping`, `dig`, `curl` GET), Docker
(`ps`, `logs`, `inspect`, `stats`, `compose ps/logs/config`), Git (`status`, `log`, `diff`, `show`, listing
branches/tags), Kubernetes (`get`, `describe`, `logs`, `top`, `rollout status`), Helm (`list`, `status`, `get`).

Arguments that change state are checked separately:

| Command | Requires confirmation when... |
|---------|-------------------------------|
| `git` | the subcommand isn't a read, a flag runs a program (`-c core.pager=`, `--upload-pack`, `--receive-pack`, `--exec`, `-O`/`--open-files-in-pager`) or writes a file (`-o`/`--output`), `branch -D`, `branch new`, `tag v1`, `remote add`, `config` without `--get/--list` |
| `find` | `-exec`, `-execdir`, `-ok`, `-delete`, `-fprint` |
| `curl` | `-o/-O`, `-T`, `-d/--data`, `-F`, `--json`, `-D`, `--trace*`, `-X`/`--request` (also `--request=`, `-X<METHOD>`) other than GET/HEAD, **or a host other than localhost/private** (exfiltration), including proxies (`-x`, `--proxy`, `--socks5`), `--resolve`/`--connect-to` and an address written as a single number (`http://134744072/`) |
| `sort` | `-o`/`--output`, `--compress-program` (runs a program) |
| `journalctl` | `--vacuum-*`, `--rotate`, `--flush` |
| `kubectl get secret` | `-o yaml/json/jsonpath/go-template/custom-columns` (also `-o=...`) or `--template`, also when secrets are in a resource list (`get cm,secret`), because the secret's content would reach the LLM |

Reading these paths also requires approval, because it would send secrets to the provider:
`/etc/shadow`, SSH keys, `*.pem`/`*.key`, `.env` (also relative `cat .env`), **`/proc/*/environ` and `/proc/*/root`**
(any PID, `self` and globs; with `pid: host` these are the environment and home directories of host processes, a way
around the read-only `/hostfs`), and globs in secret directories (`cat /etc/s*adow`, `cat /root/.ssh/id_*`).
Patterns are also checked after removing quotes and backslashes, so `cat /etc/sha""dow` won't get through.
The same files read with `read_file` also require approval.

### CONFIRM -- state changes

`rm`, `mv`, `cp`, `chmod`, `chown`, `kill`, `systemctl start/stop/restart/enable...`, `apt install/upgrade/remove`,
`docker run/exec/start/stop/restart/rm/compose up/down`, `git push/commit/pull/reset...`, `kubectl apply/delete/scale/exec...`,
`helm install/upgrade/uninstall`, `ufw`, `iptables` (except listing), `reboot`, `crontab` (except `-l`), and every
unknown command. Reading files with secrets (`/etc/shadow`, SSH keys, `*.pem`, `*.key`, `.env`) also requires approval.

### FORBIDDEN -- always refused

`rm -rf /` (and variants: `-fr`, `/*`, `~`, `"/"`, `/hostfs`), `rm --no-preserve-root`, `dd if=`, `mkfs`,
`wipefs`, `shred`, writing to `/etc/passwd`, `/etc/shadow`, `/boot/`, `/dev/sd*`, fork bomb,
`curl|wget ... | sh`, `base64 ... | sh`, `python -c ...exec`, `kubectl delete ns kube-system`.
A refusal doesn't go back to the model as an error to retry; it gets a message telling it not to try to get around it.

Regression tests for bypasses found during the rebuild: `backend/tests/test_security_hardening.py`,
`backend/tests/test_review_fixes_v091.py`.

Besides the hand-written cases the classifier is fuzzed (`backend/tests/test_security_fuzz.py`, hypothesis): a
recognised read plus an appended command (`;`, `&&`, `|`, `&`, a newline, `$(...)`, backticks, `<(...)`, `${IFS}`,
`$'\x3b'`), a redirect to a file, a command name split by quotes or a backslash, case and whitespace changes, reading
a secret with a quoted path. There is one property: none of it may be `safe`, and no text may crash the classifier.
CI runs 300 examples per test; deeper locally: `PIPE_FUZZ_EXAMPLES=5000 pytest backend/tests/test_security_fuzz.py`.
Counterexamples go to the `KNOWN_BYPASSES` list as permanent regressions.

---

## Secret redaction

Every tool result (commands, files, worker reports) goes through `memory.redact_secrets()` before it is sent to the
LLM provider:

- private keys (the whole PEM block), AWS keys, `sk-...`, GitHub, Slack, Google and Telegram bot tokens, **JWT**,
- `NAME_WITH_PASSWORD/SECRET/TOKEN/API_KEY=value`: the key stays, the value disappears
  (`DB_PASSWORD=[ZREDAGOWANO: sekret]`), so the agent knows the variable exists. Matching doesn't cross NUL
  characters, so it doesn't swallow the whole `/proc/<pid>/environ`,
- **credentials in URLs** (`postgres://user:password@host` -> `postgres://user:[ZREDAGOWANO]@host`), including
  `git remote -v` and `docker inspect`,
- **password hashes from `/etc/shadow`** (`$6$...`).

The agent can't write content containing the `[ZREDAGOWANO` marker with `write_file`; otherwise rewriting an `.env`
file would destroy the real values. It has to change such a file in place (for example `sed -i`, with confirmation).
Turning it off: `REDACT_SECRETS=0` (not recommended).

Results longer than 24,000 characters are truncated (beginning + end), and the session history is trimmed at a user
message boundary, so a tool call / result pair is never split.

---

## Confirmations: what you see is what runs

- The command in a confirmation (`as_code()`) has **control and bidi characters neutralised**: ANSI sequences, `\r`
  and text direction overrides (U+202E) are replaced with a visible `\xNN`, so they can't hide a difference between
  what you see and what runs.
- `write_file` shows a **diff against the current content** (or a preview of a new file), and for files that run
  automatically (`.bashrc`, `authorized_keys`, cron, systemd, `.gitconfig`) adds a warning about persistent access.
- A confirmation is bound to the `tool_call_id` in the session; a new message instead of YES cancels the operation.
- The safety-fuse plan (backups, checks, verification) is part of the confirmation and is stored with it, so after YES
  exactly the plan you were shown is executed. Check commands (`nginx -t`, `sshd -t`, `docker inspect`...) are built
  from templates in code, with parameters through `shlex.quote`.

## YOLO mode (`/yolo`)

By default every change waits for YES. `/yolo on` (CLI, `pipe web`, Telegram) turns that question off, **only in this
conversation (session)**, only for an admin and only until the backend restarts or `/yolo off`. What doesn't change:

- classification: `forbidden` operations are still refused, and `safe` ones work as always;
- the safety fuse: every change goes through the same plan as after YES (file backups, check before, verification
  after, automatic restore of configs) and goes into the journal, so `/undo` works;
- workers still only read (proposed changes are not run), and external MCP agents' approvals still wait for a decision;
- the viewer role can't turn YOLO on, and even a set flag changes nothing for a viewer's session.

Running without asking is visible: the client gets a `progress` "YOLO — running without asking: ...", `pipe web`
shows a red badge above the input box, the CLI shows `YOLO` in the prompt, and the audit log and journal record the
interface with a `[yolo]` suffix (for example `cli:kuba [yolo]`). The model gets a block about YOLO mode in its prompt
(caution, the smallest change, no irreversible operations without an explicit request). YOLO takes the human out of
the loop, so turn it on for a specific piece of work.

## Observe mode (`PIPE_OBSERVE=1`)

To start with, before you trust the agent, you can install it so that it changes nothing:
`sudo bash scripts/install-server.sh --profile observe`. Pipe still watches the server, sends alerts and reports,
answers questions, draws diagrams and works as a read-only MCP gateway. Changes are refused on two levels.

In the application (`runtime.observe()`, native mode included):

- a state-changing command, a file write, `journal undo`, `cron_manage add/remove`, `mcp_manage` and
  `pipe_update apply` are refused without asking for YES, in YOLO mode too; the model gets an observe-mode block
  in its prompt,
- `run_command` in the MCP gateway refuses instead of creating an approval, and an existing approval can't be approved,
- `undo` with `execute` (the button in `pipe web`, `/undo` in the CLI and Telegram) returns an error,
- Pipe's registries and memory keep working, with confirmation as usual: routines (they read), targets, SERVER.md,
  skills.

In the container (`backend/docker-compose.observe.yml`, docker mode only):

- the agent has no `docker.sock`; it sees Docker through `tecnativa/docker-socket-proxy` with a read-only API
  (`ps`, `inspect`, `logs`, `info` work; `run`, `exec`, `restart`, `stop` get 403). The proxy sits on a separate
  internal network with no outside access,
- the host's `/root` is read-only (no rw overlay),
- the container has no kernel capabilities except `DAC_READ_SEARCH` (reading files) and `NET_RAW` (ping) and runs
  with `no-new-privileges`; `/proc/*/environ` of host processes is then unreadable.

`backend/data` (Pipe's memory) and the host's `/tmp` (the socket for the Telegram bot) stay writable. In native mode
only the application-level protection applies: the process runs as root. Full mode comes back with
`sudo bash scripts/install-server.sh --profile full`.

## Internet (`web_search`, `web_fetch`, `software_info`)

The agent decides by itself when to go to the web, so the internet is treated as an untrusted source:

- **Nothing confidential goes out**: a query, an address and `software_info` arguments containing a secret
  (`memory.find_secret()`) are refused, and the prompt says to write queries generally (the error text, the software
  and version, without domains, IPs or user names). Every query goes into the audit log.
- **An address is a leak channel too** (`https://foreign.example/?d=<data>`): `web_fetch` reads without asking only
  addresses from this session's `web_search` results (`Session.web_urls`) or typed by the user (messages built by the
  backend, such as webhook alerts, don't count). Any other address needs confirmation.
- **Public internet only** (SSRF): the host must point only at public addresses (`ipaddress.is_global`); loopback,
  private networks, link-local (cloud metadata `169.254.169.254`), CGNAT and IPv4-in-IPv6 addresses are refused, on
  every redirect separately. Limitation: the DNS check and the connection are two steps (DNS rebinding is
  theoretically possible); the agent checks local services with `network_info`.
- **Prompt injection**: results are marked as data, not instructions. A page with a question is read by a separate
  model request **without tools** (`WEB_READER_PROMPT`), and only the answer reaches the agent. After reading anything
  from the internet (`Session.web_tainted`), YOLO mode is paused until the next user message: a change proposed under
  the influence of a page always waits for YES.
- Keyless search engines are unofficial endpoints (DuckDuckGo) and public APIs (Stack Exchange, Wikipedia,
  endoflife.date, OSV.dev); `WEB_SEARCH=off` takes the internet away from the agent completely.

## Attachments (files, screenshots, voice messages)

A file sent by the user is foreign text too: a log can contain a line written by an attacker:

- the file content is described in the message as DATA, not instructions, and after a message with an attachment YOLO
  mode is paused until the next message (`Session.web_tainted`), so a change proposed under the influence of the file
  waits for YES,
- text files go through `memory.redact_secrets()` before they are sent to the model (like tool results),
- text from an attachment doesn't teach VIBE and doesn't count as an address *typed by the user* for `web_fetch`
  (`pipe_text` holds only what the user wrote),
- an image is in the history only for its own turn; binary files are kept in `DATA_DIR/uploads/` with 0600
  permissions for 7 days, and the write goes into the audit log; a viewer can't save a file on the server (images and
  text, yes),
- the file kind comes from the content (signature, UTF-8), not from the name or the client's MIME type; the name is
  reduced to safe characters (no directories),
- limits: 10 MB per file, 20 MB per message; the server's `READ_LIMIT` and the `pipe web` bridge's `MAX_BODY`
  (32 MiB) follow from them.

## Roles and tokens

- `AGENT_TOKEN` -- admin, `AGENT_VIEWER_TOKEN` -- viewer, client tokens from `python3 -m backend.tokens`
  (`tokens.json`, 0600, SHA-256 hashes only; revoking: `revoke`). Comparison in constant time, on bytes.
- Viewer: the backend refuses `confirm: true` and `undo` with `execute`, and tools that change state (including writing
  Pipe's memory) return a refusal to the model, so no YES question is ever created. The backend enforces this, not the
  client.
- A session is bound to the identity of the token that created it: another token can't read its history or approve
  someone else's operation, even knowing the `session_id`.

## MCP

**Pipe as an MCP server.** An external agent gets Pipe's tools instead of a shell: every command goes through the
classifier, a read runs right away, a change waits for admin approval (Telegram/CLI) and then goes through the safety
fuse and the journal. Forbidden commands are refused, files with secrets are not handed out, results are redacted. A
viewer token doesn't create approvals. Only the agent that created an approval can check it; approvals live in memory
and expire after 30 minutes. `ask_pipe` runs the Pipe agent in the viewer role. HTTP endpoint: localhost only, a Pipe
token (`Authorization: Bearer`), `Origin` validation (403) and 2026-07-28 header validation (`-32020`).

**Pipe as an MCP client.** An MCP server is someone else's code: adding a server always requires confirmation (you see
the program or URL; `env`/`headers` values are hidden), `mcp.json` has 0600 permissions, and a stdio subprocess gets a
minimal environment (PATH, HOME, LANG... and what was given), never the LLM key or Pipe's tokens. Every tool call needs
a YES with the arguments visible, unless the user marked it as safe (`autoApprove`, `trustReadOnly`); the server's
annotations alone are not enough. MCP tool results are data for the model, they go through secret redaction, and a
viewer can't approve any call.

## Webhooks

- The webhook server is off by default; without `WEBHOOK_TOKEN` it doesn't start. It listens on `127.0.0.1` (in
  Docker the port is published only on localhost); senders outside the server go through a reverse proxy with TLS.
- The alert content comes from outside: it reaches the model as data, and the automatic investigation is done by a
  worker that only reads. Investigation limit: once an hour per alert, 10 a day.

## Change journal and undo

- Backups of files from before a change are kept in `backend/data/journal/` (directories `0700`, files `0600`). An
  edited `.env` is stored there in full; it is a copy on the server's disk, not sent to the LLM. Limit: 50 entries,
  14 days, 5 MB per file.
- `/undo` shows the diff and the inverse commands before YES. Inverse commands go through the classifier: a forbidden
  command is skipped and logged. The entry identifier is validated (hex only), so it can't point at a path outside the
  journal directory. Restores and inverse commands go into the audit log.
- `/undo` from a client works without the LLM; like other commands it requires the token (`AGENT_TOKEN`), and on
  Telegram the *Undo* button works only for the allowed user who opened the preview.

## Workers, targets and routines

- **Workers and routines run only `safe`.** A `confirm` command is not run; it goes into the report as a proposal
  that the main agent can run through `remote_exec`, with your confirmation. `forbidden` is logged as always.
- **Adding a target or a routine requires confirmation**, so a manipulated model can't quietly add an attacker's
  server. A routine's confirmation shows the whole task text.
- Target fields are validated with patterns, and the command goes as one argument (`shlex.quote`). On a cluster
  target one kubectl/helm command is allowed (further `| grep` segments run locally), because a second kubectl command
  would go to the default context.
- SSH in `BatchMode` (no passwords), `StrictHostKeyChecking=accept-new`, `known_hosts` in `backend/data`.

## Watching

The checks are deterministic and only read. The *Investigate* button sends the agent a message built by the backend
(not by the client), and the usual rules apply from there. The event subscription requires the token, and the bot
sends alerts only to users in `TELEGRAM_ALLOWED_USER_IDS`.

---

## Workspace (file tools)

`read_file`, `write_file`, `change_directory` resolve the path (symlinks, `..`) and refuse the paths below. In Docker
a host symlink (`/etc/nginx/sites-enabled/x -> /etc/nginx/sites-available/x`) is resolved relative to the host root
(`/hostfs`), and `..` doesn't go above that root. Refused:
everything outside the host (`/hostfs` in Docker), `/boot`, `/dev`, `/proc`, `/sys`, `/var/spool`, binary
directories, `/root/.ssh`, `/home/*/.ssh`, `/etc/shadow`, `/etc/gshadow`, the SSH host private keys. Writing to
`/etc/passwd`, `/etc/shadow`, `/boot`, `/dev` is forbidden; every other write requires confirmation.

## Agent memory

Memory goes into the prompt of EVERY future conversation (SERVER.md, DIRECTORY, VIBE) or is loaded on demand
(skills), so one successful prompt injection could plant a persistent instruction there. Defences:

- **Saving/replacing a skill and fully overwriting SERVER.md require confirmation** (with a content preview/diff), so
  the strongest persistent injection vectors don't get through without your YES.
- **Every other memory write is reported with a visible `[PAMIEC]` message** (Telegram and CLI), so memory can't be
  changed quietly, even when the model tries to hide it in its answer.
- every write is in the audit log (the path, without content),
- obvious secrets are refused, and credentials are stripped from repository remote URLs,
- in the prompt the content is marked as **data, not instructions**; it doesn't change classification or
  confirmations, and VIBE is only style guidance.

Still, review `backend/data/` from time to time (`/server`, `/directory`, `/vibe`, `/skills`); the "data, not
instructions" framing is not a guarantee.

---

## Limitations you need to know about

- **`docker.sock` = root on the host.** Whoever can run `docker run -v /:/x` has full access to the host; mounting `/`
  read-only changes little here. That's why `docker run/exec` always requires confirmation, but the final safeguard is
  your YES. If you don't need container management, install Pipe with `--profile observe` (see above) or remove the
  `docker.sock` mount from `docker-compose.yml`.
- Conversations are kept on disk in `DATA_DIR/sessions` (mode 0600, directory 0700) for `SESSION_KEEP_DAYS` days so
  they survive a restart. Tool results in them are redacted the same way as in requests to the model, but what you
  type yourself stays. `SESSION_KEEP_DAYS=0` turns saving off and removes saved sessions at startup.
- The classifier is not a sandbox. It recognises patterns; the confirmation is there so you see what will run. Read
  commands before YES.
- Native mode and the Kubernetes `host-agent` overlay run as root on the machine.
- The Kubernetes `operator` overlay allows deleting pods and scaling workloads (with confirmation).

## Audit log

```
[2026-09-24 14:23:11] [cli:kuba] [SAFE] df -h /hostfs → exit_code=0
[2026-09-24 14:25:03] [telegram:123456789] [CONFIRMED] docker restart shop-web → exit_code=0
[2026-09-24 14:26:44] [telegram:123456789] [BLOCKED] execute_command(rm -rf /) → FORBIDDEN
[2026-09-24 14:30:02] [worker:web-1@telegram:123456789] [SAFE] ssh ... web-1 -- 'uptime' → exit_code=0
```

File contents (read_file, write_file, memory) never go into the log. Newlines in a command are escaped (`\n`), so a
command with `\n` can't forge further entries. The token is compared in constant time (`hmac.compare_digest`).

## Recommendations

1. **`AGENT_TOKEN`** -- the wizard and `scripts/install-server.sh` generate it automatically; in kubernetes mode it is
   required (the backend won't start without it). You set it by hand only to change it.
2. Don't publish port 7379 on a public interface; access goes through an SSH tunnel or `kubectl port-forward`.
   The Kubernetes overlay adds a `NetworkPolicy` (deny from other pods) next to the token.
3. Keep `TELEGRAM_ALLOWED_USER_IDS` to a minimum.
4. For workers on other servers use a dedicated SSH key and a user with limited permissions.
5. Read commands before confirming and review the audit log (`/history`) and memory (`/skills`, `/server`).

## LLM provider keys added from the interface

`pipe web` lets you add an API key for another provider. The key goes from the browser to the local bridge
(`127.0.0.1`), through the SSH tunnel to the backend, and stays there in `DATA_DIR/llm_keys.json` (0600). It doesn't go
back to the client (the provider list only says whether a key exists and where it came from), it doesn't go into the
audit log (only the provider change is logged) and not to the model. Before it is saved, the backend checks it with
the provider, only at the address from the preset or the providers file, so the key can't be sent to an arbitrary
address. Only an admin can make changes. The agent reading `llm_keys.json` is classified like `.env` (requires
confirmation), and secret redaction covers tool results as usual.

## Updating Pipe itself (`pipe_update`)

This tool pulls code from the network and runs it with the agent's permissions, so it is deliberately narrow:

- it takes no address, branch, command or path; the model can only choose `check` or `apply`;
- the only source is the `origin` of the repository Pipe is installed from, through `git pull --ff-only`
  (no rewriting history and no merging);
- `apply` always requires confirmation, and the viewer role is refused upfront;
- the compose project, directory and services come from the Docker labels of its own container and are quoted one by
  one;
- one update runs at a time; the helper container gets `docker.sock` and the repository directory, nothing more.

Trust moves to the remote repository: whoever can push a commit to the tracked branch will run code on the server
after your "YES". It is the same boundary as with a manual `git pull`, but worth keeping in mind.
