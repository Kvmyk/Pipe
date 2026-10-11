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
        assert "DSA-1 (CVE-2026-0001) [KEV]" in vulns.describe_item(openssl)

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
        assert not audit.findings and "300" in audit.passed[0] and "40" in audit.passed[0]

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
        result, note = asyncio.run(w.vuln_for_audit())
        assert result.items and note == "" and len(fake.batches) == batches
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
        assert "--force-confold openssl" in events[0].detail
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
        assert "1" in lines[0] and lines[1].startswith("openssl") and "/audyt" in lines[-1]
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


class TestPatchCommand:

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

    def test_preview_in_docker_gives_host_command(self, server):
        from backend.tests.test_server_commands import exchange

        [[frame]] = exchange([{"command": "patch"}])
        data = frame["data"]
        assert data["packages"] == ["openssl"] and not data["can_execute"] and "powloce serwera" in data["reason"]
        assert "--only-upgrade -o Dpkg::Options::=--force-confold libssl3 openssl" in data["command"]
        assert data["token"] == vulns.patch_token(data["command"]) and "`" in data["text"]
        [[refused]] = exchange([{"command": "patch", "execute": True, "token": data["token"]}])
        assert refused["status"] == "error"

    def test_execute_needs_matching_token_and_runs_through_the_fuse(self, server, monkeypatch):
        from backend.core import executor, journal
        from backend.tests.test_server_commands import exchange

        ran = []

        async def fake_execute(cmd, cwd=None, timeout=None):
            ran.append((cmd, timeout))
            return "Setting up openssl (3.0.13-1~deb12u1) ...\n", "", 0

        monkeypatch.setattr(server, "_patch_blocker", lambda role: "")
        monkeypatch.setattr(executor, "execute", fake_execute)
        [[preview]] = exchange([{"command": "patch", "scope": "urgent"}])
        token = preview["data"]["token"]
        [[stale]] = exchange([{"command": "patch", "scope": "urgent", "execute": True, "token": "nope"}])
        assert stale["status"] == "error" and not ran
        frames = exchange([{"command": "patch", "scope": "urgent", "execute": True, "token": token}])[0]
        assert "data" in frames[-1], frames[-1]
        done = frames[-1]["data"]
        assert ran and ran[0][0] == preview["data"]["command"] and ran[0][1] >= 600
        assert done["exit_code"] == 0 and "Setting up openssl" in done["text"] and done["entry_id"]
        entry = journal.load(done["entry_id"])
        assert entry.tool == "patch" and any("cofam" in n or "rolled back" in n for n in entry.notes)

    def test_blockers(self, monkeypatch):
        import backend.server as server
        from backend.core import runtime

        monkeypatch.setattr(runtime, "kind", lambda: "native")
        monkeypatch.setattr(runtime, "observe", lambda: False)
        assert server._patch_blocker("admin") == ""
        assert "viewer" in server._patch_blocker("viewer")
        monkeypatch.setattr(runtime, "observe", lambda: True)
        assert "PIPE_OBSERVE" in server._patch_blocker("admin")

    def test_names_outside_the_package_pattern_never_reach_the_shell(self):
        item = vulns.PackageVulns("x", "1", ["openssl", "evil;rm -rf /", "$(id)", "libc6:i386"])
        assert vulns.upgrade_command("Debian:12", [item]).endswith("--force-confold libc6:i386 openssl")
