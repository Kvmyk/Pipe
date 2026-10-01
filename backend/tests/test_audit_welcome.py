"""
Testy audytu bezpieczenstwa, straznika zmian bezpieczenstwa, logu SSH,
powitania po instalacji i skilli wbudowanych (v0.12).
"""

import asyncio
import os
import stat
import time
from collections import Counter
from pathlib import Path

import pytest

from backend.core import memory, posture, snapshots, watch, welcome
from backend.core.hostinfo import Socket
from backend.core.infra import Container, PortMap


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    """Tryb docker: host pod tmp_path/host (jak /hostfs)."""
    root = tmp_path / "host"
    root.mkdir()
    monkeypatch.setenv("HOST_ROOT", str(root))
    monkeypatch.setenv("HOST_PROC", str(tmp_path / "proc"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return root


def write(root, path, text, mode=None):
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if mode is not None:
        os.chmod(target, mode)
    return target


def run(coro):
    return asyncio.run(coro)


# ─── SSH ────────────────────────────────────────────────────────────────────

class TestSshd:

    def test_first_value_wins_with_include_and_match(self, host):
        write(host, "/etc/ssh/sshd_config", "Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication yes\n"
                                           "PermitRootLogin no\nMatch User deploy\n  PasswordAuthentication yes\n")
        write(host, "/etc/ssh/sshd_config.d/50-cloud-init.conf", "PasswordAuthentication no\n")
        settings = posture.sshd_settings()
        assert settings["passwordauthentication"] == "no" and settings["permitrootlogin"] == "no"

    def test_password_login_with_keys_gets_dropin_fix(self, host):
        write(host, "/etc/ssh/sshd_config", "Include /etc/ssh/sshd_config.d/*.conf\nUsePAM yes\n")
        write(host, "/root/.ssh/authorized_keys", "ssh-ed25519 AAAA kuba\n")
        audit = posture.Audit()
        assert posture.ssh_findings(audit, {"root": "uid=0 shell=/bin/bash"})
        finding = next(f for f in audit.findings if f.id == "ssh-password")
        assert finding.severity == "high" and finding.host_only
        assert "/etc/ssh/sshd_config.d/00-pipe-password.conf" in finding.command
        assert "sshd -t && systemctl reload ssh" in finding.command

    def test_password_login_without_keys_has_no_command(self, host):
        write(host, "/etc/ssh/sshd_config", "PasswordAuthentication yes\n")
        audit = posture.Audit()
        posture.ssh_findings(audit, {"root": "x"})
        finding = audit.findings[0]
        assert finding.command == "" and "NAJPIERW" in finding.fix

    def test_keys_only_and_root_yes(self, host):
        write(host, "/etc/ssh/sshd_config", "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
                                           "PermitRootLogin yes\n")
        audit = posture.Audit()
        assert not posture.ssh_findings(audit, {})
        assert [f.id for f in audit.findings] == ["ssh-root"] and "SSH tylko kluczem" in audit.passed
        assert audit.findings[0].command.startswith("sed -i -e 's/^#\\?PermitRootLogin\\b.*/PermitRootLogin prohibit-password/'")

    def test_no_sshd(self):
        audit = posture.Audit()
        assert not posture.ssh_findings(audit, {})
        assert audit.unknown


# ─── Pozostale sprawdzenia ──────────────────────────────────────────────────

class TestAuditChecks:

    def test_firewall_detection(self, host):
        assert posture.firewall_active(set()) == ""
        write(host, "/etc/iptables/rules.v4", "*filter\n:INPUT DROP [0:0]\nCOMMIT\n")
        assert posture.firewall_active(set()) == "iptables"
        write(host, "/etc/ufw/ufw.conf", "ENABLED=yes\n")
        assert posture.firewall_active(set()) == "ufw"
        audit = posture.Audit()
        posture.firewall_findings(audit, set(), "2222")
        assert audit.passed == ["zapora: ufw"]

    def test_missing_firewall_uses_ssh_port(self):
        audit = posture.Audit()
        posture.firewall_findings(audit, set(), "2222")
        assert "ufw allow 2222/tcp" in audit.findings[0].command

    def test_public_database_ports(self):
        sockets = [Socket("tcp", "0.0.0.0", 5432), Socket("tcp6", "::", 6379), Socket("tcp", "127.0.0.1", 3306),
                   Socket("tcp", "0.0.0.0", 443)]
        db = Container("shop-db", "postgres:16", "running", ports=[PortMap("0.0.0.0", 5432, 5432)],
                       workdir="/srv/shop")
        audit = posture.Audit()
        posture.port_findings(audit, sockets, [db])
        by_id = {f.id: f for f in audit.findings}
        assert set(by_id) == {"port-5432", "port-6379"}
        assert "shop-db" in by_id["port-5432"].detail and "127.0.0.1:5432:5432" in by_id["port-5432"].fix
        assert by_id["port-6379"].command == "ufw deny 6379/tcp"

    def test_containers(self):
        audit = posture.Audit()
        posture.container_findings(audit, [
            Container("pipe-vps-agent-1", "pipe", "running", mounts=[("/var/run/docker.sock", "/var/run/docker.sock")]),
            Container("portainer", "portainer/portainer-ce", "running", mounts=[("/var/run/docker.sock", "/x")]),
            Container("vpn", "wireguard", "running", privileged=True, network_mode="host"),
        ])
        assert sorted(f.id for f in audit.findings) == ["docker-sock-portainer", "host-network-vpn", "privileged-vpn"]

    def test_accounts(self, host):
        write(host, "/etc/passwd", "root:x:0:0::/root:/bin/bash\ntoor:x:0:0::/root:/bin/sh\nkuba:x:1000:1000::/h:/bin/sh\n")
        write(host, "/etc/shadow", "root:$6$abc:1::::::\ntoor::1::::::\nkuba:!:1::::::\n")
        audit = posture.Audit()
        posture.account_findings(audit)
        ids = {f.id: f for f in audit.findings}
        assert "toor" in ids["uid0"].title and ids["empty-password"].command == "passwd -l toor"

    def test_updates(self, host):
        write(host, "/var/lib/dpkg/status", "Package: unattended-upgrades\nStatus: install ok installed\nVersion: 2\n")
        write(host, "/var/lib/update-notifier/updates-available", "5 updates can be applied immediately.\n"
                                                                  "3 of these updates are standard security updates.\n")
        write(host, "/run/reboot-required", "*** System restart required ***\n")
        audit = posture.Audit()
        posture.update_findings(audit, set(), {"unattended-upgrades": "2"})
        assert {f.id for f in audit.findings} == {"auto-updates", "security-updates", "reboot"}
        write(host, "/etc/apt/apt.conf.d/20auto-upgrades", 'APT::Periodic::Unattended-Upgrade "1";\n')
        audit = posture.Audit()
        posture.update_findings(audit, set(), {"unattended-upgrades": "2"})
        assert "auto-updates" not in {f.id for f in audit.findings}
        assert "Czeka 3 aktualizacji bezpieczenstwa" in [f.title for f in audit.findings]

    def test_protection_depends_on_password_login(self, host):
        audit = posture.Audit()
        posture.protection_findings(audit, set(), {}, password_login=True)
        assert audit.findings[0].severity == "medium" and audit.findings[0].command == ""   # system nieznany
        write(host, "/var/lib/dpkg/status", "")
        audit = posture.Audit()
        posture.protection_findings(audit, set(), {}, password_login=False)
        assert audit.findings[0].severity == "low" and audit.findings[0].command == "apt-get install -y fail2ban"
        audit = posture.Audit()
        posture.protection_findings(audit, {"fail2ban.service"}, {}, password_login=True)
        assert not audit.findings

    def test_world_readable_env(self, host):
        write(host, "/srv/shop/.env", "DB_PASSWORD=x", mode=0o644)
        write(host, "/srv/blog/.env", "x", mode=0o600)
        memory.upsert_directory("/srv/shop", "app", "sklep")
        memory.upsert_directory("/srv/blog", "app", "blog")
        audit = posture.Audit()
        posture.secret_file_findings(audit)
        assert audit.findings[0].command == "chmod 600 /srv/shop/.env"

    def test_score_grade_and_render(self):
        audit = posture.Audit(findings=[posture.Finding("a", "high", "A"), posture.Finding("b", "low", "B", command="x",
                                                                                            host_only=True)])
        assert audit.score == 82 and audit.grade == "B"
        text = audit.render()
        assert text.startswith("Ocena bezpieczenstwa: 82/100 (B)") and "w powloce hosta" in text
        assert audit.to_data()["findings"][0]["id"] == "a"

    def test_install_command(self, host):
        assert posture.install_command("x") == ""
        write(host, "/etc/alpine-release", "3.20")
        assert posture.install_command("x") == "apk add x"


# ─── Log SSH ────────────────────────────────────────────────────────────────

AUTH = """Sep 29 10:00:01 vps sshd[100]: Failed password for root from 203.0.113.9 port 4000 ssh2
Sep 29 10:00:02 vps sshd[101]: Invalid user admin from 203.0.113.9 port 4001
Sep 29 10:00:03 vps sshd[102]: Accepted publickey for kuba from 198.51.100.4 port 5000 ssh2: ED25519 SHA256:x
"""


class TestAuthLog:

    def test_parse(self):
        events = posture.parse_auth(AUTH)
        assert events.failures == Counter({"203.0.113.9": 2})
        assert events.accepted == [("publickey", "kuba", "198.51.100.4")]

    def test_incremental_read_and_rotation(self, host):
        log = write(host, "/var/log/auth.log", AUTH)
        state = {}
        assert posture.read_auth_increment(state).failures == Counter()          # pierwszy odczyt = punkt startowy
        with log.open("a") as fh:
            fh.write("Sep 29 10:05:00 vps sshd[103]: Failed password for root from 192.0.2.1 port 1 ssh2\n")
        assert posture.read_auth_increment(state).failures == Counter({"192.0.2.1": 1})
        log.write_text("Sep 29 11:00:00 vps sshd[104]: Invalid user x from 192.0.2.2 port 1\n")   # rotacja
        assert posture.read_auth_increment(state).failures == Counter({"192.0.2.2": 1})
        assert posture.read_auth_increment({"auth_offset:/var/log/secure": 0}) is not None

    def test_bruteforce_breach_and_new_login(self):
        auth = posture.AuthWatch()
        state = {}
        many = posture.AuthEvents(failures=Counter({"203.0.113.9": 70}),
                                  accepted=[("publickey", "kuba", "198.51.100.4")])
        found = posture.auth_findings(many, auth, state, threshold=60, notify_logins=True, now=1000)
        assert [f.key for f in found] == ["auth:ssh"]                   # pierwsze logowanie = punkt startowy
        breach = posture.AuthEvents(accepted=[("password", "root", "203.0.113.9"), ("publickey", "kuba", "192.0.2.7")])
        found = posture.auth_findings(breach, auth, state, threshold=60, notify_logins=True, now=1100)
        keys = {f.key: f for f in found}
        assert keys["auth:breach:root@203.0.113.9"].severity == "critical"
        assert "auth:login:kuba@192.0.2.7" in keys
        again = posture.auth_findings(posture.AuthEvents(accepted=[("publickey", "kuba", "192.0.2.7")]), auth, state,
                                      threshold=60, notify_logins=True, now=2000)
        assert [f.key for f in again] == []                             # znany adres, okno 10 min minelo


# ─── Straznik ───────────────────────────────────────────────────────────────

class TestSentinel:

    def test_suid_detection(self, host):
        write(host, "/usr/bin/sudo", "x", mode=0o4755)
        write(host, "/usr/bin/ls", "x", mode=0o755)
        write(host, "/usr/local/bin/.hidden", "x", mode=0o2755)
        found = snapshots.suid()
        assert set(found) == {"/usr/bin/sudo", "/usr/local/bin/.hidden"}
        assert found["/usr/bin/sudo"].startswith("suid")

    def test_findings(self):
        before = {"users": {"root": "uid=0 shell=/bin/bash"}, "ssh_keys": {"/root/.ssh/authorized_keys": "k1"},
                  "suid": {"/usr/bin/sudo": "suid 1 B 1"}, "configs": {"/etc/sudoers": "a"}}
        after = {"users": {"root": "uid=0 shell=/bin/bash", "sys": "uid=0 shell=/bin/sh", "kuba": "uid=1000 shell=/bin/sh"},
                 "ssh_keys": {"/root/.ssh/authorized_keys": "k1\nk2"},
                 "suid": {"/usr/bin/sudo": "suid 1 B 1", "/tmp/x": "suid 1 B 2"}, "configs": {"/etc/sudoers": "b"}}
        found = {f.key: f for f in posture.sentinel_findings(before, after)}
        assert found["security:users:sys"].severity == "critical"
        assert found["security:users:kuba"].severity == "warning"
        assert found["security:ssh_keys:/root/.ssh/authorized_keys"].severity == "critical"
        assert found["security:suid:/tmp/x"].severity == "critical"
        assert found["security:configs:/etc/sudoers"].severity == "warning"
        assert all(f.transient for f in found.values())
        assert posture.sentinel_findings(None, after) == []
        quiet = posture.sentinel_findings(before, after, pipe_changed={"/etc/sudoers"})
        assert "security:configs:/etc/sudoers" not in {f.key for f in quiet}

    def test_removed_keys_are_not_critical(self):
        before = {"ssh_keys": {"/root/.ssh/authorized_keys": "k1\nk2"}}
        after = {"ssh_keys": {"/root/.ssh/authorized_keys": "k1"}}
        [finding] = posture.sentinel_findings(before, after)
        assert finding.severity == "warning"

    def test_watcher_alerts_on_new_key(self, host, monkeypatch):
        from backend.core import infra

        async def no_docker():
            return None

        monkeypatch.setattr(infra, "docker_containers", no_docker)
        watcher = watch.Watcher()
        run(watcher.check_once())
        key = "ssh-ed25519 " + __import__("base64").b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"k" * 32).decode()
        write(host, "/root/.ssh/authorized_keys", key + " intruz@evil\n")
        events = run(watcher.check_once())
        alert = next(e for e in events if e.key.startswith("security:ssh_keys"))
        assert alert.severity == "critical" and alert.state == "event" and "intruz@evil" in alert.title + alert.detail


# ─── Powitanie ──────────────────────────────────────────────────────────────

class TestWelcome:

    def _fake_infra(self, monkeypatch):
        from backend.core import infra

        found = infra.Infra(hostname="vps1", containers=[
            Container("shop-web", "shop:1", "running", project="shop", domains=["shop.example.com"]),
            Container("shop-db", "postgres:16", "exited", project="shop")],
            listeners=[Socket("tcp", "0.0.0.0", 443)])

        async def discover(include_kube=None):
            return found

        async def audit_now(report=None, cert_days=14):
            return posture.Audit(findings=[posture.Finding("ssh-password", "high", "SSH pozwala logowac sie haslem")])

        monkeypatch.setattr(infra, "discover", discover)
        monkeypatch.setattr(posture, "audit_now", audit_now)

    def test_build(self, monkeypatch):
        self._fake_infra(monkeypatch)
        event = run(welcome.build(digest_time="07:00"))
        assert event["type"] == "welcome" and event["title"] == "Czesc! Jestem Pipe na vps1."
        text = event["text"]
        assert "kontenery: 1/2 dziala, projekty compose: shop" in text and "shop.example.com" in text
        assert "ocena: 85/100 (B)" in text and "1. SSH pozwala logowac sie haslem" in text
        assert "powiedz mi, gdzie leza" in text and "raport codziennie o 07:00" in text

    def test_loop_waits_for_subscriber_and_sends_once(self, monkeypatch):
        self._fake_infra(monkeypatch)
        real_sleep = asyncio.sleep
        monkeypatch.setattr(watch.asyncio, "sleep", lambda s: real_sleep(0))

        async def scenario():
            watcher = watch.Watcher()
            task = asyncio.create_task(watcher._welcome_loop())
            await real_sleep(0.01)
            assert not task.done()                   # nikt nie slucha — czeka
            queue = watcher.notifier.subscribe()
            await asyncio.wait_for(task, 2)
            event = queue.get_nowait()
            await watch.Watcher()._welcome_loop()    # drugi raz — juz wyslane
            return event

        event = run(scenario())
        assert event["type"] == "welcome" and welcome.already_sent()


# ─── Skille wbudowane ───────────────────────────────────────────────────────

class TestBuiltinSkills:

    def test_real_builtin_skills_are_valid(self):
        installed = memory.seed_builtin_skills()
        assert {"nginx-vhost", "swap", "fail2ban-ssh", "backup-postgres", "aktualizuj-kontener", "utwardz-ssh",
                "wolne-miejsce"} <= set(installed)
        assert all(s["command"] for s in memory.skill_commands())

    def _source(self, tmp_path, text):
        source = tmp_path / "builtin"
        (source / "demo").mkdir(parents=True, exist_ok=True)
        (source / "demo" / "SKILL.md").write_text(f"---\nname: demo\ndescription: Demo\n---\n\n{text}\n")
        return source

    def test_update_only_untouched_and_never_resurrect(self, tmp_path):
        source = self._source(tmp_path, "wersja 1")
        assert memory.seed_builtin_skills(source) == ["demo"]
        assert memory.seed_builtin_skills(source) == []
        source = self._source(tmp_path, "wersja 2")
        assert memory.seed_builtin_skills(source) == ["demo"]
        assert "wersja 2" in memory.read_skill("demo").content
        memory.save_skill("demo", "Demo", "moja wersja")                 # uzytkownik zmienil
        source = self._source(tmp_path, "wersja 3")
        assert memory.seed_builtin_skills(source) == []
        assert memory.read_skill("demo").content == "moja wersja"
        memory.delete_skill("demo")                                     # uzytkownik usunal
        assert memory.seed_builtin_skills(source) == [] and memory.read_skill("demo") is None

    def test_user_skill_with_same_name_is_kept(self, tmp_path):
        memory.save_skill("demo", "Moj", "moj")
        assert memory.seed_builtin_skills(self._source(tmp_path, "wbudowany")) == []
        assert memory.read_skill("demo").content == "moj"


# ─── Protokol i klienci ─────────────────────────────────────────────────────

class TestServerAndClients:

    def test_audit_and_welcome_commands(self, monkeypatch):
        import backend.server as server
        from backend.config import settings
        from backend.tests.test_server_commands import FakeAgent, exchange

        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(server, "get_agent", lambda: FakeAgent())
        TestWelcome()._fake_infra(monkeypatch)
        [[audit], welcome_frames] = exchange([{"command": "audit"}, {"command": "welcome"}])
        assert audit["data"]["score"] == 85 and audit["data"]["findings"][0]["id"] == "ssh-password"
        assert welcome_frames[-1]["data"]["type"] == "welcome"

    def test_format_audit(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
        from tg_format import BUILTIN_COMMANDS, format_audit

        text = format_audit({"score": 48, "grade": "D", "findings": [
            {"severity": "high", "title": "Redis <publiczny>", "detail": "", "fix": "bind 127.0.0.1",
             "command": "ufw deny 6379/tcp", "host_only": True}], "passed": ["zapora: ufw"]})
        assert "<b>Bezpieczenstwo: 48/100 (D)</b>" in text and "Redis &lt;publiczny&gt;" in text
        assert "<code>ufw deny 6379/tcp</code> (na hoscie)" in text and "napraw 1" in text
        assert "audyt" in {c for c, _ in BUILTIN_COMMANDS} and "audyt" in memory.RESERVED_COMMANDS
