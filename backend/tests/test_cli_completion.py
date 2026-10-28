"""Podpowiedzi komend "/" w CLI: lista pasujacych komend i skilli, uzupelnianie Tabem."""

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("rich")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "cli"))
import cli  # noqa: E402

SKILLS = [{"name": "odnow-certyfikat", "description": "Odnawia certyfikat", "command": "odnow_certyfikat"},
          {"name": "status", "description": "zarezerwowana nazwa", "command": ""}]


def names(text, skills=SKILLS):
    return [name for name, _ in cli._slash_options(text, skills)]


def test_slash_lists_every_command_and_skill(monkeypatch):
    monkeypatch.setattr(cli, "LANG", "pl")
    options = names("/")
    assert options[:3] == ["status", "nowosci", "projekty"]       # najczestsze sprawy na gorze
    assert len(options) == len(cli.COMMANDS) - len(cli.HIDDEN_COMMANDS) + 1
    assert "odnow_certyfikat" in options


def test_hidden_commands_only_on_full_name(monkeypatch):
    monkeypatch.setattr(cli, "LANG", "pl")
    assert "alerty" not in names("/") and "alerty" not in names("/al")
    assert names("/alerty") == ["alerty"] and "historia" in names("/history")
    assert all(f"/{name}" not in cli.HELP_TEXT for name in cli.HIDDEN_COMMANDS)


def test_prefix_narrows_and_exact_match_goes_first(monkeypatch):
    monkeypatch.setattr(cli, "LANG", "pl")
    assert names("/sta") == ["status"]
    assert names("/co") == ["cofnij", "koszt"]       # "cost" pasuje po angielsku, polska nazwa pierwsza
    assert names("/now")[:2] == ["nowosci", "nowa"]           # potem skill "odnow..." (dopasowanie w srodku)
    assert names("/nowa") == ["nowa"]
    assert names("/news") == ["nowosci"]             # alias angielski


def test_names_follow_language_but_both_match(monkeypatch):
    monkeypatch.setattr(cli, "LANG", "en")
    assert names("/rap") == ["report"]
    assert dict(cli._slash_options("/rep", []))["report"].startswith("report:")
    monkeypatch.setattr(cli, "LANG", "pl")
    assert names("/rep") == ["raport"]
    assert names("/usage") == ["koszt"]          # alias


def test_no_options_for_plain_text_or_arguments():
    assert names("co slychac") == []
    assert names("/var/log jest pelny?") == []
    assert names("/zmiany 3") == []
    assert names("/zzz") == []


def test_commands_are_listed_once():
    names_pl = [entry[0] for entry in cli.COMMANDS]
    assert len(names_pl) == len(set(names_pl))


def test_every_command_is_handled_or_aliased():
    for name_pl, name_en, _, _ in cli.COMMANDS:
        assert name_pl not in cli.COMMAND_ALIASES
        assert name_en in (name_pl, "help") or cli.COMMAND_ALIASES.get(name_en) == name_pl


def test_tab_fills_the_command(monkeypatch):
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setattr(cli, "LANG", "pl")
    monkeypatch.setattr(cli, "_skills", SKILLS)

    async def typed(keys):
        with create_pipe_input() as pipe:
            ask = cli._prompt_reader(input=pipe, output=DummyOutput())
            task = asyncio.ensure_future(ask())
            for key in keys:                      # po znaku — jak czlowiek, zeby lista zdazyla sie pojawic
                pipe.send_text(key)
                await asyncio.sleep(0.05)
            return await asyncio.wait_for(task, 5)

    assert asyncio.run(typed("/sta\t\r")) == "/status"
    assert asyncio.run(typed("/odn\t tylko example.com\r")) == "/odnow_certyfikat tylko example.com"
    assert asyncio.run(typed("/\t\t\r")) == "/nowosci"
    assert asyncio.run(typed("co tam\t\r")).strip() == "co tam"


def test_busy_local_port_is_not_reused():
    """Zajety port lokalny = cudzy tunel; CLI nie moze uznac go za swoj (haslo ssh trafialoby do agenta)."""
    import socket

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        assert cli._own_local_port(port) != port
    assert cli._own_local_port(port) == port


@pytest.mark.parametrize("answer, expected", [("tak", True), ("t", True), ("y", True), ("YES", True),
                                              ("nie", False), ("n", False), ("no", False)])
def test_confirm_accepts_polish_and_english_answers(answer, expected):
    """README, web i Telegram ucza odpowiadac "tak" — CLI odrzucalo to komunikatem "Please enter Y or N"."""
    assert cli.Confirm("?").process_response(answer) is expected


def test_confirm_rejects_other_words_in_cli_language():
    from rich.prompt import InvalidResponse
    with pytest.raises(InvalidResponse):
        cli.Confirm("?").process_response("moze")


class TestServerLanguage:
    """CLI (i pipe web, ktory dostaje jezyk z CLI) mowi jezykiem serwera, gdy na komputerze nie wybrano zadnego."""

    class Client:
        def __init__(self, lang, chosen):
            self.data = {"lang": lang, "chosen": chosen}

        async def send_command(self, name, **_):
            return [{"response": "", "status": "ok", "done": True, "data": self.data}]

    @pytest.mark.parametrize("env, chosen, expected", [
        ("", False, "pl"),        # nic nie ustawiono na komputerze: jezyk z .env serwera
        ("en", False, "en"),      # PIPE_LANG na komputerze wygrywa z .env serwera...
        ("en", True, "pl"),       # ...ale nie z wyborem /jezyk
    ])
    def test_follows_server(self, monkeypatch, env, chosen, expected):
        monkeypatch.setattr(cli, "LANG_FORCED", False)
        monkeypatch.setattr(cli, "LANG", "en")
        monkeypatch.setenv("PIPE_LANG", env)
        asyncio.run(cli._sync_language(self.Client("pl", chosen)))
        assert cli.LANG == expected

    def test_lang_flag_wins(self, monkeypatch):
        monkeypatch.setattr(cli, "LANG_FORCED", True)
        monkeypatch.setattr(cli, "LANG", "en")
        asyncio.run(cli._sync_language(self.Client("pl", True)))
        assert cli.LANG == "en"
