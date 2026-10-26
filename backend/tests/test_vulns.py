"""Podatnosci w pakietach: inwentarz dpkg/apk, OSV (podrobione), KEV, audyt, alert czuwania, raport."""

import asyncio

import pytest

from backend.core import digest, posture, vulns, watch


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    root = tmp_path / "host"
    root.mkdir()
    monkeypatch.setenv("HOST_ROOT", str(root))
    monkeypatch.setenv("HOST_PROC", str(tmp_path / "proc"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return root


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    """Czuwanie pyta Dockera o kontenery — w testach go nie ma (testy etapu 3 podstawiaja wlasne)."""
    from backend.core import infra

    async def none():
        return None
    monkeypatch.setattr(infra, "docker_containers", none)


def write(root, path, text):
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


DEBIAN = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\nVERSION="12 (bookworm)"\n'
DPKG = """Package: openssl
Status: install ok installed
Architecture: amd64
Version: 3.0.11-1~deb12u1

Package: libssl3
Status: install ok installed
Architecture: amd64
Source: openssl
Version: 3.0.11-1~deb12u1

Package: libc6
Status: install ok installed
Architecture: i386
Source: glibc (2.36-9+deb12u3)
Version: 2.36-9+deb12u3+b1

Package: bash
Status: install ok installed
Architecture: amd64
Version: 5.2.15-2+b2

Package: removed
Status: deinstall ok config-files
Architecture: amd64
Version: 1.0
"""


def osv_vuln(vid, package, fixed="", severity=None, cve=None, eco="Debian:12"):
    events = [{"introduced": "0"}] + ([{"fixed": fixed}] if fixed else [])
    data = {"id": vid, "modified": "2026-10-01T00:00:00Z", "summary": f"{vid} summary",
            "aliases": [cve] if cve else [],
            "affected": [{"package": {"name": package, "ecosystem": eco}, "ranges": [{"type": "ECOSYSTEM", "events": events}]}]}
    if severity:
        data["severity"] = [{"type": "CVSS_V3", "score": severity}]
    return data


CRITICAL = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


class FakeOsv:
    def __init__(self, ids, details, kev=()):
        self.ids, self.details, self.kev = ids, details, kev
        self.calls: list[str] = []
        self.batches: list[list[str]] = []

    def __call__(self, url, data=None, timeout=0):
        self.calls.append(url)
        if url == vulns.OSV_BATCH:
            self.batches.append([q["package"]["name"] for q in data["queries"]])
            return {"results": [{"vulns": [{"id": i, "modified": "2026-10-01T00:00:00Z"} for i in self.ids.get(q["package"]["name"], [])]}
                                for q in data["queries"]]}
        if url.startswith(vulns.OSV_VULN):
            return self.details[url[len(vulns.OSV_VULN):]]
        if url == vulns.KEV_URL:
            return {"vulnerabilities": [{"cveID": c} for c in self.kev]}
        raise AssertionError(url)


def debian_host(host):
    write(host, "/etc/os-release", DEBIAN)
    write(host, "/var/lib/dpkg/status", DPKG)


def fake_osv(monkeypatch, kev=("CVE-2026-0001",)):
    fake = FakeOsv(
        {"openssl": ["DSA-1", "DEBIAN-CVE-2026-0002"], "glibc": ["DEBIAN-CVE-2026-0003"]},
        {"DSA-1": osv_vuln("DSA-1", "openssl", "3.0.13-1~deb12u1", cve="CVE-2026-0001"),
         "DEBIAN-CVE-2026-0002": osv_vuln("DEBIAN-CVE-2026-0002", "openssl", "3.0.13-1~deb12u1", CRITICAL),
         "DEBIAN-CVE-2026-0003": osv_vuln("DEBIAN-CVE-2026-0003", "glibc")},
        kev)
    monkeypatch.setattr(vulns, "get_json", fake)
    return fake


class TestInventory:

    def test_dpkg_groups_by_source(self, host):
        debian_host(host)
        eco, packages = vulns.inventory()
        assert eco == "Debian:12"
        by_name = {p.name: p for p in packages}
        assert set(by_name) == {"openssl", "glibc", "bash"}
        assert by_name["openssl"].binaries == ["openssl", "libssl3"]
        assert by_name["glibc"].version == "2.36-9+deb12u3" and by_name["glibc"].binaries == ["libc6:i386"]
        assert by_name["bash"].version == "5.2.15-2+b2"

    def test_apk_uses_origin(self, host):
        write(host, "/etc/os-release", "ID=alpine\nVERSION_ID=3.20.3\n")
        write(host, "/lib/apk/db/installed", "P:libcrypto3\nV:3.3.2-r0\no:openssl\n\nP:busybox\nV:1.36.1-r29\n")
        eco, packages = vulns.inventory()
        assert eco == "Alpine:v3.20"
        assert [(p.name, p.binaries) for p in packages] == [("busybox", ["busybox"]), ("openssl", ["libcrypto3"])]

    @pytest.mark.parametrize("release,expected", [
        ({"ID": "ubuntu", "VERSION_ID": "22.04", "VERSION": "22.04.4 LTS (Jammy Jellyfish)"}, "Ubuntu:22.04:LTS"),
        ({"ID": "ubuntu", "VERSION_ID": "24.10", "VERSION": "24.10 (Oracular Oriole)"}, "Ubuntu:24.10"),
        ({"ID": "debian"}, None),                         # testing/sid bez VERSION_ID
        ({"ID": "rocky", "VERSION_ID": "9.4"}, None),
    ])
    def test_ecosystem(self, release, expected):
        assert vulns.ecosystem(release) == expected


class TestSeverity:

    @pytest.mark.parametrize("vector,score", [
        (CRITICAL, 9.8),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),
        ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N", 5.5),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
    ])
    def test_cvss3(self, vector, score):
        assert vulns.cvss3_score(vector) == score

    def test_cvss4_and_garbage_are_unknown(self):
        assert vulns.cvss3_score("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N") is None
        assert vulns.cvss3_score("CVSS:3.1/AV:X") is None

    def test_distro_word_wins(self):
        vuln = {"severity": [{"type": "Ubuntu", "score": "Medium"}, {"type": "CVSS_V3", "score": CRITICAL}]}
        assert vulns.severity_level(vuln) == "medium"
        assert vulns.severity_level({"severity": [{"type": "CVSS_V3", "score": CRITICAL}]}) == "critical"
        assert vulns.severity_level({}) == ""


class TestScan:

    def test_fixable_unfixed_kev_and_command(self, host, monkeypatch):
        debian_host(host)
        fake_osv(monkeypatch)
        result, queried = vulns.scan()
        assert queried and result.ok and result.checked == 3
        assert [p.name for p in result.items] == ["openssl"]          # glibc: tylko bez poprawki
        openssl = result.items[0]
        assert openssl.kev and openssl.urgent and openssl.fixed_version == "3.0.13-1~deb12u1"
        assert {v.id: v.severity for v in openssl.fixable} == {"DSA-1": "", "DEBIAN-CVE-2026-0002": "critical"}
        assert result.unfixed == 1
        assert vulns.upgrade_command(result.ecosystem, result.items) == \
            "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade -o Dpkg::Options::=--force-confold libssl3 openssl"
        line = vulns.describe_item(openssl, {"DSA-1": "Atakujacy moze odczytac pamiec."})
        assert line == ("openssl 3.0.11-1~deb12u1 → 3.0.13-1~deb12u1 — aktywnie wykorzystywana: "
                        "Atakujacy moze odczytac pamiec. (CVE-2026-0001); kolejnych podatnosci: 1")

    def test_cached_until_inventory_changes_or_interval(self, host, monkeypatch):
        debian_host(host)
        fake = fake_osv(monkeypatch)
        vulns.scan(max_age=3600, now=1000)
        batches = fake.calls.count(vulns.OSV_BATCH)
        details = sum(1 for c in fake.calls if c.startswith(vulns.OSV_VULN))
        result, queried = vulns.scan(max_age=3600, now=2000)
        assert not queried and result.items and fake.calls.count(vulns.OSV_BATCH) == batches
        # minelo VULN_INTERVAL: OSV znowu, ale szczegoly juz znane
        _, queried = vulns.scan(max_age=3600, now=5000)
        assert queried and fake.calls.count(vulns.OSV_BATCH) == batches + 1
        assert sum(1 for c in fake.calls if c.startswith(vulns.OSV_VULN)) == details
        # nowy pakiet: od razu
        write(host, "/var/lib/dpkg/status", DPKG + "\nPackage: nginx\nStatus: install ok installed\nVersion: 1.22.1-9\n")
        result, queried = vulns.scan(max_age=3600, now=5100)
        # ...ale OSV pyta tylko o niego (audyt po latce trwa sekundy)
        assert queried and fake.batches[-1] == ["nginx"] and result.checked == 4
        assert [p.name for p in result.items] == ["openssl"]

    def test_unsupported_system_and_osv_error(self, host, monkeypatch):
        write(host, "/etc/os-release", 'ID=rocky\nVERSION_ID="9.4"\nPRETTY_NAME="Rocky Linux 9.4"\n')
        result, queried = vulns.scan()
        assert result.ecosystem is None and not queried and result.system == "Rocky Linux 9.4"

        debian_host(host)
        fake_osv(monkeypatch)
        vulns.scan()

        def offline(url, data=None, timeout=0):
            raise vulns.WebError("api.osv.dev: timed out")
        monkeypatch.setattr(vulns, "get_json", offline)
        result, _ = vulns.scan(force=True)
        assert result.error and not result.ok and [p.name for p in result.items] == ["openssl"]   # ostatni dobry wynik

    def test_kev_cached_and_stale_on_error(self, host, monkeypatch):
        fake_osv(monkeypatch, kev=("CVE-1",))
        assert vulns.kev_ids(now=100) == {"CVE-1"}

        def offline(url, data=None, timeout=0):
            raise vulns.WebError("down")
        monkeypatch.setattr(vulns, "get_json", offline)
        assert vulns.kev_ids(now=200) == {"CVE-1"}                       # swieza kopia
        assert vulns.kev_ids(now=200 + vulns.KEV_MAX_AGE) == {"CVE-1"}   # stara, ale lepsza niz nic


class TestAudit:

    def test_groups_and_replaces_notifier_count(self, host, monkeypatch):
        debian_host(host)
        fake_osv(monkeypatch)
        result, _ = vulns.scan()
        audit = posture.Audit(findings=[posture.Finding("security-updates", "medium", "12 security updates pending")])
        posture.vuln_findings(audit, result)
        ids = [f.id for f in audit.findings]
        assert ids == ["vuln-kev"]
        kev = audit.findings[0]
        assert kev.severity == "high" and kev.host_only and "--force-confold libssl3 openssl" in kev.command

    def test_clean_unsupported_and_missing(self, monkeypatch):
        clean = vulns.Scan(at=1, ecosystem="Debian:12", system="Debian 12", checked=300, unfixed=40)
        audit = posture.Audit()
        posture.vuln_findings(audit, clean)
        # bez "40 znanych bez poprawki" — z tym nic sie nie da zrobic
        assert not audit.findings and "300" in audit.passed[0] and "40" not in audit.passed[0]

        audit = posture.Audit()
        posture.vuln_findings(audit, vulns.Scan(at=1, ecosystem=None, system="Rocky Linux 9.4"))
        posture.vuln_findings(audit, None, "off: VULN_INTERVAL=0")
        assert "Rocky Linux 9.4" in audit.unknown[0] and "VULN_INTERVAL=0" in audit.unknown[1]

    def test_audit_command_runs_scan(self, host, monkeypatch):
        from backend.core.handlers.audit import run_audit_text

        debian_host(host)
        fake_osv(monkeypatch)
        monkeypatch.setattr(watch, "_watcher", None)
        audit, text = asyncio.run(run_audit_text())
        assert any(f.id == "vuln-kev" for f in audit.findings) and "CISA KEV" in text


class TestWatcher:

    def test_audit_uses_fresh_result_without_osv(self, host, monkeypatch):
        from backend.config import settings

        debian_host(host)
        fake = fake_osv(monkeypatch)
        monkeypatch.setattr(settings, "VULN_INTERVAL", 3600)
        w = watch.Watcher()
        asyncio.run(w.vuln_scan_now())
        batches = len(fake.batches)
        result, note = asyncio.run(w.vuln_for_audit(fresh=False))          # instalacja latki: ostatni wynik
        assert result.items and note == "" and len(fake.batches) == batches
        # /podatnosci, /audyt: inwentarz od nowa, ale bez pelnego zapytania o wszystko przy kazdym wywolaniu
        result, note = asyncio.run(w.vuln_for_audit())
        assert result.items and note == "" and len(fake.batches) == batches
        write(host, "/var/lib/dpkg/status", DPKG.replace("5.2.15-2+b2", "5.2.15-3"))
        asyncio.run(w.vuln_for_audit())                                     # zmieniony pakiet — tylko on do OSV
        assert fake.batches[-1] == ["bash"]
        monkeypatch.setattr(watch, "VULN_FRESH_AGE", 0)
        asyncio.run(w.vuln_for_audit())                                     # po VULN_FRESH_AGE — calosc
        assert sorted(fake.batches[-1]) == ["bash", "glibc", "openssl"]
        monkeypatch.setattr(settings, "VULN_INTERVAL", 0)
        assert asyncio.run(w.vuln_for_audit())[0] is None

    def scan(self, ids=("DSA-1",), kev=True):
        item = vulns.PackageVulns("openssl", "3.0.11-1", ["openssl"],
                                  [vulns.Vuln(i, ["CVE-2026-0001"], "", "", "3.0.13-1", kev=kev) for i in ids])
        return vulns.Scan(at=1, ecosystem="Debian:12", system="Debian 12", checked=10, items=[item])

    def test_alert_once_new_ids_refire_and_resolve(self):
        w = watch.Watcher()
        events = w.apply(w.vuln_findings(self.scan()), watch.VULN_SCOPE)
        assert [(e.state, e.severity) for e in events] == [("new", "critical")]
        assert events[0].detail == "openssl 3.0.11-1 → 3.0.13-1"          # jedna linia dla promptu i raportu
        assert "**openssl** 3.0.11-1 → 3.0.13-1" in events[0].report and "(CVE-2026-0001)" in events[0].report
        assert "/zalataj serwer" in events[0].report
        assert w.apply(w.vuln_findings(self.scan()), watch.VULN_SCOPE) == []

        restarted = watch.Watcher()                     # znane ID nie wracaja po restarcie
        assert restarted.apply(restarted.vuln_findings(self.scan()), watch.VULN_SCOPE) == []
        assert watch.VULN_KEY in restarted.active

        events = restarted.apply(restarted.vuln_findings(self.scan(("DSA-1", "DSA-2"))), watch.VULN_SCOPE)
        assert [e.state for e in events] == ["new"]

        events = restarted.apply(restarted.vuln_findings(vulns.Scan(at=2, ecosystem="Debian:12", checked=10)),
                                 watch.VULN_SCOPE)
        assert [e.state for e in events] == ["resolved"]

    def test_non_urgent_and_failed_scan(self):
        w = watch.Watcher()
        assert w.vuln_findings(self.scan(kev=False)) == []          # bez severity i KEV: tylko audyt i raport
        broken = self.scan()
        broken.error = "down"
        assert w.vuln_findings(broken) is None


class TestDigest:

    def test_lines(self):
        scan = TestWatcher().scan()
        lines = digest.vuln_lines(scan)
        assert "1" in lines[0] and lines[1].startswith("openssl") and "/podatnosci" in lines[-1]
        assert digest.vuln_lines(None) == []
        assert "500" in digest.vuln_lines(vulns.Scan(at=1, ecosystem="Debian:12", checked=500))[0]

    def test_english(self, monkeypatch):
        from backend.tests.test_i18n import assert_english

        monkeypatch.setenv("PIPE_LANG", "en")
        assert_english("\n".join(digest.vuln_lines(TestWatcher().scan())))
        audit = posture.Audit()
        posture.vuln_findings(audit, TestWatcher().scan())
        assert_english(audit.render())


class TestAdvisories:

    def test_kernel_only_advisories_and_covered_records_merged(self, host, monkeypatch):
        write(host, "/etc/os-release", 'ID=ubuntu\nVERSION_ID="22.04"\nVERSION="22.04.4 LTS (Jammy Jellyfish)"\n')
        write(host, "/var/lib/dpkg/status",
              "Package: linux-libc-dev\nStatus: install ok installed\nSource: linux\nVersion: 5.15.0-100.110\n\n"
              "Package: curl\nStatus: install ok installed\nVersion: 7.81.0-1ubuntu1.15\n")
        eco = "Ubuntu:22.04:LTS"
        usn = osv_vuln("USN-1", "curl", "7.81.0-1ubuntu1.16", eco=eco)
        usn["upstream"] = ["CVE-2026-1", "UBUNTU-CVE-2026-1"]
        record = osv_vuln("UBUNTU-CVE-2026-1", "curl", "7.81.0-1ubuntu1.16", eco=eco)
        record["severity"] = [{"type": "Ubuntu", "score": "high"}]
        fake = FakeOsv(
            {"linux": ["UBUNTU-CVE-2026-9", "USN-2"], "curl": ["USN-1", "UBUNTU-CVE-2026-1"]},
            {"USN-1": usn, "UBUNTU-CVE-2026-1": record,
             "USN-2": osv_vuln("USN-2", "linux", "5.15.0-101.111", eco=eco)})
        monkeypatch.setattr(vulns, "get_json", fake)
        result, _ = vulns.scan()
        assert not result.partial
        assert not any("UBUNTU-CVE-2026-9" in c for c in fake.calls)          # jadro: same biuletyny
        by_name = {p.name: p for p in result.items}
        assert [v.id for v in by_name["linux"].fixable] == ["USN-2"]
        curl = by_name["curl"].fixable
        assert [(v.id, v.severity, v.cves) for v in curl] == [("USN-1", "high", ["CVE-2026-1"])]


class FakeShell:
    """executor.execute w testach latania: zapisuje komendy, odpowiada wedlug regul (fragment komendy -> wynik)."""

    def __init__(self, rules=None, apply=None):
        self.ran: list[str] = []
        self.rules = list(rules or [])
        self.apply = apply

    async def __call__(self, cmd, cwd=None, timeout=None):
        self.ran.append(cmd)
        if self.apply and "--only-upgrade" in cmd and "docker run --name" not in cmd:
            self.apply()
        for fragment, result in self.rules:
            if fragment in cmd:
                return result
        return "ok\n", "", 0

    def having(self, fragment):
        return [c for c in self.ran if fragment in c]


def patch_env(monkeypatch, *, kind="native", shell=None):
    """Latanie bez prawdziwych komend: tryb, executor, bez czekania na dostarczenie wyniku."""
    from backend.config import settings
    from backend.core import executor, hostexec, patching, runtime

    shell = shell or FakeShell()
    monkeypatch.setattr(runtime, "kind", lambda: kind)
    monkeypatch.setattr(runtime, "observe", lambda: False)
    monkeypatch.setattr(executor, "execute", shell)
    monkeypatch.setattr(patching, "DELIVER_AFTER", 0)
    monkeypatch.setattr(hostexec, "_project", "")
    monkeypatch.setattr(settings, "WATCH_SITES", False)
    monkeypatch.setattr(settings, "VULN_INTERVAL", 3600)
    return shell


class TestPatchCommand:
    """/zalataj lata od razu: bez podgladu, tokenu i drugiego pytania — wszystko, co jest do zalatania."""

    @pytest.fixture
    def server(self, host, monkeypatch):
        import backend.server as server
        from backend.config import settings
        from backend.tests.test_server_commands import FakeAgent

        debian_host(host)
        fake_osv(monkeypatch)
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(settings, "WATCH_SITES", False)
        monkeypatch.setattr(server, "get_agent", lambda: FakeAgent())
        monkeypatch.setattr(watch, "_watcher", None)
        return server

    def test_patch_runs_right_away_with_cleanup_through_the_fuse(self, server, monkeypatch):
        from backend.core import journal
        from backend.tests.test_server_commands import exchange

        shell = patch_env(monkeypatch, shell=FakeShell([("docker info", ("", "no docker", 1))]))
        frames = exchange([{"command": "patch"}])[0]
        done = frames[-1]["data"]
        assert done["exit_code"] == 0 and done["entry_id"] and done["text"].startswith("Gotowe")
        assert any(f.get("event", {}).get("type") == "progress" for f in frames)
        install = shell.having("--only-upgrade")
        assert len(install) == 1 and install[0].endswith("--force-confold libssl3 openssl")
        assert "DPkg::Lock::Timeout=300" in install[0]
        # porzadki: przerwane dpkg, cache paczek, stare jadra — przed i po; cache npm/pip po calosci
        assert shell.having("dpkg --force-confold --configure -a") and len(shell.having("apt-get -s autoremove")) == 2
        assert shell.having("npm cache clean --force") and not shell.having("docker image prune")
        assert shell.ran.index(install[0]) < shell.ran.index(shell.having("npm cache clean")[0])
        entry = journal.load(done["entry_id"])
        assert entry.tool == "patch" and any("cofam" in n for n in entry.notes)
        assert "Ten serwer:" in done["text"]

    def test_install_works_with_agent_token_like_on_a_real_server(self, server, monkeypatch):
        """Klienci wpisuja AGENT_TOKEN w pole `token`; dawne `execute` i `patch_token` nic nie zmieniaja."""
        from backend.config import settings
        from backend.tests.test_server_commands import exchange

        patch_env(monkeypatch)
        monkeypatch.setattr(settings, "AGENT_TOKEN", "admintok")
        frames = exchange([{"command": "patch", "scope": "pilne", "execute": True, "patch_token": "stary",
                            "token": "admintok"}])[0]
        assert frames[-1]["data"]["exit_code"] == 0, frames[-1]

    def test_web_bridge_forwards_scope_and_patch_token(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from clients.webui.server import WebBridge

        request = WebBridge("127.0.0.1", 1, "auth")._backend_request(
            {"command": "patch", "scope": "images", "patch_token": "abc", "execute": True, "token": "obcy"})
        assert request["scope"] == "images" and request["token"] == "auth"

    def test_viewer_observe_and_unknown_scope_are_refused(self, server, monkeypatch):
        from backend.core import patching, runtime
        from backend.tests.test_server_commands import exchange

        shell = patch_env(monkeypatch)
        assert asyncio.run(patching.blocker("admin")) == "" and asyncio.run(patching.local_blocker()) == ""
        assert "viewer" in asyncio.run(patching.blocker("viewer"))
        [[unknown]] = exchange([{"command": "patch", "scope": "obrazki"}])
        assert unknown["status"] == "error" and "/zalataj obrazy" in unknown["response"]
        monkeypatch.setattr(runtime, "observe", lambda: True)
        assert "PIPE_OBSERVE" in asyncio.run(patching.blocker("admin"))
        [[refused]] = exchange([{"command": "patch"}])
        assert refused["status"] == "error" and not shell.ran

    def test_docker_mode_patches_through_the_helper_container(self, server, monkeypatch):
        """Tryb docker: apt i porzadki na hoscie ida przez kontener pomocniczy (nsenter), wpis w dzienniku to mowi."""
        from backend.core import hostexec, journal, vulns as vulns_module
        from backend.tests.test_server_commands import exchange

        image = "sha256:" + "a" * 64
        monkeypatch.setattr(hostexec, "_image", image)
        shell = patch_env(monkeypatch, kind="docker")
        done = exchange([{"command": "patch", "scope": "serwer"}])[0][-1]["data"]
        assert done["exit_code"] == 0
        steps = vulns_module.host_steps("Debian:12", [vulns_module.PackageVulns("openssl", "1", ["libssl3", "openssl"])])
        assert [hostexec.wrap(s.command, image) for s in steps] == [c for c in shell.ran if c.startswith("docker run --rm")][:4]
        assert any("nsenter" in n for n in journal.load(done["entry_id"]).notes)

    def test_docker_mode_without_the_helper_says_where_to_do_it(self, server, monkeypatch):
        from backend.core import hostexec
        from backend.tests.test_server_commands import exchange

        patch_env(monkeypatch, kind="docker")

        async def no_image():
            return ""
        monkeypatch.setattr(hostexec, "image", no_image)
        frame = exchange([{"command": "patch"}])[0][-1]
        assert frame["status"] == "error" and "powloce" in frame["response"]

    def test_names_outside_the_package_pattern_never_reach_the_shell(self):
        item = vulns.PackageVulns("x", "1", ["openssl", "evil;rm -rf /", "$(id)", "libc6:i386"])
        assert vulns.upgrade_command("Debian:12", [item]).endswith("--force-confold libc6:i386 openssl")
        assert all("evil" not in s.command and "$(id)" not in s.command for s in vulns.host_steps("Debian:12", [item]))


# ─── Etap 3: obrazy kontenerow i zaleznosci aplikacji ───────────────────────

from backend.core import lockfiles  # noqa: E402
from backend.core.infra import Container  # noqa: E402


class TestLockfiles:

    def parse(self, folder, name, text):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(text)
        return lockfiles.parse(str(path))

    def test_npm_v3_and_v1(self, tmp_path):
        v3 = '{"lockfileVersion": 3, "packages": {"": {"name": "shop"}, "node_modules/lodash": {"version": "4.17.20"},' \
             ' "node_modules/a/node_modules/@scope/b": {"version": "1.0.0"}, "node_modules/local": {"link": true}}}'
        assert self.parse(tmp_path, "package-lock.json", v3) == [("npm", "lodash", "4.17.20"), ("npm", "@scope/b", "1.0.0")]
        v1 = '{"lockfileVersion": 1, "dependencies": {"x": {"version": "1.0.0", "dependencies": {"y": {"version": "2.0.0"}}}}}'
        assert self.parse(tmp_path / "v1", "npm-shrinkwrap.json", v1) == [("npm", "x", "1.0.0"), ("npm", "y", "2.0.0")]

    def test_python(self, tmp_path):
        req = "Django==4.2.1\nrequests[socks] == 2.31.0 ; python_version > \"3\"\nflask>=2\n# komentarz\n-e .\n"
        assert self.parse(tmp_path, "requirements.txt", req) == [("PyPI", "django", "4.2.1"), ("PyPI", "requests", "2.31.0")]
        assert self.parse(tmp_path, "requirements-prod.txt", "pyyaml==6.0\n") == [("PyPI", "pyyaml", "6.0")]
        pip = '{"default": {"urllib3": {"version": "==1.26.5"}}, "develop": {"pytest": {"version": "*"}}}'
        assert self.parse(tmp_path, "Pipfile.lock", pip) == [("PyPI", "urllib3", "1.26.5")]

    def test_toml_based(self, tmp_path):
        pytest.importorskip("tomllib")
        poetry = '[[package]]\nname = "Jinja2"\nversion = "3.1.2"\n'
        assert self.parse(tmp_path, "poetry.lock", poetry) == [("PyPI", "jinja2", "3.1.2")]
        cargo = '[[package]]\nname = "shop"\nversion = "0.1.0"\n\n[[package]]\nname = "hyper"\nversion = "0.14.10"\n' \
                'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        assert self.parse(tmp_path, "Cargo.lock", cargo) == [("crates.io", "hyper", "0.14.10")]

    def test_go_composer_gems_and_garbage(self, tmp_path):
        gomod = ("module example.com/shop\n\ngo 1.21\n\nrequire (\n\tgolang.org/x/net v0.17.0\n"
                 "\tgithub.com/a/b v1.2.3+incompatible // indirect\n)\nrequire github.com/c/d v0.1.0\n")
        assert self.parse(tmp_path, "go.mod", gomod) == [("Go", "golang.org/x/net", "v0.17.0"), ("Go", "github.com/a/b", "v1.2.3"),
                                                        ("Go", "github.com/c/d", "v0.1.0")]
        composer = '{"packages": [{"name": "guzzlehttp/guzzle", "version": "v7.4.0"}, {"name": "x/y", "version": "dev-main"}]}'
        assert self.parse(tmp_path, "composer.lock", composer) == [("Packagist", "guzzlehttp/guzzle", "7.4.0")]
        gems = ("GEM\n  remote: https://rubygems.org/\n  specs:\n    rack (2.2.3)\n      x (>= 1)\n"
                "    nokogiri (1.13.0-x86_64-linux)\n\nPLATFORMS\n  ruby\n")
        assert self.parse(tmp_path, "Gemfile.lock", gems) == [("RubyGems", "rack", "2.2.3"), ("RubyGems", "nokogiri", "1.13.0")]
        assert self.parse(tmp_path / "broken", "package-lock.json", "{nie json") == []
        assert lockfiles.find(str(tmp_path)) and lockfiles.find(str(tmp_path / "brak")) == []


def container(name="shop-nginx", image="nginx:1.25", image_id="sha256:aaaaaaaaaaaa", pid=0, layers=(), **kw):
    return Container(name=name, image=image, state="running", image_id=image_id, pid=pid, layers=list(layers), **kw)


class TestContainers:

    def test_reader_prefers_proc_root_then_layers(self, host, tmp_path):
        write(tmp_path, "/proc/4242/root/etc/os-release", "ID=debian\nVERSION_ID=12\n")
        write(host, "/var/lib/docker/overlay2/up/diff/etc/os-release", "ID=alpine\nVERSION_ID=3.19.1\n")
        write(host, "/var/lib/docker/overlay2/low/diff/lib/apk/db/installed", "P:busybox\nV:1.36.1-r15\n")
        read = vulns.container_reader(container(pid=4242, layers=["/var/lib/docker/overlay2/up/diff"]))
        assert "debian" in read("/etc/os-release")
        layered = container(layers=["/var/lib/docker/overlay2/up/diff", "/var/lib/docker/overlay2/low/diff"])
        [target] = vulns.image_targets([layered])
        assert target.ecosystem == "Alpine:v3.19" and [p.name for p in target.packages] == ["busybox"]

    def test_one_target_per_image_with_compose_meta(self, host, tmp_path):
        write(tmp_path, "/proc/7/root/etc/os-release", "ID=debian\nVERSION_ID=12\n")
        write(tmp_path, "/proc/7/root/var/lib/dpkg/status", DPKG)
        web = container(pid=7, project="shop", service="web", compose_files=["/srv/shop/compose.yml"])
        twin = container(name="shop-nginx-2", pid=7, project="shop", service="web2", compose_files=["/srv/shop/compose.yml"])
        stopped = Container(name="old", image="x", state="exited", image_id="sha256:bbbbbbbbbbbb")
        [target] = vulns.image_targets([web, twin, stopped])
        assert target.meta["containers"] == ["shop-nginx", "shop-nginx-2"] and len(target.meta["compose"]) == 2
        assert target.ecosystem == "Debian:12" and {p.ecosystem for p in target.packages} == {"Debian:12"}

    def test_inspect_carries_pid_layers_and_compose_files(self):
        from backend.core.infra import parse_inspect

        [c] = parse_inspect([{"Name": "/web", "Config": {"Image": "nginx", "Labels": {
            "com.docker.compose.project.config_files": "/srv/a/compose.yml,/srv/a/override.yml"}},
            "State": {"Status": "running", "Pid": 99}, "Image": "sha256:" + "c" * 64,
            "GraphDriver": {"Data": {"UpperDir": "/u", "LowerDir": "/l1:/l2"}}}])
        assert c.pid == 99 and c.layers == ["/u", "/l1", "/l2"]
        assert c.compose_files == ["/srv/a/compose.yml", "/srv/a/override.yml"]


def stage3_host(host, tmp_path, monkeypatch):
    """Host Debian, kontener z tym samym openssl i aplikacja npm z podatnym lodash."""
    from backend.core import memory

    debian_host(host)
    write(tmp_path, "/proc/7/root/etc/os-release", DEBIAN)
    write(tmp_path, "/proc/7/root/var/lib/dpkg/status", DPKG)
    write(host, "/srv/shop/package-lock.json", '{"packages": {"node_modules/lodash": {"version": "4.17.20"}}}')
    memory.upsert_directory("/srv/shop", "app", "sklep")
    fake = fake_osv(monkeypatch)
    fake.ids["lodash"] = ["GHSA-1"]
    fake.details["GHSA-1"] = osv_vuln("GHSA-1", "lodash", "4.17.21", eco="npm")
    fake.details["GHSA-1"]["database_specific"] = {"severity": "CRITICAL"}
    web = container(pid=7, project="shop", service="web", compose_files=["/srv/shop/compose.yml"])
    plain = container(name="adhoc", pid=7)
    return fake, [web, plain]


class TestStage3Scan:

    def test_images_and_apps_in_one_scan(self, host, tmp_path, monkeypatch):
        fake, containers = stage3_host(host, tmp_path, monkeypatch)
        result, _ = vulns.scan(containers=containers)
        [image] = result.of_kind("image")
        [app] = result.of_kind("app")
        assert [p.name for p in result.items] == ["openssl"]
        assert [p.name for p in image.items] == ["openssl"] and image.kev
        assert image.meta["containers"] == ["adhoc", "shop-nginx"]
        assert app.name == "/srv/shop" and app.ecosystem == "npm"
        assert [(p.name, p.fixed_version, p.fixable[0].severity) for p in app.items] == [("lodash", "4.17.21", "critical")]
        # ten sam pakiet w hoscie i obrazie = jedno zapytanie do OSV
        assert sorted(sum(fake.batches, [])) == ["bash", "glibc", "lodash", "openssl"]
        assert vulns.Scan.from_dict(result.to_dict()).of_kind("app")[0].items[0].name == "lodash"

    def test_old_state_format_starts_over(self, host):
        from backend.core import memory

        memory.data_dir().mkdir(parents=True, exist_ok=True)
        (memory.data_dir() / vulns.STORE).write_text('{"notified": ["DSA-1"], "details": {"DSA-1": {}}}')
        assert vulns.load_state() == {"format": vulns.STORE_FORMAT} and vulns.notified_ids("vuln:system") == set()

    def test_image_patch_compose_inverse_and_manual(self, host, tmp_path, monkeypatch):
        _, containers = stage3_host(host, tmp_path, monkeypatch)
        result, _ = vulns.scan(containers=containers)
        patch = vulns.image_patch(result)
        base = "docker compose -f /srv/shop/compose.yml -p shop"
        assert patch.command == f"{base} pull --ignore-buildable web && {base} build --pull web && {base} up -d web"
        assert patch.inverse == ["docker tag aaaaaaaaaaaa nginx:1.25", f"{base} up -d web"]
        assert len(patch.manual) == 1 and patch.manual[0].startswith("adhoc")
        [project] = patch.projects
        assert project.services == ["web"] and [t.name for t in project.targets] == ["nginx:1.25"]
        assert [t.name for t in patch.refreshable] == ["nginx:1.25"]

    def test_image_patch_rejects_odd_names(self):
        vuln = vulns.PackageVulns("a", "1", ["a"], [vulns.Vuln("V", [], "", "", "2")])
        target = vulns.TargetResult("image", "x", "Debian:12", 1, [vuln],
                                    meta={"image": "nginx", "image_id": "sha256:aaaaaaaaaaaa", "containers": ["c"],
                                          "compose": [{"container": "c", "project": "shop;rm -rf /", "service": "web",
                                                       "files": ["/srv/compose.yml"]}]})
        patch = vulns.image_patch(vulns.Scan(targets=[target]))
        assert patch.command == "" and patch.inverse == [] and patch.manual


class TestStage3AlertsAndAudit:

    def test_alerts_per_kind(self, host, tmp_path, monkeypatch):
        _, containers = stage3_host(host, tmp_path, monkeypatch)
        result, _ = vulns.scan(containers=containers)
        w = watch.Watcher()
        events = w.apply(w.vuln_findings(result), watch.VULN_SCOPE)
        assert {e.key for e in events} == {"vuln:system", "vuln:images", "vuln:apps"}
        images = next(e for e in events if e.key == "vuln:images")
        assert images.severity == "critical" and images.detail == "nginx:1.25 (adhoc, shop-nginx)"
        assert "### Obraz nginx:1.25" in images.report and "/zalataj obrazy" in images.report
        apps = next(e for e in events if e.key == "vuln:apps")
        assert "### Aplikacja /srv/shop (npm)" in apps.report and "**lodash** 4.17.20 → 4.17.21" in apps.report
        assert w.apply(w.vuln_findings(result), watch.VULN_SCOPE) == []
        restarted = watch.Watcher()
        assert restarted.apply(restarted.vuln_findings(result), watch.VULN_SCOPE) == []

    def test_audit_findings_and_unsupported_host(self, host, tmp_path, monkeypatch):
        _, containers = stage3_host(host, tmp_path, monkeypatch)
        result, _ = vulns.scan(containers=containers)
        audit = posture.Audit()
        posture.vuln_findings(audit, result)
        by_id = {f.id: f for f in audit.findings}
        assert set(by_id) == {"vuln-kev", "vuln-images", "vuln-apps"}
        assert by_id["vuln-images"].severity == "high" and "pull web" in by_id["vuln-images"].command
        assert by_id["vuln-apps"].severity == "medium" and by_id["vuln-apps"].detail == "/srv/shop (npm): lodash"
        assert by_id["vuln-images"].patch == "images" and by_id["vuln-kev"].patch == "system"
        data = audit.to_data()                                    # dla klientow /zalataj zamiast komendy
        assert "/zalataj obrazy" in data["text"] and "pull web" not in data["text"]
        # model dostaje narzedzie z jedna zgoda na cala latke, nie komende compose do execute_command
        assert "security_patch, scope images" in audit.render() and "pull web" not in audit.render()
        assert by_id["vuln-apps"].command == ""

        write(host, "/etc/os-release", 'ID=rocky\nVERSION_ID="9.4"\nPRETTY_NAME="Rocky Linux 9.4"\n')
        result, _ = vulns.scan(containers=containers, force=True)
        assert result.ecosystem is None and result.ok and result.of_kind("image")[0].items
        audit = posture.Audit()
        posture.vuln_findings(audit, result)
        assert "vuln-images" in {f.id for f in audit.findings} and "Rocky" in audit.unknown[0]
        assert any("obrazy" in line for line in digest.vuln_lines(result))

    def test_patch_images_keeps_rollback_then_layers_an_image_without_a_newer_version(self, host, tmp_path, monkeypatch):
        """/zalataj obrazy: poprzedni obraz pod tagiem pipe-rollback-*, pull/build, a gdy nowszego nie ma — warstwa
        z poprawkami przez docker run + commit (ten sam tag, USER/ENTRYPOINT/CMD obrazu), potem up -d i sprzatanie."""
        import json as _json

        import backend.server as server
        from backend.core import infra, journal

        _, containers = stage3_host(host, tmp_path, monkeypatch)

        async def fake_containers():
            return containers
        monkeypatch.setattr(infra, "docker_containers", fake_containers)
        monkeypatch.setattr(watch, "_watcher", None)
        config = {"User": "nginx", "Entrypoint": ["/docker-entrypoint.sh"], "Cmd": ["nginx", "-g", "daemon off;"]}
        shell = patch_env(monkeypatch, shell=FakeShell([
            ("{{.Id}}", ("sha256:" + "a" * 64 + "\n", "", 0)),           # pod tagiem ten sam obraz — nowszego nie ma
            ("{{json .Config.Labels}}", ("null\n", "", 0)),
            ("{{json .Config}}", (_json.dumps(config) + "\n", "", 0)),
            ("DockerRootDir", ("/var/lib/docker\n", "", 0)),
        ]))
        out = []

        async def send(w, frame):
            out.append(frame)
        monkeypatch.setattr(server, "_send", send)
        asyncio.run(server._patch(None, {"scope": "obrazy"}, "cli:t", "admin"))
        done = out[-1]["data"]
        base = "docker compose -f /srv/shop/compose.yml -p shop"
        order = [shell.having(f)[0] for f in ("docker tag aaaaaaaaaaaa nginx:pipe-rollback-1.25",
                                               f"{base} pull --ignore-buildable web", f"{base} build --pull web",
                                               "docker run --name pipe-patch-", f"{base} up -d web", "docker image prune -f")]
        assert [shell.ran.index(c) for c in order] == sorted(shell.ran.index(c) for c in order)
        layer = shell.having("docker run --name pipe-patch-")[0]
        assert "--user root --entrypoint sh nginx:1.25 -c" in layer and "--only-upgrade" in layer
        assert "--change 'USER nginx'" in layer and "--change 'ENTRYPOINT [\"/docker-entrypoint.sh\"]'" in layer
        assert f"LABEL pipe.patch.base=sha256:{'a' * 64}" in layer and "docker rm -f -v pipe-patch-" in layer
        assert shell.having("docker builder prune -af --filter until=24h")
        assert not [c for c in shell.ran if c.startswith("DEBIAN_FRONTEND")]           # pakiety hosta nie byly latane
        entry = journal.load(done["entry_id"])
        assert entry.inverse == ["docker tag aaaaaaaaaaaa nginx:1.25", f"{base} up -d web"]
        assert done["scope"] == "images" and "Obrazy:" in done["text"]


class TestAppDedup:

    def test_same_cve_counted_once_and_highest_fix(self):
        package = vulns.Package("django", "3.2.0", ["django"], "PyPI")
        details = {
            "PYSEC-1": {"cves": ["CVE-1"], "severity": "", "fixed": {"PyPI|django": "3.2.13"}},
            "GHSA-a": {"cves": ["CVE-1"], "severity": "critical", "fixed": {"PyPI|django": "3.2.13"}},
            "GHSA-b": {"cves": ["CVE-2"], "severity": "high", "fixed": {"PyPI|django": "3.2.20"}},
        }
        item = vulns._package_vulns(package, ["PYSEC-1", "GHSA-a", "GHSA-b"], details, set())
        assert [v.id for v in item.fixable] == ["GHSA-a", "GHSA-b"] and item.fixed_version == "3.2.20"


# ─── Czytelny raport, wyjasnienia, obce rekordy, ostrzezenie software_info ──

class FakeExplainAgent:
    def __init__(self, answer=None, enabled=True):
        self.calls, self.answer, self.enabled = [], answer, enabled

    def llm_enabled(self):
        return self.enabled

    async def complete(self, system_prompt, user_message, model=None, who=""):
        import json as _json
        items = _json.loads(user_message)
        self.calls.append((system_prompt, items, who))
        if self.answer is not None:
            return self.answer
        return _json.dumps({i["id"]: f"Atakujacy przez siec moze przejac {i['package']}. Zobacz https://evil.example <b>x</b>"
                            for i in items})


class TestReadable:

    def scan_with(self, host, monkeypatch):
        debian_host(host)
        fake_osv(monkeypatch)
        result, _ = vulns.scan()
        return result

    def test_item_markdown_highlights_and_explains(self, host, monkeypatch):
        result = self.scan_with(host, monkeypatch)
        explain = {"DSA-1": "Atakujacy moze odczytac pamiec serwera."}
        # domyslnie najpowazniejsza z opisem, reszta jako liczba przy pakiecie
        assert vulns.item_markdown(result.items[0], explain) == [
            "", "**openssl** 3.0.11-1~deb12u1 → 3.0.13-1~deb12u1 · 2 podatnosci",
            "- **Aktywnie wykorzystywana:** Atakujacy moze odczytac pamiec serwera. (CVE-2026-0001)"]
        lines = vulns.item_markdown(result.items[0], explain, 2)
        assert lines[0] == "" and lines[1] == "**openssl** 3.0.11-1~deb12u1 → 3.0.13-1~deb12u1"
        # najpierw co grozi, numer na koncu; bez kodu i kursywy — czyta sie jak zwykly tekst
        assert lines[2] == "- **Aktywnie wykorzystywana:** Atakujacy moze odczytac pamiec serwera. (CVE-2026-0001)"
        assert lines[3] == "- **Krytyczna:** DEBIAN-CVE-2026-0002 summary (CVE-2026-0002)"
        assert not any("`" in line for line in lines)

    def test_plain_strips_links_markup_and_length(self):
        assert vulns.plain("Zobacz https://x.example/a i <script>`kod`</script> **mocno** [link](y) ERR_raise_data") == \
            "Zobacz i scriptkod/script mocno link(y) ERR_raise_data"
        assert len(vulns.plain("a" * 1000)) == vulns.MAX_EXPLANATION

    def test_report_sections_and_next_steps(self, host, monkeypatch):
        result = self.scan_with(host, monkeypatch)
        text = vulns.report_markdown(result, {})
        assert "### Pakiety serwera (Debian GNU/Linux 12 (bookworm)): 1 pakiet z poprawka, pilne: 1" in text
        assert text.startswith("**Co z tym zrobic**\n- Serwer: 1 pakiet z poprawka (pilne: 1) — zainstaluje /zalataj.")
        assert "Bez poprawki" not in text
        assert "Nie ma jeszcze wyniku" in vulns.report_markdown(None, {}, "off")

    def test_explanations_written_once_cleaned_and_cached(self, host, monkeypatch):
        result = self.scan_with(host, monkeypatch)
        agent = FakeExplainAgent()
        w = watch.Watcher(agent)
        assert asyncio.run(w.explain_vulns(result)) is True
        (system_prompt, items, who), = agent.calls
        assert who == "podatnosci" and {i["id"] for i in items} == {"DSA-1"}       # tylko pokazywana
        assert "DSA-1 summary" in items[0]["summary"] or "summary" in items[1]["summary"]
        saved = vulns.explanations("pl")
        assert saved["DSA-1"] == "Atakujacy przez siec moze przejac openssl. Zobacz bx/b"
        assert asyncio.run(w.explain_vulns(result)) is True and len(agent.calls) == 1     # z pamieci
        monkeypatch.setenv("PIPE_LANG", "en")
        asyncio.run(w.explain_vulns(result))
        assert len(agent.calls) == 2 and "English" in agent.calls[1][0]                 # inny jezyk = nowe opisy

    def test_explanations_skip_without_model_and_bad_answers(self, host, monkeypatch):
        result = self.scan_with(host, monkeypatch)
        assert asyncio.run(watch.Watcher(FakeExplainAgent(enabled=False)).explain_vulns(result)) is False
        assert asyncio.run(watch.Watcher(None).explain_vulns(result)) is False
        broken = FakeExplainAgent(answer='to nie json {"DSA-1": 5, "OBCE-1": "x"}')
        assert asyncio.run(watch.Watcher(broken).explain_vulns(result)) is False and vulns.explanations("pl") == {}

    def test_vulns_report_command(self, host, monkeypatch):
        import backend.server as server
        from backend.config import settings

        self.scan_with(host, monkeypatch)
        monkeypatch.setattr(settings, "VULN_INTERVAL", 3600)
        monkeypatch.setattr(watch, "_watcher", None)
        watch.get_watcher(FakeExplainAgent())
        text, view = asyncio.run(server._vulns_report())
        assert view["groups"][0]["items"][0]["name"] == "openssl"
        assert "**openssl**" in text and "Atakujacy przez siec moze przejac openssl" in text



class TestForeignRecords:

    def test_record_of_another_distro_is_ignored(self):
        package = vulns.Package("openssl", "3.5.1-1", ["openssl"], "Debian:13")
        details = {
            "DEBIAN-CVE-1": {"cves": ["CVE-1"], "severity": "", "fixed": {"Debian:13|openssl": "3.5.4-1"},
                             "ecosystems": ["Debian:13", "Debian:14"]},
            "UBUNTU-CVE-1": {"cves": ["CVE-1"], "severity": "high", "fixed": {"Ubuntu:24.04:LTS|openssl": "3.0.13"},
                             "ecosystems": ["Ubuntu:24.04:LTS"]},
            "OLD-1": {"cves": [], "severity": "", "fixed": {"Debian|openssl": "3.5.2-1"}},      # stary format bez ecosystems
        }
        item = vulns._package_vulns(package, list(details), details, set())
        assert [v.id for v in item.fixable] == ["DEBIAN-CVE-1", "OLD-1"] and item.unfixed == 0

    def test_software_info_warns_about_wrong_distro(self, host):
        from backend.core.handlers.web import _ecosystem_warning

        write(host, "/etc/os-release", 'ID=debian\nVERSION_ID="13"\nVERSION="13 (trixie)"\n')
        assert "Debian:13" in _ecosystem_warning("Ubuntu:24.04:LTS")
        assert _ecosystem_warning("Debian:13") == "" and _ecosystem_warning("npm") == ""


class TestOneMessage:
    """Alert, raport i podglad latki mieszcza sie w jednej wiadomosci Telegrama, nawet przy 75 pakietach."""

    def big_scan(self):
        long = "Atakujacy moze spowodowac awarie programu albo wykonac dowolny kod, jesli uzytkownik otworzy " \
               "specjalnie przygotowany plik lub adres URL w tej bibliotece."
        items = [vulns.PackageVulns(f"pakiet{i}", "1.0-1ubuntu0.1", [f"pakiet{i}", f"lib{i}-dev", f"lib{i}-common"],
                                    [vulns.Vuln(f"USN-{i}-{j}", [f"CVE-2026-{i}{j}"], "high" if i < 20 else "medium",
                                                long, "1.0-1ubuntu0.2") for j in range(6)])
                 for i in range(75)]
        return vulns.Scan(at=1, ecosystem="Ubuntu:22.04:LTS", system="Ubuntu 22.04", checked=900, items=items, unfixed=40)

    def test_everything_fits_one_telegram_message(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
        from tg_format import format_alert, markdown_html

        scan = self.big_scan()
        texts = {
            "report": vulns.report_markdown(scan, {}),
            "alert": format_alert({"severity": "warning", "title": "Pilne podatnosci w 20 pakietach serwera",
                                   "report": vulns.system_alert(scan, {})}),
        }
        for name, text in texts.items():
            html_text = text if name == "alert" else markdown_html(text)
            assert len(html_text) <= 4096, (name, len(html_text))
        report = texts["report"]
        assert "**pakiet0** 1.0-1ubuntu0.1 → 1.0-1ubuntu0.2 · 6 podatnosci" in report
        assert "kolejnych" in report and "/zalataj" in report and "Bez poprawki" not in report
        assert vulns.short_command("apt-get install x") == "apt-get install x"


class TestPatchWhatIsThere:
    """/zalataj lata to, co jest do zalatania — nie mowi "nie ma podatnosci", gdy /podatnosci pokazuje obrazy i aplikacje."""

    def scan(self, compose=True):
        vuln = vulns.Vuln("DSA-1", ["CVE-2026-1"], "high", "opis", "3.0.13-1")
        image = vulns.TargetResult("image", "dermafoto_backend", "Debian:13", 10,
                                   [vulns.PackageVulns("openssl", "3.0.11-1", ["libssl3"], [vuln])],
                                   meta={"image": "dermafoto_backend", "image_id": "sha256:" + "a" * 64,
                                         "containers": ["dermavision-backend"],
                                         "compose": [{"container": "dermavision-backend", "project": "dermafoto",
                                                      "service": "backend", "files": ["/root/DermaFoto/compose.yml"]}]
                                         if compose else []})
        app = vulns.TargetResult("app", "/root/TeamBIT/Quiz", "npm", 300,
                                 [vulns.PackageVulns("shell-quote", "1.8.3", ["shell-quote"],
                                                     [vulns.Vuln("GHSA-1", ["CVE-2026-2"], "critical", "x", "1.9.0")])])
        return vulns.Scan(at=1, ecosystem="Ubuntu:24.04:LTS", system="Ubuntu 24.04", checked=500, items=[],
                          targets=[image, app], unfixed=507)

    def run_patch(self, monkeypatch, scan, request):
        import backend.server as server
        from backend.config import settings
        from backend.tests.test_server_commands import FakeAgent, exchange

        async def last(self, fresh=True):
            return scan, ""

        async def explain(self, *args, **kwargs):
            return True

        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(server, "get_agent", lambda: FakeAgent())
        monkeypatch.setattr(watch, "_watcher", None)
        monkeypatch.setattr(watch.Watcher, "vuln_for_audit", last)
        monkeypatch.setattr(watch.Watcher, "explain_vulns", explain)
        patch_env(monkeypatch)
        return exchange([{"command": "patch", **request}])[0][-1]["data"]

    def test_plain_patch_takes_the_images_when_the_server_is_clean(self):
        from backend.core import patching

        job = patching.plan(self.scan(), "all")
        assert [p.kind for p in job.parts] == ["images"] and [t.name for t in job.parts[0].items] == ["dermafoto_backend"]
        command = job.parts[0].image_patch.command
        assert "build --pull backend" in command and "pull --ignore-buildable backend" in command
        assert job.describe() == "obrazy: 1 obraz (dermafoto_backend)"

    def test_nothing_to_install_explains_where_the_vulnerabilities_are(self, monkeypatch):
        data = self.run_patch(monkeypatch, self.scan(compose=False), {})
        text = data["text"]
        assert data["nothing"] and not data["command"] and text.startswith("Nie ma nic do zainstalowania.")
        assert "Kontenery bez compose (dermavision-backend)" in text and "do zalatania" not in text
        assert "/root/TeamBIT/Quiz" in text and "npm audit fix" in text and "507" not in text


class TestRefreshContainers:
    """Zapisany skan pamieta kontenery z chwili skanu — /zalataj obrazy bierze compose z biezacego stanu Dockera."""

    def test_stale_scan_gets_live_compose(self):
        from types import SimpleNamespace

        image_id = "sha256:" + "c" * 12
        stale = vulns.Scan(at=1, targets=[vulns.TargetResult(
            "image", "dermafoto_backend", "Debian:13", 10,
            [vulns.PackageVulns("openssl", "3.0.11-1", ["libssl3"], [vulns.Vuln("DSA-1", [], "high", "x", "3.0.13-1")])],
            meta={"image": "dermafoto_backend", "image_id": image_id, "containers": ["dermavision-backend"], "compose": []})])
        assert vulns.image_patch(stale).command == "" and vulns.image_patch(stale).manual
        live = [SimpleNamespace(name="dermavision-backend", state="running", image="dermafoto_backend", image_id=image_id,
                                project="dermafoto", service="backend", compose_files=["/root/DermaFoto/docker-compose.yml"]),
                SimpleNamespace(name="inny", state="running", image="redis", image_id="sha256:" + "d" * 12,
                                project="", service="", compose_files=[])]
        vulns.refresh_containers(stale, live)
        patch = vulns.image_patch(stale)
        assert "-p dermafoto build --pull backend" in patch.command and not patch.manual
        assert vulns.refresh_containers(stale, None) is stale                # Docker niedostepny — bez zmian


def image_target(name="shop_web", image_id="sha256:" + "e" * 12, project="shop", vuln_id="DSA-9", containers=("shop-web",)):
    item = vulns.PackageVulns("openssl", "3.0.11-1", ["libssl3"], [vulns.Vuln(vuln_id, [], "high", "x", "3.0.13-1")],
                              ecosystem="Debian:12")
    return vulns.TargetResult("image", name, "Debian:12", 10, [item], meta={
        "image": name, "image_id": image_id, "containers": list(containers),
        "compose": [{"container": c, "project": project, "service": "web", "files": ["/srv/shop/compose.yml"]}
                    for c in containers]})


class TestAfterPatch:
    """Po latce: swiezy wynik zamiast starego, i to, czego latka nie zmienila, nie wraca do /zalataj w kolko."""

    def patch_server(self, host, monkeypatch, apply):
        """`_patch` z podrobionym executorem: `apply()` robi to, co apt na hoscie (albo nic)."""
        import backend.server as server
        from backend.tests.test_server_commands import FakeAgent

        debian_host(host)
        fake_osv(monkeypatch)
        monkeypatch.setattr(server, "get_agent", lambda: FakeAgent())
        monkeypatch.setattr(watch, "_watcher", None)
        patch_env(monkeypatch, shell=FakeShell([("docker info", ("", "", 1))], apply=apply))

        def call(request):
            out = []

            async def send(w, frame):
                out.append(frame)
            monkeypatch.setattr(server, "_send", send)
            asyncio.run(server._patch(None, request, "cli:t", "admin"))
            return out
        return call

    def test_upgraded_package_disappears_right_after_the_install(self, host, monkeypatch):
        def apt():
            write(host, "/var/lib/dpkg/status", DPKG.replace("3.0.11-1~deb12u1", "3.0.13-1~deb12u1"))
            vulns.get_json.ids["openssl"] = []                         # OSV: nowa wersja juz nie jest podatna
        call = self.patch_server(host, monkeypatch, apt)
        frames = call({"scope": ""})
        done = frames[-1]["data"]
        assert done["fixed"] == ["openssl"] and done["unchanged"] == [] and done["exit_code"] == 0
        assert "zainstalowane: 1 (openssl)" in done["text"] and done["report"]["groups"] == []
        assert any(f.get("event", {}).get("type") == "progress" for f in frames)
        assert watch.get_watcher().vuln_scan.items == []                    # /podatnosci zaraz potem: stan po latce

    def test_package_apt_could_not_upgrade_is_held_not_offered_again(self, host, monkeypatch):
        call = self.patch_server(host, monkeypatch, lambda: None)            # repozytorium nie ma poprawki
        done = call({"scope": ""})[-1]["data"]
        assert done["unchanged"] == ["openssl"] and done["fixed"] == [] and "repozytoria nie maja" in done["text"]
        again = call({"scope": ""})[-1]["data"]
        assert again["nothing"] and "openssl) bez zmian po ostatniej instalacji" in again["text"]
        scan = watch.get_watcher().vuln_scan
        assert "openssl) bez zmian po ostatniej instalacji" in vulns.report_markdown(scan, {})
        view = vulns.report_view(scan, {})
        assert view["groups"][0]["items"][0]["held"] and view["actions"] == []
        assert vulns.select(scan, "all", now=scan.at + vulns.HOLD_FOR + 1)    # po dobie probuje znowu

    def test_audit_does_not_offer_held_packages(self, host, monkeypatch):
        call = self.patch_server(host, monkeypatch, lambda: None)
        call({"scope": ""})
        audit = posture.Audit()
        posture.vuln_findings(audit, watch.get_watcher().vuln_scan)
        kev = next(f for f in audit.findings if f.id == "vuln-kev")
        assert kev.command == "" and kev.patch == ""

    def test_image_without_a_newer_build_waits(self):
        before = image_target()
        scan = vulns.Scan(at=1, ecosystem="Debian:12", targets=[image_target()])
        outcome = vulns.record_outcome("images", [before], scan)
        assert outcome.unchanged == ["shop_web"]
        patch = vulns.image_patch(scan)
        assert patch.command == "" and [t.name for t in patch.waiting] == ["shop_web"]
        assert "zmien FROM w Dockerfile na nowsza glowna wersje" in vulns.nothing_to_patch(scan)
        newer = vulns.Scan(at=2, ecosystem="Debian:12", targets=[image_target(vuln_id="DSA-10")])
        assert vulns.image_patch(newer).command                              # inne podatnosci — nowa proba

    def test_failed_install_shows_only_the_tail_of_the_output(self):
        from backend.core import patching

        item = vulns.PackageVulns("foo", "1", ["foo"], [vulns.Vuln("DSA-1", [], "high", "x", "2")], ecosystem="Debian:12")
        job = patching.Job("all", [patching.Part("system", "system", [item])], vulns.Scan(at=1, ecosystem="Debian:12"))
        output = "\n".join(f"#{i} CACHED" for i in range(200)) + "\nE: Unable to locate package foo"
        failed = patching.PartResult(job.parts[0], exit_code=100, entry_id="ab12cd34", output=patching._tail(output),
                                     fuse=["Dziennik zmian: #ab12cd34 — cofniecie: /cofnij ab12cd34."])
        done = patching.summary(job, [failed], vulns.Scan(at=2, ecosystem="Debian:12"), 0, False)
        assert done.exit_code == 100 and done.text.startswith("Nie udalo sie zalatac")
        assert "E: Unable to locate package foo" in done.text and "#ab12cd34" in done.text
        assert "#150 CACHED" not in done.text and len(done.text.splitlines()) < 25
        ok = patching.PartResult(job.parts[0], output=patching._tail(output), fuse=["Zweryfikowano: x."])
        text = patching.summary(job, [ok], vulns.Scan(at=2, ecosystem="Debian:12"), 3 * 1024 ** 3, False).text
        assert "CACHED" not in text and "Zweryfikowano: x." in text and "zwolnione 3.0 GB" in text

    def test_locally_built_image_gets_the_right_reason(self):
        from backend.core import patching

        built = image_target(name="flowers-valentines-card", project="flowers")
        built.meta["compose"][0]["service"] = "valentines-card"
        pulled = image_target(name="nginx:1.25")
        assert vulns.built_locally(built) and not vulns.built_locally(pulled)
        part = patching.Part("images", "images", [built, pulled])
        result = patching.PartResult(part, layered=["flowers-valentines-card"], refreshed=["nginx:1.25"],
                                     outcome=vulns.PatchOutcome(unchanged=["flowers-valentines-card", "nginx:1.25"]))
        lines = patching._fixed_lines(result)
        assert any("flowers-valentines-card" in l and "zmien FROM w Dockerfile" in l for l in lines)
        assert any("nginx:1.25" in l and "zmien tag w compose" in l for l in lines)
        assert not any(l.startswith(("nowszy obraz", "poprawki zainstalowane")) for l in lines)

    def test_pipe_own_project_is_never_recreated(self):
        scan = vulns.Scan(at=1, targets=[image_target(name="ghcr.io/kvmyk/pipe:0.36.6", project="backend",
                                                      containers=("vps-agent",)), image_target()])
        patch = vulns.image_patch(scan, skip_projects={"backend"})
        assert "-p shop" in patch.command and "-p backend" not in patch.command
        assert patch.skipped == ["vps-agent"] and not patch.manual
        assert "/aktualizuj" in vulns.waiting_note(patch)

    def test_rescan_after_patch_does_not_reuse_an_older_running_scan(self, host, monkeypatch):
        import threading

        calls, release = [], threading.Event()

        def fake_scan(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                release.wait(5)
            return vulns.Scan(at=len(calls)), True
        monkeypatch.setattr(vulns, "scan", fake_scan)
        w = watch.Watcher()

        async def run():
            older = asyncio.create_task(w.vuln_scan_now())
            await asyncio.sleep(0.05)
            since = __import__("time").time()
            newer = asyncio.create_task(w.vuln_scan_now(since=since))
            await asyncio.sleep(0.05)
            release.set()
            return (await older).at, (await newer).at
        assert asyncio.run(run()) == (1, 2) and len(calls) == 2

    def test_explanations_saved_during_a_scan_survive_it(self, host, monkeypatch):
        debian_host(host)
        fake = fake_osv(monkeypatch)
        original = fake.__call__

        def during(url, data=None, timeout=0):
            if url == vulns.OSV_BATCH:
                vulns.save_explanations("pl", {"DSA-1": "Wyjasnienie zapisane w trakcie skanu."})
            return original(url, data, timeout)
        monkeypatch.setattr(vulns, "get_json", during)
        vulns.scan(force=True)
        assert vulns.explanations("pl")["DSA-1"].startswith("Wyjasnienie")

    def test_report_view_shape(self, host, monkeypatch):
        debian_host(host)
        fake_osv(monkeypatch)
        result, _ = vulns.scan()
        view = vulns.report_view(result, {"DSA-1": "Atakujacy moze odczytac pamiec."})
        [group] = view["groups"]
        assert group["kind"] == "system" and group["count"] == 1 and group["kev"] == 1 and group["patchable"]
        [item] = group["items"]
        assert item["level"] == "kev" and item["fixed"] == "3.0.13-1~deb12u1"
        assert item["vulns"][0]["text"] == "Atakujacy moze odczytac pamiec." and item["vulns"][0]["ref"] == "CVE-2026-0001"
        # jeden przycisk na wszystko (jak /zalataj), z tym, co zalata, w etykiecie
        assert view["actions"] == [{"scope": "all", "count": 1, "label": "1 pakiet serwera"}] and view["totals"]["urgent"] == 1
        assert vulns.report_view(None, {}, "off")["notes"] == ["off"]


class TestReadableForPeople:
    """Raport mowi najpierw, co z tym zrobic; bez liczby podatnosci bez poprawki i bez pustych sekcji."""

    def test_counts_are_declined(self):
        assert [vulns.n_packages(n) for n in (1, 2, 5, 12, 22, 25)] == \
            ["1 pakiet", "2 pakiety", "5 pakietow", "12 pakietow", "22 pakiety", "25 pakietow"]

    def test_model_explanations_lose_polish_letters_like_the_rest(self):
        assert vulns.plain("Atakujący może zawiesić usługę") == "Atakujacy moze zawiesic usluge"

    def test_section_without_room_is_named_not_left_empty(self):
        many = [vulns.PackageVulns(f"p{i}", "1", [f"p{i}"], [vulns.Vuln(f"V{i}", [], "high", "opis " * 40, "2")],
                                   ecosystem="npm") for i in range(4)]
        sections = [("Aplikacja /a (npm): 4 zaleznosci", many, 4), ("Aplikacja /b (npm): 4 zaleznosci", many, 4)]
        text = vulns.sections_markdown(sections, {}, budget=1200)
        assert "### Aplikacja /b" not in text and "Nie zmiescilo sie w tej wiadomosci: Aplikacja /b (npm)" in text

    def test_todo_for_a_clean_server_with_old_base_images_and_apps(self):
        built = image_target(name="flowers-valentines-card", project="flowers")
        built.meta["compose"][0]["service"] = "valentines-card"
        app = vulns.TargetResult("app", "/root/quiz", "npm", 10, [vulns.PackageVulns(
            "shell-quote", "1.8.3", ["shell-quote"], [vulns.Vuln("GHSA-1", [], "critical", "x", "1.9.0")], ecosystem="npm")])
        scan = vulns.Scan(at=1, ecosystem="Debian:12", system="Debian 12", checked=10, unfixed=512, targets=[built, app])
        vulns.record_outcome("images", [built], scan)
        lines = vulns.todo(scan)
        assert lines[0] == "Serwer: pakiety aktualne."
        assert lines[1].startswith("Obrazy budowane z Dockerfile (flowers-valentines-card)")
        assert "zmien FROM w Dockerfile na nowsza glowna wersje" in lines[1]
        assert lines[2].startswith("Aplikacje (/root/quiz): podatne zaleznosci") and "(npm audit fix)" in lines[2]
        nothing = vulns.nothing_to_patch(scan)
        assert nothing.startswith("Nie ma nic do zainstalowania.") and "512" not in nothing and "Serwer:" not in nothing
        assert vulns.report_view(scan, {})["todo"] == lines
