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
from backend.core.i18n import tr


def _reply(session: Session, tool_call: Any, content: str) -> None:
    session.messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})


def _notice(what: str) -> str:
    """Widoczny dla uzytkownika komunikat o zmianie w pamieci agenta."""
    return f"[PAMIEC] {what}"


def _diff(old: str, new: str, max_lines: int = 40) -> str:
    import difflib
    if not old.strip():
        body = new.strip().splitlines()[:max_lines]
        return tr("Nowa tresc:\n", "New content:\n") + as_code("\n".join(body) or tr("(pusta)", "(empty)"))
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     fromfile=tr("obecny", "current"), tofile=tr("nowy", "new"), lineterm=""))
    if len(diff) > max_lines:
        more = len(diff) - max_lines
        diff = diff[:max_lines] + [tr(f"[... i {more} linii roznicy]", f"[... and {more} more diff lines]")]
    return tr("Zmiany:\n", "Changes:\n") + as_code("\n".join(diff) or tr("(brak roznic)", "(no differences)"))


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
            _reply(session, tool_call, memory.read_server_md() or tr("(SERVER.md jeszcze nie istnieje)",
                                                                      "(SERVER.md does not exist yet)"))
            return
        if operation == "update_section":
            section = args.get("section", "")
            document = memory.update_section(memory.read_server_md(), section, args.get("content", ""))
            memory.write_server_md(document)
            await audit.log_file_write(session.interface, f"SERVER.md#{section.strip()}", 0)
            yield _notice(tr(f"zaktualizowano sekcje '{section.strip()}' w SERVER.md",
                             f"updated section '{section.strip()}' in SERVER.md"))
            _reply(session, tool_call, tr(f"Zaktualizowano sekcje '{section.strip()}' w SERVER.md.",
                                          f"Updated section '{section.strip()}' in SERVER.md."))
            return
        if operation == "write":
            # Pelne nadpisanie SERVER.md (dolaczanego do kazdej rozmowy) wymaga potwierdzenia —
            # to najsilniejszy wektor trwalej injekcji. Zmiane widac przed TAK.
            content = memory.check_server_md(str(args.get("content", "")))  # walidacja bez zapisu
            preview = _diff(memory.read_server_md(), content)

            async def do_write(content=content, interface=session.interface) -> str:
                memory.write_server_md(content)
                await audit.log_file_write(interface, "SERVER.md", 0)
                return tr("Nadpisano caly SERVER.md.", "Overwrote the whole SERVER.md.")

            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id, tool_name="server_md",
                command=tr("pelne nadpisanie SERVER.md", "full overwrite of SERVER.md"), classification="confirm",
                action=do_write,
                plan=safety.Plan(local_files=[(str(memory.server_md_path()), tr("pamiec Pipe: SERVER.md",
                                                                                "Pipe memory: SERVER.md"))]),
            )
            yield tr(f"[POTWIERDZ] Nadpisanie calego SERVER.md wymaga potwierdzenia.\n{preview}",
                     f"[POTWIERDZ] Overwriting the whole SERVER.md requires confirmation.\n{preview}")
            return
        _reply(session, tool_call, tr(f"Nieznana operacja {operation!r}. Dostepne: read, update_section, write.",
                                      f"Unknown operation {operation!r}. Available: read, update_section, write."))
    except memory.MemoryWriteError as exc:
        _reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
    except OSError as exc:
        _reply(session, tool_call, tr(f"Blad zapisu SERVER.md: {exc}", f"Error writing SERVER.md: {exc}"))


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
                   or tr("(brak zapisanych skilli)", "(no saved skills)"))
            return
        if operation == "read":
            skill = memory.read_skill(name)
            _reply(session, tool_call, f"# {skill.name}\n{skill.description}\n\n{skill.content}" if skill
                   else tr(f"Brak skilla {name!r}. Dostepne: skill_manage, operation=list.",
                           f"No skill {name!r}. See: skill_manage, operation=list."))
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
                return tr(f"{'Utworzono' if created else 'Zastapiono'} skill '{n}'.",
                          f"{'Created' if created else 'Replaced'} skill '{n}'.")

            verb = tr("Zastapienie" if existing else "Zapis", "Replacing" if existing else "Saving")
            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id, tool_name="skill_manage",
                command=tr(f"zapis skilla {skill_name}", f"saving skill {skill_name}"), classification="confirm",
                action=do_save,
                plan=safety.Plan(local_files=[(str(memory.skills_dir() / skill_name / "SKILL.md"),
                                               tr(f"pamiec Pipe: skill {skill_name}", f"Pipe memory: skill {skill_name}"))]),
            )
            yield tr(f"[POTWIERDZ] {verb} skilla {as_code(skill_name)} wymaga potwierdzenia:\n{preview}",
                     f"[POTWIERDZ] {verb} the skill {as_code(skill_name)} requires confirmation:\n{preview}")
            return
        if operation == "delete":
            if memory.delete_skill(name):
                skill_name = memory.validate_skill_name(name)
                await audit.log_file_write(session.interface, f"skills/{skill_name}/SKILL.md (usuniety)", 0)
                yield _notice(tr(f"usunieto skill '{skill_name}'", f"deleted skill '{skill_name}'"))
                _reply(session, tool_call, tr(f"Usunieto skill '{name}'.", f"Deleted skill '{name}'."))
            else:
                _reply(session, tool_call, tr(f"Brak skilla {name!r} — nic nie usunieto.", f"No skill {name!r} — nothing deleted."))
            return
        _reply(session, tool_call, tr(f"Nieznana operacja {operation!r}. Dostepne: list, read, save, delete.",
                                      f"Unknown operation {operation!r}. Available: list, read, save, delete."))
    except memory.MemoryWriteError as exc:
        _reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
    except OSError as exc:
        _reply(session, tool_call, tr(f"Blad zapisu skilla: {exc}", f"Error writing the skill: {exc}"))


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
            result = memory.render_directory() or tr("(DIRECTORY jest puste — uzyj operation=scan albo upsert)",
                                                     "(DIRECTORY is empty — use operation=scan or upsert)")
        elif operation == "upsert":
            created = memory.upsert_directory(
                str(args.get("path", "")), str(args.get("kind", "other")), str(args.get("description", "")),
                remote=str(args.get("remote", "") or ""), branch=str(args.get("branch", "") or ""),
            )
            path = memory.normalize_dir_path(str(args.get("path", "")))
            await audit.log_file_write(session.interface, f"directory.json#{path}", 0)
            result = tr(f"{'Dodano' if created else 'Zaktualizowano'} {path} w DIRECTORY.",
                        f"{'Added' if created else 'Updated'} {path} in DIRECTORY.")
            notice = _notice(tr(f"{'dodano' if created else 'zaktualizowano'} {path} w DIRECTORY",
                                f"{'added' if created else 'updated'} {path} in DIRECTORY"))
        elif operation == "remove":
            path = str(args.get("path", ""))
            if memory.remove_directory(path):
                result = tr(f"Usunieto {path} z DIRECTORY.", f"Removed {path} from DIRECTORY.")
                normalized = memory.normalize_dir_path(path)
                notice = _notice(tr(f"usunieto {normalized} z DIRECTORY", f"removed {normalized} from DIRECTORY"))
            else:
                result = tr(f"Nie ma {path} w DIRECTORY.", f"{path} is not in DIRECTORY.")
        elif operation == "scan":
            added: list[str] = []
            for repo in await asyncio.to_thread(infra.git_repos):  # os.walk po /home bywa wolny
                if memory.upsert_directory(repo.path, "repo", "", remote=repo.remote, branch=repo.branch,
                                           source="scan", keep_description=True):
                    added.append(repo.path)
            containers = await infra.docker_containers() or []
            for project in sorted({(c.project, c.workdir) for c in containers if c.project and c.workdir}):
                name, workdir = project
                if memory.upsert_directory(workdir, "compose", tr(f"projekt docker compose '{name}'",
                                                                  f"docker compose project '{name}'"),
                                           source="scan", keep_description=True):
                    added.append(workdir)
            await audit.log_file_write(session.interface, "directory.json (skan)", 0)
            entries = memory.load_directory()
            without = [e.path for e in entries if not e.description
                       or e.description.startswith(("projekt docker compose", "docker compose project"))]
            result = (tr(f"Skan zakonczony: nowych wpisow {len(added)}, razem {len(entries)}.\n",
                         f"Scan finished: {len(added)} new entries, {len(entries)} in total.\n")
                      + memory.render_directory(entries)
                      + (tr("\n\nWpisy bez opisu (uzupelnij upsert z description — co to za aplikacja): ",
                            "\n\nEntries without a description (fill in with upsert + description — what app it is): ")
                         + ", ".join(without[:20]) if without else ""))
            notice = _notice(tr(f"skan DIRECTORY: {len(added)} nowych wpisow", f"DIRECTORY scan: {len(added)} new entries"))
        else:
            result = tr(f"Nieznana operacja {operation!r}. Dostepne: list, upsert, remove, scan.",
                        f"Unknown operation {operation!r}. Available: list, upsert, remove, scan.")
    except memory.MemoryWriteError as exc:
        result = tr(f"Blad: {exc}", f"Error: {exc}")
    except OSError as exc:
        result = tr(f"Blad zapisu DIRECTORY: {exc}", f"Error writing DIRECTORY: {exc}")

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
            result = memory.read_vibe(key) or tr("(brak notatki VIBE dla tego uzytkownika)", "(no VIBE note for this user)")
        elif operation == "update":
            memory.write_vibe(key, str(args.get("content", "")))
            await audit.log_file_write(session.interface, f"vibe/{key}.md", 0)
            result = tr("Zaktualizowano VIBE tego uzytkownika.", "Updated this user's VIBE.")
            notice = _notice(tr("zaktualizowano notatke VIBE o stylu rozmowy", "updated the VIBE note on conversation style"))
        else:
            result = tr(f"Nieznana operacja {operation!r}. Dostepne: read, update.",
                        f"Unknown operation {operation!r}. Available: read, update.")
    except memory.MemoryWriteError as exc:
        result = tr(f"Blad: {exc}", f"Error: {exc}")
    except OSError as exc:
        result = tr(f"Blad zapisu VIBE: {exc}", f"Error writing VIBE: {exc}")

    if notice:
        yield notice
    _reply(session, tool_call, result)
