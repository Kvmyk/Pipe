"""
Testy petli agenta na atrapie klienta LLM (bez sieci).

Najwazniejszy niezmiennik: historia wysylana do providera jest zawsze
poprawna — kazde wywolanie narzedzia ma odpowiedz. Inaczej provider
odrzuca kolejne zapytanie i sesja jest zepsuta.
"""

import asyncio

import pytest

from backend.config import settings
from backend.core import agent as agent_module
from backend.core.agent import ABANDONED_CONFIRMATION, SKIPPED_FOR_CONFIRMATION, VPSAgent, truncate_result
from backend.core.events import Attachment
from backend.tests.fakes import FakeClient, assert_history_valid, completion


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"), raising=False)
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


def make_agent(script):
    client = FakeClient(script)
    return VPSAgent(client=client), client


class TestLoop:

    def test_text_answer(self):
        agent, client = make_agent([completion("Czesc")])
        assert run(agent.chat("s", "hej")) == ["Czesc"]
        assert client.calls[0]["messages"][0]["role"] == "system"

    def test_safe_command_runs_and_result_goes_to_llm(self):
        agent, client = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "echo pipe-test"})]),
            completion("Gotowe"),
        ])
        events = run(agent.chat("s", "uruchom"))
        assert events == ["Gotowe"]
        tool_msg = client.calls[1]["messages"][-1]
        assert tool_msg["role"] == "tool" and "pipe-test" in tool_msg["content"]

    def test_confirm_skips_remaining_calls_but_answers_them(self):
        agent, client = make_agent([
            completion(tool_calls=[
                ("c1", "execute_command", {"command": "rm plik"}),
                ("c2", "execute_command", {"command": "ls"}),
            ]),
        ])
        events = run(agent.chat("s", "usun"))
        assert any("wymaga potwierdzenia" in e for e in events if isinstance(e, str))
        session = agent._sessions["s"]
        assert session.pending_confirmation.tool_call_id == "c1"
        assert session.messages[-1] == {"role": "tool", "tool_call_id": "c2", "content": SKIPPED_FOR_CONFIRMATION}

        # odmowa: c1 dostaje odpowiedz, historia poprawna
        agent._client.chat.completions.script.append(completion("OK, nie usuwam"))
        run(agent.confirm("s", False))
        assert_history_valid(session.messages)

    def test_new_message_instead_of_confirmation(self):
        agent, client = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "rm plik"})]),
            completion("Rozumiem"),
        ])
        run(agent.chat("s", "usun"))
        run(agent.chat("s", "jednak nie, pokaz dysk"))
        session = agent._sessions["s"]
        assert session.pending_confirmation is None
        assert {"role": "tool", "tool_call_id": "c1", "content": ABANDONED_CONFIRMATION} in session.messages
        assert_history_valid(client.calls[-1]["messages"][1:])

    def test_interrupted_tool_call_is_repaired(self):
        agent, client = make_agent([completion("dalej")])
        session = agent.get_or_create_session("s")
        session.messages += [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "execute_command", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "execute_command", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        run(agent.chat("s", "kolejna"))
        assert_history_valid(client.calls[0]["messages"][1:])

    def test_confirmed_command_executes(self):
        agent, client = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "echo zmiana > /dev/null; mkdir -p x"})]),
            completion("Zrobione"),
        ])
        run(agent.chat("s", "zrob"))
        events = run(agent.confirm("s", True))
        assert events == ["Zrobione"]
        assert_history_valid(agent._sessions["s"].messages)

    def test_unknown_tool_and_bad_json_are_answered(self):
        agent, client = make_agent([
            completion(tool_calls=[("c1", "nie_ma_takiego", {})]),
            completion("ok"),
        ])
        run(agent.chat("s", "x"))
        assert_history_valid(agent._sessions["s"].messages)
        assert "Nieznane narzedzie" in agent._sessions["s"].messages[2]["content"]

    def test_iteration_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_MAX_ITERATIONS", 2)
        loop = [completion(tool_calls=[(f"c{i}", "execute_command", {"command": "pwd"})]) for i in range(5)]
        agent, _ = make_agent(loop)
        events = run(agent.chat("s", "petla"))
        assert "limit" in events[-1]

    def test_generated_marker_is_not_sent_to_provider(self):
        agent, client = make_agent([completion("ok")])
        run(agent.chat("s", "skan", generated=True))
        assert all("pipe_generated" not in m for m in client.calls[0]["messages"])
        assert agent._sessions["s"].messages[0]["pipe_generated"] is True


class TestSanitizing:

    def test_secrets_are_redacted_before_llm(self, tmp_path):
        env = tmp_path / "host" / "srv" / ".env"
        env.parent.mkdir(parents=True)
        env.write_text("DB_PASSWORD=supertajnehaslo123\nOPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\nPORT=8080\n")
        agent, client = make_agent([
            completion(tool_calls=[("c1", "read_file", {"path": "/srv/.env"})]),
            completion("ok"),
        ])
        events = run(agent.chat("s", "pokaz env"))
        # .env to plik z sekretami — read_file pyta o zgode tak samo jak `cat .env`
        assert any("[POTWIERDZ]" in str(e) for e in events)
        assert len(client.calls) == 1
        run(agent.confirm("s", True))
        content = client.calls[1]["messages"][-1]["content"]
        assert "supertajnehaslo123" not in content and "sk-abcdef" not in content
        assert "DB_PASSWORD=[ZREDAGOWANO" in content and "PORT=8080" in content
        assert_history_valid(client.calls[1]["messages"])

    def test_write_file_refuses_redacted_content(self):
        agent, client = make_agent([
            completion(tool_calls=[("c1", "write_file", {"path": "/srv/.env", "content": "A=[ZREDAGOWANO: sekret]"})]),
            completion("ok"),
        ])
        run(agent.chat("s", "zapisz"))
        session = agent._sessions["s"]
        assert session.pending_confirmation is None
        assert "ZREDAGOWANO" in session.messages[2]["content"] and "ODMOWA" in session.messages[2]["content"]

    def test_truncate_keeps_head_and_tail(self):
        text = "A" * 50_000 + "KONIEC"
        out = truncate_result(text, 1000)
        assert len(out) < 1200 and out.startswith("A") and out.endswith("KONIEC")


class TestHistory:

    def test_trim_cuts_at_user_message(self):
        from backend.core.session import Session
        session = Session()
        for i in range(30):
            session.messages += [
                {"role": "user", "content": f"u{i}"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": f"t{i}"}]},
                {"role": "tool", "tool_call_id": f"t{i}", "content": "x"},
                {"role": "assistant", "content": "a"},
            ]
        agent_module._trim_history(session, limit=10)
        assert session.messages[0]["role"] == "user"
        assert len(session.messages) <= 10


class TestDiagramTool:

    def test_mermaid_diagram_is_sent_as_attachment(self):
        pytest.importorskip("mermaidx")
        agent, client = make_agent([
            completion(tool_calls=[("c1", "diagram", {"mode": "mermaid", "title": "Deploy",
                                                      "mermaid": "flowchart LR\n  a[git push] --> b[CI] --> c[VPS]"})]),
            completion("Oto przeplyw"),
        ])
        events = run(agent.chat("s", "narysuj deploy"))
        attachment = next(e for e in events if isinstance(e, Attachment))
        assert attachment.mime == "image/png" and attachment.data.startswith(b"\x89PNG")
        assert attachment.name == "deploy.png"
        assert events[-1] == "Oto przeplyw"

    def test_syntax_error_goes_back_to_llm(self):
        pytest.importorskip("mermaidx")
        agent, client = make_agent([
            completion(tool_calls=[("c1", "diagram", {"mode": "mermaid", "mermaid": "flowchart LR\n a[x --> "})]),
            completion("poprawie"),
        ])
        events = run(agent.chat("s", "narysuj"))
        assert not any(isinstance(e, Attachment) for e in events)
        assert "Blad renderowania" in client.calls[1]["messages"][-1]["content"]
