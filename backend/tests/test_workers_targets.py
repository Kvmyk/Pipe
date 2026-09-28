"""
Testy zdalnych celow (budowanie komend, walidacja) i workerow (delegate).
"""

import asyncio
import shlex

import pytest

from backend.config import settings
from backend.core import targets
from backend.core.agent import VPSAgent
from backend.core.events import Progress
from backend.core.targets import Target, TargetError
from backend.tests.fakes import FakeClient, assert_history_valid, completion


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


class TestWrap:

    def test_ssh_quotes_command_as_one_argument(self):
        t = targets.validate(Target("web", "ssh", host="10.0.0.5", user="deploy", port=2222))
        cmd = targets.wrap(t, "df -h; echo 'x'")
        parts = shlex.split(cmd)
        assert parts[0] == "ssh" and "deploy@10.0.0.5" in parts and parts[-1] == "df -h; echo 'x'"
        assert "BatchMode=yes" in cmd and "-p" in parts

    def test_docker_exec(self):
        t = targets.validate(Target("db", "docker", container="shop-db"))
        assert shlex.split(targets.wrap(t, "ps aux"))[:5] == ["docker", "exec", "shop-db", "sh", "-c"]

    def test_kubernetes_cluster_adds_context(self):
        t = targets.validate(Target("prod", "kubernetes", context="k3s-prod", namespace="shop"))
        assert targets.wrap(t, "kubectl get pods") == "kubectl --context k3s-prod --namespace shop get pods"
        assert targets.wrap(t, "helm list").startswith("helm --kube-context k3s-prod")
        assert targets.wrap(t, "kubectl get pods -A | grep Crash").endswith("| grep Crash")

    def test_kubernetes_cluster_rejects_non_kubectl_and_second_kubectl(self):
        t = targets.validate(Target("prod", "kubernetes"))
        with pytest.raises(TargetError):
            targets.wrap(t, "rm -rf /tmp/x")
        with pytest.raises(TargetError):
            targets.wrap(t, "kubectl get pods && kubectl delete pod x")

    def test_kubernetes_pod_exec(self):
        t = targets.validate(Target("api", "kubernetes", namespace="shop", pod="api-0", pod_container="app"))
        assert shlex.split(targets.wrap(t, "env"))[-5:] == ["app", "--", "sh", "-c", "env"]

    @pytest.mark.parametrize("field,value", [
        ("host", "evil.com; rm -rf /"), ("user", "root$(id)"), ("container", "x y"),
        ("context", "a;b"), ("identity_file", "../key"),
    ])
    def test_injection_in_fields_rejected(self, field, value):
        kind = {"container": "docker", "context": "kubernetes"}.get(field, "ssh")
        t = Target("x", kind, host="h", container="c")
        setattr(t, field, value)
        with pytest.raises(TargetError):
            targets.validate(t)

    def test_registry_roundtrip_and_local(self):
        assert targets.save_target(Target("web", "ssh", host="1.2.3.4", description="frontend")) is True
        assert targets.save_target(Target("web", "ssh", host="1.2.3.5")) is False
        assert targets.get_target("web").host == "1.2.3.5"
        assert targets.get_target("local").kind == "local"
        with pytest.raises(TargetError):
            targets.save_target(Target("local", "ssh", host="x"))
        assert targets.remove_target("web") and targets.get_target("web") is None


class TestTargetTools:

    def test_add_requires_confirmation_then_saves(self):
        client = FakeClient([
            completion(tool_calls=[("c1", "target_manage", {"operation": "add", "name": "web-2", "kind": "ssh",
                                                            "host": "10.0.0.7", "user": "root"})]),
            completion("Dodalem"),
        ])
        agent = VPSAgent(client=client)
        events = run(agent.chat("s", "dodaj serwer"))
        assert any("wymaga potwierdzenia" in e for e in events if isinstance(e, str))
        assert targets.get_target("web-2") is None
        run(agent.confirm("s", True))
        assert targets.get_target("web-2").host == "10.0.0.7"
        assert_history_valid(agent._sessions["s"].messages)

    def test_remote_exec_classifies_inner_command(self):
        targets.save_target(Target("db", "docker", container="shop-db"))
        client = FakeClient([completion(tool_calls=[("c1", "remote_exec", {"target": "db", "command": "rm -rf /"})]),
                             completion("nie")])
        agent = VPSAgent(client=client)
        events = run(agent.chat("s", "wyczysc"))
        assert any(e.startswith("[ODMOWA]") for e in events if isinstance(e, str))
        assert agent._sessions["s"].pending_confirmation is None


class TestDelegate:

    def test_workers_run_in_parallel_and_report(self, monkeypatch):
        seen = []

        async def fake_execute(cmd, cwd=None, timeout=None):
            seen.append(cmd)
            return "Filesystem 80%", "", 0

        from backend.core import workers
        monkeypatch.setattr(workers.executor, "execute", fake_execute)
        targets.save_target(Target("web", "ssh", host="10.0.0.1"))

        # Kolejnosc odpowiedzi: glowny agent -> worker web (narzedzie) -> worker local (narzedzie)
        # -> raporty -> podsumowanie. Oba workery dostaja odpowiedzi z tej samej kolejki.
        client = FakeClient([
            completion(tool_calls=[("d1", "delegate", {"tasks": [
                {"target": "web", "task": "sprawdz dysk"},
                {"target": "local", "task": "sprawdz dysk"},
            ]})]),
            completion(tool_calls=[("w1", "run", {"command": "df -h"})]),
            completion(tool_calls=[("w2", "run", {"command": "rm -rf /var/log/old"})]),
            completion("USTALENIA: dysk 80%\nPROBLEMY: brak\nPROPOZYCJE: brak"),
            completion("USTALENIA: dysk 80%\nPROBLEMY: brak\nPROPOZYCJE: rm"),
            completion("Oba serwery maja 80% dysku"),
        ])
        agent = VPSAgent(client=client)
        events = run(agent.chat("s", "sprawdz dyski wszedzie"))
        assert events[-1] == "Oba serwery maja 80% dysku"
        assert any(isinstance(e, Progress) and "start" in e.text for e in events)
        # tylko odczyt zostal wykonany; rm trafil do propozycji, nie do powloki
        assert len(seen) == 1 and "df -h" in seen[0]
        report = agent._sessions["s"].messages[2]["content"]
        assert "WORKER web" in report and "WORKER local" in report and "rm -rf /var/log/old" in report
        assert set(agent._sessions["s"].workers) == {"web", "local"}
        assert_history_valid(agent._sessions["s"].messages)

    def test_unknown_target_is_reported(self):
        client = FakeClient([completion(tool_calls=[("d1", "delegate", {"tasks": [{"target": "nope", "task": "x"}]})]),
                             completion("ok")])
        agent = VPSAgent(client=client)
        run(agent.chat("s", "x"))
        assert "nie ma celu" in agent._sessions["s"].messages[2]["content"]

    def test_too_many_workers(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_WORKERS", 1)
        client = FakeClient([completion(tool_calls=[("d1", "delegate", {"tasks": [
            {"target": "local", "task": "a"}, {"target": "local", "task": "b"}]})]), completion("ok")])
        agent = VPSAgent(client=client)
        run(agent.chat("s", "x"))
        assert "najwyzej 1" in agent._sessions["s"].messages[2]["content"]
