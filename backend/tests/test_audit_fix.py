"""
Po poprawce audyt, alerty, pulpit i skrzynka nie pokazuja juz naprawionego (uwagi testera), oraz /audyt napraw —
wszystkie poprawki z audytu po kolei, kazda z osobnym potwierdzeniem.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import feed, journal, posture, vulns, watch


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    root = tmp_path / "host"
    root.mkdir()
    monkeypatch.setenv("HOST_ROOT", str(root))
    monkeypatch.setenv("HOST_PROC", str(tmp_path / "proc"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from backend.core import infra

    async def none():
        return None
    monkeypatch.setattr(infra, "docker_containers", none)
    return root


def item(name="openssl", version="3.0.11-1", ids=("DSA-1",), kev=True, fixed="3.0.13-1", ecosystem="Debian:12"):
    return vulns.PackageVulns(name, version, [name], [vulns.Vuln(i, ["CVE-2026-0001"], "", "", fixed, kev=kev)
                                                      for i in ids], ecosystem=ecosystem)


def scan(items=None, targets=None, esm=False):
    return vulns.Scan(at=time.time(), ecosystem="Debian:12", system="Debian 12", checked=10,
                      items=list(items or []), targets=list(targets or []), esm=esm)


def hold(*keys):
    vulns.update_state(lambda state: state.update({"held": {k: {"at": time.time()} for k in keys}}))


class TestPatchableView:

    def test_drops_held_pro_only_stale_images_servers_and_apps(self):
        held, pro, fresh = item("bash", "5.2"), item("libc", "2.36", fixed="2.36+esm1"), item()
        image = vulns.TargetResult("image", "nginx:1.25", "Debian:12", 5, [item("zlib", "1.2")],
                                   meta={"containers": ["web"]})
        server = vulns.TargetResult("server", "db1", "Debian:12", 5, [item("curl", "7.88")])
        app = vulns.TargetResult("app", "/srv/shop", "npm", 5, [item("lodash", "4.17.0", ecosystem="npm")])
        result = scan([held, pro, fresh], [image, server, app])
        hold(held.key, vulns.server_key(server, server.items[0]), f"app:{app.name}|{app.items[0].key}")
        vulns.update_state(lambda state: state.update(
            {"stale_images": {image.name: {"ids": vulns.target_fingerprint(image), "at": time.time()}}}))
        view = vulns.patchable_view(result)
        assert [p.name for p in view.items] == ["openssl"]
        assert all(not t.items for t in view.targets)
        assert len(result.items) == 3 and image.items             # oryginal bez zmian (raport i audyt go pokazuja)

    def test_esm_keeps_pro_only(self):
        result = scan([item("libc", "2.36", fixed="2.36+esm1")], esm=True)
        assert vulns.patchable_view(result).items


class TestAlertsFollowTheFix:

    def test_alert_resolves_when_only_held_packages_are_left(self):
        w = watch.Watcher()
        result = scan([item()])
        events = w.apply(w.vuln_findings(result), watch.VULN_SCOPE)
        assert [e.state for e in events] == ["new"]
        hold(result.items[0].key)                                     # /zalataj nie zmienil pakietu
        events = w.apply(w.vuln_findings(result), watch.VULN_SCOPE)
        assert [e.state for e in events] == ["resolved"]
        # po HOLD_FOR te same ID nie wysylaja powiadomienia drugi raz — alert wraca po cichu
        vulns.update_state(lambda state: state.update({"held": {}}))
        assert w.apply(w.vuln_findings(result), watch.VULN_SCOPE) == []
        assert watch.VULN_KEY in w.active

    def test_vuln_for_audit_resolves_fixed_alerts_and_closes_the_inbox_item(self, monkeypatch):
        monkeypatch.setattr(settings, "VULN_INTERVAL", 3600)
        w = watch.Watcher()
        w._publish(w.apply(w.vuln_findings(scan([item()])), watch.VULN_SCOPE))
        assert watch.VULN_KEY in w.active
        before = [i for i in feed.recent() if i.key == f"alert:{watch.VULN_KEY}"]
        assert before and not before[0].done and before[0].actions

        async def fixed(**kwargs):
            return scan([])
        monkeypatch.setattr(w, "vuln_scan_now", fixed)
        result, note = asyncio.run(w.vuln_for_audit())
        assert note == "" and not result.items
        assert watch.VULN_KEY not in w.active
        items = {i.id: i for i in feed.recent()}
        assert items[before[0].id].done                               # przyciski starej wiadomosci znikaja
        assert any(i.title.startswith(("Rozwiazane", "Resolved")) for i in items.values())

    def test_resolve_does_not_announce_new_vulnerabilities(self):
        w = watch.Watcher()
        assert w.resolve_fixed_vulns(scan([item()])) == [] and not w.active

    def test_fresh_audit_waits_for_a_scan_newer_than_the_last_change(self, monkeypatch):
        monkeypatch.setattr(settings, "VULN_INTERVAL", 3600)
        monkeypatch.setattr(journal, "last_change_at", lambda limit=5: 1234.5)
        seen = {}

        async def fake(**kwargs):
            seen.update(kwargs)
            return scan([])
        w = watch.Watcher()
        monkeypatch.setattr(w, "vuln_scan_now", fake)
        asyncio.run(w.vuln_for_audit())
        assert seen["since"] == 1234.5

    def test_last_change_at_uses_the_finish_time(self):
        assert journal.last_change_at() == 0.0
        entry = journal.Entry(id="ab12cd34", at=100.0, interface="cli", tool="execute_command", command="apt-get upgrade")
        journal._save(entry)
        path = journal.journal_dir() / entry.id / "entry.json"
        os.utime(path, (5000.0, 5000.0))
        assert journal.last_change_at() == 5000.0


class TestActiveAlertsSurviveRestart:

    def finding(self):
        return watch.Finding("disk:/", "warning", "Dysk / zajety w 95%", "95%")

    def test_fixed_while_down_is_resolved_after_start(self):
        w = watch.Watcher()
        w._publish(w.apply([self.finding()], watch.RESOURCE_SCOPE))
        old = next(i for i in feed.recent() if i.key == "alert:disk:/")

        restarted = watch.Watcher()
        assert restarted.restore_active(watch.RESOURCE_SCOPE) == 1
        events = restarted._publish(restarted.apply([], watch.RESOURCE_SCOPE))
        assert [e.state for e in events] == ["resolved"]
        assert feed.get(old.id).done
        assert watch.load_state()["active"] == []

    def test_still_present_is_not_announced_again(self):
        w = watch.Watcher()
        w.apply([self.finding()], watch.RESOURCE_SCOPE)
        restarted = watch.Watcher()
        restarted.restore_active(watch.RESOURCE_SCOPE)
        assert restarted.apply([self.finding()], watch.RESOURCE_SCOPE) == []

    def test_only_scopes_pipe_checks_itself(self):
        w = watch.Watcher()
        w.apply([watch.Finding("prometheus:HighLoad", "warning", "Load", "")], frozenset())
        restarted = watch.Watcher()
        assert restarted.restore_active(watch.RESOURCE_SCOPE | watch.CHECKS_SCOPE | watch.VULN_SCOPE) == 0


class TestRescanAfterPackageChange:

    @pytest.mark.parametrize("command,expected", [
        ("apt-get update && apt-get install -y --only-upgrade openssl", True),
        ("sudo DEBIAN_FRONTEND=noninteractive apt upgrade -y", True),
        ("sh -c 'apk upgrade musl'", True),
        ("docker compose -f /srv/app/compose.yml pull", True),
        ("docker pull nginx:1.27", True),
        ("npm audit fix", True),
        ("docker compose ps", False),
        ("ls /var/cache/apt | grep apt", False),
        ("ufw --force enable", False),
        ("systemctl reload ssh", False),
    ])
    def test_touches_packages(self, command, expected):
        assert vulns.touches_packages(command) is expected

    def test_confirmed_command_schedules_a_scan(self, monkeypatch):
        from backend.core import agent as agent_module

        calls = []
        w = watch.get_watcher()
        monkeypatch.setattr(w, "rescan_vulns_soon", lambda: calls.append(1) or True)
        agent_module._rescan_after(None, "apt-get install -y fail2ban")
        agent_module._rescan_after(None, "systemctl restart nginx")
        assert calls == [1]

    def test_rescan_only_when_the_watcher_runs(self, monkeypatch):
        monkeypatch.setattr(settings, "VULN_INTERVAL", 3600)
        w = watch.Watcher()
        assert w.rescan_vulns_soon() is False                         # testy, CLI bez serwera: nic w tle

        async def run():
            started = []

            async def once(since=0):
                started.append(since)
            monkeypatch.setattr(w, "vulns_once", once)
            w._tasks.append(asyncio.ensure_future(asyncio.sleep(0)))
            assert w.rescan_vulns_soon() is True
            await asyncio.sleep(0.01)
            return started
        assert len(asyncio.run(run())) == 1


class TestAuditText:

    def test_notifier_count_only_without_a_scan(self, data):
        def audit_with(scan_result):
            audit = posture.Audit(findings=[posture.Finding("security-updates", "medium", "12 security updates pending")])
            posture.vuln_findings(audit, scan_result)
            return {f.id for f in audit.findings}
        assert "security-updates" not in audit_with(scan([]))              # skan OSV: nic do poprawki
        assert "security-updates" in audit_with(None)                     # bez skanu licznik to jedyna wskazowka
        broken = scan([])
        broken.error = "OSV down"
        assert "security-updates" in audit_with(broken)

    def test_run_audit_text_refreshes_containers(self, monkeypatch):
        from backend.core.handlers import audit as audit_handler

        result = scan([item()])
        refreshed = []
        w = watch.get_watcher()

        async def fake_scan(fresh=True):
            return result, ""

        async def explained(*args, **kwargs):
            return True
        monkeypatch.setattr(w, "vuln_for_audit", fake_scan)
        monkeypatch.setattr(w, "explain_vulns", explained)
        monkeypatch.setattr(vulns, "refresh_containers", lambda scan_result, containers: refreshed.append(scan_result))
        asyncio.run(audit_handler.run_audit_text())
        assert refreshed == [result]

    def test_hint_says_old_results_are_stale(self, monkeypatch):
        from backend.core.handlers import audit as audit_handler

        assert "security_audit jeszcze raz" in audit_handler.AUDIT_HINT_PL
        assert "security_audit again" in audit_handler.AUDIT_HINT_EN


class TestFixAll:

    def fake_audit(self, findings=True):
        audit = posture.Audit(passed=["zapora: ufw"])
        if findings:
            audit.findings = [posture.Finding("bruteforce", "medium", "Brak fail2ban", "", "zainstaluj fail2ban",
                                              "apt-get install -y fail2ban", True),
                              posture.Finding("reboot", "low", "Restart po aktualizacji jadra")]
        return audit

    def test_words_and_message(self, monkeypatch):
        from backend.core.handlers import audit as audit_handler

        assert audit_handler.wants_fix_all("napraw") and audit_handler.wants_fix_all("Fix all")
        assert not audit_handler.wants_fix_all("") and not audit_handler.wants_fix_all("raport")
        message = audit_handler.fix_all_message(self.fake_audit())
        assert "90/100" in message and "apt-get install -y fail2ban" in message
        assert "security_patch" in message and "Pominiete" in message and "security_audit" in message
        monkeypatch.setenv("PIPE_LANG", "en")
        message = audit_handler.fix_all_message(self.fake_audit())
        assert "one item at a time" in message and "'Skipped'" in message

    def test_prompt_exists_in_both_languages(self):
        from backend.config import prompts, prompts_en

        for module in (prompts, prompts_en):
            module.AUDIT_FIX_ALL_MESSAGE.format(score=1, grade="E", audit="x")

    def run_command(self, monkeypatch, audit, args="napraw", token=""):
        import backend.server as server
        from backend.core.handlers import audit as audit_handler
        from backend.tests.test_server_commands import FakeAgent, exchange

        fake = FakeAgent()
        monkeypatch.setattr(server, "get_agent", lambda: fake)

        async def fake_run(with_vulns=False):
            return audit, audit.render()
        monkeypatch.setattr(audit_handler, "run_audit_text", fake_run)
        [frames] = exchange([{"command": "audit", "args": args, "token": token}])
        return fake, frames

    def test_runs_the_agent_with_the_server_built_prompt(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        fake, frames = self.run_command(monkeypatch, self.fake_audit())
        assert len(fake.messages) == 1 and "apt-get install -y fail2ban" in fake.messages[0][1]
        assert frames[-1]["done"] and "data" not in frames[-1]

    def test_nothing_to_fix(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        fake, frames = self.run_command(monkeypatch, self.fake_audit(findings=False), args="fix")
        assert not fake.messages and "100/100" in frames[-1]["data"]["text"]

    def test_viewer_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "admin-token")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "viewer-token")
        fake, frames = self.run_command(monkeypatch, self.fake_audit(), token="viewer-token")
        assert not fake.messages and frames[-1]["status"] == "error"

    def test_plain_audit_is_unchanged(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        fake, frames = self.run_command(monkeypatch, self.fake_audit(), args="")
        assert not fake.messages and frames[-1]["data"]["score"] == 90


class TestClients:

    def test_telegram(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
        import tg_format

        assert tg_format.wants_audit_fix("napraw") and tg_format.wants_audit_fix("fix")
        assert not tg_format.wants_audit_fix("")
        assert "/audyt napraw" in dict(tg_format.BUILTIN_COMMANDS)["audyt"]
        assert "/audit fix" in dict(tg_format.BUILTIN_COMMANDS_EN)["audit"]
        text = tg_format.format_audit({"score": 90, "grade": "A", "findings": [{"severity": "low", "title": "x"}]})
        assert "/audyt napraw" in text

    def test_cli_and_web(self):
        root = Path(__file__).resolve().parents[2]
        cli = (root / "clients" / "cli" / "cli.py").read_text(encoding="utf-8")
        assert "AUDIT_FIX_WORDS" in cli and "/audyt napraw" in cli and "/audit fix" in cli
        app = (root / "clients" / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        assert '["audyt", "audit", "c_audit", true]' in app and "auditFixAll(" in app
        i18n = (root / "clients" / "webui" / "static" / "i18n.js").read_text(encoding="utf-8")
        assert i18n.count("auditFixAll:") == 2
