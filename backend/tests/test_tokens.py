"""
Oszczednosc tokenow: narzedzia na zadanie (core/toolsets.py), skracanie starych wynikow narzedzi
(agent.mask_old_results) i licznik tokenow, ktorych Pipe nie wyslal (usage.json: `saved`).
"""

import asyncio
import json

import pytest

from backend.config import settings
from backend.core import sessions_store, toolsets, usage
from backend.core.agent import MASK_KEEP_CHARS, VPSAgent, mask_old_results, tools_for_agent
from backend.core.session import Session
from backend.core.tools import TOOLS
from backend.tests.fakes import FakeClient, assert_history_valid, completion


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(settings, "TOOLS_MODE", "lazy")
    monkeypatch.setattr(settings, "CONTEXT_KEEP_TURNS", 2)
    monkeypatch.setattr(settings, "WEB_SEARCH", "on")
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


def sent_tools(call) -> list[str]:
    return [t["function"]["name"] for t in call.get("tools") or []]


class TestGroups:

    def test_every_tool_belongs_to_exactly_one_group(self):
        names = [t["function"]["name"] for t in TOOLS]
        grouped = [tool for tools in toolsets.GROUPS.values() for tool in tools]
        assert sorted(grouped) == sorted(names) and len(grouped) == len(set(grouped))

    def test_load_tools_lists_every_lazy_group(self):
        # bez enum: grupy mcp:<serwer> dochodza w locie — lista jest w opisie
        schema = next(t for t in TOOLS if t["function"]["name"] == "load_tools")
        described = schema["function"]["parameters"]["properties"]["groups"]["description"]
        assert all(g in described for g in toolsets.GROUPS if g != toolsets.CORE) and "mcp:<serwer>" in described

    @pytest.mark.parametrize("text,group", [
        ("przypomnij mi jutro o 9 o domenie", "schedule"), ("napisz do mnie za 10 minut", "schedule"),
        ("remind me tomorrow", "schedule"), ("czy serwer jest bezpieczny?", "security"), ("zalataj to", "security"),
        ("cofnij ostatnią zmianę", "security"), ("zapamiętaj, że backupy są w /srv/b", "memory"),
        ("narysuj mapę serwera", "diagram"), ("pokaż architekturę", "diagram"), ("sprawdź dyski na wszystkich serwerach", "fleet"),
        ("czy ta wersja nginx jest jeszcze wspierana?", "web"), ("przeczytaj https://example.com/docs", "web"),
        ("zaktualizuj się do nowej wersji", "update"), ("use skill_manage to save it", "memory"),
    ])
    def test_triggers(self, text, group):
        assert group in toolsets.groups_for_text(text)

    def test_plain_questions_stay_on_core(self):
        assert toolsets.groups_for_text("czemu sklep działa wolno?") == set()
        assert toolsets.groups_for_text("pokaż logi kontenera shop") == set()

    def test_select_keeps_order_and_core(self):
        session = Session()
        lazy = toolsets.select(TOOLS, session, lazy=True)
        names = [t["function"]["name"] for t in lazy]
        assert set(names) == set(toolsets.GROUPS["core"]) and "load_tools" in names
        assert names == [t["function"]["name"] for t in TOOLS if t["function"]["name"] in names]
        toolsets.activate(session, ["security"])
        assert "security_patch" in [t["function"]["name"] for t in toolsets.select(TOOLS, session, lazy=True)]
        everything = [t["function"]["name"] for t in toolsets.select(TOOLS, session, lazy=False)]
        assert "load_tools" not in everything and len(everything) == len(TOOLS) - 1
        assert toolsets.size(lazy) * 2 < toolsets.size(toolsets.select(TOOLS, None, lazy=True))   # ponad polowa mniej

    def test_groups_survive_a_restart(self):
        session = Session(session_id="s1", messages=[{"role": "user", "content": "x"}])
        toolsets.activate(session, ["memory", "web"])
        sessions_store.save(session)
        restored = sessions_store.load("s1", 7)
        assert restored.tool_groups == {"core", "memory", "web"}


def make_agent(script):
    client = FakeClient(script)
    return VPSAgent(client=client), client


class TestAgentLoop:

    def test_first_call_sends_only_core_tools(self):
        agent, client = make_agent([completion("Czesc")])
        run(agent.chat("s", "jak sie masz serwerze?"))
        assert set(sent_tools(client.calls[0])) == set(toolsets.GROUPS["core"])

    def test_keywords_enable_a_group_before_the_call(self):
        agent, client = make_agent([completion("Ustawione")])
        run(agent.chat("s", "przypomnij mi jutro o 9"))
        assert "reminder" in sent_tools(client.calls[0]) and "diagram" not in sent_tools(client.calls[0])

    def test_load_tools_then_the_tool(self):
        agent, client = make_agent([
            completion(tool_calls=[("c1", "load_tools", {"groups": ["memory"]})]),
            completion(tool_calls=[("c2", "server_md", {"operation": "read"})]),
            completion("Gotowe"),
        ])
        assert run(agent.chat("s", "co wiesz o tym serwerze?"))[-1] == "Gotowe"
        assert "server_md" not in sent_tools(client.calls[0]) and "server_md" in sent_tools(client.calls[1])
        assert_history_valid(agent._sessions["s"].messages)

    def test_direct_call_of_a_hidden_tool_still_works_and_enables_it(self):
        agent, client = make_agent([
            completion(tool_calls=[("c1", "journal", {"operation": "list"})]),
            completion("Pusto"),
        ])
        run(agent.chat("s", "co robiles?"))
        tool = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"][-1]
        assert "Nieznane narzedzie" not in tool["content"] and "journal" in sent_tools(client.calls[1])

    def test_unknown_group_is_reported(self):
        agent, client = make_agent([completion(tool_calls=[("c1", "load_tools", {"groups": ["magia"]})]), completion("ok")])
        run(agent.chat("s", "x"))
        tool = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"][-1]
        assert "Nie ma takich grup" in tool["content"]

    def test_all_mode_sends_everything(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOLS_MODE", "all")
        agent, client = make_agent([completion("ok")])
        run(agent.chat("s", "hej"))
        assert "web_search" in sent_tools(client.calls[0]) and "load_tools" not in sent_tools(client.calls[0])

    def test_web_off_never_loads_web(self, monkeypatch):
        monkeypatch.setattr(settings, "WEB_SEARCH", "off")
        agent, client = make_agent([completion(tool_calls=[("c1", "load_tools", {"groups": ["web"]})]), completion("ok")])
        run(agent.chat("s", "poszukaj w internecie"))
        assert not any(name.startswith("web_") for call in client.calls for name in sent_tools(call))

    def test_note_from_a_command_enables_the_tools_it_needs(self):
        agent, client = make_agent([completion("ok")])
        agent.note("s", "Uzytkownik uzyl /podatnosci i zobaczyl: openssl ma podatnosc", interface="cli")
        run(agent.chat("s", "zrob to sam"))
        assert "security_patch" in sent_tools(client.calls[0])


class TestMasking:

    def history(self, turns=3, size=5000):
        messages = []
        for turn in range(turns):
            messages.append({"role": "user", "content": f"pytanie {turn}"})
            messages.append({"role": "assistant", "tool_calls": [{"id": f"c{turn}", "type": "function",
                                                                 "function": {"name": "execute_command", "arguments": "{}"}}]})
            messages.append({"role": "tool", "tool_call_id": f"c{turn}", "content": f"wynik {turn} " + "x" * size})
            messages.append({"role": "assistant", "content": f"odpowiedz {turn}"})
        return messages

    def test_old_results_shrink_recent_stay(self):
        masked, saved = mask_old_results(self.history(), 2)
        tools = [m["content"] for m in masked if m.get("role") == "tool"]
        assert len(tools[0]) < MASK_KEEP_CHARS + 300 and "skrocony przez Pipe" in tools[0]
        assert tools[1].endswith("x" * 100) and tools[2].endswith("x" * 100)
        assert saved > 1000
        assert_history_valid(masked)

    def test_small_results_and_off_are_untouched(self):
        history = self.history(size=100)
        assert mask_old_results(history, 2) == (history, 0)
        assert mask_old_results(self.history(), 0)[1] == 0
        assert mask_old_results(self.history(turns=2), 2)[1] == 0

    def test_session_keeps_the_full_history(self, tmp_path):
        (tmp_path / "host" / "dziennik.txt").write_text("".join(f"linia {i}\n" for i in range(1, 1001)))
        agent, client = make_agent([
            completion(tool_calls=[("c1", "read_file", {"path": "/dziennik.txt"})]), completion("A"),
            completion("B"), completion("C"),
        ])
        run(agent.chat("s", "pierwsze"))
        run(agent.chat("s", "drugie"))
        run(agent.chat("s", "trzecie"))
        last_call_tools = [m["content"] for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
        assert "skrocony przez Pipe" in last_call_tools[0]
        full = [m["content"] for m in agent._sessions["s"].messages if m.get("role") == "tool"][0]
        assert "skrocony" not in full and "linia 1000" in full


class TestUsageSaved:

    def test_saved_tokens_are_counted_and_reported(self):
        usage.record("m", {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 600}},
                     "cli-kuba", usage.Prices(), saved={"tools": 3800, "history": 1200})
        usage.record("m", {"prompt_tokens": 500, "completion_tokens": 50}, "cli-kuba", usage.Prices(), saved={"tools": 0})
        summary = usage.day_summary()
        assert summary["saved"] == 5000 and summary["saved_by"] == {"tools": 3800, "history": 1200}
        report = usage.report(7, priced=False)
        assert "nie wyslal ~5.0 tys." in report["text"] and "40% wejscia" in report["text"]
        assert report["history"][0]["saved"] == 5000 and report["saved"] == {"tools": 3800, "history": 1200}

    def test_agent_records_what_it_did_not_send(self):
        answer = completion("ok").model_dump()
        answer["usage"] = {"prompt_tokens": 3000, "completion_tokens": 10, "total_tokens": 3010}
        from openai.types.chat import ChatCompletion
        agent, client = make_agent([ChatCompletion.model_validate(answer)])
        run(agent.chat("s", "hej"))
        saved = usage.day_summary()["saved_by"]
        assert saved["tools"] > 2000                              # ~6 tys. tokenow schematow -> ~2,4 tys.


class TestPrompt:

    def test_prompt_mentions_the_workshop_and_drops_the_dir_prefix(self, monkeypatch):
        monkeypatch.setattr(settings, "WORKSPACE_DIR", "/srv/warsztat")
        prompt = Session().system_prompt
        assert "/srv/warsztat" in prompt and "load_tools" in prompt
        assert "ZA KAZDYM RAZEM" not in prompt and "[Katalog:" not in prompt

    def test_english_prompt_too(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "en")
        prompt = Session().system_prompt
        assert "Workshop" in prompt and "load_tools" in prompt and "[Dir:" not in prompt
