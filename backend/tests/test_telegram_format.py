"""
Testy formatowania odpowiedzi dla Telegrama (clients/telegram/tg_format.py).

Najwazniejsze: potwierdzenie operacji musi pokazac DOKLADNIE te komende,
ktora zostanie wykonana — inaczej uzytkownik zatwierdza cos, czego nie widzi.
"""

import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))

from backend.core.text import as_code  # noqa: E402
from tg_format import (  # noqa: E402
    BUILTIN_COMMANDS,
    MAX_MENU_COMMANDS,
    build_menu,
    collect_response_text,
    format_help,
    format_skill_list,
    parse_command,
    response_data,
    split_message,
    to_plain_text,
    to_telegram_html,
)

TELEGRAM_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a",
                 "span", "tg-spoiler", "tg-emoji", "blockquote"}


class _TagBalance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in TELEGRAM_TAGS:
            self.errors.append(f"tag spoza Telegrama: <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"niepasujacy </{tag}>")


def assert_valid_telegram_html(text: str) -> None:
    """Przyblizenie parsera Telegrama: brak surowych '<'/'&', zbalansowane dozwolone tagi."""
    assert not re.search(r"&(?!(lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);)", text), f"surowy '&': {text!r}"
    assert not re.search(r"<(?!/?[a-zA-Z-]+[\s>])", text), f"surowy '<': {text!r}"
    checker = _TagBalance()
    checker.feed(text)
    assert not checker.errors and not checker.stack, (checker.errors, checker.stack, text)


def rendered(text: str) -> str:
    """Co zobaczy uzytkownik po wyrenderowaniu HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def confirmation_message(command: str) -> str:
    """Komunikat w dokladnie takim formacie, jaki wysyla handler."""
    return f"[POTWIERDZ] Operacja wymaga potwierdzenia: {as_code(command)}"


def confirmation_shown_for(command: str) -> str:
    """Pelna sciezka bota: komunikat handlera -> collect -> HTML."""
    responses = [
        {"response": confirmation_message(command), "status": "confirm", "done": False},
        {"response": "", "status": "ok", "done": True},
    ]
    text, needs_confirm = collect_response_text(responses)
    assert needs_confirm
    return to_telegram_html(text)


DANGEROUS_COMMANDS = [
    "rm /var/log/*.gz /tmp/*.old",
    "mysql produkcja < drop_all.sql",
    "find /srv -name '*.bak' -o -name '*.tmp' -delete",
    r"sed -i 's/\./_/g' plik.conf",
    "cp config_prod_ config_dev_",
    "make build 2>&1 | tee log && rm -rf _build_",
    "[ -f /etc/x ] && echo <tag> # nie naglowek",
    "echo **nie pogrubiaj** __ani tego__",
    "cat &amp; plik",  # doslownie '&amp;' — nie wolno go rozwinac do '&'
    "echo `whoami` > /tmp/kto",
    "kill $(cat ``/run/x.pid``)",
    "`id`",
    "cat > /etc/motd <<'EOF'\nWitaj *uzytkowniku* na _serwerze_\n```\nEOF",
]


class TestConfirmationShowsExactCommand:

    @pytest.mark.parametrize("command", DANGEROUS_COMMANDS)
    def test_rendered_command_is_identical(self, command):
        shown = confirmation_shown_for(command)
        assert command in rendered(shown), f"widac: {rendered(shown)!r}"

    @pytest.mark.parametrize("command", DANGEROUS_COMMANDS)
    def test_confirmation_is_valid_telegram_html(self, command):
        assert_valid_telegram_html(confirmation_shown_for(command))

    @pytest.mark.parametrize("command", DANGEROUS_COMMANDS)
    def test_plain_text_fallback_also_shows_exact_command(self, command):
        assert command in to_plain_text(confirmation_shown_for(command))

    def test_forbidden_command_refusal_shows_exact_command(self):
        text, _ = collect_response_text([
            {"response": f"[ODMOWA] Komenda {as_code('rm -rf /*')} jest zabroniona.", "status": "error", "done": False},
        ])
        assert "rm -rf /*" in rendered(to_telegram_html(text))


class TestCliShowsExactCommand:
    """CLI renderuje odpowiedzi przez rich.Markdown (clients/cli/cli.py::_print_response)."""

    @pytest.mark.parametrize("command", DANGEROUS_COMMANDS)
    def test_rendered_command_is_identical(self, command):
        rich = pytest.importorskip("rich")
        import io
        from rich.console import Console
        from rich.markdown import Markdown

        text = confirmation_message(command).replace("[POTWIERDZ]", "**WYMAGA POTWIERDZENIA:**")
        buf = io.StringIO()
        Console(file=buf, width=500, color_system=None).print(Markdown(text))
        output = buf.getvalue()
        # Bloki kodu rich rysuje z marginesem — porownujemy linie bez bialych znakow na brzegach.
        shown_lines = [line.strip() for line in output.splitlines()]
        for line in command.splitlines():
            assert line.strip() in output if "\n" not in command else line.strip() in shown_lines, output

    @pytest.mark.parametrize("command", DANGEROUS_COMMANDS)
    def test_confirmation_panel_shows_exact_command(self, command):
        """Panel potwierdzenia (cli._confirmation): komenda jednoliniowa jako zwykly tekst, znak w znak."""
        pytest.importorskip("rich")
        import io
        from rich.console import Console
        from clients.cli import cli

        message = confirmation_message(command) + "\nBezpiecznik:\n- kopia przed zmiana: /etc/x\n- cofniecie pozniej: /cofnij"
        buf = io.StringIO()
        Console(file=buf, width=500, color_system=None).print(cli._confirmation(message))
        output = buf.getvalue()
        if "\n" in command:
            shown_lines = [line.strip(" │") for line in output.splitlines()]
            for line in command.splitlines():
                assert line.strip() in shown_lines, output
        else:
            assert "$ " + command in output, output
        assert "/etc/x" in output and "[POTWIERDZ]" not in output


class TestAsCode:

    def test_plain_command_single_backticks(self):
        assert as_code("ls -la") == "`ls -la`"

    def test_fence_longer_than_inner_backticks(self):
        assert as_code("echo `whoami` x") == "``echo `whoami` x``"

    def test_padding_when_text_touches_the_fence(self):
        """CommonMark zdejmuje po jednej spacji z obu stron — tekst sie nie zmienia."""
        assert as_code("echo `whoami`") == "`` echo `whoami` ``"

    def test_multiline_becomes_block(self):
        assert as_code("a\nb") == "\n```\na\nb\n```\n"

    def test_multiline_with_triple_backticks_uses_longer_fence(self):
        assert as_code("a\n```\nb").startswith("\n````\n")


class TestLlmHtmlIsPreserved:

    def test_allowed_tags_pass_through(self):
        text = "<b>Status serwera</b>\n• RAM: <code>1.2 GB / 2.0 GB</code>\n<i>/etc/nginx</i>"
        assert to_telegram_html(text) == text

    def test_existing_entities_are_not_double_escaped(self):
        assert to_telegram_html("Zuzycie &lt; 50% &amp; stabilne") == "Zuzycie &lt; 50% &amp; stabilne"

    def test_bare_special_characters_are_escaped(self):
        result = to_telegram_html("load < 1 & RAM > 50%")
        assert result == "load &lt; 1 &amp; RAM &gt; 50%"
        assert_valid_telegram_html(result)

    def test_unknown_tags_are_escaped(self):
        result = to_telegram_html("<div>x</div> <script>alert(1)</script>")
        assert "<div>" not in result and "<script>" not in result
        assert_valid_telegram_html(result)

    def test_link_with_attributes_passes_through(self):
        text = '<a href="https://example.com">link</a>'
        assert to_telegram_html(text) == text


class TestMarkdownArtifacts:

    def test_bold(self):
        assert to_telegram_html("**Dysk** i *RAM*") == "<b>Dysk</b> i <b>RAM</b>"

    def test_italic(self):
        assert to_telegram_html("plik _konfiguracyjny_") == "plik <i>konfiguracyjny</i>"

    def test_header(self):
        assert to_telegram_html("## Podsumowanie") == "<b>Podsumowanie</b>"

    def test_code_block_is_literal(self):
        result = to_telegram_html("```bash\nls *.log *.tmp\n```")
        assert result == "<pre>ls *.log *.tmp</pre>"

    def test_single_line_triple_backticks_is_inline_code(self):
        assert to_telegram_html("```ls *.log```") == "<code>ls *.log</code>"

    def test_multiplication_is_not_bold(self):
        assert to_telegram_html("2 * 3 * 4") == "2 * 3 * 4"

    def test_snake_case_is_not_italic(self):
        assert to_telegram_html("zmienna max_tool_iterations") == "zmienna max_tool_iterations"


class TestSplitMessage:

    def test_short_message_is_single_chunk(self):
        assert split_message("krotko") == ["krotko"]

    def test_long_message_splits_on_newline(self):
        text = ("a" * 30 + "\n") * 10
        chunks = split_message(text, max_length=100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


SKILLS = [
    {"name": "odnow-certyfikat", "description": "Odnowienie TLS dla <nginx>", "command": "odnow_certyfikat"},
    {"name": "status", "description": "Zarezerwowana nazwa", "command": ""},
]


class TestParseCommand:

    @pytest.mark.parametrize("text,expected", [
        ("/server", ("server", "")),
        ("/server aktualizuj", ("server", "aktualizuj")),
        ("/Deploy_App@PipeBot  na produkcji ", ("deploy_app", "na produkcji")),
        ("/odnow_certyfikat tylko dla\nexample.com", ("odnow_certyfikat", "tylko dla\nexample.com")),
        ("", ("", "")),
    ])
    def test_parse(self, text, expected):
        assert parse_command(text) == expected


class TestBuildMenu:

    def test_builtins_first_then_skills_with_command(self):
        from tg_format import MENU_COMMANDS

        menu = build_menu(SKILLS)
        # menu "/" to tylko codzienne komendy (reszta w /pomoc), w ustalonej kolejnosci, potem skille
        assert [name for name, _ in menu[:len(MENU_COMMANDS)]] == list(MENU_COMMANDS)
        assert menu[0] == ("status", dict(BUILTIN_COMMANDS)["status"]) and menu[1][0] == "nowosci"
        assert menu[len(MENU_COMMANDS):] == [("odnow_certyfikat", "Odnowienie TLS dla <nginx>")]
        assert set(MENU_COMMANDS) <= set(dict(BUILTIN_COMMANDS))

    def test_every_builtin_is_in_help(self):
        from tg_format import format_help

        from tg_format import HIDDEN_COMMANDS

        text = format_help([])
        assert all(f"/{name}" in text for name, _ in BUILTIN_COMMANDS if name not in HIDDEN_COMMANDS)
        # ukryte dzialaja (sa w BUILTIN_COMMANDS, wiec maja handler), ale pomoc ich nie pokazuje
        assert all(not re.search(rf"/{name}\b", text) and name in dict(BUILTIN_COMMANDS) for name in HIDDEN_COMMANDS)

    def test_limits(self):
        many = [{"name": f"s{i}", "description": "x" * 400, "command": f"s{i}"} for i in range(200)]
        menu = build_menu(many)
        assert len(menu) == MAX_MENU_COMMANDS
        assert all(len(description) <= 256 for _, description in menu)

    def test_empty_description_gets_fallback(self):
        assert build_menu([{"name": "a", "description": " ", "command": "a"}])[-1] == ("a", "Skill")


class TestSkillListAndHelp:

    def test_skill_list_is_valid_html_with_commands(self):
        text = format_skill_list(SKILLS)
        assert "/odnow_certyfikat" in text
        assert "&lt;nginx&gt;" in text  # opis escapowany
        assert "uruchom skill status" in text  # skill bez komendy
        assert_valid_telegram_html(text)

    def test_empty_skill_list(self):
        assert "Brak zapisanych skilli" in format_skill_list([])

    def test_help_lists_builtins_and_skill_count(self):
        text = format_help(SKILLS)
        from tg_format import HIDDEN_COMMANDS

        assert all(f"/{command}" in text for command, _ in BUILTIN_COMMANDS if command not in HIDDEN_COMMANDS)
        assert "Skille (1)" in text
        assert_valid_telegram_html(text)

    def test_server_md_through_bot_formatting(self):
        """cmd_server wysyla '<b>SERVER.md</b>' + surowy Markdown przez te sama sciezke co odpowiedzi."""
        text, _ = collect_response_text([{"response": "<b>SERVER.md</b>\n\n## Uslugi\n- nginx <443> & certbot", "status": "ok"}])
        result = to_telegram_html(text)
        assert "<b>Uslugi</b>" in result and "&lt;443&gt; &amp; certbot" in result
        assert_valid_telegram_html(result)


class TestResponseData:

    def test_returns_data(self):
        assert response_data([{"response": "", "status": "ok", "done": True, "data": {"skills": []}}]) == {"skills": []}

    def test_backend_error_raises(self):
        with pytest.raises(RuntimeError, match="token"):
            response_data([{"response": "Blad: Nieprawidlowy token autoryzacji.", "status": "error", "done": True}])



class TestConfirmationLayout:
    """Prosba o zgode: naglowek raz, ogolnikowy temat znika, plan bezpiecznika jako lista z kropkami."""

    def test_generic_subject_dropped_and_fuse_rows_as_bullets(self):
        text, needs = collect_response_text([{"response": "[POTWIERDZ] Operacja wymaga potwierdzenia: `rm x`\n"
                                                          "Bezpiecznik:\n- kopia przed zmiana: /etc/x", "status": "confirm"}])
        shown = rendered(to_telegram_html(text))
        assert needs and "Operacja" not in shown and "wymaga potwierdzenia" not in shown
        assert "rm x" in shown and "• kopia przed zmiana: /etc/x" in shown

    def test_specific_subject_kept(self):
        text, _ = collect_response_text([{"response": "[POTWIERDZ] Zapis pliku wymaga potwierdzenia: `/etc/x`", "status": "confirm"}])
        assert "Zapis pliku:" in rendered(to_telegram_html(text))

    def test_diff_lines_in_preview_untouched(self):
        text, _ = collect_response_text([{"response": "[POTWIERDZ] Zapis pliku wymaga potwierdzenia: `/etc/x`\n"
                                                      "```diff\n- stare\n+ nowe\n```", "status": "confirm"}])
        assert "- stare" in rendered(to_telegram_html(text))


class TestProgressSummary:

    def test_done_collapses_steps(self):
        from tg_format import progress_text
        text = progress_text(["a <b>", "b"], done=True)
        assert "Wykonane kroki: 2" in text and "<blockquote expandable>" in text and "a &lt;b&gt;" in text
        assert_valid_telegram_html(text.replace("<blockquote expandable>", "<blockquote>"))

    def test_agent_step_with_output(self):
        from tg_format import activity_entry, progress_text
        base = {"type": "activity", "id": "c1", "tool": "execute_command", "label": "systemctl status nginx"}
        assert activity_entry({**base, "phase": "start"}) == "⏳ $ systemctl status nginx"
        assert "czeka na Twoja zgode" in activity_entry({**base, "phase": "wait"})
        done = activity_entry({**base, "phase": "end", "ok": True, "code": 3, "auto": True,
                               "output": "Active: failed\n<x> & y\nl3\nl4\nl5"})
        assert done.splitlines()[0] == "✓ $ systemctl status nginx (bez pytania, kod 3)"
        assert done.splitlines()[1:] == ["  Active: failed", "  <x> & y", "  l3", "  l4"]
        assert activity_entry({**base, "tool": "load_tools", "phase": "end", "ok": True}) == ""
        assert activity_entry({**base, "tool": "read_file", "label": "czyta /etc/hosts", "phase": "end",
                               "ok": False}) == "✗ czyta /etc/hosts"
        for text in (progress_text([done]), progress_text([done], done=True)):
            assert "&lt;x&gt; &amp; y" in text
            assert_valid_telegram_html(text.replace("<blockquote expandable>", "<blockquote>"))

    def test_long_outputs_fit_in_one_message(self):
        from tg_format import progress_text
        entries = ["✓ $ cmd\n" + "\n".join("x" * 160 for _ in range(4))] * 30
        for done in (False, True):
            text = progress_text(entries, done=done)
            assert len(text) < 4096
            assert_valid_telegram_html(text.replace("<blockquote expandable>", "<blockquote>"))
        assert "wczesniej" in progress_text(entries)


def test_cli_and_web_show_agent_steps():
    from pathlib import Path

    pytest.importorskip("rich")              # CI nie instaluje zaleznosci CLI — jak test_cli_completion.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "cli"))
    import cli

    event = {"type": "activity", "id": "c1", "tool": "execute_command", "label": "df -h [x]", "phase": "end",
             "ok": True, "output": "/dev/sda1 80%\nl2\nl3\nl4\nl5", "code": 1}
    lines = cli._activity_lines(event)
    assert "$ df -h \\[x]" in lines[0] and "kod 1" in lines[0] and len(lines) == 1 + cli.ACTIVITY_LINES
    assert cli._activity_lines({**event, "phase": "start"}) == [] and cli._activity_lines({**event, "ok": None}) == []
    static = Path(__file__).resolve().parents[2] / "clients" / "webui" / "static"
    app = (static / "app.js").read_text(encoding="utf-8")
    strings = (static / "i18n.js").read_text(encoding="utf-8")
    assert "function chatStep(event)" in app and "chatStep(event);" in app and "foldSteps();" in app
    for key in ("stepsRunning:", "stepsDone:", "autoDone:", "exitCode:", "fullOutput:"):
        assert strings.count(key) == 2, key


def test_commands_for_everyone_are_in_both_menus():
    """/aktualizuj, /web, /mcp... sa dla kazdego — nie moga zniknac z menu widoku prostego ani technicznego."""
    from tg_format import EVERYONE_COMMANDS, MENU_COMMANDS, MENU_COMMANDS_SIMPLE, TECH_COMMANDS
    assert set(EVERYONE_COMMANDS) <= set(MENU_COMMANDS)
    assert set(EVERYONE_COMMANDS) <= set(MENU_COMMANDS_SIMPLE)
    assert not set(EVERYONE_COMMANDS) & set(TECH_COMMANDS)
    assert set(MENU_COMMANDS_SIMPLE).isdisjoint(TECH_COMMANDS)
