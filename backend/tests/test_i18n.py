"""
Tryb angielski (PIPE_LANG=en): prompty, narzedzia, komunikaty protokolu, raporty i klienci.

Testy pilnuja dwoch rzeczy: (1) znaczniki protokolu ([POTWIERDZ], [BLAD]...) sa niezalezne
od jezyka, wiec potwierdzenia dzialaja tak samo, (2) w trybie angielskim model i uzytkownik
nie dostaja polskich tekstow z miejsc, ktore latwo przeoczyc (plan bezpiecznika, audyt, MCP).
"""

import asyncio
import re
import sys
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import executor, i18n, memory, posture, routines, runtime, safety, targets, usage
from backend.core.agent import VPSAgent, tools_for_agent
from backend.core.mcp import server as mcp_server
from backend.core.session import Session
from backend.server import event_frame
from backend.tests.fakes import FakeClient, assert_history_valid, completion

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
import tg_format  # noqa: E402

# Slowa, ktore w angielskim tekscie nie maja prawa sie pojawic (polskie bez znakow diakrytycznych).
POLISH = re.compile(
    r"\b(jest|nie|sie|brak|oraz|albo|przez|ktory|ktora|ktore|zostal|zostala|bedzie|moze|tylko|wymaga|"
    r"potwierdzenia|uzytkownik|komenda|komendy|plik|pliku|serwer|serwera|blad|haslo|hasla|kopia|zmiana|zmiany|"
    r"sprawdz|dziala|narzedzie|wykonano|odmowa|ocena|poprawka)\b", re.IGNORECASE)


def assert_english(text: str) -> None:
    # znaczniki protokolu ([BLAD], [ODMOWA]...) sa niezalezne od jezyka
    found = POLISH.findall(re.sub(r"\[[A-Z_]+(?::[^\]]*)?\]", "", text))
    assert not found, f"polskie slowa w trybie angielskim: {sorted(set(w.lower() for w in found))}\n{text[:600]}"


@pytest.fixture
def english(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPE_LANG", "en")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"), raising=False)
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    return tmp_path


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


class TestSwitch:

    def test_default_is_polish(self, monkeypatch):
        monkeypatch.delenv("PIPE_LANG", raising=False)
        assert i18n.lang() == "pl" and i18n.tr("tak", "yes") == "tak"

    @pytest.mark.parametrize("value", ["en", "EN", "en_US.UTF-8", " english "])
    def test_english_values(self, monkeypatch, value):
        monkeypatch.setenv("PIPE_LANG", value)
        assert i18n.lang() == "en" and i18n.tr("tak", "yes") == "yes"

    def test_unknown_value_falls_back_to_polish(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "de")
        assert i18n.lang() == "pl"

    def test_load_env_lang_reads_file_but_environment_wins(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("LLM_PROVIDER=gemini\nPIPE_LANG='en'\n")
        # setenv (nie delenv): load_env_lang pisze do os.environ, a monkeypatch musi to potem cofnac
        monkeypatch.setenv("PIPE_LANG", "")
        i18n.load_env_lang(env)
        assert i18n.lang() == "en"
        monkeypatch.setenv("PIPE_LANG", "pl")
        i18n.load_env_lang(env)
        assert i18n.lang() == "pl"

    def test_load_env_lang_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "")
        i18n.load_env_lang(tmp_path / "nope.env")
        assert i18n.lang() == "pl"


class TestPromptAndTools:

    def test_system_prompt_is_english(self, english):
        prompt = Session(session_id="s", interface="cli:kuba").system_prompt
        assert "--- ENVIRONMENT ---" in prompt and "--- SRODOWISKO ---" not in prompt
        assert "SERVER.md does not exist yet" in prompt
        assert_english(prompt)

    def test_telegram_prompt_is_english(self, english):
        assert_english(Session(session_id="1", interface="telegram:1").system_prompt)

    def test_every_tool_has_english_description(self, english):
        for tool in tools_for_agent():
            function = tool["function"]
            assert_english(function["description"])
            for name, prop in function["parameters"].get("properties", {}).items():
                assert_english(prop.get("description", "")), name

    def test_tool_names_do_not_depend_on_language(self, english, monkeypatch):
        english_names = [t["function"]["name"] for t in tools_for_agent()]
        monkeypatch.setenv("PIPE_LANG", "pl")
        assert english_names == [t["function"]["name"] for t in tools_for_agent()]

    def test_runtime_description(self, english, monkeypatch):
        for kind in ("docker", "native", "kubernetes"):
            monkeypatch.setenv("PIPE_RUNTIME", kind)
            assert runtime.describe().startswith("Mode: ")
            assert_english(runtime.describe())

    def test_mcp_tools_and_instructions(self, english, monkeypatch):
        tools = mcp_server.tools()
        assert [t["name"] for t in tools] == [t["name"] for t in mcp_server.TOOLS]
        for tool in tools:
            assert_english(tool["title"] + " " + tool["description"])
            for prop in tool["inputSchema"].get("properties", {}).values():
                assert_english(prop.get("description", ""))
        assert_english(mcp_server.instructions())
        monkeypatch.setenv("PIPE_LANG", "pl")
        assert mcp_server.tools() is mcp_server.TOOLS


class TestConfirmationProtocol:

    def test_confirm_frame_status_in_english(self, english):
        """[POTWIERDZ] zostaje, fraza jest angielska — serwer i tak musi oznaczyc ramke jako confirm."""
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "execute_command", {"command": "rm plik.txt"})]),
        ]))
        events = [e for e in run(agent.chat("s", "delete it")) if isinstance(e, str)]
        confirm = next(e for e in events if "[POTWIERDZ]" in e)
        assert "requires confirmation" in confirm
        assert event_frame(confirm)["status"] == "confirm"
        assert agent._sessions["s"].pending_confirmation is not None

    def test_decline_and_history_valid(self, english):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "execute_command", {"command": "rm plik.txt"}),
                                   ("c2", "execute_command", {"command": "ls"})]),
            completion("OK, not deleting"),
        ]))
        run(agent.chat("s", "delete it"))
        session = agent._sessions["s"]
        assert session.messages[-1]["content"].startswith("NOT EXECUTED")
        run(agent.confirm("s", False))
        assert_history_valid(session.messages)
        declined = next(m for m in session.messages if m.get("tool_call_id") == "c1")
        assert declined["content"] == "The user declined this operation."

    def test_polish_confirm_still_detected(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "pl")
        assert event_frame("[POTWIERDZ] Komenda wymaga potwierdzenia: `rm x`")["status"] == "confirm"
        assert event_frame("[BLAD] cos")["status"] == "error"

    def test_tool_result_for_model_is_english(self, english):
        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "read_file", {"path": "/no/such/file"})]),
            completion("done"),
        ]))
        run(agent.chat("s", "read"))
        result = next(m for m in agent._sessions["s"].messages if m.get("role") == "tool")["content"]
        assert_english(result)

    def test_no_output_marker_known_in_both_languages(self, english):
        stdout, _, code = asyncio.run(executor.execute("true"))
        assert code == 0 and stdout == "Command produced no output" and stdout in executor.NO_OUTPUT


class TestReports:

    def test_safety_plan(self, english):
        plan = safety.Plan(files=["/etc/nginx/nginx.conf"], inverse=["systemctl start nginx"],
                           pre=[safety.Check("nginx -t", "shell", "nginx -t")], sites=True, auto_restore=True,
                           notes=["x"])
        text = plan.describe()
        assert text.startswith("Safety fuse:") and "/undo" in text
        assert_english(text.replace("- note: x", ""))

    def test_audit_render(self, english, monkeypatch):
        audit = posture.Audit()
        monkeypatch.setattr(posture, "sshd_settings", lambda: {"passwordauthentication": "yes", "permitrootlogin": "yes"})
        monkeypatch.setattr(posture, "_enabled_units", lambda: set())
        posture.ssh_findings(audit, {"root": ""})
        posture.firewall_findings(audit, set(), "22")
        posture.protection_findings(audit, set(), {}, True)
        text = audit.render()
        assert text.startswith("Security score:") and "[HIGH]" in text
        assert_english(text)

    def test_usage_report(self, english):
        usage.record("m", {"prompt_tokens": 1500, "completion_tokens": 300}, "cli:kuba", usage.Prices(0, 0))
        text = usage.report(7, priced=False)["text"]
        assert text.startswith("Today:") and "tokens" in text
        assert_english(text)

    def test_secret_labels_and_redaction_marker(self, english):
        assert memory.find_secret("AKIAABCDEFGHIJKLMNOP") == "AWS key"
        text, count = memory.redact_secrets("DB_PASSWORD=supersecretvalue")
        # znacznik jest czescia protokolu (write_file go rozpoznaje) — nie zalezy od jezyka
        assert count == 1 and text == "DB_PASSWORD=[ZREDAGOWANO: secret]"

    def test_registry_errors(self, english):
        with pytest.raises(targets.TargetError) as target_error:
            targets.validate(targets.Target("Bad Name", "ssh", host="h"))
        assert_english(str(target_error.value))
        with pytest.raises(routines.RoutineError) as routine_error:
            routines.parse_schedule("whenever")
        assert_english(str(routine_error.value))
        with pytest.raises(memory.MemoryWriteError) as memory_error:
            memory.write_server_md("")
        assert_english(str(memory_error.value))


class TestBuiltinSkills:

    def test_same_number_of_skills_in_both_languages(self):
        polish = sorted(p.name for p in memory.BUILTIN_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())
        english = sorted(p.name for p in memory.BUILTIN_SKILLS_DIR_EN.iterdir() if (p / "SKILL.md").is_file())
        assert len(polish) == len(english) == 7
        # skille, do ktorych odwoluje sie audyt, maja te sama nazwe w obu jezykach
        assert {"swap", "fail2ban-ssh"} <= set(polish) & set(english)

    def test_english_skills_are_valid_and_english(self):
        for entry in memory.BUILTIN_SKILLS_DIR_EN.iterdir():
            skill = memory.parse_skill((entry / "SKILL.md").read_text(encoding="utf-8"), entry.name)
            assert skill.name == entry.name
            memory.validate_skill_content(entry.name, skill.description, skill.content)
            assert_english(skill.description)
            assert "/cofnij" not in skill.content and "TAK" not in skill.content

    def test_seed_follows_language(self, english):
        seeded = memory.seed_builtin_skills()
        assert "harden-ssh" in seeded and "utwardz-ssh" not in seeded


class TestClients:

    def test_telegram_menu_and_aliases(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "en")
        english = [name for name, _ in tg_format.builtin_commands()]
        monkeypatch.setenv("PIPE_LANG", "pl")
        polish = [name for name, _ in tg_format.builtin_commands()]
        assert len(english) == len(polish)
        # kazda angielska komenda z menu trafia do handlera polskiej komendy na tej samej pozycji
        for en, pl in zip(english, polish):
            assert tg_format.COMMAND_ALIASES.get(en, en) == pl
            assert en in tg_format.command_names(pl)

    def test_telegram_tags_and_help(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "en")
        text, needs_confirm = tg_format.collect_response_text([
            {"response": "[POTWIERDZ] Command requires confirmation: `rm x`", "status": "confirm"}])
        assert needs_confirm and text.startswith("<b>NEEDS CONFIRMATION:</b>")
        assert_english(tg_format.format_help([]))
        assert_english(tg_format.format_alert({"severity": "critical", "title": "Disk / at 95%", "detail": "x"}))
        assert tg_format.format_alert({"state": "resolved", "title": "t"}).startswith("<b>RESOLVED</b>")

    def test_telegram_memory_notice_survives(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "en")
        text, _ = tg_format.collect_response_text([
            {"response": "[PAMIEC] Updated SERVER.md", "status": "ok"}, {"response": "Done.", "status": "ok"}])
        assert text == "<b>Memory:</b> Updated SERVER.md\nDone."

    def test_cli_aliases_point_at_existing_commands(self):
        source = (Path(__file__).resolve().parents[2] / "clients" / "cli" / "cli.py").read_text(encoding="utf-8")
        aliases = dict(re.findall(r'"(\w+)": "(\w+)"', source[source.index("COMMAND_ALIASES = {"):source.index("HELP_TEXT = ")]))
        assert aliases and aliases["undo"] == "cofnij"
        for polish in set(aliases.values()):
            assert f'name == "{polish}"' in source, polish
        help_en = source[source.index('HELP_TEXT_EN = """'):source.index("def _print_list")]
        for english in re.findall(r"- `/(\w+)", help_en):
            assert english in aliases or f'name == "{english}"' in source or english in ("help", "exit", "mermaid"), english

    def test_english_command_names_are_reserved_for_skills(self):
        """Skill nie moze dostac komendy, ktora jest angielskim aliasem komendy wbudowanej."""
        source = (Path(__file__).resolve().parents[2] / "clients" / "cli" / "cli.py").read_text(encoding="utf-8")
        cli_aliases = set(re.findall(r'"(\w+)": "\w+"', source[source.index("COMMAND_ALIASES = {"):source.index("HELP_TEXT = ")]))
        assert cli_aliases | set(tg_format.COMMAND_ALIASES) <= memory.RESERVED_COMMANDS
        commands = {entry["name"]: entry["command"] for entry in memory.skill_commands(
            [memory.Skill("report", "d", "c"), memory.Skill("deploy-app", "d", "c")])}
        assert commands == {"report": "", "deploy-app": "deploy_app"}
