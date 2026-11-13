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


# ─── Plik z tokenem: ${PIPE_HOME:-~/.pipe}/token ─────────────────────────────

def test_token_file_location_follows_pipe_home(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPE_HOME", str(tmp_path))
    assert cli.token_file() == tmp_path / "token"


def test_read_token_file_strips_whitespace_and_ignores_empty_or_missing(tmp_path):
    path = tmp_path / "token"
    assert cli.read_token_file(path) == ""                       # brak pliku
    path.write_text("  \n", encoding="utf-8")
    assert cli.read_token_file(path) == ""                       # pusty
    path.write_text("  sekret123 \r\n", encoding="utf-8")
    path.chmod(0o600)
    assert cli.read_token_file(path) == "sekret123"
    path.write_bytes(b"\xff\xfe\x00")
    assert cli.read_token_file(path) == ""                       # nieczytelny (nie UTF-8)


@pytest.mark.skipif(sys.platform == "win32", reason="prawa POSIX")
def test_group_readable_token_file_warns(tmp_path, capsys):
    path = tmp_path / "token"
    path.write_text("abc\n", encoding="utf-8")
    path.chmod(0o640)
    assert cli.read_token_file(path) == "abc"
    assert "chmod 600" in capsys.readouterr().out
    path.chmod(0o600)
    cli.read_token_file(path)
    assert capsys.readouterr().out == ""


def test_token_order_flag_then_env_then_file(monkeypatch, tmp_path):
    """--token i AGENT_TOKEN dochodza jako `explicit` (default flagi = zmienna srodowiskowa), plik jest ostatni."""
    monkeypatch.setenv("PIPE_HOME", str(tmp_path))
    (tmp_path / "token").write_text("z-pliku", encoding="utf-8")
    (tmp_path / "token").chmod(0o600)
    assert cli.resolve_token("z-flagi-albo-env") == "z-flagi-albo-env"
    assert cli.resolve_token("") == "z-pliku"
    (tmp_path / "token").write_text("", encoding="utf-8")
    assert cli.resolve_token("") == ""


def test_agent_token_env_is_the_default_of_the_flag(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "z-env")
    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert 'default=os.getenv("AGENT_TOKEN", "")' in src and "resolve_token(args.token)" in src


def test_hint_mentions_the_token_file(monkeypatch):
    monkeypatch.setattr(cli, "LANG", "en")
    assert "~/.pipe/token" in cli.auth_hint(False)
