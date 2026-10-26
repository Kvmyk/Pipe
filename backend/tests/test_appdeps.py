"""
Zaleznosci aplikacji w /zalataj (core/appdeps.py + czesc `apps` w core/patching.py): wybor menedzera, plan,
komendy w izolowanym kontenerze, edycja requirements, przebudowa projektu compose, wstrzymanie tego, czego
menedzer nie podbil, i /cofnij w trybie docker dla plikow hosta tylko do odczytu.
"""

import asyncio
import errno
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.core import appdeps, journal, patching, vulns
from backend.tests.test_vulns import FakeShell, patch_env, write


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
    from backend.core import infra

    async def none():
        return []
    monkeypatch.setattr(infra, "docker_containers", none)


def run(coro):
    return asyncio.run(coro)


def item(name, version, fixed, eco="npm", severity="high"):
    return vulns.PackageVulns(name, version, [name], [vulns.Vuln(f"GHSA-{name}", [], severity, "", fixed)], ecosystem=eco)


def app(host, directory="/srv/shop", files=None, items=None, contents=None):
    for path, text in (contents or {}).items():
        write(host, f"{directory}/{path}", text)
    files = [f"{directory}/{f}" for f in (files or [])]
    return vulns.TargetResult("app", directory, "npm", items=list(items or []), meta={"files": files})


NPM = {"package.json": json.dumps({"dependencies": {"lodash": "^4.17.0"}}), "package-lock.json": "{}"}


class TestPlan:

    def test_npm_direct_and_transitive(self, host):
        target = app(host, files=["package-lock.json"], contents=NPM,
                     items=[item("lodash", "4.17.20", "4.17.21"), item("minimist", "1.2.0", "1.2.6")])
        bump = appdeps.plan_target(target)
        [npm] = bump.bumps
        assert npm.manager == "npm" and npm.direct == {"lodash"}
        command, _ = appdeps.script(npm)
        assert "npm install --package-lock-only --ignore-scripts" in command and "lodash@4.17.21" in command
        assert "npm audit fix --package-lock-only --ignore-scripts" in command and "minimist" not in command
        assert sorted(os.path.basename(f) for f in bump.files) == ["package-lock.json", "package.json"]

    def test_exact_pins_stay_exact(self, host):
        target = app(host, files=["package-lock.json"], items=[item("lodash", "4.17.20", "4.17.21"),
                                                              item("axios", "1.6.0", "1.7.4")],
                     contents={"package.json": json.dumps({"dependencies": {"lodash": "4.17.20", "axios": "^1.6.0"}}),
                               "package-lock.json": "{}"})
        [npm] = appdeps.plan_target(target).bumps
        command, _ = appdeps.script(npm)
        assert npm.exact == {"lodash"} and "--save-exact lodash@4.17.21" in command
        assert "--loglevel=error axios@1.7.4" in command

    def test_yarn_1_is_left_for_the_report_and_berry_is_bumped(self, host):
        v1 = app(host, "/srv/a", ["yarn.lock"], [item("lodash", "4.17.20", "4.17.21")],
                 {"yarn.lock": "# yarn lockfile v1\n", "package.json": "{}"})
        berry = app(host, "/srv/b", ["yarn.lock"], [item("lodash", "4.17.20", "4.17.21")],
                    {"yarn.lock": "__metadata:\n  version: 8\n", "package.json": "{}"})
        assert appdeps.plan_target(v1).bumps == [] and "yarn 1" in appdeps.plan_target(v1).manual[0]
        [yarn] = appdeps.plan_target(berry).bumps
        command, env = appdeps.script(yarn)
        assert yarn.manager == "yarn" and "yarn up -R --mode=update-lockfile lodash" in command
        assert env["YARN_ENABLE_SCRIPTS"] == "0"

    def test_odd_names_versions_and_held_are_skipped(self, host):
        target = app(host, files=["package-lock.json"], contents=NPM, items=[
            item("lodash; rm -rf /", "1.0.0", "1.0.1"), item("ok", "1.0.0", "1.0.1 && id"),
            item("held", "1.0.0", "1.0.1"), item("fine", "1.0.0", "1.0.1")])
        held = {appdeps.app_key(target, target.items[2])}
        bump = appdeps.plan_target(target, held)
        assert [p.name for p in bump.items] == ["fine"] and bump.bumps[0].packages == [("fine", "1.0.1")]

    def test_go_gets_a_v_prefix_and_stdlib_is_manual(self, host):
        target = app(host, files=["go.mod"], contents={"go.mod": "module x\n"}, items=[
            item("golang.org/x/net", "0.17.0", "0.23.0", eco="Go"), item("stdlib", "1.21.0", "1.21.8", eco="Go")])
        bump = appdeps.plan_target(target)
        command, env = appdeps.script(bump.bumps[0])
        assert "go get golang.org/x/net@v0.23.0 && go mod tidy" == command and env["GOFLAGS"] == "-mod=mod"
        assert any("stdlib" in reason for reason in bump.manual)

    def test_requirements_with_hashes_are_not_edited(self, host):
        target = app(host, files=["requirements.txt"], contents={"requirements.txt": "django==4.2.0 --hash=sha256:x\n"},
                     items=[item("django", "4.2.0", "4.2.11", eco="PyPI")])
        bump = appdeps.plan_target(target)
        assert bump.bumps == [] and "--hash" in bump.manual[0]

    @pytest.mark.parametrize("manager, fragment", [
        ("cargo", "cargo update -p serde --precise 1.0.1 || rc=1;"),
        ("composer", "--no-install --no-scripts --no-plugins"),
        ("bundler", "bundle lock --update serde"),
        ("poetry", "poetry update --lock --no-interaction serde"),
        ("uv", "--upgrade-package serde==1.0.1"),
        ("pnpm", "pnpm update --lockfile-only --depth Infinity serde"),
    ])
    def test_manager_commands(self, manager, fragment):
        command, _ = appdeps.script(appdeps.Bump(manager, [("serde", "1.0.1")]))
        assert fragment in command

    def test_container_sees_only_the_app_and_runs_as_its_owner(self):
        command = appdeps.docker_command("/srv/shop", appdeps.Bump("npm", [("a", "1.0.0")], {"a"}), 1000, 1000)
        assert "--cap-drop ALL" in command and "--user 1000:1000" in command and "-v /srv/shop:/work" in command
        assert "docker.sock" not in command and "--privileged" not in command and "no-new-privileges" in command


class TestCpuLimit:

    def test_small_server_gets_at_most_its_cpus(self, tmp_path, monkeypatch):
        from backend.core import hostinfo

        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "cpuinfo").write_text("processor : 0\nmodel name : x\n")
        monkeypatch.setenv("HOST_PROC", str(proc))
        assert hostinfo.cpu_limit("2") == "1" and hostinfo.cpu_limit("0.5") == "0.5" and hostinfo.cpu_limit("x") == "1"
        command = appdeps.docker_command("/srv/shop", appdeps.Bump("npm", [("a", "1.0.0")], {"a"}), 0, 0)
        assert "--cpus 1 " in command

    def test_rejected_cpu_quota_drops_the_limit(self, monkeypatch):
        from backend.core import hostinfo

        monkeypatch.setattr(hostinfo, "_cpu_limit_rejected", False)
        error = ('docker: Error response from daemon: failed to create task for container: ... failed to write "100000": '
                 'write /sys/fs/cgroup/cpu,cpuacct/docker/3b06/cpu.cfs_quota_us: invalid argument.')
        assert not hostinfo.cpu_limit_rejected("npm ERR! code ERESOLVE", 1)
        assert not hostinfo.cpu_limit_rejected(error, 1)
        assert "--cpus" in appdeps.docker_command("/srv/shop", appdeps.Bump("npm", [("a", "1.0.0")], {"a"}), 0, 0)
        assert hostinfo.cpu_limit_rejected(error, 125)
        command = appdeps.docker_command("/srv/shop", appdeps.Bump("npm", [("a", "1.0.0")], {"a"}), 0, 0)
        assert "--cpus" not in command and "--memory 2g" in command and "--pids-limit" in command


class TestRequirementsEdit:

    def test_only_the_version_after_the_pin_changes(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("# zaleznosci\nDjango[argon2]==4.2.0  ; python_version > '3.8'  # web\nrequests==2.31.0\n"
                       "django-extensions==3.2.0\n")
        bump = appdeps.Bump("pip", [("django", "4.2.11"), ("Requests", "2.32.0")], files=[str(req)])
        _, env = appdeps.script(bump)
        subprocess.run([sys.executable, "-c", env["PIPE_EDIT"]], cwd=tmp_path, check=True, capture_output=True,
                       env={**os.environ, "PIPE_BUMPS": env["PIPE_BUMPS"], "PIPE_FILES": env["PIPE_FILES"]})
        assert req.read_text() == ("# zaleznosci\nDjango[argon2]==4.2.11  ; python_version > '3.8'  # web\n"
                                   "requests==2.32.0\ndjango-extensions==3.2.0\n")


class TestPatch:

    def scan(self, host, items=None):
        target = app(host, files=["package-lock.json"], contents=NPM,
                     items=items or [item("lodash", "4.17.20", "4.17.21")])
        return vulns.Scan(at=1, ecosystem=None, targets=[target])

    def test_scope_words_and_plan(self, host):
        assert patching.parse_scope("aplikacje") == patching.parse_scope("apps") == "apps"
        job = patching.plan(self.scan(host), "apps")
        assert [p.kind for p in job.parts] == ["apps"] and "lodash" not in job.describe()
        assert patching.plan(self.scan(host), "images").empty
        assert [p.kind for p in patching.plan(self.scan(host), "all").parts] == ["apps"]

    def test_bump_then_rebuild_the_compose_project_and_back_up_the_files(self, host, monkeypatch):
        from backend.core import infra

        container = SimpleNamespace(project="shop", service="web", workdir="/srv/shop",
                                    compose_files=["/srv/shop/docker-compose.yml"])

        async def containers():
            return [container]
        monkeypatch.setattr(infra, "docker_containers", containers)
        shell = patch_env(monkeypatch)
        job = patching.plan(self.scan(host), "apps")
        result = run(patching.Run(job, "cli:t")._apps(job.parts[0]))
        bump_cmd = shell.having("docker run --rm --init --label pipe.appdeps=1")
        assert len(bump_cmd) == 1 and "lodash@4.17.21" in bump_cmd[0]
        build = shell.having("-p shop build web")
        assert build and shell.ran.index(build[0]) > shell.ran.index(bump_cmd[0])
        assert result.exit_code == 0 and result.redeployed == ["shop"] and result.entries == [result.entry_id]
        entry = journal.load(result.entry_id)
        assert sorted(f.label for f in entry.files) == ["/srv/shop/package-lock.json", "/srv/shop/package.json"]
        assert any("-p shop build web" in c for c in entry.inverse)

    def test_failed_manager_skips_the_rebuild(self, host, monkeypatch):
        from backend.core import infra

        async def containers():
            return [SimpleNamespace(project="shop", service="web", workdir="/srv/shop",
                                    compose_files=["/srv/shop/docker-compose.yml"])]
        monkeypatch.setattr(infra, "docker_containers", containers)
        shell = patch_env(monkeypatch, shell=FakeShell([("pipe.appdeps", ("", "npm ERR!", 1))]))
        job = patching.plan(self.scan(host), "apps")
        result = run(patching.Run(job, "cli:t")._apps(job.parts[0]))
        assert result.exit_code != 0 and not shell.having(" build web") and "npm" in result.warnings[0]

    def test_what_did_not_change_waits_a_day_and_the_report_says_why(self, host):
        scan = self.scan(host)
        job = patching.plan(scan, "apps")
        outcome = vulns.record_outcome("apps", job.parts[0].items, scan)      # po podbiciu dalej ta sama wersja
        assert outcome.unchanged == ["lodash"]
        assert patching.plan(scan, "apps").empty
        lines = vulns.app_todo(scan)
        assert lines and lines[0].startswith("Aplikacje") and "ograniczenie wersji" in lines[0]
        assert any(line.startswith(patching._REMAINING) for line in lines)

    def test_todo_parts_and_button(self, host):
        scan = self.scan(host)
        [line] = vulns.app_todo(scan)
        assert line.startswith("Zaleznosci aplikacji") and "/zalataj aplikacje" in line
        assert not line.startswith(patching._REMAINING)
        assert vulns.patch_parts(scan).apps == 1 and "zaleznosci aplikacji" in vulns.patch_parts(scan).describe()

    def test_compose_projects_of_the_app_directory(self):
        containers = [SimpleNamespace(project="shop", service="api", workdir="/srv/shop", compose_files=["/srv/shop/c.yml"]),
                      SimpleNamespace(project="other", service="x", workdir="/srv/other", compose_files=["/srv/other/c.yml"]),
                      SimpleNamespace(project="pipe", service="vps-agent", workdir="/srv", compose_files=["/srv/c.yml"])]
        [project] = patching.app_projects("/srv/shop/backend", containers, skip={"pipe"})
        assert project.project == "shop" and project.services == ["api"]


class TestUndoOnReadOnlyHost:

    def test_read_only_files_are_restored_through_the_host_helper(self, tmp_path, monkeypatch):
        from backend.core import attachments, executor, hostexec

        target = tmp_path / "app" / "package-lock.json"
        target.parent.mkdir()
        target.write_text("old")
        entry = run(journal.begin("cli:t", "patch", "x", files=[(str(target), "/srv/shop/package-lock.json")]))
        real_replace = os.replace

        def read_only(src, dst):
            if str(dst) == str(target):
                raise OSError(errno.EROFS, "Read-only file system")
            return real_replace(src, dst)
        monkeypatch.setattr(os, "replace", read_only)
        monkeypatch.setattr(attachments, "host_path", lambda p: "/opt/pipe/backend/data/journal/" + p.name)

        async def no_blocker():
            return ""

        async def image():
            return "sha256:" + "a" * 64
        monkeypatch.setattr(hostexec, "blocker", no_blocker)
        monkeypatch.setattr(hostexec, "image", image)
        shell = FakeShell()
        monkeypatch.setattr(executor, "execute", shell)
        text = run(journal.rollback(entry, "cli:t"))
        [restore] = shell.having("install ")
        assert "nsenter" in restore and "/srv/shop/package-lock.json" in restore and "BLAD" not in text
