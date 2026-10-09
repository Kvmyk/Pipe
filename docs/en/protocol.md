# Communication protocol -- Pipe

Pipe v0.30.0

## Overview

The backend listens on both at once:
1. A Unix socket (`/tmp/vps-agent.sock`) -- for clients on the same server (the Telegram bot)
2. TCP `127.0.0.1:7379` -- for an SSH tunnel from a laptop (CLI)

Communication uses a simple JSON lines protocol (one JSON line = one message).
In Kubernetes mode `kubectl port-forward svc/pipe 7379:7379` gives access to the TCP port.

## Message format

### Request -- user message

```json
{
  "message": "message text",
  "session_id": "uuid-per-user",
  "interface": "cli|telegram:user_id|discord:user_id",
  "token": "secret-token",
  "attachments": [{"name": "screenshot.png", "mime": "image/png", "data": "<base64>"}]
}
```

`attachments` is optional (files, photos, screenshots); with attachments `message` can be empty. Limits: 10 MB per
file, 20 MB and 10 files per message; exceeding them or bad base64 ends with an error frame without calling the agent.
The backend determines the kind from the content: an image goes to the model as `image_url`, text is pasted into the
message (after secret redaction), other files are saved in `DATA_DIR/uploads/` and the agent gets the path. A request
line can be up to 32 MiB (the server's `READ_LIMIT`).

### Request -- confirming an operation

```json
{
  "confirm": true,
  "session_id": "uuid-per-user",
  "token": "secret-token"
}
```

### Request -- client command

The clients' "/" commands (Telegram, CLI) send `{"command": ...}`:

```json
{
  "command": "run_skill",
  "session_id": "uuid-per-user",
  "interface": "cli:kuba",
  "name": "renew_certificate",
  "args": "only for example.com",
  "token": "secret-token"
}
```

| `command` | Extra fields | Response |
|---|---|---|
| `list_skills` | -- | one line with `"data": {"skills": [{"name", "description", "command"}]}` |
| `server_md` | -- | `"data": {"content": "..."}` (empty when SERVER.md doesn't exist) |
| `scan_server` | -- | streaming: the agent explores the server, creates/updates SERVER.md and DIRECTORY |
| `status` | -- | streaming: a summary of the server's state |
| `run_skill` | `name` (skill name or command), optionally `args` | streaming |
| `diagram` | optionally `args` (title) | infrastructure map **without the LLM**: a frame with `attachment`, then `done` with `"data": {"summary": "..."}` |
| `directory` | -- | `"data": {"entries": [{"path", "kind", "description", "remote", "branch", ...}], "text": "..."}` |
| `vibe` | `args`: empty or `reset` | `"data": {"content": "..."}` or `{"content": "", "reset": true\|false}` (the VIBE of the user from `interface`) |
| `alerts` | -- | `"data": {"active": [alert], "recent": [event], "enabled": bool}` |
| `targets` | -- | `"data": {"targets": ["target description", ...]}` |
| `routines` | -- | `"data": {"routines": ["routine description", ...]}` |
| `skill` | `name` (name or command) | `"data": {"name", "description", "content", "command"}` -- the skill's content for preview |
| `graph` | -- | `"data": {"root", "hostname", "views": {"fleet" \| "host" \| "p:<project>": {"title", "parent", "nodes": [{id, kind, label, sub, state, opens?, meta, alerts?}], "edges": [{from, to, kind, label?}]}}, "index": {id: [node on each level]}}` -- the map as data (web interface) |
| `journal_changes` | `id` | `"data": {"id", "command", "status", "undoable", "files": [{path, status: added\|deleted\|modified\|unchanged\|unknown, diff, note}], "inverse", "notes"}` -- the "before -> now" diff from the journal backup, secrets redacted |
| `providers` | -- | `"data": {"providers": [{id, name, model, default_model, requires_key, has_key, key_source: web\|env\|"", ready, key_url, notes, base, active}], "active": {id, name, model}, "chosen": bool, "can_edit": bool}` -- LLM providers without keys; `ready` = has a key |
| `provider_models` | `name` (provider id), `key` (optional -- a new key to check) | `"data": {"models": [...], "total"}` -- the provider's current chat models; a rejected key = `error`. Admin only |
| `provider_set` | `name`, `key` (optional), `model` (optional) | like `providers` -- saves the key (after checking it with the provider) and the model, switches the agent, sets `chosen`. Admin only |
| `provider_forget` | `name` | like `providers` -- removes a key added from the interface; a provider without a key stops being active. Admin only |
| `yolo` | `args`: `on` \| `off` (optional; also `wlacz`/`wylacz`, `tak`/`nie`) | `"data": {"yolo": bool, "can_edit": bool, "text"}` -- without `args` the state, with `args` turns YOLO mode **of this session** on/off: operations that need confirmation run right away (through the safety fuse and the journal; instead of `[POTWIERDZ]` the stream carries a `progress` event "YOLO — ..."). Admin only; doesn't survive a backend restart |
| `language` | `args`: `pl` \| `en` (optional; also `polski`, `english`, `angielski`) | `"data": {"lang", "chosen": bool, "languages": ["pl", "en"], "can_edit": bool, "text"}` -- without `args` the state, with `args` switches the language of the whole agent (prompts, tool descriptions, reports, messages) without a restart and saves the choice in `DATA_DIR/language.json`; subscribers get a `language` event. Admin only |
| `reminders` | `cancel`: id (optional), `claim`: true (optional) | `"data": {"text", "reminders": [{id, due, kind, text, fired}], "claimed": [reminder events], "cancelled"}` -- `claim` picks up reminders that fired while nobody was subscribed (CLI) |
| `history` | -- | `"data": {"entries": ["audit log line", ...]}` (the last 15) |
| `update` | `args`: empty or `sprawdz` / `check` | streaming: the agent calls the `pipe_update` tool (`apply` with confirmation or `check`) |
| `investigate` | alert `id` | streaming: the agent investigates a watcher alert; the backend adds the server changes from the last day |
| `changes` | `args`: period (`24h`, `3d`) | `"data": {"text": "...", "hours": 24}` -- what changed (snapshots), **without the LLM** |
| `chart` | `args`: `load\|ram\|dysk` and a period (`ram 7d`) | a frame with `attachment` (PNG), then `"data": {"summary", "metric", "image": bool}` |
| `health` | -- | `"data": {"text": "..."}` -- certificates, sites, DNS, backups (checks without configuration) |
| `digest` | -- | a frame with `attachment` (24 h load chart), then `"data"` like the `digest` event (below) |
| `mcp` | `rpc`: one MCP JSON-RPC message | `"data": {"rpc": response \| null}` -- MCP bridge (CLI `--mcp`); identity and role from the token |
| `mcp_servers` | -- | `"data": {"servers": ["MCP server description", ...]}` |
| `approvals` | -- | `"data": {"pending": [approval event]}` |
| `approve` | `id`, `decision` (bool) | admin only; approving runs the operation through the safety fuse, `"data": {"id", "status", "text"}` |
| `incidents` | -- | `"data": {"text", "incidents": [...]}` -- incident memory (what happened, findings, what helped) |
| `transcribe` | `audio` (base64, max. 10 MB; OGG, WAV, MP3...), `filename` | `"data": {"text": "..."}` -- voice message transcription (STT) |
| `audit` | -- | `"data": {"score", "grade", "findings": [{"id", "severity", "title", "detail", "fix", "command", "host_only"}], "passed", "unknown", "text"}` -- **without the LLM** |
| `welcome` | -- | a frame with `attachment` (map), then `"data"` like the `welcome` event |
| `journal` | -- | `"data": {"entries": [{"id", "summary", "undoable", "status"}], "text": "..."}` -- change journal |
| `undo` | optionally `id`; `execute: true` | without `execute`: `"data": {"id", "undoable", "preview"}` (the last entry to undo when there is no `id`); with `execute` and `id`: the undo, `"data": {"id", "text"}`. **Without the LLM** |
| `usage` | -- | `"data": {"today", "history", "month", "text"}` -- LLM tokens and cost |
| `subscribe` | -- | the connection stays open; watcher events (below) until the client disconnects |

The backend builds the message for the agent (`scan_server`, `status`, `run_skill`, `investigate`); the client sends
only the command. Messages built by the backend don't teach VIBE.
A skill's `command` field is a name that follows Telegram's rules (lowercase letters, digits, `_`, up to 32
characters). It is empty when the skill name is reserved (`status`, `mapa`, `server`, `skille`, `katalogi`, `vibe`,
`alerty`, `cele`, `rutyny`, `zmiany`, `wykres`, `zdrowie`, `raport`, `koszt`, `cofnij`, `dziennik`, `audyt`, ...) or
collides with another skill after shortening.

### Response

```json
{
  "response": "response text",
  "status": "ok|confirm|error",
  "done": false
}
```

The server sends many JSON lines (streaming). The last line has `"done": true`.
A frame can additionally contain (then `response` is empty, so older clients skip it):

**Attachment** -- a file for the user, for example a diagram:

```json
{"response": "", "status": "ok", "done": false, "attachment": {
  "name": "infrastructure-map.png", "mime": "image/png", "caption": "Infrastructure map",
  "data": "<base64>", "source": "<Mermaid source>", "text": "<ASCII preview or empty>"}}
```

**Progress** -- an intermediate status (for example what a worker is doing), to show live; it isn't part of the
answer:

```json
{"response": "", "status": "ok", "done": false, "event": {"type": "progress", "text": "web-1 $ uptime", "source": "worker:web-1"}}
```

Frames with diagrams are hundreds of KB, so the client must read lines with a larger limit than asyncio's default
64 KiB (`open_connection(..., limit=32 * 1024 * 1024)`).

### Watcher events (`subscribe`)

After `{"command": "subscribe"}` the server sends `{"event": {"type": "subscribed", "lang": "pl|en", "lang_chosen": bool}}`
(`lang_chosen` -- the language was chosen with the `language` command, and the client should adopt it), and then,
without `done`:

```json
{"response": "", "status": "ok", "done": false, "event": {"type": "language", "lang": "en", "at": "..."}}
```

(Pipe's language was changed from any channel, so the client switches its messages; the event doesn't go into the
`alerts` history)

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "alert", "id": "3f2a9c1d0b", "key": "disk:/", "severity": "warning|critical",
  "title": "Disk / is 93% full", "detail": "3.1 GB free of 40 GB (/dev/sda1)",
  "since": "2026-09-24 14:02", "state": "new|resolved|event", "history": "", "at": "2026-09-24 14:02"}}
```

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "routine", "name": "morning-review", "status": "OK|PROBLEM", "report": "...", "at": "..."}}
```

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "reminder", "id": "6d1546", "kind": "message|task", "text": "...", "report": "",
  "to": "telegram:123", "set_at": "2026-10-01 20:02", "due": "2026-10-01 20:02:42", "at": "..."}}
```

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "activity", "id": "<tool call id>", "phase": "start|wait|end", "tool": "execute_command",
  "label": "docker restart web", "nodes": ["c:web"], "ok": null, "entry": "", "workers": {}}}
```

Only a client whose `interface` starts with `web` gets `activity` events; they say what the agent is doing and on
which nodes of the map (`graph`). `wait` = waiting for confirmation; `end` has `ok` (true/false/null after a refusal)
and `entry`, the journal entry id when the change was made.

`to` is the interface that set the reminder; the client decides whom to show it to. For `kind: task` the `report`
field contains the worker's report.

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "digest", "title": "Report vps1 -- 29.09.2026 07:00", "text": "<everything as text>",
  "sections": [{"title": "Health", "lines": ["..."]}, {"title": "Changes since yesterday", "lines": ["..."]}],
  "attachment": {"name": "load-chart.png", "mime": "image/png", "data": "<base64>", "...": "..."}, "at": "..."}}
```

`{"type": "investigation", "key", "title", "report"}` -- the report of a worker that investigated a webhook alert by
itself (`WEBHOOK_INVESTIGATE=1`). `history` in an alert -- the last occurrence of the same problem (incident memory).

`{"type": "approval", "id", "command", "target", "requested_by", "reason", "plan", "status"}` -- an external agent
(MCP) is waiting for admin approval; the client shows buttons and sends `approve`.

`{"type": "welcome", "title", "sections", "score", "grade", "text", "attachment"?}` -- once after installation, when
the first subscriber connects.

`state`: `new` -- a new or escalated alert, `resolved` -- the problem is gone, `event` -- a one-off event (a new public
port, a stopped container). `investigate` with an `id` asks the agent to investigate the alert.
Alert keys have a group prefix: `disk:`, `memory`, `load`, `container:`, `port:` (every `WATCH_INTERVAL`)
and `cert:`, `site:`, `dns:`, `backup:` (checks without configuration, every `CHECKS_INTERVAL`).
`hook:<source>:<name>` -- webhook alerts (persistent until a `resolved` message). `auth:ssh` (a series of failed
logins) is persistent; `security:<section>:<key>`, `auth:breach:...` and `auth:login:...` are one-off events.
`digest` arrives once a day at `DIGEST_TIME`.

### Token authorization

The `token` field is required when any token is configured: `AGENT_TOKEN` (admin role), `AGENT_VIEWER_TOKEN` (viewer
role) or client tokens (`python3 -m backend.tokens add NAME --role admin|viewer`, with only the SHA-256 hash in
`data/tokens.json`). Without any token the backend accepts every request (admin role).

The **viewer** role (read only): `{"confirm": true}` and `undo` with `execute` are refused, tools that change state
return a refusal to the model (the client never gets a YES question). A session belongs to the identity that created
it (the admin token, the viewer token or a named token); a request with another token and the same `session_id` gets
`This session belongs to another client.`

The token is checked for **every** request type, including confirmations (`confirm`), so an unauthenticated client
can't approve an operation pending in someone else's session. With a wrong token the backend answers:

```json
{"response": "Error: invalid authorization token.", "status": "error", "done": true}
```

## Response states

| Status | Meaning |
|--------|---------|
| `ok` | A normal agent response |
| `confirm` | The agent is waiting for the user's confirmation |
| `error` | An agent error or a refusal |

## Session flow

1. The client connects (Unix socket or TCP)
2. The client sends a request (a JSON line + `\n`)
3. The server answers with one or more JSON lines
4. The last line has `"done": true`
5. If `"status": "confirm"`, the client asks the user for confirmation
6. The client sends `{"confirm": true|false, "session_id": "..."}`
7. The server continues processing and sends further responses

## Python implementation

```python
import asyncio
import json

async def chat_with_agent(message: str, session_id: str) -> list[dict]:
    reader, writer = await asyncio.open_unix_connection("/tmp/vps-agent.sock", limit=32 * 1024 * 1024)

    request = {"message": message, "session_id": session_id, "interface": "my-client"}
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()

    responses = []
    while True:
        raw = await reader.readline()
        resp = json.loads(raw)
        if resp.get("attachment"):
            save(resp["attachment"])            # e.g. base64 -> PNG file
        elif resp.get("event"):
            show_progress(resp["event"])
        else:
            responses.append(resp)
        if resp.get("done"):
            break

    writer.close()
    return responses
```

## Requirements for new clients

- One `session_id` per user (for example `str(user_id)`)
- Send `interface` (`<client>:<user>`); it identifies the user in the audit log and in VIBE
- Handle the `confirm` status: ask the user and send the answer
- Handle `attachment` (show the image or save the file) and `event` of type `progress` (show it live)
- Read lines with a limit >= 32 MiB
- Handle long answers: split them into smaller messages if the platform has a limit
- Alerts: a separate, permanent connection with `subscribe` and reconnecting after a disconnect

## Webhooks (HTTP)

A separate port (`WEBHOOK_PORT`, off by default) that accepts alerts from other systems:

```
POST /hook/alertmanager | /hook/grafana | /hook/uptime-kuma | /hook/github | /hook/generic
Authorization: Bearer <WEBHOOK_TOKEN>        (or ?token=..., or GitHub's X-Hub-Signature-256 signature)
Content-Type: application/json

202 {"ok": true, "alerts": 2, "events": 1}
GET /health -> 200 {"ok": true}
```

Generic format (`/hook/generic`): `{"title", "message", "severity": "critical|warning", "status": "firing|resolved",
"name"}`. Body limit 1 MiB. Without `WEBHOOK_TOKEN` the webhook server doesn't start.
