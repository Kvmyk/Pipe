"""
Testy czuwania (alerty) i rutyn (harmonogram cron, rejestr).
"""

import asyncio
from datetime import datetime

import pytest

from backend.config import settings
from backend.core import routines, watch
from backend.core.infra import Container
from backend.core.routines import Routine, RoutineError, parse_schedule


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def write_proc(root, *, mem_total=4_000_000, mem_avail=2_000_000, load="0.5 0.4 0.3", cpus=2):
    root.mkdir(parents=True, exist_ok=True)
    (root / "meminfo").write_text(f"MemTotal: {mem_total} kB\nMemAvailable: {mem_avail} kB\n")
    (root / "loadavg").write_text(f"{load} 1/100 1\n")
    (root / "cpuinfo").write_text("".join(f"processor\t: {i}\n\n" for i in range(cpus)))
    return str(root)


class TestSchedule:

    @pytest.mark.parametrize("expr,when,expected", [
        ("0 7 * * *", datetime(2026, 9, 24, 7, 0), True),
        ("0 7 * * *", datetime(2026, 9, 24, 7, 1), False),
        ("*/15 * * * *", datetime(2026, 9, 24, 3, 45), True),
        ("*/15 * * * *", datetime(2026, 9, 24, 3, 46), False),
        ("5/10 * * * *", datetime(2026, 9, 24, 3, 25), True),
        ("0 9 * * 1-5", datetime(2026, 9, 26, 9, 0), False),   # sobota
        ("0 9 * * 1-5", datetime(2026, 9, 25, 9, 0), True),    # piatek
        ("0 9 * * 0", datetime(2026, 9, 27, 9, 0), True),      # niedziela = 0
        ("0 9 * * 7", datetime(2026, 9, 27, 9, 0), True),      # niedziela = 7
        ("0 0 1 * *", datetime(2026, 10, 1, 0, 0), True),
        ("0 0 1,15 * 1", datetime(2026, 9, 28, 0, 0), True),   # dzien LUB dzien tygodnia
        ("@daily", datetime(2026, 9, 24, 7, 0), True),
    ])
    def test_matches(self, expr, when, expected):
        assert parse_schedule(expr).matches(when) is expected

    @pytest.mark.parametrize("expr", ["* * *", "60 * * * *", "*/0 * * * *", "a b c d e", "5-1 * * * *"])
    def test_invalid(self, expr):
        with pytest.raises(RoutineError):
            parse_schedule(expr)


class TestRoutineRegistry:

    def test_save_due_and_status(self):
        assert routines.save_routine(Routine("poranek", "0 7 * * *", "sprawdz backupy"))
        assert not routines.save_routine(Routine("poranek", "0 8 * * *", "sprawdz backupy"))
        found = routines.load_routines()
        assert len(found) == 1 and found[0].schedule == "0 8 * * *"
        assert routines.due(found, datetime(2026, 9, 24, 8, 0))[0].name == "poranek"
        routines.update_routine("poranek", enabled=False)
        assert routines.due(routines.load_routines(), datetime(2026, 9, 24, 8, 0)) == []
        assert routines.report_status("STATUS: OK\nwszystko dziala") == "OK"
        assert routines.report_status("cos poszlo nie tak") == "PROBLEM"

    def test_rejects_secret_and_bad_name(self):
        with pytest.raises(RoutineError):
            routines.save_routine(Routine("Zla Nazwa", "@daily", "x"))
        with pytest.raises(RoutineError):
            routines.save_routine(Routine("ok", "@daily", "uzyj DB_PASSWORD=supertajne123 do logowania"))


class TestFindings:

    def test_resources(self, tmp_path, monkeypatch):
        proc = write_proc(tmp_path / "proc", mem_total=1_000_000, mem_avail=50_000, load="9.0 8.0 7.0", cpus=2)
        monkeypatch.setattr(settings, "WATCH_MEM_PCT", 90)
        keys = {f.key for f in watch.resource_findings(proc)}
        assert "memory" in keys and "load" in keys

    def test_healthy_host_has_no_findings(self, tmp_path):
        proc = write_proc(tmp_path / "proc")
        assert [f for f in watch.resource_findings(proc) if not f.key.startswith("disk")] == []

    def test_containers(self):
        containers = [
            Container("web", "app:1", "running", health="unhealthy"),
            Container("worker", "app:1", "restarting", restart_count=12),
            Container("db", "postgres", "exited"),
        ]
        first = watch.container_findings(containers, None)
        assert {f.key for f in first} == {"container:web:unhealthy", "container:worker:restarting"}
        later = watch.container_findings(containers, {"db", "web"})
        stopped = next(f for f in later if f.key == "container:db:stopped")
        assert stopped.transient and stopped.severity == "critical"

    def test_new_public_port_only_after_baseline(self):
        assert watch.port_findings({"6379/tcp": "0.0.0.0:6379"}, None) == []
        found = watch.port_findings({"22/tcp": "0.0.0.0:22", "6379/tcp": "0.0.0.0:6379"}, {"22/tcp"})
        assert [f.key for f in found] == ["port:6379/tcp"]


class TestWatcher:

    def test_alert_lifecycle(self):
        watcher = watch.Watcher()
        finding = watch.Finding("disk:/", "warning", "Dysk / 91%", "wolne 1 GB")
        events = watcher.apply([finding])
        assert [e.state for e in events] == ["new"]
        assert watcher.apply([finding]) == []                       # bez powtorek
        escalated = watcher.apply([watch.Finding("disk:/", "critical", "Dysk / 98%", "")])
        assert escalated[0].severity == "critical"
        resolved = watcher.apply([])
        assert resolved[0].state == "resolved" and not watcher.active
        transient = watcher.apply([watch.Finding("port:1", "warning", "Nowy port", "", transient=True)])
        assert transient[0].state == "event" and not watcher.active

    def test_publish_and_find(self):
        watcher = watch.Watcher()
        queue = watcher.notifier.subscribe()
        alert = watcher.apply([watch.Finding("memory", "warning", "RAM 95%", "x")])[0]
        watcher.notifier.publish(alert.to_event())
        assert queue.get_nowait()["title"] == "RAM 95%"
        assert watcher.find_alert(alert.id)["title"] == "RAM 95%"

    def test_prompt_block(self, monkeypatch):
        watcher = watch.Watcher()
        monkeypatch.setattr(watch, "_watcher", watcher)
        assert watch.prompt_alerts() == ""
        watcher.apply([watch.Finding("memory", "warning", "RAM 95%", "x")])
        assert "RAM 95%" in watch.prompt_alerts()

    def test_run_routine_publishes_report(self, monkeypatch):
        from backend.core import workers

        async def fake_worker(agent, parent, name, target, task, progress=None, *, extra_instructions=""):
            assert "STATUS" in extra_instructions
            return workers.WorkerResult(name, target.name, report="STATUS: PROBLEM\nbackup nie powstal")

        monkeypatch.setattr(workers, "run_worker", fake_worker)
        routines.save_routine(Routine("backupy", "@daily", "sprawdz backupy", notify="problems"))
        watcher = watch.Watcher(agent=object())
        queue = watcher.notifier.subscribe()
        event = asyncio.run(watcher.run_routine(routines.load_routines()[0]))
        assert event["status"] == "PROBLEM"
        assert queue.get_nowait()["name"] == "backupy"
        assert routines.load_routines()[0].last_status == "PROBLEM"
