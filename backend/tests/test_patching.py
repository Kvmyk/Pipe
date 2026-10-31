"""
/zalataj bez pytan (core/patching.py): jadro przez metapakiety, porzadki apt, obrazy z tagiem do /cofnij i warstwa
z poprawkami, zdalne serwery przez ssh, wynik po rozlaczeniu klienta, narzedzie agenta security_patch, aplikacje
na hoscie (lockfile'e w podkatalogach, yarn, pnpm, uv).
"""

import asyncio
import json

import pytest

from backend.core import digest, fleet, lockfiles, patching, posture, targets, vulns, watch
from backend.tests.test_vulns import DEBIAN, DPKG, FakeShell, debian_host, fake_osv, image_target, osv_vuln, patch_env, write


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    root = tmp_path / "host"
    root.mkdir()
    monkeypatch.setenv("HOST_ROOT", str(root))
    monkeypatch.setenv("HOST_PROC", str(tmp_path / "proc"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(vulns, "_repo_cache", {"at": 0.0, "paths": []})
    return root


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    from backend.core import infra

    async def none():
        return None
    monkeypatch.setattr(infra, "docker_containers", none)


def run(coro):
    return asyncio.run(coro)


# ─── Wersje dpkg i jadro ────────────────────────────────────────────────────

class TestDpkgVersions:

    @pytest.mark.parametrize("a, b, expected", [
        ("1.0", "1.0", 0), ("1.0-1", "1.0-2", -1), ("1:0.9", "2.0", 1), ("1.0~rc1", "1.0", -1),
        ("5.15.0-94.104", "5.15.0-91.101", 1), ("3.0.13-1~deb12u1", "3.0.11-1~deb12u1", 1),
        ("2.36-9+deb12u4", "2.36-9+deb12u10", -1), ("1.0a", "1.0", 1), ("1.0+b1", "1.0", 1), ("1.0.0", "1.0", 1),
    ])
    def test_compare_like_dpkg(self, a, b, expected):
        result = vulns.dpkg_compare(a, b)
        assert (result > 0) - (result < 0) == expected

    def test_highest_fix_for_distro_packages(self):
        item = vulns.PackageVulns("openssl", "3.0.11-1", ["openssl"], [
            vulns.Vuln("DSA-1", [], "high", "", "3.0.13-1~deb12u1"), vulns.Vuln("DSA-2", [], "high", "", "3.0.15-1~deb12u1")],
            ecosystem="Debian:12")
        assert item.fixed_version == "3.0.15-1~deb12u1"


KERNELS = """Package: linux-image-5.15.0-91-generic
Status: install ok installed
Architecture: amd64
Source: linux-signed
Version: 5.15.0-91.101

Package: linux-modules-5.15.0-91-generic
Status: install ok installed
Architecture: amd64
Source: linux
Version: 5.15.0-91.101

Package: linux-modules-5.15.0-94-generic
Status: install ok installed
Architecture: amd64
Source: linux
Version: 5.15.0-94.104

Package: linux-image-generic
Status: install ok installed
Architecture: amd64
Source: linux-meta
Version: 5.15.0.94.91

Package: linux-libc-dev
Status: install ok installed
Architecture: amd64
Source: linux
Version: 5.15.0-94.104
"""


class TestKernel:

    def test_newest_installed_kernel_counts_and_metapackages_are_found(self, host):
        write(host, "/etc/os-release", 'ID=ubuntu\nVERSION_ID="22.04"\nVERSION="22.04.4 LTS (Jammy Jellyfish)"\n')
        write(host, "/var/lib/dpkg/status", KERNELS)
        eco, _, packages, meta = vulns._os_inventory(vulns._read_host)
        linux = [p for p in packages if p.name == "linux"]
        # stare jadro zostaje do autoremove, nowe czeka na restart — liczy sie najnowsze
        assert eco == "Ubuntu:22.04:LTS" and [p.version for p in linux] == ["5.15.0-94.104"]
        assert meta == ["linux-image-generic"]

    def test_kernel_is_upgraded_through_its_metapackages(self):
        kernel = vulns.PackageVulns("linux", "5.15.0-91.101", ["linux-modules-5.15.0-91-generic", "linux-libc-dev"],
                                    ecosystem="Ubuntu:22.04:LTS")
        other = vulns.PackageVulns("openssl", "3.0.2", ["libssl3", "openssl"], ecosystem="Ubuntu:22.04:LTS")
        assert vulns.upgrade_binaries([kernel, other], ["linux-image-generic", "linux-headers-generic"]) == \
            ["libssl3", "linux-headers-generic", "linux-image-generic", "linux-libc-dev", "openssl"]
        # bez metapakietu zostaje to, co bylo (apt i tak nic nie zrobi, ale niczego nie zgubimy)
        assert vulns.upgrade_binaries([kernel]) == ["linux-libc-dev", "linux-modules-5.15.0-91-generic"]

    def test_host_steps_tidy_up_before_and_after(self):
        item = vulns.PackageVulns("openssl", "3.0.2", ["libssl3", "openssl"], ecosystem="Debian:12")
        steps = vulns.host_steps("Debian:12", [item])
        assert [s.required for s in steps] == [False, False, True, False]
        assert "dpkg --force-confold --configure -a" in steps[0].command and "apt-get -s autoremove" in steps[0].command
        assert steps[1].warn and steps[2].command.endswith("--force-confold libssl3 openssl")
        assert "purge -y $old" in steps[3].command and steps[3].command.endswith("apt-get -o DPkg::Lock::Timeout=300 clean")
        alpine = vulns.host_steps("Alpine:v3.20", [vulns.PackageVulns("busybox", "1", ["busybox"], ecosystem="Alpine:v3.20")])
        assert [s.command for s in alpine][1] == "apk add --upgrade busybox"

    def test_docker_packages_are_named_in_the_result(self):
        item = vulns.PackageVulns("docker.io", "24.0.7", ["docker.io"], ecosystem="Ubuntu:22.04:LTS")
        assert vulns.docker_packages([item]) == ["docker.io"]


class TestUbuntuPro:

    def test_pro_only_fixes_are_skipped_without_a_subscription(self):
        pro = vulns.PackageVulns("libtiff", "4.3.0-6", ["libtiff5"], [vulns.Vuln("USN-1", [], "high", "", "4.3.0-6ubuntu0.8+esm1")],
                                 ecosystem="Ubuntu:22.04:LTS")
        scan = vulns.Scan(at=1, ecosystem="Ubuntu:22.04:LTS", items=[pro])
        assert vulns.select(scan, "all") == [] and vulns.waiting_items(scan) == [pro]
        assert "tylko w Ubuntu Pro (pro attach)" in "\n".join(vulns.todo(scan))
        scan.esm = True
        assert vulns.select(scan, "all") == [pro]
        assert vulns.esm_enabled(["ubuntu-esm-infra.sources"]) and not vulns.esm_enabled(["docker.list"])


# ─── Obrazy: tag do /cofnij, warstwa z poprawkami ───────────────────────────

class TestImageLayer:

    def test_rollback_tag_keeps_one_previous_image(self):
        assert vulns.rollback_ref("nginx:1.25") == "nginx:pipe-rollback-1.25"
        assert vulns.rollback_ref("shop-web") == "shop-web:pipe-rollback-latest"
        assert vulns.rollback_ref("registry.local:5000/team/app:v2") == "registry.local:5000/team/app:pipe-rollback-v2"
        assert vulns.rollback_ref("nginx@sha256:" + "a" * 64) == "" and vulns.rollback_ref("Zle;rm") == ""

    def test_layer_restores_user_entrypoint_and_cmd(self):
        target = image_target()
        base = "sha256:" + "b" * 64
        command = vulns.layer_commands(target, "shop_web", base, {"User": "app", "Entrypoint": None,
                                                                  "Cmd": ["python", "app.py"]}, "pipe-patch-0a1b2c3d")
        assert command.startswith("docker run --name pipe-patch-0a1b2c3d --user root --entrypoint sh shop_web -c ")
        assert "--change 'ENTRYPOINT []'" in command and "--change 'CMD [\"python\", \"app.py\"]'" in command
        assert "--change 'USER app'" in command and f"LABEL pipe.patch.base={base}" in command
        assert command.endswith("docker rm -f -v pipe-patch-0a1b2c3d >/dev/null 2>&1; exit $rc")
        root = vulns.layer_commands(target, "shop_web", base, {"User": ""}, "pipe-patch-0a1b2c3d")
        assert "--user root" not in root and "USER" not in root               # obraz roota zostaje bez zmiany USER

    def test_layer_refuses_odd_input(self):
        target, base = image_target(), "sha256:" + "b" * 64
        assert vulns.layer_commands(target, "shop_web", base, {"User": "x;rm -rf /"}, "pipe-patch-0a1b2c3d") == ""
        assert vulns.layer_commands(target, "shop_web", base, {"Cmd": "sh -c x"}, "pipe-patch-0a1b2c3d") == ""
        assert vulns.layer_commands(target, "nginx@sha256:" + "a" * 64, base, {}, "pipe-patch-0a1b2c3d") == ""
        assert vulns.layer_commands(target, "shop_web", "zly", {}, "pipe-patch-0a1b2c3d") == ""
        assert vulns.layer_commands(target, "shop_web", base, {}, "pipe-patch-x; reboot") == ""
        pro = image_target()
        pro.items[0].fixable[0].fixed = "3.0.13-1+esm1"                        # tylko Ubuntu Pro — apt w obrazie nie znajdzie
        assert vulns.layer_run(pro) == ""


def image_run(monkeypatch, shell, target):
    """Jedna czesc `images` z podrobionym Dockerem (tryb native)."""
    patch_env(monkeypatch, shell=shell)
    scan = vulns.Scan(at=1, ecosystem="Debian:12", targets=[target])
    job = patching.plan(scan, "images")
    runner = patching.Run(job, "cli:t")
    return run(runner._images(job.parts[0]))


class TestRefreshOrLayer:

    def test_newer_image_is_kept_without_a_layer(self, monkeypatch):
        shell = FakeShell([("{{.Id}}", ("sha256:" + "f" * 64, "", 0)), ("{{json .Config.Labels}}", ("null", "", 0))])
        result = image_run(monkeypatch, shell, image_target())
        assert result.refreshed == ["shop_web"] and not result.layered and not shell.having("docker run --name")

    def test_image_patched_by_pipe_is_layered_again_from_its_base(self, monkeypatch):
        """Pod tagiem znow obraz bazowy (pull przywrocil oryginal) — to nie "nowszy obraz", tylko ponowna warstwa."""
        target = image_target()
        base = "sha256:" + "c" * 64
        shell = FakeShell([("{{.Id}}", (base, "", 0)), ("{{json .Config.Labels}}", (json.dumps({vulns.PATCH_LABEL: base}), "", 0)),
                           ("{{json .Config}}", (json.dumps({"User": "", "Cmd": ["x"]}), "", 0))])
        result = image_run(monkeypatch, shell, target)
        assert result.layered == ["shop_web"] and f"pipe.patch.base={base}" in shell.having("docker run --name")[0]

    def test_failed_layer_keeps_the_previous_patched_image(self, monkeypatch):
        target = image_target()
        base = "sha256:" + "c" * 64
        shell = FakeShell([("{{.Id}}", (base, "", 0)), ("{{json .Config.Labels}}", (json.dumps({vulns.PATCH_LABEL: base}), "", 0)),
                           ("{{json .Config}}", (json.dumps({"Cmd": ["x"]}), "", 0)),
                           ("docker run --name", ("", "E: Failed to fetch", 100))])
        result = image_run(monkeypatch, shell, target)
        assert result.failed == ["shop_web"] and any("bez wsparcia" in w for w in result.warnings)
        # pull przywrocil pod tagiem obraz bez poprawek — kontener nie moze na niego wrocic
        assert shell.having("docker tag eeeeeeeeeeee shop_web")


# ─── Zdalne serwery ─────────────────────────────────────────────────────────

def ssh_target(name="web2", user="deploy"):
    target = targets.Target(name, "ssh", host="10.0.0.5", user=user)
    targets.save_target(target)
    return target


class TestFleet:

    def test_dump_round_trip_filters_the_package_database(self):
        marker = "==PIPE-abc=="
        command = fleet.dump_command(marker)
        assert "grep -E '^(Package|Status|Source|Version|Architecture):|^$' /var/lib/dpkg/status" in command
        text = f"\n{marker} /etc/os-release\n{DEBIAN}\n{marker} /var/lib/dpkg/status\n{DPKG}\n{marker} /etc/apt/sources.list.d\nubuntu-esm-infra.list\n"
        files, listing = fleet.parse_dump(text, marker)
        assert set(files) == {"/etc/os-release", "/var/lib/dpkg/status"} and listing == ["ubuntu-esm-infra.list"]
        assert files["/var/lib/dpkg/status"].startswith("Package: openssl")

    def test_root_or_passwordless_sudo(self):
        command = fleet.as_root(targets.Target("web2", "ssh", host="10.0.0.5", user="deploy"), "apt-get update")
        assert command.startswith("ssh -o BatchMode=yes") and "sudo -n sh -c" in command and "deploy@10.0.0.5" in command
        assert fleet.needs_password("sudo: a password is required")

    def test_remote_server_in_the_scan_report_and_patch(self, host, monkeypatch):
        debian_host(host)
        fake_osv(monkeypatch)
        ssh_target()
        dump = vulns.ServerDump("web2", "deploy@10.0.0.5", files={"/etc/os-release": DEBIAN, "/var/lib/dpkg/status": DPKG},
                                listing=[])
        result, _ = vulns.scan(servers=[dump, vulns.ServerDump("db1", "10.0.0.6", error="Connection timed out")])
        [web2] = vulns.servers_with_items(result)
        assert web2.name == "web2" and [p.name for p in web2.items] == ["openssl"] and web2.urgent
        assert any("Serwer db1: brak polaczenia (Connection timed out)" in line for line in vulns.todo(result))
        assert "### Serwer web2 (Debian GNU/Linux 12 (bookworm)): 1 pakiet z poprawka" in vulns.report_markdown(result, {})
        view = vulns.report_view(result, {})
        assert [g["kind"] for g in view["groups"]] == ["system", "server"] and view["totals"]["servers"] == 1
        assert view["actions"][0]["label"] == "1 pakiet serwera, serwer zdalny (1 pakiet)"
        # alert o zdalnych serwerach
        w = watch.Watcher()
        events = w.apply(w.vuln_findings(result), watch.VULN_SCOPE)
        assert {e.key for e in events} == {"vuln:system", "vuln:servers"}
        # plan: tylko zdalne / jeden serwer / wszystko
        assert [p.kind for p in patching.plan(result, "servers").parts] == ["server"]
        assert [p.name for p in patching.plan(result, "server:web2").parts] == ["web2"]
        assert [p.kind for p in patching.plan(result, "all").parts] == ["system", "server"]
        assert patching.parse_scope("web2") == "server:web2" and patching.parse_scope("WEB3") == ""

    def test_remote_patch_runs_over_ssh_and_remembers_what_did_not_change(self, host, monkeypatch):
        debian_host(host)
        fake_osv(monkeypatch)
        target = ssh_target()
        dump = vulns.ServerDump("web2", "deploy@10.0.0.5", files={"/etc/os-release": DEBIAN, "/var/lib/dpkg/status": DPKG})
        result, _ = vulns.scan(servers=[dump])
        shell = patch_env(monkeypatch)
        job = patching.plan(result, "server:web2")
        runner = patching.Run(job, "cli:t")
        part = run(runner._server(job.parts[0]))
        steps = vulns.host_steps("Debian:12", job.parts[0].items)
        assert shell.ran == [fleet.as_root(target, s.command) for s in steps] and part.exit_code == 0 and part.entry_id
        outcome = vulns.record_outcome("server:web2", job.parts[0].items, result)       # apt nic nie podbil
        assert outcome.unchanged == ["openssl"]
        [web2] = result.of_kind("server")
        assert vulns.select_server(web2) == [] and vulns.select(result, "all")          # host liczy sie osobno


# ─── Przebieg: tlo, dolaczanie, wynik po rozlaczeniu ────────────────────────

class TestRun:

    def test_result_stays_for_a_client_that_lost_the_connection(self, monkeypatch):
        import time

        from backend.tests.test_server_commands import exchange

        patch_env(monkeypatch, shell=FakeShell([("docker info", ("", "", 1))]))
        monkeypatch.setattr(patching, "_current", None)
        assert exchange([{"command": "patch_status"}])[0][-1]["data"] == {"lost": True}

        async def scenario():
            current = patching.start(self.job(), interface="telegram:5")
            await current.task
            return current
        current = run(scenario())
        assert patching.latest() is current and patching.current() is None
        frames = exchange([{"command": "patch_status"}])[0]
        assert frames[-1]["data"]["scope"] == "all" and "exit_code" in frames[-1]["data"]
        current.ended = time.time() - patching.RESULT_KEEP - 1
        assert patching.latest() is None

    def job(self):
        item = vulns.PackageVulns("openssl", "3.0.11", ["openssl"], [vulns.Vuln("DSA-1", [], "high", "", "3.0.13")],
                                  ecosystem="Debian:12")
        return patching.Job("all", [patching.Part("system", "system", [item])], vulns.Scan(at=1, ecosystem="Debian:12"))

    def test_result_reaches_a_late_follower_and_a_reminder_when_nobody_got_it(self, monkeypatch):
        from backend.core import reminders

        patch_env(monkeypatch, shell=FakeShell([("docker info", ("", "", 1))]))

        async def scenario():
            current = patching.start(self.job(), interface="telegram:5")
            assert patching.current() is current
            await current.task
            late = [e async for e in current.follow()]               # cala historia, do wyniku wlacznie
            return current, late
        current, late = run(scenario())
        assert isinstance(late[-1], patching.Done) and any(getattr(e, "text", "").startswith("Ten serwer") for e in late)
        assert patching.current() is None
        [reminder] = reminders.load_reminders()
        assert reminder.to == "telegram:5" and reminder.text.startswith("Latanie (/zalataj) zakonczone.")

    def test_delivered_result_is_not_sent_again(self, monkeypatch):
        from backend.core import reminders

        patch_env(monkeypatch, shell=FakeShell([("docker info", ("", "", 1))]))

        async def scenario():
            current = patching.start(self.job(), interface="cli:k")
            async for event in current.follow():
                if isinstance(event, patching.Done):
                    current.delivered = True
            await current.task
        run(scenario())
        assert reminders.load_reminders() == []


# ─── Narzedzie agenta: jedno potwierdzenie na cala latke ────────────────────

class TestSecurityPatchTool:

    def test_one_confirmation_then_the_same_run_as_patch(self, host, monkeypatch, tmp_path):
        from backend.config import settings
        from backend.core import audit
        from backend.core.agent import VPSAgent
        from backend.core.events import Progress
        from backend.tests.fakes import FakeClient, assert_history_valid, completion

        debian_host(host)
        fake_osv(monkeypatch)
        monkeypatch.setattr(settings, "VIBE_EVERY", 0)
        monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
        monkeypatch.setattr(watch, "_watcher", None)
        shell = patch_env(monkeypatch, shell=FakeShell([("docker info", ("", "", 1))]))
        agent = VPSAgent(client=FakeClient([completion(tool_calls=[("c1", "security_patch", {})]), completion("Zalatane.")]))

        async def chat():
            return [e async for e in agent.chat("s", "zalataj serwer")]

        async def confirm():
            return [e async for e in agent.confirm("s", True)]
        events = run(chat())
        ask = next(e for e in events if isinstance(e, str) and "[POTWIERDZ]" in e)
        assert "wymaga potwierdzenia" in ask and "Ten serwer: 1 pakiet (openssl)" in ask and "Bezpiecznik:" in ask
        assert not shell.ran                                                  # przed TAK nic sie nie dzieje
        done = run(confirm())
        assert any(isinstance(e, Progress) and "instaluje poprawki" in e.text for e in done)
        session = agent._sessions["s"]
        result = next(m for m in session.messages if m.get("tool_call_id") == "c1")
        assert result["content"].startswith("Gotowe") and shell.having("--only-upgrade")
        assert_history_valid(session.messages)

    def test_viewer_and_observe_never_get_it(self):
        from backend.core.agent import OBSERVE_WRITE_OPERATIONS, VIEWER_WRITE_OPERATIONS

        assert VIEWER_WRITE_OPERATIONS["security_patch"] is None and OBSERVE_WRITE_OPERATIONS["security_patch"] is None


# ─── Aplikacje na hoscie ────────────────────────────────────────────────────

class TestApps:

    def test_yarn_v1_and_berry(self, tmp_path):
        v1 = ('# yarn lockfile v1\n\n"@babel/code-frame@^7.0.0", "@babel/code-frame@^7.10.4":\n  version "7.12.13"\n'
              '  resolved "https://registry.yarnpkg.com/x"\n\nlodash@^4.17.20:\n  version "4.17.21"\n')
        (tmp_path / "yarn.lock").write_text(v1)
        assert lockfiles.parse(str(tmp_path / "yarn.lock")) == [("npm", "@babel/code-frame", "7.12.13"), ("npm", "lodash", "4.17.21")]
        berry = ('__metadata:\n  version: 6\n\n"lodash@npm:^4.17.21":\n  version: 4.17.21\n\n'
                 '"shop@workspace:.":\n  version: 0.0.0-use.local\n\n"real@npm:alias@^1":\n  version: 1.2.0\n')
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "yarn.lock").write_text(berry)
        assert lockfiles.parse(str(tmp_path / "b" / "yarn.lock")) == [("npm", "lodash", "4.17.21"), ("npm", "alias", "1.2.0")]

    @pytest.mark.parametrize("text", [
        "lockfileVersion: 5.4\n\npackages:\n\n  /lodash/4.17.21:\n    resolution: {}\n  /@babel/core/7.12.3_react@18.2.0:\n    dev: true\n",
        "lockfileVersion: '6.0'\n\npackages:\n\n  /lodash@4.17.21:\n    resolution: {}\n  /@babel/core@7.12.3(react@18.2.0):\n    dev: true\n",
        "lockfileVersion: '9.0'\n\nimporters:\n  .:\n    dependencies: {}\n\npackages:\n\n  lodash@4.17.21:\n    resolution: {}\n"
        "  '@babel/core@7.12.3':\n    resolution: {}\n\nsnapshots:\n\n  lodash@4.17.21: {}\n",
    ])
    def test_pnpm_versions(self, tmp_path, text):
        (tmp_path / "pnpm-lock.yaml").write_text(text)
        assert sorted(lockfiles.parse(str(tmp_path / "pnpm-lock.yaml"))) == [("npm", "@babel/core", "7.12.3"),
                                                                           ("npm", "lodash", "4.17.21")]

    def test_uv_lock(self, tmp_path):
        pytest.importorskip("tomllib")
        text = ('version = 1\n\n[[package]]\nname = "shop"\nversion = "0.1.0"\nsource = { editable = "." }\n\n'
                '[[package]]\nname = "Jinja2"\nversion = "3.1.2"\nsource = { registry = "https://pypi.org/simple" }\n')
        (tmp_path / "uv.lock").write_text(text)
        assert lockfiles.parse(str(tmp_path / "uv.lock")) == [("PyPI", "jinja2", "3.1.2")]

    def test_tree_finds_backend_and_frontend_but_not_dependencies(self, tmp_path):
        app = tmp_path / "shop"
        for path in ("backend/requirements.txt", "frontend/package-lock.json", "frontend/node_modules/x/package-lock.json",
                     "a/b/c/d/package-lock.json", ".git/package-lock.json"):
            (app / path).parent.mkdir(parents=True, exist_ok=True)
            (app / path).write_text("{}")
        found = lockfiles.find_tree(str(app))
        assert sorted(found) == [str(app / "backend"), str(app / "frontend")]

    def test_compose_projects_are_scanned_without_directory(self, host, monkeypatch):
        """Bez wpisow w DIRECTORY: katalog projektu compose dzialajacego kontenera, lockfile w podkatalogu."""
        from types import SimpleNamespace

        debian_host(host)
        write(host, "/srv/quiz/web/package-lock.json", '{"packages": {"node_modules/shell-quote": {"version": "1.8.3"}}}')
        fake = fake_osv(monkeypatch)
        fake.ids["shell-quote"] = ["GHSA-1"]
        fake.details["GHSA-1"] = osv_vuln("GHSA-1", "shell-quote", "1.8.4", eco="npm")
        containers = [SimpleNamespace(name="quiz-web", image="quiz-web", state="exited", image_id="", project="quiz",
                                      service="web", workdir="/srv/quiz", compose_files=[], pid=0, layers=[])]
        result, _ = vulns.scan(containers=containers)
        [app] = result.of_kind("app")
        assert app.name == "/srv/quiz/web" and [p.name for p in app.items] == ["shell-quote"]
        assert any(line.startswith("Zaleznosci aplikacji (/srv/quiz/web)") for line in vulns.todo(result))

    def test_nested_roots_are_walked_once_but_deep_ones_stay(self):
        roots = ["/srv/shop", "/srv/shop/backend", "/root", "/root/TeamBIT/Quiz", "/srv/shop"]
        # /srv/shop/backend przejdzie razem z /srv/shop; /root/TeamBIT/Quiz zostaje — duze /root moze wyczerpac limit
        assert vulns._outer_roots(roots) == ["/root", "/srv/shop", "/root/TeamBIT/Quiz"]

    def test_tree_walks_shallow_directories_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lockfiles, "MAX_DIRS", 6)
        for i in range(20):
            (tmp_path / "aaa-deep" / f"d{i}").mkdir(parents=True)
        (tmp_path / "zzz-app").mkdir()
        (tmp_path / "zzz-app" / "package-lock.json").write_text("{}")
        assert list(lockfiles.find_tree(str(tmp_path))) == [str(tmp_path / "zzz-app")]

    def test_git_repositories_on_the_host_count_too(self, host, monkeypatch):
        (host / "root" / "apps" / "blog" / ".git").mkdir(parents=True)
        write(host, "/root/apps/blog/requirements.txt", "django==4.2.1\n")
        assert "/root/apps/blog" in vulns.app_roots()
        assert [t.name for t in vulns.app_targets()] == ["/root/apps/blog"]


def notification_scan(esm=False):
    """Host z jadrem (KEV) i poprawka tylko z Ubuntu Pro, obraz bez compose, zdalny serwer, krytyczna aplikacja."""
    V, P, T = vulns.Vuln, vulns.PackageVulns, vulns.TargetResult
    items = vulns.sort_items([
        P("linux", "5.15.0-91.101", ["linux-libc-dev"], [V("USN-1", ["CVE-2024-1086"], "high", "x", "5.15.0-101.111", kev=True)],
          ecosystem="Ubuntu:22.04:LTS"),
        P("libxml2", "2.9.13", ["libxml2"], [V("USN-2", ["CVE-2"], "high", "x", "2.9.13+esm1")], ecosystem="Ubuntu:22.04:LTS"),
    ])
    image = T("image", "redis:7", "Debian:12", 10, [P("zlib", "1", ["zlib1g"], [V("DSA-3", ["CVE-3"], "medium", "x", "2")],
                                                    ecosystem="Debian:12")],
              meta={"image": "redis:7", "image_id": "sha256:" + "c" * 12, "containers": ["cache"], "compose": []})
    server = T("server", "web2", "Debian:12", 10, [P("openssl", "1", ["libssl3"], [V("DSA-4", ["CVE-4"], "high", "x", "2")],
                                                     ecosystem="Debian:12")], meta={"system": "Debian 12"})
    app = T("app", "/srv/quiz", "npm", 10, [P("lodash", "4.17.20", ["lodash"], [V("GHSA-5", ["CVE-5"], "critical", "x", "4.17.21")],
                                              ecosystem="npm")])
    return vulns.Scan(at=1, ecosystem="Ubuntu:22.04:LTS", system="Ubuntu 22.04", checked=500, items=items,
                      targets=[image, server, app], esm=esm)


class TestNotifications:
    """Alerty, /podatnosci, /audyt i raport poranny: jasno, bez obietnic, ktorych /zalataj nie spelni."""

    def test_alert_titles_read_well_for_any_count(self, monkeypatch):
        w = watch.Watcher()
        titles = {f.key: f.title for f in w.vuln_findings(notification_scan())}
        assert titles["vuln:system"] == "Pilne podatnosci w pakietach serwera: 2, w tym aktywnie wykorzystywane"
        assert titles["vuln:servers"] == "Pilne podatnosci na serwerze web2"
        assert titles["vuln:apps"] == "Krytyczne podatnosci w zaleznosciach aplikacji: 1"
        monkeypatch.setenv("PIPE_LANG", "en")
        titles = {f.key: f.title for f in watch.Watcher().vuln_findings(notification_scan())}
        assert titles["vuln:system"] == "Urgent vulnerabilities in server packages: 2, including actively exploited ones"

    def test_alerts_do_not_promise_what_patch_cannot_do(self):
        scan = notification_scan()
        system = vulns.system_alert(scan, {})
        assert "**linux (jadro)**" in system and "· poprawka tylko w Ubuntu Pro" in system and "pro attach" in system
        assert "pro attach" not in vulns.system_alert(notification_scan(esm=True), {})       # z subskrypcja apt je znajdzie
        scan.targets[0].items[0].fixable[0].severity = "high"                                 # obraz bez compose w alercie
        images = vulns.images_alert(scan, {})
        assert "### Obraz redis:7 (cache) — kontener bez compose, tego nie odswieze sam" in images
        apps = vulns.apps_alert(scan, {})
        assert "kontenerze: npm audit fix)" in apps and "pip" not in apps and "composer" not in apps
        assert "/zalataj aplikacje" in apps and "nie zmienia" not in apps

    def test_most_dangerous_first_when_the_message_is_full(self):
        sections = vulns.report_sections(notification_scan())
        heads = [head for head, _, _ in sections]
        # host, potem od najgrozniejszego: krytyczna aplikacja i wysoki serwer przed srednim obrazem
        assert heads[0].startswith("Pakiety tego serwera") and heads[1].startswith("Aplikacja /srv/quiz")
        assert heads[2].startswith("Serwer web2") and heads[3].startswith("Obraz redis:7")

    def test_todo_names_this_server_only_when_there_are_others(self):
        lines = vulns.todo(notification_scan())
        assert lines[0].startswith("Ten serwer: 1 pakiet z poprawka (pilne: 1) — zainstaluje /zalataj.")
        assert any(line.startswith("Serwer web2: 1 pakiet z poprawka (pilne: 1) — zainstaluje /zalataj (tylko ten serwer: "
                                   "/zalataj web2)") for line in lines)
        alone = notification_scan()
        alone.targets = [t for t in alone.targets if t.kind != "server"]
        assert vulns.todo(alone)[0].startswith("Serwer: ")

    def test_audit_kev_first_unknown_shown_and_model_uses_the_tool(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
        from tg_format import format_audit

        scan = notification_scan()
        scan.targets.append(vulns.TargetResult("server", "db1", None, 0, [], meta={"error": "Connection timed out"}))
        audit = posture.Audit()
        posture.vuln_findings(audit, scan)
        ordered = [f.id for f in audit.sorted()]
        assert ordered[0] == "vuln-kev" and "w pakietach serwera: 1" in audit.sorted()[0].title
        assert "Nie sprawdzono:</b> Serwer db1: brak polaczenia" in format_audit(audit.to_data())
        model = audit.render()
        assert "security_patch, scope system" in model and "apt-get" not in model

    def test_audit_says_what_patch_will_skip(self):
        from backend.tests.test_vulns import image_target

        audit = posture.Audit()
        posture.vuln_findings(audit, notification_scan())
        found = {f.id: f for f in audit.findings}
        assert found["vuln-kev"].detail == "linux (jadro)" and found["vuln-kev"].patch == "system"
        # jedyny pilny pakiet poza KEV ma poprawke tylko w Ubuntu Pro — bez "Napraw", za to z powodem
        assert found["vuln-high"].detail == "libxml2 (tylko Ubuntu Pro)" and found["vuln-high"].patch == ""
        assert "pro attach" in found["vuln-high"].fix
        own = vulns.Scan(at=1, targets=[image_target(name="ghcr.io/kvmyk/pipe:0.37.1", project="backend",
                                                     containers=("vps-agent",))])
        audit = posture.Audit()
        posture.vuln_findings(audit, own, skip_projects={"backend"})      # kontenery Pipe odswieza /aktualizuj
        images = next(f for f in audit.findings if f.id == "vuln-images")
        assert images.patch == "" and images.fix == "Kontenery Pipe (vps-agent) odswieza /aktualizuj"

    def test_digest_lines_decline_counts_and_mention_the_reboot(self):
        scan = notification_scan()
        scan.reboot = True
        lines = digest.vuln_lines(scan)
        assert lines[0] == "pakiety serwera: 2 z poprawka, w tym 2 pilne (1 aktywnie wykorzystywany)"
        assert any("czeka na restart" in line for line in lines) and lines[-1].endswith("/zalataj")
        one = vulns.Scan(at=1, ecosystem="Debian:12", items=scan.items[:1])
        assert vulns.summary_line(one) == "pakiety serwera: 1 z poprawka, w tym 1 pilny (1 aktywnie wykorzystywany)"

    def test_every_alert_fits_one_telegram_message(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
        from tg_format import format_alert

        long = "Atakujacy moze wykonac kod na serwerze, jesli usluga przyjmuje polaczenia z internetu. " * 3
        many = [vulns.PackageVulns(f"p{i}", "1", [f"p{i}"], [vulns.Vuln(f"DSA-{i}", [], "critical", long, "2")],
                                   ecosystem="Debian:12") for i in range(40)]
        targets = [vulns.TargetResult("image", f"img{i}:1", "Debian:12", 40, many,
                                      meta={"image": f"img{i}:1", "image_id": "sha256:" + "d" * 12, "containers": [f"c{i}"],
                                            "compose": []}) for i in range(8)]
        targets += [vulns.TargetResult("server", f"s{i}", "Debian:12", 40, many, meta={"system": "Debian 12"}) for i in range(8)]
        scan = vulns.Scan(at=1, ecosystem="Debian:12", items=many, targets=targets)
        for finding in watch.Watcher().vuln_findings(scan):
            assert len(format_alert({"severity": finding.severity, "title": finding.title, "report": finding.report})) <= 4096


class TestReboot:

    def test_reboot_required_is_a_todo_line(self, host, monkeypatch):
        debian_host(host)
        fake_osv(monkeypatch)
        write(host, "/run/reboot-required", "*** System restart required ***\n")
        result, _ = vulns.scan()
        assert result.reboot and any("czeka na restart" in line for line in vulns.todo(result))
