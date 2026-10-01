"""
Testy funkcji "zna serwer" (v0.10): historia pomiarow i wykresy, migawki
("co sie zmienilo"), sprawdzenia bez konfiguracji, poranny raport, licznik kosztow.
"""

import asyncio
import base64
import hashlib
import json
import os
import time
from datetime import date, datetime

import pytest

from backend.config import settings
from backend.core import checks, diagram, digest, metrics, snapshots, usage, watch
from backend.core.infra import Container, Route


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("HOST_ROOT", str(host))
    monkeypatch.setenv("HOST_PROC", str(tmp_path / "proc"))
    return host


def write(host, path, text, mtime=None):
    target = host / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if mtime is not None:
        os.utime(target, (mtime, mtime))
    return target


def write_proc(proc, *, load="0.5 0.4 0.3", cpus=2, total=4_000_000, avail=2_000_000):
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text(f"MemTotal: {total} kB\nMemAvailable: {avail} kB\nSwapTotal: 0 kB\n")
    (proc / "loadavg").write_text(f"{load} 1/100 1\n")
    (proc / "cpuinfo").write_text("".join(f"processor\t: {i}\n\n" for i in range(cpus)))
    (proc / "uptime").write_text("7200.0 100.0\n")
    return str(proc)


# ─── Historia pomiarow i wykresy ────────────────────────────────────────────

class TestMetrics:

    def test_record_load_and_prune(self):
        now = 1_800_000_000
        for i in range(10):
            metrics.record({"t": now - (9 - i) * 86400, "load5": float(i), "mem": 10 * i, "cpus": 2}, keep_days=30)
        assert len(metrics.load(24 * 3 - 1, now=now)) == 3
        metrics.prune(keep_days=5, now=now)
        assert [p["load5"] for p in metrics.load(24 * 30, now=now)] == [4.0, 5.0, 6.0, 7.0, 8.0, 9.0]

    def test_sample_reads_host_proc(self, tmp_path):
        proc = write_proc(tmp_path / "proc", load="3.0 2.5 2.0", cpus=4, total=1_000_000, avail=250_000)
        point = metrics.sample(proc, now=100)
        assert point["load5"] == 2.5 and point["cpus"] == 4 and point["mem"] == 75 and point["t"] == 100

    def test_chart_series_threshold_and_summary(self):
        now = 1_800_000_000
        for i in range(48):
            metrics.record({"t": now - (47 - i) * 1800, "load1": 1.0, "load5": 5.0 if i == 40 else 1.0, "cpus": 2,
                            "mem": 50, "disks": {"/": 70, "/data": 95}})
        source, text = metrics.chart("load", 24, disk_pct=90, mem_pct=92, load_factor=2, now=now)
        assert source.startswith("---") and "xychart-beta" in source
        assert source.count("line [") == 3                      # load 5, load 1, prog
        assert "Prog alertu: 4." in text and "Prog przekroczony w: load 5 min" in text
        disk_source, disk_text = metrics.chart("disk", 24, disk_pct=90, mem_pct=92, load_factor=2, now=now)
        assert "dysk /data" in disk_text and "0 --> 100" in disk_source

    def test_chart_without_data(self):
        source, text = metrics.chart("memory", 24, disk_pct=90, mem_pct=92, load_factor=2)
        assert source is None and "Brak pomiarow" in text

    def test_gaps_filled_with_last_value(self):
        assert metrics._fill([None, 2.0, None, 3.0, None]) == [2.0, 2.0, 2.0, 3.0, 3.0]
        assert metrics._fill([None, None]) is None

    @pytest.mark.parametrize("text,hours", [("", 24), ("24h", 24), ("3d", 72), ("2 dni", 48), ("1t", 168),
                                            ("90m", 1.5), ("12", 12), ("bzdura", 24)])
    def test_parse_hours(self, text, hours):
        assert metrics.parse_hours(text) == hours

    @pytest.mark.parametrize("name,expected", [("ram", "memory"), ("dysk", "disk"), ("cpu", "load"), ("x", None)])
    def test_metric_aliases(self, name, expected):
        assert metrics.normalize_metric(name) == expected

    @pytest.mark.skipif(not diagram.available(), reason="mermaidx niezainstalowany")
    def test_chart_renders_to_png(self):
        now = time.time()
        for i in range(10):
            metrics.record({"t": now - i * 600, "load1": 0.5, "load5": 0.4 + i / 10, "cpus": 2})
        source, _ = metrics.chart("load", 3, disk_pct=90, mem_pct=92, load_factor=2, now=now)
        rendered = asyncio.run(diagram.render(source, ascii_art=False))
        assert rendered.png and rendered.png[:8] == b"\x89PNG\r\n\x1a\n"


# ─── Migawki ────────────────────────────────────────────────────────────────

DPKG = """Package: nginx
Status: install ok installed
Architecture: amd64
Version: 1.24.0-1

Package: removed-pkg
Status: deinstall ok config-files
Architecture: amd64
Version: 1.0

Package: curl
Status: install ok installed
Architecture: amd64
Version: 8.5.0
"""


def ssh_key_line(comment="kuba@laptop", seed=b"klucz"):
    blob = b"\x00\x00\x00\x0bssh-ed25519" + hashlib.sha256(seed).digest()
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}", blob


class TestSnapshotReaders:

    def test_packages_skip_removed(self, isolated):
        write(isolated, "/var/lib/dpkg/status", DPKG)
        assert snapshots.packages() == {"nginx": "1.24.0-1", "curl": "8.5.0"}

    def test_users_login_and_root(self, isolated):
        write(isolated, "/etc/passwd", "root:x:0:0:root:/root:/bin/bash\n"
                                       "daemon:x:1:1::/usr/sbin:/usr/sbin/nologin\n"
                                       "kuba:x:1000:1000::/home/kuba:/bin/zsh\n"
                                       "toor:x:0:0::/root:/usr/sbin/nologin\n")
        found = snapshots.users()
        assert set(found) == {"root", "kuba", "toor"}          # uid 0 zawsze, nawet z nologin
        assert found["kuba"] == "uid=1000 shell=/bin/zsh"

    def test_key_fingerprint_matches_ssh_keygen_format(self):
        line, blob = ssh_key_line()
        expected = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        assert snapshots.key_fingerprint(line) == f"ssh-ed25519 SHA256:{expected} kuba@laptop"
        assert snapshots.key_fingerprint(f'command="x" {line}').endswith("[z opcjami]")
        assert snapshots.key_fingerprint("smiec") is None

    def test_ssh_keys_never_store_key_material(self, isolated):
        line, _ = ssh_key_line()
        write(isolated, "/root/.ssh/authorized_keys", line + "\n")
        stored = json.dumps(snapshots.ssh_keys())
        assert line.split()[1] not in stored and "SHA256:" in stored

    def test_cron_is_redacted(self, isolated):
        write(isolated, "/etc/cron.d/backup", "# komentarz\n0 3 * * * root DB_PASSWORD=supertajne123 /opt/backup.sh\n")
        found = snapshots.cron()
        assert "supertajne123" not in found["/etc/cron.d/backup"]
        assert "/opt/backup.sh" in found["/etc/cron.d/backup"]

    def test_configs_include_compose_files(self, isolated):
        write(isolated, "/etc/ssh/sshd_config", "PermitRootLogin no\n")
        write(isolated, "/srv/shop/docker-compose.yml", "services: {}\n")
        container = Container("shop-web", "shop:1", "running", workdir="/srv/shop")
        found = snapshots.configs(snapshots.compose_files([container]))
        assert set(found) == {"/etc/ssh/sshd_config", "/srv/shop/docker-compose.yml"}


def sections(**overrides):
    base = {name: {} for name in snapshots.SECTIONS}
    base.update(overrides)
    return base


class TestSnapshotHistory:

    def test_save_only_when_changed(self):
        first = sections(packages={"nginx": "1.24"})
        assert snapshots.save(first, now=1_000_000)
        assert not snapshots.save(first, now=1_003_600)
        assert snapshots.save(sections(packages={"nginx": "1.26"}), now=1_007_200)
        assert len(snapshots.list_snapshots()) == 2

    def test_docker_outage_is_not_a_change(self):
        snapshots.save(sections(containers={"web": "nginx | sha256:aaa | dziala"}), now=1_000_000)
        outage = sections()
        outage["_meta"] = {"docker": "niedostepny"}
        assert not snapshots.save(outage, now=1_003_600)

    def test_prune_keeps_recent_and_one_per_day(self):
        now = datetime(2026, 9, 29, 12).timestamp()
        for hours_ago in (0, 5, 50, 51, 52, 24 * 40):   # 50-52 h temu = ten sam dzien
            path = snapshots.snapshots_dir() / f"{snapshots._stamp(now - hours_ago * 3600)}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"t": now - hours_ago * 3600, "sections": {}}))
        snapshots.prune(keep_days=30, now=now)
        ages = sorted(round((now - t) / 3600) for t, _ in snapshots.list_snapshots())
        assert 0 in ages and 5 in ages and 24 * 40 not in ages
        assert len([a for a in ages if 50 <= a <= 52]) == 1

    def test_diff_describes_changes_in_polish(self):
        old = sections(packages={"nginx": "1.24"}, containers={"web": "app:1 | sha256:aaa | dziala"},
                       ports={"tcp/80 0.0.0.0": "publiczny"}, system={"rozruch": "a"})
        new = sections(packages={"nginx": "1.26", "redis": "7"}, containers={"web": "app:1 | sha256:bbb | zatrzymany"},
                       ports={"tcp/80 0.0.0.0": "publiczny", "tcp/6379 0.0.0.0": "publiczny"}, system={"rozruch": "b"})
        text = snapshots.render(snapshots.diff(old, new))
        assert "pakiet nginx: 1.24 -> 1.26" in text
        assert "zainstalowano pakiet redis 7" in text
        assert "nowa wersja obrazu app:1" in text and "dziala -> zatrzymany" in text
        assert "nowy port nasluchujacy tcp/6379 0.0.0.0 (publiczny)" in text
        assert "serwer zostal zrestartowany" in text

    def test_security_changes_are_marked(self):
        old = sections(users={"root": "uid=0 shell=/bin/bash"}, ssh_keys={"/root/.ssh/authorized_keys": "k1"})
        new = sections(users={"root": "uid=0 shell=/bin/bash", "backdoor": "uid=0 shell=/bin/sh"},
                       ssh_keys={"/root/.ssh/authorized_keys": "k1\nk2"},
                       configs={"/etc/sudoers.d/x": "abc"})
        changes = snapshots.diff(old, new)
        assert all(c.security for c in changes)
        text = snapshots.render(changes)
        assert text.count("[BEZPIECZENSTWO]") == 3 and "+ k2" in text

    def test_many_package_changes_are_collapsed(self):
        old = sections(packages={f"p{i}": "1" for i in range(40)})
        new = sections(packages={f"p{i}": "2" for i in range(40)})
        assert "i 28 innych pakietow" in snapshots.render(snapshots.diff(old, new))

    def test_timeline_shows_when(self):
        base = datetime(2026, 9, 29, 0).timestamp()
        snapshots.save(sections(packages={"nginx": "1.24"}), now=base)
        snapshots.save(sections(packages={"nginx": "1.26"}), now=base + 3 * 3600)
        current = sections(packages={"nginx": "1.26"}, ports={"tcp/8080 0.0.0.0": "publiczny"})
        text = snapshots.timeline(24, current, now=base + 10 * 3600)
        assert "Miedzy 2026-09-29 00:00 a 2026-09-29 03:00" in text and "1.24 -> 1.26" in text
        assert "a teraz" in text and "tcp/8080" in text

    def test_timeline_without_snapshots(self):
        assert "Brak migawek" in snapshots.timeline(24, sections())


# ─── Sprawdzenia bez konfiguracji ───────────────────────────────────────────

class TestChecks:

    @pytest.mark.parametrize("name,ok", [("shop.example.com", True), ("localhost", False), ("_", False),
                                         ("*.example.com", False), ("10.0.0.1", False), ("app.local", False),
                                         ("$host", False), ("intranet", False)])
    def test_checkable_domain(self, name, ok):
        assert checks.is_checkable_domain(name) is ok

    def test_domains_from_proxy_and_labels(self):
        routes = [Route("nginx", ["shop.example.com", "www.shop.example.com"], "127.0.0.1:3000"),
                  Route("nginx", ["_"], "127.0.0.1:8080")]
        containers = [Container("api", "api:1", "running", domains=["api.example.com", "Shop.Example.com."])]
        assert checks.domains_from(routes, containers, ignore={"www.shop.example.com"}) == \
            ["api.example.com", "shop.example.com"]

    def test_backup_age(self, isolated):
        old = time.time() - 50 * 3600
        write(isolated, "/var/backups/pg/2026-09-27.sql.gz", "x", mtime=old)
        write(isolated, "/var/backups/pg/2026-09-28.sql.gz", "x", mtime=old + 3600)
        result = checks.check_backup("/var/backups/pg")
        assert result.files == 2 and result.newest_name == "2026-09-28.sql.gz"
        assert 48 < result.age_hours() < 50
        assert checks.check_backup("/nie/ma").error == "sciezka nie istnieje"
        (isolated / "empty").mkdir()
        assert checks.check_backup("/empty").error == "katalog jest pusty"

    def test_report_findings(self):
        now = time.time()
        report = checks.Report(
            certs=[checks.CertResult("a.example.com", days_left=2, not_after="2026-10-01"),
                   checks.CertResult("b.example.com", days_left=10, not_after="2026-10-09"),
                   checks.CertResult("c.example.com", days_left=80),
                   checks.CertResult("d.example.com", error="certyfikat odrzucony: certificate has expired",
                                     rejected=True)],
            sites=[checks.SiteResult("a.example.com", 200), checks.SiteResult("b.example.com", 502)],
            dns=[checks.DnsResult("e.example.com", error="nie rozwiazuje sie")],
            backups=[checks.BackupResult("/var/backups", newest=now - 30 * 3600, newest_name="x", files=1)],
            at=now)
        found = {f.key: f for f in report.findings(cert_days=14, backup_hours=26)}
        assert found["cert:a.example.com"].severity == "critical"
        assert found["cert:b.example.com"].severity == "warning"
        assert "cert:c.example.com" not in found
        assert found["cert:d.example.com"].severity == "critical"
        assert "site:b.example.com" in found and "site:a.example.com" not in found
        assert "dns:e.example.com" in found and "backup:/var/backups" in found
        text = report.render(cert_days=14, backup_hours=26)
        assert "a.example.com: wazny jeszcze 2 dni" in text and "HTTP 502" in text

    def test_host_addresses_from_fib_trie(self, tmp_path):
        net = tmp_path / "proc" / "1" / "net"
        net.mkdir(parents=True)
        (net / "fib_trie").write_text(
            "Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 127.0.0.1\n        /32 host LOCAL\n"
            "     |-- 203.0.113.7\n        /32 host LOCAL\n     |-- 10.0.0.5\n        /32 host LOCAL\n")
        (net / "if_inet6").write_text("00000000000000000000000000000001 01 80 10 80 lo\n"
                                      "20010db8000000000000000000000001 02 40 00 80 eth0\n")
        assert checks.host_addresses(str(tmp_path / "proc")) == {"203.0.113.7", "10.0.0.5", "2001:db8::1"}

    def test_dns_points_here(self, monkeypatch):
        async def fake_getaddrinfo(host, port, **kwargs):
            return [(0, 0, 0, "", ("93.184.215.14", 0))]

        async def run():
            loop = asyncio.get_running_loop()
            monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
            here = await checks.check_dns("shop.example.com", {"93.184.215.14"})
            elsewhere = await checks.check_dns("shop.example.com", {"151.101.1.69"})
            unknown = await checks.check_dns("shop.example.com", {"10.0.0.5"})
            return here, elsewhere, unknown

        here, elsewhere, unknown = asyncio.run(run())
        assert here.points_here is True and elsewhere.points_here is False and unknown.points_here is None

    def test_parse_ignore(self):
        assert checks.parse_ignore("A.example.com., /var/backups/old ,") == {"a.example.com", "/var/backups/old"}


# ─── Czuwanie: zakresy alertow i sprawdzenia ────────────────────────────────

class TestWatcherScopes:

    def test_scope_does_not_resolve_other_groups(self):
        watcher = watch.Watcher()
        watcher.apply([watch.Finding("cert:a.example.com", "warning", "cert", "x")], watch.CHECKS_SCOPE)
        events = watcher.apply([], watch.RESOURCE_SCOPE)
        assert events == [] and "cert:a.example.com" in watcher.active
        events = watcher.apply([], watch.CHECKS_SCOPE)
        assert events[0].state == "resolved" and not watcher.active

    def test_site_alert_needs_two_failures(self, monkeypatch):
        report = checks.Report(sites=[checks.SiteResult("shop.example.com", 503)], at=time.time())

        async def fake_run_checks(**kwargs):
            return report

        monkeypatch.setattr(checks, "run_checks", fake_run_checks)
        watcher = watch.Watcher()
        assert asyncio.run(watcher.checks_once()) == []
        events = asyncio.run(watcher.checks_once())
        assert [e.key for e in events] == ["site:shop.example.com"]
        report.sites = [checks.SiteResult("shop.example.com", 200)]
        assert asyncio.run(watcher.checks_once())[0].state == "resolved"

    def test_check_once_records_metrics(self, tmp_path, monkeypatch):
        write_proc(tmp_path / "proc")

        async def no_docker():
            return None

        from backend.core import infra
        monkeypatch.setattr(infra, "docker_containers", no_docker)
        asyncio.run(watch.Watcher().check_once())
        points = metrics.load(1)
        assert len(points) == 1 and points[0]["cpus"] == 2


# ─── Poranny raport ─────────────────────────────────────────────────────────

class TestDigest:

    @pytest.mark.parametrize("value,when,expected", [
        ("07:00", datetime(2026, 9, 29, 7, 0), True), ("07:00", datetime(2026, 9, 29, 7, 1), False),
        ("off", datetime(2026, 9, 29, 7, 0), False), ("", datetime(2026, 9, 29, 7, 0), False),
        ("7:30", datetime(2026, 9, 29, 7, 30), True), ("zle", datetime(2026, 9, 29, 7, 0), False)])
    def test_due(self, value, when, expected):
        assert digest.due(value, when) is expected

    def test_build_sections(self, tmp_path, isolated):
        proc = write_proc(tmp_path / "proc")
        base = time.time() - 30 * 3600
        snapshots.save(sections(users={"root": "uid=0 shell=/bin/bash"}), now=base)
        current = sections(users={"root": "uid=0 shell=/bin/bash", "evil": "uid=0 shell=/bin/sh"},
                           packages={"nginx": "1.26"})
        write(isolated, "/var/lib/update-notifier/updates-available", "\n12 updates can be applied immediately.\n")
        write(isolated, "/run/reboot-required", "*** System restart required ***\n")
        report = checks.Report(certs=[checks.CertResult("shop.example.com", days_left=5, not_after="x")],
                               at=time.time())
        alert = watch.Alert("1", "disk:/", "warning", "Dysk / zapelniony w 91%", "x", "2026-09-29 06:00")
        result = digest.build(hostname="vps1", active=[alert], history=[], current=current, report=report,
                              usage_line="LLM wczoraj: 10 tys. tokenow", cert_days=14, backup_hours=26,
                              load_factor=2, proc=proc)
        event = result.to_event()
        titles = [s["title"] for s in event["sections"]]
        assert titles[:3] == ["Stan", "Alerty", "Zmiany od wczoraj"]
        changes = next(s for s in event["sections"] if s["title"] == "Zmiany od wczoraj")["lines"]
        assert changes[0].startswith("[BEZPIECZENSTWO] nowe konto evil")
        assert any("certyfikat shop.example.com: 5 dni (!)" in l for s in event["sections"] for l in s["lines"])
        updates = next(s for s in event["sections"] if s["title"] == "Aktualizacje")["lines"]
        assert "12 updates can be applied immediately." in updates and "wymagany restart serwera" in updates
        assert "Raport vps1" in event["title"] and "LLM" in titles


# ─── Licznik kosztow ────────────────────────────────────────────────────────

class TestUsage:

    def test_record_and_report(self):
        today = date(2026, 9, 29)
        prices = usage.Prices(input=1.0, output=4.0)
        usage.record("gemini-x", {"prompt_tokens": 1_000_000, "completion_tokens": 250_000,
                                  "prompt_tokens_details": {"cached_tokens": 400_000}}, "cli-kuba", prices, today)
        usage.record("gemini-x", {"prompt_tokens": 1000, "completion_tokens": 10}, "worker", prices, today)
        total = usage.day_total(today)
        assert total["calls"] == 2 and total["prompt"] == 1_001_000 and total["cached"] == 400_000
        assert total["cost"] == pytest.approx(2.00104)
        text = usage.report(priced=True, today=today)["text"]
        assert "cli-kuba" in text and "worker" in text and "~$2.0010" in text

    def test_record_ignores_missing_usage(self):
        usage.record("m", None, "cli", usage.Prices())
        usage.record("m", {"prompt_tokens": 0, "completion_tokens": 0}, "cli", usage.Prices())
        assert usage.day_total() == {}

    def test_budget(self):
        today = date(2026, 9, 29)
        usage.record("m", {"prompt_tokens": 900, "completion_tokens": 200}, "cli", usage.Prices(1, 1), today)
        usage.check_budget(0, 0, today)
        usage.check_budget(5000, 0, today)
        with pytest.raises(usage.BudgetExceeded, match="limit tokenow"):
            usage.check_budget(1000, 0, today)
        with pytest.raises(usage.BudgetExceeded, match="limit kosztu"):
            usage.check_budget(0, 0.001, today)

    @pytest.mark.parametrize("interface,who", [("cli:kuba", "cli-kuba"), ("telegram:123", "telegram-123"),
                                               ("worker:web1@cli:kuba", "worker"), ("routine:rano", "rutyna"),
                                               ("vibe", "vibe")])
    def test_who(self, interface, who):
        assert usage.who_from_interface(interface) == who

    def test_agent_records_usage_and_respects_budget(self, monkeypatch):
        from openai.types.chat import ChatCompletion

        from backend.core.agent import VPSAgent
        from backend.tests.fakes import FakeClient

        response = ChatCompletion.model_validate({
            "id": "x", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}})
        monkeypatch.setattr(settings, "VIBE_EVERY", 0)
        monkeypatch.setattr(settings, "DAILY_TOKEN_LIMIT", 100)
        agent = VPSAgent(client=FakeClient([response, response]))

        async def chat():
            return [event async for event in agent.chat("s", "hej", "cli:kuba")]

        assert asyncio.run(chat()) == ["ok"]
        assert usage.day_total()["prompt"] == 120
        second = asyncio.run(chat())
        assert "limit tokenow" in second[-1] and second[-1].startswith("[BLAD]")
        assert len(agent._client.calls) == 1                # drugie zapytanie nie poszlo do providera


# ─── Protokol, narzedzie, klienci ───────────────────────────────────────────

@pytest.fixture
def server_agent(monkeypatch):
    import backend.server as server
    from backend.tests.test_server_commands import FakeAgent

    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    fake = FakeAgent()
    monkeypatch.setattr(server, "get_agent", lambda: fake)
    return fake


class TestServerCommands:

    def test_changes(self, server_agent, monkeypatch):
        from backend.tests.test_server_commands import exchange

        async def fake_capture():
            return sections(packages={"nginx": "1.26"})

        monkeypatch.setattr(snapshots, "capture", fake_capture)
        snapshots.save(sections(packages={"nginx": "1.24"}), now=time.time() - 3600)
        [[frame]] = exchange([{"command": "changes", "args": "3h"}])
        assert frame["done"] and "nginx: 1.24 -> 1.26" in frame["data"]["text"] and frame["data"]["hours"] == 3
        assert server_agent.messages == []

    def test_skill_preview_and_graph_for_the_web_ui(self, server_agent, monkeypatch):
        from backend.core import infra, memory
        from backend.tests.test_server_commands import exchange

        memory.write_skill("odnow-certyfikat", "Odnawia certyfikat.", "1. certbot renew\n2. sprawdz nginx")

        async def fake_discover(include_kube=None):
            return infra.Infra(hostname="vps1", containers=[])

        monkeypatch.setattr(infra, "discover", fake_discover)
        [[by_command], [by_name], [missing], [graph]] = exchange([
            {"command": "skill", "name": "/odnow_certyfikat"}, {"command": "skill", "name": "odnow-certyfikat"},
            {"command": "skill", "name": "nie-ma"}, {"command": "graph"}])
        assert by_command["data"]["content"].startswith("1. certbot renew") and by_command["data"]["command"] == "odnow_certyfikat"
        assert by_name["data"]["name"] == "odnow-certyfikat" and missing["status"] == "error"
        assert graph["data"]["root"] == "host" and graph["data"]["hostname"] == "vps1"
        assert server_agent.messages == []                           # zadna z tych komend nie uzywa LLM

    def test_chart_without_history(self, server_agent):
        from backend.tests.test_server_commands import exchange

        [[frame]] = exchange([{"command": "chart", "args": "ram 7d"}])
        assert frame["data"]["metric"] == "memory" and frame["data"]["image"] is False
        assert "Brak pomiarow" in frame["data"]["summary"]

    @pytest.mark.skipif(not diagram.available(), reason="mermaidx niezainstalowany")
    def test_chart_sends_image(self, server_agent):
        from backend.tests.test_server_commands import exchange

        now = time.time()
        for i in range(12):
            metrics.record({"t": now - i * 300, "load1": 0.3, "load5": 0.2, "cpus": 1, "mem": 40})
        [frames] = exchange([{"command": "chart", "args": "load 2h"}])
        assert frames[0]["attachment"]["mime"] == "image/png"
        assert frames[-1]["data"]["image"] is True

    def test_usage(self, server_agent, monkeypatch):
        from backend.tests.test_server_commands import exchange

        monkeypatch.setattr(settings, "LLM_PRICE_IN", 0.0)
        monkeypatch.setattr(settings, "LLM_PRICE_OUT", 0.0)
        usage.record("m", {"prompt_tokens": 1500, "completion_tokens": 20}, "cli-kuba", usage.Prices())
        [[frame]] = exchange([{"command": "usage"}])
        assert frame["data"]["today"]["prompt"] == 1500 and "Ceny nieustawione" in frame["data"]["text"]

    def test_investigate_includes_recent_changes(self, server_agent, monkeypatch):
        from backend.core.watch import Finding, get_watcher
        from backend.tests.test_server_commands import exchange

        async def fake_capture():
            return sections(containers={"web": "app:2 | sha256:bbb | dziala"})

        monkeypatch.setattr(snapshots, "capture", fake_capture)
        snapshots.save(sections(containers={"web": "app:1 | sha256:aaa | dziala"}), now=time.time() - 7200)
        watcher = get_watcher()
        [alert] = watcher.apply([Finding("container:web:unhealthy", "warning", "Kontener web jest unhealthy", "x")])
        try:
            exchange([{"command": "investigate", "id": alert.id}])
        finally:
            watcher.active.clear()
        message = server_agent.messages[-1][1]
        assert "Kontener web jest unhealthy" in message and "obraz app:1 -> app:2" in message


class TestHistoryTool:

    def _call(self, args):
        from types import SimpleNamespace

        from backend.core.handlers import handle_server_history
        from backend.core.session import Session

        session = Session()
        tool_call = SimpleNamespace(id="t1", function=SimpleNamespace(name="server_history", arguments="{}"))

        async def run():
            return [e async for e in handle_server_history(None, session, tool_call, args)]

        events = asyncio.run(run())
        return events, session.messages[-1]["content"]

    def test_chart_without_data_replies_text(self):
        events, result = self._call({"operation": "chart", "metric": "memory"})
        assert events == [] and "Brak pomiarow" in result

    def test_unknown_operation(self):
        _, result = self._call({"operation": "nic"})
        assert "Dostepne: changes, chart, checks" in result

    def test_checks_without_domains(self, monkeypatch):
        async def no_domains(ignore=frozenset()):
            return []

        monkeypatch.setattr(checks, "discover_domains", no_domains)
        _, result = self._call({"operation": "checks"})
        assert "Nie znalazlem domen" in result and "brak wpisow [backup]" in result


class TestClients:

    def test_new_commands_reserved_for_skills(self):
        from backend.core.memory import RESERVED_COMMANDS
        assert {"zmiany", "wykres", "zdrowie", "raport", "koszt"} <= RESERVED_COMMANDS

    def test_format_digest_escapes_and_marks_security(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
        from tg_format import BUILTIN_COMMANDS, format_digest, format_pre

        text = format_digest({"title": "Raport <vps>", "sections": [
            {"title": "Zmiany", "lines": ["[BEZPIECZENSTWO] nowe konto <evil>", "pakiet a & b"]}]})
        assert "Raport &lt;vps&gt;" in text and "<b>[BEZPIECZENSTWO]</b> nowe konto &lt;evil&gt;" in text
        assert "pakiet a &amp; b" in text
        assert format_pre("Zmiany", "<x>").endswith("<pre>&lt;x&gt;</pre>")
        assert {"raport", "zmiany", "wykres", "zdrowie", "koszt"} <= {c for c, _ in BUILTIN_COMMANDS}


def test_kubernetes_without_node_mount_skips_host_files(isolated, monkeypatch):
    monkeypatch.setenv("PIPE_RUNTIME", "kubernetes")
    monkeypatch.setenv("HOST_ROOT", "")
    captured = snapshots.capture_sync(containers=None)
    assert set(captured) == {"containers"}
