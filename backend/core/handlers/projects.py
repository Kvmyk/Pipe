"""
Handlery warsztatu (`project`, core/projects.py) i narzedzi na zadanie (`load_tools`, core/toolsets.py).

W warsztacie Pipe dziala sam: klonowanie, rozpoznanie, komendy w izolowanym kontenerze, logi, zatrzymanie i
wznowienie nie pytaja o zgode (WORKSPACE_AUTONOMY=1). Wyjatki:
  - `run` (usluga na stale, porty, zasoby serwera) i `remove` (usuwa katalog) — jedno potwierdzenie z planem,
  - `exec`, gdy w tej turze model czytal tresc z zewnatrz (README sklonowanego repozytorium, strona z internetu):
    jak przy YOLO — prompt injection nie moze sam uruchomic kodu z dostepem do sieci,
  - tryb obserwacji i rola viewer — odmowa z gory (agent.py, OBSERVE/VIEWER_WRITE_OPERATIONS).
"""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator

from backend.config import settings
from backend.core import hostexec, projects, runtime, toolsets
from backend.core.events import Event, Progress
from backend.core.handlers.common import reply
from backend.core.i18n import tr
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code

_SIZE = re.compile(r"^\d+(\.\d+)?[kmgKMG]?$")
_CPUS = re.compile(r"^\d+(\.\d+)?$")


def config() -> projects.Config:
    """Konfiguracja warsztatu z .env (bledne limity — domyslne)."""
    return projects.Config(
        workspace=settings.WORKSPACE_DIR, autonomy=settings.WORKSPACE_AUTONOMY,
        memory=settings.PROJECT_MEMORY if _SIZE.match(settings.PROJECT_MEMORY) else "1g",
        cpus=settings.PROJECT_CPUS if _CPUS.match(settings.PROJECT_CPUS) else "1",
        exec_timeout=max(30, settings.PROJECT_EXEC_TIMEOUT), build_timeout=max(120, settings.PROJECT_BUILD_TIMEOUT),
        max_mb=max(10, settings.PROJECT_MAX_MB))


def _need(project: projects.Project | None, name: str) -> projects.Project:
    if project is None:
        raise projects.ProjectError(tr(f"Nie ma projektu {name!r} w warsztacie (project list).",
                                       f"There is no project {name!r} in the workshop (project list)."))
    return project


async def project_list() -> str:
    found = projects.load()
    if not found:
        return tr(f"Warsztat jest pusty ({settings.WORKSPACE_DIR}). Sklonuj repozytorium (clone) albo zaloz projekt "
                  "na skrypty (create).",
                  f"The workshop is empty ({settings.WORKSPACE_DIR}). Clone a repository (clone) or create a project "
                  "for scripts (create).")
    states = await projects.statuses(found)
    lines = [tr(f"Warsztat {settings.WORKSPACE_DIR}:", f"Workshop {settings.WORKSPACE_DIR}:")]
    for project in found:
        state = states.get(project.name, {})
        status = {"running": tr("dziala", "running"), "stopped": tr("zatrzymany", "stopped"),
                  "partial": tr("czesciowo", "partly running"), "none": tr("nie uruchomiony", "not started")}.get(
            state.get("status", "none"), state.get("status", ""))
        url = project.run.get("url") if state.get("status") in ("running", "partial") else ""
        lines.append(f"- {project.name} [{', '.join(project.stack) or '-'}] {status}"
                     + (f" — {url}" if url else "") + (f" ({project.url})" if project.url else ""))
    return "\n".join(lines)


async def handle_project(agent: Any, session: Session, tool_call: Any, args: dict[str, Any]) -> AsyncGenerator[Event, None]:
    operation = str(args.get("operation", "") or "").strip().lower()
    name = str(args.get("name", "") or "").strip().lower()
    cfg = config()
    try:
        if operation == "list":
            reply(session, tool_call, await project_list())
            return
        if operation in ("create", "clone", "pull"):
            reason = projects.blocker(docker=False)
        elif operation in ("run", "exec", "stop", "start", "logs", "remove"):
            reason = projects.blocker(docker=True)
        elif operation == "inspect":
            reason = ""
        else:
            reply(session, tool_call, tr(f"Nieznana operacja {operation!r}.", f"Unknown operation {operation!r}."))
            return
        if reason:
            reply(session, tool_call, tr(f"Nie wykonam: {reason}", f"Not done: {reason}"))
            return
        projects.check_workspace(cfg.workspace)

        if operation == "create":
            project = projects.create(cfg, name)
            yield Progress(tr(f"warsztat: projekt {project.name} — {project.path}", f"workshop: project {project.name} — {project.path}"))
            reply(session, tool_call, tr(
                f"Projekt {project.name} gotowy: {project.path}. Pliki zapisuj write_file pod ta sciezka (bez pytania), "
                "a uruchamiaj exec (izolowany kontener, katalog projektu jako /work).",
                f"Project {project.name} is ready: {project.path}. Write files with write_file under this path (no "
                "confirmation) and run them with exec (isolated container, the project directory as /work)."))
            return

        if operation == "clone":
            url = str(args.get("url", "") or "").strip()

            async def do_clone() -> str:
                project = await projects.clone(cfg, url, name)
                profile = projects.detect(runtime.to_local(project.path))
                _remember(project, profile)
                session.web_tainted = True        # README i AGENTS.md to tresc z zewnatrz
                return tr("Sklonowano.\n", "Cloned.\n") + projects.describe_profile(project, profile)

            if cfg.autonomy:
                yield Progress(tr(f"klonuje {url}", f"cloning {url}"), source="projekt")
                reply(session, tool_call, await do_clone())
            else:
                target = projects.project_path(cfg, name or projects.name_from_url(projects.clean_url(url)))
                session.pending_confirmation = ConfirmationRequest(
                    tool_call_id=tool_call.id, tool_name="project", command=f"git clone {url} {target}",
                    classification="confirm", action=do_clone)
                yield tr(f"[POTWIERDZ] Klonowanie repozytorium wymaga potwierdzenia: {as_code(f'git clone {url} {target}')}",
                         f"[POTWIERDZ] Cloning the repository requires confirmation: {as_code(f'git clone {url} {target}')}")
            return

        project = _need(projects.get(name), name)

        if operation == "inspect":
            profile = projects.detect(runtime.to_local(project.path))
            project.stack = profile.stack
            projects.upsert(project)
            session.web_tainted = True
            reply(session, tool_call, projects.describe_profile(project, profile))
            return

        if operation == "pull":
            yield Progress(tr(f"git pull: {project.name}", f"git pull: {project.name}"), source="projekt")
            output = await projects.pull(cfg, project)
            session.web_tainted = True
            reply(session, tool_call, tr(f"Zaktualizowano {project.name}:\n{output}", f"Updated {project.name}:\n{output}"))
            return

        if operation == "logs":
            reply(session, tool_call, await projects.logs(project))
            return

        if operation in ("stop", "start"):
            running = operation == "start"
            if cfg.autonomy:
                yield Progress(tr(f"{'wznawiam' if running else 'zatrzymuje'} {project.name}",
                                  f"{'starting' if running else 'stopping'} {project.name}"), source="projekt")
                reply(session, tool_call, await projects.set_running(project, running))
                return

            async def toggle() -> str:
                return await projects.set_running(project, running)

            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id, tool_name="project", command=f"docker {operation} {project.container}",
                classification="confirm", action=toggle)
            yield tr(f"[POTWIERDZ] Operacja wymaga potwierdzenia: {as_code(f'docker {operation} {project.container}')}",
                     f"[POTWIERDZ] Operation requires confirmation: {as_code(f'docker {operation} {project.container}')}")
            return

        if operation == "exec":
            command = str(args.get("command", "") or "").strip()
            image = projects.exec_image(project, str(args.get("image", "") or "").strip())

            async def run_exec() -> str:
                output, code = await projects.sandbox_exec(cfg, project, command, image=image)
                return f"{output}\nEXIT CODE: {code}"

            if not command:
                reply(session, tool_call, tr("Blad: pusta komenda.", "Error: empty command."))
                return
            if cfg.autonomy and not session.web_tainted:
                yield Progress(tr(f"piaskownica {project.name} ({image}): {command[:160]}",
                                  f"sandbox {project.name} ({image}): {command[:160]}"), source="projekt")
                reply(session, tool_call, await run_exec())
                return
            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id, tool_name="project", command=f"[{project.name}/{image}] {command}",
                classification="confirm", action=run_exec)
            why = tr("w tej turze czytalem tresc z zewnatrz (repozytorium albo internet) — kod uruchomie dopiero po Twojej zgodzie",
                     "in this turn I read content from outside (a repository or the internet) — I will run code only "
                     "after your approval") if session.web_tainted else \
                tr("autonomia warsztatu jest wylaczona (WORKSPACE_AUTONOMY=0)", "workshop autonomy is off (WORKSPACE_AUTONOMY=0)")
            yield tr(f"[POTWIERDZ] Komenda w izolowanym kontenerze projektu {project.name} wymaga potwierdzenia: "
                     f"{as_code(command)}\nObraz {image}, widzi tylko {project.path} (jako /work). Powod pytania: {why}.",
                     f"[POTWIERDZ] A command in the isolated container of project {project.name} requires confirmation: "
                     f"{as_code(command)}\nImage {image}, it sees only {project.path} (as /work). Why I ask: {why}.")
            return

        if operation == "run":
            profile = projects.detect(runtime.to_local(project.path))
            image_ref = ""
            if runtime.kind() == "docker" and (args.get("strategy") in ("compose",) or
                                               (args.get("strategy") in (None, "", "auto") and profile.strategy == "compose")):
                reason = await hostexec.blocker()
                if reason:
                    reply(session, tool_call, tr(f"Nie uruchomie compose na hoscie: {reason}", f"Cannot run compose on the host: {reason}"))
                    return
                image_ref = await hostexec.image()
            try:
                port = int(args.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            plan = projects.plan_run(cfg, project, profile, strategy=str(args.get("strategy") or ""), port=port,
                                     image_ref=image_ref)
            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id, tool_name="project", command=f"project run {project.name} ({plan.strategy})",
                classification="confirm", auto=cfg.autonomy or None,
                stream=lambda: projects.run_stream(cfg, project, plan, interface=session.interface))
            main = plan.steps[-1]
            shown = main.shell or " ".join(main.argv)
            yield tr(f"[POTWIERDZ] Uruchomienie projektu {project.name} wymaga potwierdzenia: {as_code(shown)}\n",
                     f"[POTWIERDZ] Starting project {project.name} requires confirmation: {as_code(shown)}\n") + plan.describe()
            return

        if operation == "remove":
            image_ref = await hostexec.image() if runtime.kind() == "docker" and project.run.get("compose") else ""

            async def do_remove() -> str:
                return await projects.remove(cfg, project, image_ref=image_ref)

            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id, tool_name="project", command=f"project remove {project.name}",
                classification="confirm", action=do_remove)
            yield tr(f"[POTWIERDZ] Usuniecie projektu wymaga potwierdzenia: {as_code(project.path)}\n"
                     "Zniknie katalog projektu, jego kontenery i zbudowany obraz.",
                     f"[POTWIERDZ] Removing the project requires confirmation: {as_code(project.path)}\n"
                     "The project directory, its containers and the built image will be gone.")
            return
    except projects.ProjectError as exc:
        reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
        return
    yield  # noqa: unreachable — async generator


def _remember(project: projects.Project, profile: projects.Profile) -> None:
    """Sklonowane repozytorium trafia do DIRECTORY — agent i uzytkownik wiedza, gdzie lezy."""
    from backend.core import memory

    first = next((line.strip("# ").strip() for line in profile.readme.splitlines() if line.strip()), "")
    description = tr(f"Projekt warsztatu Pipe ({', '.join(profile.stack) or 'nieznany stos'})",
                     f"Pipe workshop project ({', '.join(profile.stack) or 'unknown stack'})")
    if first and not memory.find_secret(first):
        description += f": {first[:160]}"
    try:
        memory.upsert_directory(project.path, "repo", description[:290], remote=project.url, source="agent")
    except (memory.MemoryWriteError, OSError):
        pass


async def handle_load_tools(agent: Any, session: Session, tool_call: Any, args: dict[str, Any]) -> AsyncGenerator[Event, None]:
    groups = args.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",")]
    wanted = [str(g).strip().lower() for g in groups if isinstance(g, str)]
    if settings.WEB_SEARCH == "off" and "web" in wanted:
        wanted.remove("web")
    from backend.core.mcp.registry import get_manager
    servers = {toolsets.MCP_PREFIX + n for n, st in get_manager().servers.items() if st.status == "connected"}
    valid = [g for g in wanted if (g in toolsets.GROUPS and g != toolsets.CORE) or g in servers]
    if not valid:
        reply(session, tool_call, tr("Nie ma takich grup. Dostepne: " + ", ".join(toolsets.inactive(session) or ["-"]),
                                     "No such groups. Available: " + ", ".join(toolsets.inactive(session) or ["-"])))
        return
    toolsets.activate(session, valid)
    names = [tool for group in valid for tool in toolsets.GROUPS.get(group, ())] + \
        [f"{group} ({tr('narzedzia serwera MCP', 'MCP server tools')})" for group in valid if group in servers]
    reply(session, tool_call, tr(f"Wlaczone: {', '.join(names)}. Pelne opisy masz od nastepnego kroku — wywolaj potrzebne "
                                 "narzedzie.", f"Enabled: {', '.join(names)}. You have their full descriptions from the "
                                 "next step — call the tool you need."))
    return
    yield  # noqa: unreachable — async generator
