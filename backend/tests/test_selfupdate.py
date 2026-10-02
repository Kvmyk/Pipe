"""
Aktualizacja samego Pipe (core/selfupdate.py, narzedzie pipe_update): kontener pomocniczy,
potwierdzenie, raport po restarcie. Docker jest atrapa — testy nie uruchamiaja niczego na hoscie.
"""

import asyncio
import json

import pytest

from backend.core import reminders, selfupdate
from backend.core.agent import VPSAgent
from backend.tests.fakes import FakeClient, assert_history_valid, completion
from backend.version import VERSION

LABELS = {"com.docker.compose.project": "backend", "com.docker.compose.project.working_dir": "/root/pipe/backend",
          "com.docker.compose.project.config_files": "/root/pipe/backend/docker-compose.yml",
          "com.docker.compose.service": "vps-agent"}


class FakeDocker:
    """Odpowiada na wywolania `docker ...` i `git ...` z selfupdate._run."""

    def __init__(self, tmp_path, *, helpers=(), labels=LABELS, wait="0", logs="PIPE_UPDATE_OK"):
        self.calls, self.helpers, self.labels, self.wait, self.logs = [], list(helpers), labels, wait, logs
        (tmp_path / "root/pipe/.git").mkdir(parents=True)

    async def __call__(self, *args, timeout=30):
        self.calls.append(args)
        if args[:2] == ("docker", "inspect") and "--format" not in args:
            return json.dumps([{"Image": "sha256:abc", "Config": {"Labels": self.labels}}]), 0
        if args[:2] == ("docker", "inspect"):
            return json.dumps({"pipe.update.to": "telegram:7", "pipe.update.from": "0.1.0"}), 0
        if args[:2] == ("docker", "ps") and "label=pipe.update=1" in args:
            return "\n".join(f"{name}\t{state}" for name, state in self.helpers), 0
        if args[:2] == ("docker", "ps"):
            return "vps-agent\ntelegram\nvps-agent", 0
        if args[:2] == ("docker", "run"):
            return "c0ffee", 0
        if args[:2] == ("docker", "wait"):
            return self.wait, 0
        if args[:2] == ("docker", "logs"):
            return self.logs, 0
        if args[0] == "git" and "get-url" in args:
            return "https://github.com/Kvmyk/Pipe.git", 0
        if args[0] == "git" and "ls-remote" in args:
            return "b" * 40 + "\trefs/heads/main", 0
        if args[0] == "git" and "--abbrev-ref" in args:
            return "main", 0
        if args[0] == "git" and "rev-parse" in args:
            return "a" * 40, 0
        return "", 0


@pytest.fixture
def docker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path))
    monkeypatch.setattr(selfupdate, "COMPOSE_BIN", str(tmp_path / "root/pipe/.git"))     # "jest w obrazie"
    fake = FakeDocker(tmp_path)
    monkeypatch.setattr(selfupdate, "_run", fake)
    return fake


class TestLocateAndScript:

    def test_install_comes_from_compose_labels(self, docker):
        install = asyncio.run(selfupdate.locate())
        assert (install.project, install.root, install.workdir) == ("backend", "/root/pipe", "/root/pipe/backend")
        assert install.services == ["telegram", "vps-agent"] and install.image == "sha256:abc"

    def test_script_pulls_then_rebuilds_only_running_services(self, docker):
        text = selfupdate.script(asyncio.run(selfupdate.locate()))
        assert text.startswith("set -e; sleep ") and "git -c safe.directory='*' pull --ff-only" in text
        assert text.index("pull --ff-only") < text.index("up -d --build telegram vps-agent")
        assert "-p backend -f /root/pipe/backend/docker-compose.yml" in text

    def test_values_from_labels_are_quoted(self, docker):
        docker.labels = {**LABELS, "com.docker.compose.project": "x; rm -rf /",
                         "com.docker.compose.project.working_dir": "/root/pi pe/backend"}
        (selfupdate.Path(selfupdate.runtime.to_local("/root/pi pe")) / ".git").mkdir(parents=True)
        text = selfupdate.script(asyncio.run(selfupdate.locate()))
        assert "'x; rm -rf /'" in text and "cd '/root/pi pe'" in text

    def test_refuses_outside_compose_and_outside_docker(self, docker, monkeypatch):
        docker.labels = {}
        with pytest.raises(selfupdate.UpdateError, match="docker compose"):
            asyncio.run(selfupdate.locate())
        monkeypatch.setenv("PIPE_RUNTIME", "native")
        with pytest.raises(selfupdate.UpdateError, match="systemctl restart pipe"):
            asyncio.run(selfupdate.locate())

    def test_old_image_without_compose_explains_the_manual_step(self, docker, monkeypatch):
        monkeypatch.setattr(selfupdate, "COMPOSE_BIN", "/nie/ma")
        with pytest.raises(selfupdate.UpdateError, match="docker compose up -d --build"):
            asyncio.run(selfupdate.locate())

    def test_status_reports_newer_remote_commit(self, docker):
        text = asyncio.run(selfupdate.status())
        assert f"Pipe {VERSION}" in text and "commit aaaaaaa" in text and "bbbbbbb" in text and "telegram, vps-agent" in text


class TestHelperContainer:

    def test_start_mounts_repo_at_the_host_path(self, docker):
        name = asyncio.run(selfupdate.start(asyncio.run(selfupdate.locate()), "telegram:7"))
        run = next(call for call in docker.calls if call[:2] == ("docker", "run"))
        assert name.startswith("pipe-update-") and "/root/pipe:/root/pipe" in run and "sha256:abc" in run
        assert "com.docker.compose.project=" in run and "com.docker.compose.service=" in run   # nie jest czescia projektu
        assert "pipe.update.to=telegram:7" in run and f"pipe.update.from={VERSION}" in run
        assert "/root/.ssh:/root/.ssh:ro" not in run                      # repozytorium po https

    def test_only_one_update_at_a_time_and_old_helpers_are_removed(self, docker):
        install = asyncio.run(selfupdate.locate())
        docker.helpers = [("pipe-update-1", "exited")]
        asyncio.run(selfupdate.start(install, "cli:kuba"))
        assert ("docker", "rm", "-f", "pipe-update-1") in docker.calls
        docker.helpers = [("pipe-update-2", "running")]
        with pytest.raises(selfupdate.UpdateError, match="pipe-update-2"):
            asyncio.run(selfupdate.start(install, "cli:kuba"))

    def test_follow_reports_to_the_requester(self, docker):
        sent = []

        async def notify(interface, text):
            sent.append((interface, text))

        asyncio.run(selfupdate.follow("pipe-update-1", notify, restarted=True))
        assert sent == [("telegram:7", f"Pipe zaktualizowany: 0.1.0 -> {VERSION}. Backend dziala na nowym obrazie.")]
        assert ("docker", "rm", "-f", "pipe-update-1") in docker.calls

    def test_failure_keeps_the_log_tail_without_secrets(self, docker):
        text = selfupdate.result_text(1, "0.1.0", "fatal: Not possible to fast-forward\ntoken sk-abcdefghijklmnopqrstuvwxyz123", False)
        assert "nie powiodla sie (kod 1)" in text and "fast-forward" in text and "sk-abcdefghijklmnop" not in text
        assert "nie wymagal wymiany" in selfupdate.result_text(0, VERSION, "", False)
        assert "przebudowany" in selfupdate.result_text(0, VERSION, "", True)

    def test_resume_delivers_the_result_as_a_reminder(self, docker):
        docker.helpers = [("pipe-update-9", "exited")]

        class Watcher:
            changed = 0

            def reminders_changed(self):
                self.changed += 1

        async def scenario():
            watcher = Watcher()
            await selfupdate.resume(watcher)
            await asyncio.sleep(0.05)
            return watcher

        watcher = asyncio.run(scenario())
        (reminder,) = reminders.load_reminders()
        assert watcher.changed == 1 and reminder.to == "telegram:7" and "Pipe zaktualizowany" in reminder.text


class TestTool:

    def _agent(self, script):
        return VPSAgent(client=FakeClient(script))

    async def _collect(self, generator):
        return [event async for event in generator]

    def test_apply_needs_confirmation_then_starts_the_helper(self, docker, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_PATH", "/dev/null")
        agent = self._agent([completion(tool_calls=[("c1", "pipe_update", {"operation": "apply"})]), completion("Ruszam.")])

        async def scenario():
            asked = await self._collect(agent.chat("s", "zaktualizuj sie", "telegram:7"))
            before = [call for call in docker.calls if call[:2] == ("docker", "run")]
            done = await self._collect(agent.confirm("s", True))
            return asked, before, done

        asked, before, done = asyncio.run(scenario())
        assert "[POTWIERDZ]" in asked[-1] and "wymaga potwierdzenia" in asked[-1] and "/root/pipe" in asked[-1]
        assert before == [] and any(call[:2] == ("docker", "run") for call in docker.calls)
        session = agent.get_or_create_session("s")
        assert_history_valid(session.messages)
        tool = next(m for m in session.messages if m.get("role") == "tool")
        assert "pipe-update-" in tool["content"] and done[-1] == "Ruszam."

    def test_declined_update_starts_nothing(self, docker):
        agent = self._agent([completion(tool_calls=[("c1", "pipe_update", {"operation": "apply"})]), completion("OK")])

        async def scenario():
            await self._collect(agent.chat("s", "zaktualizuj sie", "cli:kuba"))
            await self._collect(agent.confirm("s", False))

        asyncio.run(scenario())
        assert not any(call[:2] == ("docker", "run") for call in docker.calls)

    def test_viewer_cannot_update(self, docker):
        agent = self._agent([completion(tool_calls=[("c1", "pipe_update", {"operation": "apply"})]), completion("Nie moge.")])
        events = asyncio.run(self._collect(agent.chat("s", "zaktualizuj sie", "telegram:9", role="viewer")))
        assert not any("[POTWIERDZ]" in str(event) for event in events)
        assert agent.get_or_create_session("s").pending_confirmation is None
        assert not any(call[:2] == ("docker", "run") for call in docker.calls)

    def test_check_changes_nothing(self, docker):
        agent = self._agent([completion(tool_calls=[("c1", "pipe_update", {"operation": "check"})]), completion("Jest nowsza.")])
        asyncio.run(self._collect(agent.chat("s", "jaka wersja?", "cli:kuba")))
        tool = next(m for m in agent.get_or_create_session("s").messages if m.get("role") == "tool")
        assert "dostepna aktualizacja" in tool["content"] and not any(call[:2] == ("docker", "run") for call in docker.calls)
