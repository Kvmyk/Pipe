"""
MCP (Model Context Protocol) w Pipe — w obie strony.

  server.py    Pipe jako serwer MCP: inni agenci (Claude Code, Cursor...) pracuja
               na serwerze przez klasyfikator, dziennik i zgody Pipe
  client.py    Pipe jako klient: narzedzia zewnetrznych serwerow MCP (stdio, HTTP)
               staja sie narzedziami agenta
  registry.py  konfiguracja serwerow (DATA_DIR/mcp.json) i polityka wywolan

Obslugiwane wersje protokolu: 2026-07-28 (bezstanowa, _meta w kazdym zadaniu,
server/discover) oraz starsze z inicjalizacja (2025-11-25, 2025-06-18, 2025-03-26).
Implementacja bez zewnetrznego SDK — tylko narzedzia (tools), bez zasobow i promptow.
"""

from __future__ import annotations

from typing import Any

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Kody bledow JSON-RPC
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022


def error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def result(request_id: Any, value: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def is_modern_error(message: Any) -> bool:
    """Blad, po ktorym klient rozpoznaje nowoczesny serwer (nie wraca do initialize)."""
    if not isinstance(message, dict) or not isinstance(message.get("error"), dict):
        return False
    return message["error"].get("code") in (UNSUPPORTED_VERSION, HEADER_MISMATCH, -32021)
