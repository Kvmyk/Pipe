"""
Klient MCP — Pipe korzysta z narzedzi innych serwerow MCP.

Transporty:
  stdio   serwer uruchamiany jako podproces (np. `npx -y @modelcontextprotocol/server-github`);
          dostaje MINIMALNE srodowisko (PATH, HOME, LANG + to, co podano w konfiguracji) —
          nigdy klucza LLM ani tokenow Pipe
  http    Streamable HTTP (POST na endpoint, odpowiedz JSON albo SSE)

Wykrywanie ery: najpierw zadanie nowoczesne (2026-07-28: server/discover z `_meta`),
przy bledzie spoza nowoczesnych kodow — `initialize` (2025-11-25 i starsze).
Obslugiwane sa tylko narzedzia; interakcje zwrotne (MRTR, sampling, elicitation)
koncza sie czytelnym bledem.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import re
from collections import deque
from typing import Any

from backend.core import mcp
from backend.core.mcp import (
    LEGACY_VERSIONS,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_VERSION,
    MODERN_VERSION,
)

CLIENT_INFO = {"name": "pipe", "version": "1"}
REQUEST_TIMEOUT = 60.0
PROBE_TIMEOUT = 8.0
MAX_LINE = 8 * 1024 * 1024
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "USER", "SHELL", "TMPDIR")
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class McpError(RuntimeError):
    def __init__(self, message: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


def modern_meta() -> dict[str, Any]:
    return {META_VERSION: MODERN_VERSION, META_CLIENT_INFO: CLIENT_INFO, META_CLIENT_CAPABILITIES: {}}


def header_value(value: str) -> str:
    """Wartosc naglowka: ASCII bez spacji na brzegach albo sentinel base64 (2026-07-28)."""
    safe = value and all(0x20 <= ord(c) <= 0x7E or c == "\t" for c in value) and value == value.strip() \
        and not (value.startswith("=?base64?") and value.endswith("?="))
    return value if safe else "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


def param_headers(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, str] | None:
    """Naglowki Mcp-Param-* z `x-mcp-header` (tylko sciezki `properties`). None = definicja nieprawidlowa."""
    headers: dict[str, str] = {}
    seen: set[str] = set()

    def walk(node: dict[str, Any], values: Any) -> bool:
        for key, prop in (node.get("properties") or {}).items():
            if not isinstance(prop, dict):
                continue
            name = prop.get("x-mcp-header")
            value = values.get(key) if isinstance(values, dict) else None
            if name is not None:
                if not isinstance(name, str) or not _HEADER_TOKEN.match(name) or name.lower() in seen \
                        or prop.get("type") not in ("string", "integer", "boolean"):
                    return False
                seen.add(name.lower())
                if value is not None:
                    text = str(value).lower() if isinstance(value, bool) else str(value)
                    headers[f"Mcp-Param-{name}"] = header_value(text)
            if prop.get("type") == "object" and not walk(prop, value):
                return False
        return True

    return headers if walk(schema or {}, arguments or {}) else None


# ─── Transporty ─────────────────────────────────────────────────────────────

class StdioTransport:
    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None,
                 cwd: str | None = None) -> None:
        self.command, self.args, self.env, self.cwd = command, args, env or {}, cwd
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.stderr_tail: deque[str] = deque(maxlen=20)

    async def start(self) -> None:
        env = {key: os.environ[key] for key in SAFE_ENV if key in os.environ}
        env.update(self.env)
        self.process = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=self.cwd, limit=MAX_LINE)
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def _stderr_loop(self) -> None:
        assert self.process and self.process.stderr
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            self.stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    async def _read_loop(self) -> None:
        assert self.process and self.process.stdout
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                # zadanie od serwera (roots/list, sampling...) — nieobslugiwane
                await self._write(mcp.error(message["id"], mcp.METHOD_NOT_FOUND, "Pipe nie obsluguje zadan serwera."))
                continue
            future = self._pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                future.set_result(message)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError("Serwer MCP zakonczyl dzialanie. " + " | ".join(self.stderr_tail)))
        self._pending.clear()

    async def _write(self, message: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin or self.process.stdin.is_closing():
            raise McpError("Proces serwera MCP nie dziala.")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, message: dict[str, Any], timeout: float, **_: Any) -> dict[str, Any]:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message["id"]] = future
        await self._write(message)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(message["id"], None)

    async def notify(self, message: dict[str, Any], **_: Any) -> None:
        await self._write(message)

    async def close(self) -> None:
        for task in (self._reader, self._stderr_task):
            if task:
                task.cancel()
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass


class HttpTransport:
    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}
        self.session_id = ""
        self.legacy_version = ""
        self._client = None

    async def start(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False)

    def _headers(self, message: dict[str, Any], extra: dict[str, str] | None) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   **self.headers, **(extra or {})}
        params = message.get("params") or {}
        if isinstance(params.get("_meta"), dict) and params["_meta"].get(META_VERSION):
            headers["MCP-Protocol-Version"] = params["_meta"][META_VERSION]
            headers["Mcp-Method"] = message["method"]
            if message["method"] == "tools/call":
                headers["Mcp-Name"] = header_value(str(params.get("name", "")))
        elif self.legacy_version and message.get("method") != "initialize":
            headers["MCP-Protocol-Version"] = self.legacy_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _post(self, message: dict[str, Any], extra: dict[str, str] | None, timeout: float):
        return await self._client.post(self.url, json=message, headers=self._headers(message, extra), timeout=timeout)

    async def request(self, message: dict[str, Any], timeout: float, extra_headers: dict[str, str] | None = None,
                      ) -> dict[str, Any]:
        response = await self._post(message, extra_headers, timeout)
        if response.headers.get("mcp-session-id") and message.get("method") == "initialize":
            self.session_id = response.headers["mcp-session-id"]
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and data.get("id") == message["id"]:
                        return data
            raise McpError(f"Strumien SSE bez odpowiedzi na zadanie (HTTP {response.status_code}).")
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and ("result" in data or "error" in data):
            return data
        raise McpError(f"HTTP {response.status_code} bez odpowiedzi JSON-RPC.", {"status": response.status_code})

    async def notify(self, message: dict[str, Any], **_: Any) -> None:
        await self._post(message, None, 15)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


# ─── Sesja klienta ──────────────────────────────────────────────────────────

class McpClient:
    def __init__(self, transport: StdioTransport | HttpTransport) -> None:
        self.transport = transport
        self.era = ""                 # modern | legacy
        self.server_info: dict[str, Any] = {}
        self.instructions = ""
        self.tools: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    def _message(self, method: str, params: dict[str, Any] | None = None, *, modern: bool) -> dict[str, Any]:
        params = dict(params or {})
        if modern:
            params["_meta"] = modern_meta()
        return {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}

    async def _call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = REQUEST_TIMEOUT,
                    extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        message = self._message(method, params, modern=self.era == "modern")
        response = await self.transport.request(message, timeout, extra_headers=extra_headers)
        if "error" in response:
            err = response["error"] if isinstance(response["error"], dict) else {}
            raise McpError(f"{method}: {err.get('message', 'blad')} (kod {err.get('code')})", response)
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method}: nieprawidlowa odpowiedz serwera.")
        if result.get("resultType") == "input_required":
            raise McpError("Serwer MCP wymaga dodatkowej interakcji (elicitation/sampling) — Pipe tego nie obsluguje.")
        return result

    async def connect(self) -> None:
        await self.transport.start()
        probe = self._message("server/discover", modern=True)
        try:
            response = await self.transport.request(probe, PROBE_TIMEOUT)
        except (McpError, asyncio.TimeoutError):
            response = None
        except Exception:                  # np. blad HTTP przy starym serwerze
            response = None
        if isinstance(response, dict) and isinstance(response.get("result"), dict) \
                and MODERN_VERSION in (response["result"].get("supportedVersions") or []):
            self.era = "modern"
            result = response["result"]
            self.server_info = (result.get("_meta") or {}).get(mcp.META_SERVER_INFO, {})
            self.instructions = str(result.get("instructions", "") or "")
        elif mcp.is_modern_error(response):
            raise McpError("Serwer MCP nie obsluguje wersji protokolu Pipe: "
                           + ", ".join((response["error"].get("data") or {}).get("supported", [])))
        else:
            self.era = "legacy"
            init = self._message("initialize", {"protocolVersion": LEGACY_VERSIONS[0], "capabilities": {},
                                                "clientInfo": CLIENT_INFO}, modern=False)
            response = await self.transport.request(init, REQUEST_TIMEOUT)
            if "error" in response:
                raise McpError(f"initialize: {response['error'].get('message')}", response)
            result = response.get("result") or {}
            if isinstance(self.transport, HttpTransport):
                self.transport.legacy_version = str(result.get("protocolVersion", LEGACY_VERSIONS[0]))
            self.server_info = result.get("serverInfo") or {}
            self.instructions = str(result.get("instructions", "") or "")
            await self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = await self.list_tools()

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            result = await self._call("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = next((t for t in self.tools if t["name"] == name), {})
        extra = param_headers(tool.get("inputSchema") or {}, arguments) if isinstance(self.transport, HttpTransport) \
            else {}
        return await self._call("tools/call", {"name": name, "arguments": arguments}, extra_headers=extra or None)

    async def close(self) -> None:
        await self.transport.close()


def result_text(result: dict[str, Any]) -> tuple[str, list[tuple[str, bytes]], bool]:
    """(tekst dla modelu, obrazy [(mime, dane)], czy blad) z wyniku tools/call."""
    parts: list[str] = []
    images: list[tuple[str, bytes]] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "image":
            try:
                images.append((str(item.get("mimeType", "image/png")), base64.b64decode(item.get("data", ""))))
            except (ValueError, TypeError):
                parts.append("[obraz — nieprawidlowe dane]")
        elif kind == "resource_link":
            parts.append(f"[zasob] {item.get('name', '')} {item.get('uri', '')}".strip())
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text", "")) or f"[zasob binarny] {resource.get('uri', '')}")
        else:
            parts.append(f"[{kind} — pominieto]")
    if not parts and "structuredContent" in result:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False)[:20_000])
    return "\n".join(parts).strip() or "(narzedzie nie zwrocilo tresci)", images, bool(result.get("isError"))
