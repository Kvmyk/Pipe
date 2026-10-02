"""CLI przezywa restart backendu (np. po /aktualizuj): laczy ponownie i ponawia zadanie."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("rich")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "cli"))
import cli  # noqa: E402


class Backend:
    """Minimalny backend JSON lines, ktory da sie zatrzymac i uruchomic ponownie na tym samym porcie."""

    def __init__(self):
        self.port = 0
        self.requests = []
        self.cut_after_first_frame = False
        self._server = None
        self._writers = []

    async def _handle(self, reader, writer):
        self._writers.append(writer)
        while line := await reader.readline():
            request = json.loads(line)
            self.requests.append(request)
            writer.write((json.dumps({"response": "czesc", "status": "ok", "done": False}) + "\n").encode())
            await writer.drain()
            if self.cut_after_first_frame:
                writer.close()
                return
            writer.write((json.dumps({"response": "", "status": "ok", "done": True}) + "\n").encode())
            await writer.drain()

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._server.close()
        for writer in self._writers:
            writer.close()
        self._writers = []
        await self._server.wait_closed()


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(cli, "RECONNECT_DELAY", 0.05)
    monkeypatch.setattr(cli, "RECONNECT_TIMEOUT", 5.0)


def test_conversation_continues_after_backend_restart():
    async def scenario():
        backend = Backend()
        await backend.start()
        client = cli.RemoteClient(session_id="s", port=backend.port)
        await client.connect()
        assert (await client.send_message("pierwsza"))[0]["response"] == "czesc"

        await backend.stop()                              # kontener przebudowuje sie po aktualizacji

        async def come_back():
            await asyncio.sleep(0.4)
            await backend.start()
        restart = asyncio.ensure_future(come_back())
        responses = await client.send_message("druga")    # wyslana, gdy backendu nie ma
        await restart
        await client.disconnect()
        await backend.stop()
        return backend.requests, responses

    requests, responses = asyncio.run(scenario())
    assert [r["message"] for r in requests] == ["pierwsza", "druga"]
    assert responses[-1]["done"] is True


def test_gives_up_after_timeout(monkeypatch):
    monkeypatch.setattr(cli, "RECONNECT_TIMEOUT", 0.2)

    async def scenario():
        backend = Backend()
        await backend.start()
        client = cli.RemoteClient(session_id="s", port=backend.port)
        await client.connect()
        await backend.stop()
        with pytest.raises(ConnectionError):
            await client.send_message("halo")

    asyncio.run(scenario())


def test_partial_answer_is_not_resent():
    async def scenario():
        backend = Backend()
        backend.cut_after_first_frame = True
        await backend.start()
        client = cli.RemoteClient(session_id="s", port=backend.port)
        await client.connect()
        responses = await client.send_message("zrob cos")
        await backend.stop()
        return backend.requests, responses

    requests, responses = asyncio.run(scenario())
    assert len(requests) == 1                             # zadna powtorka — agent mogl juz cos wykonac
    assert [r["response"] for r in responses] == ["czesc"]


def test_probe_fails_quietly_and_recovers():
    """Odpytywanie w tle nie czeka minutami na martwy backend, a po jego powrocie dziala dalej."""
    async def scenario():
        backend = Backend()
        await backend.start()
        client = cli.RemoteClient(session_id="s", port=backend.port)
        await client.connect()
        await backend.stop()
        with pytest.raises((OSError, EOFError)):
            await client.probe("reminders", claim=True)
        await backend.start()
        responses = await client.probe("reminders", claim=True)
        await client.disconnect()
        await backend.stop()
        return responses

    assert asyncio.run(scenario())[-1]["done"] is True
