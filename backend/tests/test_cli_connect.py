"""`pipe connect <aplikacja>`: gotowa konfiguracja Pipe jako serwera MCP dla Claude Code, Cursora, Codexa, Hermesa..."""

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("rich")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "cli"))
import cli  # noqa: E402


@pytest.fixture(autouse=True)
def polish(monkeypatch):
    monkeypatch.setattr(cli, "LANG", "pl")


def test_bridge_uses_the_same_connection_options():
    assert cli._bridge_args("root@1.2.3.4") == ["--mcp", "--host", "root@1.2.3.4"]
    assert cli._bridge_args("root@x", 2222, "~/.ssh/id") == ["--mcp", "--host", "root@x", "--ssh-port", "2222",
                                                              "--key", "~/.ssh/id"]
    assert cli._bridge_args(None, kube="pipe", kube_context="prod") == ["--mcp", "--kube", "pipe", "--kube-context", "prod"]
    with pytest.raises(ValueError):
        cli._bridge_args(None)


def test_json_configs_are_valid_and_keep_the_token_in_env():
    bridge = cli._bridge_args("root@x")
    for app in ("claude-desktop", "cursor"):
        text = cli.connect_config(app, bridge, "tok")
        config = json.loads(text[text.index("{"):text.rindex("}") + 1])
        assert config == {"mcpServers": {"pipe": {"command": "pipe", "args": bridge, "env": {"AGENT_TOKEN": "tok"}}}}


def test_every_app_has_a_config_and_never_puts_the_token_in_args():
    bridge = cli._bridge_args("root@x")
    for app in cli.CONNECT_APPS:
        text = cli.connect_config(app, bridge, "s3cret")
        assert "--mcp" in text and "--token" not in text
    hermes = cli.connect_config("hermes", bridge, "s3cret")
    assert '"${env:PIPE_TOKEN}"' in hermes and "PIPE_TOKEN=s3cret" in hermes and "/reload-mcp" in hermes
    assert "openclaw mcp add pipe --command pipe --arg --mcp --arg --host --arg root@x" in \
        cli.connect_config("openclaw", bridge)
    assert '[mcp_servers.pipe]' in cli.connect_config("codex", bridge)


def test_shell_quoting_and_token_hint():
    text = cli.connect_config("claude", cli._bridge_args("root@x", key="/home/a b/key"), "")
    assert re.search(r"--key '/home/a b/key'", text) and "AGENT_TOKEN='<TOKEN>'" in text
    assert "python3 -m backend.tokens add claude --role admin" in text
    with pytest.raises(ValueError):
        cli.connect_config("foo", ["--mcp"])
