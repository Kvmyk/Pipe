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
    assert options[:2] == ["status", "raport"]
    assert len(options) == len(cli.COMMANDS) + 1
    assert "odnow_certyfikat" in options


def test_prefix_narrows_and_exact_match_goes_first(monkeypatch):
    monkeypatch.setattr(cli, "LANG", "pl")
    assert names("/sta") == ["status"]
    assert names("/co") == ["cofnij", "koszt"]       # "cost" pasuje po angielsku, polska nazwa pierwsza
    assert names("/mcp")[0] == "mcp"


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
    assert asyncio.run(typed("/\t\t\r")) == "/raport"
    assert asyncio.run(typed("co tam\t\r")).strip() == "co tam"
