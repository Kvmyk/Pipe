# Web UI — Coming Soon

Przeglądarkowy interfejs dla VPS Management Agent.

> 🚧 **Status: Placeholder — Pull Requests mile widziane!**

## Planowany stack

- **Frontend:** React + Vite
- **Komunikacja z backendem:** WebSocket proxy lub HTTP-to-socket bridge
- **Funkcje:** historia rozmów, audit log viewer, real-time streaming odpowiedzi

## Architektura

Ponieważ przeglądarka nie może bezpośrednio łączyć się z Unix socket, potrzebny jest bridge:

```
Przeglądarka (WebSocket)
        ↓
    bridge.py (WebSocket → Unix socket)
        ↓
    /tmp/vps-agent.sock
        ↓
    backend/server.py
```

### Bridge WebSocket → Socket (przykład)

```python
import asyncio
import json
import websockets

async def bridge_handler(websocket):
    reader, writer = await asyncio.open_unix_connection("/tmp/vps-agent.sock")
    
    async def forward_to_backend():
        async for message in websocket:
            writer.write(message.encode() + b"\n")
            await writer.drain()
    
    async def forward_to_client():
        while True:
            raw = await reader.readline()
            if not raw:
                break
            await websocket.send(raw.decode())
    
    await asyncio.gather(forward_to_backend(), forward_to_client())

asyncio.run(websockets.serve(bridge_handler, "localhost", 8765))
```

## Chcesz dodać Web UI?

1. Fork tego repo
2. Utwórz `clients/webui/`
3. Zaimplementuj frontend + bridge WebSocket → socket
4. Dodaj `README.md` z instrukcją instalacji
5. Otwórz Pull Request 🎉

Protokół socket: patrz `clients/discord/README.md`
