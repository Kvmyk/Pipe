"""
Testy change_directory i network_info.

Session.cwd trzyma sciezke HOSTA (bez /hostfs), a komendy dostaja cwd=/hostfs{cwd}.
network_info czyta gniazda hosta z /hostproc/1/net, nie `ss` z kontenera.
"""

import asyncio

import pytest

from backend.core.handlers import system
from backend.core.handlers.system import handle_change_directory, host_path, parse_proc_net
from backend.core.session import Session


class FakeCall:
    id = "call_1"


def run(handler, args, session=None):
    session = session or Session(session_id="t")

    async def collect():
        return [c async for c in handler(None, session, FakeCall(), args)]

    return asyncio.run(collect()), session


class TestHostPath:
    @pytest.mark.parametrize("path,cwd,expected", [
        ("/var/www", "/", "/var/www"),
        ("/hostfs/var/www", "/", "/var/www"),
        ("/hostfs", "/etc", "/"),
        ("/hostfs/", "/etc", "/"),
        ("nginx", "/etc", "/etc/nginx"),
        ("..", "/etc/nginx", "/etc"),
        ("../..", "/", "/"),
        ("/hostfsx", "/", "/hostfsx"),
    ])
    def test_host_path(self, path, cwd, expected):
        assert host_path(path, cwd) == expected


class TestChangeDirectory:
    @pytest.fixture
    def hostfs(self, tmp_path, monkeypatch):
        (tmp_path / "var" / "www").mkdir(parents=True)
        real_isdir = system.os.path.isdir
        monkeypatch.setattr(
            system.os.path, "isdir",
            lambda p: real_isdir(str(tmp_path) + p[len("/hostfs"):]) if p.startswith("/hostfs") else real_isdir(p),
        )
        monkeypatch.setattr(system, "validate_workspace_access", lambda p: (True, ""))

    @pytest.mark.parametrize("path", ["/var/www", "/hostfs/var/www"])
    def test_stores_host_path(self, hostfs, path):
        chunks, session = run(handle_change_directory, {"path": path})
        assert chunks == []
        assert session.cwd == "/var/www"
        assert len(session.messages) == 1

    def test_relative(self, hostfs):
        session = Session(session_id="t", cwd="/var")
        run(handle_change_directory, {"path": "www"}, session)
        assert session.cwd == "/var/www"

    def test_missing_keeps_cwd(self, hostfs):
        _, session = run(handle_change_directory, {"path": "/nope"})
        assert session.cwd == "/"
        assert "nie istnieje" in session.messages[0]["content"]

    def test_blocked_path(self):
        _, session = run(handle_change_directory, {"path": "/root/.ssh"})
        assert session.cwd == "/"
        assert session.messages[0]["content"].startswith("Błąd")

    def test_empty(self):
        _, session = run(handle_change_directory, {"path": "  "})
        assert session.cwd == "/"
        assert len(session.messages) == 1


TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1
   1: 0100007F:1CD3 0100007F:9C40 01 00000000:00000000 00:00000000 00000000     0        0 2 1
"""
TCP6 = """  sl  local_address rem_address   st
   0: 00000000000000000000000000000000:0050 00000000000000000000000000000000:0000 0A
   1: 00000000000000000000000001000000:0019 00000000000000000000000000000000:0000 0A
"""


class TestParseProcNet:
    def test_tcp_listen(self):
        assert parse_proc_net(TCP, "tcp") == ["tcp   0.0.0.0:22"]

    def test_tcp_established(self):
        assert parse_proc_net(TCP, "tcp", established=True) == ["tcp   127.0.0.1:7379 -> 127.0.0.1:40000"]

    def test_tcp6(self):
        assert parse_proc_net(TCP6, "tcp6") == ["tcp6  [::]:80", "tcp6  [::1]:25"]

    def test_udp_has_no_state_filter(self):
        udp = "  sl  local rem st\n   0: 00000000:0035 00000000:0000 00\n"
        assert parse_proc_net(udp, "udp") == ["udp   0.0.0.0:53"]
        assert parse_proc_net(udp, "udp", established=True) == []


class TestNetworkInfo:
    def test_reads_host_proc(self, tmp_path, monkeypatch):
        net = tmp_path / "1" / "net"
        net.mkdir(parents=True)
        (net / "tcp").write_text(TCP)
        monkeypatch.setattr(system, "HOSTPROC", str(tmp_path))
        _, session = run(system.handle_network_info, {"check_type": "listeners"})
        content = session.messages[0]["content"]
        assert "HOSTA" in content and "0.0.0.0:22" in content

    def test_target_is_quoted(self, monkeypatch):
        seen = []

        async def fake_execute(cmd, cwd=None):
            seen.append(cmd)
            return "", "", 0

        monkeypatch.setattr(system.executor, "execute", fake_execute)
        run(system.handle_network_info, {"check_type": "dns", "target": "x; rm -rf /"})
        assert seen == ["getent ahosts 'x; rm -rf /'"]

    def test_missing_target(self):
        _, session = run(system.handle_network_info, {"check_type": "ping"})
        assert "target" in session.messages[0]["content"]
