"""
Testy DIRECTORY, VIBE, redakcji sekretow i warstwy runtime.
"""

import asyncio

import pytest

from backend.core import memory, runtime, vibe
from backend.core.session import Session


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


class TestDirectory:

    def test_upsert_and_render(self):
        assert memory.upsert_directory("/srv/shop", "repo", "sklep internetowy", remote="https://x:tok@github.com/a/b")
        assert not memory.upsert_directory("/hostfs/srv/shop", "repo", "sklep (Next.js)")
        [entry] = memory.load_directory()
        assert entry.path == "/srv/shop" and entry.description == "sklep (Next.js)"
        assert entry.remote == "https://github.com/a/b"
        assert "/srv/shop [repo] sklep (Next.js)" in memory.render_directory()

    def test_scan_keeps_agent_description(self):
        memory.upsert_directory("/srv/shop", "app", "glowna aplikacja")
        memory.upsert_directory("/srv/shop", "repo", "", branch="main", source="scan", keep_description=True)
        [entry] = memory.load_directory()
        assert (entry.kind, entry.description, entry.branch) == ("app", "glowna aplikacja", "main")

    @pytest.mark.parametrize("path,kind,description", [
        ("srv/relative", "repo", ""),
        ("/srv/x", "nieznany", ""),
        ("/srv/x", "repo", "haslo=SuperTajne123!"),
    ])
    def test_rejects(self, path, kind, description):
        with pytest.raises(memory.MemoryWriteError):
            memory.upsert_directory(path, kind, description)

    def test_in_prompt(self):
        memory.upsert_directory("/srv/shop", "repo", "sklep")
        assert "DIRECTORY" in memory.prompt_context() and "/srv/shop" in memory.prompt_context()


class TestVibe:

    def test_per_user_files(self):
        memory.write_vibe("telegram:123", "# VIBE\n\n- pisze krotko")
        assert "krotko" in memory.read_vibe("telegram:123")
        assert memory.read_vibe("cli") == ""
        assert "krotko" in memory.prompt_context(memory.vibe_key("telegram:123"))
        assert memory.reset_vibe("telegram:123") and memory.read_vibe("telegram:123") == ""

    def test_key_is_filesystem_safe(self):
        assert memory.vibe_key("telegram:123") == "telegram-123"
        assert "/" not in memory.vibe_key("../../etc/passwd")

    def test_distill_writes_note(self):
        class Agent:
            async def complete(self, system, user, model=None, who=""):
                assert "wiadomosci uzytkownika" in user and "ile mam ramu" in user
                return "```markdown\n# VIBE\n\n- pisze krotko, bez polskich znakow\n```"

        learner = vibe.VibeLearner(Agent())
        text = asyncio.run(learner.distill("cli", ["ile mam ramu", "a dysk?"]))
        assert text.startswith("# VIBE") and "krotko" in memory.read_vibe("cli")

    def test_distill_ignores_garbage(self):
        class Agent:
            async def complete(self, system, user, model=None, who=""):
                return "Nie mam obserwacji."

        assert asyncio.run(vibe.VibeLearner(Agent()).distill("cli", ["x"])) is None
        assert memory.read_vibe("cli") == ""

    def test_generated_messages_are_skipped(self):
        session = Session(messages=[
            {"role": "user", "content": "moja wiadomosc"},
            {"role": "user", "content": "Zbadaj serwer...", "pipe_generated": True},
        ])
        assert vibe.recent_user_messages(session, 5) == ["moja wiadomosc"]


class TestRedaction:

    @pytest.mark.parametrize("secret", [
        "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx",
    ])
    def test_tokens(self, secret):
        text, count = memory.redact_secrets(f"klucz: {secret} koniec")
        assert secret not in text and count == 1 and "koniec" in text

    def test_key_value_keeps_key(self):
        text, _ = memory.redact_secrets('DB_PASSWORD="bardzo-tajne-haslo"\nPORT=5432\nTOKEN=<ustaw>')
        assert "bardzo-tajne" not in text and "DB_PASSWORD=[ZREDAGOWANO" in text
        assert "PORT=5432" in text and "TOKEN=<ustaw>" in text

    def test_private_key_block(self):
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\nC1yc2E\n-----END OPENSSH PRIVATE KEY-----\n"
        text, count = memory.redact_secrets("przed\n" + pem + "po")
        assert "AAAAB3Nza" not in text and "przed" in text and "po" in text and count == 1


class TestRuntime:

    def test_docker(self, monkeypatch):
        monkeypatch.setenv("PIPE_RUNTIME", "docker")
        assert runtime.to_local("/etc/nginx") == "/hostfs/etc/nginx"
        assert runtime.to_local("/") == "/hostfs"
        assert runtime.to_host("/hostfs/var/www") == "/var/www"
        assert runtime.to_host("www", "/var") == "/var/www"

    def test_native(self, monkeypatch):
        monkeypatch.setenv("PIPE_RUNTIME", "native")
        assert runtime.to_local("/etc/nginx") == "/etc/nginx"
        assert runtime.host_proc() == "/proc"
        assert runtime.workspace_root() == "/"
        assert "native" in runtime.describe()

    def test_kubernetes_without_node_mount(self, monkeypatch):
        monkeypatch.setenv("PIPE_RUNTIME", "kubernetes")
        monkeypatch.setattr(runtime.os.path, "isdir", lambda p: False)
        assert runtime.host_root() == "" and "kubectl" in runtime.describe()

    def test_native_workspace_validation(self, monkeypatch):
        from backend.core.security import validate_workspace_access
        monkeypatch.setenv("PIPE_RUNTIME", "native")
        assert validate_workspace_access("/etc/nginx/nginx.conf") == (True, "OK")
        assert validate_workspace_access("/etc/shadow")[0] is False
        assert validate_workspace_access("/home/kuba/.ssh/id_ed25519")[0] is False
        assert validate_workspace_access("/proc/1/environ")[0] is False
