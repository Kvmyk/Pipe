"""
Testy bezpiecznika (plan zmiany, sprawdzenia, weryfikacja, automatyczne
przywrocenie) i dziennika zmian z cofaniem (v0.11).
"""

import asyncio
import json
import os

import pytest

from backend.config import settings
from backend.core import executor, journal, safety
from backend.core.infra import Container


@pytest.fixture(autouse=True)
def native(tmp_path, monkeypatch):
    """Tryb native: sciezki hosta = sciezki procesu (pliki w tmp_path)."""
    monkeypatch.setenv("PIPE_RUNTIME", "native")
    monkeypatch.setenv("HOST_ROOT", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path


class FakeShell:
    """Podmiana executor.execute: zapisuje komendy, odpowiada wedlug regul."""

    def __init__(self, rules=None):
        self.calls: list[str] = []
        self.rules = rules or {}

    async def __call__(self, cmd, cwd=None, timeout=None):
        self.calls.append(cmd)
        for needle, result in self.rules.items():
            if needle in cmd:
                return result() if callable(result) else result
        return "Komenda wykonana bez outputu", "", 0


def run(coro):
    return asyncio.run(coro)


async def collect(gen):
    return [item async for item in gen]


# ─── Plan ───────────────────────────────────────────────────────────────────

class TestPlanCommand:

    def test_sed_in_place_backs_up_files_relative_to_cwd(self, native):
        plan = safety.plan_command("sed -i 's/a/b/' app.conf other.conf", cwd_host="/srv/app")
        assert plan.files == ["/srv/app/app.conf", "/srv/app/other.conf"]
        plan = safety.plan_command("sed -i -e 's/a/b/' -e 's/c/d/' /etc/x.conf")
        assert plan.files == ["/etc/x.conf"]

    def test_redirects_tee_and_rm(self):
        plan = safety.plan_command("echo 1 > /etc/a && printf x >> /etc/b 2>/dev/null | tee -a /etc/c; rm -f /tmp/d")
        assert plan.files == ["/etc/a", "/etc/b", "/etc/c", "/tmp/d"]
        assert safety.plan_command("rm -rf /srv/old").notes

    def test_cp_into_directory_and_mv(self, native):
        target = native / "dir"
        target.mkdir()
        plan = safety.plan_command(f"cp a.txt b.txt {target}", cwd_host=str(native))
        assert plan.files == [str(target / "a.txt"), str(target / "b.txt")]
        plan = safety.plan_command("mv /etc/x.conf /etc/x.conf.old")
        assert plan.files == ["/etc/x.conf", "/etc/x.conf.old"]

    def test_systemctl_restart_nginx(self):
        plan = safety.plan_command("systemctl restart nginx")
        assert [c.command for c in plan.pre] == ["nginx -t"]
        assert plan.post[0].kind == "active" and plan.post[0].target == "nginx"
        assert plan.reload_after_restore == ["systemctl restart nginx"] and plan.sites

    def test_systemctl_inverse(self):
        assert safety.plan_command("systemctl stop redis").inverse == ["systemctl start redis"]
        assert safety.plan_command("systemctl disable --now cron").inverse == ["systemctl enable cron"]
        assert safety.plan_command("service nginx stop").inverse == ["systemctl start nginx"]

    def test_sshd_restart_validated(self):
        assert [c.command for c in safety.plan_command("systemctl restart ssh").pre] == ["sshd -t"]

    def test_docker_stop_start_restart_rm(self):
        proxy = Container("proxy", "nginx:1.27", "running")
        plan = safety.plan_command("docker stop web && docker restart proxy", containers=[proxy])
        assert plan.inverse == ["docker start web"]
        assert [c.target for c in plan.post] == ["proxy"] and plan.sites
        assert "nie odtworze" in safety.plan_command("docker rm web").notes[0]

    def test_docker_exec_nginx_reload(self):
        plan = safety.plan_command("docker exec -it proxy nginx -s reload")
        assert plan.pre[0].command == "docker exec proxy nginx -t"
        assert plan.reload_after_restore == ["docker exec -it proxy nginx -s reload"]

    def test_compose_up(self):
        plan = safety.plan_command("docker compose -f /srv/app/compose.yml up -d --build")
        assert plan.pre[0].command == "docker compose -f /srv/app/compose.yml config -q"
        assert plan.post[0].kind == "compose" and plan.post[0].target == "-f /srv/app/compose.yml"
        assert safety.plan_command("docker compose down").inverse == ["docker compose up -d"]
        assert any("wolumeny" in n for n in safety.plan_command("docker compose down -v").notes)

    def test_git_and_packages_and_crontab(self, native):
        plan = safety.plan_command("git -C /srv/app pull")
        assert plan.git_repos == ["/srv/app"]
        assert safety.plan_command("git -C /srv/app status").git_repos == []
        assert "pakietow nie cofam" in safety.plan_command("apt-get install -y htop").notes[0]
        assert safety.plan_command("{ crontab -l; printf '%s\\n' '* * * * * x'; } | crontab -").crontab

    def test_nginx_config_edit_validated_and_auto_restored(self):
        plan = safety.plan_command("sed -i 's/80/8080/' /etc/nginx/sites-enabled/shop && systemctl reload nginx")
        assert plan.files == ["/etc/nginx/sites-enabled/shop"]
        assert "nginx -t" in [c.command for c in plan.pre] and "nginx -t" in [c.command for c in plan.post]
        assert plan.auto_restore and plan.reload_after_restore == ["systemctl reload nginx"]

    def test_docker_mode_validates_through_container_mount(self, monkeypatch):
        monkeypatch.setenv("PIPE_RUNTIME", "docker")
        monkeypatch.delenv("HOST_ROOT")
        proxy = Container("proxy", "nginx:1.27", "running", mounts=[("/etc/nginx", "/etc/nginx")])
        plan = safety.plan_write("/etc/nginx/conf.d/app.conf", [proxy])
        assert plan.post[0].command == "docker exec proxy nginx -t" and plan.auto_restore
        without = safety.plan_write("/etc/nginx/conf.d/app.conf", [])
        assert without.post == [] and "nie sprawdze" in without.notes[0]

    def test_compose_and_json_writes(self):
        assert safety.plan_write("/srv/app/docker-compose.yml").post[0].command.endswith("config -q")
        assert safety.plan_write("/etc/docker/daemon.json").post[0].kind == "json"

    def test_describe(self):
        text = safety.plan_command("sed -i 's/a/b/' /etc/nginx/nginx.conf && systemctl reload nginx").describe()
        assert text.startswith("Bezpiecznik:") and "kopia przed zmiana: /etc/nginx/nginx.conf" in text
        assert "sprawdzenie przed" in text and "przywroce pliki" in text
        assert safety.plan_command("uptime").describe() == ""


# ─── Dziennik ───────────────────────────────────────────────────────────────

class TestJournal:

    def test_backup_restore_and_new_file_removed(self, native):
        existing = native / "app.conf"
        existing.write_text("stare\n")
        os.chmod(existing, 0o640)
        created = native / "nowy.conf"
        entry = run(journal.begin("cli", "execute_command", "sed ...",
                                  files=[(str(existing), str(existing)), (str(created), str(created))]))
        existing.write_text("nowe\n")
        os.chmod(existing, 0o600)
        created.write_text("x")
        assert "-nowe" in journal.preview(entry) and "zostanie usuniety" in journal.preview(entry)
        report = journal.restore_files(entry)
        assert existing.read_text() == "stare\n" and oct(existing.stat().st_mode & 0o777) == "0o640"
        assert not created.exists() and len(report) == 2

    def test_directories_and_big_files_are_skipped(self, native, monkeypatch):
        monkeypatch.setattr(journal, "MAX_FILE_BYTES", 10)
        big = native / "big"
        big.write_text("x" * 100)
        entry = run(journal.begin("cli", "t", "c", files=[(str(native), "katalog"), (str(big), "big")]))
        assert [f.skipped.split(" ")[0] for f in entry.files] == ["katalog", "plik"]
        assert not entry.undoable

    def test_rollback_runs_inverse_and_can_be_undone(self, native, monkeypatch):
        shell = FakeShell()
        monkeypatch.setattr(executor, "execute", shell)
        path = native / "a.conf"
        path.write_text("v1")
        entry = run(journal.begin("cli", "execute_command", "docker stop web; sed",
                                  files=[(str(path), str(path))], inverse=["docker start web"]))
        journal.finish(entry, "done", 0)
        path.write_text("v2")
        text = run(journal.rollback(entry, "cli:kuba"))
        assert path.read_text() == "v1" and "docker start web" in shell.calls
        assert journal.load(entry.id).status == "rolled_back"
        redo_id = text.split("jako #")[1].split(" ")[0]
        redo = journal.load(redo_id)
        assert redo.undo_of == entry.id
        run(journal.rollback(redo, "cli:kuba"))
        assert path.read_text() == "v2"

    def test_forbidden_inverse_is_refused(self, native, monkeypatch):
        shell = FakeShell()
        monkeypatch.setattr(executor, "execute", shell)
        entry = run(journal.begin("cli", "t", "c", inverse=["rm -rf /"]))
        text = run(journal.rollback(entry, "cli"))
        assert "ODMOWA" in text and shell.calls == []

    def test_git_head_captured(self, monkeypatch):
        monkeypatch.setattr(executor, "execute", FakeShell({
            "rev-parse HEAD": ("abc123\n", "", 0), "symbolic-ref": ("main\n", "", 0)}))
        entry = run(journal.begin("cli", "git_command", "git pull", git_repos=["/srv/app"]))
        assert entry.inverse == ["git -C /srv/app checkout main", "git -C /srv/app reset --keep abc123"]

    def test_ids_are_validated(self):
        with pytest.raises(ValueError):
            journal.load("../../etc")
        assert journal.load("deadbeef") is None

    def test_prune_and_latest(self, native, monkeypatch):
        monkeypatch.setattr(journal, "MAX_ENTRIES", 3)
        ids = []
        for i in range(5):
            entry = run(journal.begin("cli", "t", f"c{i}", inverse=["docker start x"]))
            journal.finish(entry, "done", 0)
            ids.append(entry.id)
        assert len(journal.entries()) == 3
        assert journal.latest_undoable().command == "c4"


# ─── Strzezone wykonanie ────────────────────────────────────────────────────

def guarded(plan, operation, **kwargs):
    return run(collect(safety.guarded(plan, operation, interface="cli", tool="execute_command",
                                      description="cmd", sites_enabled=False, **kwargs)))


class TestGuarded:

    def test_failed_precheck_blocks_operation(self, monkeypatch):
        monkeypatch.setattr(executor, "execute", FakeShell({"nginx -t": ("", "emerg: unknown directive", 1)}))
        called = []

        async def operation():
            called.append(True)
            return "ok", 0

        plan = safety.Plan(pre=[safety.Check("nginx -t", "shell", "nginx -t")])
        items = guarded(plan, operation)
        outcome = items[-1]
        assert called == [] and outcome.text.startswith("NIE WYKONANO") and "unknown directive" in outcome.text
        assert journal.load(outcome.entry_id).status == "aborted"

    def test_failed_validation_restores_file_and_reloads(self, native, monkeypatch):
        config = native / "site.conf"
        config.write_text("dobra konfiguracja\n")
        shell = FakeShell({"nginx -t": lambda: ("", "emerg", 1) if "zla" in config.read_text() else ("ok", "", 0)})
        monkeypatch.setattr(executor, "execute", shell)

        async def operation():
            config.write_text("zla konfiguracja\n")
            return "zapisano", 0

        plan = safety.Plan(files=[str(config)], post=[safety.Check("nginx -t", "shell", "nginx -t")],
                           auto_restore=True, reload_after_restore=["systemctl reload nginx"])
        outcome = guarded(plan, operation)[-1]
        assert config.read_text() == "dobra konfiguracja\n"
        assert "WERYFIKACJA NIE PRZESZLA" in outcome.text and "Przywrocilem pliki" in outcome.text
        assert "systemctl reload nginx" in shell.calls
        assert journal.load(outcome.entry_id).status == "restored"
        assert not journal.load(outcome.entry_id).undoable     # juz odwrocone — /cofnij wybierze wczesniejsza zmiane

    def test_auto_restore_can_be_disabled(self, native, monkeypatch):
        config = native / "site.conf"
        config.write_text("stara")
        monkeypatch.setattr(executor, "execute", FakeShell({"nginx -t": ("", "emerg", 1)}))

        async def operation():
            config.write_text("nowa")
            return "zapisano", 0

        plan = safety.Plan(files=[str(config)], post=[safety.Check("nginx -t", "shell", "nginx -t")], auto_restore=True)
        outcome = guarded(plan, operation, auto_restore=False)[-1]
        assert config.read_text() == "nowa" and f"/cofnij {outcome.entry_id}" in outcome.text

    def test_success_is_reported_and_missing_tools_skipped(self, monkeypatch):
        monkeypatch.setattr(executor, "execute", FakeShell({"sshd -t": ("", "sh: sshd: not found", 127),
                                                            "is-active": ("active\n", "", 0)}))

        async def operation():
            return "EXIT CODE: 0", 0

        plan = safety.Plan(pre=[safety.Check("sshd -t", "shell", "sshd -t")],
                           post=[safety.Check("usluga ssh aktywna", "active", target="ssh")])
        items = guarded(plan, operation)
        assert [i.text for i in items[:-1]] == ["sprawdzam przed zmiana: sshd -t", "weryfikuje: usluga ssh aktywna"]
        assert "Zweryfikowano: usluga ssh aktywna" in items[-1].text and "Dziennik zmian: #" in items[-1].text

    def test_container_check_retries(self, monkeypatch):
        states = iter(["running starting", "running healthy"])
        monkeypatch.setattr(executor, "execute", FakeShell({"docker inspect": lambda: (next(states), "", 0)}))
        check = safety.Check("web", "container", target="web", retries=3, delay=0)
        assert run(safety.run_check(check)) == ("ok", "")

    def test_compose_check_reports_bad_containers(self, monkeypatch):
        monkeypatch.setattr(executor, "execute", FakeShell({"compose": ("app-web-1\trunning\t\napp-db-1\texited\t\n", "", 0)}))
        status, detail = run(safety.run_check(safety.Check("c", "compose", target="-p app", retries=0)))
        assert status == "fail" and "app-db-1 exited" in detail

    def test_json_check(self, native):
        good, bad = native / "a.json", native / "b.json"
        good.write_text('{"a": 1}')
        bad.write_text("{zle")
        assert run(safety.run_check(safety.Check("j", "json", target=str(good))))[0] == "ok"
        assert run(safety.run_check(safety.Check("j", "json", target=str(bad))))[0] == "fail"

    def test_sites_compared_before_and_after(self, monkeypatch):
        from backend.core import checks

        async def domains(ignore=frozenset()):
            return ["a.example.com", "b.example.com"]

        phase = {"after": False}

        async def site(domain):
            broken = phase["after"] and domain == "b.example.com"
            return checks.SiteResult(domain, 502 if broken else 200)

        monkeypatch.setattr(checks, "discover_domains", domains)
        monkeypatch.setattr(checks, "check_site", site)
        real_sleep = asyncio.sleep
        monkeypatch.setattr(safety.asyncio, "sleep", lambda s: real_sleep(0))

        async def operation():
            phase["after"] = True
            return "ok", 0

        items = run(collect(safety.guarded(safety.Plan(sites=True), operation, interface="cli", tool="t",
                                           description="d", sites_enabled=True)))
        assert "strona b.example.com dzialala przed zmiana" in items[-1].text


# ─── Przebieg przez agenta ──────────────────────────────────────────────────

class TestAgentFlow:

    def test_write_file_journaled_and_undone_via_tool(self, native, monkeypatch):
        from backend.core.agent import VPSAgent
        from backend.tests.fakes import FakeClient, assert_history_valid, completion

        monkeypatch.setattr(settings, "VIBE_EVERY", 0)
        monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(native / "audit.log"), raising=False)
        from backend.core import audit
        monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(native / "audit.log"))
        target = native / "app.json"
        target.write_text('{"v": 1}')
        client = FakeClient([
            completion(tool_calls=[("c1", "write_file", {"path": str(target), "content": '{"v": 2}'})]),
            completion("zapisalem"),
            completion(tool_calls=[("c2", "journal", {"operation": "undo"})]),
            completion("cofnalem"),
        ])
        agent = VPSAgent(client=client)

        async def talk():
            first = [e async for e in agent.chat("s", "zmien v na 2", "cli:kuba")]
            after = [e async for e in agent.confirm("s", True)]
            undo = [e async for e in agent.chat("s", "cofnij to", "cli:kuba")]
            done = [e async for e in agent.confirm("s", True)]
            return first, after, undo, done

        first, after, undo, done = run(talk())
        assert "Bezpiecznik:" in first[0] and "poprawny JSON" in first[0]
        tool_result = next(m["content"] for m in agent._sessions["s"].messages if m.get("tool_call_id") == "c1")
        assert "Zweryfikowano: poprawny JSON" in tool_result and "Dziennik zmian: #" in tool_result
        assert "[POTWIERDZ] Cofniecie zmiany" in undo[0] and '+{"v": 1}' in undo[0]
        assert json.loads(target.read_text()) == {"v": 1}
        assert_history_valid(agent._sessions["s"].messages)

    def test_invalid_json_write_restored_automatically(self, native, monkeypatch):
        from backend.core.agent import VPSAgent
        from backend.tests.fakes import FakeClient, completion

        monkeypatch.setattr(settings, "VIBE_EVERY", 0)
        from backend.core import audit
        monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(native / "audit.log"))
        target = native / "daemon.json"
        target.write_text('{"log-driver": "json-file"}')
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "write_file", {"path": str(target), "content": '{"log-driver": '})]),
            completion("ups")]))

        async def talk():
            [e async for e in agent.chat("s", "zmien", "cli")]
            return [e async for e in agent.confirm("s", True)]

        events = run(talk())
        assert json.loads(target.read_text()) == {"log-driver": "json-file"}
        result = next(m["content"] for m in agent._sessions["s"].messages if m.get("tool_call_id") == "c1")
        assert "WERYFIKACJA NIE PRZESZLA" in result and "Przywrocilem pliki" in result
        assert any(getattr(e, "text", "").startswith("weryfikuje") for e in events)


# ─── Protokol i klienci ─────────────────────────────────────────────────────

class TestServerUndo:

    def test_preview_then_execute(self, native, monkeypatch):
        import backend.server as server
        from backend.tests.test_server_commands import FakeAgent, exchange

        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(server, "get_agent", lambda: FakeAgent())
        from backend.core import audit
        monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(native / "audit.log"))
        path = native / "x.conf"
        path.write_text("v1")
        entry = run(journal.begin("cli", "write_file", "write_file(x)", files=[(str(path), str(path))]))
        journal.finish(entry, "done", 0)
        path.write_text("v2")
        [[listing], [preview], [missing_id], [done]] = exchange([
            {"command": "journal"},
            {"command": "undo"},
            {"command": "undo", "execute": True},
            {"command": "undo", "id": entry.id, "execute": True},
        ])
        assert listing["data"]["entries"][0]["id"] == entry.id
        assert preview["data"]["id"] == entry.id and "+v1" in preview["data"]["preview"]
        assert missing_id["status"] == "error"
        assert "Cofnieto" in done["data"]["text"] and path.read_text() == "v1"


def test_clients_know_new_commands():
    import sys
    from pathlib import Path

    from backend.core.memory import RESERVED_COMMANDS

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
    from tg_format import BUILTIN_COMMANDS

    assert {"cofnij", "dziennik"} <= RESERVED_COMMANDS
    assert {"cofnij", "dziennik"} <= {c for c, _ in BUILTIN_COMMANDS}
