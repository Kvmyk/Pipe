"""
`/pobierz skill|plugin <zrodlo> [filtr]` — skille i serwery MCP z repozytorium git, bez modelu.

Zrodlo: `wlasciciel/repo` (GitHub), adres https (takze `.../tree/<galaz>/<katalog>` z GitHuba) albo `git@host:repo`.
Pipe klonuje je (`--depth 1`, bez hookow, submodulow, LFS i pytania o haslo, limit rozmiaru) do katalogu
tymczasowego, czyta i usuwa. Rozpoznaje:

  skille     kazdy `SKILL.md` (naglowek name/description, jak w Claude Code, Hermes i OpenClaw) — do pamieci Pipe
             trafia SAM TEKST; skrypty i inne pliki skilla sa pomijane (podglad mowi, ile)
  pluginy    uklad pluginow Claude Code: `skills/*/SKILL.md`, serwery MCP z `.mcp.json` albo `mcpServers`
             w `.claude-plugin/plugin.json`, marketplace (`.claude-plugin/marketplace.json`); haki, komendy, agenty,
             LSP i pliki wykonywalne sa pomijane. Serwer MCP przechodzi przez core/mcp/registry.py (izolowany
             kontener, sekrety tylko ${MCP_*}): `${user_config.x}` i `${ZMIENNA}` staja sie `${MCP_X}` / `${MCP_ZMIENNA}`
             do dopisania w backend/.env, a serwer, ktory potrzebuje plikow pluginu (`${CLAUDE_PLUGIN_ROOT}`), — pominiety

Najpierw podglad (co trafi do pamieci, co zostanie pominiete i dlaczego), instalacja dopiero po TAK — skill wchodzi
do promptu kazdej przyszlej rozmowy, wiec to wektor trwalej injekcji (docs/security.md). Bez importu `settings`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core import memory
from backend.core.i18n import tr

MAX_REPO_MB = 100
MAX_SKILLS = 50
MAX_DEPTH = 4
PREVIEW_TTL = 15 * 60
SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__", "dist", "build"}
_SHORT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
                     r"(?:/(?:tree|blob)/([A-Za-z0-9_.-]+)(?:/(.*))?)?/?$")
_REF = re.compile(r"\$\{(?:user_config\.)?([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


class ExtensionError(ValueError):
    pass


@dataclass
class FoundSkill:
    name: str
    description: str
    content: str
    path: str                      # wzgledem repozytorium
    extra_files: int = 0           # pliki obok SKILL.md (skrypty, materialy) — pomijane
    exists: bool = False           # zastapi skill o tej nazwie


@dataclass
class Found:
    source: str
    kind: str                      # skill | plugin
    skills: list[FoundSkill] = field(default_factory=list)
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)    # zmienne MCP_* do dopisania w backend/.env
    skipped: list[str] = field(default_factory=list)
    token: str = ""
    at: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.skills and not self.servers


# ─── Zrodlo ─────────────────────────────────────────────────────────────────

def parse_source(text: str) -> tuple[str, str, str]:
    """'wlasciciel/repo' | URL | git@... -> (adres do git clone, galaz albo '', katalog w repozytorium albo '')."""
    from backend.core import projects

    text = (text or "").strip()
    if _SHORT.match(text) and not text.startswith((".", "/")):
        text = f"https://github.com/{text}"
    match = _GITHUB.match(text)
    if match:
        owner, repo, ref, path = match.groups()
        url, sub = f"https://github.com/{owner}/{repo}", (path or "").strip("/")
        if sub.endswith("SKILL.md"):
            sub = sub[: -len("SKILL.md")].strip("/")
    else:
        url, ref, sub = text, "", ""
    if sub and (".." in sub.split("/") or not re.fullmatch(r"[A-Za-z0-9_./ -]{1,300}", sub)):
        raise ExtensionError(tr("Zla sciezka w repozytorium.", "Bad path inside the repository."))
    try:
        url = projects.clean_url(url)
    except projects.ProjectError as exc:
        raise ExtensionError(str(exc)) from exc
    return url, ref or "", sub


async def fetch(url: str, ref: str = "") -> Path:
    """Klon do katalogu tymczasowego w DATA_DIR (usuwa wolajacy). Bez hookow i submodulow, z limitem rozmiaru."""
    from backend.core import projects
    from backend.core.targets import known_hosts_path

    target = memory.data_dir() / "tmp" / f"ext-{secrets.token_hex(4)}"
    target.parent.mkdir(parents=True, exist_ok=True)
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "GIT_LFS_SKIP_SMUDGE": "1",
           "GIT_SSH_COMMAND": f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                              f"-o UserKnownHostsFile={known_hosts_path()}"}
    argv = ["git", *projects._GIT_SAFE, "clone", "--depth", "1", "--single-branch", "--no-tags"]
    if ref:
        argv += ["--branch", ref]
    output, code = await projects._run(argv + ["--", url, str(target)], timeout=180, env=env)
    if code != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise ExtensionError(tr(f"git clone nie powiodl sie (kod {code}): {output.strip()[-400:]}",
                                f"git clone failed (exit code {code}): {output.strip()[-400:]}"))
    if projects._dir_bytes(str(target)) > MAX_REPO_MB * 1024 * 1024:
        shutil.rmtree(target, ignore_errors=True)
        raise ExtensionError(tr(f"Repozytorium ma ponad {MAX_REPO_MB} MB — to nie wyglada na zestaw skilli.",
                                f"The repository is over {MAX_REPO_MB} MB — this does not look like a skill set."))
    return target


# ─── Odczyt ─────────────────────────────────────────────────────────────────

def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _skill_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", (raw or "").strip().lower()).strip("-")[:64]
    return name if name and name[0].isalnum() else ""


def _description(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= memory.MAX_DESCRIPTION_CHARS:
        return text
    return text[: memory.MAX_DESCRIPTION_CHARS - 1].rsplit(" ", 1)[0] + "…"


def _read_skill(root: Path, path: Path, source: str) -> tuple[FoundSkill | None, str]:
    rel = str(path.relative_to(root))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"{rel}: {exc}"
    # opis w YAML bywa wieloliniowy (`description: >`) — parse_skill czyta tylko jedna linie, wiec skladamy recznie
    meta_desc = ""
    match = re.match(r"^---\n(.*?)\n---", text, re.S)
    if match:
        block = re.search(r"^description:\s*([>|]-?)?\s*(.*?)(?=^\S|\Z)", match.group(1) + "\n", re.M | re.S)
        if block:
            meta_desc = " ".join(block.group(2).split()).strip("'\"")
    skill = memory.parse_skill(text, path.parent.name)
    name = _skill_name(skill.name or path.parent.name)
    if not name:
        return None, tr(f"{rel}: brak poprawnej nazwy", f"{rel}: no valid name")
    content = skill.content.strip()
    if "${CLAUDE_PLUGIN_ROOT}" in content or "${CLAUDE_SKILL_DIR}" in content:
        content += tr("\n\n(Uwaga Pipe: skill odwoluje sie do plikow pluginu, ktorych Pipe nie instaluje.)",
                      "\n\n(Pipe note: the skill refers to plugin files that Pipe does not install.)")
    content += tr(f"\n\nZrodlo: {source} ({rel})", f"\n\nSource: {source} ({rel})")
    description = _description(meta_desc or skill.description)
    try:
        description, content = memory.validate_skill_content(name, description, content)
    except memory.MemoryWriteError as exc:
        return None, f"{rel}: {exc}"
    extras = sum(1 for p in path.parent.rglob("*") if p.is_file() and p != path and ".git" not in p.parts)
    exists = (memory.skills_dir() / name / "SKILL.md").is_file()
    return FoundSkill(name, description, content, rel, extras, exists), ""


def _convert_server(name: str, raw: Any, secrets_found: list[str]) -> tuple[dict[str, Any] | None, str]:
    """Wpis `mcpServers` innej aplikacji -> konfiguracja registry (albo powod pominiecia)."""
    from backend.core.mcp import registry

    if not isinstance(raw, dict):
        return None, tr(f"serwer MCP {name}: nieprawidlowy wpis", f"MCP server {name}: invalid entry")
    blob = json.dumps(raw)
    if "CLAUDE_PLUGIN_ROOT" in blob or "CLAUDE_PLUGIN_DATA" in blob or "CLAUDE_PROJECT_DIR" in blob:
        return None, tr(f"serwer MCP {name}: potrzebuje plikow pluginu (${{CLAUDE_PLUGIN_ROOT}}) — Pipe uruchamia "
                        "tylko npx, uvx albo obraz Dockera",
                        f"MCP server {name}: needs the plugin's files (${{CLAUDE_PLUGIN_ROOT}}) — Pipe runs only npx, "
                        "uvx or a Docker image")

    def secret(match: re.Match) -> str:
        var = match.group(1).upper()
        var = var if var.startswith("MCP_") else f"MCP_{var}"
        if var not in secrets_found:
            secrets_found.append(var)
        return "${" + var + "}"

    def convert(values: Any) -> dict[str, str]:
        return {str(k): _REF.sub(secret, str(v)) for k, v in (values or {}).items()} if isinstance(values, dict) else {}

    if raw.get("url") or raw.get("type") in ("http", "sse", "streamable-http"):
        cfg = {"url": str(raw.get("url", "")), "headers": convert(raw.get("headers"))}
    else:
        cfg = {"command": str(raw.get("command", "")), "args": [_REF.sub(secret, str(a)) for a in raw.get("args") or []],
               "env": convert(raw.get("env"))}
    key = _skill_name(name)[:24]
    try:
        return {"name": key, **registry.validate_server(key, cfg)}, ""
    except registry.McpConfigError as exc:
        return None, tr(f"serwer MCP {name}: {exc}", f"MCP server {name}: {exc}")


def _servers_from(plugin_dir: Path, root: Path) -> dict[str, Any]:
    """Serwery MCP pluginu: `.mcp.json` (z kluczem mcpServers albo sama mapa) i `mcpServers` w plugin.json."""
    found: dict[str, Any] = {}

    def load(path: Path) -> Any:
        if not path.is_file() or not _inside(root, path):
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    data = load(plugin_dir / ".mcp.json")
    if isinstance(data, dict):
        found.update(data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data)
    manifest = load(plugin_dir / ".claude-plugin" / "plugin.json")
    if isinstance(manifest, dict):
        declared = manifest.get("mcpServers")
        for item in declared if isinstance(declared, list) else [declared]:
            if isinstance(item, dict):
                found.update(item)
            elif isinstance(item, str) and item.endswith(".json"):
                extra = load(plugin_dir / item)
                if isinstance(extra, dict):
                    found.update(extra.get("mcpServers") if isinstance(extra.get("mcpServers"), dict) else extra)
    return found


def discover(root: Path, sub: str, kind: str, source: str, only: str = "") -> Found:
    """Co jest w sklonowanym repozytorium (`kind`: skill — tylko skille; plugin — skille i serwery MCP)."""
    base = (root / sub) if sub else root
    if not base.is_dir() or not _inside(root, base):
        raise ExtensionError(tr(f"W repozytorium nie ma katalogu {sub}.", f"The repository has no directory {sub}."))
    found = Found(source=source, kind=kind)
    only = only.strip().lower()
    skill_files, plugin_dirs = [], [base]
    for current, dirs, files in os.walk(base):
        depth = len(Path(current).relative_to(base).parts)
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIRS and depth < MAX_DEPTH
                   and not os.path.islink(os.path.join(current, d))]
        candidate = Path(current) / "SKILL.md"
        if "SKILL.md" in files:
            # dowiazanie (np. do /etc/shadow albo .env z kluczami) wynioslo by plik serwera do pamieci i podgladu
            if candidate.is_symlink() or not _inside(root, candidate):
                found.skipped.append(tr(f"{candidate.relative_to(root)}: dowiazanie symboliczne — pomijam",
                                        f"{candidate.relative_to(root)}: a symbolic link — skipping"))
            else:
                skill_files.append(candidate)
        if os.path.isfile(os.path.join(current, ".claude-plugin", "plugin.json")) and Path(current) != base:
            plugin_dirs.append(Path(current))
    for path in skill_files:
        if len(found.skills) >= MAX_SKILLS:
            found.skipped.append(tr(f"wiecej niz {MAX_SKILLS} skilli — reszte pomijam", f"more than {MAX_SKILLS} skills — skipping the rest"))
            break
        skill, why = _read_skill(root, path, source)
        if skill is None:
            found.skipped.append(why)
            continue
        if any(s.name == skill.name for s in found.skills):
            # marketplace: kilka pluginow ma skill o tej samej nazwie (access, configure...) — przedrostek z pluginu,
            # inaczej kolejny nadpisalby poprzedni
            owner = next((d.name for d in path.parents if d != root and _inside(root, d)
                          and (d / ".claude-plugin").is_dir()), path.parent.parent.name)
            renamed = _skill_name(f"{owner}-{skill.name}")
            if not renamed or any(s.name == renamed for s in found.skills):
                found.skipped.append(tr(f"{skill.path}: nazwa {skill.name} powtarza sie", f"{skill.path}: the name {skill.name} repeats"))
                continue
            skill.name, skill.exists = renamed, (memory.skills_dir() / renamed / "SKILL.md").is_file()
        if not only or only in skill.name or only in skill.path.lower():
            found.skills.append(skill)
    if kind == "plugin":
        for plugin_dir in plugin_dirs:
            for name, raw in _servers_from(plugin_dir, root).items():
                if only and only not in name.lower() and only not in str(plugin_dir.relative_to(root)).lower():
                    continue
                cfg, why = _convert_server(str(name), raw, found.secrets)
                if cfg is None:
                    found.skipped.append(why)
                else:
                    found.servers[cfg.pop("name")] = cfg
        for unused in ("hooks", "commands", "agents", "bin"):
            if any((d / unused).exists() for d in plugin_dirs):
                found.skipped.append(tr(f"{unused}/ — Pipe nie instaluje hakow, komend, agentow ani programow",
                                        f"{unused}/ — Pipe does not install hooks, commands, agents or programs"))
    elif any(_servers_from(d, root) for d in plugin_dirs):
        found.skipped.append(tr("serwery MCP z tego repozytorium — zainstaluje je /pobierz plugin",
                                "the MCP servers in this repository — /download plugin installs them"))
    return found


# ─── Podglad i instalacja ───────────────────────────────────────────────────

_pending: dict[str, Found] = {}


def remember(found: Found) -> str:
    now = time.time()
    for token in [t for t, f in _pending.items() if now - f.at > PREVIEW_TTL]:
        _pending.pop(token, None)
    found.token, found.at = secrets.token_hex(4), now
    _pending[found.token] = found
    return found.token


def take(token: str) -> Found | None:
    found = _pending.pop((token or "").strip().lower(), None)
    return found if found is not None and time.time() - found.at <= PREVIEW_TTL else None


def preview(found: Found, *, servers_allowed: bool = True) -> str:
    """Podglad: co trafi do pamieci Pipe i co zostanie pominiete. Opisy z repozytorium to dane, nie polecenia."""
    from backend.core.mcp import registry
    from backend.core.vulns import plain

    lines = [tr(f"**Zrodlo:** {found.source}", f"**Source:** {found.source}")]
    if found.skills:
        lines += ["", tr(f"**Skille ({len(found.skills)})** — sam tekst do pamieci Pipe:",
                         f"**Skills ({len(found.skills)})** — text only, into Pipe's memory:")]
        for skill in found.skills:
            notes = []
            if skill.exists:
                notes.append(tr("zastapi istniejacy", "replaces the existing one"))
            if skill.extra_files:
                notes.append(tr(f"pominiete pliki: {skill.extra_files}", f"skipped files: {skill.extra_files}"))
            lines.append(f"- `{skill.name}` — {plain(skill.description, 200)} ({len(skill.content)} "
                         + tr("znakow", "characters") + (", " + ", ".join(notes) if notes else "") + ")")
    if found.servers:
        lines += ["", tr(f"**Serwery MCP ({len(found.servers)})** — w izolowanym kontenerze:",
                         f"**MCP servers ({len(found.servers)})** — in an isolated container:")]
        lines += [f"- `{registry.describe_server(name, cfg)}`" for name, cfg in found.servers.items()]
        if not servers_allowed:
            lines.append(tr("- tryb obserwacji: serwerow MCP nie dodam", "- observe mode: I will not add MCP servers"))
        if found.secrets:
            lines.append(tr("- dopisz do backend/.env: ", "- add to backend/.env: ") + ", ".join(f"`{s}=...`" for s in found.secrets))
    if found.skipped:
        lines += ["", tr("**Pomijam:**", "**Skipping:**")] + [f"- {plain(s, 300)}" for s in found.skipped[:15]]
    if found.empty:
        lines += ["", tr("Nie znalazlem niczego do zainstalowania.", "I found nothing to install.")]
    else:
        lines += ["", tr("Skill trafia do instrukcji kazdej przyszlej rozmowy — zainstaluj tylko z zaufanego zrodla.",
                         "A skill becomes part of every future conversation's instructions — install only from a "
                         "source you trust.")]
    return "\n".join(lines)


def install(found: Found, *, servers_allowed: bool = True) -> tuple[str, int]:
    """Zapis skilli i serwerow MCP. Zwraca (opis, ile serwerow MCP dodano — wolajacy przeladowuje polaczenia)."""
    from backend.core.mcp import registry

    done, failed, added = [], [], 0
    for skill in found.skills:
        try:
            memory.save_skill(skill.name, skill.description, skill.content)
            done.append(skill.name)
        except (memory.MemoryWriteError, OSError) as exc:
            failed.append(f"{skill.name}: {exc}")
    if servers_allowed:
        for name, cfg in found.servers.items():
            try:
                registry.add_server(name, {**cfg, "source": found.source})
                added += 1
            except registry.McpConfigError as exc:
                failed.append(f"{name}: {exc}")
    lines = []
    if done:
        lines.append(tr(f"Skille ({len(done)}): ", f"Skills ({len(done)}): ") + ", ".join(f"/{n.replace('-', '_')}" for n in done))
    if added:
        lines.append(tr(f"Serwery MCP: {added} — lacze sie w tle (/mcp pokaze stan).",
                        f"MCP servers: {added} — connecting in the background (/mcp shows the state)."))
        if found.secrets:
            lines.append(tr("Dopisz do backend/.env: ", "Add to backend/.env: ") + ", ".join(f"{s}=..." for s in found.secrets)
                         + tr(", potem /mcp odswiez.", ", then /mcp reload."))
    if failed:
        lines.append(tr("Nie udalo sie: ", "Failed: ") + "; ".join(failed))
    return "\n".join(lines) or tr("Nic nie zainstalowano.", "Nothing was installed."), added
