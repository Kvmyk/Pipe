# Pipe Web

The browser interface: chat with the agent, a live map of the server and a preview of changes.

```bash
pipe web                 # SSH tunnel as in the CLI + the page at http://127.0.0.1:7400
pipe web --lang pl       # in Polish (without --lang the page speaks the server's language)
pipe web --no-browser    # only print the address
pipe web --web-port 8080
```

The address printed in the terminal contains a one-time access key. Nothing needs to be built or installed:
the page is plain files from `static/`, and the server (`server.py`) uses only the Python standard library.

## How it works

```
browser --HTTP--> 127.0.0.1:7400 (server.py on your laptop) --SSH tunnel--> backend on the server
```

- No new port opens on the server; the backend token stays in the `pipe web` process.
- `POST /api/request` forwards a Pipe protocol request and streams the frames (NDJSON),
  `GET /api/events` forwards alerts, reminders and reports live (SSE).
- The page is reachable only from this computer, with the key from the address (then an HttpOnly cookie);
  requests with a foreign `Host` or `Origin` header are refused.

## First run

`pipe web` without `--host` and without a saved connection (or with `--setup`) opens a wizard: *What do you want to do
with Pipe?* (a few words, or a pick between the simple and the technical view), then *Where should Pipe run?*: on this
computer (Docker) or on your server (address, port, user and password, a key file or the keys from `~/.ssh`). The bridge
(`server.py`, `POST /api/setup`) calls `clients/cli/appsetup.py` and shows the progress line by line; on success it
switches to the new backend and the page reloads by itself. The password goes only to this process and is not saved.
Clicking the server address in the header opens the second step again (change of place).

## What is on the page

- **View**: a button in the header switches between the simple view (chat, a *Dashboard* without charts, *Add-ons*)
  and the technical one (plus the *Map* and *Changes*). The choice is shared with the CLI and Telegram (`/view`).
- **Language**: EN | PL with flags in the header (`/language`); the choice is shared by the whole agent.
- **Add-ons**: skills (run, preview), connected MCP servers and adding new ones from GitHub (`/download`, `/mcp add`).
- **Dashboard** (the first tab), without a model: a headline *Everything works* or what needs attention, CPU, memory
  and disk rings with the last day's history, the *From Pipe* inbox (alerts, findings, suggestions and reports with
  action buttons; the bell in the top bar counts new ones and opens the notification settings), workshop project cards
  (logs, stop, *From GitHub*) and today's model usage. A message with `push` also shows up as a toast. Sections and
  single messages fold.
- **Chat**: an empty screen suggests tasks in plain words (those marked *no AI* work at once and cost nothing); agent
  answers, diagrams, confirmations with the safety-fuse plan and the file diff.
- **Attachments and voice**: the paperclip, dragging a file or pasting a screenshot attaches files to the message
  (thumbnails before sending, up to 10 MB per file); the microphone records a voice message (WAV 16 kHz) and sends its text.
- **Map**: generated automatically, three levels: servers -> inside a server -> a compose project. The element the
  agent is working on is highlighted; *Follow the agent* moves the view after it. Any move on the map turns following
  off, the button turns it back on. Under the map, a timeline of actions.
- **Changes**: the journal of approved changes: a "before -> after" diff for every file and an undo button.
- **Provider**: on the first visit, a screen to choose the LLM provider (API key, check, model). Above the chat box,
  a switcher: the providers that have a key, switched with one click. `/providers` opens the screen again. Provider
  logos (`static/providers/`, the `@lobehub/icons-static-svg` package, MIT) are trademarks of their owners and only show
  whose models the agent connects to, see `static/providers/NOTICE.md`; your own providers get a monogram. Keys are
  stored on the server (`DATA_DIR/llm_keys.json`), not in the browser.
- **Commands**: type `/` in the chat box: a list of all commands and skills with descriptions opens (arrows, Enter
  runs, Tab completes, Esc closes). The same commands as in the CLI, in English and Polish. You run skills from here
  (`/skills` shows the list with their content); alerts are in the dashboard inbox and in `/alerts`.

## Files

| File | Role |
|------|------|
| `server.py` | local HTTP server and the bridge to the backend |
| `static/app.js` | chat, confirmations, timeline, tabs |
| `static/home.js` | dashboard: server state, inbox with actions, projects, notification settings |
| `static/onboard.js` | first run: view, place (local / server), installation progress |
| `static/addons.js` | the Add-ons tab: skills and MCP servers |
| `static/graph.js` | the map: layout, camera, transitions between levels, animations |
| `static/md.js` | a safe Markdown renderer (no `innerHTML`) |
| `static/i18n.js` | texts en / pl |
| `static/app.css` | the look: light and dark theme, colour only with a purpose (blue: the agent works, amber: waits for approval, green / red: the result) |
| `static/fonts/` | IBM Plex Sans and IBM Plex Mono (`woff2`, SIL OFL 1.1 license in `OFL.txt`); the page loads nothing from outside |
