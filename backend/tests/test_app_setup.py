"""
Pierwsze uruchomienie `pipe web` (clients/cli/appsetup.py + most clients/webui/server.py): Pipe lokalnie albo na
serwerze przez SSH, haslo tylko w tym procesie, zapisane polaczenie dla kolejnych uruchomien i dla CLI.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "clients" / "cli"))
sys.path.insert(0, str(ROOT / "clients" / "webui"))
import appsetup  # noqa: E402
import server as web  # noqa: E402
from backend.tests.test_web import TestWebBridge  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(appsetup, "APP_DIR", tmp_path / ".pipe")
    monkeypatch.setattr(appsetup, "_known_hosts", lambda: tmp_path / ".ssh" / "known_hosts")
    return tmp_path


class TestStore:

    def test_connection_roundtrip_is_private(self, home):
        assert appsetup.saved_connection() is None
        conn = {"kind": "server", "host": "203.0.113.7", "user": "kuba", "ssh_port": 22, "key": "/k", "token": "t"}
        appsetup.remember(conn)
        assert appsetup.saved_connection() == conn
        if os.name == "posix":
            assert (home / ".pipe" / "app.json").stat().st_mode & 0o777 == 0o600
        assert appsetup.label(conn) == "kuba@203.0.113.7" and appsetup.label({"kind": "local"}) == "this computer"

    def test_garbage_is_ignored(self, home):
        (home / ".pipe").mkdir()
        (home / ".pipe" / "app.json").write_text('{"connection": {"kind": "ftp"}}')
        assert appsetup.saved_connection() is None


class TestValidation:

    @pytest.mark.parametrize("params,expected", [
        ({"host": "203.0.113.7"}, ("203.0.113.7", 22, "root")),
        ({"host": "kuba@vps.example.com", "port": "2222"}, ("vps.example.com", 2222, "kuba")),
        ({"host": "[2001:db8::1]", "user": "ops"}, ("2001:db8::1", 22, "ops")),
    ])
    def test_accepts(self, params, expected):
        assert appsetup.validate_server(params) == expected

    @pytest.mark.parametrize("params", [{"host": ""}, {"host": "a;rm -rf /"}, {"host": "-oProxyCommand=x"},
                                        {"host": "vps", "user": "x y"}, {"host": "vps", "port": "99999"}])
    def test_rejects(self, params):
        with pytest.raises(appsetup.SetupError):
            appsetup.validate_server(params)

    @pytest.mark.skipif(not shutil.which("bash"), reason="bash")
    @pytest.mark.parametrize("lang", ["pl", "en"])
    def test_remote_script_is_valid_bash(self, lang):
        script = appsetup.remote_script(lang)
        assert subprocess.run(["bash", "-n", "-c", script]).returncode == 0
        assert "exec </dev/null" in script and "LLM_PROVIDER=none" in script and appsetup.TOKEN_MARK in script


# --- atrapa paramiko -------------------------------------------------------

class FakeChannel:
    def __init__(self, server, command):
        self.server, self.command, self.stdin = server, command, ""
        server.commands.append(self)
        code, output = server.reply(command)
        self.code, self.output = code, output.encode()

    def set_combined_stderr(self, _): pass
    def exec_command(self, _): pass
    def sendall(self, data): self.stdin += data.decode()
    def shutdown_write(self): pass
    def recv_ready(self): return bool(self.output)

    def recv(self, n):
        chunk, self.output = self.output[:n], self.output[n:]
        return chunk

    def exit_status_ready(self): return True
    def recv_exit_status(self): return self.code
    def close(self): pass


def fake_paramiko(server):
    module = types.ModuleType("paramiko")

    class SSHException(Exception): pass
    class AuthenticationException(SSHException): pass
    class BadHostKeyException(SSHException): pass
    class MissingHostKeyPolicy: pass

    class RSAKey:
        def __init__(self, name="k"): self.name = name
        @classmethod
        def generate(cls, bits): return cls()
        @classmethod
        def from_private_key_file(cls, path): return cls()
        def write_private_key_file(self, path): Path(path).write_text("PRIVATE")
        def get_name(self): return "ssh-rsa"
        def get_base64(self): return "AAAAPIPEKEY"

    class Transport:
        def open_session(self): return _Channel()

    class _Channel:
        def __init__(self): self.inner = None
        def set_combined_stderr(self, v): pass
        def exec_command(self, command): self.inner = FakeChannel(server, command)
        def __getattr__(self, name): return getattr(self.inner, name)

    class SSHClient:
        def load_system_host_keys(self, path): pass
        def set_missing_host_key_policy(self, policy): server.policy = policy
        def connect(self, host, **kwargs):
            server.connected = (host, kwargs)
            if server.reject:
                raise AuthenticationException()
        def get_transport(self): return Transport()
        def close(self): server.closed = True

    module.SSHException, module.AuthenticationException = SSHException, AuthenticationException
    module.BadHostKeyException, module.MissingHostKeyPolicy = BadHostKeyException, MissingHostKeyPolicy
    module.RSAKey, module.SSHClient = RSAKey, SSHClient
    return module


class FakeServer:
    def __init__(self, uid="1000", install_code=0, install_output=None, reject=False):
        self.commands, self.uid, self.reject, self.closed = [], uid, reject, False
        self.install_code = install_code
        self.install_output = install_output if install_output is not None else (
            "\x1b[36m[pipe]\x1b[0m Klonuje\nhaslo w logu: sekret\n" + appsetup.TOKEN_MARK + "tok123\n")

    def reply(self, command):
        if command == "id -u":
            return 0, self.uid + "\n"
        if "authorized_keys" in command:
            return 0, ""
        return self.install_code, self.install_output


@pytest.fixture
def ssh(home, monkeypatch):
    def make(**kwargs):
        server = FakeServer(**kwargs)
        monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko(server))
        monkeypatch.setattr(appsetup, "has_paramiko", lambda: True)
        return server
    return make


class TestDeploy:

    def test_password_installs_own_key_and_runs_installer_with_sudo(self, ssh, home):
        server = ssh()
        lines = []
        conn = appsetup.deploy_server({"host": "kuba@203.0.113.7", "port": "2222", "password": "sekret"}, lines.append)
        host, kwargs = server.connected
        assert host == "203.0.113.7" and kwargs["username"] == "kuba" and kwargs["port"] == 2222
        assert kwargs["password"] == "sekret" and kwargs["allow_agent"] is False and server.closed
        keys, _uid, install = server.commands
        assert "ssh-rsa AAAAPIPEKEY" in keys.command and keys.stdin == ""
        assert install.command.startswith("sudo -S -p '' bash -c ") and install.stdin == "sekret\n"
        assert conn == {"kind": "server", "host": "203.0.113.7", "user": "kuba", "ssh_port": 2222,
                        "key": str(home / ".pipe" / "keys" / "kuba_203.0.113.7_2222"), "token": "tok123"}
        assert (home / ".pipe" / "keys" / "kuba_203.0.113.7_2222").read_text() == "PRIVATE"
        shown = "\n".join(lines)
        assert "sekret" not in shown and appsetup.TOKEN_MARK not in shown and "\x1b" not in shown
        assert "[pipe] Klonuje" in shown

    def test_root_with_agent_keys_needs_no_sudo_and_no_own_key(self, ssh):
        server = ssh(uid="0")
        conn = appsetup.deploy_server({"host": "203.0.113.7"}, lambda line: None)
        _uid, install = server.commands
        assert install.command.startswith("bash -c ") and conn["key"] == "" and conn["user"] == "root"
        assert server.connected[1]["look_for_keys"] is True and server.connected[1]["password"] is None

    def test_sudo_without_password_explains(self, ssh):
        ssh(install_code=1, install_output="sudo: a password is required\n")
        with pytest.raises(appsetup.SetupError, match="password"):
            appsetup.deploy_server({"host": "203.0.113.7", "user": "kuba"}, lambda line: None)

    def test_rejected_credentials(self, ssh):
        ssh(reject=True)
        with pytest.raises(appsetup.SetupError, match="rejected"):
            appsetup.deploy_server({"host": "203.0.113.7", "password": "zle"}, lambda line: None)

    def test_new_host_key_is_remembered_with_fingerprint(self, ssh, home):
        server = ssh()
        appsetup.deploy_server({"host": "203.0.113.7", "password": "x"}, lambda line: None)
        key = types.SimpleNamespace(get_name=lambda: "ssh-ed25519", get_base64=lambda: "AAAAHOST", asbytes=lambda: b"k")
        lines = []
        appsetup._accept_new_policy(lines.append, "pl").missing_host_key(None, "[203.0.113.7]:2222", key)
        assert "SHA256:" in lines[0]
        assert (home / ".ssh" / "known_hosts").read_text() == "[203.0.113.7]:2222 ssh-ed25519 AAAAHOST\n"
        assert server.policy is not None


class TestBridgeSetupMode:
    """Most bez backendu: strona pyta o widok i miejsce, /api/setup stawia Pipe i przelacza most."""

    class FakeSetup:
        def __init__(self, port, fail=False):
            self.port, self.fail, self.requests = port, fail, []

        def state(self, lang):
            return {"local": "", "paramiko": True, "saved": ""}

        def run(self, request, progress, lang):
            self.requests.append(dict(request))
            progress("krok 1")
            if self.fail:
                raise appsetup.SetupError("nie wyszlo")
            return "127.0.0.1", self.port, "tok", "kuba@vps"

    def _go(self, scenario, fail=False):
        helper = TestWebBridge()

        async def go():
            received = []
            backend = await helper._backend(received, [{"response": "", "status": "ok", "done": True, "data": {"view": "simple"}}])
            setup = self.FakeSetup(backend.sockets[0].getsockname()[1], fail)
            bridge = web.WebBridge(None, 0, "", lang="pl", port=web.free_port(0), setup=setup)
            srv = await asyncio.start_server(bridge.handle, "127.0.0.1", bridge.port)
            try:
                return await scenario(helper, bridge, setup, received)
            finally:
                srv.close(); backend.close()
        return asyncio.run(go())

    def _headers(self, bridge):
        return {"Cookie": f"pipe_key={bridge.key}", "Origin": f"http://127.0.0.1:{bridge.port}"}

    def test_setup_switches_the_bridge_to_the_new_backend(self):
        async def scenario(helper, bridge, setup, received):
            page = await helper._http(bridge.port, "GET", f"/?k={bridge.key}")
            before = await helper._http(bridge.port, "POST", "/api/request", self._headers(bridge), b'{"command": "view"}')
            state = await helper._http(bridge.port, "GET", "/api/app", self._headers(bridge))
            body = json.dumps({"kind": "server", "host": "vps", "password": "sekret", "view": "simple"}).encode()
            result = await helper._http(bridge.port, "POST", "/api/setup", self._headers(bridge), body)
            return page, before, state, result, received
        page, before, state, (status, _, payload), received = self._go(scenario)
        assert b'"setup": true' in page[2] and b'"canSetup": true' in page[2]
        assert before[0] == 503
        assert json.loads(state[2]) == {"setup": True, "canSetup": True, "server": "", "local": "", "paramiko": True, "saved": ""}
        assert status == 200
        lines = [json.loads(line) for line in payload.decode().strip().splitlines()]
        assert lines == [{"progress": "krok 1"}, {"done": True, "ok": True, "server": "kuba@vps"}]
        (view,) = received
        assert view["command"] == "view" and view["args"] == "simple" and view["token"] == "tok"
        assert "sekret" not in json.dumps(view)

    def test_failure_is_reported_and_bridge_stays_in_setup(self):
        async def scenario(helper, bridge, setup, received):
            body = json.dumps({"kind": "local"}).encode()
            result = await helper._http(bridge.port, "POST", "/api/setup", self._headers(bridge), body)
            bad = await helper._http(bridge.port, "POST", "/api/setup", self._headers(bridge), b'{"kind": "ftp"}')
            return result, bad, bridge.setup_mode
        (status, _, payload), bad, still = self._go(scenario, fail=True)
        last = json.loads(payload.decode().strip().splitlines()[-1])
        assert last == {"done": True, "ok": False, "error": "nie wyszlo"} and still and bad[0] == 400

    def test_without_setup_object_there_is_no_setup_endpoint(self):
        helper = TestWebBridge()

        async def scenario(bridge, _received):
            return await helper._http(bridge.port, "POST", "/api/setup",
                                      {"Cookie": f"pipe_key={bridge.key}"}, b'{"kind": "local"}')
        assert helper._run(scenario)[0] == 404


class TestCliView:

    def test_simple_view_hides_technical_commands_from_suggestions(self, monkeypatch):
        pytest.importorskip("rich")
        import cli
        monkeypatch.setattr(cli, "LANG", "pl")
        monkeypatch.setattr(cli, "_view", "simple")
        names = [name for name, _ in cli._slash_options("/", [])]
        assert "zalataj" not in names and "mapa" not in names and "widok" in names and "skille" in names
        assert [name for name, _ in cli._slash_options("/zalataj", [])] == ["zalataj"]     # pelna nazwa dziala
        monkeypatch.setattr(cli, "_view", "tech")
        assert "zalataj" in [name for name, _ in cli._slash_options("/", [])]
