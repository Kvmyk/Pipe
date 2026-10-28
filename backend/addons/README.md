# MCP add-on catalog

Ready integrations the user turns on with one click (`/mcp` in the CLI, `pipe web` and on Telegram). Nothing is on by
default. One JSON file = one add-on; the file name = `name`.

Adding a new one? Copy `github.json` and change:

- `name`: lowercase letters, digits, `-`, `_` (up to 24 characters); this becomes the server name and the tool group
  `mcp:<name>`.
- `title`, `description`: in English and Polish, in plain words: what the user will do with it.
- `server`: the same as `/mcp add` takes: `command` `npx`/`uvx` with arguments, `image` (a Docker image) or `url`
  (an HTTP server). Pin the package version (`package@1.2.3`). The server runs in an isolated container: no server
  files, no docker.sock, as the nobody user.
- `env`: secrets only as `${MCP_NAME}`; describe every such secret in `secrets` (label, how to get it, a link). The
  user types it in the `/mcp` window in `pipe web` or in the CLI, never in the chat with the model.
- `state: true`: the server gets its own `/state` volume (e.g. tokens after a device-code login). Removing the add-on
  removes the volume too.
- `autoApprove`: only tools that change nothing (logging in, login state). The rest asks for approval, unless the
  server itself marks the tool as read-only (`readOnlyHint`).
- `login`: optional: what to do after adding (e.g. "write: log me in to ...").
- `category`: `dev`, `office` or `other` (order in the list).

`backend/tests/test_addons.py` checks every file with the same validation as `/mcp add`.
