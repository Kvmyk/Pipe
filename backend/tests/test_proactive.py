"""
Pipe pisze pierwszy: skrzynka (core/feed.py), podpowiedzi (core/insights.py), badanie alertow bez pytania
(Watcher.auto_investigate), stan bez LLM (core/overview.py) i komendy klientow (overview, feed, feed_act, prefs,
cleanup, projects).
"""

import asyncio
import json
import time
from datetime import datetime

import pytest

import backend.server as server
from backend.config import settings
from backend.core import feed, hostinfo, insights, overview
from backend.core.watch import Alert, Watcher
from backend.tests.test_server_commands import exchange


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "NOTIFY_LEVEL", "important")
    monkeypatch.setattr(settings, "QUIET_HOURS", "")
    monkeypatch.setattr(settings, "INSIGHTS_PER_DAY", 3)
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    return tmp_path


def alert_event(severity="warning", key="disk:/", state="new", title="Dysk / zapelniony w 91%"):
    return {"type": "alert", "id": "a1", "key": key, "severity": severity, "title": title, "detail": "wolne 2 GB",
            "since": "2026-10-25 09:00", "state": state, "history": "", "report": ""}


def insight_event(severity="info", key="docker-space", kind="docker-space"):
    return insights.Insight(key, kind, severity, "Moge zwolnic 5 GB", "tekst",
                            [feed.action("Zwolnij miejsce", command="cleanup", admin=True)]).to_event()


NOON = datetime(2026, 10, 25, 12, 0)
NIGHT = datetime(2026, 10, 25, 23, 30)


class TestPolicy:

    def item(self, event):
        return feed.from_event(event)

    @pytest.mark.parametrize("level,severity,expected", [
        ("important", "warning", True), ("important", "critical", True), ("critical", "warning", False),
        ("critical", "critical", True), ("all", "warning", True),
    ])
    def test_alerts_follow_the_level(self, level, severity, expected):
        push, held = feed.decide(self.item(alert_event(severity)), feed.Prefs(level=level), NOON, 0)
        assert (push, held) == (expected, False)

    def test_insights_info_waits_unless_everything_and_budget_limits_pushes(self):
        info = self.item(insight_event("info"))
        warning = self.item(insight_event("warning"))
        assert feed.decide(info, feed.Prefs(level="important"), NOON, 0) == (False, False)
        assert feed.decide(info, feed.Prefs(level="all"), NOON, 0) == (True, False)
        assert feed.decide(warning, feed.Prefs(level="important", per_day=3), NOON, 2) == (True, False)
        assert feed.decide(warning, feed.Prefs(level="important", per_day=3), NOON, 3) == (False, False)

    def test_quiet_hours_hold_everything_but_critical_and_scheduled(self):
        prefs = feed.Prefs(level="all", quiet="22:00-07:00")
        assert feed.decide(self.item(alert_event("warning")), prefs, NIGHT, 0) == (False, True)
        assert feed.decide(self.item(alert_event("critical")), prefs, NIGHT, 0) == (True, False)
        digest = self.item({"type": "digest", "title": "Raport", "sections": []})
        assert feed.decide(digest, prefs, NIGHT, 0) == (True, False)
        assert feed.decide(self.item(alert_event("warning")), prefs, NOON, 0) == (True, False)

    def test_muted_kind_stays_in_inbox_without_notification(self):
        item = self.item(insight_event("warning"))
        assert feed.decide(item, feed.Prefs(level="all", muted=["insight:docker-space"]), NOON, 0) == (False, False)

    @pytest.mark.parametrize("value,window", [("22:00-07:00", (1320, 420)), ("off", None), ("", None),
                                              ("7:30-9:00", (450, 540)), ("10:00-10:00", None)])
    def test_parse_quiet(self, value, window):
        assert feed.parse_quiet(value) == window

    @pytest.mark.parametrize("value", ["22-7", "25:00-07:00", "nocą"])
    def test_bad_quiet_is_refused(self, value):
        with pytest.raises(feed.FeedError):
            feed.parse_quiet(value)

    def test_quiet_across_midnight(self):
        assert feed.in_quiet("22:00-07:00", datetime(2026, 1, 1, 3, 0))
        assert not feed.in_quiet("22:00-07:00", datetime(2026, 1, 1, 7, 0))
        assert feed.quiet_ends_now("22:00-07:00", datetime(2026, 1, 1, 7, 0))
        assert feed.in_quiet("01:00-05:00", datetime(2026, 1, 1, 2, 0))


class TestFeedStore:

    def test_add_list_unread_and_seen(self):
        item = feed.add(alert_event(), defaults=feed.Prefs())
        assert item.push and [a["command"] for a in item.actions] == ["cleanup", "investigate"]
        assert item.actions[0]["admin"] and item.actions[1]["args"] == {"id": "a1"}
        data = feed.listing("web-kuba")
        assert data["unread"] == 1 and data["items"][0]["unread"] and data["items"][0]["actions"][1]["index"] == 1
        feed.mark_seen("web-kuba")
        assert feed.listing("web-kuba")["unread"] == 0
        assert feed.listing("cli-ania")["unread"] == 1          # kazdy odbiorca ma wlasny stan

    def test_old_messages_are_not_new_for_a_new_reader(self):
        feed.add(alert_event(), defaults=feed.Prefs(), now=time.time() - 3 * 86400)
        assert feed.unread("telegram-1") == 0

    def test_resolved_alert_closes_the_buttons_of_the_earlier_message(self):
        first = feed.add(alert_event(), defaults=feed.Prefs())
        feed.add(alert_event(state="resolved"), defaults=feed.Prefs())
        assert feed.get(first.id).done.startswith("Rozwiazane")

    def test_insight_gets_snooze_and_mute(self):
        item = feed.add(insight_event("warning"), defaults=feed.Prefs())
        assert [a["kind"] for a in item.actions] == ["command", "snooze", "mute"]

    def test_budget_counts_only_pushed_insights_per_day(self):
        prefs = feed.Prefs(level="all", per_day=2)
        pushes = [feed.add(insight_event("warning", key=f"k{i}"), defaults=prefs).push for i in range(4)]
        assert pushes == [True, True, False, False]

    def test_prefs_override_defaults_and_validate(self):
        defaults = feed.defaults_from("important", "", 3)
        assert feed.prefs(defaults).level == "important"
        feed.set_prefs(level="critical", quiet="22:00-07:00", per_day=1, defaults=defaults)
        current = feed.prefs(defaults)
        assert (current.level, current.quiet, current.per_day) == ("critical", "22:00-07:00", 1)
        feed.set_prefs(quiet="off", defaults=defaults)
        assert feed.prefs(defaults).quiet == ""
        with pytest.raises(feed.FeedError):
            feed.set_prefs(level="glosno", defaults=defaults)
        assert feed.defaults_from("bzdura", "25:99-1", 3).level == "important"

    def test_mute_snooze_and_release(self):
        assert feed.mute("insight:reboot") == ["insight:reboot"]
        assert feed.mute("insight:reboot", on=False) == []
        feed.snooze("docker-space", 1)
        assert feed.snoozed("docker-space") and not feed.snoozed("other")
        held = feed.add(alert_event(), defaults=feed.Prefs(quiet="00:00-23:59"),
                        now=datetime(2026, 10, 25, 12, 0).timestamp())
        assert held.held and not held.push
        released = feed.release_held()
        assert [i.id for i in released] == [held.id] and feed.release_held() == []
        notice = feed.held_notice(released)
        assert notice["type"] == "notice" and "Dysk" in notice["text"]

    def test_unknown_event_types_stay_out(self):
        assert feed.add({"type": "reminder", "text": "kawa"}) is None
        assert feed.add({"type": "approval", "command": "rm x"}) is None


class TestInsights:

    def disk(self, pct=80.0, total=100 * 1024 ** 3):
        used = int(total * pct / 100)
        return hostinfo.Disk("/", "/dev/sda1", "ext4", total, used, total - used)

    def points(self, now, start, step, hours=40, mem=None):
        return [{"t": now - (hours - i) * 3600, "disks": {"/": start + i * step},
                 **({"mem": mem(i)} if mem else {})} for i in range(hours)]

    def test_forecast_when_disk_fills_within_a_week(self):
        now = time.time()
        found = insights.disk_forecast(self.points(now, 70, 0.5), [self.disk(89.5)], now)
        assert len(found) == 1 and found[0].key == "disk-forecast:/" and found[0].severity == "warning"
        assert "dni" in found[0].title or "h" in found[0].title
        assert found[0].actions[0]["command"] == "cleanup"

    def test_no_forecast_for_flat_or_slow_growth(self):
        now = time.time()
        assert insights.disk_forecast(self.points(now, 70, 0.0), [self.disk(70)], now) == []
        assert insights.disk_forecast(self.points(now, 60, 0.01), [self.disk(60.4)], now) == []

    def test_short_history_gives_no_forecast(self):
        now = time.time()
        assert insights.disk_forecast(self.points(now, 70, 1.0, hours=4), [self.disk(74)], now) == []

    def test_reclaimable_and_sizes(self):
        assert insights.parse_size("1.5GB") == 1.5e9 and insights.parse_size("512MB") == 512e6
        assert insights.parse_size("0B") == 0 and insights.parse_size("bzdura") == 0
        rows = [{"Type": "Images", "Reclaimable": "40GB (90%)"}, {"Type": "Build Cache", "Reclaimable": "3GB"}]
        assert insights.reclaimable(rows, ["1GB", "500MB"], 2e8) == pytest.approx(3e9 + 1.5e9 + 2e8)
        assert insights.docker_space(1e9, 100e9) == []
        [space] = insights.docker_space(6e9, 50e9)
        assert space.kind == "docker-space" and space.cooldown == 7 * insights.DAY

    def test_restarts_within_a_day(self):
        class C:
            def __init__(self, count, state="running"):
                self.name, self.restart_count, self.state = "shop-worker", count, state

        now = time.time()
        found, state = insights.restarts([C(2)], {}, now)
        assert found == [] and state["shop-worker"]["count"] == 2
        found, state = insights.restarts([C(5)], state, now + 3600)
        assert len(found) == 1 and "3 razy" in found[0].title
        found, state = insights.restarts([C(6)], state, now + 7200)
        assert found == []                                         # liczymy od ostatniej podpowiedzi
        found, state = insights.restarts([C(0)], state, now + 9000)
        assert found == [] and state["shop-worker"]["count"] == 0  # kontener odtworzony

    def test_reboot_after_days(self):
        now = time.time()
        assert insights.reboot(None, now) == [] and insights.reboot(now - 86400, now) == []
        assert insights.reboot(now - 4 * 86400, now)[0].title.startswith("Serwer od 4 dni")

    def test_memory_trend(self):
        now = time.time()
        rising = [{"t": now - (24 - i) * 3600, "mem": 55 + i * 1.2} for i in range(24)]
        assert insights.memory_trend(rising, now)[0].kind == "memory-trend"
        steady = [{"t": now - (24 - i) * 3600, "mem": 80} for i in range(24)]
        assert insights.memory_trend(steady, now) == []

    def test_due_respects_cooldown_and_escalation(self):
        now = time.time()
        info = insights.Insight("k", "k", "info", "t", "x")
        assert insights.due(info, {}, now)
        sent = {"k": {"at": now - 3600, "severity": "info"}}
        assert not insights.due(info, sent, now)
        assert insights.due(insights.Insight("k", "k", "warning", "t", "x"), sent, now)
        assert insights.due(info, {"k": {"at": now - 2 * 86400, "severity": "info"}}, now)


class TestWatcherFeed:

    def test_published_event_gets_feed_fields(self):
        watcher = Watcher()
        queue = watcher.notifier.subscribe()
        watcher.notifier.publish(alert_event())
        event = queue.get_nowait()
        assert event["feed_id"] and event["push"] is True and event["actions"][0]["index"] == 0
        watcher.notifier.publish({"type": "language", "lang": "en"}, transient=True)
        assert "feed_id" not in queue.get_nowait() and len(feed.recent()) == 1

    def test_insights_once_sends_each_suggestion_once(self, monkeypatch):
        async def fake_collect(state, *, containers, points, now=None):
            return [insights.Insight("docker-space", "docker-space", "info", "Moge zwolnic 5 GB", "x")]

        async def no_containers():
            return []

        monkeypatch.setattr(insights, "collect", fake_collect)
        from backend.core import infra
        monkeypatch.setattr(infra, "docker_containers", no_containers)
        watcher = Watcher()
        assert len(asyncio.run(watcher.insights_once())) == 1
        assert asyncio.run(watcher.insights_once()) == []          # okres ciszy
        assert feed.recent()[0].type == "insight"

    def test_snoozed_suggestion_waits(self, monkeypatch):
        async def fake_collect(state, *, containers, points, now=None):
            return [insights.Insight("reboot", "reboot", "info", "Restart", "x")]

        async def no_containers():
            return []

        monkeypatch.setattr(insights, "collect", fake_collect)
        from backend.core import infra
        monkeypatch.setattr(infra, "docker_containers", no_containers)
        feed.snooze("reboot", 24)
        assert asyncio.run(Watcher().insights_once()) == []

    def test_digest_lists_what_waited_during_quiet_hours(self, monkeypatch):
        async def no_chart(metric, hours):
            return None, ""

        from backend.core.handlers import history
        monkeypatch.setattr(history, "chart_attachment", no_chart)
        item = feed.Item(id="x", ts=time.time(), type="alert", severity="warning", title="Dysk / 91%")
        result = asyncio.run(Watcher().build_digest(held=[item]))
        assert result.sections[0].lines == ["Dysk / 91%"] and "cisza" in result.sections[0].title


class FakeAgent:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def llm_enabled(self):
        return self.enabled


def make_alert(severity="critical", key="container:shop:stopped", state="event"):
    return Alert("id1", key, severity, "Kontener shop przestal dzialac", "stan exited", "2026-10-25 09:00", state)


class TestAutoInvestigate:

    def test_critical_problem_is_investigated_once_per_cooldown(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTO_INVESTIGATE", "critical")
        monkeypatch.setattr(settings, "AUTO_INVESTIGATE_DAILY", 4)
        watcher = Watcher(FakeAgent())
        assert watcher._should_investigate(make_alert())
        assert not watcher._should_investigate(make_alert())          # ten sam problem — najwyzej raz na 6 h
        assert not watcher._should_investigate(make_alert("warning", key="disk:/", state="new"))
        assert not watcher._should_investigate(make_alert(key="vuln:system", state="new"))
        assert not watcher._should_investigate(make_alert(key="port:8080/tcp"))

    def test_daily_limit_off_and_no_model(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTO_INVESTIGATE", "all")
        monkeypatch.setattr(settings, "AUTO_INVESTIGATE_DAILY", 2)
        watcher = Watcher(FakeAgent())
        results = [watcher._should_investigate(make_alert("warning", key=f"disk:/d{i}", state="new")) for i in range(3)]
        assert results == [True, True, False]
        monkeypatch.setattr(settings, "AUTO_INVESTIGATE", "off")
        assert not Watcher(FakeAgent())._should_investigate(make_alert())
        monkeypatch.setattr(settings, "AUTO_INVESTIGATE", "critical")
        assert not Watcher(FakeAgent(enabled=False))._should_investigate(make_alert())
        assert not Watcher(None)._should_investigate(make_alert())

    def test_findings_reach_the_inbox_with_a_fix_button(self, monkeypatch):
        from backend.core import workers

        async def fake_worker(agent, parent, name, target, task, progress=None, *, extra_instructions=""):
            assert "Kontener shop przestal dzialac" in task and "Tylko odczyty" in task
            return workers.WorkerResult(name, "local", report="Padl, bo zabraklo pamieci.", proposals=["docker start shop"])

        async def no_changes(hours):
            return "bez zmian"

        monkeypatch.setattr(workers, "run_worker", fake_worker)
        from backend.core.handlers import history
        monkeypatch.setattr(history, "changes_text", no_changes)
        watcher = Watcher(FakeAgent())
        queue = watcher.notifier.subscribe()
        event = asyncio.run(watcher.auto_investigate(make_alert()))
        assert event["type"] == "investigation" and "docker start shop" in event["report"]
        published = queue.get_nowait()
        [fix] = published["actions"]
        assert fix["kind"] == "message" and fix["admin"] and "Padl" in fix["args"]["text"]

    def test_publish_starts_the_investigation_in_the_background(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTO_INVESTIGATE", "critical")
        started = []
        watcher = Watcher(FakeAgent())

        async def fake(alert):
            started.append(alert.key)

        monkeypatch.setattr(watcher, "auto_investigate", fake)

        async def scenario():
            watcher._publish([make_alert()], investigate=True)
            watcher._publish([make_alert(key="container:db:stopped")], investigate=False)
            await asyncio.sleep(0.05)

        asyncio.run(scenario())
        assert started == ["container:shop:stopped"]


class TestOverview:

    def test_headline_and_gauges(self, monkeypatch):
        monkeypatch.setattr(hostinfo, "loadavg", lambda proc=None: (0.4, 0.5, 0.6))
        monkeypatch.setattr(hostinfo, "cpu_count", lambda proc=None: 4)
        monkeypatch.setattr(hostinfo, "memory", lambda proc=None: hostinfo.Memory(8000, 1200, 0, 0))
        monkeypatch.setattr(hostinfo, "disks", lambda proc=None: [hostinfo.Disk("/", "/dev/sda", "ext4", 100, 92, 8)])
        monkeypatch.setattr(hostinfo, "uptime_seconds", lambda proc=None: 3 * 86400)
        data = overview.build(active=[], hostname="vps")
        assert data["status"] == "ok" and data["headline"] == "Wszystko dziala"
        levels = {g["id"]: g["level"] for g in data["gauges"]}
        assert levels == {"cpu": "ok", "mem": "warn", "disk:/": "crit"}
        assert "Dysk /" in data["text"] and "▰" in data["text"]
        alert = make_alert("critical", key="disk:/", state="new")
        data = overview.build(active=[alert, make_alert("warning", key="load", state="new")])
        assert data["status"] == "critical" and data["headline"].startswith("Wymaga uwagi: Kontener") \
            and "1 wiecej" in data["headline"]

    def test_series_from_history(self):
        now = time.time()
        points = [{"t": now - 3600 * (24 - i), "load1": 2.0, "cpus": 4, "mem": 50, "disks": {"/": 60}} for i in range(24)]
        series = overview.series(points, now)
        assert series["cpu"][-1] == 50.0 and series["mem"][-1] == 50 and len(series["disk"]) == overview.SERIES_POINTS
        assert overview.series([], now) == {"cpu": [], "mem": [], "disk": []}


class TestServerCommands:

    @pytest.fixture(autouse=True)
    def agent(self, monkeypatch):
        from backend.tests.test_server_commands import FakeAgent as ServerAgent
        fake = ServerAgent()
        monkeypatch.setattr(server, "get_agent", lambda: fake)
        fake.note = lambda *a, **k: True

        async def no_containers():
            return None

        from backend.core import infra
        monkeypatch.setattr(infra, "docker_containers", no_containers)
        return fake

    def test_status_answers_without_the_model(self, agent):
        [[text, done]] = exchange([{"command": "status"}])
        assert text["response"] and not text["done"] and done["data"]["headline"]
        [[frame]] = exchange([{"command": "overview"}])
        assert frame["data"]["gauges"] is not None and agent.messages == []

    def test_feed_and_actions(self, agent):
        item = feed.add(insight_event("warning"), defaults=feed.Prefs())
        [[listing]] = exchange([{"command": "feed"}])
        assert listing["data"]["unread"] == 1 and listing["data"]["items"][0]["id"] == item.id
        [[snoozed]] = exchange([{"command": "feed_act", "id": item.id, "action": 1}])
        assert snoozed["data"]["done"] == "Pozniej" and feed.snoozed(item.key)
        [[again]] = exchange([{"command": "feed_act", "id": item.id, "action": 9}])
        assert again["status"] == "error"
        other = feed.add(insight_event("warning", key="k2", kind="reboot"), defaults=feed.Prefs())
        [[muted]] = exchange([{"command": "feed_act", "id": other.id, "action": 2}])
        assert muted["data"]["muted"] == ["insight:reboot"]
        [[seen]] = exchange([{"command": "feed", "args": "seen"}])
        assert seen["data"]["unread"] == 0

    def test_message_action_goes_to_the_agent_once(self, agent):
        event = {"type": "investigation", "key": "container:x", "title": "x", "report": "r", "alert_id": "a",
                 "severity": "critical", "fix_message": "Napraw problem x"}
        item = feed.add(event, defaults=feed.Prefs())
        frames = exchange([{"command": "feed_act", "id": item.id, "action": 0}])[0]
        assert agent.messages == [("s1", "Napraw problem x", "cli")] and frames[-1]["done"]
        [[twice]] = exchange([{"command": "feed_act", "id": item.id, "action": 0}])
        assert twice["status"] == "error" and "Juz zrobione" in twice["response"]

    def test_command_action_runs_cleanup(self, agent, monkeypatch):
        from backend.core import cleanup

        async def fake_run(interface, emit=None):
            await emit("sprzatam")
            return cleanup.Result(freed=5 * 1024 ** 3, steps=["obrazy bez tagu"])

        monkeypatch.setattr(cleanup, "run", fake_run)
        item = feed.add(alert_event(), defaults=feed.Prefs())
        frames = exchange([{"command": "feed_act", "id": item.id, "action": 0}])[0]
        assert frames[0]["event"]["text"] == "sprzatam" and "5.0 GB" in frames[-1]["data"]["text"]

    def test_viewer_cannot_press_admin_buttons_or_clean(self, agent, monkeypatch):
        monkeypatch.setattr(server, "_authorize", lambda request: ("viewer-1", "viewer"))
        item = feed.add(alert_event(), defaults=feed.Prefs())
        [[refused]] = exchange([{"command": "feed_act", "id": item.id, "action": 0}])
        assert refused["status"] == "error"
        [[clean]] = exchange([{"command": "cleanup"}])
        assert clean["status"] == "error"
        [[prefs]] = exchange([{"command": "prefs", "level": "critical"}])
        assert prefs["status"] == "error"
        [[read]] = exchange([{"command": "prefs"}])
        assert read["data"]["can_edit"] is False

    def test_prefs_round_trip(self, agent):
        [[changed]] = exchange([{"command": "prefs", "level": "critical", "quiet": "22:00-07:00", "per_day": 1}])
        assert (changed["data"]["level"], changed["data"]["quiet"], changed["data"]["per_day"]) == ("critical", "22:00-07:00", 1)
        [[bad]] = exchange([{"command": "prefs", "quiet": "w nocy"}])
        assert bad["status"] == "error"
        feed.mute("insight:reboot")
        [[back]] = exchange([{"command": "prefs", "args": "unmute insight:reboot"}])
        assert back["data"]["muted"] == []

    def test_cleanup_refused_in_observe_mode(self, agent, monkeypatch):
        monkeypatch.setenv("PIPE_OBSERVE", "1")
        [[frame]] = exchange([{"command": "cleanup"}])
        assert frame["status"] == "error" and "obserwacji" in frame["response"]

    def test_projects_list_without_docker(self, agent):
        [[frame]] = exchange([{"command": "projects"}])
        assert frame["data"]["projects"] == [] and "pusty" in frame["data"]["text"]
        [[missing]] = exchange([{"command": "projects", "args": "logs", "name": "nie-ma"}])
        assert missing["status"] == "error"

    def test_feed_seen_is_remembered_in_the_conversation(self, agent):
        notes = []
        agent.note = lambda session_id, text, **kwargs: notes.append(text) or True
        feed.add(alert_event(), defaults=feed.Prefs())
        exchange([{"command": "feed"}])
        assert notes == []                                          # odswiezanie pulpitu to nie "zobaczyl"
        exchange([{"command": "feed", "args": "seen"}])
        assert len(notes) == 1 and "/nowosci" in notes[0] and "Dysk" in notes[0]
