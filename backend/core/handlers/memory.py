"""
Handlery pamieci agenta: SERVER.md, DIRECTORY, skille, VIBE.

Pamiec trafia do promptu KAZDEJ przyszlej rozmowy (SERVER.md, DIRECTORY, VIBE)
albo jest ladowana na zadanie (skille), wiec jeden udany prompt injection
moglby zapisac tam trwala instrukcje. Dlatego:
  - kazdy zapis jest zglaszany uzytkownikowi widocznym komunikatem (nie da sie
    zmienic pamieci po cichu),
  - zapis/zastapienie skilla i pelne nadpisanie SERVER.md wymagaja potwierdzenia.
Zapis nadal nie zmienia serwera, wiec reszta operacji idzie bez potwierdzenia.
Kazdy zapis trafia do audit logu (sama sciezka, bez tresci).
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from backend.core import audit, memory, safety
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code


def _reply(session: Session, tool_call: Any, content: str) -> None:
    session.messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})


def _notice(what: str) -> str:
    """Widoczny dla uzytkownika komunikat o zmianie w pamieci agenta."""
    return f"[PAMIEC] {what}"


def _diff(old: str, new: str, max_lines: int = 40) -> str:
    import difflib
    if not old.strip():
        body = new.strip().splitlines()[:max_lines]
        return "Nowa tresc:\n" + as_code("\n".join(body) or "(pusta)")
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     fromfile="obecny", tofile="nowy", lineterm=""))
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"[... i {len(diff) - max_lines} linii roznicy]"]
    return "Zmiany:\n" + as_code("\n".join(diff) or "(brak roznic)")


async def handle_server_md(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie server_md: read, update_section, write."""
    operation = (args.get("operation") or "read").strip()
    try:
        if operation == "read":
            _reply(session, tool_call, memory.read_server_md() or "(SERVER.md jeszcze nie istnieje)")
            return
        if operation == "update_section":
            section = args.get("section", "")
            document = memory.update_section(memory.read_server_md(), section, args.get("content", ""))
            memory.write_server_md(document)
            await audit.log_file_write(session.interface, f"SERVER.md#{section.strip()}", 0)
            yield _notice(f"zaktualizowano sekcje '{section.strip()}' w SERVER.md")
            _reply(session, tool_call, f"Zaktualizowano sekcje '{section.strip()}' w SERVER.md.")
            return
        if operation == "write":
            # Pelne nadpisanie SERVER.md (dolaczanego do kazdej rozmowy) wymaga potwierdzenia —
            # to najsilniejszy wektor trwalej injekcji. Zmiane widac przed TAK.
            content = memory.check_server_md(str(args.get("content", "")))  # walidacja bez zapisu
            preview = _diff(memory.read_server_md(), content)

            async def do_write(content=content, interface=session.interface) -> str:
                memory.write_server_md(content)
                await audit.log_file_write(interface, "SERVER.md", 0)
                return "Nadpisano caly SERVER.md."

            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id, tool_name="server_md",
                command="pelne nadpisanie SERVER.md", classification="confirm", action=do_write,
                plan=safety.Plan(local_files=[(str(memory.server_md_path()), "pamiec Pipe: SERVER.md")]),
            )
            yield f"[POTWIERDZ] Nadpisanie calego SERVER.md wymaga potwierdzenia.\n{preview}"
            return
        _reply(session, tool_call, f"Nieznana operacja {operation!r}. Dostepne: read, update_section, write.")
    except memory.MemoryWriteError as exc:
        _reply(session, tool_call, f"Blad: {exc}")
    except OSError as exc:
        _reply(session, tool_call, f"Blad zapisu SERVER.md: {exc}")


async def handle_skill_manage(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie skill_manage: list, read, save, delete."""
    operation = (args.get("operation") or "list").strip()
    name = args.get("name", "")
    try:
        if operation == "list":
            skills = memory.list_skills()
            _reply(session, tool_call, "\n".join(f"- {s.name}: {s.description}" for s in skills)
                   or "(brak zapisanych skilli)")
            return
        if operation == "read":
            skill = memory.read_skill(name)
            _reply(session, tool_call, f"# {skill.name}\n{skill.description}\n\n{skill.content}" if skill
                   else f"Brak skilla {name!r}. Dostepne: skill_manage, operation=list.")
            return
        if operation == "save":
            # Skill jest ladowany na zadanie i wykonuje kroki w przyszlych sesjach —
            # zapis wymaga potwierdzenia (widoczna tresc), zeby injection nie zaszczepil procedury.
            description = args.get("description", "")
            content = args.get("content", "")
            memory.validate_skill_content(name, description, content)  # walidacja bez zapisu
            skill_name = memory.validate_skill_name(name)
            existing = memory.read_skill(skill_name)
            preview = as_code(f"{description}\n\n{str(content).strip()[:1500]}")

            async def do_save(n=skill_name, d=description, c=content, interface=session.interface) -> str:
                created = memory.save_skill(n, d, c)
                await audit.log_file_write(interface, f"skills/{n}/SKILL.md", 0)
                return f"{'Utworzono' if created else 'Zastapiono'} skill '{n}'."

            verb = "Zastapienie" if existing else "Zapis"
            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id, tool_name="skill_manage",
                command=f"zapis skilla {skill_name}", classification="confirm", action=do_save,
                plan=safety.Plan(local_files=[(str(memory.skills_dir() / skill_name / "SKILL.md"),
                                               f"pamiec Pipe: skill {skill_name}")]),
            )
            yield f"[POTWIERDZ] {verb} skilla {as_code(skill_name)} wymaga potwierdzenia:\n{preview}"
            return
        if operation == "delete":
            if memory.delete_skill(name):
                skill_name = memory.validate_skill_name(name)
                await audit.log_file_write(session.interface, f"skills/{skill_name}/SKILL.md (usuniety)", 0)
                yield _notice(f"usunieto skill '{skill_name}'")
                _reply(session, tool_call, f"Usunieto skill '{name}'.")
            else:
                _reply(session, tool_call, f"Brak skilla {name!r} — nic nie usunieto.")
            return
        _reply(session, tool_call, f"Nieznana operacja {operation!r}. Dostepne: list, read, save, delete.")
    except memory.MemoryWriteError as exc:
        _reply(session, tool_call, f"Blad: {exc}")
    except OSError as exc:
        _reply(session, tool_call, f"Blad zapisu skilla: {exc}")


async def handle_directory(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsluguje narzedzie directory: list, upsert, remove, scan."""
    from backend.core import infra

    operation = (args.get("operation") or "list").strip()
    notice = ""
    try:
        if operation == "list":
            result = memory.render_directory() or "(DIRECTORY jest puste — uzyj operation=scan albo upsert)"
        elif operation == "upsert":
            created = memory.upsert_directory(
                str(args.get("path", "")), str(args.get("kind", "other")), str(args.get("description", "")),
                remote=str(args.get("remote", "") or ""), branch=str(args.get("branch", "") or ""),
            )
            path = memory.normalize_dir_path(str(args.get("path", "")))
            await audit.log_file_write(session.interface, f"directory.json#{path}", 0)
            result = f"{'Dodano' if created else 'Zaktualizowano'} {path} w DIRECTORY."
            notice = _notice(f"{'dodano' if created else 'zaktualizowano'} {path} w DIRECTORY")
        elif operation == "remove":
            path = str(args.get("path", ""))
            if memory.remove_directory(path):
                result = f"Usunieto {path} z DIRECTORY."
                notice = _notice(f"usunieto {memory.normalize_dir_path(path)} z DIRECTORY")
            else:
                result = f"Nie ma {path} w DIRECTORY."
        elif operation == "scan":
            added: list[str] = []
            for repo in await asyncio.to_thread(infra.git_repos):  # os.walk po /home bywa wolny
                if memory.upsert_directory(repo.path, "repo", "", remote=repo.remote, branch=repo.branch,
                                           source="scan", keep_description=True):
                    added.append(repo.path)
            containers = await infra.docker_containers() or []
            for project in sorted({(c.project, c.workdir) for c in containers if c.project and c.workdir}):
                name, workdir = project
                if memory.upsert_directory(workdir, "compose", f"projekt docker compose '{name}'",
                                           source="scan", keep_description=True):
                    added.append(workdir)
            await audit.log_file_write(session.interface, "directory.json (skan)", 0)
            entries = memory.load_directory()
            without = [e.path for e in entries if not e.description or e.description.startswith("projekt docker compose")]
            result = (f"Skan zakonczony: nowych wpisow {len(added)}, razem {len(entries)}.\n"
                      + memory.render_directory(entries)
                      + ("\n\nWpisy bez opisu (uzupelnij upsert z description — co to za aplikacja): "
                         + ", ".join(without[:20]) if without else ""))
            notice = _notice(f"skan DIRECTORY: {len(added)} nowych wpisow")
        else:
            result = f"Nieznana operacja {operation!r}. Dostepne: list, upsert, remove, scan."
    except memory.MemoryWriteError as exc:
        result = f"Blad: {exc}"
    except OSError as exc:
        result = f"Blad zapisu DIRECTORY: {exc}"

    if notice:
        yield notice
    _reply(session, tool_call, result)


async def handle_vibe(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsluguje narzedzie vibe: read, update (dla biezacego uzytkownika)."""
    operation = (args.get("operation") or "read").strip()
    key = session.user_key
    notice = ""
    try:
        if operation == "read":
            result = memory.read_vibe(key) or "(brak notatki VIBE dla tego uzytkownika)"
        elif operation == "update":
            memory.write_vibe(key, str(args.get("content", "")))
            await audit.log_file_write(session.interface, f"vibe/{key}.md", 0)
            result = "Zaktualizowano VIBE tego uzytkownika."
            notice = _notice("zaktualizowano notatke VIBE o stylu rozmowy")
        else:
            result = f"Nieznana operacja {operation!r}. Dostepne: read, update."
    except memory.MemoryWriteError as exc:
        result = f"Blad: {exc}"
    except OSError as exc:
        result = f"Blad zapisu VIBE: {exc}"

    if notice:
        yield notice
    _reply(session, tool_call, result)
