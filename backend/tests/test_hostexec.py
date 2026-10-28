"""
Komendy na hoscie z trybu docker (core/hostexec.py): kontener pomocniczy z obrazu Pipe + nsenter do procesu 1.
Zawsze po TAK, klasyfikowana i pokazywana jest komenda docelowa, nigdy w trybie obserwacji.
"""

import asyncio
import shlex

import pytest

from backend.core import hostexec, runtime, safety
from backend.core.agent import VPSAgent
from backend.core.security import classify_command
from backend.tests.fakes import FakeClient, assert_history_valid, completion

IMAGE = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.core import audit

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(hostexec, "_image", IMAGE)
    monkeypatch.setattr(runtime, "observe", lambda: False)


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


def texts(events):
    return [e for e in events if isinstance(e, str)]


class TestWrap:

    def test_command_is_one_quoted_argument(self):
        command = "apt-get install -y openssl; echo 'a b' $HOME"
        tokens = shlex.split(hostexec.wrap(command, IMAGE, "/srv/my app"))
        assert tokens[:6] == ["docker", "run", "--rm", "--privileged", "--pid=host", "--network=host"]
        at = tokens.index("--entrypoint")
        assert tokens[at + 1:at + 3] == ["nsenter", IMAGE]
        assert tokens[-3:-1] == ["sh", "-c"] and tokens[-1] == "cd '/srv/my app' 2>/dev/null; " + command
        assert shlex.split(hostexec.wrap("ls", IMAGE, "/"))[-1] == "ls"

    def test_bad_image_never_reaches_the_shell(self):
        for bad in ("x; true", "--privileged", "$(id)", ""):
            with pytest.raises(ValueError):
                hostexec.wrap("ls", bad)

    def test_wrapped_inverse_is_not_forbidden(self):
        assert classify_command(hostexec.wrap("docker tag abc nginx:1 && docker compose -p shop up -d", IMAGE)) == "confirm"

    def test_host_plan_moves_checks_and_inverse(self):
        plan = safety.Plan(pre=[safety.Check("compose", "shell", "docker compose -p shop config -q")],
                           post=[safety.Check("kontenery", "compose", target="-p shop"),
                                 safety.Check("kontener", "container", target="web")],
                           inverse=["docker compose -p shop up -d"])
        moved = hostexec.host_plan(plan, IMAGE)
        assert moved.pre[0].command == hostexec.wrap("docker compose -p shop config -q", IMAGE)
        assert moved.post[0].kind == "shell" and "docker compose -p shop ps -a" in moved.post[0].command
        assert moved.post[1].kind == "container"                           # docker inspect dziala z kontenera
        assert moved.inverse == [hostexec.wrap("docker compose -p shop up -d", IMAGE)]
        assert any("nsenter" in n for n in moved.notes)


class TestBlocker:

    def test_only_docker_without_observe_and_with_image(self, monkeypatch):
        monkeypatch.setattr(runtime, "kind", lambda: "docker")
        assert asyncio.run(hostexec.blocker()) == ""
        monkeypatch.setattr(runtime, "observe", lambda: True)
        assert "PIPE_OBSERVE" in asyncio.run(hostexec.blocker())
        monkeypatch.setattr(runtime, "observe", lambda: False)
        monkeypatch.setattr(runtime, "kind", lambda: "kubernetes")
        assert "docker" in asyncio.run(hostexec.blocker())
        monkeypatch.setattr(runtime, "kind", lambda: "docker")
        monkeypatch.setattr(hostexec, "_image", None)

        async def no_docker(*args, **kwargs):
            return "Cannot connect to the Docker daemon", 1

        monkeypatch.setattr(hostexec, "_run", no_docker)
        assert "docker.sock" in asyncio.run(hostexec.blocker())


class TestExecuteCommandOnHost:

    def agent(self, args):
        return VPSAgent(client=FakeClient([completion(tool_calls=[("c1", "execute_command", args)]), completion("ok")]))

    def test_confirmation_shows_the_host_command_and_runs_the_wrapper(self, monkeypatch):
        from backend.core import executor

        monkeypatch.setattr(runtime, "kind", lambda: "docker")
        agent = self.agent({"command": "systemctl restart nginx", "host": True})
        confirm = next(e for e in texts(run(agent.chat("s", "zrestartuj nginx"))) if e.startswith("[POTWIERDZ]"))
        first = confirm.splitlines()[0]
        assert first.startswith("[POTWIERDZ] Komenda na hoscie wymaga potwierdzenia") and "systemctl restart nginx" in first
        assert "docker run" not in first and "nsenter" in confirm
        request = agent._sessions["s"].pending_confirmation
        assert request.command == hostexec.wrap("systemctl restart nginx", IMAGE, agent._sessions["s"].cwd)
        ran = []

        async def fake_execute(cmd, cwd=None, timeout=None):
            ran.append(cmd)
            return "", "", 0

        monkeypatch.setattr(executor, "execute", fake_execute)
        run(agent.confirm("s", True))
        # pelny bezpiecznik na hoscie: nginx -t przed, restart, `systemctl is-active` po — wszystko przez pomocnika
        cwd = agent._sessions["s"].cwd
        order = [ran.index(hostexec.wrap("nginx -t", IMAGE, cwd)), ran.index(request.command),
                 ran.index(hostexec.wrap("systemctl is-active --quiet nginx", IMAGE, cwd))]
        assert order == sorted(order)
        assert "nginx -t" in confirm and "usluga nginx aktywna" in confirm
        assert_history_valid(agent._sessions["s"].messages)

    def test_reads_also_need_yes_and_forbidden_is_refused(self, monkeypatch):
        monkeypatch.setattr(runtime, "kind", lambda: "docker")
        agent = self.agent({"command": "cat /etc/hostname", "host": True})
        assert any(e.startswith("[POTWIERDZ]") for e in texts(run(agent.chat("s", "x"))))
        agent = self.agent({"command": "mkfs.ext4 /dev/sda1", "host": True})
        assert any(e.startswith("[ODMOWA]") for e in texts(run(agent.chat("s", "x"))))
        assert agent._sessions["s"].pending_confirmation is None

    def test_observe_mode_refuses_without_asking(self, monkeypatch):
        monkeypatch.setattr(runtime, "kind", lambda: "docker")
        monkeypatch.setattr(runtime, "observe", lambda: True)
        agent = self.agent({"command": "apt-get install -y htop", "host": True})
        assert not any(e.startswith("[POTWIERDZ]") for e in texts(run(agent.chat("s", "x"))))
        tool = next(m for m in agent._sessions["s"].messages if m["role"] == "tool")
        assert "PIPE_OBSERVE" in tool["content"]

    def test_native_ignores_the_flag(self, monkeypatch):
        monkeypatch.setattr(runtime, "kind", lambda: "native")
        agent = self.agent({"command": "mkdir -p /tmp/x", "host": True})
        run(agent.chat("s", "x"))
        assert "docker run" not in agent._sessions["s"].pending_confirmation.command
