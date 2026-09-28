"""
Testy nowych ramek protokolu: zalaczniki, postep, komendy danych i subskrypcja zdarzen.
"""

import asyncio
import base64
import json
import tempfile
from pathlib import Path

import pytest

import backend.server as server
from backend.config import settings
from backend.core import memory, watch
from backend.core.events import Attachment, Progress


class EventAgent:
    async def chat(self, session_id, message, interface="cli", **kwargs):
        yield Progress("worker web: start", source="worker:web")
        yield Attachment("mapa.png", "image/png", b"\x89PNGdane", caption="Mapa", source="flowchart LR", text="ascii")
        yield "[POTWIERDZ] Operacja wymaga potwierdzenia: `rm x`"

    async def confirm(self, session_id, confirmed):
        yield "ok"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    monkeypatch.setattr(server, "get_agent", lambda: EventAgent())
    monkeypatch.setattr(watch, "_watcher", watch.Watcher())


async def _open():
    sock = Path(tempfile.mkdtemp(prefix="p")) / "s.sock"
    srv = await asyncio.start_unix_server(server.handle_client, path=str(sock), limit=server.READ_LIMIT)
    reader, writer = await asyncio.open_unix_connection(str(sock), limit=server.READ_LIMIT)
    return srv, reader, writer


async def _request(reader, writer, payload):
    writer.write((json.dumps({"session_id": "s", **payload}) + "\n").encode())
    await writer.drain()
    frames = []
    while True:
        frame = json.loads(await reader.readline())
        frames.append(frame)
        if frame.get("done"):
            return frames


def exchange(payload):
    async def run():
        srv, reader, writer = await _open()
        frames = await _request(reader, writer, payload)
        writer.close()
        srv.close()
        return frames
    return asyncio.run(run())


def test_event_frames(env):
    progress, attachment, confirm, done = exchange({"message": "x"})
    assert progress["event"] == {"type": "progress", "text": "worker web: start", "source": "worker:web"}
    assert progress["response"] == ""
    assert base64.b64decode(attachment["attachment"]["data"]) == b"\x89PNGdane"
    assert attachment["attachment"]["text"] == "ascii" and attachment["response"] == ""
    assert confirm["status"] == "confirm"
    assert done["done"] is True


def test_vibe_show_and_reset(env):
    memory.write_vibe("telegram:7", "# VIBE\n\n- krotko")
    [frame] = exchange({"command": "vibe", "interface": "telegram:7"})
    assert "krotko" in frame["data"]["content"]
    [frame] = exchange({"command": "vibe", "interface": "telegram:7", "args": "reset"})
    assert frame["data"]["reset"] is True and memory.read_vibe("telegram:7") == ""


def test_directory_and_targets(env):
    memory.upsert_directory("/srv/shop", "repo", "sklep")
    [frame] = exchange({"command": "directory"})
    assert frame["data"]["entries"][0]["path"] == "/srv/shop"
    [frame] = exchange({"command": "targets"})
    assert frame["data"]["targets"] == []


def test_unknown_alert(env):
    [frame] = exchange({"command": "investigate", "id": "nope"})
    assert frame["status"] == "error"


def test_diagram_command_sends_attachment(env, monkeypatch):
    pytest.importorskip("mermaidx")
    from backend.core import infra

    async def fake_discover(include_kube=None):
        return infra.Infra(hostname="vps", containers=[infra.Container("web", "nginx", "running")])

    monkeypatch.setattr(infra, "discover", fake_discover)
    frames = exchange({"command": "diagram"})
    assert base64.b64decode(frames[0]["attachment"]["data"]).startswith(b"\x89PNG")
    assert "vps" in frames[0]["attachment"]["source"]
    assert frames[-1]["done"] and "web" in frames[-1]["data"]["summary"]


def test_subscribe_receives_published_events(env):
    async def run():
        srv, reader, writer = await _open()
        writer.write((json.dumps({"command": "subscribe"}) + "\n").encode())
        await writer.drain()
        hello = json.loads(await reader.readline())
        assert hello["event"]["type"] == "subscribed"
        watch.get_watcher().notifier.publish({"type": "alert", "id": "a1", "title": "Dysk / 95%"})
        frame = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert frame["event"]["title"] == "Dysk / 95%" and frame["done"] is False
        writer.close()
        await asyncio.sleep(0.05)
        assert watch.get_watcher().notifier.subscribers == 0
        srv.close()
    asyncio.run(run())


def test_token_required_for_subscribe(env, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TOKEN", "sekret")
    [frame] = exchange({"command": "subscribe"})
    assert frame["status"] == "error" and frame["done"]
