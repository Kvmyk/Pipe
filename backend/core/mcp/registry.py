"""
Rejestr serwerow MCP, z ktorych korzysta Pipe (DATA_DIR/mcp.json).

Format jak w innych klientach MCP, plus polityka Pipe:

    {"servers": {
      "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                 "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}, "autoApprove": ["get_*", "list_*", "search_*"]},
      "grafana": {"url": "http://127.0.0.1:8000/mcp", "headers": {"Authorization": "Bearer ..."},
                  "trustReadOnly": true}
    }}

Polityka: wywolanie narzedzia MCP wymaga potwierdzenia (TAK), chyba ze nazwa pasuje
do `autoApprove` albo serwer ma `trustReadOnly` i narzedzie deklaruje readOnlyHint.
Adnotacje serwera sa niezaufane — dlatego zaufanie wlacza uzytkownik, nie serwer.
Narzedzia trafiaja do modelu jako `mcp__<serwer>__<narzedzie>`.

Dodanie serwera uruchamia cudzy program jako Pipe — zawsze wymaga potwierdzenia.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core import memory
from backend.core.i18n import tr
from backend.core.mcp.client import HttpTransport, McpClient, McpError, StdioTransport

MAX_SERVERS = 20
MAX_MCP_TOOLS = 60
CONNECT_TIMEOUT = 30.0
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


class McpConfigError(ValueError):
    pass


def config_path() -> Path:
    return memory.data_dir() / "mcp.json"


def load_config() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = raw.get("servers") if isinstance(raw, dict) else None
    return {name: cfg for name, cfg in (servers or {}).items() if isinstance(cfg, dict) and _NAME.match(name)}


def _save(servers: dict[str, dict[str, Any]]) -> None:
    path = config_path()
    memory._write_atomic(path, json.dumps({"servers": servers}, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)      # env/headers moga zawierac tokeny


def validate_server(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise McpConfigError(tr("Nazwa serwera MCP: male litery, cyfry, '-', '_' (do 24 znakow).",
                                "MCP server name: lowercase letters, digits, '-', '_' (up to 24 characters)."))
    clean: dict[str, Any] = {}
    if cfg.get("url"):
        url = str(cfg["url"]).strip()
        if not re.match(r"^https?://", url):
            raise McpConfigError(tr("url musi zaczynac sie od http:// albo https://",
                                    "url must start with http:// or https://"))
        clean["url"] = url
        headers = cfg.get("headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise McpConfigError(tr("headers: slownik napisow.", "headers: a dictionary of strings."))
        clean["headers"] = headers
    elif cfg.get("command"):
        command = cfg["command"]
        args = cfg.get("args") or []
        if isinstance(command, str) and not args and " " in command.strip():
            command, *args = shlex.split(command)
        if not isinstance(command, str) or not all(isinstance(a, str) for a in args):
            raise McpConfigError(tr("command: napis, args: lista napisow.", "command: a string, args: a list of strings."))
        clean["command"], clean["args"] = command, list(args)
        env = cfg.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise McpConfigError(tr("env: slownik napisow.", "env: a dictionary of strings."))
        clean["env"] = env
        if cfg.get("cwd"):
            clean["cwd"] = str(cfg["cwd"])
    else:
        raise McpConfigError(tr("Podaj command (serwer stdio) albo url (serwer HTTP).",
                                "Give command (stdio server) or url (HTTP server)."))
    auto = cfg.get("autoApprove") or []
    if not isinstance(auto, list) or not all(isinstance(p, str) for p in auto):
        raise McpConfigError(tr("autoApprove: lista wzorcow nazw narzedzi, np. [\"get_*\", \"list_*\"].",
                                "autoApprove: a list of tool-name patterns, e.g. [\"get_*\", \"list_*\"]."))
    clean["autoApprove"] = auto
    clean["trustReadOnly"] = bool(cfg.get("trustReadOnly", False))
    clean["enabled"] = bool(cfg.get("enabled", True))
    if cfg.get("description"):
        clean["description"] = " ".join(str(cfg["description"]).split())[:200]
    return clean


def describe_server(name: str, cfg: dict[str, Any]) -> str:
    """Opis do potwierdzenia — wartosci env/headers ukryte (tylko nazwy)."""
    if "url" in cfg:
        where = f"HTTP {cfg['url']}" + (tr(", naglowki: ", ", headers: ") + ", ".join(cfg["headers"]) if cfg.get("headers") else "")
    else:
        where = "program: " + " ".join(shlex.quote(p) for p in [cfg["command"], *cfg.get("args", [])])
        if cfg.get("env"):
            where += tr(", zmienne: ", ", variables: ") + ", ".join(cfg["env"])
    policy = []
    if cfg.get("autoApprove"):
        policy.append(tr("bez pytania: ", "without asking: ") + ", ".join(cfg["autoApprove"]))
    if cfg.get("trustReadOnly"):
        policy.append(tr("bez pytania: narzedzia oznaczone jako tylko-odczyt",
                         "without asking: tools marked read-only"))
    return f"{name}: {where}" + (f" ({'; '.join(policy)})" if policy else tr(" (kazde wywolanie z potwierdzeniem)", " (every call needs confirmation)"))


def add_server(name: str, cfg: dict[str, Any]) -> bool:
    clean = validate_server(name, cfg)
    servers = load_config()
    created = name not in servers
    if created and len(servers) >= MAX_SERVERS:
        raise McpConfigError(tr(f"Za duzo serwerow MCP (limit {MAX_SERVERS}).", f"Too many MCP servers (limit {MAX_SERVERS})."))
    servers[name.strip().lower()] = clean
    _save(servers)
    return created


def remove_server(name: str) -> bool:
    servers = load_config()
    if servers.pop((name or "").strip().lower(), None) is None:
        return False
    _save(servers)
    return True


def tool_function_name(server: str, tool: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]
    name, index = base, 2
    while name in taken:
        suffix = f"_{index}"
        name = base[:64 - len(suffix)] + suffix
        index += 1
    return name


def needs_confirmation(cfg: dict[str, Any], tool: dict[str, Any]) -> bool:
    name = tool.get("name", "")
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in cfg.get("autoApprove", [])):
        return False
    annotations = tool.get("annotations") or {}
    return not (cfg.get("trustReadOnly") and annotations.get("readOnlyHint") is True)


def _schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    return schema


@dataclass
class ServerState:
    name: str
    config: dict[str, Any]
    client: McpClient | None = None
    status: str = "disconnected"      # connected | error | disabled | disconnected
    error: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        info = self.client.server_info if self.client else {}
        label = f"{info.get('name', '')} {info.get('version', '')}".strip()
        head = f"{self.name} [{self.status}]" + (f" {label}" if label else "") + \
            (f" ({self.client.era})" if self.client and self.client.era else "")
        if self.status == "error":
            return f"{head}: {self.error}"
        return f"{head}: {len(self.tools)} narzedzi" if self.status == "connected" else head


class McpManager:
    def __init__(self) -> None:
        self.servers: dict[str, ServerState] = {}
        self.functions: dict[str, tuple[str, dict[str, Any]]] = {}   # nazwa funkcji -> (serwer, narzedzie)
        self._lock = asyncio.Lock()

    @staticmethod
    def _transport(cfg: dict[str, Any]) -> StdioTransport | HttpTransport:
        if "url" in cfg:
            return HttpTransport(cfg["url"], cfg.get("headers"))
        return StdioTransport(cfg["command"], cfg.get("args", []), cfg.get("env"), cfg.get("cwd"))

    async def _connect(self, name: str, cfg: dict[str, Any]) -> ServerState:
        state = ServerState(name, cfg)
        if not cfg.get("enabled", True):
            state.status = "disabled"
            return state
        client = McpClient(self._transport(cfg))
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
        except (McpError, asyncio.TimeoutError, OSError, ValueError) as exc:
            await client.close()
            tail = " | ".join(getattr(client.transport, "stderr_tail", [])) if isinstance(client.transport, StdioTransport) else ""
            state.status, state.error = "error", (str(exc) or type(exc).__name__) + (f" — {tail}" if tail else "")
            return state
        except Exception as exc:  # np. blad sieci httpx
            await client.close()
            state.status, state.error = "error", f"{type(exc).__name__}: {exc}"
            return state
        state.client, state.status, state.tools = client, "connected", client.tools
        return state

    async def reload(self) -> None:
        async with self._lock:
            for state in self.servers.values():
                if state.client:
                    await state.client.close()
            config = load_config()
            states = await asyncio.gather(*(self._connect(n, c) for n, c in sorted(config.items())))
            self.servers = {s.name: s for s in states}
            self._index()

    def _index(self) -> None:
        self.functions = {}
        for state in self.servers.values():
            for tool in state.tools:
                if len(self.functions) >= MAX_MCP_TOOLS:
                    return
                name = tool_function_name(state.name, tool["name"], set(self.functions))
                self.functions[name] = (state.name, tool)

    def tool_schemas(self) -> list[dict[str, Any]]:
        schemas = []
        for function, (server, tool) in sorted(self.functions.items()):
            cfg = self.servers[server].config
            ask = tr("wymaga potwierdzenia", "requires confirmation") if needs_confirmation(cfg, tool) \
                else tr("bez potwierdzenia", "no confirmation")
            description = " ".join(str(tool.get("description", "") or tool.get("title", "") or "").split())[:900]
            schemas.append({"type": "function", "function": {
                "name": function,
                "description": f"[MCP {server}, {ask}] {description}".strip(),
                "parameters": _schema(tool)}})
        return schemas

    def resolve(self, function: str) -> tuple[ServerState, dict[str, Any]] | None:
        found = self.functions.get(function)
        if found is None or found[0] not in self.servers:
            return None
        return self.servers[found[0]], found[1]

    async def call(self, state: ServerState, tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        if state.client is None:
            raise McpError(tr(f"Serwer MCP {state.name} nie jest polaczony: {state.error or state.status}",
                              f"MCP server {state.name} is not connected: {state.error or state.status}"))
        try:
            return await state.client.call_tool(tool["name"], arguments)
        except (McpError, OSError) as exc:
            # jedna proba ponownego polaczenia (np. podproces sie zakonczyl)
            fresh = await self._connect(state.name, state.config)
            if fresh.client is None:
                raise McpError(tr(f"{exc}; ponowne polaczenie nieudane: {fresh.error}",
                                  f"{exc}; reconnect failed: {fresh.error}")) from exc
            await state.client.close()
            self.servers[state.name] = fresh
            return await fresh.client.call_tool(tool["name"], arguments)

    def status(self) -> list[str]:
        if not self.servers:
            return []
        return [s.describe() for s in self.servers.values()]

    async def close(self) -> None:
        for state in self.servers.values():
            if state.client:
                await state.client.close()


_manager: McpManager | None = None


def get_manager() -> McpManager:
    global _manager
    if _manager is None:
        _manager = McpManager()
    return _manager
