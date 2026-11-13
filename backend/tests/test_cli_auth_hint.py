"""Odrzucony token: ramka bledu ma `code: auth`, a CLI mowi, jak podac token i gdzie go wziac."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("rich")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "cli"))
import cli  # noqa: E402

from backend import server  # noqa: E402


def test_server_error_frame_carries_code():
    assert server._error("x", "auth")["code"] == "auth"
    assert "code" not in server._error("x")


def test_is_auth_error_by_code_and_old_text():
    assert cli._is_auth_error({"status": "error", "code": "auth", "response": "cokolwiek"})
    assert cli._is_auth_error({"status": "error", "response": "[BLAD] Blad: Nieprawidlowy token autoryzacji."})
    assert cli._is_auth_error({"status": "error", "response": "[BLAD] Error: invalid authorization token."})
    assert not cli._is_auth_error({"status": "error", "response": "[BLAD] cos innego"})
    assert not cli._is_auth_error({"status": "ok", "code": "auth"})


def test_hint_says_where_token_lives_and_whether_one_was_sent(monkeypatch):
    monkeypatch.setattr(cli, "LANG", "en")
    monkeypatch.setenv("PIPE_LANG", "en")
    no_token, sent = cli.auth_hint(False), cli.auth_hint(True)
    assert "No token was sent" in no_token and "rejected" in sent
    for text in (no_token, sent):
        assert "--token" in text and "AGENT_TOKEN" in text and "backend/.env" in text


def _serve(frame):
    async def handle(reader, writer):
        await reader.readline()
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        writer.close()
    return handle


@pytest.mark.parametrize("token,expected", [("", "No token was sent"), ("zly", "rejected")])
def test_client_prints_hint_once_and_flags_failure(monkeypatch, capsys, token, expected):
    monkeypatch.setenv("PIPE_LANG", "en")
    monkeypatch.setattr(cli, "LANG", "en", raising=False)

    async def scenario():
        frame = {"response": "[BLAD] x", "status": "error", "code": "auth", "done": True}
        srv = await asyncio.start_server(_serve(frame), "127.0.0.1", 0)
        client = cli.RemoteClient("s", "127.0.0.1", srv.sockets[0].getsockname()[1], token=token)
        await client.connect()
        responses = await client.send_command("language")
        await client.disconnect()
        srv.close()
        return client, responses

    client, responses = asyncio.run(scenario())
    assert client.auth_failed and responses[0]["status"] == "error"
    assert expected in capsys.readouterr().out
