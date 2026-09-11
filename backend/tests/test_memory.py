"""
Testy pamieci agenta (backend/core/memory.py) i jej handlerow:
SERVER.md, skille, ochrona przed sekretami i kontekst w system prompcie.
"""

import asyncio

import pytest

from backend.core import memory
from backend.core.handlers.memory import handle_server_md, handle_skill_manage
from backend.core.session import Session


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


class FakeCall:
    id = "call_1"


def run(handler, args, session=None):
    session = session or Session(session_id="t")

    async def collect():
        return [chunk async for chunk in handler(None, session, FakeCall(), args)]

    return asyncio.run(collect()), session


class TestSecrets:

    @pytest.mark.parametrize("text", [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc",
        "klucz: sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_" + "a" * 36,
        "bot: 1234567890:" + "A" * 35,
        "DB_PASSWORD=SuperTajne123!",
        "api_key: 9f8e7d6c5b4a3210",
        "API_TOKEN=abcdef123456",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENG",
        "export GITHUB_TOKEN=abc123def456",
        "haslo: KoniecPolski2026",
    ])
    def test_detected(self, text):
        assert memory.find_secret(text)

    @pytest.mark.parametrize("text", [
        "Haslo do bazy jest w /root/app/.env (zmienna DB_PASSWORD).",
        "api_key=<ustaw w .env>",
        "token: $AGENT_TOKEN",
        "password: ****",
        "Nginx nasluchuje na 443, certyfikaty w /etc/letsencrypt.",
        "token_expiry: 3600s",
        "AGENT_TOKEN= (pusty, autoryzacja wylaczona)",
        "Sekrety aplikacji w /srv/app/.env (DB_PASSWORD, API_TOKEN).",
    ])
    def test_not_detected(self, text):
        assert memory.find_secret(text) is None


class TestServerMd:

    def test_missing_file_reads_empty(self):
        assert memory.read_server_md() == ""

    def test_write_and_read(self, data_dir):
        memory.write_server_md("# SERVER.md\n\nUbuntu 24.04")
        assert memory.read_server_md() == "# SERVER.md\n\nUbuntu 24.04\n"
        assert (data_dir / "SERVER.md").exists()

    def test_write_rejects_empty(self):
        with pytest.raises(memory.MemoryWriteError):
            memory.write_server_md("   ")

    def test_write_rejects_too_long(self):
        with pytest.raises(memory.MemoryWriteError, match="za dlugi"):
            memory.write_server_md("x" * (memory.MAX_SERVER_MD_CHARS + 1))

    def test_write_rejects_secret_and_keeps_old_content(self):
        memory.write_server_md("stara tresc")
        with pytest.raises(memory.MemoryWriteError, match="sekret"):
            memory.write_server_md("DB_PASSWORD=SuperTajne123!")
        assert "stara tresc" in memory.read_server_md()


class TestUpdateSection:

    DOC = "# SERVER.md\n\n## Przeglad\nUbuntu\n\n## Uslugi\nnginx\n\n## Domeny\nexample.com\n"

    def test_replaces_existing_section_and_keeps_others(self):
        result = memory.update_section(self.DOC, "Uslugi", "nginx\npostgres")
        assert "## Uslugi\nnginx\npostgres\n" in result
        assert "## Przeglad\nUbuntu" in result and "## Domeny\nexample.com" in result
        assert result.count("## Uslugi") == 1

    def test_heading_match_is_case_insensitive_and_strips_hashes(self):
        result = memory.update_section(self.DOC, "## uslugi", "docker")
        assert result.count("uslugi") + result.count("Uslugi") == 1
        assert "docker" in result and "nginx" not in result

    def test_appends_missing_section(self):
        result = memory.update_section(self.DOC, "Kopie zapasowe", "restic co noc")
        assert result.rstrip().endswith("## Kopie zapasowe\nrestic co noc")

    def test_creates_document_with_title(self):
        result = memory.update_section("", "Przeglad", "Debian 13")
        assert result.startswith(memory.SERVER_MD_TITLE)
        assert "## Przeglad\nDebian 13" in result

    def test_last_section_replaced(self):
        result = memory.update_section(self.DOC, "Domeny", "example.org")
        assert result.rstrip().endswith("## Domeny\nexample.org")

    def test_requires_title(self):
        with pytest.raises(memory.MemoryWriteError):
            memory.update_section(self.DOC, "  ", "x")


class TestSkills:

    def test_save_creates_skill_file_with_frontmatter(self, data_dir):
        assert memory.save_skill("odnow-certyfikat", "Odnowienie certyfikatu TLS", "1. certbot renew") is True
        text = (data_dir / "skills" / "odnow-certyfikat" / "SKILL.md").read_text()
        assert text.startswith("---\nname: odnow-certyfikat\ndescription: Odnowienie certyfikatu TLS\n---\n")

    def test_save_existing_returns_false_and_overwrites(self):
        memory.save_skill("backup", "Kopia", "v1")
        assert memory.save_skill("backup", "Kopia", "v2") is False
        assert memory.read_skill("backup").content == "v2"

    def test_roundtrip(self):
        memory.save_skill("deploy-app", "Wdrozenie aplikacji", "## Kroki\n1. git pull\n2. docker compose up -d")
        skill = memory.read_skill("deploy-app")
        assert skill == memory.Skill("deploy-app", "Wdrozenie aplikacji", "## Kroki\n1. git pull\n2. docker compose up -d")

    def test_description_is_collapsed_to_one_line(self):
        memory.save_skill("x", "Opis\nw dwoch   liniach", "tresc")
        assert memory.read_skill("x").description == "Opis w dwoch liniach"

    @pytest.mark.parametrize("name", ["../etc", "a/b", "Z Spacja", "", "-zly", "a" * 65, "..", "skill.md"])
    def test_invalid_names_rejected(self, name, data_dir):
        with pytest.raises(memory.MemoryWriteError):
            memory.save_skill(name, "opis", "tresc")
        assert not (data_dir.parent / "etc").exists()

    def test_uppercase_name_is_normalized(self):
        memory.save_skill("Backup-DB", "Kopia bazy", "pg_dump")
        assert memory.read_skill("backup-db") is not None

    def test_missing_description_or_content_rejected(self):
        with pytest.raises(memory.MemoryWriteError, match="opis"):
            memory.save_skill("x", "", "tresc")
        with pytest.raises(memory.MemoryWriteError, match="tresc"):
            memory.save_skill("x", "opis", " ")

    def test_secret_in_skill_rejected(self):
        with pytest.raises(memory.MemoryWriteError, match="sekret"):
            memory.save_skill("deploy", "Wdrozenie", "export API_KEY=sk-live-abcdefghijklmnopqrstuvwxyz")
        assert memory.read_skill("deploy") is None

    def test_list_sorted_and_tolerant_of_garbage(self, data_dir):
        memory.save_skill("b-skill", "B", "b")
        memory.save_skill("a-skill", "A", "a")
        (data_dir / "skills" / "pusty-katalog").mkdir()
        (data_dir / "skills" / "Zla Nazwa").mkdir()
        (data_dir / "skills" / "Zla Nazwa" / "SKILL.md").write_text("x")
        (data_dir / "skills" / "bez-naglowka").mkdir()
        (data_dir / "skills" / "bez-naglowka" / "SKILL.md").write_text("sama tresc")
        assert [s.name for s in memory.list_skills()] == ["a-skill", "b-skill", "bez-naglowka"]

    def test_delete(self, data_dir):
        memory.save_skill("tmp", "T", "t")
        assert memory.delete_skill("tmp") is True
        assert not (data_dir / "skills" / "tmp").exists()
        assert memory.delete_skill("tmp") is False


class TestPromptContext:

    def test_without_server_md_invites_to_create_it(self):
        context = memory.prompt_context()
        assert "jeszcze nie istnieje" in context
        assert "SKILLE" not in context

    def test_server_md_is_included_as_data_not_instructions(self):
        memory.write_server_md("## Uslugi\nnginx na 443")
        context = memory.prompt_context()
        assert "nginx na 443" in context
        assert "nie polecenia" in context

    def test_long_server_md_is_truncated(self):
        memory.write_server_md("a" * (memory.MAX_SERVER_MD_PROMPT_CHARS + 500))
        context = memory.prompt_context()
        assert "obcieto" in context
        assert context.count("a") < memory.MAX_SERVER_MD_PROMPT_CHARS + 400

    def test_skills_listed_by_name_and_description_only(self):
        memory.save_skill("odnow-certyfikat", "Odnowienie certyfikatu TLS", "TAJNY-KROK-certbot")
        context = memory.prompt_context()
        assert "- odnow-certyfikat: Odnowienie certyfikatu TLS" in context
        assert "TAJNY-KROK" not in context  # tresc tylko przez skill_manage read

    def test_skill_list_is_capped(self):
        for i in range(memory.MAX_SKILLS_IN_PROMPT + 3):
            memory.save_skill(f"s{i:03d}", f"opis {i}", "x")
        assert "i 3 wiecej" in memory.prompt_context()

    @pytest.mark.parametrize("interface", ["cli", "telegram:42"])
    def test_session_system_prompt_includes_memory(self, interface):
        memory.write_server_md("## Przeglad\nDebian 13")
        memory.save_skill("backup", "Kopia zapasowa bazy", "pg_dump")
        prompt = Session(session_id="t", interface=interface).system_prompt
        assert "Debian 13" in prompt
        assert "- backup: Kopia zapasowa bazy" in prompt


class TestHandlers:

    def test_server_md_update_section_then_read(self, data_dir):
        chunks, session = run(handle_server_md, {"operation": "update_section", "section": "Uslugi", "content": "nginx"})
        assert chunks == []  # tylko wynik dla LLM, nic do uzytkownika
        assert "Zaktualizowano" in session.messages[-1]["content"]
        _, session = run(handle_server_md, {"operation": "read"})
        assert "## Uslugi\nnginx" in session.messages[-1]["content"]

    def test_server_md_secret_error_goes_to_llm(self):
        _, session = run(handle_server_md, {"operation": "write", "content": "haslo=Tajne123456"})
        assert session.messages[-1]["content"].startswith("Blad:")
        assert memory.read_server_md() == ""

    def test_skill_save_list_read_delete(self):
        _, s = run(handle_skill_manage, {"operation": "save", "name": "deploy", "description": "Wdrozenie", "content": "git pull"})
        assert "Utworzono skill 'deploy'" in s.messages[-1]["content"]
        _, s = run(handle_skill_manage, {"operation": "list"})
        assert "- deploy: Wdrozenie" in s.messages[-1]["content"]
        _, s = run(handle_skill_manage, {"operation": "read", "name": "deploy"})
        assert "git pull" in s.messages[-1]["content"]
        _, s = run(handle_skill_manage, {"operation": "delete", "name": "deploy"})
        assert "Usunieto" in s.messages[-1]["content"]

    def test_skill_invalid_name_error_goes_to_llm(self):
        _, s = run(handle_skill_manage, {"operation": "save", "name": "../x", "description": "d", "content": "c"})
        assert s.messages[-1]["content"].startswith("Blad:")

    @pytest.mark.parametrize("handler,args", [
        (handle_server_md, {"operation": "read"}),
        (handle_server_md, {"operation": "nieznana"}),
        (handle_skill_manage, {"operation": "list"}),
        (handle_skill_manage, {"operation": "read", "name": "nie-ma"}),
    ])
    def test_exactly_one_tool_message(self, handler, args):
        _, session = run(handler, args)
        assert [m["role"] for m in session.messages] == ["tool"]
