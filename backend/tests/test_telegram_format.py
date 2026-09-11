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
from tg_format import collect_response_text, split_message, to_plain_text, to_telegram_html  # noqa: E402

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
