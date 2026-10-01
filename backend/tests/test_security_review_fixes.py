"""
Regresje poprawek z audytu bezpieczenstwa (v0.9).

Kazdy test pilnuje jednej naprawionej podatnosci. Uzupelnia klasyfikacyjne
regresje w test_security_hardening.py o warstwy: redakcja, potwierdzenia,
audit log, token, tryb kubernetes.
"""

import asyncio

import pytest

from backend.core import memory
from backend.core.audit import _format_entry
from backend.core.text import as_code, visible


class TestRedaction:
    """Sekrety, ktore wczesniej wychodzily do providera LLM."""

    def test_url_credentials(self):
        out, n = memory.redact_secrets("DATABASE_URL=postgres://app:S3cretPass99@db:5432/shop")
        assert "S3cretPass99" not in out and n == 1 and "postgres://app:" in out and "@db:5432/shop" in out

    def test_url_credentials_empty_user(self):
        out, n = memory.redact_secrets("redis://:hunter2secret@cache:6379")
        assert "hunter2secret" not in out and n == 1

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijklmnopqrstuvwx"
        out, n = memory.redact_secrets(f"Authorization: Bearer {jwt}")
        assert jwt not in out and n == 1

    def test_shadow_hash(self):
        line = "root:$6$abcd1234$" + "X" * 40 + ":19000:0:99999:7:::"
        out, n = memory.redact_secrets(line)
        assert "$6$abcd1234$" not in out and n == 1

    def test_environ_nul_does_not_swallow_everything(self):
        # /proc/<pid>/environ jest rozdzielony NUL — redakcja nie moze przejsc przez cale pole
        env = "PATH=/usr/bin\x00DB_PASSWORD=supertajnehaslo\x00LANG=C.UTF-8"
        out, n = memory.redact_secrets(env)
        assert "supertajnehaslo" not in out and "PATH=/usr/bin" in out and "LANG=C.UTF-8" in out


class TestConfirmationIntegrity:
    """Potwierdzenie musi pokazac to, co sie wykona — bez ukrytej tresci."""

    def test_control_chars_are_neutralised(self):
        # ANSI/CR moglyby przemalowac terminal albo ukryc czesc komendy w potwierdzeniu
        shown = as_code("rm -rf /tmp/x\x1b[2K\rls -la")
        assert "\x1b" not in shown and "\r" not in shown and "\\x1b" in shown

    def test_bidi_override_is_neutralised(self):
        shown = as_code("echo safe‮ evil")
        assert "‮" not in shown

    def test_newlines_and_tabs_survive(self):
        assert "\n" in visible("line1\nline2\tend")

    def test_write_file_confirmation_shows_diff(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIPE_RUNTIME", "native")
        from backend.core.handlers.file_ops import handle_write_file
        from backend.core.session import Session

        target = tmp_path / "app.conf"
        target.write_text("port=8080\ndebug=false\n")

        class Call:
            id = "c1"

        session = Session(session_id="t")

        async def collect():
            return [c async for c in handle_write_file(None, session, Call(),
                                                       {"path": str(target), "content": "port=8080\ndebug=true\n"})]

        chunks = asyncio.run(collect())
        text = "\n".join(chunks)
        assert "wymaga potwierdzenia" not in text.lower() or "Zapis pliku" in text
        assert "-debug=false" in text and "+debug=true" in text        # uzytkownik widzi ROZNICE
        assert session.pending_confirmation is not None

    def test_write_file_warns_on_startup_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIPE_RUNTIME", "native")
        from backend.core.handlers.file_ops import startup_file_warning
        assert startup_file_warning("/root/.bashrc")
        assert startup_file_warning("/home/kuba/.ssh/authorized_keys")
        assert startup_file_warning("/etc/systemd/system/x.service")
        assert not startup_file_warning("/srv/app/config.yaml")


class TestAuditLog:

    def test_newline_in_command_is_escaped(self):
        entry = _format_entry("cli:kuba", "SAFE", "ls\n[2026-01-01] [cli] [SAFE] rm -rf / ", exit_code=0)
        assert "\n" not in entry and "\\n" in entry

    def test_normal_entry_unchanged(self):
        entry = _format_entry("telegram:1", "CONFIRMED", "docker restart web", exit_code=0)
        assert entry == "[" + entry[1:20] + "] [telegram:1] [CONFIRMED] docker restart web → exit_code=0"


class TestTokenAndModes:

    def test_token_uses_constant_time_compare(self):
        # server.py porownuje token przez hmac.compare_digest (w _authorize, wolanym przez handle_client)
        import inspect
        import backend.server as server
        from backend.core import auth
        assert "compare_digest" in inspect.getsource(auth.authorize)
        assert "_authorize(request)" in inspect.getsource(server.handle_client)

    @staticmethod
    def _with_key(monkeypatch):
        """Podklada konfiguracje LLM z kluczem, zeby validate() doszlo do sprawdzenia tokenu."""
        from dataclasses import replace
        from backend.config import settings
        monkeypatch.setattr(settings, "LLM", replace(settings.LLM, api_key="test-key"))

    def test_kubernetes_requires_token(self, monkeypatch):
        from backend.config import settings
        self._with_key(monkeypatch)
        monkeypatch.setenv("PIPE_RUNTIME", "kubernetes")
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        with pytest.raises(ValueError, match="kubernetes"):
            settings.validate()

    def test_kubernetes_with_token_ok(self, monkeypatch):
        from backend.config import settings
        self._with_key(monkeypatch)
        monkeypatch.setenv("PIPE_RUNTIME", "kubernetes")
        monkeypatch.setattr(settings, "AGENT_TOKEN", "sekret")
        settings.validate()   # nie rzuca

    def test_native_no_token_ok(self, monkeypatch):
        from backend.config import settings
        self._with_key(monkeypatch)
        monkeypatch.setenv("PIPE_RUNTIME", "native")
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        settings.validate()   # na VPS token opcjonalny (chroni tunel SSH)

    def test_from_env_generates_token(self, tmp_path, monkeypatch):
        from backend import configure
        env = tmp_path / ".env"
        monkeypatch.setattr(configure, "ENV_PATH", env)
        monkeypatch.setattr(configure, "ENV_EXAMPLE_PATH", tmp_path / "none")
        for k in list(configure.FROM_ENV_KEYS) + ["GEMINI_API_KEY"]:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
        assert configure.main(["--from-env"]) == 0
        written = configure.parse_env(env.read_text())
        assert len(written.get("AGENT_TOKEN", "")) >= 32
